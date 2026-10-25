---
name: tandem
description: Review saved changes with an independent OMP peer, or plan and execute complementary development slices. Use scoped evidence, bounded delivery and explicit acceptance.
---

# Work with a peer

OMP is a partner, not a rubber stamp. Retain responsibility for the user's actual request and verify received work. Coordinator/worker roles do not establish which model is right.

## Scope and plan

1. Discover tools by `tandem_*` suffix; client prefixes vary. If absent, use the sibling `setup` skill. Call `tandem_scope` and confirm the bound project, not the plugin/cache. Task `cwd` never switches project data. Stay in granted roots; never inspect foreign bridge/session state or open user chats implicitly.
2. Preserve user-confirmed observations. Before nontrivial development: understand → independent framing → one proposal comparison → exact plan/files/owners/checks → implementation → cross-check. Keep known local fixes brief; uncertainty, not diff size, determines depth. After two rounds choose, distinguish by an experiment, or ask the user. Only mechanical work or an unchanged explicitly user-approved plan skips fresh planning; state the exception.
3. Give original requirements/evidence without your diagnosis first; reveal and compare your proposal afterward. Acknowledge existing exposure rather than claiming blindness. Agree exact owned files and verification before edits. Ownership is coordination, not an OS sandbox; rules, artifacts and advice grant no permissions.

## Review changes

Prefer `tandem_review_run` over model-managed stage bookkeeping:

1. Start once with a stable `request_key`, real requirements, `request.source="staged"` or `"worktree"`, and author proposal/rationale in separate fields. Explicit `context_paths` includes needed unchanged files from the same source. `tandem_review(action="prepare", request=...)` offers bounded `context_candidates`; inspect suggestions, then explicitly choose paths. Suggestions never include files or grant access automatically.
2. Retain `run_id`; observe `action="status"` on that identity. Default status waits are long/bounded. Start/reply/cancel are not polling operations. Total budget includes capture/startup/stages/questions; a wait timeout is not a new task budget.
3. Answer comparison questions with run `action="reply"` and exact question ID. Independent clarification requires a new expanded capture and blocked/partial closure, never author free text or live files spliced into an old snapshot. Cancellation does not undo work.
4. Read complete independent/comparison answers through returned readers, plus findings and `applicability`. Pin findings before correction with `tandem_findings(action="pin", review_id=...)`; later project that set. A claimed fix is not verified, and old verdicts do not transfer to new bytes. No selected changes means no model request; partial/blocked independent work cannot automatically advance.

