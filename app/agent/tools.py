from __future__ import annotations

import time
from typing import Any

from app.core.crd_dtr import evaluate_crd_opportunity, evaluate_dtr_requirements
from app.models import ActivePolicyChange, ClinicalTriggerEvent, PolicyChangeRecord, TrustedSource

from .schemas import (
    ClinicalRelevanceArgs,
    ClinicalRelevanceResponse,
    DocumentationChecklistArgs,
    DocumentationChecklistResponse,
    ListTrustedPolicySourcesArgs,
    PolicyChangeArgs,
    PolicyDiffArgs,
    PolicyDocumentArgs,
    RecentAuditEventsArgs,
    RecentAuditEventsResponse,
    RecentPolicyChangesArgs,
    RecentPolicyChangesResponse,
    StoredPolicyDocument,
    ToolAuthContext,
)

def _check_scope(context: ToolAuthContext) -> None:
    if "agent:read" not in context.scopes:
        raise PermissionError("agent:read scope is required")


def _check_source(context: ToolAuthContext, source_id: str) -> None:
    if source_id not in context.allowed_source_ids:
        raise PermissionError(f"Source {source_id!r} is not allowlisted for this principal")


def _clinical_trigger(value) -> ClinicalTriggerEvent:
    # Deliberately omit patient_context and raw_payload: this tool accepts only
    # the minimum clinical workflow fields needed by deterministic CRD logic.
    return ClinicalTriggerEvent(
        trigger_id=value.trigger_id,
        hook=value.hook,
        payer=value.payer,
        plan=value.plan,
        procedure=value.procedure,
        procedure_codes=value.procedure_codes,
        provider_action=value.provider_action,
    )


def _active_changes(service, context: ToolAuthContext) -> list[ActivePolicyChange]:
    return [item for item in service.get_active_policy_changes() if item.source_id in context.allowed_source_ids]


def list_trusted_policy_sources(
    service, args: ListTrustedPolicySourcesArgs, context: ToolAuthContext
) -> list[TrustedSource]:
    _check_scope(context)
    sources = [item for item in service._read_sources() if item.source_id in context.allowed_source_ids]
    return sources[: args.limit]


def get_policy_change(service, args: PolicyChangeArgs, context: ToolAuthContext) -> ActivePolicyChange:
    _check_scope(context)
    for item in _active_changes(service, context):
        if item.watch_id == args.watch_id:
            return item.model_copy(
                update={
                    "diff": item.diff.model_copy(
                        update={"changed_lines": item.diff.changed_lines[: args.max_changed_lines]}
                    )
                }
            )
    raise LookupError(f"Policy change {args.watch_id!r} was not found")


def get_policy_diff(service, args: PolicyDiffArgs, context: ToolAuthContext):
    _check_scope(context)
    change = get_policy_change(
        service,
        PolicyChangeArgs(watch_id=args.watch_id, max_changed_lines=args.max_changed_lines),
        context,
    )
    return change.diff.model_copy(update={"changed_lines": change.diff.changed_lines[: args.max_changed_lines]})


def get_recent_policy_changes(
    service, args: RecentPolicyChangesArgs, context: ToolAuthContext
) -> RecentPolicyChangesResponse:
    _check_scope(context)
    records: list[PolicyChangeRecord] = []
    for raw in service.get_recent_policy_changes(limit=args.limit):
        record = PolicyChangeRecord.model_validate(raw)
        if record.policy_id in context.allowed_source_ids:
            records.append(record)
    return RecentPolicyChangesResponse(items=records[: args.limit])


def get_policy_document(service, args: PolicyDocumentArgs, context: ToolAuthContext) -> StoredPolicyDocument:
    """Return a stored snapshot; intentionally does not call Nimble or fetch remotely."""
    _check_scope(context)
    _check_source(context, args.source_id)
    snapshots = service.store.get_recent_snapshots_for_source(args.source_id, limit=2)
    if len(snapshots) <= args.snapshot_index:
        raise LookupError(f"Stored policy snapshot for {args.source_id!r} was not found")
    raw = snapshots[args.snapshot_index]
    content = raw["normalized_content"]
    return StoredPolicyDocument(
        source_id=raw["source_id"],
        payer=raw["payer"],
        content_hash=raw["content_hash"],
        content=content[: args.max_chars],
        timestamp=raw["timestamp"],
        via=raw["via"],
        truncated=len(content) > args.max_chars,
    )


def evaluate_clinical_relevance(
    service, args: ClinicalRelevanceArgs, context: ToolAuthContext
) -> ClinicalRelevanceResponse:
    _check_scope(context)
    trigger = _clinical_trigger(args.clinical_trigger)
    candidates = _active_changes(service, context)
    if args.watch_id:
        candidates = [item for item in candidates if item.watch_id == args.watch_id]
        if not candidates:
            raise LookupError(f"Policy change {args.watch_id!r} was not found")
    decision = evaluate_crd_opportunity(trigger, candidates)
    return ClinicalRelevanceResponse(
        decision=decision,
        candidate_watch_ids=[item.watch_id for item in candidates],
    )


def get_documentation_checklist(
    service, args: DocumentationChecklistArgs, context: ToolAuthContext
) -> DocumentationChecklistResponse:
    _check_scope(context)
    trigger = _clinical_trigger(args.clinical_trigger)
    change = get_policy_change(service, PolicyChangeArgs(watch_id=args.watch_id), context)
    decision = evaluate_crd_opportunity(trigger, [change])
    dtr = evaluate_dtr_requirements(decision, trigger, change)
    return DocumentationChecklistResponse(dtr_request=dtr)


def _redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {"patient_context", "raw_payload", "patient_id", "mrn", "dob", "ssn"}
    redacted = {key: value for key, value in payload.items() if key not in blocked}
    # Audit payloads are evidence metadata, not an unbounded document channel.
    if len(str(redacted)) > 8000:
        return {"truncated": True, "available_keys": sorted(redacted)[:100]}
    return redacted


def get_recent_audit_events(
    service, args: RecentAuditEventsArgs, context: ToolAuthContext
) -> RecentAuditEventsResponse:
    _check_scope(context)
    events = []
    for raw in service.store.get_recent_events(limit=args.limit):
        payload = raw.get("payload", {})
        source_id = payload.get("source_id")
        # Global operational events have no source_id and are safe metadata;
        # source-scoped events require the trusted source allowlist.
        if source_id and source_id not in context.allowed_source_ids:
            continue
        events.append({
            "timestamp": raw["timestamp"],
            "event_type": raw["event_type"],
            "payload": _redact_event_payload(payload),
        })
    return RecentAuditEventsResponse(items=events[: args.limit])

