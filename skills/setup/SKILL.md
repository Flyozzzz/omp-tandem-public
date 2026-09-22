---
name: setup
description: Set up or diagnose OMP Tandem, uv, Oh My Pi, provider authentication, and Claude Code, Codex, or manual MCP registration. Use when tandem tools are missing, startup reports prerequisites, or the workspace boundary needs inspection.
---

# Set up OMP Tandem

Separate three concerns: install the external executables once, let the MCP launcher prepare its Python runtime, and have the user authenticate their chosen provider in OMP. Do not read credential files, copy tokens, infer an account, change global settings without permission, or run a paid diagnostic without the user's request. Explain changes and get user consent before installing tools, preparing dependencies explicitly, or adding/removing client registrations.

OMP Tandem is MIT-licensed in the public `Flyozzzz/omp-tandem-public` repository. Local plugins require a host that can run local stdio MCP subprocesses on macOS/Linux or WSL. Installing this package in a web-only client does not deploy a server there. Optional hooks use a POSIX shell; Claude watchdog hooks also use the existing `uv` prerequisite with an offline cached Python interpreter.

The first-run goal is one useful task, not learning every MCP tool. Guide the user through **installation/provider setup → correct project and local diagnosis → first read-only review**. A separate paid live check is optional and requires explicit approval; do not force it before an already requested useful provider task.

Shared tasks use the same project-bound MCP connection through `tandem_work`; they do not require an autonomous service just to create or inspect a card. Unattended work additionally needs Git and both configured Claude/OMP executables. Explain the exact task's time/launch/reported-cost and edit/shell grants before the operator uses `omp_tandem.work_daemon authorize` and `run`/`start`. Never grant autonomy, install a global service, resume a user terminal, or bypass permissions merely to diagnose setup. For operator commands use this package's prepared interpreter (`server.py --prepare` reports `python`) or `uv run --project <package> --frozen python`, not an assumed global module installation.

For shared claim/submit, the bound project root itself must be a Git repository with a HEAD commit, even in manual mode. `Work execution requires a Git repository with an immutable HEAD commit` can mean the client launched from a parent directory; correct the launch boundary, not task `cwd`. Do not initialize an unintended directory merely to silence the error.

Before activating an operator grant, explain `authorize --preview` (`stored=false`, no authority/readiness proof), model flags before authorize, and `--max-attempt-cost-usd`/`--allow-shell` after it. `preview.model_selection` distinguishes fixed selectors from OMP's dynamic configured default; selectors are not observed model identity. Recommend explicit `--omp-model` for predictable managed runs. Legacy-unpinned OMP or malformed policy needs fresh authorization; run/start cannot override the saved selection. Per-attempt cost defaults to half the total, independently of launch count. `--allow-tests` remains the deprecated arbitrary-shell alias, not a test sandbox. Without the fresh review-check capability below, legacy shell-granted reviews retain their operator blocker; never silently waive checks. Helpers remain disabled; see `docs/helper-compatibility.md`.

From 3.11, a fresh `--allow-review-checks --review-check-image IMAGE` grant authorizes only supervisor-owned commands declared in `review_verification`, after the immutable independent report. Require a running local Linux Docker engine and a prepared image with the project's tools; no image pull or host fallback. Preview pins the local daemon and immutable image ID; optional `--review-check-docker-context NAME` selects the context. Network defaults to none; `--review-check-network NAME` explicitly pins an existing bridge. `--review-check-timeout` bounds each command; repeated `--review-check-env NAME` pins selected value hashes. Reviewer Bash stays unavailable. Acceptance requires comparison, passing checks, unchanged inputs and confirmed container removal; external effects are not rolled back. Legacy grants/waivers never gain authority. Unknown execution/cleanup never retries. See `docs/guide.md#controlled-review-checks`.

## 1. Inspect without changing state

Determine the actual package directory from this skill's installed location: it is two directories above `skills/setup/`. Do not confuse a cached installation with the user's working project. In the examples, replace the path with that package directory:

```sh
TANDEM_ROOT='/absolute/path/to/omp-tandem'
command -v uv
command -v omp
```

If an existing Python 3.12 or newer is available, use it for a dependency-free diagnostic:

```sh
python3 -I "$TANDEM_ROOT/server.py" --doctor
```

