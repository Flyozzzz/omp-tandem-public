---
name: setup
description: Set up or diagnose OMP Tandem, uv, Oh My Pi, provider authentication, and Claude Code, Codex, or manual MCP registration. Use when tandem tools are missing, startup reports prerequisites, or the workspace boundary needs inspection.
---

# Set up OMP Tandem

Separate three concerns: install the external executables once, let the MCP launcher prepare its Python runtime, and have the user authenticate their chosen provider in OMP. Do not read credential files, copy tokens, infer an account, change global settings without permission, or run a model request as a diagnostic. Explain changes and get user consent before installing tools, preparing dependencies explicitly, or adding/removing client registrations.

OMP Tandem is MIT-licensed at `Flyozzzz/omp-tandem-public`; repository visibility is controlled separately by its owner. Git-based installation requires access while the repository is private. Local plugins require a host that can run local stdio MCP subprocesses. Installing this package in a web-only client does not deploy a server there. The optional diagnostic hook requires a POSIX `sh`; on Windows it needs a compatible shell environment and can otherwise remain disabled.

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

`--doctor` reports executable/dependency prerequisites without installing application dependencies or probing provider authentication. Do not mistake an executable's presence for working provider credentials. The SessionStart hook only checks whether `uv` and `omp` are on `PATH`; it is silent when both are present, ignores its input, and never installs or starts anything.

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

For authorized access to the private GitHub repository:

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

## 6. Confirm the right boundary before using a model

After reconnecting, discover the actual namespaced `tandem_scope` tool and check its project identity from the intended workspace. It must not refer to a plugin cache or an unrelated project. Data isolation is not an OS sandbox; additional work directories do not grant access to another project's MCP history. Keep legacy migration copy-only and use explicit export/import for authorized context sharing.

If startup fails, distinguish a missing executable, interpreter/dependency preparation failure, a client registration error, and an unavailable workspace binding. Report the actual error without exposing credentials. Do not repair by changing provider accounts, disabling isolation, or sharing project state. Consult the package README for the launch options supported by this installed version.

References: [Claude plugins](https://code.claude.com/docs/en/plugins-reference), [Claude marketplaces](https://code.claude.com/docs/en/plugin-marketplaces), [Agent Plugins 1.0](https://agent-plugins.org/specification), [OpenAI packaging](https://developers.openai.com/plugins/build/plugins), and [Codex hooks](https://learn.chatgpt.com/docs/hooks).
