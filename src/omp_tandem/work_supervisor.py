"""Explicitly authorized project supervisor; execution outlives clients, never its grant."""

import fcntl
import json
import logging
import os
import signal
import threading
import time
from uuid import uuid4

from .work_adapters import ClaudeWorkAdapter, OmpWorkAdapter
from .work_items import WorkConflict
from .work_workspace import WorkWorkspace

logger = logging.getLogger(__name__)


def private_json(path, value):
    temporary = path.with_name(path.name + "." + str(uuid4()))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(value, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def supervisor_status(scope):
    path = scope.directory / "work-supervisor.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            running = True
        else:
            running = False
        metadata = scope.directory / "work-supervisor.json"
        if metadata.is_symlink():
            raise ValueError("Supervisor metadata must not be a symbolic link")
        result = json.loads(metadata.read_text()) if metadata.exists() else {}
    return {**result, "running": running, "project_root": str(scope.root)}


class WorkSupervisor:
    def __init__(self, bridge, *, claude="claude", concurrency=2, work_id=None):
        if not 1 <= concurrency <= 4:
            raise ValueError("Supervisor concurrency must be 1..4")
        self.bridge = bridge
        self.store = bridge.work_items
        self.scope = bridge.scope
        self.work_id = work_id
        self.owner_id = str(uuid4())
        self.concurrency = concurrency
        self.workspace = WorkWorkspace(self.scope)
        self.adapters = {
            "omp": OmpWorkAdapter(bridge),
            "claude": ClaudeWorkAdapter(bridge, executable=claude),
        }
        self.threads = {}
        self.stop_event = threading.Event()
        self.lease = None
        self.metadata = self.scope.directory / "work-supervisor.json"
        self.stop_path = self.scope.directory / "work-supervisor-stop.json"

    def _view(self, work_id):
        return self.store.perform(
            {"action": "get", "work_id": work_id}, actor="operator"
        )

    def _publish_status(self, state):
        private_json(
            self.metadata,
            {
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "state": state,
                "work_id": self.work_id,
                "active_attempts": list(self.threads),
                "updated": time.time(),
                "project_root": str(self.scope.root),
            },
        )

    def _finish(self, attempt, plan, workspace, result):
        outcome, output = result["outcome"], None
        try:
            if outcome in ("success", "blocked"):
                if attempt["kind"] == "implement":
                    output = self.workspace.finish(attempt, plan, workspace)
                else:
                    self.workspace.verify_review(attempt, plan, workspace)
        except (OSError, ValueError, RuntimeError) as exc:
            outcome = "interrupted"
            result = {**result, "error": f"Output requires reconciliation: {exc}"}
        # poll() only returns after actual process/reader/native-thread teardown.
        self.store.confirm_stopped(attempt["attempt_id"])
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome=outcome,
            answer=result.get("answer", ""),
            evidence=result.get("evidence", []),
            output=output,
            cost_usd=result.get("cost_usd"),
            error=result.get("error"),
        )

    def _run_attempt(self, attempt):
        identifier = attempt["attempt_id"]
        adapter = self.adapters[attempt["actor"]]
        handle = None
        token_file = None
        launching = False
        finished = False
        deadline = time.monotonic() + max(0, attempt["deadline"] - time.time())
        try:
            view = self._view(attempt["work_id"])
            plan = view["plan"]
            workspace = self.workspace.prepare(attempt, plan, attempt["dependencies"])
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                raise ValueError("Assignment stopped before launch")
            self.store.authenticate(attempt["token"])
            directory = self.scope.directory / "work-attempts" / identifier
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ValueError("Attempt directory must not be a symlink")
            token_file = directory / "token"
            descriptor = os.open(
                token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w") as token:
                token.write(attempt["token"] + "\n")
            launching = True
            # Adapter persists its non-replay launch marker and native binding
            # before dispatch. No supervisor thread blocks another attempt's watchdog.
            handle = adapter.start(attempt, plan, workspace, token_file=token_file)
            while True:
                current = self.store.attempt(identifier)
                invalid = (
                    self.stop_event.is_set()
                    or time.monotonic() >= deadline
                    or current["state"] not in ("reserved", "running")
                )
                if invalid:
                    adapter.cancel(handle)
                result = adapter.poll(handle)
                if result is not None:
                    if invalid:
                        result = {
                            **result,
                            "outcome": "interrupted",
                            "error": "Execution stopped by pause, deadline, revocation or a changed plan",
                        }
                    self._finish(attempt, plan, workspace, result)
                    finished = True
                    break
                if invalid:
                    raise RuntimeError(
                        "Cancelled adapter still has an active execution"
                    )
                self.stop_event.wait(0.2)
        except Exception as exc:
            logger.exception("Shared assignment requires attention")
            stopped = not launching
            cost = 0.0 if not launching else None
            evidence, answer = [], ""
            if handle is not None:
                try:
                    adapter.cancel(handle)
                    observed = adapter.poll(handle)
                    stopped = observed is not None
                    if observed:
                        cost = observed.get("cost_usd")
                        evidence, answer = (
                            observed.get("evidence", []),
                            observed.get("answer", ""),
                        )
                except Exception:
                    logger.exception("Assignment cleanup remains unconfirmed")
            if not finished:
                self.store.finish_attempt(
                    identifier,
                    outcome="interrupted" if launching else "failed",
                    answer=answer,
                    evidence=evidence,
                    output=None,
                    cost_usd=cost,
                    error=f"Assignment requires reconciliation: {type(exc).__name__}: {exc}",
                )
                if stopped:
                    self.store.confirm_stopped(identifier)
        finally:
            if token_file is not None:
                token_file.unlink(missing_ok=True)

    def _launch(self, entry):
        attempt = self.store.reserve(
            entry["work_id"],
            entry["step_id"],
            actor=entry["actor"],
            kind=entry["kind"],
            owner_id=self.owner_id,
        )
        thread = threading.Thread(
            target=self._run_attempt,
            args=(attempt,),
            name="work-" + attempt["attempt_id"],
        )
        self.threads[attempt["attempt_id"]] = thread
        try:
            thread.start()
        except RuntimeError:
            self.threads.pop(attempt["attempt_id"])
            self.store.finish_attempt(
                attempt["attempt_id"],
                outcome="failed",
                answer="",
                evidence=[],
                output=None,
                cost_usd=0,
                error="Execution thread could not start",
            )
            self.store.confirm_stopped(attempt["attempt_id"])
            raise

    def _views(self):
        if self.work_id:
            return [self._view(self.work_id)]
        return self.store.perform({"action": "list"}, actor="operator")["items"]

    def _has_grants(self):
        return any(
            view["status"] == "active"
            and (grant := view.get("authorization"))
            and grant["revoked_at"] is None
            and grant["deadline"] > time.time()
            and not grant["unknown_cost"]
            and grant["launches"] < grant["max_launches"]
            and grant["used_cost_usd"] < grant["max_cost_usd"]
            for view in self._views()
        )

    def _requested_stop(self):
        if not self.stop_path.exists():
            return False
        if self.stop_path.is_symlink():
            raise ValueError("Supervisor stop request must not be a symlink")
        return json.loads(self.stop_path.read_text()).get("owner_id") == self.owner_id

    def _pause_owned(self):
        for observed in self._views():
            view = observed
            while view["status"] not in ("paused", "completed") and view.get(
                "authorization"
            ):
                try:
                    self.store.perform(
                        {
                            "action": "pause",
                            "work_id": view["work_id"],
                            "expected_revision": view["revision"],
                            "operation_id": str(uuid4()),
                            "note": "Operator stopped the autonomous supervisor",
                        },
                        actor="operator",
                    )
                    break
                except WorkConflict:
                    # A failed CAS performed no effect. Stopping is desired state,
                    # not an execution retry; finish pausing every targeted card.
                    view = self._view(view["work_id"])

    def run(self, *, once=False):
        descriptor = os.open(
            self.scope.directory / "work-supervisor.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "a+") as self.lease:
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "A shared-work supervisor already owns this project"
                ) from None
            previous = self.store.active_attempts()
            for owner in {
                attempt["owner_id"]
                for attempt in previous
                if attempt["autonomous"] and attempt["owner_id"] != self.owner_id
            }:
                # Singleton lease proves controller loss, NOT absence of orphaned
                # subprocesses or external effects. Recovery never replays work.
                self.store.recover_owner(owner)
            self._publish_status("running")
            print(
                json.dumps(
                    {
                        "supervisor": "ready",
                        "owner_id": self.owner_id,
                        "project_root": str(self.scope.root),
                    }
                ),
                flush=True,
            )
            try:
                while True:
                    if self._requested_stop():
                        self.stop_event.set()
                    if self.stop_event.is_set():
                        self._pause_owned()
                    for identifier, thread in list(self.threads.items()):
                        if not thread.is_alive():
                            thread.join()
                            self.threads.pop(identifier)
                    if not self.stop_event.is_set():
                        for entry in self.store.ready(self.work_id):
                            if len(self.threads) >= self.concurrency:
                                break
                            try:
                                self._launch(entry)
                            except ValueError:
                                # Readiness/grant changed between observation and
                                # atomic reservation. No admitted execution is replayed.
                                continue
                    self._publish_status("running")
                    if not self.threads and (
                        once or self.stop_event.is_set() or not self._has_grants()
                    ):
                        break
                    self.stop_event.wait(0.2)
            finally:
                self.stop_event.set()
                try:
                    for thread in self.threads.values():
                        thread.join()
                    for adapter in self.adapters.values():
                        adapter.close()
                finally:
                    self._publish_status("stopped")
                    self.bridge.shutdown()

    def install_signals(self):
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda _signum, _frame: self.stop_event.set())
