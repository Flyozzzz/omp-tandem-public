# Real OMP compatibility verification

The pinned combination is **official OMP 18.1.13** with the Python RPC SDK
at **`daf07999c2fee9b22edc7bf8fea1fb6272e0df5e`**. The checked-in
[local verification report](compatibility-result.json) passed for the **3.8.0 package**
on **Darwin 25.5.0 arm64 / Python 3.13.5**, on 2026-09-14: 18 checks passed in
30.313 seconds, `phase="complete"`, `probes="passed"`, `failure_class=null`.
Its `tandem`, `provenance` and installed-flow `runtime_identity` fields identify
the exercised bytes and environments. This is evidence for that exact combination,
not a publication receipt, a supported-version range or proof of every provider. The report also contains the
stage-G0 helper (native `task` subagent) probes described in
[helper-compatibility.md](helper-compatibility.md); their capability gates are
recorded separately from check success and `delegation.available` is `false`.
CI runs the same verifier on Linux and macOS; consult its actual run result
rather than treating the presence of a workflow as a pass.

The observed binary was `/opt/homebrew/Cellar/omp/18.1.13/bin/omp`, reporting
`omp/18.1.13`; no download or global installation was needed.

| Observation | Exact value |
|---|---|
| Official Darwin arm64 OMP SHA-256 | `a4c5c9cc5b8222184d0d7429b0bb6ac2a92bbe45dd11bf68e4b1360050791909` |
| Tested wheel | `omp_tandem-3.8.0-py3-none-any.whl` |
| Wheel SHA-256 | `33c1fc04ab6f6da773e429b9564a2482a426ca35e556faf89f59ac9d6e776d92` |
| Installed runtime | `package_version=3.8.0`, `distribution_origin=installed_wheel` |
| Installed exact build | `sha256:b45509072190bf6cfd5c533faa257955147807bb1a3e7e30f93453650c0bb196` |
| Exact-build source | `verified_distribution_record` |
| Runtime protocols | `work-v2-presentation`, `independent-first-v1` |
| Verifier SHA-256 | `78fac52a5262324ec4cffb7ec63c31d9171c4715b06c95e17690d9dd66ca2b9e` |
| Frozen lock SHA-256 | `7cea468c1786356145e894c014d3822117e09e4aa8f136bac1db923241cd8f2b` |

The isolated installed-wheel shared-flow check passed in 11.385 seconds. Its
recorded distribution path is a removed disposable environment, not the current
client's runtime or a plugin installation. Both source and installed versions
were 3.8.0. Registered schema identities and observed wire census remain separate
evidence; `runtime_identity.schema_digests.compatibility="not_assessed"` is not
rewritten to a universal compatibility claim.

The console's “Missing structured outcome” failure is the deliberately exercised
negative case, not a failed overall run. Expected refusal diagnostics and
Authlib deprecation warnings are retained. Helper checks passing means the probes
executed; the same five capability gates remain unsatisfied.

