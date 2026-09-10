"""One reserved source capture, supervised as a killable controller child."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .artifacts import ArtifactStore
from .reviews import (
    _MAX_TOTAL,
    CaptureReservation,
    ReviewRequest,
    ReviewStore,
    publication_lock,
)
from .task_store import initialize_database
from .workspace import resolve_scope

_MAX_RESPONSE = 4 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--review-id", required=True)
    args = parser.parse_args()
    try:
        reservation = CaptureReservation(args.run_id, args.owner, args.review_id)
        payload = sys.stdin.buffer.read(_MAX_TOTAL + 1)
        if len(payload) > _MAX_TOTAL:
            raise ValueError("Review request exceeds 16 MiB")
        request = ReviewRequest.model_validate_json(payload)
        scope = resolve_scope(args.state_dir, args.project_root)
        # Identity validation precedes every store that can write to this DB.
        with publication_lock(scope):
            database = initialize_database(scope)
            reviews = ReviewStore(database, scope, ArtifactStore(database))
        summary = reviews.create(request, reservation=reservation)
        response = json.dumps(summary, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        if len(response) + 1 > _MAX_RESPONSE:
            raise ValueError("Capture summary exceeds output limit")
        sys.stdout.buffer.write(response + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        sqlite3.Error,
        RuntimeError,
        KeyError,
        AttributeError,
    ) as exc:
        # Never emit a traceback, validation input, or an unbounded child error.
        message = (
            "Invalid review request"
            if isinstance(exc, ValidationError)
            else str(exc)[:2000]
            if isinstance(exc, ValueError)
            else type(exc).__name__
        )
        sys.stderr.write(
            "Capture failed: " + json.dumps(message, ensure_ascii=True) + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
