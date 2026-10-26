from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    ActivePolicyChange,
    CRDDecision,
    DTRRequest,
    PolicyChangeRecord,
    PolicyDiff,
    TrustedSource,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolAuthContext(StrictModel):
    """Trusted application context; never accept this from model arguments."""

    principal_id: str = Field(min_length=1, max_length=128)
    scopes: frozenset[str] = Field(min_length=1)
    allowed_source_ids: frozenset[str] = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=256)
    allow_phi: bool = False


class ListTrustedPolicySourcesArgs(StrictModel):
    limit: int = Field(default=50, ge=1, le=100)


class SourceArgs(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)


class PolicyChangeArgs(StrictModel):
    watch_id: str = Field(min_length=1, max_length=128)
    max_changed_lines: int = Field(default=50, ge=1, le=200)


class PolicyDiffArgs(PolicyChangeArgs):
    max_changed_lines: int = Field(default=50, ge=1, le=200)


class RecentPolicyChangesArgs(StrictModel):
    limit: int = Field(default=25, ge=1, le=100)


class PolicyDocumentArgs(SourceArgs):
    snapshot_index: int = Field(default=0, ge=0, le=1)
    max_chars: int = Field(default=12000, ge=100, le=30000)


class ClinicalRelevanceInput(StrictModel):
    trigger_id: str = Field(min_length=1, max_length=128)
    hook: Literal["order-select", "order-sign"]
    payer: str = Field(min_length=1, max_length=128)
    plan: str | None = Field(default=None, max_length=128)
    procedure: str = Field(min_length=1, max_length=256)
    procedure_codes: list[str] = Field(default_factory=list, max_length=50)
    provider_action: str = Field(min_length=1, max_length=256)


class ClinicalRelevanceArgs(StrictModel):
    clinical_trigger: ClinicalRelevanceInput
    watch_id: str | None = Field(default=None, max_length=128)


class DocumentationChecklistArgs(StrictModel):
    clinical_trigger: ClinicalRelevanceInput
    watch_id: str = Field(min_length=1, max_length=128)


class RecentAuditEventsArgs(StrictModel):
    limit: int = Field(default=50, ge=1, le=100)


class StoredPolicyDocument(StrictModel):
    source_id: str
    payer: str
    content_hash: str
    content: str
    timestamp: datetime
    via: str
    truncated: bool


class ClinicalRelevanceResponse(StrictModel):
    decision: CRDDecision
    candidate_watch_ids: list[str]


class DocumentationChecklistResponse(StrictModel):
    dtr_request: DTRRequest
    source: Literal["deterministic_policy_change"] = "deterministic_policy_change"


class AuditEvent(StrictModel):
    timestamp: datetime
    event_type: str = Field(max_length=128)
    payload: dict[str, Any]


class RecentPolicyChangesResponse(StrictModel):
    items: list[PolicyChangeRecord] = Field(max_length=100)


class RecentAuditEventsResponse(StrictModel):
    items: list[AuditEvent] = Field(max_length=100)


ToolErrorCode = Literal[
    "NOT_FOUND",
    "INVALID_ARGUMENT",
    "FORBIDDEN",
    "RETRYABLE",
    "UNAVAILABLE",
    "INVALID_PROVIDER_OUTPUT",
]


class ToolError(StrictModel):
    code: ToolErrorCode
    message: str = Field(max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionAudit(StrictModel):
    tool_name: str
    duration_ms: int = Field(ge=0)
    outcome: Literal["success", "error", "timeout"]
    principal_id: str


class ToolExecutionEnvelope(StrictModel):
    tool_name: str
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    audit: ExecutionAudit


ToolResponseModel = (
    list[TrustedSource]
    | ActivePolicyChange
    | PolicyDiff
    | RecentPolicyChangesResponse
    | StoredPolicyDocument
    | ClinicalRelevanceResponse
    | DocumentationChecklistResponse
    | RecentAuditEventsResponse
)

