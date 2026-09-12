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

Before activating an operator grant, explain `authorize --preview` (`stored=false`, no activation or readiness proof), `--claude-model` (default `sonnet`) and `--omp-model`/`--model` before the subcommand, plus `--max-attempt-cost-usd` and `--allow-shell` after it. The attempt ceiling defaults to half the total independently of max_launches; `preview.permissions` and reserve policy show real capabilities. `--allow-tests` is a deprecated alias granting the same arbitrary shell, not a test sandbox. Run/start cannot replace pinned model selections. Shell-granted managed review produces an operator blocker before launch because stage-confined shell is unavailable; do not silently waive checks or permissions. Managed reviewers read the pinned commit snapshot; the shared flow is independent report, optional comparison, then separate verdict. Helpers remain disabled; `docs/helper-compatibility.md` records the five unsatisfied gates.

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

`tandem_work` defaults to `view="summary"`; sibling `view=plan|step|full`, `format`, `limit`, `cursor`, `include_snapshots` select permitted material. History is paged without snapshots by default; follow continuations and refetch after `cursor_stale`. `next_actions` are hints. `wake_acknowledgment` distinguishes `acknowledged` (zero is valid), `deferred` and `not_attempted`; a deferred ack is not a failed state read or permission to relaunch.

Before independent comparison, `clarification_requires_new_snapshot` cannot be answered with author text, including native manual claims. Missing unchanged files need exact `review_context_paths` and a new agreed snapshot; changed plan/submission/grant/input bytes cannot inherit an old waiver. Manual disclosure is not snapshot confinement.

Recovery descriptors expose permitted material and reason codes, never secrets. Only an explicit operator `omp_tandem.work_daemon ... successor ATTEMPT_ID --host HOST_OWNER --principal claude|omp --note ...` authorizes that host to `recover` the same stopped valid claim for report-only closure. It does not launch a model/test, extend expiry, change submission/stage or migrate grants. Foreign/retired/expired/legacy-unbound/unconfirmed-stop claims cannot gain authority. Preserve `(code=cyber_policy)` as `provider_policy_refusal`; a prose mention is not that code. Review-phase refusals use reason codes; recovery is not provider-policy reformulation.

Use `show WORK_ID --format markdown` for the report; non-get tool Markdown is fenced JSON. Missing application evidence is `not_recorded`, not applied: `assess` records expected/observed HEAD/time, while explicit apply receipts are separate from acceptance/publication. Native linked usage is counted once and may be only a subtotal; Claude/unattributed cost remains unknown. Existing receipts stay historical and do not restore permission. Keep failed-run evidence, including the unexplained one-off `TemporaryDirectory` cleanup failure in `tests/test_review_runs.py`; rerun success does not explain it.

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
