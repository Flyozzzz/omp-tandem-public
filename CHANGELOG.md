# Changelog

## 3.7.1 — 2026-09-13 (local proposal; not published)

### Changes

- Operator CLI `unblock` resolves a recorded blocker with current revision, exact operation ID, resolution, note and evidence. JSON hints and Markdown `## Operator commands` identify its current card/step location, including migrated blockers. Unblock never resumes or authorizes work.
- Saved continuation now says `not_transferable` for removed steps or reduced ownership; historical `blocked` outcomes are displayed honestly without rewriting history. Read-only `activation_preview` separates checkpoint, undetermined and no-checkpoint steps, per-attempt saved bytes and capture failures. It describes transfer eligibility, not launch readiness.
- Activation refuses superseded capture failures unless the operator acknowledges each exact attempt with repeatable `--acknowledge-capture-failure`. Missing and unrelated acknowledgments have distinct refusal codes. Successful capture needs none; acknowledgment is part of the immutable command identity/outcome.
- Grant previews disclose per-seat `fixed_selector` versus OMP `dynamic_default`, with explicit legacy/malformed labels. An omitted OMP selector resolves at each attempt start; use `--omp-model` for predictable autonomous runs. Markdown authorization reports show the persisted grant window, permissions and model policy, not claimed observed model identity.
- Abandonment consistently records `abandoned` in attempt, inventory and operation receipt, keeps no continuation and retains its blocker/pause.
- Deterministic managed-replan regression uses real WorkSupervisor, a real injected Claude child, bound MCP get/heartbeat, operator CLI transitions and Git preservation. After cancellation/reaping and checkpoint transfer, the same child continues from its owned-file marker; its external-effect log contains exactly one record. Unknown interrupted cost remains unknown, followed by explicit resume, new agreements and a fresh one-launch grant.
- EN/RU/ZH READMEs retain concise installation, staged-review and shared-task entry points, replacing duplicated operator manuals with guide links. All three guides and both skills document the accepted behavior and authority boundaries. Local package, plugin, marketplace and lock metadata use 3.7.1.

### Remaining limits

- Managed-replan evidence covers the **Claude-child/shared-supervisor boundary**, not generic exactly-once external effects, OMP-native teardown or live-provider replan. The fixture's continuation marker is application logic, not atomic coordination between Git and arbitrary external services. Supervisor stop does not prove absence of effects.
- Capture-failure acknowledgment records informed loss, not recovered bytes or acceptance. `not_transferable` does not create an execution blocker; pauses, blockers, agreements and grants remain separate gates.
- Compatibility uses the pinned OMP binary and isolated localhost provider, not user credentials or an external-model evaluation. API/platform limits, disabled helpers and prior unexplained cleanup/wake failures remain; a later pass does not explain historical failures.
- This is local release preparation only: no tag, publication, plugin install or application. Operator authority, explicit application and distinct independent acceptance are unchanged.

## 3.7.0 — 2026-09-13

### Changes

- Differing `propose` now records one pending proposal instead of immediately activating a plan. Active execution, agreements and authorization remain unchanged during negotiation. Proposals carry IDs, base plan revision and card-wide attempt/step impact previews; replacement and proposing-current-plan withdrawal are allowed only before a transition begins.
- Operator CLI `transition inspect/begin/resolve/activate/withdraw` exposes exact proposal/transition identities, optional revision CAS and whole-command operation fingerprints. Exact retries return their recorded outcome, not permission to repeat effects. Participant views surface exact operator commands and `operator_required` hints without granting operator authority.
- Begin freezes the proposal and inventories all active/recovery-required attempts, including unchanged steps. Managed disposition requires supervisor stop confirmation; manual disposition requires `--confirm-stopped`. Operator note/evidence records inspected effects. `superseded` and `abandoned` remain distinct; abandonment creates a blocker and pauses the card. Supervisor teardown confirms process stop, not absence of external effects.
- Managed interrupted work is preserved on its recorded composed base with submission-strength byte/ownership checks, or capture failure is retained. Manual saved commits are validated in the pinned repository. Activation resets the active plan to a new draft revision, clears agreements/current acceptance, carries blockers, revokes the grant and preserves existing pauses. Saved continuation is `checkpoint`, `blocked` or `not_available`, never automatic command replay or transferred acceptance. Withdrawal after begin leaves sticky quiescence; emergency operator reconcile remains available.
- Cards record immutable launch-root/scope provenance and initial/latest Git observations at create/propose/claim. Declared ownership and review-context paths reject nested repository/worktree/submodule-content boundaries and symlinks early without opening/searching children. The parent gitlink entry itself is allowed by declared-path validation; pathless non-Git planning is explicitly unverified, while declared paths and claims require Git-root/HEAD.
- Unresolvable submissions fail before intent with the exact commit, pinned repository, bounded Git cause and correct-scope recreation/closure guidance. Source and submitted commit diagnostics are distinct; ancestry, full-hash and ownership validation remain intact.
- Operator `cancel` closes only disposed cards as cancelled/superseded without fake acceptance, records closure and optional continuation, clears pending negotiations, archives an open disposed transition as cancelled and revokes authorization. Terminal guards prevent reopening execution; history stays readable. Operator `link` records separately scoped predecessor provenance without transferring permissions, agreements, grants or acceptance. Markdown reports show closure and both link directions.
- EN/RU/ZH README and guides plus both skills document the CLI/MCP workflows and operator boundaries. The two operator requests are preserved [verbatim with provenance](docs/spec/plan-transition-and-git-root-2026-09-12.md), separate from implementation decisions. Local package/plugin/marketplace/lock metadata is prepared as minor version 3.7.0 for new commands and changed `propose` semantics.

