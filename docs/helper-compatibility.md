# Helper (native `task` subagent) compatibility map — stage G0

Status: evidence document for the specification in
[`docs/spec/helpers-and-shared-development.md`](spec/helpers-and-shared-development.md),
stage G0. It records what the pinned OMP runtime enforces for child agents, what
Tandem enforces today, and which guarantees are still blockers. **Production
delegation stays disabled**: every Tandem worker allowlist omits `task`
(`src/omp_tandem/native_worker.py`, mode tool lists), and the verifier reports
`delegation.available = false` with the unsatisfied gates.

## Verified identities

| Component | Value | How it was established |
|---|---|---|
| Tandem source | main `dbb111f` (implementation baseline); historical code evidence cited at `40238ee` | `git rev-parse`, shared-task card `67ecdc4f` |
| OMP binary | **18.1.13**, SHA-256 `a4c5c9cc…1909` (official `omp-darwin-arm64` digest) | `omp --version`; the Homebrew binary at `/opt/homebrew/Cellar/omp/18.1.13/bin/omp` matches the pinned asset digest, so `scripts/verify_omp.py --omp` accepts it |
| Python SDK | `omp-rpc 0.1.0` from `can1357/oh-my-pi@daf07999c2fee9b22edc7bf8fea1fb6272e0df5e` (`python/omp-rpc`) | `sdk_pin` check, `direct_url.json` |
| Verification run | [`docs/compatibility-result.json`](compatibility-result.json), Darwin arm64, Python 3.13 | `uv run --frozen python scripts/verify_omp.py --cache-dir … --omp /opt/homebrew/Cellar/omp/18.1.13/bin/omp --report …` |

Independent G0 reports that this document consolidates (Tandem artifact IDs):
`16beafb6-7942-4c8b-b341-0754dfc6ddf1` (OMP, static map),
`71b074b3-e1c1-4959-a958-15c0b351e284` (coordinator, static map + local probes),
`04e2a799-52ff-4d63-959b-2f5bc0bfca19` (comparison and agreed revisions).

## How the helper probes work

`scripts/verify_omp.py` now contains `helper_probe`. It never goes through the
production `Bridge`: it starts the official binary through the public SDK
(`omp_rpc.RpcClient`) with `--tools read,grep,glob,task`, `--no-extensions
--no-skills --no-rules --no-lsp --no-title --approval-mode=yolo`, an essential
parent-only host tool, and an isolated agent directory whose `config.yml` sets

```json
{"async": {"enabled": false},
 "modelRoles": {"default": "tandem-compat/fixture", "smol": "tandem-compat/fixture-smol"},
 "task": {"agentModelOverrides": {"scout": "@smol:low", "sonic": "@smol:low"},
          "maxRecursionDepth": 1, "maxConcurrency": 2, "disabledAgents": [],
          "isolation": {"enabled": false}},
 "retry": {"modelFallback": false, "enabled": false},
 "prewalk": {"enabled": false}, "advisor": {"enabled": false}}
```

Both parent and child model turns are scripted on the localhost fixture; the
fixture records every request body (advertised tools, model, `reasoning_effort`,
message history) so assertions are made on the **actual model-boundary input**,
not on agent prose. Two fixture models exist: `fixture` (parent) and
`fixture-smol` (helper role, reasoning-capable so the requested effort is
observable). Requests without any `tools` field are OMP's automatic subagent
label generation; the fixture answers them without consuming the script and
counts them as `label_requests`.

A probe check "passes" when the measurement executed. Whether the observed
behavior satisfies the helper contract is a separate gate in
`report.helper_capabilities[<gate>].supported`. `report.delegation` lists the
unsatisfied gates among the eleven required ones plus `no_extra_model_calls_per_spawn`;
it is `available: false` in every run of this revision.

## Measured gates (run of 2026-09-11, OMP 18.1.13)