If using uv instead, note that uv may first download a compatible Python interpreter; obtain consent for that provisioning when needed:

```sh
uv run --no-project --python '>=3.12' python -I "$TANDEM_ROOT/server.py" --doctor
```

`--doctor` reports executable/dependency prerequisites without installing application dependencies or probing provider authentication. Do not mistake an executable's presence for working provider credentials. The prerequisite diagnostic handler only checks whether `uv` and `omp` are on `PATH`; separate Claude session handlers establish and invalidate watchdog ownership without calling a model.

### Loaded runtime, compact state and recovery

Inspect `runtime_identity` from the connected runtime and every task result, not only files on disk: package version, distribution origin, exact build verified against `RECORD`, checkout observation and registered schema digests are separate facts. Editable/unverifiable builds remain unknown; requested/effective/observed models and manual/managed/grant/disclosure provenance are not interchangeable. Updating a checkout does not update the already loaded server. Do not interrupt user sessions or migrate grants during diagnosis.

For self-host development, keep the controller on a verified prepared generation: launcher `--prepare` returns `key`/`python`, `--runtime-pin KEY` selects it, `--runtime-info` inspects it. Explicit `--runtime-refresh` affects future launches only; `--runtime-unpin` restores normal update-following. Stale/unsafe pins refuse. `--candidate --candidate-smoke --project-root FIXTURE` bypasses the pin, creates private temporary state and performs local diagnostics without a model; it must not inherit live state or managed authority. Inspect and remove only its emitted fixture state when finished. New schema guards cannot restrain older binaries that ignore them: stop/inspect clients and back up before intentional upgrades. See `docs/guide.md#context-efficient-work`.

`tandem_work` defaults to `view="summary"`; sibling `view=plan|step|full`, `format`, `limit`, `cursor`, `section`, `include_snapshots` select permitted material. SQL list/history and explicit section pages bound output; concatenate section content before JSON decoding, and refetch after `cursor_stale`. `next_actions` are hints. `wake_acknowledgment` distinguishes `acknowledged`, `acknowledged_zero`, `deferred` and `not_attempted`; a deferred ack is not a failed read or permission to relaunch. Start/continue `preflight=true` checks declared paths/capabilities/check estimates without a worker or reservation.

Before independent comparison, `clarification_requires_new_snapshot` cannot be answered with author text, including native manual claims. Missing unchanged files need exact `review_context_paths` and a new agreed snapshot; changed plan/submission/grant/input bytes cannot inherit an old waiver. Manual disclosure is not snapshot confinement.

Recovery descriptors expose permitted material and reason codes, never secrets. Only an explicit operator `omp_tandem.work_daemon ... successor ATTEMPT_ID --host HOST_OWNER --principal claude|omp --note ...` authorizes that host to `recover` the same stopped valid claim for report-only closure. It does not launch a model/test, extend expiry, change submission/stage or migrate grants. Foreign/retired/expired/legacy-unbound/unconfirmed-stop claims cannot gain authority. Preserve `(code=cyber_policy)` as `provider_policy_refusal`; a prose mention is not that code. Review-phase refusals use reason codes; recovery is not provider-policy reformulation.

Use `show WORK_ID --format markdown` for the report; non-get tool Markdown is fenced JSON. Missing application evidence is `not_recorded`, not applied: `assess` records expected/observed HEAD/time, while explicit apply receipts are separate from acceptance/publication. Native linked usage is counted once and may be only a subtotal; Claude/unattributed cost remains unknown. Existing receipts stay historical and do not restore permission. Keep failed-run evidence, including the unexplained one-off `TemporaryDirectory` cleanup failure in `tests/test_review_runs.py`; rerun success does not explain it.

### Plan transitions and repository-scope diagnosis

Do not repair a stalled task by changing its root, granting yourself operator authority or reinstalling a running session. Read `repository{project_root, scope_id, provenance, initial_observation}` and `repository_observation`: create/propose/claim record verified/unverified status, Git toplevel and HEAD, not a new pin. `owned_files` and exact `review_context_paths` validate existing ancestors, including new paths. Nested repositories, `.git` files/worktrees and submodule contents are separate boundaries; owning the parent gitlink entry itself passes this path check without overriding managed-snapshot submodule refusal. Symlinks are rejected without dereference.

