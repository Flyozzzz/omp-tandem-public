# Changelog

## Unreleased

- Added coordinator-only `tandem_recommend` for advisory selection of one skill or review direction from a caller-supplied bounded catalog, including distinct `none` and `unclear` results. It works before task creation and never discovers skills, reads project context, launches candidates or changes authority.
- Recommendation preview is the default and requires no key or provider request. Sending requires separate `--jev-recommend` / `OMP_TANDEM_JEV_RECOMMEND` enablement and the approved preview's matching input hash. Project-owned durable reservations cache exact outcomes and refuse replay after uncertain execution; existing audit enablement and behavior remain separate.
- Audit and recommendation share the bounded Jev transport and strict Choice parser. Protocol tests and local synthetic responses are not evidence of Jev recommendation accuracy.
- Added opt-in task-start shadow model routing over a frozen operator pool of two or three exact models. Deterministic catalog/constraint checks precede one Jev suggestion; the requested, effective and actually running model remain unchanged, including on abstention or ordinary router failure.
- Shadow routing requires separate policy and per-task summary export approval. Explicit model selections, continuations, reviews and managed work bypass it. A metadata-only native probe isolates catalog stalls from the execution process; routing records, latency and Jev usage are separate from native execution and never replay uncertain sends.

## 3.11.0 — 2026-09-22

### Changes

- Managed submission validates declared ownership before recording immutable intent. New observations distinguish `intent_recorded`, `output_committed` and `capture_failed`; exact operation receipts replay without inspecting a removed workspace. Final capture still revalidates the exact bytes and never adopts undeclared files.
- Fresh operator `--allow-review-checks` grants require a prepared Linux Docker image, resolved to an immutable ID on a pinned local daemon. Each selected `review_verification` command runs after the immutable independent report in a fresh container, mounting only a standalone exact-commit copy. Models remain snapshot-only; no host-shell fallback, image pull or authority upgrade for old grants.
- Container removal, unchanged inputs, exit status and comparison gate acceptance across both reviewer seats. Read-only rootfs, private PID/IPC namespaces, dropped capabilities and no network by default constrain execution; optional existing bridge networks require an explicit grant. This does not undo database/network effects or certify universal adversarial isolation.
- Private verification logs have stage- and scope-checked readers, bounded output and immutable run identities. Unknown create/start/cleanup does not retry or pass. Explicit environment values are pinned by hash and not inherited wholesale.
- Permitted unowned/untracked ignored dependency outputs, including `.venv` symlinks and package-store hardlinks, are excluded without dereferencing them. Tracked/owned inputs, their ancestors and Git metadata retain strict validation; ignored dependencies are not pinned source.
- `tandem_audit(preview=true)` returns the complete bounded outgoing payload without a key, reservation or network call. Sending may require its exact `expected_input_sha256`; stale input refuses before charging. A dedicated `jev-audit` skill explains consent, typed uncertainty and advisory-only use without installing third-party hooks.
- Added opt-in coordinator-only `tandem_audit`: Jev 1.13 on OpenRouter compares completed-task acceptance criteria with reported evidence and returns advisory typed choices. Plain acceptance lists and structured obligations are supported; shared-work-bound tasks remain outside this task-local surface.
- `--jev-audit` / `OMP_TANDEM_JEV_AUDIT=1` and `OPENROUTER_API_KEY` enable explicit calls; `tandem_scope` exposes safe capability information. Task context, commands, source files, artifact bodies and conversation histories are excluded from requests, but selected report prose may contain sensitive information. Normal result/wait/finish paths never call the provider.
- Bounded asynchronous HTTP and immutable exact-page reservations preserve original task outcomes and prevent silent replay across concurrent clients, failures and uncertain interruptions. Advice never grants permissions or establishes verification, semantic sufficiency or acceptance.

## 3.10.0 — 2026-09-16

### Changes