| Gate | Result | Observation |
|---|---|---|
| `child_tools_exclude_mutation` | supported | bundled `scout` child advertised only `glob, grep, hub, read, web_search, yield` |
| `child_mutation_refused` | supported | child `write`/`bash` calls returned `Tool write not found` / `Tool bash not found`; no files created |
| `child_cannot_see_parent_host_tools` | supported | parent-only essential host tool absent from the child; call returned `Tool compatibility_probe not found` |
| `child_cannot_spawn_nested_task` | supported | with `task.maxRecursionDepth: 1` the child has no `task` tool |
| `child_model_override_applied` | supported | every child request used `fixture-smol` via `task.agentModelOverrides.scout = "@smol:low"` + `modelRoles.smol` |
| `child_thinking_low_observed` | supported | child requests carried `reasoning_effort: "low"` (bundled scout default is `medium`; the explicit `:low` suffix wins) |
| `child_transcript_absent_from_parent_input` | supported | the parent's final request contains the `<task-result>` payload, not the child's read output or tool transcript |
| `parent_write_tool_is_transport_only` | supported | enabling `task` adds a `write` tool to a read-only parent; a filesystem write through it is refused: *"This `write` tool is limited to the xd:// device transport"* |
| `xd_transport_observed_without_filesystem_write` | supported | child `write xd://report_issue` returned `Tool write not found` (the child had no `write`); no project file appeared |
| `disabled_agents_enforced_before_spawn` | supported | `task.disabledAgents: ["scout"]` → `Task … failed preflight: Agent "scout" is disabled in settings`; zero child requests |
| `parent_abort_prevents_late_child_delivery` | supported | parent `abort` while the child stream was held; **observed before any process stop**, with the parent RPC session still answering: releasing the held child response produced no further model request (52 → 52) and the child-answer and parent-answer steps stayed unconsumed. This proves abort semantics, not process-group cleanup, which the SDK `stop()` provides separately |
| `author_sentinel_visible_to_child` | observation | the batch `context` is injected verbatim into every child prompt (by design); independent-first review must never route author material through it |
| `no_extra_model_calls_per_spawn` | **unsupported** | one additional tool-less label-generation request per spawn on the helper model at `reasoning_effort: "minimal"` |
| `child_tools_intersect_parent_restriction` | **unsupported** | `sonic` under the read-only parent advertised `bash, edit, eval, write` and **created a file** — child tools come from the agent definition, not from the parent's `--tools` |
| `project_agent_definition_cannot_widen_bundled_scout` | **unsupported** | a project `.omp/agents/scout.md` with `tools: [read, write, bash]` replaced the bundled scout even with `--no-extensions --no-skills --no-rules`; the child advertised `bash, write` |
| `task_wide_settings_snapshot` | **unsupported** | changing `task.agentModelOverrides.scout` between two spawns of one parent session changed the second child's model (`fixture-smol` → `fixture`): settings are reloaded per spawn |
| `parent_stream_usage_scope_exclusive` | **unsupported** | the parent RPC stream emitted 6 `message_end` events for 3 parent requests while 7 child requests ran: child assistant messages leak into the parent stream partially, so parent `TurnUsage` is neither exclusive nor complete |

## Guarantee map (specification §3.2, §4.4, §5.1–5.5, §6.3, §8.2–8.4)

Verdicts: **T** — enforceable in Tandem on the public surface (implemented or
implementable); **U** — needs an upstream OMP contract that 18.1.13 does not
provide; **D** — the mode must stay `delegation_unavailable` until proven.
Tandem evidence is at `40238ee` unless noted; line ranges shift by about +50 in
`work_items.py` after `dbb111f` (docstring/validation clarifications only).