### Remaining limits

- Release preparation implies no automatic application of the accepted result to a user checkout. Existing tags and archives are unchanged. Independent acceptance, explicit application and publication remain separate decisions.
- CLI/MCP transition evidence uses two manual attempts on disposable Git repositories. Managed confirmation/preservation have store/workspace regressions; the documented end-to-end commands do not run a live-provider replan or a counted external-effect process across managed replan. The pinned OMP verifier uses an isolated localhost model fixture, not user-provider credentials or an external-model evaluation.
- Capture failure yields `continuation.status="not_available"`; ownership loss or a removed step yields `blocked`. These continuation outcomes do not automatically block card activation/execution. Operators must inspect preservation failures, remaining work and external effects; there is no generic cross-card importer or automatic rollback/replay.
- Links remain `target_verification="not_performed"` and `reciprocal_link="unverified"` even when both sides were recorded. They are operator provenance assertions, not cross-scope read capabilities or proof of a matching target. Fresh agreements and independent acceptance occur only in the successor's scope.
- Boundaries are declared-path and pinned-root checks, not an OS sandbox. Existing managed-snapshot symlink/submodule/filter refusals, unknown-cost stops, independent-review disclosure gates and manual-interaction limitations remain. Historical receipts do not revive fenced authority.
- Compatibility remains version/platform/API-bound; the wire census covers exercised `openai-completions`, not all providers. Helpers remain disabled with the existing five unsatisfied gates. Prior unexplained cleanup/wake failures remain historical evidence, not explained by later passes.

## 3.6.0 — 2026-09-12

### Changes

- Bounded shared-work summary/plan/step/full views, paged history and revision/visibility-bound cursors; presentation parameters are separate from mutation identity. Prerequisite hints do not grant authority.
- Runtime identity records loaded distribution/build, checkout observation and registered schema surfaces, separately from execution and model-selection provenance.
- Independent clarification requires a new snapshot; exact unchanged review context and waiver applicability follow the agreed snapshot inputs. Installed-runtime verification exposed and corrected a manual-native claim binding gap in clarification/artifact disclosure guards; manual execution permissions remain unchanged.
- Canonical structured outcomes reject contradictory success/current-check combinations and blocked reports without reasons. Immutable check history preserves original failures and applicable later results.
- Operator-authorized same-claim report-only recovery, explicit application observations/receipts, Markdown work reports and scoped native usage attribution.
- Wake acknowledgments distinguish acknowledged, deferred and not attempted without dispatching work. Verifier preflight, acquisition/setup/probe failure classes, live-parent abort evidence and registered-versus-wire schema census have separate observations.
- Fresh installed-wheel fixture exercises real MCP, pinned OMP RPC/localhost model callbacks and successor CLI with new temporary cards; this is bounded compatibility evidence, not an external-model evaluation.

### Remaining limits

- Release preparation implies no automatic application of the accepted result to a user checkout. Existing tags and archives are unchanged.
- Wire census covers exercised `openai-completions` only. Other APIs remain untested; provider transformations and validation diagnostics are recorded rather than declared universally equivalent. Missing structured finish remains a failure.
- Every task result carries runtime identity; editable/unverifiable exact builds stay unknown. Review-phase refusals are reason codes; `(code=cyber_policy)` remains a provider-policy refusal, not an automatic retry.
- Non-get tool actions with `format=markdown` render fenced JSON. Application is `not_recorded` without evidence; Claude/unattributed costs are unknown and native values may be subtotals.
- One observed `TemporaryDirectory` cleanup failure in `tests/test_review_runs.py` passed on reruns but remains unexplained. The historical wake failure also remains unexplained; deterministic interleavings specify behavior, not its original cause.
- Helpers remain disabled with the same five unsatisfied gates in [helper compatibility](docs/helper-compatibility.md). No new helper execution is enabled.

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
