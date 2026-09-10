"""Bounded Claude asyncRewake hooks with live, incarnation-bound attestation.

Only the installed hook reads wake probes. Tool results carry authenticated arm
requests, never probe secrets, paths, answers, errors, or executable instructions.
The hook owns its timeout independently of notification delivery and MCP health.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import select
import socket
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

LIFETIME = 12.0
_TICKET_LIFETIME = 120.0
_MAX_BYTES = 32768
_ACTIVE = {"starting", "running", "cancelling"}
_TOOLS = {
    "tandem_scope",
    "tandem_start",
    "tandem_continue",
    "tandem_result",
    "tandem_wait",
    "tandem_reply",
    "tandem_cancel",
    "tandem_channel",
    "tandem_review_run",
}


def _directory():
    # Fixed namespace: neither tool responses nor hook stdin may supply a path.
    path = Path("/tmp") / f"omp-tandem-watchdog-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Watchdog directory must be private and owned by this user")
    return path


def _project(root):
    return hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()[:16]


def _endpoint(root, owner):
    owner = str(UUID(owner))
    return _directory() / f"{_project(root)}-{owner}.sock"


def _session_path(root, session):
    if not isinstance(session, str) or not session or len(session) > 256:
        raise ValueError("A Claude session ID is required")
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    return _directory() / f"{_project(root)}-{key}.session"


def _session_state(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return _state(stream)


def _state(stream):
    info = os.fstat(stream.fileno())
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Invalid watchdog session marker")
    value = json.loads(stream.read(512))
    if not isinstance(value, dict) or len(value.get("epoch", "")) != 64:
        raise ValueError("Invalid watchdog session epoch")
    return value


def _epoch(path):
    return _session_state(path)["epoch"]


def _claim_owner(path, epoch, owner, born):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = _state(stream)
        if state["epoch"] != epoch:
            return False
        if state["owner"] != owner:
            if state["born"] >= born:
                return False
            stream.seek(0)
            json.dump({"epoch": epoch, "owner": owner, "born": born}, stream)
            stream.truncate()
            stream.flush()
    return True


def _current_owner(path, epoch, owner):
    try:
        state = _session_state(path)
        return (state["epoch"], state["owner"]) == (epoch, owner)
    except (OSError, ValueError):
        return False


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        try:
            raw = self.rfile.readline(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return
            request = json.loads(raw)
            response = self.server.watchdog._arm(request, self.connection)
            self.wfile.write(_encode(response) + b"\n")
        except (OSError, ValueError, TypeError, KeyError):
            return


class Watchdog:
    """One bounded hook process per MCP incarnation; no renewable background lease."""

    def __init__(self, project_root: Path, owner: str):
        self.root, self.owner = Path(project_root).resolve(), owner
        self.secret = secrets.token_bytes(32)
        self.born = time.monotonic_ns()
        self.lock = threading.RLock()
        self.entries = {}
        self.sequence = 0
        self.session = None
        self.epoch = None
        self.marker = None
        self.probe = None
        self.probe_deadline = 0.0
        self.confirmed = False
        self.timer = None
        self.closed = False
        self.server = None
        self.thread = None
        self.error = None

    def start(self):
        try:
            self.path = _endpoint(self.root, self.owner)
            self.server = _Server(str(self.path), _Handler)
            os.chmod(self.path, 0o600)
            self.server.watchdog = self
            self.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.thread.start()
        except (OSError, ValueError):
            if self.server is not None:
                self.server.server_close()
                self.path.unlink(missing_ok=True)
                self.server = None
            self.error = "Watchdog unavailable; use bounded polling"

    def observe(self, task_id, status):
        task_id = str(UUID(task_id))
        with self.lock:
            if status not in _ACTIVE:
                self.entries.pop(task_id, None)
                return
            entry = self.entries.get(task_id)
            if entry is None or entry["expires"] <= time.time():
                self.sequence += 1
                self.entries[task_id] = {
                    "owner": self.owner,
                    "project": _project(self.root),
                    "task_id": task_id,
                    "generation": self.sequence,
                    "expires": time.time() + _TICKET_LIFETIME,
                }

    def seen(self, task_id, status):
        if status not in _ACTIVE:
            with self.lock:
                self.entries.pop(task_id, None)

    def _tickets(self):
        return [
            {
                **entry,
                "signature": hmac.new(
                    self.secret, _encode(entry), "sha256"
                ).hexdigest(),
            }
            for entry in self.entries.values()
            if entry["expires"] > time.time()
        ]

    def metadata(self, task_ids=()):
        with self.lock:
            if self.confirmed and not _current_owner(
                self.marker, self.epoch, self.owner
            ):
                self.confirmed = False
            now = time.monotonic()
            live = bool(
                self.confirmed
                and self.timer
                and self.timer["ready"]
                and not self.closed
                and self.timer["deadline"] - now >= 2
            )
            covered = self.timer["tasks"] if live else {}
            ids = list(task_ids)
            armed = bool(
                ids
                and live
                and all(
                    task in self.entries
                    and covered.get(task) == self.entries[task]["generation"]
                    for task in ids
                )
            )
            return {
                "available": self.server is not None and not self.closed,
                "confirmed": self.confirmed and not self.closed,
                "armed": armed,
                "lifetime_seconds": LIFETIME,
                "remaining_seconds": max(0, round(self.timer["deadline"] - now, 2))
                if live
                else 0,
                "incarnation": self.owner,
                "tickets": self._tickets() if not self.closed else [],
                "error": self.error,
            }

    def confirm(self, token):
        with self.lock:
            if (
                self.closed
                or not self.probe
                or not isinstance(token, str)
                or time.monotonic() > self.probe_deadline
                or self.marker is None
                or not _current_owner(self.marker, self.epoch, self.owner)
                or not secrets.compare_digest(token.encode(), self.probe.encode())
            ):
                raise ValueError("No matching live watchdog wake probe")
            self.confirmed = True
            self.probe = None

    def _arm(self, request, connection):
        if not isinstance(request, dict):
            return {"action": "ignore"}
        session, epoch = request.get("session"), request.get("epoch")
        marker = _session_path(self.root, session)
        if _epoch(marker) != epoch:
            return {"action": "ignore"}
        with self.lock:
            if self.closed:
                return {"action": "ignore"}
            if self.session is not None and (session, epoch) != (
                self.session,
                self.epoch,
            ):
                return {"action": "ignore"}
            tickets = request.get("tickets")
            if not isinstance(tickets, list) or not 1 <= len(tickets) <= 4:
                return {"action": "ignore"}
            accepted = {}
            for ticket in tickets:
                if not isinstance(ticket, dict):
                    return {"action": "ignore"}
                entry = {
                    key: value for key, value in ticket.items() if key != "signature"
                }
                signature = hmac.new(self.secret, _encode(entry), "sha256").hexdigest()
                if (
                    not isinstance(ticket.get("signature"), str)
                    or not hmac.compare_digest(signature, ticket["signature"])
                    or self.entries.get(entry.get("task_id")) != entry
                    or entry["expires"] <= time.time()
                ):
                    return {"action": "ignore"}
                accepted[entry["task_id"]] = entry["generation"]
            if not _claim_owner(marker, epoch, self.owner, self.born):
                return {"action": "ignore"}
            self.session, self.epoch = session, epoch
            self.marker = marker
            if not self.confirmed:
                if self.probe is not None and time.monotonic() < self.probe_deadline:
                    return {"action": "ignore"}
                self.probe = secrets.token_urlsafe(24)
                self.probe_deadline = time.monotonic() + 120
                return {"action": "probe", "token": self.probe}
            if self.timer is not None:
                # Never extend the existing process's positive fixed lifetime.
                # Uncovered tasks retain bounded polling until their own arm succeeds.
                return {"action": "ignore"}
            timer = {
                "tasks": accepted,
                "deadline": time.monotonic() + LIFETIME,
                "ready": False,
            }
            self.timer = timer
        try:
            connection.sendall(b'{"action":"armed"}\n')
            acknowledgment = b""
            while len(acknowledgment) < 16 and not acknowledgment.endswith(b"\n"):
                part = connection.recv(16 - len(acknowledgment))
                if not part:
                    break
                acknowledgment += part
            if acknowledgment != b"ready\n":
                return {"action": "ignore"}
            with self.lock:
                timer["ready"] = True
            while True:
                with self.lock:
                    if self.closed or not _current_owner(marker, epoch, self.owner):
                        return {"action": "ignore"}
                    current = {
                        task: gen
                        for task, gen in timer["tasks"].items()
                        if self.entries.get(task, {}).get("generation") == gen
                    }
                    if not current:
                        return {"action": "ignore"}
                    if time.monotonic() >= timer["deadline"]:
                        for task in current:
                            self.entries.pop(task, None)
                        return {
                            "action": "wake",
                            "task_ids": list(current),
                            "reason": "bounded_check",
                        }
                # Detect a killed hook; its old metadata must not attest a live timer.
                readable, _, _ = select.select([connection], [], [], 0.1)
                if readable:
                    return {"action": "ignore"}
        finally:
            with self.lock:
                if self.timer is timer:
                    self.timer = None

    def close(self):
        with self.lock:
            self.closed = True
            self.confirmed = False
            self.entries.clear()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.path.unlink(missing_ok=True)
            self.thread.join(timeout=2)


def _response(payload):
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    if isinstance(response, list):
        response = {"content": response}
    if not isinstance(response, dict):
        return None
    if isinstance(response.get("structuredContent"), dict):
        return response["structuredContent"]
    if "watchdog" in response:
        return response
    content = response.get("content")
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
    ):
        value = json.loads(content[0]["text"])
        return value if isinstance(value, dict) else None
    return None


def hook(payload, *, root=None):
    """Return (exit code, short stderr). Never execute instructions from hook data."""
    root = Path(root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    event, session = payload.get("hook_event_name"), payload.get("session_id")
    marker = _session_path(root, session)
    if event == "SessionStart":
        temporary = marker.with_suffix("." + secrets.token_hex(8))
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"epoch": secrets.token_hex(32), "owner": None, "born": 0}, stream
            )
        os.replace(temporary, marker)
        return 0, ""
    if event == "SessionEnd":
        marker.unlink(missing_ok=True)
        return 0, ""
    name = payload.get("tool_name", "")
    if (
        event != "PostToolUse"
        or not isinstance(name, str)
        or not name.startswith("mcp__")
        or "omp-tandem" not in name.rsplit("__", 1)[0]
        or name.rsplit("__", 1)[-1] not in _TOOLS
    ):
        return 0, ""
    epoch = _epoch(marker)
    result = _response(payload)
    metadata = result.get("watchdog") if isinstance(result, dict) else None
    if not isinstance(metadata, dict):
        return 0, ""
    tickets = metadata.get("tickets")
    if not isinstance(tickets, list) or not 1 <= len(tickets) <= 4:
        return 0, ""
    owner = str(UUID(metadata["incarnation"]))
    path = _endpoint(root, owner)
    request = {"session": session, "epoch": epoch, "tickets": tickets}
    task_ids = [str(UUID(ticket["task_id"])) for ticket in tickets]
    # Only an authenticated arm acknowledgment enables independent timeout wakes.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(_encode(request) + b"\n")
        deadline = time.monotonic() + LIFETIME + 1
        data = b""
        armed = False
        while time.monotonic() < deadline:
            if _epoch(marker) != epoch or (
                armed and not _current_owner(marker, epoch, owner)
            ):
                return 0, ""
            readable, _, _ = select.select([client], [], [], 0.1)
            if readable:
                try:
                    part = client.recv(_MAX_BYTES - len(data))
                except OSError:
                    break
                if not part:
                    break
                data += part
                while b"\n" in data:
                    line, data = data.split(b"\n", 1)
                    response = json.loads(line)
                    if response.get("action") == "armed":
                        armed = True
                        client.sendall(b"ready\n")
                        continue
                    if response.get("action") == "probe":
                        return 2, "OMP watchdog probe " + response["token"]
                    if response.get("action") == "wake" and armed:
                        task_ids = [str(UUID(task)) for task in response["task_ids"]]
                        return 2, "OMP " + ",".join(task_ids) + " bounded_check"
                    return 0, ""
                if len(data) >= _MAX_BYTES:
                    return 0, ""
        return (2, "OMP " + ",".join(task_ids) + " bounded_check") if armed else (0, "")


def main(*, output=None):
    try:
        raw = sys.stdin.buffer.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        code, message = hook(payload)
        if message:
            print(message, file=output or sys.stderr, flush=True)
        return code
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Hook failures are not OMP task failures and never leak exception text.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
