---
name: tandem
description: Consult, design, implement, or review with an Oh My Pi peer through OMP Tandem MCP tools. Use for a second perspective, scoped delegated work, reciprocal clarification, or explicit project-context transfer.
---

# Work with a peer

OMP Tandem connects the current MCP client and Oh My Pi (OMP). Either participant can contribute analysis, propose a design, implement an agreed slice, review the other's work, or ask for missing information. Coordinator and worker are execution roles for one task, not permanent ranks or assumptions about which model is smarter. Retain responsibility for the user's request and verify received work.

## Discover and scope

1. Discover the connected tools by their `tandem_*` suffixes. MCP prefixes depend on the client, registration, and plugin namespace; never hardcode a full tool name or assume a second registration is needed. If tools are absent, use the sibling `setup` skill.
2. Call `tandem_scope` before project work. Confirm the bound project is the intended workspace, not the plugin installation or runtime cache. A task's `cwd` selects an allowed work directory; it does not bind the MCP instance or switch its project data. If the launch boundary is absent or wrong, fix the client launch configuration rather than routing work into that boundary.
3. Read the relevant local instructions and necessary source. Request only work that benefits from another perspective or a clearly owned implementation slice. Do not invent company rules, product requirements, or provider/model defaults.
4. Agree on the goal, relevant context, constraints, owned files, acceptance criteria, and output format. File ownership is coordination, not an OS sandbox. Work mode can edit files and run shell commands with the runtime's permissions. Do not authorize destructive or external actions beyond the user's request.

## Start a scoped task

Use `tandem_start` with an allowed absolute `cwd`, a mode, and exactly one of `prompt` or `contract`:

- `think`: consultation and design from supplied context.
- `analyze`: inspect/search source for investigation or review.
- `work`: implementation and shell execution, only when authorized.

Prefer a structured contract for load-bearing work:

```json
{
  "goal": "Review the retry design for duplicate writes; propose a concrete correction if needed.",
  "context": "Describe the caller, failure scenario, relevant source locations, and evidence already collected.",
  "scope": {"owned_files": []},
  "constraints": ["Read-only review; do not edit files or change external state."],
  "acceptance": ["Explain whether retries can repeat a committed write, citing the relevant code path."]
}
```

For implementation, replace the goal and criteria and enumerate exact owned files. Give siblings disjoint ownership or serialize shared edits. There are four shared execution slots; do useful local work while accepted tasks run, and handle reported capacity limits rather than assuming unlimited concurrency. Keep returned task and conversation IDs.

## Collaborate through the task lifecycle

- Read `next_action`, status, and delivery information instead of guessing from elapsed time. Use confirmed push delivery when the host supports it. Otherwise use bounded `tandem_wait` for selected active tasks; it returns ready IDs/questions, not full answers. Do not spin on zero-wait status calls.
- Retrieve each ready result with `tandem_result`. `completed` only means the turn ended. Inspect `answer`, the structured outcome, checks, and blockers before treating the work as successful.
- If `answer_truncated` is true, read the returned `answer_artifact_id` with `tandem_read_artifact` in bounded chunks. A summary is not the requested answer.
- For `waiting_input`, inspect the actual question. Supply known facts through `tandem_reply` using its exact task/question IDs. Ask the user when only they can resolve the decision; never fabricate permission or requirements. Do not use `tandem_continue` to answer a pending question.
- After completion, `tandem_continue` starts a new goal in the same conversation using exactly one prompt or turn contract. The base mode, work directory, owned files, and execution constraints persist; old acceptance criteria do not. Start a new conversation when ownership or permissions must change.
- Cancel with `tandem_cancel` only when the work is no longer wanted or authorized. Cancellation does not undo edits. Do not cancel tasks merely because this client's turn ends. Hooks never poll or cancel tasks.

## Share evidence, not implicit authority

Use `tandem_publish_artifact` for substantial evidence and pass returned artifact IDs explicitly. Project contexts are source-backed, immutable snapshots of the actual project's rules and decisions, not a place for generic invented policies. Use `tandem_project_context` to inspect or deliberately publish them; attach a context ID to a start/continue only when relevant. Updating a context does not silently change an active task.

Projects have separate histories, tasks, artifacts, and contexts. Additional client-granted directories permit working there, not browsing their MCP history. Cross-project context sharing requires explicit user intent and both sides of the transfer:

1. In the source project's client session, select only the intended context/evidence and call `tandem_export_context` for the exact recipient project root.
2. Treat its transfer ID as a private capability. Share it only with the intended recipient.
3. In the recipient project's own bound client session, explicitly call `tandem_import_context`; use the expected revision when updating an existing context.
4. Inspect imported provenance. A transfer copies selected context/evidence, not tasks or history, and grants no filesystem permission or approval.

Legacy migration is copy-only; do not delete or repurpose another project's old state to resolve a lookup failure.

## Deliver honestly

Review changed files and exercise the requested behavior before accepting implementation. Distinguish observed results from inferences. Report the actual answer, changes, checks performed, remaining blockers, and any unverified surface. Never claim a check passed if it was skipped, a worker's success report is unconfirmed, or only a process exited successfully. Preserve the user's language and requested output format. Hooks are optional diagnostics; collaboration must not depend on them being enabled or trusted.
