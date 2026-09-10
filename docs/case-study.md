# A real review during 3.3.0 development

**One development case, three recorded episodes—not a comparative benchmark.** Claude coordinated reviews through the actual local MCP server, official OMP and the configured provider. The first attempt lacked context; the second found a reproducible bug but timed out; the third completed and identified a distinct follow-on risk. None is omitted to make the workflow look cleaner.

[Machine-readable evidence](case-study.json) preserves the requests, selected-file manifests and hashes, clarification questions/answers, complete available peer and coordinator answers, actual execution metadata and cost accounting. Local machine paths are redacted. Native session histories, credentials and runtime databases are not published.

## What was being reviewed

The new `tandem_review_run` scenario was intended to save a worktree or staged snapshot, obtain an independent assessment, optionally compare a separately supplied author proposal, and return full answers without manual orchestration. Requirements included owner-scoped idempotency, no replay of reserved native tasks, a total deadline covering capture and both stages, cancellation, and explicit snapshot applicability. The scenario must remain read-only.

The review request named these criteria and the relevant source/tests. It did **not** authorize the peer to edit files or execute tests. Claude was instructed to use the scenario API, answer only from supplied facts, preserve complete stage reports, and cancel rather than invent missing context. Each deliberate recapture started a new run with a new immutable review identity.

The recorded inputs were worktree snapshots during development, based on Git `9da1ec4e04043fa98a4ee0b94aab697c639cc917`, **not the final 3.3.0 release tree**. The per-file hashes and `code_fingerprint` in the saved manifest identify the actual versions. A fingerprint quoted or abbreviated by a model is not authoritative. The public record contains manifests, not an executable archive of every historical worktree byte; reproducing the protocol against current code creates new evidence, not a replay of these answers.

## What happened

| Episode | Captured files | Scenario budget | Observed result | Whole episode wall time |
|---|---:|---:|---|---:|
| Initial review | 13 | 300 s | Cancelled after a real missing-context question; no final review verdict | 80.558 s |
| Expanded review | 18 | 300 s | Failed at total deadline; complete available stage answers retained, including a concrete capture-lifecycle finding | 331.723 s |
| Focused independent recheck after the first fix | 12 | 600 s | Completed with a review report; no author/comparison stage; a distinct shared-SQLite publication-lock risk remained to investigate | 186.127 s |

Whole episode time includes version inspection, Claude/MCP startup, capture, provider calls, waiting and client shutdown. It is not the scenario's internal elapsed time. A 300-second scenario followed by cleanup can have a longer whole-process clock; the second episode's failure is not rewritten as a success because its answers were useful.

### 1. Missing context was a blocker, not an invitation to guess

The independent peer requested `native_worker.py`, `reviews.py`, the host-tool permission/dispatch implementation, and related test context. Those bytes were absent from the initial saved capture. Claude cancelled the run instead of joining live files onto the old snapshot or pretending those boundaries had been checked.

A deliberate second request captured an expanded file set. Further unspecified boundaries were left explicitly unverified rather than silently expanding the scope again. The original blocked attempt and its cost remain in the record.

### 2. Independent review found a concrete defect

The independent report identified this path:

> `ReviewRuns.start` held the controller-wide guard while synchronous `ReviewStore.create` captured the source. Capture received neither the total deadline nor cancellation control.

Consequences: a slow capture could prevent cancellation of another run, and an empty late capture could return `no_changes` after the total budget had expired. The comparison stage agreed with the concrete finding, but agreement alone was not the proof.

The coordinator reproduced the original defect with a local deterministic RPC fixture: **a one-second internal budget returned `no_changes` after 2.745 seconds; another run's cancellation waited behind the capture**. This deliberately short internal test budget is below the public MCP minimum of ten seconds. No paid model call was used for that reproduction. The exact observed values are retained in `original_defect_reproduction` in the JSON evidence.

The implementation moved capture into a separately supervised, killable process, added reservation/owner/deadline checks around atomic snapshot publication, and rejected late results before allowing `no_changes` or a native phase. Focused regressions cover cancellation, deadlines, owner lifetime, capacity, and publication rollback.

### 3. A completed recheck was not a clean bill of health

The third episode deliberately rechecked the corrected capture-lifecycle paths, without an author proposal. The peer supported the original fix statically but identified a **different lock interaction**: a capture child could hold SQLite's shared writer lock during publication while cancellation of another run waited for that database under the controller guard. Source reads being outside the guard did not remove this publication path.

The peer proposed a distinguishing experiment—pause a real publication transaction and cancel another run—but did not execute it. Although the peer labelled its code-path finding P1, **this episode provides a static finding, not an observed timing reproduction**. Claude preserved that distinction. Subsequent runtime verification and release changes must be assessed separately from this saved answer; a release fix cannot retroactively turn the third episode into an approval of the final code.

### Runtime follow-up before publication

The new publication-lock finding was subsequently **reproduced**, then corrected. The same regression against a preserved pre-fix source tree timed out at actual MCP cancellation while a different child held a real SQLite publication transaction. Against the corrected implementation, both the cross-run cancellation/deadline regression and the same-run publication rollback regression passed.

The correction uses a nonblocking publication gate, readable WAL state, and a deferred stop that prevents another phase without falsely reporting terminal cancellation before persistence. Initial correction attempts exposed WAL connection-lifetime and post-admission contention errors in local checks; those failures were fixed before the passing verification. These implementation/check iterations are outside the three paid-episode measurements above.

