# Real OMP compatibility verification

The pinned combination is **official OMP 18.1.13** with the Python RPC SDK
at **`daf07999c2fee9b22edc7bf8fea1fb6272e0df5e`**. The checked-in
[local verification report](compatibility-result.json) passed on Darwin arm64 /
Python 3.13.5 with Tandem 3.3.0; temporary paths are redacted. This is evidence for
that exact combination, not a supported-version range or proof of every provider.
CI runs the same verifier on Linux and macOS; consult its actual run result
rather than treating the presence of a workflow as a pass.

## Reproduce without provider credentials

From the repository, after `uv sync --frozen --group dev`:

```sh
uv run --frozen python scripts/verify_omp.py --cache-dir /tmp/tandem-omp-cache --report /tmp/tandem-omp-compatibility.json
```

This single finite command downloads the platform-specific official release
binary to the explicit cache directory, verifies its SHA-256 **before execution**,
and runs the checks. It never installs or replaces a global `omp` executable.
The entire command has a 480-second deadline, with shorter startup, request,
task, cancellation, and teardown bounds. Network transfer speed can make the
first download exceed the deadline; rerun the same command after resolving the
network issue. An incomplete download is removed rather than cached.

To check a binary already on disk, add `--omp /absolute/path/to/omp`. The provided
binary must still match the pinned official asset digest; its actual digest and
`--version` output are recorded. A wrong or corrupt binary fails closed, including
a corrupt cache entry. Remove only that reported cache file and rerun to download
it again. This verifier deliberately does not certify custom builds.

Supported pinned assets are macOS arm64, macOS x64, and Linux x64 (glibc). The
existing CI matrix runs the same command on `ubuntu-latest` / Python 3.12 and
`macos-latest` / Python 3.13. The older isolated/fake-RPC regression suite remains
in CI; it does not substitute for this real-binary check.

## What executes for real

Only **model HTTP responses** are scripted. The official OMP binary and pinned
SDK perform the actual RPC negotiation, host-tool registration, execution,
session persistence, and cancellation. No fake RPC server, monkeypatch of the
worker, or dummy native-execution fallback is used.

The checks exercise:

1. Official binary SHA-256, actual version, and installed SDK source pin.
2. SDK startup, `get_state`, and `set_host_tools` with `loadMode: essential` while
   `--no-tools` is active. The probe checks the actual `dumpTools` registration.
3. The production `Bridge.start` → native worker → `Bridge.view(details=True)`
   flow in **think**, **analyze**, and **work** modes. The fixture attempts real
   `write`, `bash`, and `read` calls, even when those tools were not advertised.
   Think/analyze must not create the target files and must return unavailable-tool
   errors for mutation calls. Think must not expose/read the live input file;
   analyze must return its content through native read. Work must create the
   expected temporary file through native write and another through native bash.
4. Essential `tandem_finish` registration at the model boundary and delivery of a
   structured success outcome with the exact full answer through the real host
   callback, not a success inferred from assistant prose.
5. Saved-session continuation through `Bridge.start(conversation_id=...)`, with
   the same native session file and previous assistant history in the model input.
6. Cancellation through `Bridge.cancel` while the real binary is waiting on a
   held localhost model response. Cancellation must reach a terminal cancelled
   task without claiming a success outcome.

The tool restrictions tested here are tool availability, **not an OS filesystem
sandbox**. This does not certify every native tool, interactive UI, language
server, provider, or the absence of all network access by the OMP binary.

## Isolation and honest evidence

A fresh temporary HOME, OMP agent directory, XDG directories, project, and Tandem
state directory are used and removed on exit. The environment is rebuilt from a
small fixed allowlist; user PATH, provider credentials, auth broker settings,
proxy overrides, SDK injection settings, shell prefixes, and user configuration
are not copied. The native shell runs without login startup files. No changes
are made to user authentication, global binaries, or permission grants.

A deterministic HTTP/SSE server binds only to `127.0.0.1` on an ephemeral port.
The only selected model is `tandem-compat/fixture`, configured with `auth: none`;
all model traffic goes to this local server. The fixture rejects unexpected
routes, model IDs, authorization headers, oversized requests, and excessive
requests. It controls model outputs only and records the tool results OMP sends
back. There are no paid-provider requests. HTTP connections, server threads,
OMP process groups, and Bridge workers have bounded lifetimes and are cleaned
up, including the held cancellation request.

The JSON report records exact binary identity, SDK version/revision/source,
installed and source Tandem versions, Python, OS/kernel, architecture, per-check
status and elapsed seconds, bounded native tool results, and failure details.
CI prints this bounded report directly in the step log (also on failure) and
writes the JSON to the runner temporary directory. No additional upload-action
pin is needed. These timings and synthetic token counts are compatibility
fixture evidence, **not model-performance or cost benchmark results**.

## Pin sources

- [Official 18.1.13 release](https://github.com/can1357/oh-my-pi/releases/tag/v18.1.13)
- [Official asset metadata and SHA-256 digests](https://api.github.com/repos/can1357/oh-my-pi/releases/tags/v18.1.13)
- [Pinned model/provider configuration reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/models.md)
- [Pinned RPC reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/rpc.md)
- [Pinned environment reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/environment-variables.md)

`config/omp-compatibility.json` is the checked-in source of binary and SDK pins.
Update it only with independently verified official release metadata and rerun
the real command on each supported CI platform before claiming compatibility.