The separate deterministic managed-replan regression uses a real `WorkSupervisor`
with an injected Claude child, real bound MCP and CLI transitions, cancellation/reaping,
immutable Git preservation and a checkpoint passed to the next managed claim.
Its owned-file marker guides continuation, with exactly one fixture effect record
across both attempts and the interrupted cost left unknown. This covers the
Claude-child/shared-supervisor boundary, not generic exactly-once effects,
OMP-native teardown or live-provider replan. See the [scenario command](guide.md#plan-transitions).

The installed 3.8 context flow additionally exercises real OMP RPC and MCP:
an analyze task declaring shell is refused before dispatch; a valid work
preflight creates no task/model request; omitted examples are read from the
exact pinned context; unchanged continuation keeps mandatory rules and
decisions without repeating its overview; explicit fresh starts without
replaying old native history. The recorded fixture packets were 10,568 bytes
initially and 2,824 bytes on continuation. These are task-packet byte counts,
not the entire provider context, a quality benchmark or invoice savings.

The separate launcher smoke prepared a real non-editable generation in a
disposable package/cache, pinned it, changed only that candidate source, and
observed the old generation still selected. Candidate smoke initialized separate
private state without provider calls or changing the sentinel live-state
directory. Explicit refresh selected the new generation; the old interpreter
still reported its unchanged exact build. No user pin, credentials or active
session was modified.

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

The command distinguishes acquisition, setup and probe phases:

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

8. A registered-versus-wire keyword census for `tandem_finish` and `tandem_work`
   on the actually exercised configured API, `openai-completions`. It records
   canonical/registered digests, wire digests, keyword counts and JSON-pointer
   differences. Other provider APIs are explicitly untested. Invalid success
   with failed/not-run checks and blocked without reasons are refused by the
   real OMP registered-tool path; upstream schema validation may report a
   variant's `outcome` error instead of the missing field. Short success reaches
   the host callback; missing finish is not success.
9. With `--installed-wheel /absolute/path/to/built.whl`, uv installs frozen
   runtime dependencies and that wheel into a new temporary venv before provider
   environment isolation. A separate `python -I` process requires
   `distribution_origin=installed_wheel` and a verified RECORD build. New fixture
   cards exercise manual MCP agreement/claim/submission, native OMP review claim
   and withheld author input, saved finding, missing-finish failure, operator
   `successor`, report-only same-claim `recover`, independent report, comparison
   and exact verdict. Another native manual claim exercises clarification refusal
   and rejected late reply. The Claude seat uses FastMCP Client, not a Claude
   model; no external model or existing session/grant is used.

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

For a release report refresh, use an actual completed passing run and preserve
failed attempts separately. Keep versions, digests, observations and unsupported
verdicts. `provenance` records the exact command and verifier/lock hashes; fixture
paths and install/CLI output remain evidence, not portable installation paths.
For the local 3.7.0 refresh, the actual command was run from the proposal worktree:

```sh
uv run --frozen python scripts/verify_omp.py \
  --omp /opt/homebrew/Cellar/omp/18.1.13/bin/omp \
  --cache-dir /Users/flyoz/itquick-aihub/omp-tandem-public/.worktrees/omp/.compat-370-final-9kdTNv \
  --report docs/compatibility-result.json \
  --installed-wheel dist/omp_tandem-3.7.0-py3-none-any.whl
```

The cache above was disposable and removed after the run; choose your own writable
disposable directory when reproducing. First build with `uv build --wheel` or
`uv run --frozen python scripts/package.py`. After updating the report and this
page, regenerate the source manifest/archive with `package.py` and verify with
`package.py --check`; the JSON's tested wheel digest identifies runtime evidence,
while the final archive also includes the refreshed documentation. Packaging and
the verifier's isolated installation do not publish a release, install a plugin
into a user session or apply code to a user checkout.

The separate [documented CLI/MCP end-to-end scenarios](guide.md#repository-handover)
exercise manual plan transition and outer/nested repository handover. They are not
part of this real-binary verifier's installed-flow coverage. No live-provider
replan scenario or counted external-effect process across managed replan was run
by these commands. Managed stop-confirmation gates and saved-byte preservation
have separate store/workspace regression coverage; do not combine these boundaries
into a claim of an external-provider end-to-end run.

## Pin sources

- [Official 18.1.13 release](https://github.com/can1357/oh-my-pi/releases/tag/v18.1.13)
- [Official asset metadata and SHA-256 digests](https://api.github.com/repos/can1357/oh-my-pi/releases/tags/v18.1.13)
- [Pinned model/provider configuration reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/models.md)
- [Pinned RPC reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/rpc.md)
- [Pinned environment reference](https://github.com/can1357/oh-my-pi/blob/v18.1.13/docs/environment-variables.md)

`config/omp-compatibility.json` is the checked-in source of binary and SDK pins.
Update it only with independently verified official release metadata and rerun
the real command on each supported CI platform before claiming compatibility.
