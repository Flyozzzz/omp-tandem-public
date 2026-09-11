# Changelog

## 3.5.0 — 2026-09-11

### Features

- Two documented entry paths: review a prepared staged change with `tandem_review_run`, or run a two-agent shared development task with `tandem_work`, exact committed submissions and distinct acceptance.
- Operator-pinned managed Claude/OMP selections: `--claude-model` (documented `sonnet` default), `--omp-model` as an alias of `--model`, and separate selection provenance versus observed actual identity. Run/start cannot override an active grant.
- `authorize --preview` describes the proposed grant without activation. Explicit `--max-attempt-cost-usd` is independent of launch count; the default is half the total. Atomic launch-order reservations subtract known spend and active reserves. Grant `preview` exposes reserve policy and actual permissions.
- Canonical `--allow-shell` with deprecated `--allow-tests` alias: both mean arbitrary shell, not a sandboxed test runner.
- Independent-first shared review: immutable complete `report`, at most one optional `compare`, then separate exact-submission acceptance/rejection. Author interpretation stays withheld until comparison. Managed reviewers use a server-bound pinned commit reader; no native live-file or arbitrary-shell bypass is advertised.

### Fixes

- Unresolved blockers retain identity, origin and resolution history across plan revisions; removed-step blockers move to card scope. Corrective agreement does not enable blocked execution or publication. Only the blocker author or operator can resolve/invalidate with evidence.
- Closed shared-review author-artifact, inferred read/history and claim-response disclosure paths. Shell-granted review records an operator policy blocker before launch; participant-authored lookalike blockers cannot waive it.
- Documentation now states the claim/submit prerequisite: the bound project root must be a Git repository with an immutable HEAD commit, not its parent folder.

### Migration

- Legacy grants preserve historical total-budget/max_launches attempt ceilings, allow_tests decoding, active reservations and recovery; ambiguous old OMP selections require explicit reauthorization. Reading/migrating state does not restart execution.
- Legacy review attempts are labelled `legacy_disclosure`; historical acceptance is not relabelled independent-first. Legacy project import stays copy-only. There is no automatic downgrade or rollback of external effects; use a stopped-state backup procedure before manual restore.

### Known limits

- Helpers remain disabled (`delegation.available=false`). The [G0 compatibility map](docs/helper-compatibility.md) records five unsatisfied gates: `child_tools_intersect_parent_restriction`, `project_agent_definition_cannot_widen_bundled_scout`, `task_wide_settings_snapshot`, `parent_stream_usage_scope_exclusive`, and `no_extra_model_calls_per_spawn`. Stages C–F, production scout/sonic delegation and helper cost savings are not released.
- A passing real-OMP verifier means its probes executed, not that unsupported helper gates passed. Startup-auth fallback and post-abort revival remain unverified. Compatibility is version/platform-bound, not a provider or version-range guarantee.
- Stage-confined shell review is unavailable. A shell grant blocks managed review until an evidenced operator decision permits no-shell review; required checks must not be silently waived. Tool restrictions are not an OS sandbox, and manual review cannot erase prior interactive disclosure.
- The recorded `tandem_finish` schema/description mismatch remains a UX issue; this release does not change that API opportunistically. Structured final reporting is still required.
- `test_committed_peer_change_generates_wake_hint_without_dispatch` was intermittent during the preceding review: a pending wake remained after `get`, then the isolated test and full affected suite passed on repeat. Wake acknowledgement is explicitly best-effort under SQLite contention. The original interleaving was not traced; contention is an inference, not a confirmed diagnosis. Integration's first full suite passed without changing this assertion.
- Monetary ceilings use reported/estimated cost, not invoice-hard limits. Unknown cost prevents new launches. No migration, saved card or wake notification grants autonomous execution; applying an accepted result remains a separate operator action. No push, tag, publication or apply is implied by release preparation.