- Task contracts accept an optional `acceptance_set`: the same acceptance list in structured form, whose texts must repeat `acceptance` exactly so there is never a second source of truth. Each criterion carries a stable id, and a criterion whose content is several clauses declares them as `obligations` with their own ids and the exact `environment` each is owed in. The declared criteria — expanded to one evidence unit per obligation — become the denominator of coverage, instead of the checks somebody remembered to declare.
- `VerificationCheck` gains `acceptance_refs`, many-to-many, stating what a check is intended to exercise. A mapping of a parent criterion credits none of its declared obligations: otherwise one check against a four-clause criterion would read as covered, which is the failure this feature exists to catch. Set revisions are content digests and `CheckRun` records the `check_revision` they observed, so editing a criterion's text or a check's command leaves old evidence visible as history rather than current credit.
- `result.acceptance_coverage` keeps apart what is genuinely different: unmapped units, mapped checks the ladder did not select, selected checks with no run, runs that explicitly say not_run, current failures, passes against other bytes, environment mismatches, environments never recorded (unknown, not a match), stale check revisions, and runs of a mapped check that recorded no mapping. The dimensions are not mutually exclusive, because one unit really can have a declaration, a current failure and an older passing run at once.
- `VerificationPlan.acceptance_coverage="require_current_evidence"` refuses a success report while any declared unit lacks an applicable current passing run. The default `report_only` refuses nothing, so existing callers are unaffected; a strict request without a declared set is refused rather than quietly downgraded, and an empty set reports `not_assessable` rather than vacuous eligibility.
- A task that declares no set reports `assessment: "not_declared"` with no denominator, rather than zero coverage. Inventing identities for plain strings would accuse every existing caller of mappings it never made.
- The feature is task-local in this release. Work steps refuse these declarations with `acceptance_coverage_unsupported_surface` rather than accepting and ignoring them, a task bound to a work attempt reports `unsupported_surface` instead of applying its own set to a plan it does not own, and existing work operation receipts keep their identity because the new defaults are stripped before fingerprinting. Carrying sets and immutable runs through work cards and review captures follows in 3.10.1.
- The projection counts; it never decides. `semantic_sufficiency` and `acceptance` stay `not_assessed` in every response, including when every unit has a current pass: a participant can declare a mapping its test does not honour, and no schema detects that.

## 3.9.1 — 2026-09-16

### Fixes

- The long-wait guidance shipped in 3.9.0 described the client's side of a wait wrongly. It told callers to ask only for what "this client's own request deadline outlives", because the server "cannot deliver a response the client stopped waiting for". A real run measured the opposite: Claude Code does not stop waiting — it moves a request still outstanding at 120 seconds into a background task, says so in the immediate response, and delivers the real result later as a notification. All ten such calls in that run delivered, a median of 235 seconds after the handoff. The guide, the coordinator instructions, the tandem skill and the tool docstrings now describe that handoff as delivery to read rather than a failure to retry; they say that stopping the client's waiter is not cancelling the work and that the background task does not survive exiting the session, and they keep the fallback for a genuine cancellation as a bounded read of the same task or run identity, never a new launch. No behaviour changed: the server still serves the whole wait and the 1200-second bound is untouched.
- The guide now separates the two deliveries that share a word. `delivery` describes this plugin's own channel — whether a task event can wake the coordinator — and says nothing about what a client does with an outstanding MCP request. `delivery=poll` means this plugin will not be the one notifying, not that no notification is possible.

## 3.9.0 — 2026-09-16

### Changes

- `tandem_result`, `tandem_wait`, `tandem_work` and review-run status accept `wait_seconds` up to 1200, and a new `wait_mode="bounded"` waits for the observed state instead of returning as soon as event delivery could wake the caller. Defaults and the existing `auto` behaviour are unchanged. Every wait stays finite and cancellable, extends no turn budget, question deadline or review budget, and now reports why it ended in a `wait` block; expiry is an observation, never a task state. A response to a bounded wait is not handed back to event delivery the caller declined. Reads back off as a wait lengthens while cancellation keeps its original cadence, and a request longer than the client's own deadline is still the caller's to size: the server does not negotiate it.
- `tandem_work(action="get")` accepts `after_revision`, the revision the caller last read, so a card already past it returns at once. The baseline used to be taken when the call began, which made a revision committed between two reads wait for the next one. It is an observation baseline only: not `expected_revision`, no claim, no permission to replay work, and a baseline ahead of the card is refused rather than waited for.
- A review capture that receives a changed file as `context_paths` now reports every repairable path in one refusal with a proposed selection patch, instead of surfacing them one refused capture at a time. `tandem_review(action="prepare")` runs the same classification without saving anything, reserving nothing and launching nothing. The patch is described, never applied: which files are reviewed stays the caller's decision, a repaired request is a new logical request, and `complete: false` marks a scan that stopped for something promotion cannot repair. A failed `tandem_review_run` carries the same object as `capture_diagnostics`.
- `tandem_findings` gains `pin` and `project`. Pinning fixes the membership of a correction set once — every finding bound to that snapshot that is neither rejected nor already verified fixed — so closure can be answered later; a live list of open findings cannot answer it, because closing one removes it from the list. The projection reports each member's baseline and current revision, the revisions recorded since, and a disposition, naming an unreadable member as unknown rather than dropping it. It records and decides nothing: accounting for every member is not a claim that a fix holds, and `verify_fixed` still needs its own completed verification task.
- The coordinator instructions and the EN guide describe the bounded wait as the idle path when there is no other work to do, rather than a chain of short waits.

## 3.8.1 — 2026-09-16

### Fixes

