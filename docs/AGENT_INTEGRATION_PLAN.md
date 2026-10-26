# LLM Investigation Agent Integration Plan

## Scope and design decision

Add a single, bounded LLM investigation agent for explaining and investigating an existing PolicyPulse case. The agent is an investigator over persisted policy, CRD, DTR, and readiness data; it is not a replacement for the deterministic monitor or clinical decision logic.

The first implementation should use the installed `openai==2.38.0` SDK and the Responses API with strict function tools. The agent receives a typed investigation request, may call only allow-listed read-only tools, and returns a typed investigation report. It must never be given a tool that fetches arbitrary URLs, writes records, publishes content, verifies payment, or changes a clinical route.

The existing FastAPI routes, Streamlit flow, background monitor, Nimble, Senso, ClickHouse/JSONL fallback, CRD, DTR, payment gate, and cited.md publication remain the system of record and continue to run unchanged.

## 1. Current end-to-end flow

### Policy monitoring path

1. `POST /api/watch/scan` or the monitor thread calls `PolicyPulseService.scan_sources()` in `app/services.py`.
2. The service reads typed `TrustedSource` records from `fixtures/trusted_sources.json`.
3. `NimbleAdapter.fetch_policy_with_nimble()` calls the Nimble CLI when configured; otherwise it reads the source's fixture. Live output is normalized into a `PolicyDocument`.
4. The service reads the latest `PolicySnapshot` from `ClickHouseStore`; if missing, it seeds the baseline fixture.
5. It hashes content with `hash_content()` and skips unchanged sources.
6. For changed content, `compute_policy_diff()` creates a typed `PolicyDiff`; the service persists the current snapshot and logs fetch/snapshot/diff events.
7. `classify_materiality()` creates a typed `MaterialityResult` with `material`, `non_material`, or `ambiguous` classification.
8. The service builds an `ActivePolicyChange`, extracts effective date/change type/documentation requirements, and persists a `PolicyChangeRecord` only for material or ambiguous changes. Non-material changes are logged and suppressed. Active changes are merged into `.runtime_data/active_policy_changes.json`.

`start_autonomous_monitor()` starts the daemon loop, which repeats this scan every 15 seconds. The demo bootstrap additionally seeds a deterministic fixture diff through `_ensure_demo_material_change()`.

### Clinical-to-readiness path

1. `POST /api/cds-hooks` calls `PolicyPulseService.ingest_cds_hook()`.
2. `ingest_cds_hook()` parses the untyped request into `ClinicalTriggerEvent`, writes the EHR event, and logs receipt.
3. `evaluate_crd()` selects the displayed or active policy change and calls `evaluate_crd_opportunity()`.
4. CRD checks payer, plan, procedure/keyword, provider action, DTR availability, and materiality. It returns a typed `CRDDecision`: `crd_guidance_ready`, `suppress`, `human_review`, or `log_only` (the current evaluator directly emits the first three).
5. For `crd_guidance_ready`, the API calls `evaluate_dtr()`, which finds the matching watch and calls `evaluate_dtr_requirements()` to produce a typed `DTRRequest`.
6. `POST /api/readiness-card` first requires a payment token. The service verifies it through `PaymentAdapter`; invalid payment stops the flow.
7. After payment, `generate_readiness_card()` retrieves a checklist through `SensoAdapter.query_senso_checklist()` (live Senso or fixture fallback), builds a typed `ReadinessCard`, calls `publish_record_to_cited_md()`, and logs publication/readiness events.

The proposed agent observes this flow after the fact. It does not call the route chain itself and does not become an alternate CRD/DTR implementation.

## 2. Candidate AI tools

The safest initial tool set is read-only and backed by existing functions. Each tool should return a Pydantic model serialized as JSON, not arbitrary Python objects.