A boundary diagnostic names path, pinned root and detected boundary: “Launch Tandem at /outer/child and create a separate card in that scope”. Symlink diagnostics identify the link. Never open/search the nested repository to bypass scoped IDs or repin an old card. Non-Git pathless planning remains `unverified`; plans with paths and all claims require the correct Git root with HEAD. A task cwd does not change the launch boundary.

If a commit cannot resolve, submission refuses before intent: `Submitted commit SHA could not be resolved in pinned repository ROOT: GIT_CAUSE`, with bounded cause and next steps: create a separate correct-scope card, ask the operator to stop/dispose and cancel/supersede the mistaken card, never substitute an unrelated commit. `Source` errors refer to the pinned source, not the submitted hash.

Distinguish active `plan_revision` from pending `proposal{proposal_id, base_plan_revision, preview}`. A differing MCP `propose` records a card-wide preview (`attempts`, changed/removed/added steps, including unchanged-step attempts) and leaves active execution, agreements and grant alone. New differing proposals replace the pending one; proposing the active plan withdraws it before begin. During an open transition any propose fails with `transition_in_progress`. Agree still concerns the active plan.

Only the operator CLI may transition begin/resolve/activate/withdraw, cancel, link, reconcile or authorize. Agents propose, report, submit and review; diagnosis is not consent to act as operator. Show `operator_commands` with exact IDs, prepared interpreter, quoted root/state and observed revision. `transition inspect` returns `commands` plus inventory/stop requirements; `next_actions` transition hints have `allowed=false`, `blocked_reason="operator_required"`.

The syntax below is for explanation to the operator, not automatic execution. Use the prepared package Python (`uv run --frozen python` in a checkout). ROOT and optional STATE must match MCP; N is the observed card revision, not plan revision. Brackets are optional, `|` alternatives. Replace placeholders with observed identities and real evidence. Prefer `--expected-revision N` and `--operation-id ID`, re-read after mutation; exact replay returns historical `replayed_operation.outcome`. The fingerprint covers the entire command including revision, note/evidence and flags; different content under the same ID is refused.

```text
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] transition WORK inspect
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] transition WORK begin --proposal PROPOSAL [--expected-revision N] [--operation-id ID] --note NOTE
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] transition WORK resolve --transition TRANSITION --attempt ATTEMPT --note NOTE --evidence EVIDENCE [--confirm-stopped] [--abandon] [--saved-commit SHA] [--expected-revision N] [--operation-id ID]
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] transition WORK activate (--transition TRANSITION | --proposal PROPOSAL) [--acknowledge-capture-failure ATTEMPT] [--expected-revision N] [--operation-id ID] --note NOTE
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] transition WORK withdraw (--transition TRANSITION | --proposal PROPOSAL) [--expected-revision N] [--operation-id ID] --note NOTE
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] reconcile WORK STEP --resolution retry|abandon --confirm-stopped --note NOTE --evidence EVIDENCE
python -m omp_tandem.work_daemon --project-root ROOT [--state-dir STATE] unblock WORK --blocker BLOCKER [--step STEP] --resolution resolved|not_applicable --note NOTE --evidence EVIDENCE --expected-revision N --operation-id ID
python -m omp_tandem.work_daemon --project-root OLD_ROOT [--state-dir STATE] cancel OLD_WORK --disposition cancelled|superseded --expected-revision N --operation-id ID --note NOTE --evidence EVIDENCE [--continuation-root ABSOLUTE_CHILD_ROOT --continuation-work-id CHILD_WORK]
python -m omp_tandem.work_daemon --project-root CHILD_ROOT [--state-dir STATE] link CHILD_WORK --predecessor-root ABSOLUTE_OLD_ROOT --predecessor-work-id OLD_WORK --expected-revision N --operation-id ID --note NOTE --evidence EVIDENCE
```

Begin freezes the proposal, inventories every active and already recovery-required attempt and fences active credentials into `recovery_required`; no new claim may run during the open transition. Managed stop must be supervisor-confirmed (`supervisor_confirmed`). **Supervisor teardown confirms process stop, not absence of external effects.** Heartbeat/silence/model assertions cannot substitute. Manual stops require `--confirm-stopped` (`operator_attested`), never a replacement for managed teardown. The operator inspects effects and resolves each attempt with note/evidence. Default `superseded` is not acceptance; `--abandon` records `abandoned`, adds a blocker and pauses the card.

