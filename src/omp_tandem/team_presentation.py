"""Bounded team envelopes and digest-bound recovery from the scoped durable store."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from contextlib import closing
from uuid import UUID, uuid4

from .team_runs import ACTIVE_TEAM

MAX_RESPONSE_BYTES = 16384
MAX_READ_BYTES = 8192


def _bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _size(value):
    # ASCII escaping is an upper bound even for transports emitting literal UTF-8.
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _request(run_id, section, step_id=None):
    request = {
        "action": "read",
        "run_id": run_id,
        "section": section,
        "offset_bytes": 0,
        "max_bytes": MAX_READ_BYTES,
    }
    if step_id is not None:
        request["step_id"] = step_id
    return request


def _preview(value, limit):
    return value[:limit] if isinstance(value, str) else value


class TeamPresentation:
    def __init__(self, tasks, runs):
        self.tasks, self.runs = tasks, runs

    def _decorate(self, result, channel):
        # Logical step IDs are never native watchdog task IDs. Team polling and
        # semantic actions are not interchangeable with native task delivery.
        result.pop("pending", None)
        action = result.get("next_action")
        result = channel.decorate(result)
        if action is not None:
            result["next_action"] = action
        return result

    def present(self, raw, channel, *, details=False):
        run_id = raw["run_id"]
        readers = {
            section: _request(run_id, section)
            for section in ("run", "plan", "selection")
        }
        result = {
            key: raw.get(key)
            for key in (
                "run_id",
                "status",
                "outcome",
                "created",
                "deadline",
                "cancel_requested",
                "stop_status",
                "progress",
                "integration_step",
                "answer_artifact_id",
                "outcome_artifact_id",
                "pending_models",
                "next_action",
            )
        }
        result["readers"] = readers
        result["pending_models"] = raw.get("pending_models", [])
        result["incomplete_steps"] = raw.get("incomplete_steps", [])
        # Project the controller's action descriptors rather than full question bodies.
        actions = []
        step_ids = {
            step["task_id"]: step["id"]
            for step in raw.get("steps", [])
            if step.get("task_id")
        }
        for entry in raw.get("actions", []):
            if entry["action"] == "reply":
                actions.append(
                    {
                        "action": "reply",
                        "tool": "tandem_reply",
                        "task_id": entry["task_id"],
                        "question_id": entry["question_id"],
                        "deadline": entry.get("deadline"),
                        "reader": _request(run_id, "step", step_ids[entry["task_id"]]),
                    }
                )
        if result["pending_models"]:
            actions.append(
                {
                    "action": "assign",
                    "tool": "tandem_team",
                    "run_id": run_id,
                    "reader": readers["selection"],
                }
            )
        result["actions"] = actions
        if any(action["action"] == "reply" for action in actions):
            result["next_action"] = "reply"
        elif result["pending_models"]:
            result["next_action"] = "agree_models"
        if "wait" in raw:
            result["wait"] = raw["wait"]
        final = raw.get("final") or {}
        answer = raw.get("answer")
        result["answer"] = _preview(answer, 1200)
        result["answer_length"] = final.get(
            "answer_length", len(answer) if isinstance(answer, str) else 0
        )
        result["answer_truncated"] = bool(final.get("answer_truncated")) or result[
            "answer_length"
        ] > len(result["answer"] or "")
        result["answer_reader"] = _request(run_id, "step", raw["integration_step"])
        for key in ("request_key", "goal", "error"):
            result[key] = _preview(raw.get(key), 300)
        result["steps"] = []
        for step in raw.get("steps", []):
            item = {
                key: step.get(key)
                for key in (
                    "id",
                    "status",
                    "task_id",
                    "outcome",
                    "dependency_policy",
                    "selected_model",
                    "selected_thinking",
                    "answer_artifact_id",
                    "outcome_artifact_id",
                )
            }
            execution = step.get("execution") or {}
            item["execution"] = {"actual": execution.get("actual")}
            item["reader"] = _request(run_id, "step", step["id"])
            if details:
                item["execution"]["effective"] = {
                    key: value
                    for key, value in (execution.get("effective") or {}).items()
                    if key in {"model", "thinking", "profile"}
                }
                for key in ("title", "error", "summary"):
                    item[key] = _preview(step.get(key), 160)
            result["steps"].append(item)
        result["steps_total"] = len(result["steps"])
        result["details"] = details
        result = self._decorate(result, channel)
        if _size(result) > MAX_RESPONSE_BYTES:
            result["answer"] = _preview(result["answer"], 120)
            result["answer_truncated"] = result["answer_length"] > len(
                result["answer"] or ""
            )
            for key in ("request_key", "goal", "error"):
                result[key] = _preview(result[key], 60)
            for step in result["steps"]:
                for key in ("title", "error", "summary"):
                    step.pop(key, None)
        # Run recovery contains all step identities; each step has its own reader.
        while _size(result) > MAX_RESPONSE_BYTES and result["steps"]:
            result["steps"].pop()
        result["steps_truncated"] = len(result["steps"]) != result["steps_total"]
        # Reserve the truncation marker before enforcing the final wire bound.
        while _size(result) > MAX_RESPONSE_BYTES and result["steps"]:
            result["steps"].pop()
            result["steps_truncated"] = True
        if _size(result) > MAX_RESPONSE_BYTES:
            raise ValueError("Team delivery metadata exceeds the response budget")
        return result

    def _section(self, run_id, section, step_id):
        # Preserve lost-owner recovery, then read all selected data in one snapshot.
        self.runs.state(run_id)
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM team_runs WHERE run_id=?", (str(UUID(run_id)),)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown team run in this project")
            run = dict(row)
            if section == "plan":
                return json.loads(run["plan_json"])
            if section == "selection":
                return (
                    json.loads(run["selection_json"]) if run["selection_json"] else {}
                )
            if section == "run":
                value = {
                    key: value
                    for key, value in run.items()
                    if key not in ("plan_json", "selection_json")
                }
                value["readers"] = {
                    key: _request(run_id, key) for key in ("plan", "selection")
                }
                value["steps"] = [
                    {**dict(step), "reader": _request(run_id, "step", step["step_id"])}
                    for step in db.execute(
                        "SELECT step_id,task_id,status,error,ordinal FROM team_steps WHERE run_id=? ORDER BY ordinal",
                        (run_id,),
                    )
                ]
                return value
            plan = json.loads(run["plan_json"])
            saved = db.execute(
                "SELECT * FROM team_steps WHERE run_id=? AND step_id=?",
                (run_id, step_id),
            ).fetchone()
            if saved is None:
                raise ValueError("Unknown step in this team run")
            saved = dict(saved)
            value = {
                key: value
                for key, value in saved.items()
                if key not in ("result_json", "dependency_json")
            }
            value["definition"] = next(
                step for step in plan["steps"] if step["id"] == step_id
            )
            value["saved_result"] = (
                json.loads(saved["result_json"]) if saved["result_json"] else None
            )
            value["dependency_evidence"] = (
                json.loads(saved["dependency_json"]) if saved["dependency_json"] else []
            )
            value["current_question"] = None
            task = db.execute(
                "SELECT * FROM tasks WHERE task_id=? AND team_run_id=?",
                (saved["task_id"], run_id),
            ).fetchone()
            if task is not None:
                task = dict(task)
                value["native_result"] = {
                    key: task.get(key)
                    for key in (
                        "task_id",
                        "conversation_id",
                        "status",
                        "answer",
                        "error",
                        "actual_model",
                        "actual_thinking",
                    )
                }
                value["native_result"]["report"] = (
                    json.loads(task["report_json"]) if task["report_json"] else None
                )
                value["native_result"]["result_artifacts"] = json.loads(
                    task["result_artifacts"]
                )
                value["native_result"]["execution"] = (
                    json.loads(task["execution_json"])
                    if task["execution_json"]
                    else None
                )
                now = time.time()
                if (
                    run["status"] in {"running", "waiting_input", "waiting_models"}
                    and run["deadline"] > now
                    and not run["cancel_requested"]
                    and task["status"] == "waiting_input"
                    and not task["cancel_requested"]
                ):
                    question = db.execute(
                        "SELECT * FROM questions WHERE task_id=? AND state='pending' AND deadline>? ORDER BY created DESC LIMIT 1",
                        (saved["task_id"], now),
                    ).fetchone()
                    if question:
                        current = dict(question)
                        current["options"] = json.loads(current.pop("options_json"))
                        value["current_question"] = current
            return value

    def _page(
        self,
        data,
        request,
        channel,
        *,
        offset_bytes,
        max_bytes,
        expected_sha256=None,
        metadata=None,
        catalog_id=None,
    ):
        if type(offset_bytes) is not int or offset_bytes < 0:
            raise ValueError("offset_bytes must be a nonnegative integer")
        if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_READ_BYTES:
            raise ValueError(f"max_bytes must be between 4 and {MAX_READ_BYTES}")
        if offset_bytes and expected_sha256 is None:
            raise ValueError("expected_sha256 is required for nonzero offsets")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            result = self._decorate(
                {
                    "status": "digest_mismatch",
                    "sha256": digest,
                    "total_bytes": len(data),
                    "restart_request": request,
                    "next_action": "restart_read",
                },
                channel,
            )
            if _size(result) > MAX_RESPONSE_BYTES:
                raise ValueError("Team delivery metadata exceeds the response budget")
            return result
        if offset_bytes > len(data) or (
            offset_bytes < len(data) and data[offset_bytes] & 0xC0 == 0x80
        ):
            raise ValueError("offset_bytes must be a UTF-8 boundary within the section")
        result = self._decorate(
            {
                **(metadata or {}),
                "encoding": "utf-8",
                "sha256": digest,
                "total_bytes": len(data),
                "offset_bytes": offset_bytes,
                "returned_bytes": 0,
                "content": "",
                "next_offset_bytes": None,
                "next_request": None,
                "next_action": "read_complete",
            },
            channel,
        )
        end = min(len(data), offset_bytes + max_bytes)
        while True:
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            result["content"] = data[offset_bytes:end].decode("utf-8")
            result["returned_bytes"] = end - offset_bytes
            result["next_offset_bytes"] = end if end < len(data) else None
            result["next_request"] = (
                {
                    "action": "models",
                    "cursor": f"{catalog_id}:{end}:{digest}",
                    "max_bytes": max_bytes,
                }
                if catalog_id and end < len(data)
                else {
                    **request,
                    "offset_bytes": end,
                    "max_bytes": max_bytes,
                    "expected_sha256": digest,
                }
                if end < len(data)
                else None
            )
            if catalog_id:
                result["next_cursor"] = (
                    result["next_request"]["cursor"] if result["next_request"] else None
                )
            result["next_action"] = "read_more" if end < len(data) else "read_complete"
            if _size(result) <= MAX_RESPONSE_BYTES:
                return result
            if end <= offset_bytes + 4:
                raise ValueError("Team delivery metadata exceeds the response budget")
            end = offset_bytes + (end - offset_bytes) // 2

    def read(
        self,
        run_id,
        section,
        channel,
        *,
        step_id=None,
        offset_bytes=0,
        max_bytes=MAX_READ_BYTES,
        expected_sha256=None,
    ):
        if section not in {"run", "plan", "selection", "step"}:
            raise ValueError("section must be run, plan, selection or step")
        if (section == "step") != (step_id is not None):
            raise ValueError("step_id is required only for the step section")
        run_id = str(UUID(run_id))
        request = _request(run_id, section, step_id)
        return self._page(
            _bytes(self._section(run_id, section, step_id)),
            request,
            channel,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
            metadata={
                "run_id": run_id,
                "section": section,
                **({"step_id": step_id} if step_id else {}),
            },
        )

    def models(self, channel, *, cursor=None, max_bytes=MAX_READ_BYTES):
        with closing(self.tasks.connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS team_catalog_snapshots (snapshot_id TEXT PRIMARY KEY, content BLOB NOT NULL)"
            )
            if cursor is None:
                # TeamSelection already whitelists subscription identities and public facts.
                data = _bytes(self.runs.selection.catalog())
                snapshot_id = str(uuid4())
                db.execute(
                    "INSERT INTO team_catalog_snapshots VALUES (?,?)",
                    (snapshot_id, data),
                )
                offset, digest = 0, None
            else:
                try:
                    snapshot_id, position, digest = cursor.split(":")
                    snapshot_id, offset = str(UUID(snapshot_id)), int(position)
                except (ValueError, AttributeError):
                    return self._cursor_error("models", channel)
                row = db.execute(
                    "SELECT content FROM team_catalog_snapshots WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    return self._cursor_error("models", channel)
                data = row["content"]
        return self._page(
            data,
            {"action": "models"},
            channel,
            offset_bytes=offset,
            max_bytes=max_bytes,
            expected_sha256=digest,
            catalog_id=snapshot_id,
            metadata={"snapshot_id": snapshot_id, "provider": "openai-codex"},
        )

    def _cursor_error(self, action, channel):
        result = self._decorate(
            {
                "status": "invalid_cursor",
                "next_action": "restart_read",
                "restart_request": {"action": action},
            },
            channel,
        )
        if _size(result) > MAX_RESPONSE_BYTES:
            raise ValueError("Team delivery metadata exceeds the response budget")
        return result

    def recent(self, channel, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        anchor = None
        if cursor is not None:
            try:
                scope, created, identifier = json.loads(
                    base64.urlsafe_b64decode(cursor).decode("utf-8")
                )
                if (
                    scope != self.tasks.scope.key
                    or type(created) not in (int, float)
                    or not math.isfinite(created)
                ):
                    raise ValueError("foreign cursor")
                anchor = (created, str(UUID(identifier)))
            except (ValueError, TypeError, UnicodeError):
                return self._cursor_error("list", channel)
        with closing(self.tasks.connect()) as db:
            # Deliberately excludes plan_json, selection_json, errors and worker reports.
            rows = db.execute(
                "SELECT run_id,status,outcome,created,updated,deadline,cancel_requested,stop_status FROM team_runs "
                + ("WHERE (created,run_id)<(?,?) " if anchor else "")
                + "ORDER BY created DESC,run_id DESC LIMIT ?",
                (*anchor, limit + 1) if anchor else (limit + 1,),
            ).fetchall()
        # Recover only the selected metadata page; never load worker reports here.
        with closing(self.tasks.connect()) as db:
            for index, row in enumerate(rows[:limit]):
                if row["status"] in ACTIVE_TEAM:
                    self.runs.state(row["run_id"])
                    rows[index] = db.execute(
                        "SELECT run_id,status,outcome,created,updated,deadline,cancel_requested,stop_status "
                        "FROM team_runs WHERE run_id=?",
                        (row["run_id"],),
                    ).fetchone()
        entries = [
            {**dict(row), "reader": _request(row["run_id"], "run")} for row in rows
        ]
        result = self._decorate(
            {
                "runs": entries[:limit],
                "next_cursor": None,
                "next_request": None,
                "next_action": "list_complete",
            },
            channel,
        )
        while True:
            if len(result["runs"]) < len(entries):
                last = result["runs"][-1]
                token = base64.urlsafe_b64encode(
                    _bytes([self.tasks.scope.key, last["created"], last["run_id"]])
                ).decode("ascii")
                result["next_cursor"] = token
                result["next_request"] = {
                    "action": "list",
                    "cursor": token,
                    "limit": limit,
                }
                result["next_action"] = "read_more"
            if _size(result) <= MAX_RESPONSE_BYTES:
                return result
            if len(result["runs"]) <= 1:
                raise ValueError("Team delivery metadata exceeds the response budget")
            result["runs"].pop()
