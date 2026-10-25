"""Scoped, read-only discovery of persisted work; never lease recovery or replay."""

from __future__ import annotations

import base64
import json
import time

from .runtime_models import ACTIVE
from .team_runs import ACTIVE_TEAM
from .work_items import independent_stage


class RecoveryOverview:
    def __init__(self, bridge):
        self.bridge = bridge

    def get(self, limit=20, cursor=None):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Overview limit must be between 1 and 100")
        bridge = self.bridge
        actor = bridge.work_participant
        if bridge.work_token or actor not in {"claude", "omp"}:
            raise ValueError("Overview is available only to an unbound coordinator")
        # A coordinator temporarily holding an independent seat must not use this
        # discovery surface to bypass the seat's ordinary reader redactions.
        for token in bridge._work_claims.values():
            try:
                held = bridge.work_items.authenticate(token)
            except ValueError:
                # A completed/expired remembered claim is not a live review seat.
                continue
            if independent_stage(held):
                raise ValueError("Overview is unavailable during independent review")
        after = ("", "")
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                if value["scope"] != bridge.scope.key or value["actor"] != actor:
                    raise ValueError
                after = tuple(value["after"])
                if len(after) != 2 or not all(
                    isinstance(item, str) and len(item) <= 100 for item in after
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError(
                    "Invalid overview cursor; restart tandem_overview without cursor"
                ) from None
        now = time.time()
        # This connection verifies the launch-project identity, is mode=ro, and
        # keeps one SQLite read transaction. Do not call controller views: those
        # legitimately recover leases, expire questions or update observations.
        with bridge.work_items._read_connection() as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            queries = []
            if "tasks" in tables:
                queries.append(
                    "SELECT 'task' kind,task_id id,status,updated,review_id evidence_id,review_stage stage,NULL revision FROM tasks"
                )
            if "team_runs" in tables:
                queries.append(
                    "SELECT 'team' kind,run_id id,status,updated,NULL evidence_id,NULL stage,NULL revision FROM team_runs"
                )
            if "review_runs" in tables:
                queries.append(
                    "SELECT 'review_run' kind,run_id id,status,updated,review_id evidence_id,phase stage,NULL revision FROM review_runs"
                )
            if "reviews" in tables:
                # Snapshots can exist without a controller run (or after capture
                # succeeded but the caller lost its response).
                queries.append(
                    "SELECT 'review' kind,review_id id,'saved' status,created updated,review_id evidence_id,NULL stage,NULL revision FROM reviews WHERE scope_id="
                    + "?"
                )
            if "work_cards" in tables:
                queries.append(
                    "SELECT 'work' kind,work_id id,status,updated_at updated,NULL evidence_id,NULL stage,revision FROM work_cards"
                )
            parameters = ([bridge.scope.key] if "reviews" in tables else []) + [
                *after,
                limit + 1,
            ]
            rows = (
                db.execute(
                    "SELECT * FROM ("
                    + " UNION ALL ".join(queries)
                    + ") WHERE (kind,id)>(?,?) ORDER BY kind,id LIMIT ?",
                    parameters,
                ).fetchall()
                if queries
                else []
            )
            items, used = [], 0
            for row in rows[:limit]:
                item = self._item(db, dict(row), now)
                size = len(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()
                )
                if items and used + size > 12000:
                    break
                items.append(item)
                used += size
        more = len(rows) > len(items)
        next_cursor = None
        if more and items:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "scope": bridge.scope.key,
                        "actor": actor,
                        "after": [last["kind"], last["id"]],
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode()
        return {
            "scope_id": bridge.scope.key,
            "participant": actor,
            "items": items,
            "next_cursor": next_cursor,
            "next_request": {
                "tool": "tandem_overview",
                "arguments": {"limit": limit, "cursor": next_cursor},
            }
            if next_cursor
            else None,
            "read_only": True,
            "observation": "Persisted state only; owners and claims were not probed, acquired, expired or recovered. Active records may have lost their owner.",
            "pagination": "Live keyset ordered by kind/id; restart without cursor to discover insertions before this page.",
            "replay_authorized": False,
        }

    @staticmethod
    def _item(db, row, now):
        kind, identifier, status = row["kind"], row["id"], row["status"]
        active = (
            status in ACTIVE_TEAM
            if kind == "team"
            else status in ACTIVE
            if kind in {"task", "review_run"}
            else status in {"draft", "active"}
        )
        recovery = status in {"interrupted", "recovery_required", "failed", "paused"}
        if kind == "task":
            read = {
                "tool": "tandem_result",
                "arguments": {"task_id": identifier, "wait_seconds": 0},
            }
            evidence = {
                "tool": "tandem_read_result",
                "arguments": {
                    "target": "task",
                    "identifier": identifier,
                    "section": "result",
                },
            }
        elif kind == "team":
            read = {
                "tool": "tandem_team",
                "arguments": {
                    "action": "status",
                    "run_id": identifier,
                    "wait_seconds": 0,
                },
            }
            evidence = {
                "tool": "tandem_team",
                "arguments": {"action": "read", "run_id": identifier, "section": "run"},
            }
        elif kind == "review_run":
            read = {
                "tool": "tandem_review_run",
                "arguments": {
                    "action": "status",
                    "run_id": identifier,
                    "wait_seconds": 0,
                },
            }
            evidence = {
                "tool": "tandem_read_result",
                "arguments": {
                    "target": "review_run",
                    "identifier": identifier,
                    "section": "result",
                },
            }
        elif kind == "review":
            read = {
                "tool": "tandem_review",
                "arguments": {
                    "action": "read",
                    "review_id": identifier,
                    "section": "manifest",
                },
            }
            evidence = read
        else:
            read = {
                "tool": "tandem_work",
                "arguments": {"request": {"action": "get", "work_id": identifier}},
            }
            evidence = {
                "tool": "tandem_work",
                "arguments": {"request": {"action": "history", "work_id": identifier}},
            }
            recovery = (
                recovery
                or db.execute(
                    "SELECT 1 FROM work_attempts WHERE work_id=? AND state='recovery_required' LIMIT 1",
                    (identifier,),
                ).fetchone()
                is not None
            )
        item = {
            "kind": kind,
            "id": identifier,
            "status": status,
            "category": "operator_required_recovery"
            if recovery
            else "active_work"
            if active
            else "ready_result",
            "next_observation": read,
            "saved_evidence": [evidence],
        }
        if row["revision"] is not None:
            item["revision"] = row["revision"]
        if kind in {"task", "review_run"} and row["evidence_id"]:
            item["saved_evidence"].append(
                {
                    "tool": "tandem_review",
                    "arguments": {
                        "action": "read",
                        "review_id": row["evidence_id"],
                        "section": "manifest",
                    },
                }
            )
        if kind == "task":
            question = db.execute(
                "SELECT question_id,deadline FROM questions WHERE task_id=? AND state='pending' AND deadline>? AND EXISTS (SELECT 1 FROM tasks WHERE task_id=? AND status='waiting_input')",
                (identifier, now, identifier),
            ).fetchone()
            if question:
                item["category"] = "pending_question"
                item["question"] = dict(question)
                # Do not project arbitrary author text or offer a generic reply
                # into an independent stage. The authoritative reader supplies
                # its exact stage-specific clarification/decision protocol.
                item["question"]["stage"] = row["stage"]
        if recovery:
            item["recovery"] = {
                "required": "Inspect saved evidence and current gates before any explicit repair; uncertain effects must not be replayed.",
                "mutation_authorized": False,
            }
        return item
