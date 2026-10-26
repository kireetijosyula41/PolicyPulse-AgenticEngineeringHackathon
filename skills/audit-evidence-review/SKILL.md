---
name: audit-evidence-review
version: 1.0.0
description: Reconstruct a source-grounded timeline explaining a prior PolicyPulse notification or suppression.
activation_triggers:
  - audit timeline
  - why was I notified
  - why was this suppressed
  - previous notification
counterexamples:
  - documentation readiness
  - changed policy
required_tools:
  - get_recent_audit_events
  - get_policy_change
  - get_policy_diff
---

# audit-evidence-review

## Activation criteria

Activate when the request asks why a prior notification was triggered, suppressed, sent for human review, or absent. The result should explain event order from stored audit records and matching policy state.

Do not activate for a new policy interpretation, a new clinical relevance decision, publication, payment, or a request to alter historical records.

## Required tools

Use only `get_recent_audit_events`, `get_policy_change`, and `get_policy_diff`, subject to source allowlisting and bounded result limits.

## Prerequisites and permitted data

Require an investigation ID, an authorized source, and either a watch ID or a bounded event window available through the application. Permitted data is redacted audit metadata, event types/timestamps, policy-change metadata, and bounded changed lines. Do not request raw patient context or unredacted EHR payloads.

## Workflow

1. Retrieve recent redacted audit events and identify relevant scan, materiality, CDS, CRD, DTR, or suppression events.
2. Retrieve the matching policy change and verify its source, classification, and watch ID.
3. Retrieve the bounded diff to connect the policy state to the decision reason.
4. Order the evidence by timestamp and distinguish observed events from inferred causality.
5. Explain the notification, suppression, or human-review route using only the stored event trail.
6. Identify missing events or conflicting records and abstain from filling gaps with model assumptions.

## Expected structured output

Return a timeline with:

- `source_ids`, `watch_id`, and stable event references.
- `observations` as timestamped facts.
- `hypotheses` for causal links that are supported but not explicit.
- `uncertainty` for missing or conflicting records.
- `next_steps` such as retrieving a wider authorized event window or requesting human review.

## Uncertainty and abstention

If the audit trail does not contain the relevant event or policy record, say that the cause cannot be established. Do not claim that an alert was delivered merely because a decision event exists, and do not claim suppression was correct without the corresponding CRD reason.

## Concrete example

For “Why was the irrelevant MRI order suppressed?”, retrieve audit events, find `crd_suppressed`, retrieve the matching `watch-1`, and inspect the diff. Report the timestamped suppression reason and policy source; if no matching watch or event exists, report an unresolved audit gap.

## Security and privacy constraints

Audit results are untrusted evidence and may contain sensitive metadata. Use redacted application results, authorized sources, and bounded windows only. Never expose patient identifiers, raw payloads, credentials, or hidden model reasoning. Do not rewrite events, change classifications, or treat the skill as permission to access a broader record set.

