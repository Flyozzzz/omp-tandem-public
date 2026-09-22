"""Docker transport simulation only: no containers, subprocesses, or shell execution."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import socketserver
import struct
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class Scenario:
    exit_code: int = 0
    stdout: bytes = b"simulated check output\n"
    stderr: bytes = b""
    hang: bool = False
    mutate_source: bool = False
    lose_create_response: bool = False
    lose_start_response: bool = False
    fail_removal: bool = False
    unavailable_after_removal: bool = False


@dataclass
class _Container:
    identifier: str
    name: str
    config: dict
    scenario: Scenario
    started: threading.Event = field(default_factory=threading.Event)
    removed: threading.Event = field(default_factory=threading.Event)
    attached: threading.Event = field(default_factory=threading.Event)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # Disconnects are expected for deliberate lost acknowledgements and cancel.
        pass


class DockerEngine:
    """A bounded local HTTP fault peer; supplied scenarios never interpret Cmd."""

    image_id = "sha256:" + "a" * 64
    daemon_id = "synthetic-review-engine"
    network_id = "b" * 64

    def __init__(self, private_workspace_root: Path):
        self.private_workspace_root = private_workspace_root.resolve()
        self.scenarios = [Scenario()]
        self.created = []
        self.start_count = 0
        self.removal_count = 0
        self.containers = {}
        self.tombstones = {}
        self.errors = []
        self._lock = threading.RLock()
        self._connections = set()
        self._closing = threading.Event()

    def __enter__(self):
        # Darwin's sockaddr_un is too short for pytest's normal temporary roots.
        self._temporary = tempfile.TemporaryDirectory(prefix="td-", dir="/tmp")
        self.socket_path = str(Path(self._temporary.name, "docker.sock").resolve())
        self._server = _UnixServer(self.socket_path, _Handler)
        self._server.engine = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._closing.set()
        self._server.shutdown()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._temporary.cleanup()

    @property
    def policy(self):
        return {
            "executor": "docker",
            "socket": self.socket_path,
            "daemon_id": self.daemon_id,
            "api_version": "1.45",
            "image_id": self.image_id,
            "network_mode": "none",
            "platform": "linux",
        }

    @property
    def live_containers(self):
        with self._lock:
            return tuple(self.containers)

    def _mutate(self, container):
        mounts = container.config.get("HostConfig", {}).get("Mounts", [])
        sources = [
            mount["Source"]
            for mount in mounts
            if mount.get("Type") == "bind" and mount.get("Target") == "/workspace"
        ]
        if len(sources) != 1:
            raise ValueError("Mutation scenario requires one private workspace bind")
        workspace = Path(sources[0])
        resolved = workspace.resolve(strict=True)
        relative = resolved.relative_to(self.private_workspace_root)
        if len(relative.parts) != 2 or relative.parts[-1] != "checkout":
            raise ValueError("Refusing mutation outside the private attempt checkout")
        target = workspace / "backend.py"
        if target.is_symlink() or target.resolve() != resolved / "backend.py":
            raise ValueError("Refusing mutation through a redirected source file")
        target.write_text("VALUE = 99\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(2)
        with self.server.engine._lock:
            self.server.engine._connections.add(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.engine._lock:
                self.server.engine._connections.discard(self.connection)

    def log_message(self, *_):
        pass

    def _reply(self, status, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _disconnect(self):
        self.close_connection = True
        with suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _dispatch(self):
        engine = self.server.engine
        parsed = urlsplit(self.path)
        path = re.sub(r"^/v1\.\d+", "", unquote(parsed.path))
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.command == "GET" and path == "/info":
            return self._reply(200, {"ID": engine.daemon_id, "OSType": "linux"})
        if self.command == "GET" and path == "/version":
            return self._reply(200, {"ApiVersion": "1.45", "Os": "linux"})
        if self.command == "GET" and path == f"/images/{engine.image_id}/json":
            return self._reply(
                200, {"Id": engine.image_id, "Os": "linux", "Config": {}}
            )
        if self.command == "GET" and path == f"/networks/{engine.network_id}":
            return self._reply(
                200, {"Id": engine.network_id, "Driver": "bridge", "Scope": "local"}
            )
        if self.command == "POST" and path == "/containers/create":
            with engine._lock:
                index = len(engine.created)
                if index >= len(engine.scenarios):
                    engine.errors.append("Unexpected additional container creation")
                    return self._reply(500, {"message": engine.errors[-1]})
                identifier = hashlib.sha256(f"container-{index}".encode()).hexdigest()
                container = _Container(
                    identifier, query["name"][0], body, engine.scenarios[index]
                )
                engine.containers[identifier] = container
                engine.created.append(identifier)
            if container.scenario.lose_create_response:
                return self._disconnect()
            return self._reply(201, {"Id": identifier, "Warnings": []})
        match = re.fullmatch(r"/containers/([^/]+)(?:/(json|attach|start))?", path)
        if not match:
            engine.errors.append(f"Unsupported request: {self.command} {path}")
            return self._reply(404, {"message": engine.errors[-1]})
        identifier, action = match.groups()
        with engine._lock:
            container = next(
                (
                    item
                    for item in engine.containers.values()
                    if identifier in {item.identifier, item.name}
                ),
                None,
            )
            absent = engine.tombstones.get(identifier)
        if container is None:
            if absent and absent.scenario.unavailable_after_removal:
                return self._reply(
                    503, {"message": "Simulated unavailable cleanup inspection"}
                )
            return self._reply(404, {"message": "No such container"})
        scenario = container.scenario
        if self.command == "GET" and action == "json":
            started = container.started.is_set()
            running = started and scenario.hang
            host = container.config.get("HostConfig", {})
            mounts = [
                {
                    "Type": mount["Type"],
                    "Source": mount["Source"],
                    "Destination": mount["Target"],
                    "RW": not mount.get("ReadOnly", False),
                    "Propagation": "rprivate",
                }
                for mount in host.get("Mounts", [])
            ]
            return self._reply(
                200,
                {
                    "Id": container.identifier,
                    "Name": "/" + container.name,
                    "Image": engine.image_id,
                    "Config": {
                        key: value
                        for key, value in container.config.items()
                        if key != "HostConfig"
                    },
                    "HostConfig": host,
                    "Mounts": mounts,
                    "State": {
                        "Status": "running"
                        if running
                        else "exited"
                        if started
                        else "created",
                        "Running": running,
                        "Paused": False,
                        "Restarting": False,
                        "Dead": False,
                        "Pid": 123 if running else 0,
                        "ExitCode": scenario.exit_code if started else 0,
                        "Error": "",
                        "StartedAt": "2026-01-01T00:00:00Z"
                        if started
                        else "0001-01-01T00:00:00Z",
                        "FinishedAt": "0001-01-01T00:00:00Z"
                        if running or not started
                        else "2026-01-01T00:00:01Z",
                    },
                },
            )
        if self.command == "POST" and action == "attach":
            self.send_response(101)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "tcp")
            self.send_header("Content-Type", "application/vnd.docker.raw-stream")
            container.attached.set()
            self.end_headers()
            self.wfile.flush()
            while not container.started.wait(0.02):
                if engine._closing.is_set() or container.removed.is_set():
                    return self._disconnect()
            for stream, output in ((1, scenario.stdout), (2, scenario.stderr)):
                if output:
                    self.wfile.write(
                        struct.pack(">BxxxI", stream, len(output)) + output
                    )
                    self.wfile.flush()
            while scenario.hang and not container.removed.wait(0.02):
                if engine._closing.is_set():
                    break
            return self._disconnect()
        if self.command == "POST" and action == "start":
            with engine._lock:
                engine.start_count += 1
            if container.started.is_set():
                return self._reply(304)
            if not container.attached.is_set():
                return self._reply(
                    409, {"message": "Attach must be established before start"}
                )
            if scenario.mutate_source:
                engine._mutate(container)
            container.started.set()
            if scenario.lose_start_response:
                return self._disconnect()
            return self._reply(204)
        if self.command == "DELETE" and action is None:
            if scenario.fail_removal:
                return self._reply(500, {"message": "Simulated removal failure"})
            with engine._lock:
                engine.removal_count += 1
                engine.containers.pop(container.identifier)
                engine.tombstones[container.identifier] = container
                engine.tombstones[container.name] = container
                container.removed.set()
            return self._reply(204)
        engine.errors.append(f"Unsupported request: {self.command} {path}")
        return self._reply(405, {"message": engine.errors[-1]})

    def do_GET(self):
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.server.engine.errors.append(f"{type(error).__name__}: {error}")
            self._reply(500, {"message": str(error)})

    do_POST = do_GET
    do_DELETE = do_GET