Managed saved work is preserved by `WorkWorkspace.preserve` on its recorded composed base or `saved.capture_failure` is recorded; manual `--saved-commit` validates like a submission. Activate requires the frozen proposal, every inventory entry disposed and no live/recovery-required attempts. `activate --proposal` without begin is only for no executing/recovery-required attempts. It installs a new draft plan revision, clears agreements/current acceptance, carries blockers (removed-step blockers become card-wide), revokes the grant and retains an existing pause. New agreements, blocker resolution, explicit resume when paused and fresh managed authorization are separate gates.

Continuation is `checkpoint` for validated bytes within new ownership, `not_transferable` for removed steps/shrunken ownership, `not_available` without validated bytes, including capture failure. Historical 3.7.0 `blocked` displays as `not_transferable` without rewriting history. These are transfer eligibility, not automatic blockers. The checkpoint gives commit/base/operator evidence to the new claim, not acceptance/replay. Before begin withdrawal only removes the proposal; after begin it leaves sticky quiescence: fenced attempts need operator reconcile and pauses need explicit resume. Emergency reconcile is operator-only; retry never automatically replays, abandon leaves a blocker/pause.

Inspect `activation_preview` in get, transition inspect and Markdown: `steps_with_checkpoint`, `steps_undetermined` (pending dispositions), `steps_without_checkpoint`, `capture_failures`, `capture_failures_unacknowledged`, and each attempt's preserved commit/files/outcome. `ready` means dispositions complete, not launch readiness. Every superseded capture failure requires an informed operator `--acknowledge-capture-failure ATTEMPT`, repeated per exact ID. Missing acknowledgment gives `capture_failure_unacknowledged`, unrelated IDs `capture_failure_unknown`. It records the loss of uncaptured saved work in command identity/outcome; successful preservation needs none. `--abandon` consistently records `abandoned` in attempt, inventory and operation receipt and keeps no continuation.

Operator unblock requires `--blocker`, `--resolution resolved|not_applicable`, meaningful `--note`, at least one repeatable `--evidence`, current card `--expected-revision` and fresh `--operation-id`. Explain how the condition was met or why it no longer applies. `--step` is the blocker's current location; omit for card scope, including moved blockers. `operator_commands`, `next_actions` and Markdown `## Operator commands` name current location, not historical origin. Hints are `allowed=false`, `operator_required`, not authority. The blocker author may resolve through MCP; diagnosis never grants operator authority. Unblock never resumes a paused card or authorizes execution.

For wrong-scope work, stop/dispose all original attempts first, launch a separate client in the correct child Git-root, inspect `tandem_scope`, create a separate card and obtain fresh agreements. Cancel cannot stop execution and refuses live/recovery-required attempts or undisposed begun inventory. It records closure note/evidence/actor/time/revision/continuation/withdrawn_proposal_id, clears the proposal, archives a disposed open transition as cancelled and revokes the grant. Both cancelled/superseded dispositions produce terminal `status="cancelled"`, not acceptance; agree/propose/claim/resume/activate/authorize fail with `work_terminal`, while get/history remain readable.

Link records predecessor provenance in the successor's own scope; root/ID are separate options. It may append provenance after closure, not reopen execution. `target_verification=not_performed` and `reciprocal_link=unverified` remain even when both directions exist. No cross-scope reading, agreements, grants or acceptance transfer. Independent acceptance must happen only on the exact child submission in the child scope. `show WORK --format markdown` displays closure/continuation/predecessors. Acceptance is separate from application.

Local 3.8.0 packaging (`uv build --wheel`, `uv run --frozen python scripts/package.py`, then `--check`) is a proposal, not publication, tagging, user-session installation or application; only the operator decides. See `docs/guide.md#plan-transitions`, `#repository-handover` and `#context-efficient-work`. The deterministic managed-replan fixture proves real supervised Claude-child stop and checkpoint continuation with one fixture effect; not generic exactly-once effects, OMP-native teardown or live-provider replan.

## 2. Install missing external tools once

Present the appropriate official method and run only the user's chosen method after approval. Do not execute every alternative. No Bun installation is required for the Homebrew or standalone OMP options.

With an existing Homebrew installation:

```sh
brew install uv
brew install can1357/tap/omp
```

