"""Code-owned review commands in separate exact-input container workspaces."""

from __future__ import annotations

import hashlib
import logging
import os
import selectors
import threading
import time
from uuid import UUID

from docker.errors import DockerException

from .review_containers import ReviewContainer
from .workspace import _directory

MAX_CHECK_OUTPUT = 65536
logger = logging.getLogger(__name__)


def _identifier(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Check execution requires a canonical identifier")
    return value


class _Capture:
    """Incrementally decode Docker stdout/stderr frames without unbounded buffers."""

    def __init__(self, target):
        self.target = target
        self.digest = hashlib.sha256()
        self.count = 0
        self.header = bytearray()
        self.remaining = 0
        self.truncated = False
        self.eof = False

    def feed(self, chunk):
        data = memoryview(chunk)
        while data:
            if not self.remaining:
                size = min(8 - len(self.header), len(data))
                self.header.extend(data[:size])
                data = data[size:]
                if len(self.header) < 8:
                    return True
                if self.header[0] not in {1, 2} or self.header[1:4] != b"\0\0\0":
                    raise ValueError("Invalid Docker output frame")
                self.remaining = int.from_bytes(self.header[4:], "big")
                self.header.clear()
                if not self.remaining:
                    continue
            size = min(self.remaining, len(data))
            kept = data[: min(size, MAX_CHECK_OUTPUT - self.count)]
            self.target.write(kept)
            self.digest.update(kept)
            self.count += len(kept)
            self.remaining -= size
            data = data[size:]
            if len(kept) < size:
                self.truncated = True
                return False
        return True

    def read(self, selector, timeout=0.05):
        for key, _ in selector.select(timeout=timeout):
            try:
                chunk = os.read(key.fd, 16384)
            except BlockingIOError:
                continue
            if chunk:
                if not self.feed(chunk):
                    return False
            else:
                selector.unregister(key.fd)
                self.eof = True
                if self.header or self.remaining:
                    raise ValueError("Docker output ended within a frame")
        return True


class ReviewChecks:
    """One owned verifier thread; the supervisor continues polling the model."""

    def __init__(self, store, workspace, attempt_id: str):
        self.store = store
        self.workspace = workspace
        self.attempt_id = _identifier(attempt_id)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.result: dict | None = None
        self._gone = True
        self._verification_workspace: dict | None = None
        self.directory = store.scope.directory / "work-checks" / self.attempt_id

    def start(self):
        if self.thread is not None:
            raise ValueError("Review check execution cannot be started twice")
        self.thread = threading.Thread(
            target=self._run, name="checks-" + self.attempt_id, daemon=False
        )
        self.thread.start()

    def cancel(self):
        self.stop_event.set()

    def poll(self):
        if self.thread is None or self.thread.is_alive():
            return None
        return self.result or {
            "status": "uncertain",
            "teardown_confirmed": self._gone,
            "error": "Verifier ended without a terminal observation",
        }

    def close(self):
        self.cancel()
        if self.thread is not None:
            # Preparation and each Docker call are bounded independently. A
            # returned thread alone cannot certify its container was removed.
            self.thread.join(timeout=35)
            if self.thread.is_alive() or not self._gone:
                raise RuntimeError("Review check teardown is not confirmed")

    def _current_context(self):
        if self.stop_event.is_set():
            return None
        return self.store.review_check_context(self.attempt_id)

    def _environment(self, policy):
        environment = {"HOME": "/tmp", "TMPDIR": "/tmp", "GIT_OPTIONAL_LOCKS": "0"}
        for name, expected in policy.get("environment_hashes", {}).items():
            value = os.environ.get(name)
            if value is None:
                raise ValueError(f"Required review environment is missing: {name}")
            if hashlib.sha256(value.encode()).hexdigest() != expected:
                raise ValueError(f"Required review environment changed: {name}")
            if name in {"HOME", "TMPDIR"} or name.startswith("OMP_TANDEM_"):
                raise ValueError(f"Reserved review environment variable: {name}")
            environment[name] = value
        return environment

    def _log(self, row):
        _directory(self.directory.parent)
        _directory(self.directory)
        run_id = _identifier(row["run_id"])
        fd = os.open(
            self.directory / f"{run_id}.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        return os.fdopen(fd, "wb")

    def _record_unstarted(self, row, reason, *, interrupted=False):
        with self._log(row) as target:
            content = (reason + "\n").encode("utf-8")[:MAX_CHECK_OUTPUT]
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        return self.store.finish_review_check(
            row["run_id"],
            result="interrupted" if interrupted else "not_run",
            exit_code=None,
            error=reason,
            output={
                "id": row["run_id"],
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "truncated": False,
            },
            execution={},
            process_confirmed_gone=True,
            input_unchanged=False,
        )

    def _run(self):
        try:
            context = self._current_context()
            if context is None:
                self.result = {"status": "not_required", "teardown_confirmed": True}
                return
            _directory(self.directory.parent)
            _directory(self.directory)
            for check in context["checks"]:
                if self._current_context() is None:
                    raise RuntimeError("Review check execution is no longer authorized")
                row = self.store.reserve_review_check(self.attempt_id, check["id"])
                if row.get("replayed"):
                    raise RuntimeError(
                        "Existing review check reservation requires reconciliation"
                    )
                try:
                    environment = self._environment(row["policy"])
                    if self._verification_workspace is None:
                        self._verification_workspace = (
                            self.workspace.prepare_verification(
                                context["attempt"], self.directory / "checkout"
                            )
                        )
                    self.workspace.verify_verification(self._verification_workspace)
                except (OSError, ValueError, RuntimeError) as error:
                    self._record_unstarted(row, f"Check preparation failed: {error}")
                    raise
                if self._current_context() is None:
                    self._record_unstarted(
                        row, "Verification cancelled before command", interrupted=True
                    )
                    raise RuntimeError("Review checks cancelled")
                # Ordinary failed checks remain evidence. Changed inputs, lost
                # authority and uncertain effects stop the rest of the ladder.
                outcome = self._execute(row, environment)
                if outcome in {"interrupted", "not_run"}:
                    raise RuntimeError(
                        "Review check interrupted; effects require reconciliation"
                    )
            assessment = self.store.review_check_assessment(self.attempt_id)
            self.result = {
                "status": assessment["status"],
                "teardown_confirmed": self._gone,
            }
        except Exception as error:
            logger.exception("Supervisor review verification requires attention")
            self.result = {
                "status": "uncertain",
                "teardown_confirmed": self._gone,
                "error": str(error),
            }

    def _execute(self, row, environment):
        workspace = self._verification_workspace
        policy = row["policy"]
        command = row["check"].get("command")
        if not command:
            self._record_unstarted(
                row, "Selected review check has no executable command"
            )
            return "not_run"
        context = self._current_context()
        if context is None:
            self._record_unstarted(
                row, "Verification cancelled before create", interrupted=True
            )
            return "interrupted"
        seconds = min(
            policy["timeout_seconds"], context["attempt"]["deadline"] - time.time()
        )
        if seconds <= 0:
            self._record_unstarted(
                row, "Review check deadline expired", interrupted=True
            )
            return "interrupted"
        deadline = time.monotonic() + seconds
        container = None
        stream = None
        started = False
        execution = {}
        code = None
        error = None
        interrupted = False
        unchanged = False
        with self._log(row) as target, selectors.DefaultSelector() as selector:
            capture = _Capture(target)
            try:
                container = ReviewContainer(
                    policy["container"],
                    scope_id=self.store.scope.key,
                    attempt_id=self.attempt_id,
                    run_id=row["run_id"],
                    workspace=workspace["path"],
                    command=command,
                )
                self._gone = False
                container.create(environment)
                execution = {
                    "executor": "docker",
                    "container_id": container.identifier,
                    "container_name": container.name,
                    "image_id": policy["container"]["image_id"],
                    "daemon_id": policy["container"]["daemon_id"],
                    "cwd": "/workspace",
                    "commit": workspace["commit"],
                    "tree_hash": workspace["tree_hash"],
                    "environment_hashes": dict(policy.get("environment_hashes", {})),
                    "timeout_seconds": policy["timeout_seconds"],
                    "os_sandbox": False,
                    "process_boundary": "docker_pid_namespace",
                }
                stream = container.attach()
                os.set_blocking(stream.fileno(), False)
                selector.register(stream.fileno(), selectors.EVENT_READ)
                if self._current_context() is None or time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Authorization or deadline changed before container start"
                    )
                # This request is never replayed, including after a lost response.
                container.start()
                started = True
                self.store.start_review_check(row["run_id"], execution=execution)
                while True:
                    if self._current_context() is None:
                        interrupted = True
                        error = "Check cancelled by stop, rejection or invalid authorization"
                        break
                    if time.monotonic() >= deadline:
                        error = "Review check timeout"
                        break
                    if not capture.read(selector):
                        error = "Review check output limit exceeded"
                        break
                    state = container.inspect()["State"]
                    if not state.get("Running"):
                        code = state.get("ExitCode")
                        if (
                            type(code) is not int
                            or state.get("Error")
                            or state.get("OOMKilled")
                        ):
                            raise RuntimeError(
                                "Docker did not provide a normal terminal command observation"
                            )
                        break
            except (
                DockerException,
                OSError,
                ValueError,
                RuntimeError,
                KeyError,
                TypeError,
            ) as failure:
                logger.warning(
                    "Declared review check execution failed: %s", type(failure).__name__
                )
                error = f"Review check execution failed: {type(failure).__name__}"
                interrupted = True
            finally:
                if container is not None:
                    try:
                        self._gone = container.remove()
                    except (
                        DockerException,
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ) as failure:
                        self._gone = False
                        logger.warning(
                            "Review container removal unconfirmed: %s",
                            type(failure).__name__,
                        )
                    if not self._gone:
                        interrupted = True
                        error = "Review container removal is not confirmed; reconcile, never replay"
                    try:
                        # EOF is output completeness, NOT process-stop proof.
                        # Only owned resource removal above provides that boundary.
                        drain_deadline = time.monotonic() + 3
                        while selector.get_map() and not capture.truncated:
                            if time.monotonic() >= drain_deadline:
                                raise RuntimeError("Docker output stream did not close")
                            if not capture.read(selector):
                                error = error or "Review check output limit exceeded"
                    except (OSError, ValueError, RuntimeError) as failure:
                        interrupted = True
                        capture.truncated = True
                        error = (
                            error
                            or f"Review output capture failed: {type(failure).__name__}"
                        )
                    finally:
                        container.close(stream)
                target.flush()
                os.fsync(target.fileno())
            if self._gone:
                try:
                    self.workspace.verify_verification(workspace)
                    unchanged = True
                except (OSError, ValueError, RuntimeError) as invalid:
                    interrupted = True
                    error = f"Verification input changed: {invalid}"
            try:
                if self._current_context() is None:
                    interrupted = True
                    error = (
                        error or "Authorization changed before recording check outcome"
                    )
            except (OSError, ValueError, RuntimeError):
                interrupted = True
                error = error or "Authorization changed before recording check outcome"
            result = (
                "not_run"
                if not started
                else "interrupted"
                if interrupted
                else "passed"
                if code == 0 and not error and unchanged and capture.eof
                else "failed"
            )
            self.store.finish_review_check(
                row["run_id"],
                result=result,
                exit_code=code,
                error=error,
                output={
                    "id": row["run_id"],
                    "sha256": capture.digest.hexdigest(),
                    "bytes": capture.count,
                    "truncated": capture.truncated or not capture.eof,
                },
                execution=execution,
                process_confirmed_gone=self._gone,
                input_unchanged=unchanged,
            )
        return result
