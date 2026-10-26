from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.registry import ToolRegistry
from app.agent.schemas import (
    ClinicalRelevanceArgs,
    ListTrustedPolicySourcesArgs,
    ToolAuthContext,
)
from app.models import AgentEvent, ActivePolicyChange, PolicyChangeRecord, PolicySnapshot, TrustedSource


ROOT = Path(__file__).resolve().parents[1]


def context(*source_ids: str) -> ToolAuthContext:
    return ToolAuthContext(
        principal_id="test-principal",
        scopes=frozenset({"agent:read"}),
        allowed_source_ids=frozenset(source_ids),
        purpose="unit test",
    )


class FakeStore:
    def __init__(self, snapshots=None, records=None, events=None):
        self.snapshots = snapshots or []
        self.records = records or []
        self.events = events or []

    def get_recent_snapshots_for_source(self, source_id, limit=2):
        return [item for item in self.snapshots if item["source_id"] == source_id][:limit]

    def get_recent_events(self, limit=100):
        return self.events[:limit]


class FakeService:
    def __init__(self):
        self.logged = []
        self.source = TrustedSource(
            source_id="uhc-mri",
            payer="UHC",
            plan="commercial",
            procedure_keywords=["MRI lumbar spine"],
            approved_url="https://payer.example/policy",
            baseline_fixture="old.txt",
            latest_fixture="new.txt",
        )
        self.change = ActivePolicyChange.model_validate(
            {
                "watch_id": "watch-1",
                "source_id": "uhc-mri",
                "payer": "UHC",
                "plan_type": "commercial",
                "procedure_keywords": ["MRI lumbar spine"],
                "diff": {
                    "source_id": "uhc-mri",
                    "payer": "UHC",
                    "previous_hash": "old",
                    "current_hash": "new",
                    "changed_lines": ["+prior authorization required", "+clinical notes"],
                    "summary": "2 changed policy lines detected.",
                    "has_changes": True,
                },
                "materiality": {
                    "source_id": "uhc-mri",
                    "classification": "material",
                    "confidence": 0.9,
                    "explanation": "documentation changed",
                    "impacted_keywords": ["documentation"],
                },
                "documentation_required": ["Clinical notes"],
            }
        )
        self.store = FakeStore(
            snapshots=[
                {
                    "source_id": "uhc-mri",
                    "payer": "UHC",
                    "content_hash": "hash-1",
                    "normalized_content": "stored policy content " * 200,
                    "timestamp": "2026-10-25T12:00:00+00:00",
                    "via": "nimble_fixture_fallback",
                }
            ],
            records=[
                PolicyChangeRecord(
                    change_id="change-1",
                    watch_id="watch-1",
                    policy_id="uhc-mri",
                    payer="UHC",
                    change_type="documentation_requirement_change",
                    materiality="material",
                    summary="documentation changed",
                    source_url="https://payer.example/policy",
                    diff_text="+clinical notes",
                    payload=self.change.model_dump(mode="json"),
                ).model_dump(mode="json")
            ],
            events=[
                {
                    "timestamp": "2026-10-25T12:00:00+00:00",
                    "event_type": "ehr_event_recorded",
                    "payload": {"source_id": "uhc-mri", "patient_context": {"patient_id": "secret"}, "procedure": "MRI"},
                }
            ],
        )

    def _read_sources(self):
        return [self.source]

    def get_active_policy_changes(self):
        return [self.change]

    def get_recent_policy_changes(self, limit=25):
        return self.store.records[:limit]

    def log_event(self, event_type, payload):
        self.logged.append((event_type, payload))


def test_schema_rejects_unknown_or_unbounded_arguments():
    with pytest.raises(Exception):
        ListTrustedPolicySourcesArgs(limit=101)
    with pytest.raises(Exception):
        ClinicalRelevanceArgs.model_validate({"clinical_trigger": {}, "unexpected": True})


def test_registry_requires_trusted_auth_context():
    registry = ToolRegistry(FakeService())
    result = registry.execute(
        "list_trusted_policy_sources",
        {"limit": 10},
        ToolAuthContext(
            principal_id="no-scope",
            scopes=frozenset({"agent:write"}),
            allowed_source_ids=frozenset({"uhc-mri"}),
            purpose="test",
        ),
    )
    assert result.ok is False
    assert result.error.code == "FORBIDDEN"


def test_source_allowlisting_and_invalid_arguments():
    registry = ToolRegistry(FakeService())
    denied = registry.execute("get_policy_document", {"source_id": "other"}, context("uhc-mri"))
    assert denied.error.code == "FORBIDDEN"
    invalid = registry.execute("get_policy_document", {"source_id": "uhc-mri", "max_chars": 10}, context("uhc-mri"))
    assert invalid.error.code == "INVALID_ARGUMENT"


def test_stored_document_is_bounded_and_does_not_fetch_remote_policy():
    registry = ToolRegistry(FakeService())
    result = registry.execute("get_policy_document", {"source_id": "uhc-mri", "max_chars": 100}, context("uhc-mri"))
    assert result.ok is True
    assert len(result.data["content"]) == 100
    assert result.data["truncated"] is True
    assert result.data["via"] == "nimble_fixture_fallback"


def test_malformed_provider_output_is_structured():
    service = FakeService()
    service.get_recent_policy_changes = lambda limit=25: [{"not_a_policy_record": True}]
    result = ToolRegistry(service).execute("get_recent_policy_changes", {"limit": 10}, context("uhc-mri"))
    assert result.ok is False
    assert result.error.code == "INVALID_PROVIDER_OUTPUT"
    assert result.audit.tool_name == "get_recent_policy_changes"


def test_clinical_relevance_uses_deterministic_core_and_no_patient_fields():
    args = {
        "clinical_trigger": {
            "trigger_id": "trigger-1",
            "hook": "order-sign",
            "payer": "UHC",
            "plan": "commercial",
            "procedure": "MRI lumbar spine",
            "procedure_codes": ["72148"],
            "provider_action": "order-sign MRI",
        },
        "watch_id": "watch-1",
    }
    result = ToolRegistry(FakeService()).execute("evaluate_clinical_relevance", args, context("uhc-mri"))
    assert result.ok is True
    assert result.data["decision"]["route"] == "crd_guidance_ready"


def test_audit_events_are_redacted_and_bounded():
    result = ToolRegistry(FakeService()).execute("get_recent_audit_events", {"limit": 1}, context("uhc-mri"))
    assert result.ok is True
    payload = result.data["items"][0]["payload"]
    assert "patient_context" not in payload
    assert "patient_id" not in payload
    assert result.audit.outcome == "success"


def test_audit_payload_size_is_bounded():
    service = FakeService()
    service.store.events = [
        {
            "timestamp": "2026-10-25T12:00:00+00:00",
            "event_type": "large_event",
            "payload": {"source_id": "uhc-mri", "large": "x" * 10000},
        }
    ]
    result = ToolRegistry(service).execute("get_recent_audit_events", {"limit": 1}, context("uhc-mri"))
    assert result.ok is True
    assert result.data["items"][0]["payload"]["truncated"] is True