Official standalone installers for macOS/Linux (review downloaded scripts before executing if desired):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://omp.sh/install | sh
```

With Bun already installed at a version supported by OMP:

```sh
bun install -g @oh-my-pi/pi-coding-agent
```

Do not install Bun merely to run that alternative when Homebrew or standalone installation is suitable. Use the official [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) and [OMP installation instructions](https://github.com/can1357/oh-my-pi#install) for other platforms and current requirements. Ensure the client process, not just an interactive shell, inherits a `PATH` containing `uv` and `omp`; restart it after changing the environment.

## 3. Prepare Python dependencies

Normal MCP startup automatically prepares the frozen package runtime. For explicit preparation before connecting the client:

```sh
uv run --no-project --python '>=3.12' python -I "$TANDEM_ROOT/server.py" --prepare
```

This may download Python and install locked dependencies. The command prints a JSON preparation result. The runtime is a non-editable installation cached outside the plugin code directory, under the client-provided `PLUGIN_DATA` or `CLAUDE_PLUGIN_DATA`, or otherwise `~/.cache/omp-tandem`. Different source/build inputs get separate runtime identities. Preparing dependencies does not configure OMP providers or move project data into the runtime cache. A preparation performed outside the client may use a different cache base than that client's later launch.

## 4. Let the user choose and authenticate a provider

Have the user run OMP interactively:

```sh
omp setup
omp
```

Inside OMP, `/login` handles supported provider sign-in and `/model` selects the model/role. Follow the chosen provider's actual setup requirements; some providers use API keys, some account sign-in, and some local servers. The user should enter secrets directly in OMP or its documented secure setup path, not paste them into the MCP conversation.

OMP supports multiple providers and custom compatible endpoints; this is not a guarantee that every arbitrary model can execute this workflow. Choose an OMP-supported provider/model with adequate tool calling and instruction following for the requested work. Do not assume that the MCP host's account authenticates OMP, or impose a particular provider, model, or subscription. Confirm any billable model smoke request with the user separately from executable/dependency diagnostics. See [OMP provider configuration](https://github.com/can1357/oh-my-pi#sixty-plus-providers-a-thousand-models-one-model-away).

## 5. Choose one registration route per client

Before adding anything, inspect the client's existing plugin/MCP registrations and keep one OMP Tandem registration. A plugin includes the MCP server; do not also add it manually under another name. Removing an old registration requires consent and must preserve unrelated configuration and project data.

### Claude Code plugin

For the public GitHub repository:

```sh
claude plugin marketplace add Flyozzzz/omp-tandem-public
claude plugin install omp-tandem@omp-tandem
```

Or register a local checkout as the marketplace, then install the same plugin identity:

```sh
claude plugin marketplace add "$TANDEM_ROOT"
claude plugin install omp-tandem@omp-tandem
```

Select the appropriate installation scope and follow the client's reload/restart instructions. Use `/omp-tandem:setup` or `/omp-tandem:tandem`. Review optional hooks through the client's normal controls; do not bypass permissions or automatically enable model trust. The Claude manifest loads its own MCP and hook configs, not the portable MCP config.

### Codex / Agent Plugins

Use current Codex with portable Agent Plugins 1.0 support:

```sh
codex plugin marketplace add Flyozzzz/omp-tandem-public
codex plugin add omp-tandem@omp-tandem --json
```

For a local checkout, replace the first command with:

```sh
codex plugin marketplace add "$TANDEM_ROOT"
```

Alternatively open `/plugins` in Codex, select the `omp-tandem` marketplace and install `omp-tandem`. Start a new session. Other Agent Plugins clients discover root `plugin.json`, `mcp.json`, and `skills/` according to their supported installation flow. Only OpenAI clients interpret `extensions.com.openai`; no legacy `.codex-plugin` shim is required.

Codex users may review the optional diagnostic hook with `/hooks`. Installing the plugin does not trust hooks automatically, and untrusted/disabled hooks must not prevent MCP use. Do not use hook-trust bypass flags. Portable MCP subprocesses default to the plugin directory under the standard: that directory is not the user's project. Check `tandem_scope` in the intended project and use the launch/workspace-binding instructions in the package README; never fix an unbound instance by passing a task `cwd`, inventing a session-root placeholder, or storing a shared last-project value.

### Manual stdio MCP, without a plugin

From the intended project, configure executable `uv` with arguments `run`, `--no-project`, `--python`, `>=3.12`, `python`, `-I`, and the absolute package path to `server.py`. Isolated Python prevents project/PYTHONPATH modules from replacing bootstrap imports; it does not sandbox OMP. For Claude Code, one local registration is:

```sh
claude mcp add --scope local --transport stdio omp-tandem -- uv run --no-project --python '>=3.12' python -I "$TANDEM_ROOT/server.py"
```

For Codex without the plugin:

```sh
codex mcp add omp-tandem -- uv run --no-project --python '>=3.12' python -I "$TANDEM_ROOT/server.py"
```

A typical JSON client configuration is:

```json
{
  "mcpServers": {
    "omp-tandem": {
      "command": "uv",
      "args": ["run", "--no-project", "--python", ">=3.12", "python", "-I", "/absolute/path/to/omp-tandem/server.py"]
    }
  }
}
```

Use the client's documented stdio schema and workspace-binding mechanism. Do not copy `${PLUGIN_ROOT}` or `${CLAUDE_PLUGIN_ROOT}` into a client that does not expand them. Leave the caller's intended working directory intact; never set it to the package directory to make imports work. The canonical launcher handles dependencies and package imports itself. Manual MCP does not install skills or hooks; the tools remain fully usable without either.

### Optional one-command Claude launch

For a user who wants the webhook without retyping environment variables and channel flags, follow the `claude-tandem` shell-function instructions in `docs/guide.md#one-command-launch` (localized guides are available). Explain and get consent before editing their shell configuration. This is currently manual setup, not an automatically installed launcher. Use the development-plugin channel identity for a custom channel; do not confuse it with the standalone server or an allowlisted plugin. Do not add `--dangerously-skip-permissions` by default, replace the ordinary `claude` command, or claim a hook can enable parent-process Channels.

