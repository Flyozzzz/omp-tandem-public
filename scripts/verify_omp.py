"""Finite, credential-free compatibility checks against the official OMP binary.

The fixture substitutes HTTP model output, never RPC or native tool execution.
Run with the project's frozen environment; see docs/compatibility.md.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omp_tandem.runtime_models import ACTIVE

ROOT = Path(__file__).resolve().parents[1]
MODEL = "tandem-compat/fixture"
READ_TOKEN = "native-read-proof-803719"
WRITE_TOKEN = "native-write-proof-194827\n"
logger = logging.getLogger(__name__)


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


@contextmanager
def check(report, name):
    started = time.monotonic()
    item = {"name": name, "status": "running"}
    report["checks"].append(item)
    try:
        yield item
    except Exception as exc:
        item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        item["status"] = "passed"
    finally:
        item["seconds"] = round(time.monotonic() - started, 3)


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def binary(args, manifest, report):
    arch = {"aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(
        platform.machine(), platform.machine()
    )
    key = f"{platform.system().lower()}-{arch}"
    require(key in manifest["assets"], f"No pinned official asset for {key}")
    asset = manifest["assets"][key]
    target = (
        args.omp.resolve()
        if args.omp
        else args.cache_dir.resolve() / manifest["omp_version"] / asset["name"]
    )
    with check(report, "official_binary_sha256") as evidence:
        if not args.omp and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Never leave an interrupted download at the executable cache path.
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                partial = Path(output.name)
                try:
                    request = urllib.request.Request(
                        f"{manifest['download_base']}/{asset['name']}",
                        headers={"User-Agent": "omp-tandem-compatibility"},
                    )
                    with urllib.request.urlopen(request, timeout=60) as response:
                        shutil.copyfileobj(response, output, length=1024 * 1024)
                    output.flush()
                    require(
                        digest(partial) == asset["sha256"],
                        "Downloaded SHA-256 mismatch",
                    )
                    partial.chmod(0o700)
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)
        actual = digest(target)
        report["omp"] = {
            "path": str(target),
            "source": "provided" if args.omp else "official_release_cache",
            "sha256": actual,
            "expected_sha256": asset["sha256"],
            "asset": asset["name"],
        }
        evidence["sha256"] = actual
        require(
            actual == asset["sha256"],
            f"SHA-256 mismatch for {target}; refuse execution",
        )
    return target


@contextmanager
def isolated_environment(root):
    previous = dict(os.environ)
    home = root / "home"
    agent = home / ".omp" / "agent"
    agent.mkdir(parents=True)
    temporary = root / "tmp"
    temporary.mkdir()
    # Allowlist, not a list of today's known provider keys. In particular no PATH,
    # proxy, dotenv, auth broker, SDK injection, or provider overrides are inherited.
    clean = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SHELL": "/bin/bash",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "CI": "true",
        "NO_COLOR": "1",
        "PI_CODING_AGENT_DIR": str(agent),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "OMP_SKIP_SETUP": "1",
        "PI_BASH_NO_LOGIN": "1",
        "OTEL_SDK_DISABLED": "true",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield agent
    finally:
        os.environ.clear()
        os.environ.update(previous)


class Provider:
    """Sequential scripts with real tool results collected from the next HTTP call."""

    def __init__(self):
        self.lock = threading.Lock()
        self.steps = []
        self.requests = []
        self.errors = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.total_requests = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    self.connection.settimeout(10)
                    require(
                        self.path == "/v1/chat/completions",
                        f"Unexpected route: {self.path}",
                    )
                    length = int(self.headers.get("Content-Length", "0"))
                    require(0 < length <= 2 * 1024 * 1024, "Invalid HTTP request size")
                    body = json.loads(self.rfile.read(length))
                    require(
                        body.get("model") == "fixture",
                        "Unexpected paid/non-fixture model",
                    )
                    require(
                        body.get("stream") is True, "Expected streaming model request"
                    )
                    require(
                        not self.headers.get("Authorization"),
                        "Unexpected provider credentials",
                    )
                    with provider.lock:
                        provider.total_requests += 1
                        require(
                            provider.total_requests <= 64,
                            "Unexpected model request loop",
                        )
                        provider.requests.append(body)
                        require(
                            bool(provider.steps),
                            "Unexpected model request after script exhausted",
                        )
                        step = provider.steps.pop(0)
                    if step == "hold":
                        provider.entered.set()
                        provider.release.wait(timeout=90)
                        step = "released"
                    if isinstance(step, tuple):
                        name, arguments = step
                        delta = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{provider.total_requests}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        }
                        reason = "tool_calls"
                    else:
                        delta = {"role": "assistant", "content": step}
                        reason = "stop"
                    chunks = [
                        {
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}
                            ]
                        },
                        {
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": reason}
                            ],
                            "usage": {
                                "prompt_tokens": 32,
                                "completion_tokens": 16,
                                "total_tokens": 48,
                            },
                        },
                    ]
                    payload = (
                        b"".join(
                            (
                                "data: "
                                + json.dumps(
                                    {
                                        "id": f"fixture_{provider.total_requests}",
                                        "object": "chat.completion.chunk",
                                        "created": 1,
                                        "model": "fixture",
                                        **chunk,
                                    }
                                )
                                + "\n\n"
                            ).encode()
                            for chunk in chunks
                        )
                        + b"data: [DONE]\n\n"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when the real client aborts the held cancellation response.
                    pass
                except Exception as exc:
                    logger.exception("Local compatibility fixture rejected request")
                    with provider.lock:
                        provider.errors.append(f"{type(exc).__name__}: {exc}")
                    self.send_error(500, "Local compatibility fixture rejected request")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), "Local provider did not stop")

    def prepare(self, steps):
        with self.lock:
            require(not self.steps, "Previous HTTP script was not consumed")
            self.steps = list(steps)
            self.requests = []
            self.entered.clear()
            self.release.clear()

    def results(self):
        require(not self.errors, f"Local provider errors: {self.errors}")
        results = {}
        calls = {}
        for request in self.requests:
            for message in request["messages"]:
                for call in message.get("tool_calls", []):
                    calls[call["id"]] = call["function"]["name"]
                if message.get("role") == "tool":
                    name = calls.get(message["tool_call_id"], message["tool_call_id"])
                    results[name] = message.get("content", "")
        return results

    def configure(self, agent):
        # JSON is a YAML subset; no YAML library or credentials are needed.
        (agent / "models.yml").write_text(
            json.dumps(
                {
                    "providers": {
                        "tandem-compat": {
                            "baseUrl": f"http://127.0.0.1:{self.server.server_port}/v1",
                            "api": "openai-completions",
                            "auth": "none",
                            "models": [
                                {
                                    "id": "fixture",
                                    "name": "Deterministic localhost fixture",
                                    "reasoning": False,
                                    "input": ["text"],
                                    "contextWindow": 128000,
                                    "maxTokens": 4096,
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                }
            )
        )


def finish(answer):
    return (
        "tandem_finish",
        {"outcome": "success", "answer": answer, "summary": "Fixture turn complete"},
    )


def await_task(bridge, task_id, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = bridge.view(task_id, details=True)
        if result["status"] not in ACTIVE:
            return result
        time.sleep(0.05)
    bridge.cancel(task_id)
    raise TimeoutError(f"Task {task_id} did not terminate: {result.get('activity')}")


def completed(result, answer):
    require(
        result["status"] == "completed", f"Task failed: {json.dumps(result)[:12000]}"
    )
    require(result["outcome"] == "success", "Structured outcome was not success")
    require(result.get("answer") == answer, "Full structured answer was not preserved")
    require(
        result.get("answer_source") == "report",
        "Answer was not delivered through tandem_finish",
    )
    require(
        result["execution"]["actual"] == {"model": MODEL, "thinking": "off"},
        "get_state selection mismatch",
    )


def exercise(executable, root, provider, report):
    from omp_rpc import RpcClient

    from omp_tandem.bridge import Bridge

    project = root / "project"
    project.mkdir()
    (project / "read-proof.txt").write_text(READ_TOKEN)
    state_dir = root / "state"
    with check(report, "rpc_startup_get_state_essential_registration") as evidence:
        client = RpcClient(
            executable=str(executable),
            cwd=project,
            model=MODEL,
            thinking="off",
            no_skills=True,
            no_rules=True,
            no_session=True,
            extra_args=["--no-tools", "--no-lsp", "--no-extensions", "--no-title"],
            startup_timeout=45,
            request_timeout=15,
        )
        try:
            client.start()
            client.request_raw(
                "set_host_tools",
                tools=[
                    {
                        "name": "compatibility_probe",
                        "description": "Registration only; never invoked",
                        "parameters": {"type": "object", "properties": {}},
                        "loadMode": "essential",
                    }
                ],
            )
            state = client.request_raw("get_state")
            names = sorted(tool["name"] for tool in state.get("dumpTools", []))
            require(
                "compatibility_probe" in names,
                f"Essential host tool absent from get_state: {names}",
            )
            require(
                not {"read", "write", "bash"}.intersection(names),
                f"--no-tools leaked native tools: {names}",
            )
            require(
                state.get("model", {}).get("provider") == "tandem-compat",
                "get_state provider mismatch",
            )
            evidence.update(tools=names, model=MODEL, load_mode="essential")
        finally:
            client.stop()

    bridge = Bridge(
        state_dir,
        str(executable),
        MODEL,
        project_root=project,
        channel_enabled=False,
        webhook_enabled=False,
        migrate_legacy=False,
    )
    try:
        for mode in ("think", "analyze", "work"):
            answer = f"structured-{mode}-answer"
            write_path = project / f"{mode}-write.txt"
            shell_path = project / f"{mode}-shell.txt"
            steps = [
                ("write", {"path": str(write_path), "content": WRITE_TOKEN}),
                (
                    "bash",
                    {
                        "command": f"/usr/bin/touch {shlex.quote(str(shell_path))}",
                        "timeout": 5,
                    },
                ),
                ("read", {"path": str(project / "read-proof.txt")}),
                finish(answer),
                "Turn ended.",
            ]
            with check(
                report, f"bridge_{mode}_native_capabilities_and_finish"
            ) as evidence:
                provider.prepare(steps)
                started = bridge.start(
                    prompt=f"Compatibility fixture for {mode}.",
                    cwd=str(project),
                    mode=mode,
                    execution={"thinking": "off", "timeout_seconds": 60},
                )
                result = await_task(bridge, started["task_id"])
                completed(result, answer)
                results = provider.results()
                require(
                    {"write", "bash", "read", "tandem_finish"} <= results.keys(),
                    f"Missing actual tool results: {results}",
                )
                tool_names = {
                    tool["function"]["name"]
                    for tool in provider.requests[0].get("tools", [])
                }
                require(
                    "tandem_finish" in tool_names,
                    "Essential tandem_finish missing at model boundary",
                )
                if mode == "work":
                    require(
                        write_path.is_file() and write_path.read_text() == WRITE_TOKEN,
                        f"Native write did not take effect: {results['write']}",
                    )
                    require(
                        shell_path.is_file(),
                        f"Native bash did not execute: {results['bash']}",
                    )
                    require(
                        {"write", "bash", "read"} <= tool_names,
                        "Work tools not registered",
                    )
                else:
                    require(
                        not write_path.exists() and not shell_path.exists(),
                        f"{mode} executed blocked mutation",
                    )
                    require(
                        not {"write", "bash"}.intersection(tool_names),
                        f"{mode} registered mutation tools",
                    )
                    for tool in ("write", "bash"):
                        require(
                            re.search(
                                r"not found|unknown tool|not available|not enabled|disabled",
                                str(results[tool]),
                                re.I,
                            ),
                            f"{mode}/{tool} did not return an unavailable-tool error: {results[tool]}",
                        )
                if mode == "think":
                    require(
                        "read" not in tool_names
                        and READ_TOKEN not in str(results["read"]),
                        "Think read a live file",
                    )
                else:
                    require(
                        READ_TOKEN in str(results["read"]),
                        f"Native read failed: {results['read']}",
                    )
                evidence.update(
                    task_id=started["task_id"],
                    tools=sorted(tool_names),
                    tool_results={
                        key: str(value)[:1500] for key, value in results.items()
                    },
                    session_saved=Path(result["diagnostics"]["session_file"]).is_file(),
                )
                require(evidence["session_saved"], "OMP session was not saved")
                if mode == "think":
                    first = result

        with check(report, "bridge_saved_session_continue") as evidence:
            provider.prepare([finish("continued-answer"), "Continuation ended."])
            started = bridge.start(
                prompt="Continue the saved compatibility conversation.",
                conversation_id=first["conversation_id"],
                execution={"thinking": "off", "timeout_seconds": 60},
            )
            result = await_task(bridge, started["task_id"])
            completed(result, "continued-answer")
            require(
                result["diagnostics"]["session_file"]
                == first["diagnostics"]["session_file"],
                "Continuation switched native session files",
            )
            history = json.dumps(provider.requests[0]["messages"])
            require(
                "structured-think-answer" in history,
                "Saved assistant history absent on continuation",
            )
            evidence.update(
                conversation_id=first["conversation_id"],
                task_id=started["task_id"],
                history_restored=True,
            )

        with check(report, "bridge_cancel_inflight_model_stream") as evidence:
            provider.prepare(["hold"])
            started = bridge.start(
                prompt="Cancellation fixture.",
                cwd=str(project),
                mode="think",
                execution={"thinking": "off", "timeout_seconds": 60},
            )
            require(
                provider.entered.wait(timeout=45),
                "OMP never reached held localhost model response",
            )
            cancelled_at = time.monotonic()
            bridge.cancel(started["task_id"])
            result = await_task(bridge, started["task_id"], timeout=15)
            require(result["status"] == "cancelled", f"Cancellation failed: {result}")
            require(
                result.get("outcome") is None,
                "Cancelled task incorrectly claimed success",
            )
            evidence.update(
                task_id=started["task_id"],
                cancel_seconds=round(time.monotonic() - cancelled_at, 3),
            )
            provider.release.set()
        require(not provider.errors, f"Provider errors: {provider.errors}")
    finally:
        provider.release.set()
        bridge.shutdown()
        require(
            not bridge.runtime.threads,
            "Bridge shutdown left native worker threads alive",
        )
    report["provider"] = {
        "transport": "localhost OpenAI-compatible HTTP/SSE",
        "model": MODEL,
        "requests": provider.total_requests,
        "credentials": "none",
        "usage": "synthetic fixture tokens; not a cost or performance benchmark",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Explicit local binary download cache (not a global install)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Machine-readable JSON evidence output",
    )
    parser.add_argument(
        "--omp",
        type=Path,
        help="Use an existing binary; still require the official pinned digest",
    )
    args = parser.parse_args()
    report_path = args.report.resolve()
    report = {
        "schema_version": 1,
        "status": "running",
        "checks": [],
        "platform": {
            "os": platform.system(),
            "release": platform.release(),
            "arch": platform.machine(),
        },
        "python": platform.python_version(),
    }
    started = time.monotonic()
    manifest = json.loads((ROOT / "config" / "omp-compatibility.json").read_text())

    def expired(_signal, _frame):
        raise TimeoutError(
            "Compatibility command exceeded its 480-second total deadline"
        )

    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.alarm(480)
    try:
        executable = binary(args, manifest, report)
        with check(report, "sdk_pin") as evidence:
            distribution = importlib.metadata.distribution("omp-rpc")
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
            require(
                manifest["sdk_revision"] in direct.get("url", ""),
                f"SDK is not installed from pinned revision: {direct}",
            )
            report["sdk"] = {
                "version": distribution.version,
                "revision": manifest["sdk_revision"],
                "direct_url": direct,
            }
            evidence["revision"] = manifest["sdk_revision"]
            report["tandem"] = {
                "installed_version": importlib.metadata.version("omp-tandem"),
                "source_version": tomllib.loads((ROOT / "pyproject.toml").read_text())[
                    "project"
                ]["version"],
            }
        with tempfile.TemporaryDirectory(prefix="tandem-real-omp-") as temporary:
            root = Path(temporary).resolve()
            with isolated_environment(root) as agent:
                with check(report, "actual_omp_version") as evidence:
                    version = subprocess.run(
                        [str(executable), "--version"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=True,
                    ).stdout.strip()
                    report["omp"]["version_output"] = version
                    require(
                        re.fullmatch(
                            r"(?:omp(?:/|\s+))?" + re.escape(manifest["omp_version"]),
                            version,
                        )
                        is not None,
                        f"Expected OMP {manifest['omp_version']}, got {version!r}",
                    )
                    evidence["version"] = version
                with Provider() as provider:
                    provider.configure(agent)
                    exercise(executable, root, provider, report)
        report["status"] = "passed"
    except Exception as exc:
        logger.exception("Real OMP compatibility verification failed")
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        report["seconds"] = round(time.monotonic() - started, 3)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        # Bounded CI log evidence, including failures, without another action pin.
        print(json.dumps(report, indent=2))
        print(f"Compatibility evidence: {report_path}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