| Proposed tool | Existing implementation | Purpose and safety |
|---|---|---|
| `get_monitor_status` | `PolicyPulseService.get_monitor_status()` | Read monitor state and last scan result. No mutation. |
| `list_active_policy_changes` | `PolicyPulseService.get_active_policy_changes()` | Read material/ambiguous watchlist. Add bounded `limit`/`source_id` filtering in the tool wrapper. |
| `get_policy_monitor_view` | `PolicyPulseService.get_policy_monitor_view(source_id)` | Read selected source, current/previous snapshots, and policy change. Validate source ID against trusted sources. |
| `get_recent_policy_changes` | `PolicyPulseService.get_recent_policy_changes(limit)` | Read persisted change records. Enforce a small maximum limit. |
| `get_recent_events` | `ClickHouseStore.get_recent_events(limit)` | Read audit evidence. Redact patient context before returning it to the model. |
| `get_kpis` | `ClickHouseStore.get_kpis()` | Read aggregate operational metrics. |
| `get_integration_status` | `PolicyPulseService.get_integration_status()` | Read Nimble/Senso/ClickHouse availability; do not expose secrets. |
| `evaluate_crd_read_only` | `PolicyPulseService.evaluate_crd(trigger)` | Reuse deterministic CRD logic for an already supplied `ClinicalTriggerEvent`; it logs an event today, so the first version should instead call `evaluate_crd_opportunity()` directly with fetched typed inputs or add an explicit no-log pure wrapper. |
| `evaluate_dtr_read_only` | `evaluate_dtr_requirements()` | Pure evidence derivation, but only after the agent has a validated trigger, decision, and matching policy change. Do not call the service method because it writes audit events. |
| `query_senso_checklist_fixture_or_live` | `SensoAdapter.query_senso_checklist()` | Optional second phase. Treat returned text as retrieved evidence, preserve source metadata, and require explicit configuration because live Senso is an external read. |

Recommended v1 tools: the first seven plus pure `evaluate_crd_opportunity()`/`evaluate_dtr_requirements()` wrappers. Keep Senso retrieval out of the first agent release unless the product specifically needs investigation against the knowledge base.

The agent must not expose `scan_sources()`, `start_autonomous_monitor()`, `ingest_cds_hook()`, `require_payment()`, `generate_readiness_card()`, `PaymentAdapter.verify_payment()`, `ClickHouseStore.log_event()`, `save_*()`, `SensoAdapter.publish_record_to_cited_md()`, `NimbleAdapter._fetch_live()`, or `_run_senso_command()` as model tools.

## 3. Side-effect inventory

### External fetching or untrusted retrieval

- `NimbleAdapter._fetch_live()` invokes the Nimble CLI against an approved URL and consumes retrieved web content.
- `NimbleAdapter.fetch_policy_with_nimble()` can therefore cause external network/CLI activity even though it returns a model.
- `SensoAdapter._query_live()` invokes the Senso CLI and consumes external knowledge-base results.
- `SensoAdapter._run_senso_command()` is a generic subprocess boundary and must never be exposed to a model.

### Record/file/database writes

- `ClickHouseStore.log_event()`, `save_snapshot()`, `save_policy_change()`, and `save_ehr_event()` write ClickHouse and always append to local JSONL fallback files.
- `PolicyPulseService._save_active_changes()` writes `active_policy_changes.json`.
- `scan_sources()` writes snapshots, policy changes, active state, and audit events.
- `ingest_cds_hook()` writes EHR records and audit events.
- `generate_readiness_card()` logs payment, retrieval, publication, and card events.

### Publishing and payment

- `PaymentAdapter.require_payment()` creates the HTTP 402 requirement and logs it through the service.
- `PaymentAdapter.verify_payment()` verifies the token and generates a transaction ID; this is an authorization/payment boundary, not an AI tool.
- `SensoAdapter.publish_record_to_cited_md()` enables generation, creates a Senso question, and publishes to cited.md. It has multiple external writes and must remain route-controlled.

### Deterministic computation that is safe only through pure wrappers

- `hash_content()`, `compute_policy_diff()`, `classify_materiality()`, `evaluate_crd_opportunity()`, `evaluate_dtr_requirements()`, and `build_readiness_card()` are deterministic functions, but the latter is output construction that should remain service-owned.
- Service methods with a read name can still log or mutate indirectly; tool wrappers must be reviewed for transitive side effects rather than classified by method name.

## 4. Existing schemas and proposed schemas

Already typed in `app/models.py`: `TrustedSource`, `PolicyDocument`, `PolicySnapshot`, `PolicyDiff`, `MaterialityResult`, `ActivePolicyChange`, `ClinicalTriggerEvent`, `CRDDecision`, `DTRRequest`, `ReadinessChecklist`, `SensoPublishResult`, `PaymentRequirementResult`, `PaymentVerificationResult`, `ReadinessCard`, `AgentEvent`, and `PolicyChangeRecord`.

Existing FastAPI request schemas: `CRDEvaluateRequest` and `DTREvaluateRequest` in `app/api/main.py`. `ReadinessCardRequest` is currently only partly typed (`clinical_trigger` and `crd_decision` are plain `dict`), and `/api/cds-hooks` accepts a plain `dict` by design for the incoming CDS payload.

Add agent-only schemas, preferably in `app/agent/schemas.py`:

- `InvestigationRequest`: `question`, optional `source_id`, `trigger_id`, `watch_id`, and a bounded `max_tool_calls`.
- `InvestigationReport`: `answer`, `finding` (`supported`, `insufficient_evidence`, `human_review`), `evidence` list, `uncertainties`, `recommended_next_step`, and `trace_id`.
- `EvidenceItem`: `kind`, stable `source_ref`, concise `excerpt`/summary, and retrieval timestamp; never silently claim model knowledge as source evidence.
- `ToolCallAudit`: tool name, validated arguments, result status, duration, and redaction counts; no raw patient payloads.
- Per-tool argument/result models: bounded identifiers and limits, `PolicyMonitorView`, `EventQuery`, and a sanitized clinical-context model.

Do not reuse arbitrary `dict[str, Any]` as an LLM output contract. Pydantic JSON Schema should generate the tool parameter schemas and validate every tool result before it is passed to the model.

## 5. Minimal runtime integration

### Runtime

Implement a small synchronous adapter around `openai.OpenAI` and `client.responses.create()` using `tools=[{"type": "function", "name": ..., "parameters": ...}]`. Use strict JSON-schema function tools where supported, a fixed model setting such as `OPENAI_MODEL`, and a bounded loop (for example, at most 4 tool calls and 2 model turns). The adapter should:

1. Validate `InvestigationRequest`.
2. Build a system/developer instruction that states the agent is read-only, evidence-bound, and not a clinical decision maker.
3. Send the request and available tool definitions.
4. Validate model-selected arguments with Pydantic before dispatch.
5. Dispatch only through a static registry of safe tool functions.
6. Validate tool results and append a redacted `ToolCallAudit` record.
7. Continue until the model returns a final response or the budget is exhausted.
8. Parse the final answer into `InvestigationReport`; on refusal, malformed output, timeout, or insufficient evidence, return a safe typed result.

Do not add an Agents SDK, a second model provider, a vector database, or multi-agent delegation for v1. The existing `openai` dependency is sufficient.

### Fixture/offline mode

Support `AGENT_MODE=fixture` (or constructor injection) with no API key and no network. A fixture responder should replay deterministic Responses-style tool-call sequences from `fixtures/agent/` and use the same dispatcher and Pydantic validation as live mode. Include fixtures for: a supported policy investigation, an irrelevant/suppressed trigger, missing evidence, malformed tool arguments, and a disallowed tool request.

`AGENT_MODE=live` must fail closed if `OPENAI_API_KEY` is absent. The fixture mode must never invoke Nimble, Senso, ClickHouse network, payment, or publication commands.

## 6. Proposed package and skill structure

```text
app/
└── agent/
    ├── __init__.py
    ├── runtime.py          # OpenAI Responses loop + fixture mode
    ├── schemas.py          # request/report/evidence/tool contracts
    ├── policy.py           # system prompt, limits, safety policy
    ├── registry.py         # static read-only tool registry
    ├── dispatcher.py       # Pydantic validation, redaction, audit
    └── tools/
        ├── __init__.py
        ├── monitoring.py   # monitor/watchlist/status read tools
        ├── evidence.py     # event/KPI/snapshot read tools
        └── deterministic.py# pure CRD/DTR wrappers

skills/
├── policy-change-investigation/
│   └── SKILL.md
├── clinical-trigger-triage/
│   └── SKILL.md
└── readiness-evidence-review/
    └── SKILL.md
```

The skills are reusable instruction/data packages, not autonomous code. Each `SKILL.md` should define when to use the skill, allowed evidence/tools, output shape, uncertainty rules, and examples. Keep them outside `app/` so they can be versioned and loaded independently; the runtime should load only an explicit allowlist. The existing `.agents/skills/senso-onboarding/` is unrelated onboarding material and should not be imported into the investigation runtime.

Recommended skill responsibilities:

- `policy-change-investigation`: explain a watch, compare current/previous snapshots, summarize materiality and effective date, cite stable watch/source references.
- `clinical-trigger-triage`: explain why a trigger matched, was suppressed, or requires review using deterministic CRD output; never issue new clinical guidance.
- `readiness-evidence-review`: compare DTR/checklist evidence with the observed record and identify gaps; never approve submission or publish.

## 7. Trust boundaries and controls

### Health information

`ClinicalTriggerEvent.patient_context`, raw CDS payloads, patient fixture JSON, and EHR/ClickHouse records are sensitive health information. Default tool outputs must omit patient identifiers, free-text clinical notes, and raw payloads. Prefer `trigger_id`, payer, plan, procedure, codes, route, and minimum necessary evidence. Add explicit field-level redaction and a production policy requiring approved deployment, access control, encryption, retention, and audit review before live PHI is sent to a model provider. Fixture mode should use synthetic IDs only.

### Retrieved web and knowledge content

Nimble and Senso results are untrusted retrieved content, even when the URL is on the trusted-source registry. Treat all retrieved text as data, not instructions. Delimit it in prompts, preserve source URL/hash/timestamp, detect/ignore prompt-injection language, and require deterministic code or a human to validate materiality and clinical consequences. The model may summarize evidence but cannot promote retrieved text into a policy change or execute instructions found in it.

