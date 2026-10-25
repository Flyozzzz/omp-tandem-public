"""Field-addressed protocol refusals; a repair hint never authorizes replay."""

import json

from pydantic import ValidationError

PRESERVABLE_ACCEPTANCE_REFUSALS = frozenset(
    {"acceptance_evidence_required", "evidence_identity_conflict", "stale_work_plan"}
)


class ProtocolError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields=(),
        allowed_fix: str,
        next_request: dict | None = None,
        retryable: bool = False,
    ):
        self.envelope = {
            "code": code,
            "fields": list(fields),
            "reasons": [message],
            "allowed_fix": allowed_fix,
            "retryable": retryable,
        }
        if next_request is not None:
            self.envelope["next_request"] = next_request
        super().__init__(
            message
            + " "
            + json.dumps(self.envelope, ensure_ascii=False, separators=(",", ":"))
        )


def require_fields(action: str, values: dict, *, next_request: dict | None = None):
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ProtocolError(
            "missing_fields",
            f"{action} requires {', '.join(missing)}",
            fields=missing,
            allowed_fix="Supply the named fields from the current observation; do not invent identifiers or repeat execution.",
            next_request=next_request,
        )


def protocol_error_response(error):
    """Keep MCP error semantics while exposing repair data without parsing prose."""
    from mcp.types import CallToolResult, TextContent

    cause = error
    while (
        not isinstance(cause, (ValueError, ValidationError))
        and cause.__cause__ is not None
    ):
        cause = cause.__cause__
    if isinstance(cause, ProtocolError):
        envelope = cause.envelope
    elif isinstance(cause, ValidationError):
        issues = cause.errors(include_input=False, include_url=False)
        declared = (
            (issues[0].get("ctx") or {}).get("error") if len(issues) == 1 else None
        )
        if isinstance(declared, ProtocolError):
            envelope = declared.envelope
        else:
            shown = issues[:16]
            envelope = {
                "code": "invalid_arguments",
                "fields": [
                    ".".join(str(part) for part in issue["loc"]) or "<root>"
                    for issue in shown
                ],
                "reasons": [issue["msg"][:256] for issue in shown],
                "allowed_fix": "Correct the named arguments using the tool schema and current identifiers. A corrected mutation is a new operation, not a retry.",
                "retryable": False,
                "error_count": len(issues),
                "errors_truncated": len(shown) != len(issues),
            }
    elif isinstance(cause, ValueError):
        reason = str(cause)
        envelope = {
            "code": "request_refused",
            "fields": [],
            "reasons": [reason[:4000]],
            "reason_truncated": len(reason) > 4000,
            "allowed_fix": "Inspect the current scoped state and satisfy the stated prerequisite. Do not replay execution or infer permissions from this error.",
            "retryable": False,
        }
    else:
        return None
    payload = {"error": envelope}
    return CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structuredContent=payload,
    )
