# Real OMP compatibility verification

The pinned combination is **official OMP 18.1.13** with the Python RPC SDK
at **`daf07999c2fee9b22edc7bf8fea1fb6272e0df5e`**. The checked-in
[local verification report](compatibility-result.json) passed on Darwin arm64 /
Python 3.13 with the integrated 3.5.0 source (accepted A–B chain plus G0);
temporary paths are redacted. This is evidence for that exact combination, not a
supported-version range or proof of every provider. The report also contains the
stage-G0 helper (native `task` subagent) probes described in
[helper-compatibility.md](helper-compatibility.md); their capability gates are
recorded separately from check success and `delegation.available` is `false`.
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
The entire command has a 600-second deadline, with shorter startup, request,
task, cancellation, and teardown bounds. Network transfer speed can make the
first download exceed the deadline; rerun the same command after resolving the
network issue. An incomplete download is removed rather than cached.

The command runs in three phases, and the report says which one failed:

1. `binary_preflight` names the pinned asset for this platform and decides
   whether the binary comes from `--omp`, from an existing cache entry, or has
   to be downloaded. A missing pin, an unwritable cache or a supplied path that
   is not a file is an **environment** failure; nothing is downloaded or run.
2. `official_binary_acquisition` downloads into the cache only when preflight
   found no binary. A timeout or a digest mismatch here is an **acquisition**
   failure. `official_binary_sha256` then compares the binary on disk with the
   pin, whatever its source.
3. `sdk_pin`, the isolated environment and `actual_omp_version` prepare the
   run; a failure there is still an **environment** failure and no probe has
   executed yet.
4. Every later check is a probe of the running OMP. Only a failure there is a
   **probe** failure. An unsupported helper capability is a measured gate in
   `delegation.unsatisfied_gates`, not a failure of any class.

The report records `phase` (`preflight`, `acquisition`, `setup`, `probes`,
`complete`), `failure_class` (`environment`, `acquisition`, `probe`, or `null`)
and `probes` (`not_run`, `failed`, `passed`). A failure raised outside any
check, such as the isolated environment not being creatable, is classified by
the phase that was running. A run that failed before the probe phase therefore
says `probes: "not_run"` even when preflight, the digest and the SDK pin
passed, and never counts as compatibility evidence. A failed rerun does not overwrite a passing report at the same
`--report` path: it is written next to it as `<name>.failed-<timestamp>.json`
and the passing file is kept. Cached binaries are reused across runs, so a
compatibility-only rerun with the same `--cache-dir` (or `--omp`) repeats the
probes without downloading again and without repeating unrelated test or
package checks.

To check a binary already on disk, add `--omp /absolute/path/to/omp`. The provided
binary must still match the pinned official asset digest; its actual digest and
`--version` output are recorded. The Homebrew `omp` 18.1.13 binary for Darwin
arm64 is byte-identical to the official release asset, so
`--omp /opt/homebrew/Cellar/omp/18.1.13/bin/omp` avoids the download. A wrong or corrupt binary fails closed, including
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
7. Helper probes (`helper_*` checks) that start the binary through the public
   SDK with `task` enabled — never through the production `Bridge` — and script
   parent and child model turns: a read-only `scout` child under a restricted
   parent, a full-access `sonic` child under the same parent, a project
   `.omp/agents/scout.md` substitution, `task.disabledAgents`, a configuration
   change between two spawns of one session, and a parent abort while the child
   stream is held. Each probe records the actual model-boundary requests
   (advertised tools, model, `reasoning_effort`, tool results) and writes a
   capability gate to `report.helper_capabilities`. A gate that is not
   `supported` is a documented blocker, not a failed run; see
   [helper-compatibility.md](helper-compatibility.md).

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
The selected models are `tandem-compat/fixture` (parent) and
`tandem-compat/fixture-smol` (helper role, reasoning-capable so the requested
effort is observable), both configured with `auth: none`; all model traffic goes
to this local server. During helper probes the fixture answers OMP's automatic
tool-less subagent label requests without consuming the script and counts them. The fixture rejects unexpected
routes, model IDs, authorization headers, oversized requests, and excessive
requests. It controls model outputs only and records the tool results OMP sends
back. There are no paid-provider requests. HTTP connections, server threads,
OMP process groups, and Bridge workers have bounded lifetimes and are cleaned
up, including the held cancellation request.

The JSON report records exact binary identity, SDK version/revision/source,
installed and source Tandem versions, Python, OS/kernel, architecture, per-check
status and elapsed seconds, bounded native tool results, helper capability gates
with their observations, the `delegation` verdict, and failure details.
CI prints this bounded report directly in the step log (also on failure) and
writes the JSON to the runner temporary directory. No additional upload-action
pin is needed. These timings and synthetic token counts are compatibility
fixture evidence, **not model-performance or cost benchmark results**.

For a release report refresh, keep only a completed passing run; never replace
this evidence with a failed or partial result. Preserve versions, digests,
observations and unsupported verdicts. Redact the generated
`tandem-real-omp-*` temporary root as `<tmp>`, including truncated path excerpts.
Do not refresh merely to change durations or temporary paths. The 3.5.0 refresh
records changed installed/source versions with the same helper capability verdicts.

## Pin sources

- [Official 18.1.13 release](https://github.com/can1357/oh-my-pi/releases/tag/v18.1.13)
- [Official asset metadata and SHA-256 digests](https://api.github.com/repos/can1357/oh-my-pi/releases/tags/v18.1.13)
- [Pinned model/provider configuration reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/models.md)
- [Pinned RPC reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/rpc.md)
- [Pinned environment reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/environment-variables.md)

`config/omp-compatibility.json` is the checked-in source of binary and SDK pins.
Update it only with independently verified official release metadata and rerun
the real command on each supported CI platform before claiming compatibility.
