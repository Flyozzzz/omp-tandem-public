"""Bounded Jev Choice transport without service policy or persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field

import httpx

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
MAX_REQUEST_BYTES = 32000
MAX_RESPONSE_BYTES = 65536


@dataclass(frozen=True)
class JevConfig:
    enabled: bool = False
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    recommend_enabled: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        if type(self.recommend_enabled) is not bool:
            raise TypeError("recommend_enabled must be a boolean")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise TypeError("api_key must be text or None")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 10
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 10")


def canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Nonfinite JSON constant")


def _number(value, *, maximum=None):
    return (
        type(value) in (int, float)
        and (type(value) is int or math.isfinite(value))
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _identifier(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value)
        else None
    )


class JevClient:
    def __init__(self, config: JevConfig | None = None, *, endpoint: str = ENDPOINT):
        self.config = config or JevConfig()
        # This override is deliberately not an operator/MCP setting.
        self.endpoint = endpoint
        self.closed = False
        self._client: httpx.AsyncClient | None = None

    def status(self) -> dict:
        return {
            "configured": bool(self.config.api_key and self.config.api_key.strip()),
            "model": MODEL,
            "provider": "OpenRouter / TypeSafe",
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "timeout_seconds": self.config.timeout_seconds,
        }

    def prepare(self, body: dict) -> tuple[bytes, str]:
        payload = canonical_json(body).encode("utf-8")
        identity = hashlib.sha256(self.endpoint.encode("utf-8"))
        identity.update(b"\0")
        identity.update(payload)
        return payload, identity.hexdigest()

    async def close(self):
        self.closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, payload: bytes):
        if self.closed:
            return None, "service_closed"
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                trust_env=False,
                follow_redirects=False,
                timeout=self.config.timeout_seconds,
            )
        async with asyncio.timeout(self.config.timeout_seconds):
            async with self._client.stream(
                "POST",
                self.endpoint,
                content=payload,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code != 200:
                    return {"http_status": response.status_code}, "http_error"
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    return None, "unsupported_response_encoding"
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        return None, "response_too_large"
                    body.extend(chunk)
        try:
            return json.loads(
                body, object_pairs_hook=_object, parse_constant=_constant
            ), None
        except (ValueError, UnicodeError, RecursionError):
            return None, "malformed_response"

    @staticmethod
    def _decode(response, questions):
        if not isinstance(response, dict):
            raise TypeError("Response must be an object")
        answers = response.get("answers")
        if not isinstance(answers, dict) or answers.keys() != questions.keys():
            raise ValueError("Unexpected question keys")
        decoded = {}
        for key, question in questions.items():
            criteria = question["criteria"]
            answer = answers[key]
            if not isinstance(answer, dict) or answer.keys() != {
                "type",
                "choice",
                "confidence",
                "probabilities",
            }:
                raise ValueError("Malformed choice")
            probabilities = answer["probabilities"]
            choice = answer["choice"]
            if (
                answer["type"] != "choice"
                or not isinstance(choice, str)
                or choice not in criteria
                or not isinstance(probabilities, dict)
                or probabilities.keys() != criteria.keys()
                or not all(
                    _number(value, maximum=1) for value in probabilities.values()
                )
                or not math.isclose(
                    sum(probabilities.values()), 1, rel_tol=0, abs_tol=0.020000001
                )
                or probabilities[choice] < max(probabilities.values()) - 1e-9
                or not _number(answer["confidence"], maximum=1)
            ):
                raise ValueError("Invalid choice distribution")
            decoded[key] = {
                "choice": choice,
                "probabilities": probabilities,
                "confidence": answer["confidence"],
            }
        return decoded

    @staticmethod
    def _provenance(response):
        response = response if isinstance(response, dict) else {}
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return {
            "model": {
                "requested": MODEL,
                "observed": _identifier(response.get("model")),
            },
            "provider": _identifier(response.get("provider")),
            "response_id": _identifier(response.get("id")),
            "usage": {
                key: usage[key]
                if type(usage.get(key)) is int and usage[key] >= 0
                else None
                for key in ("input_tokens", "output_tokens")
            }
            | {"cost": usage.get("cost") if _number(usage.get("cost")) else None},
        }

    async def decide(self, payload: bytes, questions: dict) -> dict:
        result = {"status": "unavailable"}
        # Cancellation propagates so service-owned reservations remain unresolved.
        try:
            response, reason = await self._request(payload)
            if reason == "http_error":
                result["http_status"] = response["http_status"]
            if reason is None:
                result.update(self._provenance(response))
                try:
                    answers = self._decode(response, questions)
                except (ValueError, TypeError, OverflowError):
                    reason = "malformed_response"
                else:
                    result.update(answers=answers, status="completed")
        except (TimeoutError, httpx.TimeoutException):
            reason = "timeout"
        except (httpx.HTTPError, OSError, ValueError):
            reason = "transport_error"
        if reason is not None:
            result["reason"] = reason
        return result