| Requirement | Tandem today (evidence) | Upstream mechanism observed | Verdict | Check |
|---|---|---|---|---|
| §3.2 overrides, aliases, thinking; requested vs actual | parent-only `ExecutionOptions`/`resolve_execution` (`execution.py:40-90`); startup actual model/thinking recorded (`native_worker.py:265-320`); no helper policy | `task.agentModelOverrides` + `modelRoles` alias resolution; explicit `:low` overrides bundled `medium`; observed in provider payload | T for routing/diagnostics; U/D for strict fail-closed (`retry.modelFallback`, startup auth fallback in `model-resolver.ts` untested here) | `child_model_override_applied`, `child_thinking_low_observed` |
| §3.2 child tools, eval/hub/extensions/revival | `task` absent from every allowlist (`native_worker.py:196-223`); worker.yml disables foreign providers/MCP/memory | child tool list built from **agent.tools**, not intersected with the parent allowlist; `hub`, `eval` (sonic) available to children; `--no-extensions` does not stop project `.omp/agents` discovery | **U/D**: no public knob pins the definition source or intersects tools | `child_tools_intersect_parent_restriction`, `project_agent_definition_cannot_widen_bundled_scout` |
| §3.2 child context/result/async/`agent://` `history://` | no child protocol; parent report saved as artifact (`native_worker.py:345-401`) | separate child history; `yield {data}` with agent `output` schema; `<task-result>` injected into the parent; `agent://<id>`, `history://<id>` URIs; `async.enabled` default true (results auto-deliver later) | T for a compact result contract with `async.enabled:false`; D until async injection paths and URI access are gated | `child_transcript_absent_from_parent_input` |
| §3.2 cancel/deadline/owner-close, late results, revival | monotonic deadline + `client.stop()` (`native_worker.py:175-190`); `TaskRuntime.shutdown` owns its threads (`task_runtime.py:260-285`) | parent `abort` (RPC, process alive) stopped the held child stream and no late request followed the release; `task.maxRuntimeMs`, `task.agentIdleTtlMs` (parked agents revive when messaged) exist; revival after abort not exercised | T for abort + process stop; U/D for a tree-stop acknowledgement and unrevivable retirement | `parent_abort_prevents_late_child_delivery` |
| §3.2 usage scope/completeness | `TurnUsage` listens to parent `message_end`/`agent_end` only (`execution.py:100-225`) | child assistant messages partially appear in the parent stream; label request is a separate paid call; RPC `get_subagents` / `set_subagent_subscription` exist | **U/D**: parent totals are neither exclusive nor inclusive; per-node accounting needs a subscription-based child ledger | `parent_stream_usage_scope_exclusive`, `no_extra_model_calls_per_spawn` |
| §3.2 discovery / settings reload | only parent execution persisted (`task_runtime.py:105-205`) | task preflight reloads settings and rediscovers agents on every spawn; project definitions win over bundled | **U/D**: a task-wide immutable snapshot cannot be guaranteed by an overlay file alone | `task_wide_settings_snapshot`, `project_agent_definition_cannot_widen_bundled_scout` |
| §3.2 startup/retry/overflow/prewalk/advisor extra calls | `--no-title`, `--no-extensions`, memory off | `retry.modelFallback` (default **true**), `retry.fallbackChains`, `prewalk.enabled`, `advisor.enabled`, `task.agentPrewalk/agentAdvisor` toggles exist; label generation is an extra call that no toggle removed in this run | T for explicit toggles; U for fail-closed startup auth fallback; D for undeclared calls | `no_extra_model_calls_per_spawn` |
| §3.2 review snapshot, not only cwd | ReviewRuns stage-bound `tandem_review_read` (`native_worker.py:90-112`); managed reviewer runs `analyze` in a worktree (`work_adapters.py:250-285`) | children read the filesystem with native `read`; host URI schemes are process-global | T for snapshot-only reviewer without helpers (stage b2); U/D for a child inheriting the same reader | — (stage b2) |
| §4.4 immutable effective-settings snapshot | not persisted for helpers | per-spawn `Settings` reload observed | **D** until Tandem persists a policy snapshot **and** can prove the spawn used it (needs upstream hook or per-spawn overlay pinning + hash check) | `task_wide_settings_snapshot` |
| §5.1 rights by parent mode | parent mode → parent tools only | child rights are the agent definition's; `task.disabledAgents` is a name-level preflight gate | **D** for `analyze`+scout unless the definition source is pinned; sonic under `work` **U/D** | see above |
| §5.2 no external authority for helpers | host callbacks bound to the parent task/attempt token (`native_worker.py:45-165`) | child does not see parent essential host tools; batch `context` is copied into every child | T (confirmed for host tools); author material must not be placed in `context` | `child_cannot_see_parent_host_tools`, `author_sentinel_visible_to_child` |
| §5.3 file ownership for sonic | exact owned_files at admission/result check (`work_workspace.py`) | no per-edit lease; isolated tasks can commit/merge (`task.isolation.*`) | **U/D** for per-child enforcement; post-check only | `child_tools_intersect_parent_restriction` |
| §5.4 depth/concurrency/launch limits | four external active attempts (`work_items.py:1230-1265`) | `task.maxRecursionDepth` enforced (no nested `task`); `task.maxConcurrency` session-scoped; no project-wide helper semaphore or finite launch counter | T for Tandem-side counters; U/D for atomic admission before native dispatch | `child_cannot_spawn_nested_task` |
| §5.5 whole-tree stop | process-group stop; owner fences | RPC abort stopped the child stream with the parent alive; no separate tree acknowledgement API | T for the observed stop; **U** for acknowledged stop and durable "stopped" state | `parent_abort_prevents_late_child_delivery` |
| §6.3 result format | none for children | `yield {data}` + `outputSchema`/`schemaMode`; `<task-result … status>` envelope; plain prose never satisfies a schema (three idle reminders then `failed (exit 1)`) | T (Tandem must supply the schema and map native statuses) | probe traces |
| §8.2 one reserve for the tree | external envelope only (`work_items.py:1200-1305`) | no admission hook before a child model request | **U/D** | — |
| §8.3 no double counting | parent-only accumulator | parent stream contains a subset of child messages; label calls unaccounted | **U/D** | `parent_stream_usage_scope_exclusive` |
| §8.4 fallback | `retry.modelFallback` default true; startup auth fallback in resolver | toggles exist; explicit `:low` honored | T for toggles; U for fail-closed missing auth | not exercised (no auth failure path in fixture) |

