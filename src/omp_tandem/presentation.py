"""Bounded public projections; complete domain values remain in their scoped stores."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from .errors import ProtocolError

MAX_RESPONSE_BYTES = 16384
MAX_READ_BYTES = 8192


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def wire_size(value):
    # Also bound transports that escape Unicode or use default JSON whitespace.
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def utf8_prefix(value, max_bytes):
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _request(target, identifier, section):
    return {
        "target": target,
        "identifier": identifier,
        "section": section,
        "offset_bytes": 0,
        "max_bytes": MAX_READ_BYTES,
    }


def _reader(target, identifier, section):
    return {
        "tool": "tandem_read_result",
        "arguments": _request(target, identifier, section),
    }


# Only transport observations are removed. In particular, task execution accounting,
# deadlines, evidence and report text are not normalized away to hide source changes.
_TRANSPORT = {
    "delivery",
    "delivery_instructions",
    "delivery_status",
    "watchdog",
    "wait",
}
_DETAIL_FIELDS = {
    "report",
    "contract",
    "diagnostics",
    "work_policy",
    "current_task",
}
_IDENTITY = {
    "task_id",
    "conversation_id",
    "run_id",
    "review_id",
    "review_run_id",
    "team_run_id",
    "status",
    "outcome",
    "phase",
    "next_action",
    "accepted",
    "question_id",
    "deadline",
    "cancel_requested",
    "stop_pending",
}


def _stable(raw, *, review=False):
    result = {key: value for key, value in raw.items() if key not in _TRANSPORT}
    if isinstance(result.get("applicability"), dict):
        result["applicability"] = {
            key: value
            for key, value in result["applicability"].items()
            if key != "assessed_at"
        }
    if isinstance(result.get("review"), dict) and isinstance(
        result["review"].get("applicability"), dict
    ):
        result["review"] = {
            **result["review"],
            "applicability": {
                key: value
                for key, value in result["review"]["applicability"].items()
                if key != "assessed_at"
            },
        }
    if review:
        # This clock advances without any source change and cannot bind a page.
        result.pop("elapsed_seconds", None)
        for stage in ("independent", "comparison"):
            if isinstance(result.get(stage), dict):
                result[stage] = _stable(result[stage])
    return result


class ResultPresentation:
    def __init__(self, bridge):
        self.bridge = bridge

    def _decorate(self, result, *, reader=False):
        # A reader's action and a review's semantic action are not native task waits.
        action = result.get("next_action")
        result = self.bridge.channel.decorate(result, include_instructions=not reader)
        if action not in (None, "wait", "await_event"):
            result["next_action"] = action
        return result

    def _project(self, raw, target, *, details):
        identifier = raw["task_id" if target == "task" else "run_id"]
        sections = (
            ("result", "answer", "report", "contract")
            if target == "task"
            else ("result", "independent", "comparison")
        )
        readers = {
            section: _reader(target, identifier, section) for section in sections
        }
        # A pre-existing summary may already contain only an answer prefix.
        # Bind only complete source values, never mistake that prefix for a snapshot.
        for section, reader in readers.items():
            if section == "result":
                # Action acknowledgments are not full task snapshots.
                if target == "task" and "diagnostics" not in raw:
                    continue
                value = _stable(raw, review=target == "review_run")
            elif section not in raw or (
                section == "answer" and raw.get("answer_truncated")
            ):
                continue
            else:
                value = raw[section]
                if section in ("independent", "comparison") and isinstance(value, dict):
                    value = _stable(value)
            data = (
                value.encode("utf-8")
                if section == "answer" and isinstance(value, str)
                else json_bytes(value)
            )
            reader["arguments"]["expected_sha256"] = hashlib.sha256(data).hexdigest()
        result = {
            key: value for key, value in raw.items() if key not in _TRANSPORT - {"wait"}
        }
        omitted = []
        if not details and target == "task":
            for key in _DETAIL_FIELDS:
                if key in result:
                    result.pop(key)
                    omitted.append(key)
            if isinstance(result.get("project_context"), dict):
                result["project_context"] = dict(result["project_context"])
                if "context" in result["project_context"]:
                    result["project_context"].pop("context")
                    omitted.append("project_context.context")
        result["readers"] = readers
        result["omitted_fields"] = sorted(omitted)
        result["truncated_fields"] = []
        result["truncated"] = False
        result["response_limit_bytes"] = MAX_RESPONSE_BYTES
        if target == "task" and isinstance(raw.get("report"), dict):
            summary = raw["report"].get("summary")
            if isinstance(summary, str) and raw.get("summary") != summary:
                result["truncated_fields"].append("summary")
        if isinstance(result.get("report"), dict) and "answer" in result["report"]:
            # The full immutable report remains available as its own section.
            result["report"] = {
                key: value for key, value in result["report"].items() if key != "answer"
            }
            result["report"]["answer_reader"] = readers["answer"]
            result["omitted_fields"].append("report.answer")
        if target == "review_run":
            for stage in ("independent", "comparison"):
                value = result.get(stage)
                if isinstance(value, dict):
                    result[stage] = {
                        key: value[key]
                        for key in (
                            *sorted(_IDENTITY),
                            "summary",
                            "answer",
                            "answer_length",
                        )
                        if key in value
                    }
                    result[stage]["reader"] = readers[stage]
                    result[stage]["details_omitted"] = True
                    result["omitted_fields"].append(stage)
        if isinstance(result.get("answer"), str):
            result["answer_length"] = raw.get("answer_length", len(result["answer"]))
            result["answer_reader"] = readers["answer"]
            limit = 2400 if details else 1200
            result["answer"] = utf8_prefix(result["answer"], limit)
            result["answer_truncated"] = (
                bool(raw.get("answer_truncated"))
                or len(result["answer"]) < result["answer_length"]
            )
            if result["answer_truncated"]:
                result["truncated_fields"].append("answer")
        question = result.get("question")
        if isinstance(question, dict):
            question = result["question"] = dict(question)
            question["reader"] = readers["result"]
            # Keep identity, deadline, kind and blocking scope even if the question
            # body cannot fit. Never invite a reply based on incomplete options.
            for key in ("question", "context", "options"):
                if key in question and wire_size(question[key]) > 1800:
                    question[key] = (
                        utf8_prefix(question[key], 300)
                        if isinstance(question[key], str)
                        else []
                    )
                    result["truncated_fields"].append("question." + key)
                    question["truncated"] = True
            if question.get("truncated"):
                result["next_action"] = "read_question"
        result = self._decorate(result)
        # Preserve small, familiar domain fields. Replace only the largest fields
        # when needed, with explicit recovery rather than silently dropping keys.
        protected = _IDENTITY | {
            "question",
            "readers",
            "answer_reader",
            "omitted_fields",
            "truncated_fields",
            "truncated",
            "response_limit_bytes",
            "delivery",
            "delivery_instructions",
            "watchdog",
            "answer_length",
            "answer_truncated",
        }
        candidates = sorted(
            (key for key in result if key not in protected),
            key=lambda key: wire_size(result[key]),
            reverse=True,
        )
        result["truncated"] = bool(result["truncated_fields"])
        for key in candidates:
            if wire_size(result) <= MAX_RESPONSE_BYTES:
                break
            reader = readers.get(key, readers["result"])
            if key == "answer":
                result[key] = ""
                result["answer_truncated"] = True
            elif key in ("independent", "comparison") and isinstance(result[key], dict):
                result[key] = {
                    field: value
                    for field, value in result[key].items()
                    if field in _IDENTITY
                }
                result[key].update(truncated=True, reader=reader)
            else:
                result[key] = {"truncated": True, "reader": reader}
            if key not in result["truncated_fields"]:
                result["truncated_fields"].append(key)
            result["truncated"] = True
        if wire_size(result) > MAX_RESPONSE_BYTES:
            raise ProtocolError(
                "response_metadata_too_large",
                "Result identity or delivery metadata exceeds the response budget",
                allowed_fix="Read the complete result section using the bounded reader.",
                next_request=readers["result"],
            )
        return result

    def task(self, raw, *, details=False):
        return self._project(raw, "task", details=details)

    def review(self, raw):
        return self._project(raw, "review_run", details=True)

    def read(
        self,
        target,
        identifier,
        section,
        *,
        offset_bytes=0,
        max_bytes=MAX_READ_BYTES,
        expected_sha256=None,
    ):
        allowed = {
            "task": {"result", "answer", "report", "contract"},
            "review_run": {"result", "independent", "comparison"},
        }
        if target not in allowed or section not in allowed[target]:
            raise ProtocolError(
                "invalid_result_section",
                "Use task: result/answer/report/contract or review_run: result/independent/comparison",
                fields=("target", "section"),
                allowed_fix="Choose a reader returned by the current task or review projection.",
            )
        try:
            identifier = str(UUID(identifier))
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError(
                "invalid_identifier",
                "identifier must be a UUID",
                fields=("identifier",),
                allowed_fix="Use the task_id or run_id from the current observation.",
            ) from None
        request = _request(target, identifier, section)
        if (
            type(offset_bytes) is not int
            or offset_bytes < 0
            or type(max_bytes) is not int
            or not 4 <= max_bytes <= MAX_READ_BYTES
        ):
            raise ProtocolError(
                "invalid_page",
                f"offset_bytes must be nonnegative and max_bytes must be 4..{MAX_READ_BYTES}",
                fields=("offset_bytes", "max_bytes"),
                allowed_fix="Restart from the returned reader request.",
                next_request={"tool": "tandem_read_result", "arguments": request},
            )
        if offset_bytes and expected_sha256 is None:
            raise ProtocolError(
                "missing_digest",
                "Nonzero offsets require expected_sha256",
                fields=("expected_sha256",),
                allowed_fix="Follow next_request from the previous page or restart at byte zero.",
                next_request={"tool": "tandem_read_result", "arguments": request},
            )
        raw = (
            self.bridge.view(identifier, details=True)
            if target == "task"
            else self.bridge.review_runs.view(identifier)
        )
        value = (
            _stable(raw, review=target == "review_run")
            if section == "result"
            else raw.get(section)
        )
        if section in ("independent", "comparison") and isinstance(value, dict):
            value = _stable(value)
        # Answer pages are literal UTF-8; structured sections are canonical JSON.
        data = (
            value.encode("utf-8")
            if section == "answer" and isinstance(value, str)
            else json_bytes(value)
        )
        digest = hashlib.sha256(data).hexdigest()
        metadata = {
            "target": target,
            "identifier": identifier,
            "section": section,
            "sha256": digest,
            "total_bytes": len(data),
        }
        if expected_sha256 is not None and expected_sha256 != digest:
            result = self._decorate(
                {
                    **metadata,
                    "status": "digest_mismatch",
                    "next_action": "restart_read",
                    "restart_request": request,
                    "content": None,
                },
                reader=True,
            )
            if wire_size(result) > MAX_RESPONSE_BYTES:
                raise ProtocolError(
                    "response_metadata_too_large",
                    "Delivery metadata exceeds the read budget",
                    allowed_fix="Restart this read without replaying task execution.",
                    next_request={"tool": "tandem_read_result", "arguments": request},
                )
            return result
        if offset_bytes > len(data) or (
            offset_bytes < len(data) and data[offset_bytes] & 0xC0 == 0x80
        ):
            raise ProtocolError(
                "invalid_offset",
                "offset_bytes must be a UTF-8 boundary within the section",
                fields=("offset_bytes",),
                allowed_fix="Use the exact next_request from the preceding page or restart.",
                next_request={"tool": "tandem_read_result", "arguments": request},
            )
        result = self._decorate(
            {
                **metadata,
                "encoding": "utf-8",
                "format": "text"
                if section == "answer" and isinstance(value, str)
                else "json",
                "offset_bytes": offset_bytes,
                "returned_bytes": 0,
                "content": "",
                "next_offset_bytes": None,
                "next_request": None,
                "next_action": "read_complete",
            },
            reader=True,
        )
        end = min(len(data), offset_bytes + max_bytes)
        while True:
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            result.update(
                content=data[offset_bytes:end].decode("utf-8"),
                returned_bytes=end - offset_bytes,
            )
            result["next_offset_bytes"] = end if end < len(data) else None
            result["next_request"] = (
                {
                    **request,
                    "offset_bytes": end,
                    "max_bytes": max_bytes,
                    "expected_sha256": digest,
                }
                if end < len(data)
                else None
            )
            result["next_action"] = "read_more" if end < len(data) else "read_complete"
            if wire_size(result) <= MAX_RESPONSE_BYTES:
                return result
            if end <= offset_bytes + 4:
                raise ProtocolError(
                    "response_metadata_too_large",
                    "Delivery metadata exceeds the read budget",
                    allowed_fix="Inspect tandem_channel status before retrying the read; do not replay task execution.",
                )
            end = offset_bytes + (end - offset_bytes) // 2
