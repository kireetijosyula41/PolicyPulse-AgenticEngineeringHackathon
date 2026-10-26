from __future__ import annotations

from app.agent.model_client import FixtureModelClient
from app.agent.runtime import InvestigationRuntime
from app.agent.schemas import InvestigationRequest

from test_agent_tools import FakeService


def request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_id="investigation-1",
        trigger_kind="clinical_event",
        authorized_policy_sources=["uhc-mri"],
        policy_change_id="watch-1",
        clinical_event_reference="trigger-1",
        validated_clinical_event={
            "trigger_id": "trigger-1",
            "hook": "order-sign",
            "payer": "UHC",
            "plan": "commercial",
            "procedure": "MRI lumbar spine",
            "procedure_codes": ["72148"],
            "provider_action": "order-sign MRI",
        },
        question="Investigate the policy change and documentation impact.",
        token_budget=500,
    )


def test_fixture_runtime_makes_dependent_multi_step_calls():
    service = FakeService()
    req = request()
    fixture = FixtureModelClient(req)
    result = InvestigationRuntime(service, model_client=fixture, model="fixture-model").run(req)

    assert result.provider == "fixture"
    assert result.report.status == "complete"
    assert result.skill_name == "policy-change-investigation"
    assert result.skill_version == "1.0.0"
    assert [call.tool_name for call in result.tool_calls] == [
        "get_policy_change",
        "get_policy_diff",
        "get_documentation_checklist",
    ]
    # The fixture's second and third calls use the watch_id observed in the
    # preceding tool result, rather than a hard-coded independent path.
    assert fixture.calls[1]["arguments"]["watch_id"] == fixture.observed_tool_results[0]["data"]["watch_id"]
    assert fixture.calls[2]["arguments"]["watch_id"] == fixture.observed_tool_results[0]["data"]["watch_id"]
    assert fixture.calls[2]["arguments"]["clinical_trigger"]["trigger_id"] == "trigger-1"
    assert any(event[0] == "investigation_tool_call" for event in service.logged)


def test_runtime_rejects_unknown_fixture_tool_without_execution():
    service = FakeService()
    req = request()

    class UnknownToolFixture(FixtureModelClient):
        def create(self, **kwargs):
            if not self.calls:
                call = self._call("not_registered", {})
                return type("Response", (), {"id": "unknown", "output": [call], "usage": {"total_tokens": 1}})()
            return self._final_response()

    result = InvestigationRuntime(service, model_client=UnknownToolFixture(req), model="fixture-model").run(req)
    assert result.tool_calls[0].outcome == "rejected"
    assert result.tool_calls[0].error_code == "INVALID_ARGUMENT"
    assert not any(event[0] == "tool_executed" for event in service.logged)

