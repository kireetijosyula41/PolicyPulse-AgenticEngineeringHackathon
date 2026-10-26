from __future__ import annotations

import time
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin

from pydantic import BaseModel, ValidationError

from . import tools
from .schemas import (
    ClinicalRelevanceArgs,
    ClinicalRelevanceResponse,
    DocumentationChecklistArgs,
    DocumentationChecklistResponse,
    ExecutionAudit,
    ListTrustedPolicySourcesArgs,
    PolicyChangeArgs,
    PolicyDiffArgs,
    PolicyDocumentArgs,
    RecentAuditEventsArgs,
    RecentAuditEventsResponse,
    RecentPolicyChangesArgs,
    RecentPolicyChangesResponse,
    SourceArgs,
    StoredPolicyDocument,
    ToolAuthContext,
    ToolError,
    ToolExecutionEnvelope,
)
from app.models import ActivePolicyChange, PolicyDiff, TrustedSource


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    response_model: type[BaseModel] | Any
    handler: Callable[..., Any]
    side_effects: str
    max_seconds: float = 2.0
    max_result_bytes: int = 50000


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_trusted_policy_sources",
        "List approved payer policy sources visible to the trusted caller.",
        ListTrustedPolicySourcesArgs,
        list[TrustedSource],
        tools.list_trusted_policy_sources,
        "read_only_stored_configuration",
    ),
    ToolSpec(
        "get_policy_change",
        "Retrieve one allowlisted active material or ambiguous policy change by watch ID.",
        PolicyChangeArgs,
        ActivePolicyChange,
        tools.get_policy_change,
        "read_only_stored_state",
    ),
    ToolSpec(
        "get_policy_diff",
        "Retrieve bounded changed lines for one allowlisted policy watch.",
        PolicyDiffArgs,
        PolicyDiff,
        tools.get_policy_diff,
        "read_only_stored_state",
    ),
    ToolSpec(
        "get_recent_policy_changes",
        "Retrieve a bounded, allowlisted view of recent persisted policy-change records.",
        RecentPolicyChangesArgs,
        RecentPolicyChangesResponse,
        tools.get_recent_policy_changes,
        "read_only_stored_state",
    ),
    ToolSpec(
        "get_policy_document",
        "Retrieve a bounded stored policy snapshot; this does not fetch a remote policy.",
        PolicyDocumentArgs,
        StoredPolicyDocument,
        tools.get_policy_document,
        "read_only_stored_snapshot",
    ),
    ToolSpec(
        "evaluate_clinical_relevance",
        "Evaluate deterministic CRD relevance for minimum necessary clinical workflow fields.",
        ClinicalRelevanceArgs,
        ClinicalRelevanceResponse,
        tools.evaluate_clinical_relevance,
        "read_only_deterministic_computation",
    ),
    ToolSpec(
        "get_documentation_checklist",
        "Derive the documentation checklist from an allowlisted policy change without querying Senso.",
        DocumentationChecklistArgs,
        DocumentationChecklistResponse,
        tools.get_documentation_checklist,
        "read_only_deterministic_computation",
    ),
    ToolSpec(
        "get_recent_audit_events",
        "Retrieve bounded, redacted audit metadata for allowlisted sources.",
        RecentAuditEventsArgs,
        RecentAuditEventsResponse,
        tools.get_recent_audit_events,
        "read_only_audit_query",
    ),
)

_SPECS = {spec.name: spec for spec in TOOL_SPECS}


class ToolRegistry:
    """Static registry; model output can select a name but cannot add tools."""

    def __init__(self, service) -> None:
        self.service = service

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
                "side_effects": spec.side_effects,
            }
            for spec in TOOL_SPECS
        ]

    def execute(self, name: str, arguments: dict[str, Any], context: ToolAuthContext) -> ToolExecutionEnvelope:
        started = time.monotonic()
        spec = _SPECS.get(name)
        if spec is None:
            return self._error(name, context, "INVALID_ARGUMENT", "Unknown tool", started)
        try:
            args = spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            return self._error(name, context, "INVALID_ARGUMENT", "Tool arguments failed validation", started, {"errors": exc.errors()})

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(spec.handler, self.service, args, context)
        try:
            data = future.result(timeout=spec.max_seconds)
            if get_origin(spec.response_model) is list:
                item_model = get_args(spec.response_model)[0]
                if not isinstance(data, list):
                    raise TypeError("provider returned a non-list result")
                data = [item_model.model_validate(item) for item in data]
            else:
                data = spec.response_model.model_validate(data)
            payload = data if isinstance(data, dict) else {"items": data} if isinstance(data, list) else data.model_dump(mode="json")
            if len(json.dumps(payload, separators=(",", ":"), default=str)) > spec.max_result_bytes:
                raise ValueError("tool result exceeded the configured size limit")
            return ToolExecutionEnvelope(
                tool_name=name,
                ok=True,
                data=payload,
                audit=ExecutionAudit(tool_name=name, duration_ms=_duration_ms(started), outcome="success", principal_id=context.principal_id),
            )
        except FutureTimeoutError:
            future.cancel()
            return self._error(name, context, "RETRYABLE", "Tool execution exceeded its time budget", started, outcome="timeout")
        except PermissionError as exc:
            return self._error(name, context, "FORBIDDEN", str(exc), started)
        except LookupError as exc:
            return self._error(name, context, "NOT_FOUND", str(exc), started)
        except (ValidationError, TypeError, KeyError, ValueError) as exc:
            return self._error(name, context, "INVALID_PROVIDER_OUTPUT", "Stored provider output was malformed", started, {"reason": str(exc)})
        except Exception as exc:  # store/network adapter failures are not model-visible details
            return self._error(name, context, "UNAVAILABLE", "The backing PolicyPulse resource is unavailable", started, {"reason": type(exc).__name__})
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _error(self, name, context, code, message, started, details=None, outcome="error"):
        return ToolExecutionEnvelope(
            tool_name=name,
            ok=False,
            error=ToolError(code=code, message=message, details=details or {}),
            audit=ExecutionAudit(tool_name=name, duration_ms=_duration_ms(started), outcome=outcome, principal_id=context.principal_id),
        )


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))

