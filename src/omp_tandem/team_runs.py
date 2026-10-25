"""Owner-leased DAG execution in the live workspace, never replaying uncertain work."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
from contextlib import closing, suppress
from pathlib import Path
from uuid import UUID, uuid4

from .execution import resolve_execution
from .models import TaskContract
from .runtime_models import ACTIVE
from .team_models import TeamPlan, team_dependency_ready
from .workspace import TaskAdmissionBusy, WorkerSlots

ACTIVE_TEAM = frozenset({"running", "waiting_input", "waiting_models", "cancelling"})
_ACTIVE_SQL = "('running','waiting_input','waiting_models','cancelling')"
_BUSY_STEPS = frozenset({"reserved", *ACTIVE})
logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _execution(plan, step):
    return {
        **(plan.get("execution") or {}),
        **(step.get("execution") or {}),
    }


def _same_plan(saved, submitted):
    # Only the newly introduced default is equivalent to its historical absence.
    left, right = json.loads(saved), json.loads(submitted)
    for plan in (left, right):
        for step in plan["steps"]:
            step.setdefault("dependency_policy", "success")
    return _json(left) == _json(right)


class TeamRuns:
    def __init__(
        self, tasks, scope, start_task, read_task, cancel_task, selection, owner
    ):
        self.tasks, self.scope = tasks, scope
        self.slots = WorkerSlots(scope.base)
        self.start_task, self.read_task, self.cancel_task = (
            start_task,
            read_task,
            cancel_task,
        )
        self.selection = selection
        self.owner = str(UUID(owner))
        self.guard = threading.RLock()
        self.close_guard = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.closing = False
        self.monotonic_deadlines = {}
        self.driver = None
        self.lease = self._lease(self.owner)
        try:
            with closing(self.tasks.connect()) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS team_runs (
                        run_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                        request_key TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL,
                        selection_json TEXT, status TEXT NOT NULL,
                        created REAL NOT NULL, updated REAL NOT NULL,
                        deadline REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                        error TEXT, selection_started INTEGER NOT NULL DEFAULT 0, outcome TEXT,
                        stop_status TEXT
                    );
                    CREATE TABLE IF NOT EXISTS team_steps (
                        run_id TEXT NOT NULL, step_id TEXT NOT NULL, task_id TEXT UNIQUE,
                        status TEXT NOT NULL, result_json TEXT, error TEXT,
                        ordinal INTEGER NOT NULL, dependency_json TEXT,
                        PRIMARY KEY (run_id, step_id)
                    );
                    CREATE INDEX IF NOT EXISTS team_run_owner ON team_runs(owner, status);
                """)
                columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(team_runs)")
                }
                if "stop_status" not in columns:
                    db.execute("ALTER TABLE team_runs ADD COLUMN stop_status TEXT")
                # Reusing an owner UUID is not authority to resume its old controller.
                for row in db.execute(
                    f"SELECT run_id FROM team_runs WHERE owner=? AND status IN {_ACTIVE_SQL}",
                    (self.owner,),
                ).fetchall():
                    self._interrupt_db(
                        db,
                        row["run_id"],
                        "Owner lease was lost; work will not be replayed.",
                    )
        except BaseException:
            self.lease.close()
            raise

    def _lease(self, owner):
        handle = (self.tasks.root / f"team-owner-{UUID(owner)!s}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ValueError("Team owner is still active") from None
        return handle

    def _get(self, run_id, *, metadata_only=False):
        fields = "run_id,owner,status" if metadata_only else "*"
        with closing(self.tasks.connect()) as db:
            row = db.execute(
                f"SELECT {fields} FROM team_runs WHERE run_id=?", (str(UUID(run_id)),)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown team run in this project")
        return dict(row)

    def _steps(self, run_id):
        with closing(self.tasks.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM team_steps WHERE run_id=? ORDER BY ordinal",
                    (run_id,),
                )
            ]

    def _update(self, run_id, **values):
        values["updated"] = time.time()
        with closing(self.tasks.connect()) as db:
            db.execute(
                f"UPDATE team_runs SET {', '.join(key + '=?' for key in values)} WHERE run_id=? AND owner=?",
                [*values.values(), run_id, self.owner],
            )

    def _step_update(self, run_id, step_id, **values):
        with closing(self.tasks.connect()) as db:
            db.execute(
                f"UPDATE team_steps SET {', '.join(key + '=?' for key in values)} WHERE run_id=? AND step_id=?",
                [*values.values(), run_id, step_id],
            )

    @staticmethod
    def _interrupt_db(db, run_id, reason):
        db.execute(
            f"UPDATE team_runs SET status='interrupted',error=?,updated=? WHERE run_id=? AND status IN {_ACTIVE_SQL}",
            (reason, time.time(), run_id),
        )
        db.execute(
            "UPDATE team_steps SET status=CASE WHEN task_id IS NULL THEN 'blocked' ELSE 'interrupted' END, "
            "error=? WHERE run_id=? AND status IN ('pending','reserved','starting','running','waiting_input','cancelling')",
            (reason, run_id),
        )

    def _recover(self, run):
        if run["status"] not in ACTIVE_TEAM:
            return run
        if run["owner"] == self.owner:
            return run
        try:
            lease = self._lease(run["owner"])
        except ValueError:
            return run
        with lease, closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._interrupt_db(
                db,
                run["run_id"],
                "Owner lease was lost; accepted work will not be replayed.",
            )
            db.commit()
        return self._get(run["run_id"], metadata_only="plan_json" not in run)

    def _owned(self, run_id):
        run = self._recover(self._get(run_id))
        if run["owner"] != self.owner:
            raise ValueError("Only the owning MCP server may control this team run")
        if self.closing:
            raise ValueError("MCP server is shutting down")
        return run

    def _remaining(self, run):
        remaining = run["deadline"] - time.time()
        if run["run_id"] in self.monotonic_deadlines:
            remaining = min(
                remaining, self.monotonic_deadlines[run["run_id"]] - time.monotonic()
            )
        return remaining

    def start(self, request_key, plan):
        if (
            not isinstance(request_key, str)
            or not request_key.strip()
            or len(request_key) > 200
        ):
            raise ValueError("request_key must be nonblank and at most 200 characters")
        validated = TeamPlan.model_validate(plan)
        cwd = self.scope.validate_cwd(validated.cwd)
        # MCP-granted extra roots never widen a team's launch-bound project.
        if not cwd.is_relative_to(self.scope.root):
            raise ValueError("Team cwd must remain inside the bound project")
        payload = validated.model_dump(mode="json")
        payload["cwd"] = str(cwd)
        payload["execution"] = (
            validated.execution.model_dump(exclude_unset=True, exclude_none=True)
            if validated.execution
            else None
        )
        if (payload["execution"] or {}).get("purpose", "work") != "work":
            raise ValueError("Team turns cannot use reserved coordination capacity")
        for step, item in zip(validated.steps, payload["steps"], strict=True):
            item["execution"] = (
                step.execution.model_dump(exclude_unset=True, exclude_none=True)
                if step.execution
                else None
            )
            if (item["execution"] or {}).get("purpose", "work") != "work":
                raise ValueError("Team turns cannot use reserved coordination capacity")
            # Reserve bounded context for role advice and complete evidence pointers.
            role_size = len(_json(step.role or "development")) + 128
            if (
                len(step.contract.context)
                + role_size
                + 1024
                + 1400 * len(step.depends_on)
                > 60000
            ):
                raise ValueError(
                    f"Step {step.id} context has no room for team role and predecessor evidence"
                )
        encoded = _json(payload)
        with self.guard:
            if self.closing:
                raise ValueError("MCP server is shutting down")
            with closing(self.tasks.connect()) as db:
                existing = db.execute(
                    "SELECT run_id,plan_json FROM team_runs WHERE request_key=?",
                    (request_key,),
                ).fetchone()
            if existing:
                if not _same_plan(existing["plan_json"], encoded):
                    raise ValueError(
                        "request_key already identifies a different team plan"
                    )
                return self.view(existing["run_id"])
            # Validate all contracts before accepting any work or invoking selection.
            for step in payload["steps"]:
                # Contention is transient; native admission revalidates before launch.
                with suppress(TaskAdmissionBusy):
                    self.start_task(
                        contract=step["contract"],
                        mode=step["mode"],
                        cwd=payload["cwd"],
                        execution=_execution(payload, step),
                        project_context_id=payload["project_context_id"],
                        dry_run=True,
                    )
            now = time.time()
            run_id = str(uuid4())
            with closing(self.tasks.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                # A second MCP owner may have admitted this project-scoped key during preflight.
                existing = db.execute(
                    "SELECT run_id,plan_json FROM team_runs WHERE request_key=?",
                    (request_key,),
                ).fetchone()
                if existing:
                    db.rollback()
                    if not _same_plan(existing["plan_json"], encoded):
                        raise ValueError(
                            "request_key already identifies a different team plan"
                        )
                    return self.view(existing["run_id"])
                db.execute(
                    "INSERT INTO team_runs(run_id,owner,request_key,plan_json,status,created,updated,deadline) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        self.owner,
                        request_key,
                        encoded,
                        "running",
                        now,
                        now,
                        now + validated.budget_seconds,
                    ),
                )
                db.executemany(
                    "INSERT INTO team_steps(run_id,step_id,status,ordinal) VALUES(?,?,'pending',?)",
                    [
                        (run_id, step.id, index)
                        for index, step in enumerate(validated.steps)
                    ],
                )
                db.commit()
            self.monotonic_deadlines[run_id] = (
                time.monotonic() + validated.budget_seconds
            )
            if self.driver is None:
                self.driver = threading.Thread(
                    target=self._drive, name="tandem-team-controller", daemon=True
                )
                try:
                    self.driver.start()
                except RuntimeError:
                    self._stop(
                        self._get(run_id),
                        "interrupted",
                        "Could not start team controller",
                    )
                    raise
            self.wake.set()
            return self.view(run_id)

    def state(self, run_id):
        return self._recover(self._get(run_id, metadata_only=True))["status"]

    @staticmethod
    def _dependency_ready(step, predecessor):
        result = json.loads(predecessor["result_json"] or "{}")
        return team_dependency_ready(
            step.get("dependency_policy", "success"),
            step_status=predecessor["status"],
            native_status=result.get("status"),
            outcome=result.get("outcome"),
            report_outcome=(result.get("report") or {}).get("outcome"),
        )

    def _pending_models(self, plan, selection, saved):
        blocked = set()
        changed = True
        while changed:
            changed = False
            for step in plan["steps"]:
                if saved[step["id"]]["status"] != "pending" or step["id"] in blocked:
                    continue
                if any(
                    key in blocked
                    or (
                        saved[key]["status"] not in _BUSY_STEPS | {"pending"}
                        and not self._dependency_ready(step, saved[key])
                    )
                    for key in step["depends_on"]
                ):
                    blocked.add(step["id"])
                    changed = True
        return [
            step["id"]
            for step in plan["steps"]
            if saved[step["id"]]["status"] == "pending"
            and step["id"] not in blocked
            and step["id"] not in selection.get("assignments", {})
        ]

    @staticmethod
    def _next_action(status, outcome, pending, questions):
        return (
            "reply"
            if questions
            else "agree_models"
            if pending
            else "wait"
            if status in ACTIVE_TEAM
            else "review_partial_result"
            if status == "completed" and outcome == "partial"
            else "review_result"
            if status == "completed"
            else "inspect_error"
        )

    def observation(self, run_id):
        run_id = str(UUID(run_id))
        with closing(self.tasks.connect()) as db:
            identity = db.execute(
                "SELECT run_id,owner,status FROM team_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if identity is None:
            raise ValueError("Unknown team run in this project")
        self._recover(dict(identity))
        with self.guard, closing(self.tasks.connect()) as db:
            db.execute("BEGIN")
            run = dict(
                db.execute(
                    "SELECT run_id,owner,status,outcome,deadline,cancel_requested,"
                    "json_extract(selection_json,'$.status') AS selection_status "
                    "FROM team_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            )
            plan = {
                "steps": [
                    {
                        "id": row["step_id"],
                        "depends_on": json.loads(row["dependencies"]),
                        "dependency_policy": row["dependency_policy"],
                    }
                    for row in db.execute(
                        "SELECT json_extract(value,'$.id') AS step_id,"
                        "json_extract(value,'$.depends_on') AS dependencies,"
                        "COALESCE(json_extract(value,'$.dependency_policy'),'success') AS dependency_policy "
                        "FROM json_each((SELECT plan_json FROM team_runs WHERE run_id=?),'$.steps')",
                        (run_id,),
                    )
                ]
            }
            selection = (
                {
                    "assignments": {
                        row["key"]: None
                        for row in db.execute(
                            "SELECT key FROM json_each((SELECT selection_json FROM team_runs WHERE run_id=?),'$.assignments')",
                            (run_id,),
                        )
                    }
                }
                if run["selection_status"] in {"ready", "needs_assignment"}
                else {}
            )
            saved = {}
            for row in db.execute(
                "SELECT s.step_id,s.status,t.status AS native_status,"
                "json_extract(t.report_json,'$.outcome') AS outcome "
                "FROM team_steps s LEFT JOIN tasks t USING(task_id) WHERE s.run_id=?",
                (run_id,),
            ):
                status = row["status"]
                if (
                    status in _BUSY_STEPS
                    and row["native_status"] is not None
                    and row["native_status"] not in ACTIVE
                ):
                    status = (
                        "completed"
                        if row["native_status"] == "completed"
                        and row["outcome"] == "success"
                        else "partial"
                        if row["native_status"] == "completed"
                        and row["outcome"] == "partial"
                        else "failed"
                    )
                saved[row["step_id"]] = {
                    "status": status,
                    "result_json": _json(
                        {
                            "status": row["native_status"],
                            "outcome": row["outcome"],
                            "report": {"outcome": row["outcome"]},
                        }
                    ),
                }
            actionable = (
                run["status"] in {"running", "waiting_input", "waiting_models"}
                and not run["cancel_requested"]
                and self._remaining(run) > 0
            )
            questions = (
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT q.task_id,q.question_id,q.deadline FROM questions q "
                        "JOIN tasks t USING(task_id) JOIN team_steps s USING(task_id) "
                        "WHERE s.run_id=? AND q.state='pending' AND q.deadline>? "
                        "AND t.status='waiting_input' AND t.cancel_requested=0 ORDER BY s.ordinal",
                        (run_id, time.time()),
                    )
                ]
                if actionable
                else []
            )
            pending = (
                self._pending_models(plan, selection, saved)
                if actionable and selection
                else []
            )
            status = run["status"]
            if actionable:
                busy = any(state["status"] in _BUSY_STEPS for state in saved.values())
                ready = any(
                    saved[step["id"]]["status"] == "pending"
                    and step["id"] in selection.get("assignments", {})
                    and all(
                        self._dependency_ready(step, saved[key])
                        for key in step["depends_on"]
                    )
                    for step in plan["steps"]
                )
                status = (
                    "waiting_input"
                    if questions
                    else "running"
                    if busy or ready or not pending
                    else "waiting_models"
                )
            return {
                "status": status,
                "outcome": run["outcome"],
                "pending_models": pending,
                "questions": questions,
                "next_action": self._next_action(
                    status, run["outcome"], pending, questions
                ),
            }

    def _outcome_artifact_id(self, task_id):
        if not task_id:
            return None
        with closing(self.tasks.connect()) as db:
            task = db.execute(
                "SELECT conversation_id,report_json,result_artifacts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not task or not task["report_json"]:
                return None
            digest = hashlib.sha256(task["report_json"].encode("utf-8")).hexdigest()
            for artifact_id in reversed(json.loads(task["result_artifacts"])):
                if db.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id=? AND task_id=? "
                    "AND conversation_id=? AND name='outcome' AND media_type='application/json' AND sha256=?",
                    (artifact_id, task_id, task["conversation_id"], digest),
                ).fetchone():
                    return artifact_id
        return None

    def view(self, run_id, details=False):
        run = self._recover(self._get(run_id))
        plan = json.loads(run["plan_json"])
        selection = json.loads(run["selection_json"]) if run["selection_json"] else {}
        observation = self.observation(run_id)
        current_questions = {item["question_id"] for item in observation["questions"]}
        assignments = selection.get("assignments", {})
        definitions = {step["id"]: step for step in plan["steps"]}
        steps, questions, workers = [], [], []
        final_id = TeamPlan.model_validate(plan).integration_step
        final = None
        for saved in self._steps(run["run_id"]):
            result = json.loads(saved["result_json"]) if saved["result_json"] else None
            # Terminal/interrupted views can still expose actual worker outputs; never dispatch.
            if saved["task_id"] and (result is None or result.get("status") in ACTIVE):
                with closing(self.tasks.connect()) as db:
                    exists = db.execute(
                        "SELECT 1 FROM tasks WHERE task_id=?", (saved["task_id"],)
                    ).fetchone()
                if exists:
                    result = self.read_task(
                        saved["task_id"], details=True, refresh=False
                    )
            definition = definitions[saved["step_id"]]
            assignment = assignments.get(saved["step_id"])
            item = {
                "id": saved["step_id"],
                "title": definition["title"],
                "mode": definition["mode"],
                "depends_on": definition["depends_on"],
                "dependency_policy": definition.get("dependency_policy", "success"),
                "outcome_artifact_id": self._outcome_artifact_id(saved["task_id"]),
                "status": saved["status"],
                "task_id": saved["task_id"],
                "error": saved["error"],
                "queue_reason": saved["error"]
                if saved["status"] == "pending"
                else None,
                "assignment": assignment,
                "selected_model": assignment.get("model") if assignment else None,
                "selected_thinking": assignment.get("thinking") if assignment else None,
                "execution": result.get("execution") if result else None,
                "outcome": result.get("outcome") if result else None,
                "question": result.get("question")
                if result
                and (result.get("question") or {}).get("question_id")
                in current_questions
                else None,
                "dependency_evidence": json.loads(saved["dependency_json"])
                if saved["dependency_json"]
                else [],
            }
            if result:
                item.update(
                    {
                        key: result.get(key)
                        for key in ("summary", "answer_artifact_id", "artifacts")
                    }
                )
                if details:
                    item["result"] = result
                workers.append(
                    {
                        "step_id": saved["step_id"],
                        "task_id": saved["task_id"],
                        "usage": (result.get("usage") or {}).get("task"),
                    }
                )
            if item["question"]:
                questions.append(
                    {
                        "step_id": saved["step_id"],
                        "task_id": saved["task_id"],
                        "question": item["question"],
                    }
                )
            if saved["step_id"] == final_id:
                final = result
            steps.append(item)
        status = observation["status"]
        capacity = {
            **self.slots.capacity(),
            "purpose": "work",
            "requested_parallel": plan["max_parallel"],
            "effective_parallel": min(plan["max_parallel"], WorkerSlots.LIMITS["work"]),
        }
        value = {
            "run_id": run["run_id"],
            "request_key": run["request_key"],
            "capacity": capacity,
            "goal": plan["goal"],
            "status": status,
            "outcome": observation["outcome"],
            "error": run["error"],
            "created": run["created"],
            "deadline": run["deadline"],
            "cancel_requested": bool(run["cancel_requested"]),
            "stop_status": run["stop_status"],
            "workspace": "Live project workspace; declared ownership is coordination, not a sandbox or independent snapshot review.",
            "steps": steps,
            "progress": {
                "total": len(steps),
                "completed": sum(item["status"] == "completed" for item in steps),
            },
            "integration_step": final_id,
            "final": final,
            "answer": final.get("answer") if final else None,
            "answer_artifact_id": final.get("answer_artifact_id") if final else None,
            "outcome_artifact_id": next(
                item["outcome_artifact_id"] for item in steps if item["id"] == final_id
            ),
            "questions": questions,
            "question": questions[0]["question"] if questions else None,
            "task_id": questions[0]["task_id"] if questions else None,
            "selection": selection,
            "catalog": selection.get("catalog"),
            "pending_models": observation["pending_models"],
            "actions": [
                {"action": "reply", **item} for item in observation["questions"]
            ]
            + (
                [{"action": "assign", "pending_models": observation["pending_models"]}]
                if observation["pending_models"]
                else []
            ),
            "incomplete_steps": [
                item["id"]
                for item in steps
                if item["status"] != "completed" or item["outcome"] != "success"
            ],
            "usage": {
                "scope": "OMP worker turns only; excludes coordinator usage",
                "workers": workers,
                "jev": selection.get("jev"),
                "coordinator": None,
                "total_cost": None,
            },
            "next_action": observation["next_action"],
        }
        if observation["pending_models"]:
            value["guidance"] = (
                "Consult the OMP partner using an explicitly listed OpenAI subscription model/thinking pair. Agree model, thinking and reason for exactly every current pending_models step from selection.eligible_assignments, then call team assign. Assigned ready workers may run meanwhile."
            )
        if details:
            value["plan"] = plan
        return value

    def assign(self, run_id, assignments):
        with self.guard:
            run = self._owned(run_id)
            if (
                run["status"] not in {"running", "waiting_input", "waiting_models"}
                or run["cancel_requested"]
            ):
                raise ValueError(
                    "Team cannot accept model agreement in its current state"
                )
            if self._remaining(run) <= 0:
                self._stop(
                    run, "failed", "Team deadline expired before model agreement"
                )
                return self.view(run_id)
            plan = json.loads(run["plan_json"])
            original = json.loads(run["selection_json"] or "{}")
            saved = self._refresh_steps(run)
            if saved is None:
                raise ValueError("Team work is uncertain; model agreement is closed")
            self._block_dependencies(run, plan, saved)
            pending = self._pending_models(plan, original, saved) if original else []
            if (
                not pending
                or not isinstance(assignments, dict)
                or set(assignments) != set(pending)
            ):
                raise ValueError(
                    "Assign exactly current actionable pending_models: "
                    + _json(pending)
                )
            action_set = copy.deepcopy(original)
            action_set.update(status="needs_assignment", pending=pending)
            selected = self.selection.assign(plan, action_set, assignments)
            if selected.get("status") != "ready":
                raise ValueError(
                    "Model agreement must resolve every pending assignment"
                )
            if self._remaining(run) <= 0:
                self._stop(
                    run, "failed", "Team deadline expired during model agreement"
                )
                return self.view(run_id)
            # Keep frozen eligibility, Jev decisions and original reasons intact.
            original["assignments"] = selected["assignments"]
            original["pending"] = [
                key
                for key in original.get("pending", [])
                if key not in selected["assignments"]
            ]
            original["status"] = "needs_assignment" if original["pending"] else "ready"
            self._update(run_id, selection_json=_json(original), status="running")
            self.wake.set()
            return self.view(run_id)

    def _stop(self, run, status, reason=None):
        run = self._get(run["run_id"])
        if run["owner"] != self.owner or run["status"] not in ACTIVE_TEAM:
            return
        if run["status"] != "cancelling":
            # Close native reservation admission before delivering stop requests.
            # The requested terminal status is not proof that native work has ended.
            with closing(self.tasks.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE team_runs SET status='cancelling',stop_status=?,cancel_requested=1,error=?,updated=? "
                    "WHERE run_id=? AND owner=?",
                    (status, reason, time.time(), run["run_id"], self.owner),
                )
                db.execute(
                    "UPDATE team_steps SET status=?,error=? WHERE run_id=? AND status='pending'",
                    (
                        "cancelled" if status == "cancelled" else "blocked",
                        reason,
                        run["run_id"],
                    ),
                )
                db.commit()
        self._flush_stop(self._get(run["run_id"]))
        self.wake.set()

    def _flush_stop(self, run):
        if run["owner"] != self.owner or run["status"] != "cancelling":
            return
        active = False
        for step in self._steps(run["run_id"]):
            if step["status"] not in _BUSY_STEPS:
                continue
            with closing(self.tasks.connect()) as db:
                exists = db.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (step["task_id"],)
                ).fetchone()
            if not exists:
                # A durable reservation without admission cannot have a native
                # worker. It remains non-replayable, with the uncertainty visible.
                self._step_update(
                    run["run_id"],
                    step["step_id"],
                    status="interrupted",
                    error="Reserved task was not admitted; no replay",
                )
                continue
            with suppress(Exception):
                self.cancel_task(step["task_id"])
            try:
                result = self.read_task(step["task_id"], details=True)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                # Failure to observe termination is not evidence of termination.
                active = True
                continue
            active = active or result["status"] in ACTIVE
            self._step_update(
                run["run_id"],
                step["step_id"],
                status=result["status"],
                result_json=_json(result),
                error=result.get("error"),
            )
        if not active:
            self._update(run["run_id"], status=run["stop_status"] or "interrupted")

    def _drain_stops(self):
        while True:
            with self.guard, closing(self.tasks.connect()) as db:
                runs = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM team_runs WHERE owner=? AND status='cancelling'",
                        (self.owner,),
                    )
                ]
                if not runs:
                    return
                for run in runs:
                    self._flush_stop(run)
            time.sleep(0.05)

    def cancel(self, run_id):
        with self.guard:
            run = self._owned(run_id)
            self._stop(
                run,
                "cancelled",
                "Team cancellation requested; pending steps will not launch.",
            )
            return self.view(run_id)

    def _cancelled(self, run_id):
        if self.stopping.is_set():
            return True
        run = self._get(run_id)
        return (
            run["cancel_requested"]
            or run["status"] not in ACTIVE_TEAM
            or self._remaining(run) <= 0
        )

    def _select(self, run):
        run_id = run["run_id"]
        with self.guard:
            current = self._get(run_id)
            if current["owner"] != self.owner:
                return
            if self._remaining(current) <= 0:
                self._stop(
                    current, "failed", "Team deadline expired before model selection"
                )
                return
            if self._cancelled(run_id):
                return
            if current["selection_started"]:
                self._stop(
                    current,
                    "interrupted",
                    "Model selection was interrupted; it will not be replayed.",
                )
                return
            self._update(run_id, selection_started=1)
        # Classification may block on bounded native/Jev calls. Never hold the cancellation guard.
        selected = self.selection.select(
            json.loads(run["plan_json"]),
            deadline=time.monotonic() + max(0, self._remaining(run)),
            cancelled=lambda: self._cancelled(run_id),
        )
        with self.guard:
            current = self._get(run_id)
            # Persist even a late diagnostic, but never revive a stopped run.
            self._update(run_id, selection_json=_json(selected))
            if self._cancelled(run_id):
                if current["status"] in ACTIVE_TEAM:
                    self._stop(
                        current,
                        "failed",
                        "Team deadline expired during model selection",
                    )
                return
            if selected.get("status") in {"ready", "needs_assignment"}:
                # _advance derives quiescent model waiting from per-step readiness.
                self._update(run_id, status="running")
            else:
                self._stop(
                    current,
                    "failed",
                    "Available-model selection is unavailable; inspect selection diagnostics",
                )

    @staticmethod
    def _conflicts(plan, step, other_plan, other):
        if step["mode"] != "work" and other["mode"] != "work":
            return False
        if (
            step["mode"] == "work" and not step["contract"]["scope"]["owned_files"]
        ) or (
            other["mode"] == "work" and not other["contract"]["scope"]["owned_files"]
        ):
            return True
        left = [
            (Path(plan["cwd"]) / item).resolve()
            for item in step["contract"]["scope"]["owned_files"]
        ]
        right = [
            (Path(other_plan["cwd"]) / item).resolve()
            for item in other["contract"]["scope"]["owned_files"]
        ]
        return any(
            a == b or a.is_relative_to(b) or b.is_relative_to(a)
            for a in left
            for b in right
        )

    def _busy(self):
        with closing(self.tasks.connect()) as db:
            rows = db.execute(
                "SELECT r.plan_json,s.step_id FROM team_steps s JOIN team_runs r USING(run_id) "
                "WHERE r.status IN ('running','waiting_input','cancelling') AND s.status IN ('reserved','starting','running','waiting_input','cancelling')"
            ).fetchall()
        busy = []
        for row in rows:
            plan = json.loads(row["plan_json"])
            busy.append(
                (
                    plan,
                    next(
                        step for step in plan["steps"] if step["id"] == row["step_id"]
                    ),
                )
            )
        return busy

    def _contract(self, step, saved, role):
        contract = dict(step["contract"])
        contract["context"] += (
            "\n\nTeam role (advisory; grants no permissions): " + _json(role)
        )
        evidence = []
        for identifier in step["depends_on"]:
            predecessor = saved[identifier]
            result = json.loads(predecessor["result_json"])
            summary = (result.get("summary") or result["report"].get("summary", ""))[
                :200
            ]
            # Bound JSON-escaped control characters as well as ordinary text.
            while len(_json(summary)) > 250:
                summary = summary[: len(summary) // 2]
            report = result.get("report") or {}
            outcome_artifact_id = self._outcome_artifact_id(predecessor["task_id"])
            if outcome_artifact_id is None:
                raise ValueError("Predecessor structured outcome is not recoverable")
            evidence.append(
                {
                    "step_id": identifier,
                    "task_id": predecessor["task_id"],
                    "status": predecessor["status"],
                    "native_status": result["status"],
                    "outcome": result["outcome"],
                    "summary": summary,
                    "answer_artifact_id": result.get("answer_artifact_id"),
                    "outcome_artifact_id": outcome_artifact_id,
                    "limits": {
                        "failed_checks": sum(
                            check.get("result") == "failed"
                            for check in report.get("checks", [])
                        ),
                        "unrun_checks": sum(
                            check.get("result") == "not_run"
                            for check in report.get("checks", [])
                        ),
                        "blockers": len(report.get("blockers", [])),
                    },
                    "reader": "tandem_read_artifact(artifact_id=outcome_artifact_id)",
                }
            )
        if evidence:
            contract["context"] += (
                "\n\nTeam predecessor evidence (live workspace; worker-reported, not independent verification). Read full structured predecessor outcome artifacts, including checks, blockers and source evidence, before integration; answer_artifact_id recovers answer text:\n"
                + _json(evidence)
            )
        return TaskContract.model_validate(contract).model_dump(mode="json"), evidence

    def _dispatch(self, run, plan, step, saved, selection):
        assignment = selection["assignments"][step["id"]]
        contract, evidence = self._contract(
            step, saved, step["role"] or assignment.get("role") or step["mode"]
        )
        task_id = str(uuid4())
        execution = _execution(plan, step)
        execution["model"] = assignment["model"]
        execution["thinking"] = assignment["thinking"]
        timeout = min(
            max(1, math.ceil(self._remaining(run))),
            resolve_execution(execution)["effective"]["timeout_seconds"],
        )
        self._step_update(
            run["run_id"],
            step["id"],
            status="reserved",
            task_id=task_id,
            dependency_json=_json(evidence),
            error=None,
        )
        try:
            result = self.start_task(
                contract=contract,
                mode=step["mode"],
                cwd=plan["cwd"],
                execution=execution,
                project_context_id=plan["project_context_id"],
                timeout_seconds=timeout,
                reserved_task_id=task_id,
                team_run_id=run["run_id"],
            )
            if result["task_id"] != task_id:
                raise ValueError("Dispatch did not consume the reserved task ID")
        except TaskAdmissionBusy as exc:
            # The runtime guarantees this specific exception means no task was admitted.
            self._step_update(
                run["run_id"],
                step["id"],
                status="pending",
                task_id=None,
                error=str(exc),
            )
            return False
        except Exception as exc:
            logger.exception("Team dispatch failed; uncertain work will not replay")
            self._stop(
                self._get(run["run_id"]),
                "interrupted",
                f"Dispatch effects are uncertain; no replay: {exc}",
            )
            return False
        self._step_update(
            run["run_id"],
            step["id"],
            status=result["status"],
            result_json=_json(result),
        )
        return True

    def _refresh_steps(self, run):
        saved = {step["step_id"]: step for step in self._steps(run["run_id"])}
        for step in saved.values():
            if step["status"] not in _BUSY_STEPS:
                continue
            with closing(self.tasks.connect()) as db:
                exists = db.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (step["task_id"],)
                ).fetchone()
            if not exists:
                self._stop(
                    run,
                    "interrupted",
                    "Reserved task has no admitted record; it will not be replayed",
                )
                return None
            result = self.read_task(step["task_id"], details=True)
            status = result["status"]
            error = result.get("error")
            if status not in ACTIVE:
                outcome = result.get("outcome")
                if (
                    status == "completed"
                    and outcome in {"success", "partial"}
                    and (result.get("report") or {}).get("outcome") == outcome
                ):
                    status = "completed" if outcome == "success" else "partial"
                else:
                    status = "blocked" if outcome == "blocked" else "failed"
                    error = (
                        error
                        or "Worker did not deliver a completed structured successful outcome"
                    )
            self._step_update(
                run["run_id"],
                step["step_id"],
                status=status,
                result_json=_json(result),
                error=error,
            )
            step.update(status=status, result_json=_json(result), error=error)
        return saved

    def _block_dependencies(self, run, plan, saved):
        # Propagate failure even when the submitted list is not topological.
        changed = True
        while changed:
            changed = False
            for step in plan["steps"]:
                state = saved[step["id"]]
                failed = [
                    key
                    for key in step["depends_on"]
                    if saved[key]["status"] not in _BUSY_STEPS | {"pending"}
                    and not self._dependency_ready(step, saved[key])
                ]
                if state["status"] == "pending" and failed:
                    error = "Unsuccessful dependencies: " + ", ".join(failed)
                    self._step_update(
                        run["run_id"], step["id"], status="blocked", error=error
                    )
                    state.update(status="blocked", error=error)
                    changed = True

    def _advance(self, run):
        if run["status"] == "cancelling":
            self._flush_stop(run)
            return
        if self._remaining(run) <= 0:
            self._stop(
                run, "failed", "Team deadline expired; pending steps will not launch"
            )
            return
        plan = json.loads(run["plan_json"])
        selection = json.loads(run["selection_json"])
        saved = self._refresh_steps(run)
        if saved is None:
            return
        if self._remaining(run) <= 0:
            self._stop(run, "failed", "Team deadline expired")
            return
        self._block_dependencies(run, plan, saved)
        if all(
            step["status"] not in _BUSY_STEPS | {"pending"} for step in saved.values()
        ):
            final_id = TeamPlan.model_validate(plan).integration_step
            final = next(step for step in plan["steps"] if step["id"] == final_id)
            complete = all(
                self._dependency_ready({"dependency_policy": "success"}, step)
                for step in saved.values()
            )
            synthesis = (
                final.get("dependency_policy") == "reported_evidence"
                and self._dependency_ready(
                    {"dependency_policy": "success"}, saved[final_id]
                )
                and all(self._dependency_ready(final, step) for step in saved.values())
            )
            success = complete or synthesis
            self._update(
                run["run_id"],
                status="completed" if success else "failed",
                outcome=("success" if complete else "partial")
                if success
                else "blocked",
                error=None
                if success
                else "One or more workers failed or reported incomplete work; dependent steps were blocked",
            )
            return
        ready = [
            step
            for step in plan["steps"]
            if saved[step["id"]]["status"] == "pending"
            and step["id"] in selection.get("assignments", {})
            and all(
                self._dependency_ready(step, saved[key]) for key in step["depends_on"]
            )
        ]
        active_count = sum(step["status"] in _BUSY_STEPS for step in saved.values())
        status = (
            "waiting_input"
            if any(step["status"] == "waiting_input" for step in saved.values())
            else "running"
            if active_count or ready
            else "waiting_models"
        )
        # Native admission accepts running/waiting_input, not quiescent model waits.
        self._update(run["run_id"], status=status)
        busy = self._busy()
        for step in ready:
            if active_count >= min(plan["max_parallel"], WorkerSlots.LIMITS["work"]):
                self._step_update(
                    run["run_id"],
                    step["id"],
                    error="Ordinary team parallel capacity is busy; the fourth slot is reserved for think-only coordination.",
                )
                continue
            if any(
                self._conflicts(plan, step, other_plan, other)
                for other_plan, other in busy
            ):
                self._step_update(
                    run["run_id"],
                    step["id"],
                    error="Declared workspace ownership conflicts with an active team step.",
                )
                continue
            if self._cancelled(run["run_id"]):
                return
            if self.slots.capacity()["work"]["available"] == 0:
                self._step_update(
                    run["run_id"],
                    step["id"],
                    error="Shared ordinary worker capacity is busy; waiting for owned lifecycle termination.",
                )
                continue
            if self._dispatch(run, plan, step, saved, selection):
                active_count += 1
                busy.append((plan, step))
            elif self._get(run["run_id"])["status"] not in ACTIVE_TEAM:
                return

    def _drive(self):
        try:
            while not self.stopping.is_set():
                with closing(self.tasks.connect()) as db:
                    runs = [
                        dict(row)
                        for row in db.execute(
                            f"SELECT * FROM team_runs WHERE owner=? AND status IN {_ACTIVE_SQL} ORDER BY created",
                            (self.owner,),
                        )
                    ]
                for run in runs:
                    try:
                        if self.stopping.is_set():
                            break
                        if (
                            run["selection_json"] is None
                            and run["status"] != "cancelling"
                        ):
                            self._select(run)
                        with self.guard:
                            current = self._get(run["run_id"])
                            if (
                                not self.closing
                                and current["owner"] == self.owner
                                and current["status"] in ACTIVE_TEAM
                            ):
                                self._advance(current)
                    except Exception as exc:
                        logger.exception("Team controller transition failed")
                        with self.guard:
                            self._stop(
                                self._get(run["run_id"]),
                                "interrupted",
                                f"Controller failed; no work will replay: {exc}",
                            )
                self.wake.wait(0.05 if runs else None)
                self.wake.clear()
        except BaseException:
            logger.exception("Team controller exited unexpectedly")
            self.stopping.set()
            with self.guard:
                self.closing = True
                with closing(self.tasks.connect()) as db:
                    rows = db.execute(
                        f"SELECT * FROM team_runs WHERE owner=? AND status IN {_ACTIVE_SQL}",
                        (self.owner,),
                    ).fetchall()
                for row in rows:
                    with suppress(Exception):
                        self._stop(
                            dict(row),
                            "interrupted",
                            "Team controller exited; work will not replay",
                        )
            try:
                self._drain_stops()
            finally:
                self.lease.close()

    def close(self):
        with self.close_guard:
            self.stopping.set()
            self.wake.set()
            with self.guard:
                self.closing = True
                with closing(self.tasks.connect()) as db:
                    runs = [
                        dict(row)
                        for row in db.execute(
                            f"SELECT * FROM team_runs WHERE owner=? AND status IN {_ACTIVE_SQL}",
                            (self.owner,),
                        )
                    ]
                for run in runs:
                    self._stop(
                        run,
                        "interrupted",
                        "Owning MCP server shut down; work will not replay",
                    )
                self.wake.set()
            try:
                if (
                    self.driver is not None
                    and self.driver is not threading.current_thread()
                ):
                    self.driver.join()
                self._drain_stops()
            finally:
                self.lease.close()
