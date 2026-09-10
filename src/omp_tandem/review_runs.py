"""Owner-leased, durable two-stage snapshot reviews; never replay a reserved stage."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, closing, contextmanager, suppress
from uuid import UUID, uuid4

from .execution import ExecutionOptions, conversation_usage, resolve_execution
from .reviews import PublicationBusy, ReviewRequest, _json, publication_lock
from .runtime_models import ACTIVE

RUN_ACTIVE = frozenset({"starting", "running", "waiting_input"})
logger = logging.getLogger(__name__)
MAX_CAPTURES = 4
_CAPTURE_OUTPUT_LIMIT = 4 * 1024 * 1024


class ReviewRuns:
    def __init__(
        self, tasks, reviews, start_task, read_task, reply_task, cancel_task, owner
    ):
        self.tasks, self.reviews = tasks, reviews
        self.start_task, self.read_task = start_task, read_task
        self.reply_task, self.cancel_task = reply_task, cancel_task
        self.owner = str(UUID(owner))
        self.guard = threading.RLock()
        self.wake = threading.Event()
        self.closing = False
        self.captures = {}
        self.capture_stops = set()
        self.monotonic_deadlines = {}
        self.pending_stops = {}
        self.capture_results = {}
        self.lease = self._lease(self.owner)
        # Keep WAL's shared-memory index alive across short-lived query connections;
        # status observation must not continually trigger last-close checkpoints
        # and first-open recovery as capture children come and go.
        self.database_anchor = sqlite3.connect(
            tasks.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self.database_anchor.row_factory = sqlite3.Row
        with publication_lock(reviews.scope):
            db = self.database_anchor
            db.execute("""CREATE TABLE IF NOT EXISTS review_runs (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                payload_json TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                deadline REAL NOT NULL, review_id TEXT, status TEXT NOT NULL,
                phase TEXT NOT NULL, outcome TEXT, error TEXT,
                independent_task_id TEXT UNIQUE, comparison_task_id TEXT UNIQUE,
                independent_json TEXT, comparison_json TEXT,
                UNIQUE(owner, request_key))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(review_runs)")}
            for name, declaration in (
                ("capture_id", "TEXT"),
                ("capture_started", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE review_runs ADD COLUMN {name} {declaration}"
                    )
            db.execute(
                "UPDATE review_runs SET status='interrupted', error=?, updated=? "
                "WHERE owner=? AND status IN ('starting','running','waiting_input')",
                (
                    "Owner lease was lost; accepted work will not be replayed.",
                    time.time(),
                    self.owner,
                ),
            )
        self.driver = None

    def _lease(self, owner):
        handle = (self.tasks.root / f"review-owner-{UUID(owner)!s}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ValueError("Review run owner is still active") from None
        return handle

    @contextmanager
    def _writing(self):
        try:
            with publication_lock(self.reviews.scope, blocking=False):
                yield
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise PublicationBusy(
                    "Snapshot database is busy; retry shortly"
                ) from None
            raise

    def _get(self, run_id, include_results=True):
        run_id = str(UUID(run_id))
        fields = (
            "*"
            if include_results
            else (
                "run_id, owner, status, phase, deadline, independent_task_id, comparison_task_id"
            )
        )
        with closing(self.tasks.connect()) as db:
            row = db.execute(
                f"SELECT {fields} FROM review_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown review run_id")
        return dict(row)

    def _update(self, run_id, **values):
        values["updated"] = time.time()
        with self._writing(), closing(self.tasks.connect()) as db:
            db.execute(
                f"UPDATE review_runs SET {', '.join(key + '=?' for key in values)} WHERE run_id=?",
                [*values.values(), run_id],
            )

    def start(
        self, request_key, request, execution=None, budget_seconds=600, compare=True
    ):
        if (
            not isinstance(request_key, str)
            or not request_key.strip()
            or len(request_key) > 200
        ):
            raise ValueError("request_key must be nonblank and at most 200 characters")
        if type(budget_seconds) is not int or not 1 <= budget_seconds <= 7200:
            raise ValueError("budget_seconds must be 1..7200")
        if type(compare) is not bool:
            raise ValueError("compare must be boolean")
        request = ReviewRequest.model_validate(request)
        execution = ExecutionOptions.model_validate(execution or {}).model_dump(
            exclude_none=True
        )
        normalized = request.model_dump()
        for key in ("paths", "context_paths"):
            if normalized[key] is not None:
                normalized[key] = sorted(set(normalized[key]))
        payload = _json(
            {
                "request": normalized,
                "execution": execution,
                "budget_seconds": budget_seconds,
                "compare": compare,
            }
        )
        with self.guard:
            if self.closing:
                raise ValueError("MCP server is shutting down")
            with closing(self.tasks.connect()) as db:
                existing = db.execute(
                    "SELECT run_id, payload_json FROM review_runs WHERE owner=? AND request_key=?",
                    (self.owner, request_key),
                ).fetchone()
                if existing:
                    if existing["payload_json"] != payload:
                        raise ValueError(
                            "request_key conflicts with a different normalized review request"
                        )
                    return self.view(existing["run_id"])
                if self.driver is not None and not self.driver.is_alive():
                    raise ValueError(
                        "Review controller stopped; reconnect instead of replaying accepted work"
                    )
                active_captures = db.execute(
                    "SELECT count(*) FROM review_runs WHERE owner=? AND phase='capture' "
                    "AND status IN ('starting','running','waiting_input')",
                    (self.owner,),
                ).fetchone()[0]
                if max(active_captures, len(self.captures)) >= MAX_CAPTURES:
                    raise ValueError("All four review capture slots are occupied")
                run_id, now = str(uuid4()), time.time()
                monotonic_deadline = time.monotonic() + budget_seconds
                with self._writing():
                    db.execute(
                        "INSERT INTO review_runs (run_id, owner, request_key, payload_json, created, updated, "
                        "deadline, status, phase, capture_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            self.owner,
                            request_key,
                            payload,
                            now,
                            now,
                            now + budget_seconds,
                            "starting",
                            "capture",
                            str(uuid4()),
                        ),
                    )
            # Both identity and attempt admission survive a lost response. Only this
            # live controller may consume the reservation; recovery never replays it.
            self.monotonic_deadlines[run_id] = monotonic_deadline
            if self.driver is None:
                self.driver = threading.Thread(target=self._drive, daemon=True)
                try:
                    self.driver.start()
                except RuntimeError as exc:
                    self.driver = None
                    self._stop(
                        self._get(run_id),
                        "interrupted",
                        f"Review driver could not start: {exc}",
                    )
            self.wake.set()
        return self.view(run_id)

    def _recover(self, run):
        if run["status"] not in RUN_ACTIVE:
            return run
        if run["owner"] == self.owner:
            if self.driver is not None and self.driver.is_alive():
                return run
            with self.guard:
                if self.driver is None or not self.driver.is_alive():
                    self._stop(
                        run,
                        "interrupted",
                        "Review controller stopped; stages will not be replayed.",
                    )
                    return self._get(
                        run["run_id"], include_results="payload_json" in run
                    )
            return run
        try:
            lease = self._lease(run["owner"])
        except ValueError:
            return run
        with lease:
            # The lifetime flock, not a PID or a stale heartbeat, proves the owner
            # cannot be dispatching. Recovery only interrupts; it never takes over.
            try:
                with self._writing(), closing(self.tasks.connect()) as db:
                    db.execute(
                        "UPDATE review_runs SET status='interrupted', error=?, updated=? "
                        "WHERE run_id=? AND status IN ('starting','running','waiting_input')",
                        (
                            "Owning MCP server exited; reserved stages will not be restarted.",
                            time.time(),
                            run["run_id"],
                        ),
                    )
                    self.tasks.recover()
            except PublicationBusy:
                return run
        return self._get(run["run_id"], include_results="payload_json" in run)

    def _stage_view(self, run, phase):
        saved = run[phase + "_json"]
        result = json.loads(saved) if saved else None
        task_id = run[phase + "_task_id"]
        if task_id and (result is None or result.get("status") in ACTIVE):
            with closing(self.tasks.connect()) as db:
                exists = db.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            if exists:
                result = self.read_task(task_id, details=True, refresh=False)
        return result

    def state(self, run_id):
        """Cheap bounded-wait projection; never loads answers or hashes snapshot bytes."""
        run = self._recover(self._get(run_id, include_results=False))
        if run["status"] not in RUN_ACTIVE:
            return run["status"]
        if run["run_id"] in self.pending_stops:
            return "running"
        task_id = (
            run[run["phase"] + "_task_id"]
            if run["phase"] in ("independent", "comparison")
            else None
        )
        if task_id:
            with closing(self.tasks.connect()) as db:
                question = db.execute(
                    "SELECT 1 FROM questions JOIN tasks USING(task_id) "
                    "WHERE task_id=? AND tasks.status='waiting_input' "
                    "AND questions.state='pending' AND questions.deadline>?",
                    (task_id, time.time()),
                ).fetchone()
            if question:
                return "waiting_input"
        return "running" if run["status"] == "waiting_input" else run["status"]

    def view(self, run_id):
        run = self._recover(self._get(run_id))
        independent = self._stage_view(run, "independent")
        comparison = self._stage_view(run, "comparison")
        current = comparison if run["phase"] == "comparison" else independent
        status = "running" if run["status"] == "waiting_input" else run["status"]
        stop_pending = self.pending_stops.get(run_id)
        if (
            not stop_pending
            and status in RUN_ACTIVE
            and current
            and current["status"] in ACTIVE
        ):
            status = (
                "waiting_input"
                if current["status"] == "waiting_input" and current.get("question")
                else "running"
            )
        question = (
            current.get("question")
            if current and status == "waiting_input" and not stop_pending
            else None
        )
        identifiers = [
            run[phase + "_task_id"]
            for phase in ("independent", "comparison")
            if run[phase + "_task_id"]
        ]
        with closing(self.tasks.connect()) as db:
            turns = (
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT accounting_json,started_at,ended_at,duration_seconds,status FROM tasks "
                        f"WHERE task_id IN ({','.join('?' for _ in identifiers)})",
                        identifiers,
                    )
                ]
                if identifiers
                else []
            )
        return {
            "run_id": run["run_id"],
            "review_id": run["review_id"],
            "task_id": run[run["phase"] + "_task_id"]
            if run["phase"] in ("independent", "comparison")
            else None,
            "status": status,
            "phase": run["phase"],
            "outcome": run["outcome"],
            "error": run["error"],
            "stop_pending": dict(stop_pending) if stop_pending else None,
            "created": run["created"],
            "deadline": run["deadline"],
            "elapsed_seconds": max(
                0,
                (time.time() if status in RUN_ACTIVE else run["updated"])
                - run["created"],
            ),
            "independent": independent,
            "comparison": comparison,
            "question": question,
            "applicability": self.reviews.assess(run["review_id"])
            if run["review_id"]
            and (run["phase"] != "capture" or status == "no_changes")
            else None,
            "findings": {
                phase: result.get("findings", []) if result else []
                for phase, result in (
                    ("independent", independent),
                    ("comparison", comparison),
                )
            },
            "usage": {
                "scope": "OMP worker turns only; excludes coordinator usage",
                "peer": conversation_usage(turns),
                "coordinator": None,
                "total_cost": None,
            },
            "next_action": "wait"
            if stop_pending
            else "reply"
            if question
            else "wait"
            if status in RUN_ACTIVE
            else "review_result"
            if status == "completed"
            else "no_action"
            if status == "no_changes"
            else "inspect_error",
        }

    def _owned(self, run_id):
        run = self._recover(self._get(run_id))
        if run["owner"] != self.owner:
            raise ValueError("Only the owning MCP server may control this review run")
        if self.closing:
            raise ValueError("MCP server is shutting down")
        return run

    def reply(self, run_id, question_id, answer):
        with self.guard, self._writing():
            run = self._owned(run_id)
            with closing(self.tasks.connect()) as db:
                replied = db.execute(
                    "SELECT questions.task_id, questions.state, questions.answer "
                    "FROM questions JOIN tasks USING(task_id) "
                    "WHERE question_id=? AND tasks.review_run_id=?",
                    (question_id, run_id),
                ).fetchone()
            if (
                replied is not None
                and replied["state"] == "answered"
                and replied["answer"] == answer
            ):
                return {**self.view(run_id), "replied_task_id": replied["task_id"]}
            if (
                run_id in self.pending_stops
                or run["status"] not in RUN_ACTIVE
                or self._remaining(run) <= 0
            ):
                raise ValueError("Review run no longer accepts clarification")
            task_id = (
                run[run["phase"] + "_task_id"]
                if run["phase"] in ("independent", "comparison")
                else None
            )
            if not task_id:
                raise ValueError("Review run has no dispatched question")
            self.reply_task(task_id, question_id, answer)
            self.wake.set()
        return {**self.view(run_id), "replied_task_id": task_id}

    def _stop(self, run, status, error=None):
        # Stop intent wins over every later transition. A contended publication
        # must not turn an accepted cancellation into a lost write or a new stage.
        self.pending_stops.setdefault(run["run_id"], {"status": status, "error": error})
        capture = self.captures.get(run["run_id"])
        if capture:
            self._kill_capture(capture[0])
        self._flush_stop(run)

    def _flush_stop(self, run):
        if run["run_id"] not in self.pending_stops:
            return
        try:
            with self._writing():
                self._commit_stop(run)
        except PublicationBusy:
            # The existing driver retries; the response remains honestly active
            # until the durable stop and child cancellation have been processed.
            return
        self.pending_stops.pop(run["run_id"], None)
        self.capture_results.pop(run["run_id"], None)

    def _commit_stop(self, run):
        pending = self.pending_stops[run["run_id"]]
        status, error = pending["status"], pending["error"]
        self._update(run["run_id"], status=status, error=error)
        for phase in ("independent", "comparison"):
            task_id = run[phase + "_task_id"]
            if not task_id:
                continue
            with closing(self.tasks.connect()) as db:
                child = db.execute(
                    "SELECT status FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            if child and child["status"] in ACTIVE:
                try:
                    self.cancel_task(task_id)
                except Exception as exc:
                    logger.exception("Review child cancellation failed")
                    self._update(
                        run["run_id"],
                        error=f"{error or status}; child cancellation failed: {exc}",
                    )

    def cancel(self, run_id):
        with self.guard:
            run = self._owned(run_id)
            if run["status"] in RUN_ACTIVE:
                self._stop(run, "cancelled")
            self.wake.set()
        # A just-killed child may need one scheduler turn to release its gate.
        # Give the uncontended case its terminal response without ever sleeping
        # under the global guard or waiting on a different healthy publication.
        until = time.monotonic() + 0.05
        while run_id in self.pending_stops and time.monotonic() < until:
            time.sleep(0.005)
            with self.guard:
                self._flush_stop(self._get(run_id))
        return self.view(run_id)

    def _remaining(self, run):
        remaining = run["deadline"] - time.time()
        deadline = self.monotonic_deadlines.get(run["run_id"])
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        return remaining

    def _capture_command(self, run):
        return [
            sys.executable,
            "-I",
            "-m",
            "omp_tandem.capture_worker",
            "--state-dir",
            str(self.reviews.scope.base),
            "--project-root",
            str(self.reviews.scope.root),
            "--run-id",
            run["run_id"],
            "--owner",
            self.owner,
            "--review-id",
            run["capture_id"],
        ]

    def _kill_capture(self, process):
        # Multiple stop/reap paths must not signal an already terminated group.
        with self.guard:
            if process in self.capture_stops:
                return
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            self.capture_stops.add(process)

    def _spawn_capture(self, run):
        with ExitStack() as stack:
            streams = []
            for _ in range(3):
                streams.append(stack.enter_context(tempfile.TemporaryFile()))
            request = _json(json.loads(run["payload_json"])["request"]).encode("utf-8")
            if len(request) > 16 * 1024 * 1024:
                raise ValueError("Review request exceeds 16 MiB")
            streams[0].write(request)
            streams[0].seek(0)
            process = subprocess.Popen(
                self._capture_command(run),
                stdin=streams[0],
                stdout=streams[1],
                stderr=streams[2],
                start_new_session=True,
                close_fds=True,
            )
            return (process, *streams, stack.pop_all())

    def _collect_capture(self, run_id, capture):
        """Only called outside the controller guard after nonblocking exit detection."""
        process, _request, output, errors, resources = capture
        try:
            self._kill_capture(process)
            process.wait()
            output.seek(0)
            errors.seek(0)
            result = output.read(_CAPTURE_OUTPUT_LIMIT + 1)
            error = errors.read(_CAPTURE_OUTPUT_LIMIT + 1)
            if (
                len(result) > _CAPTURE_OUTPUT_LIMIT
                or len(error) > _CAPTURE_OUTPUT_LIMIT
            ):
                raise ValueError("Snapshot capture response exceeds protocol limit")
            return process.returncode, result, error
        finally:
            resources.close()
            with self.guard:
                self.captures.pop(run_id, None)
                self.capture_stops.discard(process)

    def _capture_step(self, run_id):
        with self.guard:
            run = self._get(run_id)
            if (
                self.closing
                or run_id in self.pending_stops
                or run["status"] not in RUN_ACTIVE
            ):
                return
            if self._remaining(run) <= 0:
                self._advance(run)
                return
            capture = self.captures.get(run_id)
            if capture is None and run_id not in self.capture_results:
                if run["capture_started"]:
                    self._stop(
                        run,
                        "interrupted",
                        "Reserved snapshot capture will not be replayed.",
                    )
                    return
                self._update(run_id, capture_started=1)
        if capture is None and run_id not in self.capture_results:
            # No SQLite transaction or controller guard spans process creation or IPC.
            capture = self._spawn_capture(run)
            with self.guard:
                self.captures[run_id] = capture
                current = self._get(run_id)
                if (
                    self.closing
                    or run_id in self.pending_stops
                    or current["status"] not in RUN_ACTIVE
                ):
                    self._kill_capture(capture[0])
                elif self._remaining(current) <= 0:
                    self._advance(current)
            return
        if run_id in self.capture_results:
            snapshot, failure = self.capture_results[run_id]
        else:
            if capture[0].poll() is None:
                return
            code, output, error = self._collect_capture(run_id, capture)
            snapshot = None
            failure = None
            try:
                if code:
                    raise ValueError(
                        error.decode("utf-8", "replace").strip()
                        or f"worker exited {code}"
                    )
                snapshot = json.loads(output)
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("review_id") != run["capture_id"]
                ):
                    raise ValueError(
                        "Capture response does not match the reserved snapshot"
                    )
                # The child response alone is not proof of publication in our source DB.
                manifest = self.reviews._manifest(run["capture_id"])
                if snapshot != self.reviews._summary(manifest):
                    raise ValueError(
                        "Capture response does not match the saved snapshot"
                    )
            except Exception as exc:
                logger.exception("Review snapshot response could not be collected")
                failure = f"Snapshot capture failed: {exc}"
            self.capture_results[run_id] = (snapshot, failure)
        with self.guard:
            current = self._get(run_id)
            if (
                self.closing
                or run_id in self.pending_stops
                or current["status"] not in RUN_ACTIVE
            ):
                self.capture_results.pop(run_id, None)
                return
            if self._remaining(current) <= 0:
                self._advance(current)
            elif failure:
                self._stop(current, "failed", failure)
            elif current["review_id"] != current["capture_id"]:
                self._stop(
                    current, "failed", "Snapshot publication lost its run mapping"
                )
            elif not snapshot["change_count"]:
                self._update(run_id, status="no_changes")
            else:
                self._update(run_id, phase="independent")
            self.capture_results.pop(run_id, None)

    def _reap_captures(self):
        with self.guard:
            captures = list(self.captures.items())
        for run_id, capture in captures:
            with self.guard:
                active = self._get(run_id)["status"] in RUN_ACTIVE
            if not active and capture[0].poll() is not None:
                self._collect_capture(run_id, capture)

    def _advance(self, run):
        if run["run_id"] in self.pending_stops:
            return
        if self._remaining(run) <= 0:
            self._stop(
                run,
                "failed",
                "Total review budget exhausted, including startup and clarification time.",
            )
            return
        with self._writing():
            self._advance_stage(run)

    def _advance_stage(self, run):
        phase = run["phase"]
        if phase == "capture":
            return
        task_id = run[phase + "_task_id"]
        if task_id is None:
            task_id = str(uuid4())
            self._update(
                run["run_id"], **{phase + "_task_id": task_id}, status="starting"
            )
            payload = json.loads(run["payload_json"])
            remaining = max(1, math.ceil(self._remaining(run)))
            execution = payload["execution"]
            timeout = min(
                remaining, resolve_execution(execution)["effective"]["timeout_seconds"]
            )
            first = (
                json.loads(run["independent_json"]) if run["independent_json"] else None
            )
            try:
                result = self.start_task(
                    prompt=(
                        "Independently assess the pinned review snapshot against its saved requirements and criteria. "
                        "Read only saved review material. Do not request or infer the author proposal. "
                        "If necessary context is absent, ask explicitly for an expanded capture or report blocked; "
                        "never read live files. Submit a structured report with the complete assessment and findings."
                        if phase == "independent"
                        else "Compare your completed independent assessment with the saved author proposal and rationale "
                        "for this same snapshot. Preserve disagreements and uncertainties; do not synthesize consensus. "
                        "Read only saved review material and submit a structured complete comparison with findings."
                    ),
                    cwd=str(self.reviews.scope.root),
                    mode="think",
                    conversation_id=first["conversation_id"] if first else None,
                    timeout_seconds=timeout,
                    question_timeout_seconds=min(timeout, 1800),
                    execution=execution,
                    review_id=run["review_id"],
                    review_stage=phase,
                    reserved_task_id=task_id,
                    review_run_id=run["run_id"],
                )
                if result["task_id"] != task_id:
                    raise ValueError("Dispatch did not consume the reserved task ID")
                if self._remaining(run) <= 0:
                    self._advance(self._get(run["run_id"]))
                    return
                self._update(run["run_id"], status="running")
            except PublicationBusy:
                # Admission already has a durable reservation. Observe that same
                # task on the next drive; never fail or replay admitted work just
                # because its subsequent status persistence was contended.
                raise
            except Exception as exc:
                logger.exception("Review stage startup failed")
                if self._remaining(run) <= 0:
                    self._advance(self._get(run["run_id"]))
                    return
                self._stop(
                    self._get(run["run_id"]), "failed", f"{phase} startup failed: {exc}"
                )
            return
        with closing(self.tasks.connect()) as db:
            exists = db.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if not exists:
            self._stop(
                run,
                "interrupted",
                "Reserved stage has no admitted task; it will not be replayed.",
            )
            return
        result = self.read_task(task_id, details=True)
        if self._remaining(run) <= 0:
            self._advance(self._get(run["run_id"]))
            return
        if result["status"] in ACTIVE:
            self._update(
                run["run_id"],
                status="waiting_input"
                if result["status"] == "waiting_input"
                else "running",
            )
            return
        # Persist the entire first answer before making comparison eligible.
        self._update(run["run_id"], **{phase + "_json": _json(result)})
        outcome = result.get("outcome")
        if result["status"] != "completed":
            self._update(
                run["run_id"],
                status=result["status"]
                if result["status"] in ("failed", "cancelled", "interrupted")
                else "failed",
                error=result.get("error"),
                outcome=outcome,
            )
            return
        if outcome != "success" or not result.get("report"):
            self._update(
                run["run_id"],
                status="completed" if outcome in ("blocked", "partial") else "failed",
                outcome=outcome,
                error=None
                if outcome in ("blocked", "partial")
                else "Stage ended without a structured successful outcome",
            )
            return
        payload = json.loads(run["payload_json"])
        author = payload["request"]
        if (
            phase == "independent"
            and payload["compare"]
            and (
                author["author_proposal"].strip() or author["author_rationale"].strip()
            )
        ):
            self._update(run["run_id"], phase="comparison", status="starting")
        else:
            self._update(run["run_id"], status="completed", outcome="success")

    def _drive(self):
        try:
            while True:
                with self.guard:
                    if self.closing:
                        return
                    with closing(self.tasks.connect()) as db:
                        runs = [
                            dict(row)
                            for row in db.execute(
                                "SELECT * FROM review_runs WHERE owner=? AND status IN "
                                "('starting','running','waiting_input') ORDER BY created",
                                (self.owner,),
                            )
                        ]
                    for run_id in list(self.pending_stops):
                        self._flush_stop(self._get(run_id))
                for run in runs:
                    try:
                        if run["phase"] == "capture":
                            self._capture_step(run["run_id"])
                        else:
                            with self.guard:
                                current = self._get(run["run_id"])
                                if not self.closing and current["status"] in RUN_ACTIVE:
                                    self._advance(current)
                    except PublicationBusy:
                        # No admission occurred without its committed reservation.
                        # Retry this same transition, not an admitted native stage.
                        continue
                    except Exception as exc:
                        logger.exception("Review driver failed")
                        with self.guard:
                            current = self._get(run["run_id"])
                            if current["status"] in RUN_ACTIVE:
                                self._stop(
                                    current,
                                    "interrupted",
                                    f"Review driver failed: {exc}",
                                )
                self._reap_captures()
                self.wake.wait(
                    0.05 if runs or self.captures or self.pending_stops else None
                )
                self.wake.clear()
        except BaseException:
            logger.exception("Review controller exited unexpectedly")
            with self.guard:
                for capture in self.captures.values():
                    self._kill_capture(capture[0])
                try:
                    with closing(self.tasks.connect()) as db:
                        runs = db.execute(
                            "SELECT * FROM review_runs WHERE owner=? AND status IN "
                            "('starting','running','waiting_input')",
                            (self.owner,),
                        ).fetchall()
                    for run in runs:
                        self._stop(
                            dict(run),
                            "interrupted",
                            "Review controller exited unexpectedly",
                        )
                finally:
                    for capture in self.captures.values():
                        self._kill_capture(capture[0])
                    self.lease.close()
            for run_id, capture in list(self.captures.items()):
                self._collect_capture(run_id, capture)

    def close(self):
        with self.guard:
            if self.closing:
                return
            self.closing = True
            for capture in self.captures.values():
                self._kill_capture(capture[0])
            with closing(self.tasks.connect()) as db:
                runs = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM review_runs WHERE owner=? AND status IN ('starting','running','waiting_input')",
                        (self.owner,),
                    )
                ]
            for run in runs:
                self._stop(
                    run,
                    "interrupted",
                    "Owning MCP server shut down; stages will not restart.",
                )
            self.wake.set()
        try:
            if self.driver is not None:
                self.driver.join()
            for run_id, capture in list(self.captures.items()):
                self._kill_capture(capture[0])
                self._collect_capture(run_id, capture)
            with self.guard:
                for run_id in list(self.pending_stops):
                    self._flush_stop(self._get(run_id))
        finally:
            # If another owner is publishing, durable interruption is deferred to
            # lease recovery. Never wait for or kill that owner's healthy work.
            self.lease.close()
            self.database_anchor.close()