- Review capture reads the Git state of an explicit `review_directory` inside the launch project instead of always using the project root, so work that lives in a nested repository can be captured by `tandem_review` and the `tandem_review_run` scenario. The selector chooses Git context only: the launch-project boundary, the component-by-component symlink-free working-file reads and the project-relative saved paths are unchanged, and the captured directory is recorded in the manifest and reused when a snapshot is assessed later.
- Selected paths that reach into a nested repository are refused and name the directory to pass, rather than silently reporting every nested file as newly added because the enclosing repository does not know that history. An unresolvable base now says the same thing.
- Automatic selection at the project root skips a nested repository's directory entry instead of failing the whole capture with an unsafe-path error, and files the enclosing repository still tracks under such a subtree are refused with the same guidance rather than captured against the wrong history.
- The nested-repository rule also applies when the project root is not itself a Git repository, where those files would otherwise be saved as plain additions with no history at all.
- A capture that names a directory binds one no-follow descriptor for the whole capture, and the Git child changes into that descriptor rather than into a pathname. A directory replaced during the capture can no longer redirect later reads to another repository; the regression fails without this binding.
- Snapshot assessment reuses the recorded directory for identity, index, staged bytes and the reviewed commit's existence, so an unchanged nested snapshot is no longer reported stale or unknown. Manifests captured before this change keep assessing against the project root.
- Working-file reads of a bound capture start at the same opened directory as its Git reads, so one snapshot can no longer combine one directory's history with a replacement directory's live bytes.
- The binding spans the whole capture operation rather than a single attempt, so a mutation retry cannot publish a repository that took the original directory's place.
- A recorded directory that has since been removed or replaced makes applicability report unknown instead of raising out of the reader. Only acquiring the directory is handled that way, so an input/output failure later in the assessment still reports the staleness already observed alongside its unknown cause.

## 3.8.0 — 2026-09-14

### Changes

- Pinned product-context capsules retain required rules and current decision provenance, with a bounded `tandem_context_read` reader for omitted examples/details. Unchanged continuations do not repeat the complete snapshot; explicit full delivery remains available.
- Start/continue `preflight=true` checks declared Git entry/boundary paths, actual execution capabilities and cumulative verification estimates without dispatch or reservation. Live-route claims remain attributed evidence, not proof inferred from file existence.
- Explicit `continuation=fresh` records a bounded source-backed handoff and starts a new native context without copying history, claims, grants or acceptance. Existing mode/root/policy/model constraints remain; unsafe source work is refused rather than replayed.
- Managed workers receive role-specific step capsules. Implementation and review requirements/verification are separate; required global rules are not truncated and reviewer write requirements remain invalid.
- Corrective review captures a new exact snapshot linked to the prior fingerprint and declared open findings, with a source delta and explicit prior-exposure provenance. It never transfers an old verdict to new bytes.
- Explicit targeted/candidate/integration check ladders preserve required earlier phases and structured check history; success needs selected current passing check evidence. Commands and estimates are declarations, not automatic execution or permission.
- Public lists and histories use SQL pagination. Large summary sections have explicit bounded readers, actor/stage/revision-bound cursors and no unbounded remaining-ID arrays. Recent-task summaries avoid full artifact/report/conversation processing; requested full answers remain available.
- Operator runtime pin/refresh/unpin selects verified immutable prepared generations for future launches. Candidate mode uses separate private state and local no-provider diagnostics; active runtime generations are never overwritten.
- New runtimes reject unsupported state schemas before migration. EN/RU/ZH guides and skills document the new surfaces and safety boundaries.

### Verification

- Local final regression suite: 728 tests and 537 subtests passed; two existing Authlib deprecation warnings remain.
- Real pinned OMP/SDK and fresh installed-wheel verifier: 18 checks passed, including context retrieval, no-dispatch preflight, unchanged-context invariants and fresh-history isolation. The recorded fixture packet shrank from 10,568 to 2,824 bytes on continuation; this is not a provider-cost benchmark.
- Independent review findings were reproduced and fixed, including comparison-admission races, initial/unbound independent disclosure, corrective finding provenance/update linkage and chronological/current-input check assessment. The concurrent recapture regression fails on the pre-fix source and passes after transactional admission checks.
- Actual launcher pin/candidate/refresh smoke preserved the old generation and sentinel live state. CI builds and checks an installed wheel on both Linux and macOS.

### Remaining limits

- Capsules reduce bytes, not necessarily invoice cost by the same ratio. Required policy remains inline; omitted data must be retrieved from its pin without assuming native compaction retained history.
- Fresh is explicit, not automatic retry or a remedy for unresolved side effects/provider-policy refusal. Corrective review is prior-exposed; independent assessment and exact acceptance remain separate.
- Declared verification does not prove commands ran or that a path is a live production entry. Existing no-shell independent-review and operator gates remain.
- Schema guards cannot retroactively control older binaries that ignore them. Stop/inspect old clients and back up before intentionally upgrading live state; candidate fixtures must remain separate.
- Compatibility evidence uses pinned binaries and isolated localhost models, not user credentials or external-model evaluation. Helpers remain disabled; historical unexplained cleanup/wake failures are not explained by later passes.

## 3.7.1 — 2026-09-13

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
- Release preparation implies no automatic application of the accepted result to a user checkout. Operator authority, explicit application and distinct independent acceptance are unchanged.

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
