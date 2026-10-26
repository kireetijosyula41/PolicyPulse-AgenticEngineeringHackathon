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


class InvestigationRequest(StrictModel):
    investigation_id: str = Field(min_length=1, max_length=128)
    trigger_kind: str = Field(min_length=1, max_length=64)
    authorized_policy_sources: list[str] = Field(min_length=1, max_length=50)
    policy_change_id: str = Field(min_length=1, max_length=128)
    clinical_event_reference: str | None = Field(default=None, max_length=128)
    validated_clinical_event: ClinicalRelevanceInput | None = None
    question: str = Field(min_length=1, max_length=2000)
    requested_skill: str | None = Field(default=None, max_length=128)
    max_tool_calls: int = Field(default=8, ge=1, le=8)
    execution_deadline_seconds: float = Field(default=30.0, gt=0.1, le=300.0)
    token_budget: int = Field(default=4000, ge=100, le=20000)


class InvestigationAuthContext(ToolAuthContext):
    investigation_id: str = Field(min_length=1, max_length=128)
    trigger_kind: str = Field(min_length=1, max_length=64)
    authorized_policy_change_id: str = Field(min_length=1, max_length=128)
    clinical_event_reference: str | None = Field(default=None, max_length=128)
    validated_clinical_event: ClinicalRelevanceInput | None = None


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


class InvestigationReport(StrictModel):
    investigation_id: str
    status: Literal["complete", "insufficient_evidence", "error"]
    answer: str = Field(max_length=5000)
    source_ids: list[str] = Field(max_length=50)
    observations: list[str] = Field(max_length=50)
    hypotheses: list[str] = Field(max_length=50)
    uncertainty: list[str] = Field(max_length=50)
    next_steps: list[str] = Field(max_length=50)
    tool_call_count: int = Field(ge=0, le=8)


class InvestigationToolCall(StrictModel):
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    duration_ms: int = Field(default=0, ge=0)
    outcome: Literal["success", "error", "rejected"]
    error_code: str | None = None


class InvestigationRun(StrictModel):
    report: InvestigationReport
    tool_calls: list[InvestigationToolCall] = Field(max_length=8)
    provider: Literal["openai", "fixture"]
    model: str
    skill_name: str | None = None
    skill_version: str | None = None


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