Reproduce the retained timing/rollback regressions:

```sh
uv run --frozen pytest -q \
  tests/test_review_runs.py::ReviewRunTests::test_publication_writer_defers_other_cancel_without_blocking_status_or_deadlines \
  tests/test_review_runs.py::ReviewRunTests::test_cancel_during_publication_rolls_back_snapshot_and_keeps_reservation
```

A separate throwaway smoke also exercised the **actual stdio MCP server and official OMP 18.1.13**, with only model HTTP replies supplied by a deterministic localhost fixture: two automatic stages, repeated-start identity, complete 25,200/24,300-character answers, staged-only applicability despite unstaged edits, stale detection after an index change, and no native project/shell tools exposed to the review model. It passed in 4.939 seconds. This verifies orchestration and boundaries, not the quality of the scripted answers. The machine-readable case includes this release follow-up separately from the historical peer reports.

## Actual models, versions and cost

Recorded environment: OMP **18.1.13**, Claude Code **2.1.267**, Python **3.13.5**, macOS arm64, Tandem development version **3.3.0**, official Python SDK revision **`daf07999c2fee9b22edc7bf8fea1fb6272e0df5e`**. The peer used `openai-codex/gpt-6-astra` with low thinking. Claude was requested through the `sonnet` alias; its reported model-usage map includes the actual Sonnet model and ancillary Haiku usage. Exact executable digests, per-episode model usage and collector identity where recorded are in the evidence.

| Episode | Coordinator reported USD | Peer reported USD | Whole-process reported USD |
|---|---:|---:|---:|
| Initial | 0.1619536 | 0.7092600 | 0.8712136 |
| Expanded | 0.2997556 | **Unknown**; known subtotal 3.1735280 | **Unknown**; known subtotal 3.4732836 |
| Focused recheck | 0.1691082 | 1.1693220 | 1.3384302 |

Across the three recorded episodes: **598.408 seconds** of measured wall time and **USD 5.6829274 known subtotal**, not a complete total. Unknown usage is not zero. These are client/provider estimates, not invoices. Prior design discussions, implementation, repairs, local checks and human preparation were outside this recording clock; human time was not measured. This is therefore **not the total cost or duration of developing the feature**.

## Reproduce the workflow, not a promised outcome

Ordinary first use does not require this recorder. Ask the host agent to review the prepared commit through `tandem_review_run`, with requirements and enough explicitly selected context; see the [first useful review](../README.md#quick-start) and [scenario guide](guide.md#one-read-only-review-scenario).

For an explicitly authorized paid measurement from a checkout:

```sh
uv sync --frozen --group dev
uv run --frozen python scripts/record_review_case.py \
  --project-root /absolute/path/to/project \
  --request /absolute/path/to/review-request.json \
  --output-dir /absolute/path/to/new-private-evidence-directory \
  --budget-seconds 600 \
  --allow-paid
```

`review-request.json` is a normal `ReviewRequest`: requirements, criteria, source, paths, optional context/check evidence and separately supplied author material. The three actual requests in the evidence are examples for this repository, not defaults for another project. The recorder requires working Claude/OMP authentication, uses fresh private state, and does not silently overwrite an existing evidence directory. It bounds the coordinator process and preserves partial results on failure. Inspect the private output before sharing; do not publish its runtime database or session files.

Model aliases, provider behaviour, prompts, source bytes and scheduling can change. A later run need not return the same findings, timing or price. The separate [real-binary compatibility check](compatibility.md) uses a localhost model fixture without paid credentials; it answers a different question. The [four-arm benchmark protocol](benchmark.md) is preparation only and has no comparative results.

## What this case establishes—and does not

- The actual Claude → MCP → OMP path produced a useful missing-context request, full reports and a concrete defect subsequently reproduced locally.
- Failures and costs were visible; a timed-out run still preserved useful answers without being relabelled successful.
- An independent recheck could challenge the fix instead of merely agreeing with the author.
- This selected case does not measure improvement over Claude alone, self-review or a peer shown the author's proposal. It does not establish an optimum budget, absence of false positives, or that a second agent is always worth the cost.
- Independent-first describes the order in which explicit author material was revealed. Existing code, tests and requirements can expose intent; absolute blindness is not claimed.

## Кратко по-русски

Это реальный случай разработки, не бенчмарк: первая проверка остановилась из-за неполного контекста; вторая нашла затем воспроизведённую ошибку захвата, но исчерпала бюджет; третья завершила отчёт и указала отдельный риск блокировки SQLite. В JSON сохранены все три попытки, исходные ответы, запросы, хеши снимков и учёт обоих участников. Измерено 598,408 секунды; известная часть расходов — $5,6829274, полный расход неизвестен. Время разработки, исправлений и человека не измерялось. Завершённое ревью не означает, что код признан правильным; последующая проверка исправления — отдельное свидетельство.

## 中文摘要

这是实际开发案例，不是比较基准：第一次因缺少上下文而取消；第二次发现后来在本地复现的捕获缺陷，但总预算耗尽；第三次完成报告，同时指出另一条 SQLite 发布锁风险。JSON 保留三次尝试、原始完整回答、请求、快照哈希以及双方用量。已测墙钟时间合计 598.408 秒；已知费用小计为 5.6829274 美元，完整费用未知。此前设计、修复与人工时间不在测量范围内。审查完成不等于代码正确，后续修复验证属于独立证据。
