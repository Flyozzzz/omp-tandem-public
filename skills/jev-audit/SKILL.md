---
name: jev-audit
description: Preview an optional Jev audit of a completed Tandem task's reported evidence, obtain consent for the exact exported page, and interpret typed advice without treating it as verification or permission.
---

# Optional evidence-claim audit

Use this only when the user wants a second model's bounded triage of **reported evidence**. It is not required for normal Tandem work. Read the [Tandem guide](../../docs/guide.md#optional-jev-evidence-audit) for configuration, limits and failure semantics. Discover tools by their `tandem_*` suffix; registration prefixes vary.

## Preview before export

1. Confirm the project with `tandem_scope` and read the completed task's result. Shared-work-bound tasks are ineligible. Do not substitute task-local criteria for a shared plan or manufacture an unbound copy to evade this restriction.
2. Call `tandem_audit` with the selected `task_id`, `offset`, `limit` and `preview: true`. Preview requires neither enablement nor a provider key. It sends nothing, creates no reservation or artifact, and changes no configuration. It still refuses ineligible tasks and oversized inputs rather than presenting a truncated approximation.
3. Inspect the complete returned `payload`, `endpoint`, `model`, `request_bytes` and `input_sha256`. Show the user what would leave the project and identify the exact page. A preview is not consent, an enabled flag is not consent for extra pages, and possession of a key is not permission.
4. Only after explicit approval for this export, call `tandem_audit` with the same task/page, `preview: false` and `expected_input_sha256` copied from the approved preview. The service prepares current input again and refuses a changed hash before reserving or sending. A mismatch requires a fresh preview and renewed approval, not removal of the hash guard.

Example shapes (replace placeholders with observed values):

```json
{"task_id":"<completed-task-id>","offset":0,"limit":5,"preview":true}
```

```json
{"task_id":"<same-task-id>","offset":0,"limit":5,"preview":false,"expected_input_sha256":"<approved-preview-hash>"}
```

The fixed destination is OpenRouter's `https://openrouter.ai/api/alpha/decisions`, using `typesafe/jev-1.13`. Sending requires separate operator enablement and `OPENROUTER_API_KEY` in the MCP process. Never self-enable auditing, fetch/store credentials, change launch settings, restart active work, or install an upstream integration to make an unavailable call succeed. Refer the operator to the guide; keep keys out of chat and project files.

## Disclose the actual data

The exported JSON contains the task ID, goal, selected acceptance criteria or obligations (including parent context and required environments), page selection, report outcome/answer/summary, reported check names/results/details, and recorded-run criteria/notes/results/roles/environments/provenance with computed applicability/currentness/failure flags. It also contains the fixed questions and choice definitions.

It does not fetch task context, repository source, commands, artifact bodies or conversation history. However, included prose and environment descriptions may already contain sensitive text: this projection is **not secret redaction**. Inspect the preview rather than promising confidentiality. Sending retains the request and result/failure as local audit artifacts and may incur provider charges; preview does neither.

## Keep advice separate from facts

- Read the typed choice for each unit: `no_obvious_mismatch`, `partial_or_missing`, `contradiction` or `unclear`. Preserve uncertainty and distinguish missing evidence from contradictory evidence and provider failure.
- `no_obvious_mismatch` means only that the supplied claims appear to cover the unit. Jev has not inspected implementation, test source, raw output or independent observations. A protocol-valid `completed` response is not acceptance.
- Probabilities and confidence are model outputs, not correctness probabilities, calibrated guarantees, permission thresholds or proof that checks passed. Use uncertainty to identify questions for ordinary review; never turn it into an automatic verdict.
- Do not alter task outcomes, checks, acceptance, findings, grants or receipts based solely on this advice. Never put the preview, author report or Jev judgment into an independent review stage; preserve the existing staged disclosure boundary.
- No automatic pagination or retries. `next_offset` only identifies another possible page; obtain separate approval before exporting it. Do not vary page size, task identity or input to bypass cached failures or an unresolved reservation.
- Exact repeated sends reuse the existing attempt. `prior_attempt_unresolved`, timeout, malformed response and unavailable credentials are not negative judgments of the code. Report the reason and continue normal evidence-based review without silent fallback or replay.

## Provenance

This is an independently authored, advisory-only Tandem workflow. Design inspiration: [TypeSafe skills](https://github.com/typesafe-ai/skills) for typed judgments, [pi-typesafe](https://github.com/DevMortimer/pi-typesafe) for export consent and bounded calls, and [jev-use](https://github.com/shitianfang/jev-use) for explicit uncertainty handoff (all MIT). No upstream code, dependencies, hooks or permission gates are imported; their examples and measurements do not establish Tandem accuracy, latency or cost guarantees.