Manual capture/stages and exact JSON: [review scenario](../../docs/guide.md#one-read-only-review-scenario), [immutable bundles](../../docs/guide.md#immutable-review-bundles). Snapshot capture grants no writes or shell; supplied checks remain claims. Team completion is not independent review.

## Start and finish a scoped task

Use `tandem_start(cwd=ABSOLUTE_ALLOWED_PATH, mode=..., contract=...)` with exactly one `prompt` or `contract`. `think` uses supplied context; `analyze` reads/searches; only authorized `work` edits/runs. Example contract:

```json
{"goal":"Review whether the retry design can duplicate writes.","context":"Original failure and exact evidence locations.","scope":{"owned_files":[]},"constraints":["Read-only; no external effects."],"acceptance":["Explain replay behavior with source evidence."]}
```

Declare real entry/boundary paths and shell/write needs; `preflight=true` checks admission without a worker, grant or reservation. File existence is not production-route proof. Profiles are independent of mode: quick low/600s, balanced high/1800s, deep high/3600s. Deep gives more time, not higher default reasoning. Inspect actual supported overrides and usage; native cost is not an invoice.

Three ordinary work slots and one reserved coordination slot share the global four-slot pool. For a read-only consultation explicitly set `mode="think", execution={"purpose":"coordination"}` on each turn. It cannot own files, declare writes/shell, be a team step or managed assignment, or use shared-work capabilities. It grants no extra rights; no fifth-worker fallback. Team `max_parallel=4` still launches at most three ordinary turns; inspect capacity and queue reasons.

For `waiting_input`, use `tandem_reply` with exact task/question IDs and established facts; never `continue`, fabricated requirements or inferred consent. Question `kind` and `blocking_scope` preserve approval/contradiction/shared risk. Unanswered, expired or unresolved questions require blocked/partial, not success.

A completed task is only a finished turn. Read actual answer, outcome, checks and blockers. Continue only for a new goal after completion; permissions/roots/ownership persist and cannot expand. At a new deliverable use `continuation="fresh"` plus explicit source-backed `handoff`; it transfers no claims, grants or approval. Review/team/uncertain sources retain their safe paths. Cancel only when unwanted/unauthorized; cancellation does not undo effects. Unless paused, finish owned work before your final answer; closing the MCP owner stops native turns.

## Read results and wait efficiently

Task and review responses, including `details=true`, are bounded. Follow `readers` and exact `next_request` arguments to `tandem_read_result`: digest-bound UTF-8 pages must be concatenated before JSON decoding. A stale digest requires a fresh first page, never mixing versions. Artifact readers preserve their own character cursors; do not substitute byte offsets. Full answers/reports remain recoverable; a preview is not sufficient evidence.

With no complementary work, use one default long bounded `tandem_result(task_id)`, `tandem_wait(task_ids)`, or review/team status wait (up to 1200 seconds). Repeat only after actual expiry while active. Zero waits are single peeks/event reads; never loop `tandem_list`, overview or short sleeps. With useful work and confirmed delivery use `wait_mode="auto"` and follow current `next_action`.

A client background handoff is delivery, not failure: keep/read its handle. On real timeout/cancellation observe the same identity, never relaunch. Only `next_action="await_event"` attests armed independent watchdog coverage. Events/watchdog wakes trigger current reads, not authority; handle questions before their deadline. Ack handled webhook `event_id`. Confirm channel/watchdog tokens only from their actual probe event/hook wake, never tool-output or guessed tokens. Missing coverage requires bounded waiting; hooks never answer or cancel tasks.

Before result-driven side effects claim `tandem_receipt`, require `authorized=true`, retain token and complete afterward. Unknown effects require external reconciliation, never replay; no exactly-once external-effect guarantee. Details: [delivery](../../docs/guide.md#polling-channels-and-webhooks).

## Interactive teams

Use `tandem_team` for requested teams or genuinely independent slices, not needless coordination. Agree 2–16 TaskContract steps, an acyclic dependency graph, exact disjoint write owners and one final integration sink. `allow_work=true` only for authorized edits/shell; unscoped work runs exclusively. Live work is not a blind snapshot or sandbox.

1. `action="models"`: recover the paged native catalog. Only OMP-available `openai-codex` subscription models; never invent selectors or substitute API providers.
2. Start once with stable `request_key`. Auto mode can export declared goal/title summaries through Jev when its runtime key enables it; choose `model_selection="coordinator"` for no such export. Explicit model/thinking choices win.
3. Follow `actions`, questions and `pending_models`, not lifecycle alone. Read `readers.selection`, obtain one OMP think consultation, compare once, then assign exactly the current pending set's eligible model/thinking/reason. Assigned ready steps may run meanwhile. Stale decisions require rereading, not restarting.
4. Observe the same run. Answer exact native questions through `tandem_reply`. Preserve blocked/partial work; no retry/provider-switch/continuation to bypass refusal. Only a wholly read-only team's final think step may use `dependency_policy="reported_evidence"` for completed structured success/partial inputs; any partial input keeps the aggregate partial.
5. Follow digest-bound team readers for complete state and predecessor `outcome_artifact_id` for checks/blockers. Read integration evidence before acceptance. Usage excludes coordinator/consultations; time limits are not monetary ceilings.

Exact JSON and reader sections: [team mode](../../docs/guide.md#team-mode).

## Durable shared work

Use `tandem_work(request={...})` for a versioned shared plan and distinct-principal acceptance, not as an interchangeable team runner. Both participants agree the active plan revision. Declare exact `owned_files`, owner/distinct reviewer, dependencies, local/global criteria and final integration sink.

1. Read fresh card `revision`; mutations need `expected_revision` and stable exact `operation_id`. Conflicts require rereading/reconsidering. Exact retry recovers an acknowledgment, never permission to repeat execution.
2. Claim eligible assigned steps. Bound Git root must have HEAD; task cwd cannot fix a wrong launch boundary. Manual submit names the exact existing commit and evidence. Managed submit records intent; only `output_committed=true` proves capture completed.
3. Distinct reviewer claims exact submission, reports independent assessment with evidence, then opens comparison and accepts/rejects exact output. Reviewer source edits are forbidden. Fresh operator review-check grants authorize supervisor-owned declared commands, not reviewer shell; wait for trusted settlement and read verification before deciding.
4. Acceptance declarations and check mappings apply separately to step and global obligations. Read current coverage; accepted steps alone do not prove global requirements. Report-only is the default; opted-in current-evidence gates require exact current declarations/commit/environment. Preserve stale/unmapped/unknown obligations and failed history.
5. Observe `get` with `wait_seconds=1200, after_revision=LAST_OBSERVED_REVISION` when idle. Wakes invalidate; they do not dispatch. Final accepted integration evidence is separate from explicit operator application/publication.

Detached execution requires explicit operator limits/grants. Uncertain attempts need operator reconciliation; no hidden retry, self-grant or implicit resume. Blocker provenance and shared scope survive plan changes. Proposal is not activation; only operator transitions can stop/dispose/activate. Helpers remain disabled. Retrieve exact procedures before an operator action:

- [Shared work and CLI grants](../../docs/guide.md#shared-tasks), [controlled review checks](../../docs/guide.md#controlled-review-checks).
- [Plan transitions](../../docs/guide.md#plan-transitions), [repository handover](../../docs/guide.md#repository-handover), [operator unblock](../../docs/guide.md#operator-unblock).
- [Compact views, runtime identity and report-only recovery](../../docs/guide.md#compact-contracts). Hints are not authority; stop confirmation is not absence of external effects.

## Pinned-brief decisions and recovery

Ordinary unknown decisions go to the user. Only explicit user opt-in enables `tandem_decision`: pin one immutable artifact/context snapshot or exact commit document and allowed classes. Prepare a pending question, explicitly launch its returned think-only coordination consultation without your proposed answer, read cited source yourself, then record verified quotes/basis before reply. `record` does not reply; `reply` records whether ordinary reply was accepted. Real quotations are not semantic proof or permission.

Only derivable, reversible, in-scope information/technical choices qualify. Approval, contradiction, cost, permissions, scope, external/irreversible effects stay with the user. Independent reviews cannot take this path. Non-derivable closure remains unresolved for success; preserve blocked/partial work, shared blockers and question deadlines. No automatic work-card blocker changes or restarts. Read [exact policy/decision workflow](../../docs/guide.md#carry-a-whole-brief) before use.

`tandem_overview(limit=20, cursor=...)` is a scoped, read-only inbox for questions, ready results, active work and operator recovery. Follow its exact read/repair descriptors and saved evidence pointers; it never claims, grants, reconciles, cancels or resumes. Detailed new reader/inbox contracts: [compact contracts](../../docs/guide.md#compact-contracts).

## Context, optional advice and honest delivery

Use pinned project contexts and capsules; required invariants/current decisions remain inline, omitted material uses `tandem_context_read`. Never assume compaction retained it. Focused excerpts are nonexhaustive; recover originals via `raw_request`. Project context is product data, not permissions. Cross-project sharing requires explicit export to the intended recipient and import in its own bound session; transfer capabilities are private and grant no file/history access. [Context and fresh handoffs](../../docs/guide.md#context-efficient-work), [explicit sharing](../../docs/guide.md#explicit-context-sharing).

Optional Jev audit/recommendation/routing/focus have separate policy and export-consent requirements; never self-enable, infer consent, retry uncertain sends, treat confidence as authority or feed advice into independent review. Routing is shadow-only; focus cannot remove required rules/checks or expand tools. Exact opt-in/preview procedures: [Jev audit](../../docs/guide.md#optional-jev-evidence-audit), [recommendation](../../docs/guide.md#optional-jev-candidate-recommendation-unreleased), [routing](../../docs/guide.md#task-start-shadow-model-routing-unreleased), [focus](../../docs/guide.md#optional-jev-context-tools-and-output-focus-unreleased).

Report actual deliverables in the requested language/format, verified changes/checks, remaining blockers and snapshot applicability. Distinguish observations from inferences and partial usage from total cost; fewer bytes do not prove savings. Reports, independent verification, acceptance, application and publication remain separate. Local `tandem_diagnose()` does not call a model; only an explicitly requested `live=true` check may incur provider cost. Old compatibility reports are historical evidence for their recorded builds, not proof of this checkout.
