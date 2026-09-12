"""Operator-only autonomous grants and bounded local supervisor lifecycle."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .bridge import Bridge
from .work_items import CLAUDE_DEFAULT_MODEL, validate_model
from .work_supervisor import WorkSupervisor, private_json, supervisor_status
from .work_workspace import WorkWorkspace
from .workspace import resolve_scope


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--project-root", type=Path, required=True)
    root.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/omp-tandem"
    )
    root.add_argument("--omp", default=shutil.which("omp") or "omp")
    root.add_argument("--claude", default=shutil.which("claude") or "claude")
    root.add_argument(
        "--model",
        "--omp-model",
        default=None,
        type=validate_model,
        help="OMP model override for authorize; default: OMP configured selection",
    )
    root.add_argument(
        "--claude-model",
        type=validate_model,
        default=None,
        help=f"Claude model for authorize (default: {CLAUDE_DEFAULT_MODEL}); not an observed model identity",
    )
    commands = root.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser(
        "authorize",
        help="Explicitly permit bounded unattended launches for one agreed task",
    )
    authorize.add_argument("work_id")
    authorize.add_argument("--budget-seconds", type=int, required=True)
    authorize.add_argument("--max-launches", type=int, required=True)
    authorize.add_argument("--max-cost-usd", type=float, required=True)
    authorize.add_argument(
        "--max-attempt-cost-usd",
        type=float,
        default=None,
        help=(
            "Ceiling one launch may reserve; independent of --max-launches. "
            "Default: half of --max-cost-usd, shown in the authorization result."
        ),
    )
    authorize.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Validate and print the ceiling, reserve policy and permissions this "
            "grant would activate, without storing anything"
        ),
    )
    authorize.add_argument(
        "--allow-work",
        action="store_true",
        help="Permit edits in managed implementation worktrees",
    )
    authorize.add_argument(
        "--allow-shell",
        action="store_true",
        help=(
            "Permit arbitrary shell execution by managed workers (edits, network, "
            "processes); NOT an OS sandbox and NOT limited to tests"
        ),
    )
    authorize.add_argument(
        "--allow-tests",
        action="store_true",
        help="Deprecated alias of --allow-shell with the same arbitrary-shell permission",
    )
    revoke = commands.add_parser(
        "revoke", help="Revoke one task's launch grant and stop its managed work"
    )
    revoke.add_argument("work_id")
    for name in ("run", "start"):
        command = commands.add_parser(
            name,
            help="Run authorized work in foreground"
            if name == "run"
            else "Start a detached explicitly authorized controller",
        )
        command.add_argument("--work-id")
        command.add_argument("--concurrency", type=int, default=2)
        command.add_argument(
            "--once",
            action="store_true",
            help="Exit when no currently runnable or active work remains",
        )
    commands.add_parser(
        "status", help="Inspect actual kernel lease and controller state"
    )
    commands.add_parser(
        "stop", help="Pause owned tasks and stop this project's controller"
    )
    reconcile = commands.add_parser(
        "reconcile", help="Explicit recovery decision; never an automatic retry"
    )
    reconcile.add_argument("work_id")
    reconcile.add_argument("step_id")
    reconcile.add_argument("--resolution", choices=("retry", "abandon"), required=True)
    reconcile.add_argument("--note", required=True)
    reconcile.add_argument("--evidence", action="append", required=True)
    reconcile.add_argument(
        "--confirm-stopped",
        action="store_true",
        required=True,
        help="Attest old execution and possible external effects have been inspected; not merely a missing heartbeat",
    )
    apply = commands.add_parser(
        "apply",
        help="Explicitly fast-forward the clean original project to the final accepted result",
    )
    apply.add_argument("work_id")
    apply.add_argument("--expected-head", required=True)
    successor = commands.add_parser(
        "successor",
        help="Authorize one exact host and principal to recover a live manual claim for report-only closure",
    )
    successor.add_argument("attempt_id")
    successor.add_argument(
        "--host",
        required=True,
        help="host_owner shown by tandem_scope of the successor session",
    )
    successor.add_argument("--principal", choices=("claude", "omp"), required=True)
    successor.add_argument("--note", required=True)
    assess = commands.add_parser(
        "assess",
        help="Explicit isolated Git observation of the project HEAD against the final result; records it, changes nothing",
    )
    assess.add_argument("work_id")
    assess.add_argument("--expected-head", required=True)
    show = commands.add_parser("show", help="Read a shared task or list project tasks")
    show.add_argument("work_id", nargs="?")
    show.add_argument(
        "--view", choices=("summary", "plan", "step", "full"), default="summary"
    )
    show.add_argument("--format", choices=("json", "markdown"), default="json")
    show.add_argument("--step-id")
    return root


def make_bridge(args):
    return Bridge(
        args.state_dir,
        args.omp,
        None,  # Managed model selections belong to grants, never this process.
        project_root=args.project_root,
        channel_enabled=False,
        webhook_enabled=False,
        migrate_legacy=False,
    )


def start_detached(args, scope):
    observed = supervisor_status(scope)
    if observed["running"]:
        return observed
    log_path = scope.directory / "work-supervisor.log"
    descriptor = os.open(
        log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    command = [
        sys.executable,
        "-I",
        "-m",
        "omp_tandem.work_daemon",
        "--project-root",
        str(scope.root),
        "--state-dir",
        str(scope.base),
        "--omp",
        args.omp,
        "--claude",
        args.claude,
    ]
    command += ["run", "--concurrency", str(args.concurrency)]
    if args.work_id:
        command += ["--work-id", args.work_id]
    if args.once:
        command += ["--once"]
    with os.fdopen(descriptor, "wb") as log:
        process = subprocess.Popen(
            command,
            cwd=scope.root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    until = time.monotonic() + 15
    while time.monotonic() < until:
        state = supervisor_status(scope)
        if state["running"] and state.get("pid") == process.pid:
            return {**state, "log": str(log_path), "detached": True}
        if process.poll() is not None:
            # Very short authorized runs may legitimately complete before observation.
            if (
                state.get("pid") == process.pid
                and state.get("state") == "stopped"
                and process.returncode == 0
            ):
                return {**state, "log": str(log_path), "detached": True}
            raise RuntimeError(
                f"Supervisor exited before readiness; inspect {log_path}"
            )
        time.sleep(0.05)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    raise TimeoutError("Supervisor did not establish its project lease")


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.command != "authorize" and (
        args.model is not None or args.claude_model is not None
    ):
        arguments.error(
            "Model flags apply only to authorize; run/start use the saved grant"
        )
    os.umask(0o077)
    scope = resolve_scope(args.state_dir, args.project_root)
    if args.command == "status":
        result = supervisor_status(scope)
    elif args.command == "stop":
        result = supervisor_status(scope)
        if result["running"]:
            private_json(
                scope.directory / "work-supervisor-stop.json",
                {"owner_id": result["owner_id"]},
            )
            result = {**result, "stop_requested": True, "terminal": False}
    elif args.command == "start":
        # Validate authorization exists before detaching any process.
        bridge = make_bridge(args)
        try:
            probe = WorkSupervisor(
                bridge,
                claude=args.claude,
                concurrency=args.concurrency,
                work_id=args.work_id,
            )
            if not probe._has_grants():
                raise ValueError(
                    "No active explicit autonomy grant; authorize the agreed task first"
                )
        finally:
            bridge.shutdown()
        result = start_detached(args, scope)
    else:
        bridge = make_bridge(args)
        try:
            store = bridge.work_items
            if args.command == "authorize":
                if args.allow_tests:
                    print(
                        "Warning: --allow-tests is a deprecated alias of --allow-shell; "
                        "it grants arbitrary shell execution, not a sandboxed test runner.",
                        file=sys.stderr,
                        flush=True,
                    )
                request = {
                    "budget_seconds": args.budget_seconds,
                    "max_launches": args.max_launches,
                    "max_cost_usd": args.max_cost_usd,
                    "max_attempt_cost_usd": args.max_attempt_cost_usd,
                    "allow_work": args.allow_work,
                    "allow_shell": args.allow_shell or args.allow_tests,
                    "claude_model": args.claude_model,
                    "omp_model": args.model,
                }
                if args.preview:
                    # Nothing is written: the operator sees the exact ceiling,
                    # reserve policy and permissions before activating a grant.
                    result = {
                        "work_id": args.work_id,
                        **store.preview_authorization(**request),
                    }
                else:
                    result = store.authorize(args.work_id, **request)
            elif args.command == "revoke":
                result = store.revoke(args.work_id)
            elif args.command == "run":
                supervisor = WorkSupervisor(
                    bridge,
                    claude=args.claude,
                    concurrency=args.concurrency,
                    work_id=args.work_id,
                )
                supervisor.install_signals()
                supervisor.run(once=args.once)
                return 0
            elif args.command == "reconcile":
                view = store.perform(
                    {"action": "get", "work_id": args.work_id}, actor="operator"
                )
                step = next(
                    (row for row in view["steps"] if row["id"] == args.step_id), None
                )
                if step is None or not step.get("attempt"):
                    raise ValueError("Step has no attempt to reconcile")
                identifier = step["attempt"]["attempt_id"]
                controller = supervisor_status(scope)
                if controller["running"] and identifier in controller.get(
                    "active_attempts", []
                ):
                    raise ValueError(
                        "Controller still owns this execution; stop and wait before reconciliation"
                    )
                store.confirm_stopped(identifier)
                view = store.perform(
                    {"action": "get", "work_id": args.work_id}, actor="operator"
                )
                result = store.perform(
                    {
                        "action": "reconcile",
                        "work_id": args.work_id,
                        "step_id": args.step_id,
                        "expected_revision": view["revision"],
                        "operation_id": str(uuid4()),
                        "resolution": args.resolution,
                        "note": args.note,
                        "evidence": args.evidence,
                    },
                    actor="operator",
                )
                result["reconciliation_basis"] = (
                    "Explicit operator attestation; not proof inferred from process silence"
                )
            elif args.command == "apply":
                view = store.perform(
                    {"action": "get", "work_id": args.work_id}, actor="operator"
                )
                if view["status"] != "completed" or not view.get("result"):
                    raise ValueError(
                        "Only the final accepted current task result can be applied"
                    )
                output = view["result"].get("output", view["result"])
                result = WorkWorkspace(scope).apply(output, args.expected_head)
                receipt = {
                    "kind": "apply_receipt",
                    "commit": result["commit"],
                    "tree_hash": result["tree_hash"],
                    "expected_head": args.expected_head,
                    "project_root": result["project_root"],
                    "applied_at": time.time(),
                }
                try:
                    recorded = store.record_application(args.work_id, receipt)
                    result["recording"] = {
                        "status": "recorded",
                        "revision": recorded["revision"],
                    }
                except (ValueError, OSError, sqlite3.Error) as error:
                    # The fast-forward already happened; say so instead of hiding
                    # it or replaying the merge. An explicit assess can observe it.
                    result["recording"] = {
                        "status": "failed",
                        "error": str(error),
                        "next_step": "run assess to record the observed HEAD",
                    }
            elif args.command == "successor":
                result = store.authorize_successor(
                    args.attempt_id,
                    host_owner=args.host,
                    principal=args.principal,
                    note=args.note,
                )
                result = {
                    "work_id": result["work_id"],
                    "revision": result["revision"],
                    "successor": result["successor"],
                }
            elif args.command == "assess":
                view = store.perform(
                    {"action": "get", "work_id": args.work_id}, actor="operator"
                )
                final = view.get("result") or {}
                target = (final.get("output") or final).get("commit") if final else None
                observation = WorkWorkspace(scope).assess(target, args.expected_head)
                result = {
                    **observation,
                    "recording": {
                        "status": "recorded",
                        "revision": store.record_application(args.work_id, observation)[
                            "revision"
                        ],
                    },
                }
            else:
                result = store.perform(
                    {"action": "get", "work_id": args.work_id}
                    if args.work_id
                    else {"action": "list"},
                    actor="operator",
                )
                from .runtime_identity import runtime_identity
                from .work_items import present_work

                if args.view == "step":
                    result = store.step_material(
                        result, step_id=args.step_id, actor="operator"
                    )
                result["runtime_identity"] = runtime_identity()
                if args.format == "markdown" and args.work_id:
                    result = {
                        "markdown": store.report_markdown(
                            result,
                            actor="operator",
                            usage=store.work_usage(args.work_id),
                            identity=result["runtime_identity"],
                        )
                    }
                else:
                    result = present_work(
                        result,
                        actor="operator",
                        view=args.view,
                        format=args.format,
                        step_id=args.step_id,
                    )
        finally:
            bridge.shutdown()
    print(
        result["markdown"]
        if args.command == "show" and args.format == "markdown"
        else json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Shared work: {error}", file=sys.stderr)
        raise SystemExit(1) from None