## 6. Confirm the right boundary before using a model

After reconnecting, discover the actual namespaced `tandem_scope` tool and check its project identity from the intended workspace. It must not refer to a plugin cache or an unrelated project. Data isolation is not an OS sandbox; additional work directories do not grant access to another project's MCP history. Keep legacy migration copy-only and use explicit export/import for authorized context sharing.

Use `tandem_diagnose()` for current-client local diagnostics. If the user requests a live check, call `tandem_diagnose(live=true, expected_project=...)`; explain that it starts one short provider request and may incur a charge. Continue inspecting its returned task ID if still running. Do not rerun paid diagnostics just to confirm a channel probe. Actual model/authentication, channel receipt and independently armed watchdog are separate observations.

Once setup is ready, ask for a concrete review requirement and whether the material is staged or working content. Use `tandem_review_run` for the first read-only review, with a stable request key and explicitly needed context paths. Follow run status until terminal, report complete answers and current applicability, and explain known peer cost versus unmeasured coordinator cost. Do not silently grant `work`, execute test commands or apply changes. Remind the user that closing the owning MCP session stops active work.

For binary/protocol compatibility, use the separately documented `scripts/verify_omp.py` check in `docs/compatibility.md`: a real checksum-pinned OMP binary with an isolated localhost test provider, not the user's paid account. This verifies the recorded binary/SDK combination, not arbitrary models or a claimed version range.

The Claude plugin includes a bounded `PostToolUse` `asyncRewake` watchdog plus session lifecycle handlers. Review/enable these through ordinary client controls. Confirm only a watchdog token delivered by a real hook wake, separately from the real channel probe. `delivery=push` without live watchdog coverage still requires bounded polling. The hook uses offline `uv` and cached Python; unavailable hooks/interpreter leave polling available. Do not interpret its control exit `2` as a failed OMP task or substitute ordinary `async=true`.

If startup fails, distinguish a missing executable, interpreter/dependency preparation failure, a client registration error, and an unavailable workspace binding. Report the actual error without exposing credentials. Do not repair by changing provider accounts, disabling isolation, or sharing project state. Consult the package README for the launch options supported by this installed version.

References: [Claude plugins](https://code.claude.com/docs/en/plugins-reference), [Claude marketplaces](https://code.claude.com/docs/en/plugin-marketplaces), [Agent Plugins 1.0](https://agent-plugins.org/specification), [OpenAI packaging](https://developers.openai.com/plugins/build/plugins), and [Codex hooks](https://learn.chatgpt.com/docs/hooks).