## Consequences for the release plan

1. Stages A and B (explicit models and budgets, `--allow-shell`, durable blockers,
   independent-first shared review) do not depend on helpers and proceed.
2. Read-only scout is **not** yet safe to enable even under `analyze`: a project
   `.omp/agents/scout.md` widens the child's tools, and settings reload per
   spawn. Enabling it requires either an upstream pin for the definition source
   or a Tandem-side pre-spawn hash check of the effective definition plus a
   per-spawn overlay whose application is verified — neither exists.
3. `sonic` under a read-only parent is unsafe by construction in 18.1.13: child
   tools do not intersect the parent's allowlist.
4. Cost accounting for helper trees cannot rely on the parent `TurnUsage`; a child
   ledger (RPC subagent subscription) and explicit handling of label calls are
   prerequisites for §8.
5. Abort propagation (observed through the live RPC session, before any process
   stop) and `task.disabledAgents` work as observed and can be relied upon once
   the above blockers are addressed. Revival of an aborted or parked child was
   not exercised.

## Not covered by this run

- Paid-provider behavior (Spark/`gpt-6` availability, actual `low` support) — the
  fixture only proves routing of the requested selector.
- Startup auth fallback to the parent model when the helper model lacks
  credentials (`resolveModelOverrideWithAuthFallback`): not reproducible without
  an auth failure in the fixture.
- Parked/idle revival through `hub send` after a deadline (LIFE-02).
- Linux verification of the helper probes: CI runs the same command; consult the
  actual run rather than this local Darwin evidence.