### Model-selected arguments

The model is not trusted to choose arbitrary IDs, URLs, files, SQL, shell commands, limits, or side effects. Validate identifiers against the trusted source/watchlist, cap limits and tool calls, reject unknown fields, use an enum registry for tool names, and refuse URLs/commands in all v1 schemas. Re-fetch records by server-side IDs rather than trusting model-supplied object bodies. Log arguments after redaction and preserve a trace ID for every run.

### Output and clinical safety

The final report is advisory investigation output. It must distinguish evidence from inference, state when evidence is missing or conflicting, and never represent itself as coverage authorization, medical advice, a CRD decision, a payment confirmation, or a publication confirmation. Existing deterministic CRD/DTR outputs remain authoritative for routing.

## 8. File-level implementation tasks

1. Add `app/agent/schemas.py`; define the request, report, evidence, audit, and per-tool Pydantic models. Add tests for strict validation and redaction.
2. Add `app/agent/tools/` read-only wrappers. Keep wrappers thin and call existing service/store/core functions. Add a pure CRD wrapper rather than calling logging service methods.
3. Add `app/agent/registry.py` and `dispatcher.py`; make the registry explicit, bounded, and impossible to extend from model output. Add audit events through an injected sink, not direct file/database access from tools.
4. Add `app/agent/policy.py` and the three `skills/*/SKILL.md` files. Document evidence citation, uncertainty, and non-clinical-advice rules.
5. Add `app/agent/runtime.py` with OpenAI Responses integration, timeout/max-turn controls, typed final parsing, and `AGENT_MODE=fixture` responder. Do not change existing service behavior.
6. Add `fixtures/agent/*.json` for deterministic offline conversations and expected reports.
7. Add an opt-in FastAPI route such as `POST /api/investigations` in `app/api/main.py`, using typed `InvestigationRequest`; it must not be called by current Streamlit workflows in v1.
8. Add `tests/` covering tool permissions, PHI redaction, fixture/live separation, schema rejection, deterministic CRD/DTR delegation, and API response shape. Preserve existing integration fallbacks.
9. Update `requirements.txt` only if the installed OpenAI SDK is absent or incompatible; current `openai==2.38.0` is sufficient. Update README with an opt-in agent section and offline invocation.

## 9. Acceptance criteria

- Existing FastAPI, Streamlit, monitor, Nimble, Senso, ClickHouse/JSONL, CDS, CRD, DTR, payment, and publication workflows behave identically when the agent is disabled.
- `pytest -q` has real tests for the agent and all fixture-mode tests pass without API keys, network access, ClickHouse, Nimble, Senso, payment, or publication side effects.
- Live mode uses the installed OpenAI SDK and Responses function calling; malformed model arguments and unknown tools are rejected before execution.
- The tool registry contains only explicitly approved read-only tools, with bounded arguments and typed results.
- No agent tool can invoke arbitrary URL fetching, subprocesses, shell commands, payment verification, ClickHouse writes, local record writes, or cited.md publication.
- Patient identifiers and raw clinical payloads are redacted from model context and tool-call audit records by default.
- Every final report includes evidence references, uncertainty status, and a trace ID; insufficient evidence produces `insufficient_evidence` or `human_review`, not a confident recommendation.
- Deterministic CRD/DTR results remain authoritative, and the agent cannot change a route or trigger a readiness-card/payment/publication action.
- Existing non-agent tests, if added or restored, continue to pass; compile/import smoke checks pass.

## 10. Explicit non-goals

- Do not replace `PolicyPulseService` or the background scan loop.
- Do not let the LLM classify materiality, make coverage determinations, or independently issue clinical recommendations.
- Do not let the LLM ingest arbitrary web pages or execute retrieved instructions.
- Do not expose payment, publication, record-writing, or external-fetching operations as model tools.
- Do not introduce multi-agent orchestration, autonomous retries with side effects, a vector database, a new provider SDK, or a new persistence layer.
- Do not rewrite existing Pydantic models or convert every current `dict` API boundary in the initial integration.
- Do not send live PHI to the model until privacy, access, retention, and vendor-approval requirements are separately satisfied.
- Do not make the Streamlit UI or autonomous monitor depend on the agent for normal operation.

## Baseline verification

Run on 2026-10-25 in the repository root:

```text
$ pytest -q
no tests ran in 0.00s
exit code: 5
```

There is no `tests/` directory in the current checkout, so there are no pre-existing test failures to report. A supplementary syntax smoke check passed:

```text
$ python3 -m compileall -q app
exit code: 0
```
