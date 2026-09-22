"""Private supervisor verification ledger; never a participant evidence channel."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from uuid import UUID, uuid4

from .models import CheckRun, VerificationPlan, check_revision
from .verification import verification_requirements

POLICY = "supervisor_checks_v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def declaration_revision(spec):
    return hashlib.sha256(
        _json(
            {
                "requirements": spec.get("review_requirements") or {},
                "verification": spec.get("review_verification"),
            }
        ).encode()
    ).hexdigest()


def valid_container(container):
    """Validate resolved identities only; the operator boundary resolves Docker."""
    if not isinstance(container, dict) or set(container) != {
        "executor",
        "socket",
        "daemon_id",
        "api_version",
        "image_id",
        "network_mode",
        "platform",
    }:
        return False
    if any(not isinstance(value, str) for value in container.values()):
        return False
    socket = container["socket"]
    daemon = container["daemon_id"]
    return bool(
        container["executor"] == "docker"
        and container["platform"] == "linux"
        and socket.startswith("/")
        and not socket.startswith("//")
        and socket != "/"
        and len(socket) <= 4096
        and all(character.isprintable() for character in socket)
        and os.path.normpath(socket) == socket
        and daemon.strip() == daemon
        and 0 < len(daemon) <= 256
        and all(character.isprintable() for character in daemon)
        and re.fullmatch(r"1\.[0-9]{1,3}", container["api_version"])
        and re.fullmatch(r"sha256:[0-9a-f]{64}", container["image_id"])
        and (
            container["network_mode"] == "none"
            or re.fullmatch(r"[0-9a-f]{64}", container["network_mode"])
        )
    )


def has_review_runner(attempt: dict) -> bool:
    policy = attempt.get("review_check_policy") or {}
    return bool(
        attempt.get("autonomous") is True
        and attempt.get("kind") == "review"
        and attempt.get("protocol") == "independent_first"
        and policy.get("policy") == POLICY
        and policy.get("version") == 1
        and valid_container(policy.get("container"))
    )


def authorization_policy(enabled, timeout, names, container=None):
    if (
        type(enabled) is not bool
        or type(timeout) is not int
        or not 1 <= timeout <= 7200
    ):
        raise ValueError(
            "Review checks require explicit boolean permission and timeout 1..7200"
        )
    names = [] if names is None else names
    if (
        not isinstance(names, list)
        or len(names) > 100
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            for name in names
        )
        or len(set(names)) != len(names)
    ):
        raise ValueError(
            "Review check environment must contain unique exact variable names"
        )
    if not enabled:
        if names:
            raise ValueError("Review check environment requires --allow-review-checks")
        if container is not None:
            raise ValueError("Review check container requires --allow-review-checks")
        return None
    if not valid_container(container):
        raise ValueError(
            "Review checks require a resolved local Docker container identity"
        )
    hashes = {}
    for name in names:
        if name not in os.environ:
            raise ValueError(f"Review check environment variable is missing: {name}")
        hashes[name] = hashlib.sha256(os.environ[name].encode()).hexdigest()
    return {
        "policy": POLICY,
        "version": 1,
        "timeout_seconds": timeout,
        "environment_hashes": hashes,
        "container": dict(container),
    }


def selected_policy(card, spec):
    grant = card.get("authorization") or {}
    policy = grant.get("review_check_policy") or {}
    if policy.get("policy") != POLICY or policy.get("version") != 1:
        return None
    if not valid_container(policy.get("container")):
        raise ValueError("Review check authorization has no valid container identity")
    if policy.get("plan_revision") != card["plan_revision"] or (
        policy.get("declarations", {}).get(spec["id"]) != declaration_revision(spec)
    ):
        raise ValueError("Review check authorization declaration binding changed")
    checks = verification_requirements(
        spec.get("review_verification") or VerificationPlan()
    )["checks"]
    if not checks or any(not (check.get("command") or "").strip() for check in checks):
        raise ValueError("Review checks require a complete executable selected ladder")
    return policy


def public_policy(policy):
    public = {
        key: value
        for key, value in policy.items()
        if key
        not in {
            "environment_hashes",
            "declarations",
            "declaration_revision",
            "container",
        }
    }
    if isinstance(policy.get("container"), dict):
        public["container"] = {
            key: value for key, value in policy["container"].items() if key != "socket"
        }
    return public


class ReviewCheckState:
    """WorkStore-owned methods using its transaction, authentication and CAS model."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _initialize_review_checks(db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS work_review_checks (run_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, check_id TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(attempt_id,check_id))"
        )

    def _review_binding(self, db, attempt):
        if not has_review_runner(attempt):
            raise ValueError("Attempt has no supervisor review check authorization")
        card = self.store._load(db, attempt["work_id"])
        step = self.store._step(card, attempt["step_id"])
        spec = next(item for item in card["plan"]["steps"] if item["id"] == step["id"])
        policy = selected_policy(card, spec)
        expected = {
            **(policy or {}),
            "commit": (attempt.get("submission") or {}).get("commit"),
            "declaration_revision": declaration_revision(spec),
        }
        submission = step.get("submission") or step.get("checkpoint") or {}
        grant = card.get("authorization") or {}
        if (
            not policy
            or expected != attempt["review_check_policy"]
            or attempt["authorization_id"] != grant.get("authorization_id")
            or grant.get("revoked_at") is not None
            or grant.get("deadline", 0) <= time.time()
            or attempt["plan_revision"] != card["plan_revision"]
            or attempt.get("verification") != spec.get("review_verification")
            or submission.get("submission_id")
            != (attempt.get("submission") or {}).get("submission_id")
            or submission.get("commit") != expected["commit"]
            or not re.fullmatch(r"[0-9a-f]{40,64}", expected["commit"] or "")
        ):
            raise ValueError(
                "Review check authorization or immutable submission binding changed"
            )
        checks = verification_requirements(spec["review_verification"])["checks"]
        return (
            {
                "attempt": attempt,
                "checks": checks,
                "policy": expected,
                "commit": expected["commit"],
            },
            card,
            step,
        )

    def _review_check_context(self, db, attempt):
        if not has_review_runner(attempt):
            return None
        context, card, step = self._review_binding(db, attempt)
        if (
            attempt["state"] not in {"reserved", "running"}
            or attempt["deadline"] <= time.time()
            or card["status"] != "active"
            or self.store._unresolved(card, step)
            or attempt.get("block_intent")
            or attempt.get("verdict")
            or attempt.get("clarification_request")
            or (attempt.get("independent_report") or {}).get("outcome") != "success"
        ):
            return None
        rows = self._review_rows(db, attempt["attempt_id"])
        if any(
            row.get("input_unchanged") is False
            or (row["state"] == "finished" and not row.get("process_confirmed_gone"))
            for row in rows
        ):
            return None
        return context

    def review_check_context(self, attempt_id):
        with self.store._read_connection() as db:
            return self._review_check_context(db, self.store._attempt(db, attempt_id))

    @staticmethod
    def _review_rows(db, attempt_id):
        return [
            json.loads(row["record"])
            for row in db.execute(
                "SELECT record FROM work_review_checks WHERE attempt_id=? ORDER BY rowid",
                (attempt_id,),
            )
        ]

    @staticmethod
    def _review_run(db, run_id):
        row = db.execute(
            "SELECT record FROM work_review_checks WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown review check run")
        return json.loads(row["record"])

    def _save_review_run(self, db, row, event):
        db.execute(
            "UPDATE work_review_checks SET record=? WHERE run_id=?",
            (_json(row), row["run_id"]),
        )
        attempt = self.store._attempt(db, row["attempt_id"])
        self.store._record(
            db,
            self.store._load(db, attempt["work_id"]),
            event,
            "supervisor",
            {"attempt_id": attempt["attempt_id"], "check_state": row["state"]},
        )

    def reserve_review_check(self, attempt_id, check_id):
        with self.store._transaction() as db:
            attempt = self.store._attempt(db, attempt_id)
            context = self._review_check_context(db, attempt)
            if context is None:
                raise ValueError("Review checks are not eligible in this stage")
            check = next(
                (item for item in context["checks"] if item["id"] == check_id), None
            )
            if check is None:
                raise ValueError("Check is not selected by the authorized declaration")
            rows = self._review_rows(db, attempt_id)
            for row in rows:
                if row["check_id"] == check_id:
                    self._validate_review_row(row, context)
                    return {**row, "replayed": True}
            if any(row["state"] != "finished" for row in rows):
                raise ValueError(
                    "An existing reserved or running check requires reconciliation, never replay"
                )
            row = {
                "run_id": str(uuid4()),
                "attempt_id": attempt_id,
                "check_id": check_id,
                "state": "reserved",
                "check": check,
                "check_revision": check_revision(check),
                "commit": context["commit"],
                "policy": context["policy"],
                "reserved_at": time.time(),
                "started_at": None,
            }
            db.execute(
                "INSERT INTO work_review_checks VALUES (?,?,?,?)",
                (row["run_id"], attempt_id, check_id, _json(row)),
            )
            self._save_review_run(db, row, "review_check_reserved")
            return {**row, "replayed": False}

    @staticmethod
    def _validate_review_row(row, context):
        check = next(
            (item for item in context["checks"] if item["id"] == row["check_id"]), None
        )
        if (
            check != row["check"]
            or check_revision(check) != row["check_revision"]
            or row["commit"] != context["commit"]
            or row["policy"] != context["policy"]
        ):
            raise ValueError("Trusted review check identity no longer matches")

    def start_review_check(self, run_id, *, pid=None, execution):
        if (pid is not None and (type(pid) is not int or pid <= 0)) or not isinstance(
            execution, dict
        ):
            raise ValueError("Review check start requires actual container identity")
        with self.store._transaction() as db:
            row = self._review_run(db, run_id)
            context = self._review_check_context(
                db, self.store._attempt(db, row["attempt_id"])
            )
            if context is None:
                raise ValueError("Review check authorization is no longer executable")
            self._validate_review_row(row, context)
            container = row["policy"]["container"]
            if (
                set(execution)
                != {
                    "executor",
                    "container_id",
                    "container_name",
                    "image_id",
                    "daemon_id",
                    "cwd",
                    "commit",
                    "tree_hash",
                    "environment_hashes",
                    "timeout_seconds",
                    "os_sandbox",
                    "process_boundary",
                }
                or execution.get("executor") != "docker"
                or not isinstance(execution.get("container_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", execution["container_id"])
                or execution.get("container_name") != "omp-tandem-check-" + run_id
                or execution.get("image_id") != container["image_id"]
                or execution.get("daemon_id") != container["daemon_id"]
                or execution.get("cwd") != "/workspace"
                or execution.get("commit") != row["commit"]
                or not isinstance(execution.get("tree_hash"), str)
                or not re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})", execution["tree_hash"]
                )
                or execution["tree_hash"]
                != context["attempt"]["submission"].get("tree_hash")
                or execution.get("environment_hashes")
                != row["policy"]["environment_hashes"]
                or type(execution.get("timeout_seconds")) is not int
                or execution["timeout_seconds"] != row["policy"]["timeout_seconds"]
                or execution.get("os_sandbox") is not False
                or execution.get("process_boundary") != "docker_pid_namespace"
            ):
                raise ValueError(
                    "Review check execution must match approved commit, environment and container identity"
                )
            if row["state"] != "reserved":
                if (
                    row["state"] == "running"
                    and row["pid"] == pid
                    and row["execution"] == execution
                ):
                    return row
                raise ValueError("Review check container identity is immutable")
            row.update(
                state="running", pid=pid, execution=execution, started_at=time.time()
            )
            self._save_review_run(db, row, "review_check_started")
            return row

    def finish_review_check(
        self,
        run_id,
        *,
        result,
        exit_code,
        error,
        output,
        execution,
        process_confirmed_gone,
        input_unchanged,
    ):
        if result not in {"passed", "failed", "interrupted", "not_run"}:
            raise ValueError("Invalid review check result")
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("Review check exit code must be an integer or unknown")
        if (
            type(process_confirmed_gone) is not bool
            or type(input_unchanged) is not bool
        ):
            raise ValueError(
                "Review check cleanup and input observations must be explicit"
            )
        if (
            not isinstance(output, dict)
            or output.get("id") != run_id
            or not re.fullmatch(r"[0-9a-f]{64}", output.get("sha256", ""))
            or type(output.get("bytes")) is not int
            or not 0 <= output["bytes"] <= 1048576
            or type(output.get("truncated")) is not bool
        ):
            raise ValueError("Review check requires bounded private output metadata")
        values = dict(
            result=result,
            exit_code=exit_code,
            error=error,
            output=output,
            execution=execution,
            process_confirmed_gone=process_confirmed_gone,
            input_unchanged=input_unchanged,
        )
        fingerprint = hashlib.sha256(_json(values).encode()).hexdigest()
        with self.store._transaction() as db:
            row = self._review_run(db, run_id)
            if row.get("finish_fingerprint"):
                if row["finish_fingerprint"] != fingerprint:
                    raise ValueError("Review check completion is immutable")
                return row
            if result == "passed" and (
                row["state"] != "running"
                or row.get("started_at") is None
                or not valid_container(row["policy"].get("container"))
                or (row.get("execution") or {}).get("executor") != "docker"
                or exit_code != 0
                or not process_confirmed_gone
                or not input_unchanged
            ):
                raise ValueError(
                    "Passing check requires observed exit zero, unchanged input and confirmed teardown"
                )
            if row.get("execution") is not None and row["execution"] != execution:
                raise ValueError("Review check execution identity changed")
            ended = time.time()
            check = row["check"]
            run = CheckRun(
                check_id=row["check_id"],
                run_id=run_id,
                criterion=check["criterion"],
                role="reviewer",
                command=check["command"],
                started_at=row["started_at"],
                ended_at=ended,
                scope={
                    "kind": "commit",
                    "digest": row["commit"],
                    "boundaries": [
                        "Standalone submitted-commit copy; parent history and external services are not pinned",
                        "Input integrity observed before and after execution; process cleanup requires owned container removal",
                        "Linux Docker PID namespace; external effects are not rolled back; not universal adversarial isolation",
                    ],
                },
                result=result if result in {"passed", "failed"} else "not_run",
                provenance="machine_observed",
                check_revision=row["check_revision"],
                acceptance_refs=check.get("acceptance_refs") or [],
                environment=(
                    {
                        "platform": row["policy"]["container"]["platform"],
                        "image_id": row["execution"]["image_id"],
                    }
                    if row.get("started_at") is not None
                    and valid_container(row["policy"].get("container"))
                    and (row.get("execution") or {}).get("executor") == "docker"
                    else {}
                ),
                note=(error or "")[:4000],
            ).model_dump()
            row.update(
                **values,
                state="finished",
                ended_at=ended,
                check_run=run,
                finish_fingerprint=fingerprint,
            )
            self._save_review_run(db, row, "review_check_finished")
            return row

    def _review_assessment(self, db, attempt, *, current=True):
        if current:
            context, _, _ = self._review_binding(db, attempt)
        else:
            policy = attempt.get("review_check_policy") or {}
            if not (
                attempt.get("autonomous") is True
                and attempt.get("kind") == "review"
                and attempt.get("protocol") == "independent_first"
                and policy.get("policy") == POLICY
                and policy.get("version") == 1
            ):
                raise ValueError("Attempt has no recorded supervisor checks")
            context = {
                "attempt": attempt,
                "checks": verification_requirements(attempt["verification"])["checks"],
                "policy": attempt["review_check_policy"],
                "commit": attempt["submission"]["commit"],
            }
        rows = self._review_rows(db, attempt["attempt_id"])
        for row in rows:
            self._validate_review_row(row, context)
        if any(row["state"] == "running" for row in rows):
            status, settled = "running", False
        elif any(
            row["state"] == "reserved"
            or not row.get("process_confirmed_gone")
            or not row.get("input_unchanged")
            or row.get("result") in {"interrupted", "not_run"}
            for row in rows
        ):
            status, settled = "uncertain", False
        elif len(rows) < len(context["checks"]):
            status, settled = "not_run", False
        elif all(row["result"] == "passed" for row in rows):
            status, settled = "passed", True
        else:
            status, settled = "failed", True
        return {
            "status": status,
            "settled": settled,
            "checks": context["checks"],
            "runs": [row["check_run"] for row in rows if row.get("check_run")],
        }

    def review_check_assessment(self, attempt_id, *, current=True):
        with self.store._read_connection() as db:
            return self._review_assessment(
                db, self.store._attempt(db, attempt_id), current=current
            )

    def trusted_verification_context(self, attempt_id):
        with self.store._read_connection() as db:
            attempt = self.store._attempt(db, attempt_id)
            if not has_review_runner(attempt):
                return None
            self.store._authenticate(db, attempt["token"])
            assessment = self._review_assessment(db, attempt)
            teardown_confirmed = all(
                row["state"] == "finished" and row.get("process_confirmed_gone") is True
                for row in self._review_rows(db, attempt_id)
            )
            return {
                "source": POLICY,
                "status": assessment["status"],
                "settled": assessment["settled"],
                "runs": assessment["runs"],
                "check_ids": [check["id"] for check in assessment["checks"]],
                "comparison_opened": bool(attempt.get("comparison_opened_at")),
                "teardown_confirmed": teardown_confirmed,
                "verdict": (attempt.get("verdict") or {}).get("verdict"),
            }

    def _require_review_checks(self, db, attempt, *, accept=False):
        if not attempt.get("review_check_policy"):
            return
        assessment = self._review_assessment(db, attempt)
        if not assessment["settled"]:
            raise ValueError("Trusted review checks are not settled")
        if accept and (
            assessment["status"] != "passed" or not attempt.get("comparison_opened_at")
        ):
            raise ValueError(
                "Acceptance requires comparison and all trusted review checks passed"
            )

    def require_finalization(self, db, attempt):
        if not attempt.get("review_check_policy"):
            return
        assessment = self._review_assessment(db, attempt)
        if (attempt.get("verdict") or {}).get("verdict") == "accept":
            self._require_review_checks(db, attempt, accept=True)
        elif any(
            row["state"] != "finished" or not row.get("process_confirmed_gone")
            for row in self._review_rows(db, attempt["attempt_id"])
        ):
            raise ValueError(
                "Review check execution must be stopped before rejection finishes"
            )
        return assessment

    def review_check_section(
        self,
        request,
        *,
        actor,
        attempt_token=None,
        origin=None,
        section="verification",
        cursor=None,
        limit=50,
    ):
        from .work_items import WorkCommand

        request = WorkCommand.model_validate(request)
        if request.action != "get" or actor not in {"claude", "omp", "operator"}:
            raise ValueError("Verification reader requires an authorized work get")
        if type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError("Verification reader limit must be 1..16000")
        with self.store._read_connection() as db:
            bound = (
                self.store._authenticate(db, attempt_token, actor)
                if attempt_token
                else None
            )
            work_id = request.work_id or (bound or {}).get("work_id")
            step_id = request.step_id or (bound or {}).get("step_id")
            if bound and (
                work_id != bound["work_id"]
                or step_id != bound["step_id"]
                or bound["kind"] != "review"
            ):
                raise ValueError("Verification reader is bound to the review step")
            card = self.store._load(db, work_id)
            requested_run = None
            if section.startswith("verification/"):
                run_id = section.partition("/")[2]
                if str(UUID(run_id)) != run_id:
                    raise ValueError("Invalid opaque verification run identity")
                requested_run = self._review_run(db, run_id)
                attempt = self.store._attempt(db, requested_run["attempt_id"])
                if (
                    attempt["work_id"] != work_id
                    or (step_id is not None and attempt["step_id"] != step_id)
                    or (
                        bound is not None
                        and attempt["attempt_id"] != bound["attempt_id"]
                    )
                ):
                    raise ValueError(
                        "Verification output is not available in this scope"
                    )
                step_id = attempt["step_id"]
            else:
                if step_id is None:
                    candidates = [step for step in card["steps"] if step.get("attempt")]
                    if len(candidates) != 1:
                        raise ValueError("Verification reader requires step_id")
                    step_id = candidates[0]["id"]
                step = self.store._step(card, step_id)
                attempt = bound or self.store._attempt(db, step["attempt"])
            # Reading immutable evidence does not renew execution authority.
            assessment = self._review_assessment(db, attempt, current=False)
            visible = bound is None or bool(bound.get("comparison_opened_at"))
            stage = bound.get("review_stage") if bound else "coordinator"
            binding = {
                "work_id": work_id,
                "attempt_id": attempt["attempt_id"],
                "revision": card["revision"],
                "actor": actor,
                "bound_attempt": (bound or {}).get("attempt_id"),
                "stage": stage,
                "section": section,
            }
            after = 0
            if cursor:
                try:
                    page = json.loads(cursor)
                    if (
                        page["binding"] != binding
                        or type(page["after"]) is not int
                        or page["after"] < 0
                    ):
                        raise ValueError("Cursor binding differs")
                    after = page["after"]
                except (ValueError, TypeError, KeyError):
                    return {"error": {"code": "cursor_stale"}, "work_id": work_id}
            base = {
                "work_id": work_id,
                "step_id": step_id,
                "revision": card["revision"],
                "section": section,
                "stage": stage,
            }
            if section == "verification":
                rows = self._review_rows(db, attempt["attempt_id"])
                metadata = (
                    [
                        {
                            "run_id": row["run_id"],
                            "check_id": row["check_id"],
                            "state": row["state"],
                            "result": row.get("result"),
                            "exit_code": row.get("exit_code"),
                            "input_unchanged": row.get("input_unchanged"),
                            "section": "verification/" + row["run_id"],
                        }
                        for row in rows
                    ]
                    if visible
                    else []
                )
                end = min(len(metadata), after + limit)
                return {
                    **base,
                    "status": assessment["status"],
                    "settled": assessment["settled"],
                    "output_visible": visible,
                    "runs": metadata[after:end],
                    "next_cursor": _json({"binding": binding, "after": end})
                    if end < len(metadata)
                    else None,
                }
            if not visible:
                raise ValueError(
                    "Verification output is hidden until comparison is opened"
                )
            prefix, _, run_id = section.partition("/")
            if prefix != "verification" or str(UUID(run_id)) != run_id:
                raise ValueError("Invalid opaque verification run identity")
            row = requested_run
            if row["attempt_id"] != attempt["attempt_id"] or row["state"] != "finished":
                raise ValueError("Verification output is not available in this scope")
            output = row["output"]
            # Open every directory with NOFOLLOW: final-file checks alone leave parent swaps exposed.
            flags = os.O_RDONLY | os.O_NOFOLLOW
            root_fd = os.open(self.store.scope.directory, flags | os.O_DIRECTORY)
            try:
                parent_fd = os.open(
                    "work-checks", flags | os.O_DIRECTORY, dir_fd=root_fd
                )
                try:
                    attempt_fd = os.open(
                        attempt["attempt_id"], flags | os.O_DIRECTORY, dir_fd=parent_fd
                    )
                    try:
                        fd = os.open(run_id + ".log", flags, dir_fd=attempt_fd)
                    finally:
                        os.close(attempt_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size != output["bytes"]
                    or info.st_size > 1048576
                ):
                    raise ValueError("Private verification output size changed")
                data = stream.read(1048577)
            if hashlib.sha256(data).hexdigest() != output["sha256"]:
                raise ValueError("Private verification output digest changed")
            text = data.decode("utf-8", errors="replace")
            if after > len(text):
                raise ValueError("Invalid verification output offset")
            chunk = text[after : after + min(limit, 4000)]
            end = after + len(chunk)
            return {
                **base,
                "run_id": run_id,
                "encoding": "text",
                "content": chunk,
                "offset": after,
                "total_characters": len(text),
                "output": output,
                "check": row["check"],
                "check_revision": row["check_revision"],
                "observation": {
                    key: row.get(key)
                    for key in (
                        "result",
                        "exit_code",
                        "error",
                        "started_at",
                        "ended_at",
                        "input_unchanged",
                        "process_confirmed_gone",
                    )
                },
                "execution": {
                    key: value
                    for key, value in row.get("execution", {}).items()
                    if key != "environment_hashes"
                },
                "environment_names": sorted(row["policy"]["environment_hashes"]),
                "scope": row["check_run"]["scope"],
                "provenance": "machine_observed",
                "next_cursor": _json({"binding": binding, "after": end})
                if end < len(text)
                else None,
            }
