"""Finite, credential-free compatibility checks against the official OMP binary.

The fixture substitutes HTTP model output, never RPC or native tool execution.
Run with the project's frozen environment; see docs/compatibility.md.
"""

from __future__ import annotations

import argparse
import asyncio
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
from uuid import uuid4

from omp_tandem.runtime_models import ACTIVE

ROOT = Path(__file__).resolve().parents[1]
MODEL = "tandem-compat/fixture"
HELPER_MODEL = "tandem-compat/fixture-smol"
FIXTURE_MODELS = {"fixture", "fixture-smol"}
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


# Checks that establish the binary and the environment before any probe runs.
# Everything else in the report is a probe of the running OMP, so a failure there
# is a different class. The run also records its phase so a failure raised outside
# any check (for example while creating the isolated environment) is classified by
# where it happened rather than defaulting to "no failed check".
PREFLIGHT_CHECK = "binary_preflight"
ACQUISITION_CHECKS = ("official_binary_acquisition", "official_binary_sha256")
ENVIRONMENT_CHECKS = ("sdk_pin", "actual_omp_version", "installed_runtime_setup")
SETUP_CHECKS = (PREFLIGHT_CHECK, *ACQUISITION_CHECKS, *ENVIRONMENT_CHECKS)
PHASES = ("preflight", "acquisition", "setup", "probes", "complete")
PHASE_FAILURE = {
    "preflight": "environment",
    "acquisition": "acquisition",
    "setup": "environment",
    "probes": "probe",
}


def platform_key():
    arch = {"aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(
        platform.machine(), platform.machine()
    )
    return f"{platform.system().lower()}-{arch}"


def preflight(args, manifest, report, *, key=None):
    """Identify the pinned asset and where the binary must come from.

    Runs before any network or process activity so a missing pin, an unwritable
    cache or a supplied path that does not exist fails as an environment problem
    rather than as a probe result.
    """
    key = key or platform_key()
    with check(report, PREFLIGHT_CHECK) as evidence:
        require(key in manifest["assets"], f"No pinned official asset for {key}")
        asset = manifest["assets"][key]
        if args.omp:
            target = args.omp.resolve()
            require(target.is_file(), f"Supplied binary is not a file: {target}")
            source = "provided"
        else:
            cache = args.cache_dir.resolve()
            require(
                not cache.exists() or cache.is_dir(),
                f"Cache path is not a directory: {cache}",
            )
            target = cache / manifest["omp_version"] / asset["name"]
            try:
                # Cache readiness is an environment question; settle it here so a
                # later download cannot fail for a reason that has nothing to do
                # with the network or the asset.
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(f"Cache directory is not usable: {exc}") from exc
            require(
                os.access(target.parent, os.W_OK),
                f"Cache directory is not writable: {target.parent}",
            )
            source = "official_release_cache"
        plan = {
            "key": key,
            "asset": asset["name"],
            "expected_sha256": asset["sha256"],
            "url": f"{manifest['download_base']}/{asset['name']}",
            "target": target,
            "source": source,
            "acquisition_required": source != "provided" and not target.exists(),
        }
        report["preflight"] = {
            **{name: value for name, value in plan.items() if name != "target"},
            "target": str(target),
            "omp_version": manifest["omp_version"],
        }
        evidence.update(
            source=source,
            acquisition_required=plan["acquisition_required"],
            asset=asset["name"],
        )
    return plan


def acquire(plan, report, *, opener=urllib.request.urlopen):
    """Download the pinned asset into the cache; separate from probe execution.

    The whole command deadline still applies: a slow download fails here as an
    acquisition failure with probes recorded as not run, not as a probe result.
    """
    target = plan["target"]
    with check(report, "official_binary_acquisition") as evidence:
        evidence.update(url=plan["url"], target=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Never leave an interrupted download at the executable cache path.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            partial = Path(output.name)
            try:
                request = urllib.request.Request(
                    plan["url"], headers={"User-Agent": "omp-tandem-compatibility"}
                )
                with opener(request, timeout=60) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
                output.flush()
                require(
                    digest(partial) == plan["expected_sha256"],
                    "Downloaded SHA-256 mismatch",
                )
                partial.chmod(0o700)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        evidence["bytes"] = target.stat().st_size
    return target


def verify_binary(plan, report):
    """Refuse execution unless the binary on disk matches the official pin."""
    target = plan["target"]
    with check(report, "official_binary_sha256") as evidence:
        actual = digest(target)
        report["omp"] = {
            "path": str(target),
            "source": plan["source"],
            "sha256": actual,
            "expected_sha256": plan["expected_sha256"],
            "asset": plan["asset"],
        }
        evidence["sha256"] = actual
        require(
            actual == plan["expected_sha256"],
            f"SHA-256 mismatch for {target}; refuse execution",
        )
    return target


def binary(args, manifest, report):
    report["phase"] = "preflight"
    plan = preflight(args, manifest, report)
    if plan["acquisition_required"]:
        report["phase"] = "acquisition"
        acquire(plan, report)
    report["phase"] = "acquisition"
    return verify_binary(plan, report)


def _probe_checks(report):
    return [
        item for item in report.get("checks", []) if item["name"] not in SETUP_CHECKS
    ]


def _phase(report):
    phase = report.get("phase")
    if phase in PHASES:
        return phase
    # Reports written before phases were recorded: infer from what ran.
    return "probes" if _probe_checks(report) else "preflight"


def failure_class(report):
    """environment | acquisition | probe | None.

    A failed check decides by its name; a failure raised outside any check (the
    report says failed but no check does) decides by the phase that was running.
    """
    for item in report.get("checks", []):
        if item.get("status") != "failed":
            continue
        if item["name"] == PREFLIGHT_CHECK or item["name"] in ENVIRONMENT_CHECKS:
            return "environment"
        if item["name"] in ACQUISITION_CHECKS:
            return "acquisition"
        return "probe"
    if report.get("status") == "failed":
        return PHASE_FAILURE.get(_phase(report))
    return None


def probe_status(report):
    """not_run | failed | passed for the checks that exercise the binary.

    Setup checks (pin, SDK, version) never count as probes: a run that failed
    before the probe phase started reports not_run even when setup passed.
    """
    if _phase(report) not in ("probes", "complete"):
        return "not_run"
    probes = _probe_checks(report)
    if not probes:
        return "not_run"
    if any(item.get("status") == "failed" for item in probes):
        return "failed"
    return "passed"


def abort_gate_supported(alive):
    """Abort-only capability needs a parent that was still answering.

    Silence after the release proves nothing when the parent process is gone:
    the gate requires a positive liveness observation made before any cleanup,
    plus no late model request and unconsumed child/parent script steps.
    """
    return (
        alive.get("parent_alive") is True
        and alive.get("requests_after_release") == alive.get("requests_before_release")
        and (alive.get("steps_left_after_release") or 0) >= 1
    )


def evidence_destination(report_path, report, *, now=None):
    """Keep a passing report on disk; a failed rerun lands next to it instead."""
    if report.get("status") == "passed" or not report_path.exists():
        return report_path
    try:
        existing = json.loads(report_path.read_text())
        previous_status = existing.get("status") if isinstance(existing, dict) else None
    except (OSError, ValueError):
        previous_status = None  # unreadable evidence is preserved, not replaced
    if previous_status not in (None, "passed"):
        return report_path
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return report_path.with_name(
        f"{report_path.stem}.failed-{stamp}{report_path.suffix}"
    )


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
        self.lenient = False
        self.overflow = 0
        self.label_requests = 0
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
                        body.get("model") in FIXTURE_MODELS,
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
                        if provider.lenient and not body.get("tools"):
                            # OMP generates a subagent label with an extra tool-less
                            # model call; answer it without consuming the script.
                            provider.label_requests += 1
                            step = "probe-label"
                        elif provider.steps:
                            step = provider.steps.pop(0)
                        else:
                            require(
                                provider.lenient,
                                "Unexpected model request after script exhausted",
                            )
                            provider.overflow += 1
                            step = "fixture-script-exhausted"
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
                                        "model": body.get("model"),
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
            self.overflow = 0
            self.label_requests = 0
            self.entered.clear()
            self.release.clear()

    def drain(self):
        """Discard an unconsumed script; returns how many steps were left."""
        with self.lock:
            leftover = len(self.steps)
            self.steps = []
        return leftover

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
                                },
                                {
                                    "id": "fixture-smol",
                                    "name": "Deterministic helper fixture",
                                    "reasoning": True,
                                    "input": ["text"],
                                    "contextWindow": 128000,
                                    "maxTokens": 4096,
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                },
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


def schema_keywords(schema):
    """Count schema keywords, not property/definition names or example payloads."""
    counts = {}

    def visit(node):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            counts[key] = counts.get(key, 0) + 1
            if key in ("properties", "$defs", "definitions", "patternProperties"):
                for child in value.values():
                    visit(child)
            elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                for child in value:
                    visit(child)
            elif key in (
                "items",
                "additionalProperties",
                "unevaluatedProperties",
                "contains",
                "not",
                "if",
                "then",
                "else",
                "propertyNames",
            ):
                visit(value)

    visit(schema)
    return dict(sorted(counts.items()))


def schema_census(registered, wire):
    from omp_tandem.runtime_identity import schema_digest

    before, after = schema_keywords(registered), schema_keywords(wire)
    differences = []

    def compare(left, right, path):
        if left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in left or key not in right:
                    differences.append(
                        {
                            "path": pointer,
                            "change": "added" if key not in left else "removed",
                            "registered": left.get(key),
                            "wire": right.get(key),
                        }
                    )
                else:
                    compare(left[key], right[key], pointer)
        elif (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
        ):
            for index, (a, b) in enumerate(zip(left, right, strict=True)):
                compare(a, b, path + "/" + str(index))
        else:
            differences.append(
                {"path": path, "change": "changed", "registered": left, "wire": right}
            )

    compare(registered, wire, "")
    return {
        "registered_sha256": schema_digest(registered),
        "wire_sha256": schema_digest(wire),
        "registered_keywords": before,
        "wire_keywords": after,
        "keyword_count_differences": {
            key: {"registered": before.get(key, 0), "wire": after.get(key, 0)}
            for key in sorted(before.keys() | after.keys())
            if before.get(key, 0) != after.get(key, 0)
        },
        "differences": differences,
    }


def exercise_schema_callbacks(executable, root, agent, report):
    from omp_tandem.bridge import Bridge
    from omp_tandem.native_worker import NATIVE_SCHEMAS
    from omp_tandem.runtime_identity import runtime_identity

    project = root / "schema-project"
    project.mkdir()
    bridge = Bridge(
        root / "schema-state",
        str(executable),
        MODEL,
        project_root=project,
        channel_enabled=False,
        webhook_enabled=False,
        migrate_legacy=False,
    )
    try:
        with (
            Provider() as provider,
            check(report, "registered_wire_schema_callbacks") as evidence,
        ):
            provider.configure(agent)
            invalid = [
                {
                    "outcome": "success",
                    "answer": "invalid",
                    "summary": "invalid",
                    "checks": [{"name": "probe", "result": result}],
                }
                for result in ("failed", "not_run")
            ] + [{"outcome": "blocked", "answer": "invalid", "summary": "invalid"}]
            provider.prepare(
                [
                    ("tandem_work", {"request": {"action": "list"}}),
                    *(("tandem_finish", value) for value in invalid),
                    finish("short-success"),
                    "Ended.",
                ]
            )
            started = bridge.start(
                prompt="Exercise registered callbacks with deterministic fixture arguments.",
                cwd=str(project),
                mode="think",
                execution={"thinking": "off", "timeout_seconds": 60},
            )
            result = await_task(bridge, started["task_id"])
            completed(result, "short-success")
            observed = list(tool_results(provider.requests[-1]).values())
            finishes = [text for name, text in observed if name == "tandem_finish"]
            require(len(finishes) == 4, f"Missing E2 callback results: {observed}")
            for text in finishes[:3]:
                require(
                    'Validation failed for tool "tandem_finish"' in text,
                    f"Invalid outcome was not refused: {text}",
                )
            require(
                "tandem_work" in provider.results(), "Work callback was not exercised"
            )
            wire = {
                tool["function"]["name"]: tool["function"]["parameters"]
                for tool in provider.requests[0]["tools"]
            }
            evidence.update(
                configured_api="openai-completions",
                exercised_apis=["openai-completions"],
                untested_apis="All provider APIs other than openai-completions; not enumerated as supported",
                boundary="Pinned OMP and localhost fixture only; no universal schema parity claim",
                schemas={
                    name: schema_census(NATIVE_SCHEMAS[name], wire[name])
                    for name in ("tandem_finish", "tandem_work")
                },
                callback_results=observed,
                runtime_identity=runtime_identity(),
                task_id=started["task_id"],
            )
            provider.prepare(["No structured finish."])
            missing = bridge.start(
                prompt="Missing-finish fixture.",
                cwd=str(project),
                mode="think",
                execution={"thinking": "off", "timeout_seconds": 60},
            )
            missing_result = await_task(bridge, missing["task_id"])
            require(
                missing_result.get("outcome") != "success",
                "Missing finish incorrectly became success",
            )
            evidence["missing_finish"] = {
                "task_id": missing["task_id"],
                "status": missing_result["status"],
                "outcome": missing_result.get("outcome"),
            }
    finally:
        bridge.shutdown()


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


# --- Native helper (task subagent) capability probes -----------------------------
#
# These probes never run through the production Bridge: Tandem's worker still omits
# `task` from every allowlist, so production delegation stays unavailable. They start
# the official binary through the public SDK with `task` enabled, script both parent
# and child model turns on the localhost fixture, and record what the runtime enforces.
# A probe check "passes" when the measurement executed; whether the measured behavior
# satisfies the helper contract is recorded separately in report["helper_capabilities"].

CHILD_ANSWER = "child-final-answer-517204"
SCOUT_YIELD = (
    "yield",
    {
        "data": {
            "summary": CHILD_ANSWER,
            "files": [],
            "architecture": "compatibility probe",
        }
    },
)
PLAIN_YIELD = ("yield", {"data": CHILD_ANSWER})
PARENT_ANSWER = "parent-final-answer-661938"
PARENT_WRITE = "parent-write-probe-330011\n"
AUTHOR_SENTINEL = "author-interpretation-sentinel-445120"


def helper_settings(agent, *, disabled_agents=(), scout_override="@smol:low"):
    """Isolated OMP settings for helper probes; JSON is a YAML subset."""
    (agent / "config.yml").write_text(
        json.dumps(
            {
                "async": {"enabled": False},
                "modelRoles": {"default": MODEL, "smol": HELPER_MODEL},
                "task": {
                    "agentModelOverrides": {
                        "scout": scout_override,
                        "sonic": "@smol:low",
                    },
                    "maxRecursionDepth": 1,
                    "maxConcurrency": 2,
                    "disabledAgents": list(disabled_agents),
                    "isolation": {"enabled": False},
                },
                "retry": {"modelFallback": False, "enabled": False},
                "prewalk": {"enabled": False},
                "advisor": {"enabled": False},
            }
        )
    )


def spawn(agent_name, task_text, name="Probe"):
    return (
        "task",
        {
            "context": (
                "# Goal\nCompatibility probe.\n# Constraints\nDo exactly the scripted calls.\n"
                "# Contract\nNone.\n" + AUTHOR_SENTINEL
            ),
            "tasks": [{"name": name, "agent": agent_name, "task": task_text}],
        },
    )


def request_tools(request):
    return {tool["function"]["name"] for tool in request.get("tools", [])}


def tool_results(request):
    """Map tool_call_id -> (name, result) visible in one model request's history."""
    calls, results = {}, {}
    for message in request["messages"]:
        for call in message.get("tool_calls", []) or []:
            calls[call["id"]] = call["function"]["name"]
        if message.get("role") == "tool":
            identifier = message.get("tool_call_id")
            results[identifier] = (
                calls.get(identifier, identifier),
                str(message.get("content", "")),
            )
    return results


def request_trace(provider):
    """Compact per-request view: model, effort, advertised tools, last input role."""
    trace = []
    for request in provider.requests:
        last = request["messages"][-1] if request.get("messages") else {}
        trace.append(
            {
                "model": request.get("model"),
                "effort": request.get("reasoning_effort"),
                "tools": sorted(request_tools(request)),
                "last_role": last.get("role"),
                "last": str(last.get("content"))[:160],
            }
        )
    return trace


def split_requests(provider):
    """Parent requests advertise `task`; child requests never should. Returns both."""
    parents, children = [], []
    for request in provider.requests:
        if not request.get("tools"):
            continue  # tool-less label generation calls are counted separately
        (parents if "task" in request_tools(request) else children).append(request)
    return parents, children


def parent_client(executable, project, host_tools):
    from omp_rpc import RpcClient

    client = RpcClient(
        executable=str(executable),
        cwd=project,
        model=MODEL,
        thinking="off",
        no_skills=True,
        no_rules=True,
        no_session=True,
        tools=("read", "grep", "glob", "task"),
        custom_tools=host_tools,
        extra_args=[
            "--no-lsp",
            "--no-extensions",
            "--no-title",
            "--approval-mode=yolo",
        ],
        startup_timeout=45,
        request_timeout=20,
    )
    return client


def run_probe(
    executable,
    project,
    provider,
    steps,
    *,
    probe_tool,
    abort_when_held=False,
    before_stop=None,
):
    """One parent turn with scripted parent+child model responses; returns observations."""
    provider.drain()
    provider.prepare(steps)
    provider.lenient = True
    observed = {
        "parent_message_end": 0,
        "parent_tool_events": [],
        "unknown_notifications": [],
    }
    client = parent_client(executable, project, (probe_tool,))
    client.on_message_end(
        lambda _event: observed.__setitem__(
            "parent_message_end", observed["parent_message_end"] + 1
        )
    )
    client.on_tool_execution_end(
        lambda event: observed["parent_tool_events"].append(
            {
                "tool": event.tool_name,
                "is_error": event.is_error,
                "result": str(event.result)[:1200],
            }
        )
    )
    client.on_unknown_notification(
        lambda payload: observed["unknown_notifications"].append(
            str(getattr(payload, "type", payload))[:200]
        )
    )
    client.start()
    try:
        ended = threading.Event()
        client.on_agent_end(lambda _event: ended.set())
        client.prompt("Helper compatibility probe turn.")
        if abort_when_held:
            require(
                provider.entered.wait(timeout=45),
                "Child never reached the held localhost model response",
            )
            client.abort()
        require(ended.wait(timeout=90), "Parent turn did not end within 90 seconds")
        try:
            observed["subagents"] = client.request_raw("get_subagents")
        except Exception as exc:  # noqa: BLE001 - observation only
            observed["subagents"] = f"{type(exc).__name__}: {exc}"[:300]
        if before_stop is not None:
            # Observations that must be made while the parent process is still alive.
            observed["before_stop"] = before_stop(client)
    finally:
        client.stop()
        provider.lenient = False
    observed["unconsumed_script"] = provider.drain()
    observed["overflow_requests"] = provider.overflow
    observed["label_requests"] = provider.label_requests
    return observed


def helper_probe(executable, root, agent, provider, report):
    from omp_rpc import host_tool

    project = root / "helper-project"
    project.mkdir()
    (project / "read-proof.txt").write_text(READ_TOKEN)
    probe_tool = host_tool(
        name="compatibility_probe",
        description="Parent-only host tool; a child must never see or call it",
        parameters={"type": "object", "properties": {}},
        execute=lambda _params, _ctx: "parent-host-tool-called",
    )
    capabilities = report.setdefault("helper_capabilities", {})

    def gate(name, supported, **evidence):
        capabilities[name] = {"supported": bool(supported), **evidence}

    helper_settings(agent)

    # Probe 1: read-only scout under a restricted parent; mutation, xd transport,
    # parent host tools, nested task, model/thinking routing, usage scope.
    with check(report, "helper_scout_child_boundary") as evidence:
        write_path = project / "child-write.txt"
        shell_path = project / "child-shell.txt"
        steps = [
            spawn(
                "scout",
                "# Target\nread-proof.txt only.\n# Change\nNone.\n# Acceptance\n"
                f"Reply with the literal text {CHILD_ANSWER}.",
                name="ScoutProbe",
            ),
            ("write", {"path": str(write_path), "content": WRITE_TOKEN}),
            (
                "bash",
                {
                    "command": f"/usr/bin/touch {shlex.quote(str(shell_path))}",
                    "timeout": 5,
                },
            ),
            ("read", {"path": str(project / "read-proof.txt")}),
            ("write", {"path": "xd://report_issue", "content": "compatibility probe"}),
            ("compatibility_probe", {}),
            (
                "task",
                {"context": "nested", "tasks": [{"task": "nested spawn attempt"}]},
            ),
            SCOUT_YIELD,
            (
                "write",
                {"path": str(project / "parent-write.txt"), "content": PARENT_WRITE},
            ),
            PARENT_ANSWER,
        ]
        observed = run_probe(
            executable, project, provider, steps, probe_tool=probe_tool
        )
        parents, children = split_requests(provider)
        parent_write = (project / "parent-write.txt").exists()
        if parent_write:
            (project / "parent-write.txt").unlink()
        require(
            len(parents) >= 2, f"Parent did not receive the task result: {len(parents)}"
        )
        require(children, "No child model request reached the fixture")
        child_tools = set().union(*(request_tools(request) for request in children))
        results = {}
        for request in children + parents[1:]:
            for name, content in tool_results(request).values():
                results.setdefault(name, content)
        child_models = sorted({request.get("model") for request in children})
        efforts = sorted({str(request.get("reasoning_effort")) for request in children})
        final_parent = parents[-1]
        parent_history = json.dumps(final_parent["messages"])
        child_history = json.dumps([request["messages"] for request in children])
        evidence.update(
            child_tools=sorted(child_tools),
            child_models=child_models,
            child_reasoning_effort=efforts,
            child_tool_results={key: value[:600] for key, value in results.items()},
            parent_message_end_events=observed["parent_message_end"],
            parent_requests=len(parents),
            child_requests=len(children),
            subagents=str(observed["subagents"])[:1500],
            unknown_notifications=observed["unknown_notifications"][:20],
            request_trace=request_trace(provider),
            overflow_requests=observed["overflow_requests"],
            unconsumed_script=observed["unconsumed_script"],
            label_requests=observed["label_requests"],
            parent_tools=sorted(request_tools(parents[0])),
            parent_write_created_file=parent_write,
        )
        gate(
            "parent_write_tool_is_transport_only",
            "write" in request_tools(parents[0]) and not parent_write,
            parent_tools=sorted(request_tools(parents[0])),
            parent_write_result=next(
                (
                    content
                    for name, content in tool_results(parents[-1]).values()
                    if name == "write"
                ),
                "",
            )[:300],
            note="Enabling `task` adds a `write` tool to a read-only parent; a real filesystem write through it must be refused",
        )
        gate(
            "child_tools_exclude_mutation",
            not {"write", "edit", "bash"} & child_tools
            and not write_path.exists()
            and not shell_path.exists(),
            child_tools=sorted(child_tools),
        )
        unavailable = re.compile(
            r"not found|unknown tool|not available|not enabled|disabled|no such tool",
            re.I,
        )
        gate(
            "child_mutation_refused",
            bool(unavailable.search(results.get("write", "")))
            and bool(unavailable.search(results.get("bash", "")))
            and not write_path.exists()
            and not shell_path.exists(),
            write=results.get("write", "")[:300],
            bash=results.get("bash", "")[:300],
        )
        gate(
            "child_cannot_see_parent_host_tools",
            "compatibility_probe" not in child_tools
            and "parent-host-tool-called" not in child_history,
            result=results.get("compatibility_probe", "")[:300],
        )
        gate(
            "child_cannot_spawn_nested_task",
            "task" not in child_tools,
            result=results.get("task", "")[:300],
        )
        gate(
            "child_model_override_applied",
            child_models == ["fixture-smol"],
            child_models=child_models,
            configured="task.agentModelOverrides.scout=@smol:low; modelRoles.smol="
            + HELPER_MODEL,
        )
        gate(
            "no_extra_model_calls_per_spawn",
            observed["label_requests"] == 0,
            label_requests=observed["label_requests"],
            note="Each spawn issues an additional tool-less label-generation request on the helper model; it is a paid call outside the scripted child turn",
        )
        gate(
            "child_thinking_low_observed",
            efforts == ["low"],
            reasoning_effort=efforts,
            note="Requested :low on a reasoning-capable fixture model; observed provider payload",
        )
        gate(
            "child_transcript_absent_from_parent_input",
            CHILD_ANSWER in parent_history
            and READ_TOKEN not in parent_history
            and WRITE_TOKEN.strip() not in parent_history,
            note="Parent final request contains the child result, not the child's tool transcript",
        )
        gate(
            "parent_stream_usage_scope_exclusive",
            observed["parent_message_end"] == len(parents),
            parent_message_end_events=observed["parent_message_end"],
            parent_requests=len(parents),
            child_requests=len(children),
            note="Parent message_end events equal parent model requests: child usage is not folded into the parent RPC stream",
        )
        gate(
            "xd_transport_observed_without_filesystem_write",
            set(project.iterdir()) == {project / "read-proof.txt"},
            observed_result=results.get("write", "")[:300],
            project_entries=sorted(path.name for path in project.iterdir()),
            note="Observation: a child write to xd://report_issue must not create project files; the tool result text is recorded verbatim",
        )
        gate(
            "author_sentinel_visible_to_child",
            AUTHOR_SENTINEL in child_history,
            note="Batch `context` is injected verbatim into every child; independent-first review must not pass author material through it",
        )
        require(CHILD_ANSWER in parent_history, "Child result did not reach the parent")

    # Probe 2: a FULL-access agent (sonic) under the same restricted parent.
    with check(report, "helper_sonic_inherits_or_intersects_parent_tools") as evidence:
        write_path = project / "sonic-write.txt"
        steps = [
            spawn(
                "sonic",
                "# Target\nsonic-write.txt\n# Change\nWrite the file.\n# Acceptance\n"
                f"Reply with {CHILD_ANSWER}.",
                name="SonicProbe",
            ),
            ("write", {"path": str(write_path), "content": WRITE_TOKEN}),
            PLAIN_YIELD,
            PARENT_ANSWER,
        ]
        run_probe(executable, project, provider, steps, probe_tool=probe_tool)
        parents, children = split_requests(provider)
        child_tools = set().union(*(request_tools(request) for request in children))
        results = {}
        for request in children + parents[1:]:
            for name, content in tool_results(request).values():
                results.setdefault(name, content)
        evidence.update(
            request_trace=request_trace(provider),
            child_tools=sorted(child_tools),
            write_result=results.get("write", "")[:400],
            file_written=write_path.exists(),
        )
        gate(
            "child_tools_intersect_parent_restriction",
            "write" not in child_tools and not write_path.exists(),
            child_tools=sorted(child_tools),
            file_written=write_path.exists(),
            note="Parent ran with --tools read,grep,glob,task; a sonic child that can still write proves child tools come from the agent definition, not the parent allowlist",
        )

    # Probe 3: project-level agent substitution and task.disabledAgents.
    with check(report, "helper_project_agent_override_and_disabled_agents") as evidence:
        agents_dir = project / ".omp" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "scout.md").write_text(
            "---\nname: scout\ndescription: substituted scout\n"
            'tools:\n  - read\n  - write\n  - bash\nmodel:\n  - "@default"\n'
            "thinkingLevel: high\n---\nSubstituted project scout.\n"
        )
        write_path = project / "substituted-write.txt"
        steps = [
            spawn(
                "scout", f"# Target\nWrite.\n# Acceptance\n{CHILD_ANSWER}", name="Subst"
            ),
            ("write", {"path": str(write_path), "content": WRITE_TOKEN}),
            SCOUT_YIELD,
            PARENT_ANSWER,
        ]
        run_probe(executable, project, provider, steps, probe_tool=probe_tool)
        parents, children = split_requests(provider)
        child_tools = set().union(*(request_tools(request) for request in children))
        child_models = sorted({request.get("model") for request in children})
        override_result = ""
        for request in parents[1:]:
            for name, content in tool_results(request).values():
                if name == "task":
                    override_result = content
        evidence.update(
            child_tools=sorted(child_tools),
            child_models=child_models,
            file_written=write_path.exists(),
            override_task_result=override_result[:800],
            request_trace=request_trace(provider),
        )
        gate(
            "project_agent_definition_cannot_widen_bundled_scout",
            "write" not in child_tools and not write_path.exists(),
            child_tools=sorted(child_tools),
            child_models=child_models,
            child_requests=len(children),
            task_result=override_result[:300],
            note="A project .omp/agents/scout.md with write/bash was present while the parent ran with --no-extensions --no-skills --no-rules",
        )
        shutil.rmtree(project / ".omp")
        helper_settings(agent, disabled_agents=("scout",))
        steps = [
            spawn(
                "scout",
                f"# Target\nnone\n# Acceptance\n{CHILD_ANSWER}",
                name="Disabled",
            ),
            PARENT_ANSWER,
        ]
        run_probe(executable, project, provider, steps, probe_tool=probe_tool)
        parents, children = split_requests(provider)
        task_result = ""
        for request in parents[1:]:
            for name, content in tool_results(request).values():
                if name == "task":
                    task_result = content
        evidence["disabled_agent_task_result"] = task_result[:600]
        gate(
            "disabled_agents_enforced_before_spawn",
            not children and "disabled" in task_result.lower(),
            task_result=task_result[:300],
            child_requests=len(children),
        )
        helper_settings(agent)

    # Probe 4: configuration changed between two spawns of one parent session.
    with check(report, "helper_config_reload_between_spawns") as evidence:
        provider.drain()
        provider.lenient = True
        provider.prepare(
            [
                spawn("scout", f"# Acceptance\n{CHILD_ANSWER}", name="First"),
                SCOUT_YIELD,
                "first spawn done",
            ]
        )
        client = parent_client(executable, project, (probe_tool,))
        client.start()
        try:
            client.prompt_and_wait("First spawn.", timeout=90)
            _first_parents, first_children = split_requests(provider)
            first_models = sorted({request.get("model") for request in first_children})
            helper_settings(agent, scout_override=MODEL)
            provider.drain()
            provider.prepare(
                [
                    spawn("scout", f"# Acceptance\n{CHILD_ANSWER}", name="Second"),
                    SCOUT_YIELD,
                    "second spawn done",
                ]
            )
            client.prompt_and_wait("Second spawn.", timeout=90)
            _second_parents, second_children = split_requests(provider)
            second_models = sorted(
                {request.get("model") for request in second_children}
            )
        finally:
            client.stop()
            provider.lenient = False
            provider.drain()
            helper_settings(agent)
        evidence.update(
            first_child_models=first_models, second_child_models=second_models
        )
        gate(
            "task_wide_settings_snapshot",
            first_models == second_models == ["fixture-smol"],
            first_child_models=first_models,
            second_child_models=second_models,
            changed_to=MODEL,
            note="Equal models across spawns means the child selection is pinned for the session; a change proves per-spawn settings reload",
        )

    # Probe 5: parent abort while the child model stream is held open.
    with check(report, "helper_parent_abort_stops_child") as evidence:
        steps = [
            spawn("scout", f"# Acceptance\n{CHILD_ANSWER}", name="Held"),
            "hold",
            SCOUT_YIELD,
            PARENT_ANSWER,
        ]

        def release_while_alive(client):
            # The parent process is still running here: release the held child
            # response and watch for late model requests before any process stop.
            before = provider.total_requests
            provider.release.set()
            time.sleep(4)
            try:
                subagents = client.request_raw("get_subagents")
            except Exception as exc:  # noqa: BLE001 - observation only
                subagents = f"{type(exc).__name__}: {exc}"[:300]
            return {
                "requests_before_release": before,
                "requests_after_release": provider.total_requests,
                "steps_left_after_release": len(provider.steps),
                "subagents_after_release": str(subagents)[:1500],
                "parent_alive": not isinstance(subagents, str),
            }

        observed = run_probe(
            executable,
            project,
            provider,
            steps,
            probe_tool=probe_tool,
            abort_when_held=True,
            before_stop=release_while_alive,
        )
        alive = observed["before_stop"]
        evidence.update(
            **alive,
            unconsumed_script=observed["unconsumed_script"],
            subagents=str(observed["subagents"])[:1500],
        )
        gate(
            "parent_abort_prevents_late_child_delivery",
            abort_gate_supported(alive),
            **alive,
            note="Observed before client.stop() and only counted while the parent RPC session still answered get_subagents: after abort, releasing the held child response produced no further model request and the child answer and parent answer steps stayed unconsumed; an unresponsive parent fails this gate regardless of request counts",
        )

    required = (
        "child_tools_exclude_mutation",
        "child_mutation_refused",
        "child_cannot_see_parent_host_tools",
        "child_cannot_spawn_nested_task",
        "child_model_override_applied",
        "child_tools_intersect_parent_restriction",
        "project_agent_definition_cannot_widen_bundled_scout",
        "disabled_agents_enforced_before_spawn",
        "task_wide_settings_snapshot",
        "parent_abort_prevents_late_child_delivery",
        "parent_stream_usage_scope_exclusive",
        "no_extra_model_calls_per_spawn",
    )
    missing = [
        name for name in required if not capabilities.get(name, {}).get("supported")
    ]
    report["delegation"] = {
        "available": False,
        "reason": (
            "Production worker allowlists omit `task`; helper gates not satisfied: "
            + ", ".join(missing)
            if missing
            else "Production worker allowlists omit `task`; all measured gates passed but "
            "budget admission, tree shutdown acknowledgement and snapshot-bound child readers "
            "are not implemented in Tandem"
        ),
        "unsatisfied_gates": missing,
        "measured_gates": list(required),
    }


def recorded_tool_results(results):
    """Do not export even disposable fixture claim capabilities as evidence."""
    recorded = dict(results)
    if "tandem_work" in recorded:
        work = json.loads(recorded["tandem_work"])
        claim = work.get("claim") or {}
        if "token" in claim:
            claim["token"] = "<redacted-fixture-capability>"
        recorded["tandem_work"] = json.dumps(work, ensure_ascii=False)
    return recorded


def fixture_command(arguments, *, cwd, evidence):
    result = subprocess.run(
        [str(value) for value in arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    evidence.append(
        {
            "command": [str(value) for value in arguments],
            "cwd": str(cwd),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    require(result.returncode == 0, f"Fixture command failed: {evidence[-1]}")
    return result.stdout.strip()


def install_fixture_runtime(root, wheel, report):
    with check(report, "installed_runtime_setup") as evidence:
        uv = shutil.which("uv")
        require(uv is not None, "uv required to install locked fixture dependencies")
        evidence.update(wheel=str(wheel), wheel_sha256=digest(wheel), commands=[])
        commands = evidence["commands"]
        python = root / "installed" / "bin" / "python"
        fixture_command(
            [uv, "venv", "--python", sys.executable, python.parent.parent],
            cwd=ROOT,
            evidence=commands,
        )
        dependencies = fixture_command(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            cwd=ROOT,
            evidence=commands,
        )
        requirements = root / "requirements.txt"
        requirements.write_text(dependencies + "\n")
        fixture_command(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "--require-hashes",
                "-r",
                requirements,
            ],
            cwd=ROOT,
            evidence=commands,
        )
        fixture_command(
            [uv, "pip", "install", "--python", python, "--no-deps", wheel],
            cwd=ROOT,
            evidence=commands,
        )
        return python


async def installed_shared_flow(root, executable, report_path):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from pydantic_core import to_jsonable_python

    from omp_tandem.api import build_server
    from omp_tandem.bridge import Bridge
    from omp_tandem.runtime_identity import runtime_identity

    identity = runtime_identity()
    require(identity["distribution_origin"] == "installed_wheel", str(identity))
    require(identity["exact_build"] != "unknown", "Installed RECORD not verified")
    project, state = root / "project", root / "state"
    project.mkdir(parents=True)
    evidence = {"runtime_identity": identity, "commands": [], "flows": []}

    def git(*args):
        return fixture_command(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=project,
            evidence=evidence["commands"],
        )

    git("init", "-q")
    (project / "module.txt").write_text("before\n")
    git("add", "module.txt")
    git("commit", "-qm", "fixture: initial")
    bridges = [
        Bridge(
            state,
            str(executable),
            MODEL,
            project_root=project,
            channel_enabled=False,
            webhook_enabled=False,
            migrate_legacy=False,
            work_participant=actor,
        )
        for actor in ("claude", "omp")
    ]
    author, reviewer = bridges

    async def call(client, request, **presentation):
        result = await client.call_tool(
            "tandem_work", {"request": request, "view": "full", **presentation}
        )
        return to_jsonable_python(result.data)

    async def mutate(client, identifier, action, **values):
        current = await call(client, {"action": "get", "work_id": identifier})
        return await call(
            client,
            {
                "action": action,
                "work_id": identifier,
                "expected_revision": current["revision"],
                "operation_id": str(uuid4()),
                **values,
            },
        )

    def counts():
        with author.tasks.connect() as db:
            return [
                db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("tasks", "work_attempts")
            ]

    try:
        async with (
            Client(build_server(author)) as claude,
            Client(build_server(reviewer)) as omp,
        ):
            with Provider() as provider:
                provider.configure(Path(os.environ["PI_CODING_AGENT_DIR"]))
                for scenario in ("recovery", "clarification"):
                    created = await call(
                        claude,
                        {
                            "action": "create",
                            "expected_revision": 0,
                            "operation_id": str(uuid4()),
                            "plan": {
                                "title": "Installed runtime fixture",
                                "goal": "Change one module",
                                "acceptance": ["Exact committed module is reviewed"],
                                "steps": [
                                    {
                                        "id": "change",
                                        "title": "Change module",
                                        "goal": "Write fixture output",
                                        "owner": "claude",
                                        "reviewer": "omp",
                                        "owned_files": ["module.txt"],
                                        "depends_on": [],
                                        "acceptance": ["Committed output matches"],
                                    }
                                ],
                            },
                        },
                    )
                    identifier = created["work_id"]
                    await mutate(claude, identifier, "agree")
                    await mutate(omp, identifier, "agree")
                    await mutate(claude, identifier, "claim", step_id="change")
                    (project / "module.txt").write_text(scenario + "\n")
                    git("add", "module.txt")
                    git("commit", "-qm", "fixture: " + scenario)
                    commit = git("rev-parse", "HEAD")
                    sentinel = "WITHHELD-AUTHOR-INTERPRETATION-947513"
                    submitted = await mutate(
                        claude,
                        identifier,
                        "submit",
                        step_id="change",
                        commit=commit,
                        note=sentinel,
                        evidence=[sentinel],
                    )
                    submission = submitted["steps"][0]["submission"]["submission_id"]
                    claim = (
                        "tandem_work",
                        {
                            "request": {
                                "action": "claim",
                                "work_id": identifier,
                                "step_id": "change",
                                "expected_revision": submitted["revision"],
                                "operation_id": str(uuid4()),
                            }
                        },
                    )
                    if scenario == "recovery":
                        # Independently inspect exact committed bytes before saving the finding.
                        observed = git("show", f"{commit}:module.txt")
                        require(
                            observed == scenario, "Committed fixture output differs"
                        )
                        steps = [
                            claim,
                            (
                                "tandem_publish_artifact",
                                {
                                    "name": "finding",
                                    "content": f"Read {commit}:module.txt = {observed}",
                                },
                            ),
                            "Interrupted before structured finish.",
                        ]
                    else:
                        steps = [
                            claim,
                            (
                                "tandem_ask",
                                {
                                    "question": "Need an unchanged caller",
                                    "context": json.dumps(
                                        {"requested_paths": ["caller.py"]}
                                    ),
                                },
                            ),
                            (
                                "tandem_finish",
                                {
                                    "outcome": "blocked",
                                    "answer": "New snapshot required",
                                    "summary": "Missing context",
                                    "blockers": ["Need caller.py in new snapshot"],
                                },
                            ),
                            "Ended.",
                        ]
                    provider.prepare(steps)
                    started = to_jsonable_python(
                        (
                            await omp.call_tool(
                                "tandem_start",
                                {
                                    "cwd": str(project),
                                    "mode": "think",
                                    "prompt": "Installed fixture " + scenario,
                                    "execution": {
                                        "thinking": "off",
                                        "timeout_seconds": 60,
                                    },
                                },
                            )
                        ).data
                    )
                    result = await asyncio.to_thread(
                        await_task, reviewer, started["task_id"]
                    )
                    require(
                        sentinel not in json.dumps(provider.requests),
                        "Author interpretation reached independent model input",
                    )
                    require(
                        result["runtime_identity"]["distribution_origin"]
                        == "installed_wheel",
                        "Task result loaded checkout instead of wheel",
                    )
                    row = {
                        "scenario": scenario,
                        "work_id": identifier,
                        "commit": commit,
                        "submission_id": submission,
                        "task_result": result,
                        "withheld_input": True,
                        "tool_results": recorded_tool_results(provider.results()),
                    }
                    evidence["flows"].append(row)
                    if scenario == "clarification":
                        ask = json.loads(provider.results()["tandem_ask"])
                        require(
                            ask["clarification_requires_new_snapshot"],
                            "Independent clarification did not require new snapshot",
                        )
                        try:
                            await claude.call_tool(
                                "tandem_reply",
                                {
                                    "task_id": started["task_id"],
                                    "question_id": ask["question_id"],
                                    "answer": sentinel,
                                },
                            )
                        except ToolError as exc:
                            row["late_reply_refused"] = str(exc)
                        else:
                            raise RuntimeError("Independent late reply was accepted")
                        require(
                            result["outcome"] == "blocked",
                            "Clarification stage did not close blocked",
                        )
                        continue
                    require(
                        result["status"] == "failed"
                        and result.get("outcome") != "success",
                        "Missing finish failure was erased",
                    )
                    finding = next(
                        item
                        for item in result["provisional_artifacts"]
                        if item["name"] == "finding"
                    )
                    attempt = result["execution"]["attempt"]["originated_claims"][0]
                    require(
                        attempt["settled"]["teardown_confirmed"],
                        "Origin stop not confirmed",
                    )
                    baseline, requests = counts(), provider.total_requests
                    cli = [
                        sys.executable,
                        "-I",
                        "-m",
                        "omp_tandem.work_daemon",
                        "--project-root",
                        project,
                        "--state-dir",
                        state,
                    ]
                    handoff = json.loads(
                        fixture_command(
                            [
                                *cli,
                                "successor",
                                attempt["attempt_id"],
                                "--host",
                                author.channel.owner,
                                "--principal",
                                "claude",
                                "--note",
                                "Fixture report-only closure after stopped task",
                            ],
                            cwd=root,
                            evidence=evidence["commands"],
                        )
                    )
                    recovered = await mutate(
                        claude, identifier, "recover", step_id="change"
                    )
                    require(
                        recovered["recovery"]["attempt_id"] == attempt["attempt_id"],
                        "Recovery changed claim",
                    )
                    require(
                        sentinel not in json.dumps(recovered),
                        "Recovery disclosed author input",
                    )
                    await mutate(
                        claude,
                        identifier,
                        "report",
                        step_id="change",
                        submission_id=submission,
                        resolution="success",
                        note="Independent finding preserved before interruption",
                        evidence=[finding["artifact_id"]],
                    )
                    compared = await mutate(
                        claude,
                        identifier,
                        "compare",
                        step_id="change",
                        submission_id=submission,
                    )
                    require(
                        sentinel in json.dumps(compared),
                        "Comparison did not open author material",
                    )
                    accepted = await mutate(
                        claude,
                        identifier,
                        "accept",
                        step_id="change",
                        submission_id=submission,
                        note="Exact finding supports committed output",
                        evidence=[finding["artifact_id"]],
                    )
                    require(
                        accepted["status"] == "completed"
                        and accepted["result"]["commit"] == commit,
                        "Exact verdict did not complete fixture",
                    )
                    require(
                        counts() == baseline and provider.total_requests == requests,
                        "Administrative closure launched work",
                    )
                    row.update(
                        successor=handoff,
                        recovered_attempt_id=attempt["attempt_id"],
                        accepted=accepted,
                        closure_counts=baseline,
                        closure_model_requests=0,
                    )
                    row["markdown"] = fixture_command(
                        [*cli, "show", identifier, "--format", "markdown"],
                        cwd=root,
                        evidence=evidence["commands"],
                    )
        evidence["runtime_identity"] = runtime_identity()
        evidence["boundaries"] = {
            "mode": "manual/disclosed fixture; protocol checks are not OS confinement",
            "transport": "FastMCP Client and real pinned OMP RPC plus localhost HTTP/SSE; successor/show CLI",
            "claude_model": "not invoked; Claude seat driven through MCP",
            "claude_cost": "unknown",
            "external_models": "not exercised",
            "application": "not_recorded; no apply or assessment command executed",
        }
        return evidence
    finally:
        for bridge in bridges:
            await asyncio.to_thread(bridge.shutdown)
        report_path.write_text(json.dumps(evidence, indent=2) + "\n")


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
    parser.add_argument(
        "--installed-wheel",
        type=Path,
        help="Install this built wheel into a new temporary environment and exercise shared flow",
    )
    args = parser.parse_args()
    report_path = args.report.resolve()
    report = {
        "schema_version": 1,
        "status": "running",
        "checks": [],
        "provenance": {
            "command": [sys.executable, *sys.argv],
            "verifier_sha256": digest(Path(__file__)),
            "lock_sha256": digest(ROOT / "uv.lock"),
            "environment_boundary": "Provider processes use isolated_environment allowlist; installation uses uv locked dependencies before isolation",
        },
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
            "Compatibility command exceeded its 600-second total deadline"
        )

    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.alarm(600)
    try:
        executable = binary(args, manifest, report)
        report["phase"] = "setup"
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
            installed_python = (
                install_fixture_runtime(root, args.installed_wheel.resolve(), report)
                if args.installed_wheel
                else None
            )
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
                    report["phase"] = "probes"
                    exercise_schema_callbacks(executable, root, agent, report)
                    provider.configure(agent)
                    exercise(executable, root, provider, report)
                    helper_probe(executable, root, agent, provider, report)
                if installed_python:
                    with check(report, "installed_runtime_shared_flow") as evidence:
                        child_report = root / "installed-result.json"
                        evidence["commands"] = []
                        try:
                            fixture_command(
                                [
                                    installed_python,
                                    "-I",
                                    Path(__file__).resolve(),
                                    "--installed-child",
                                    root / "shared",
                                    executable,
                                    child_report,
                                ],
                                cwd=root,
                                evidence=evidence["commands"],
                            )
                        finally:
                            if child_report.is_file():
                                evidence["result"] = json.loads(
                                    child_report.read_text()
                                )
        report["phase"] = "complete"
        report["status"] = "passed"
    except Exception as exc:
        logger.exception("Real OMP compatibility verification failed")
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        report["seconds"] = round(time.monotonic() - started, 3)
        # Acquisition, environment and probe failures are different results; an
        # unsupported capability is a measured gate, never a failure class.
        report["failure_class"] = failure_class(report)
        report["probes"] = probe_status(report)
        destination = evidence_destination(report_path, report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
        # Bounded CI log evidence, including failures, without another action pin.
        print(json.dumps(report, indent=2))
        print(f"Compatibility evidence: {destination}", file=sys.stderr)
        if destination != report_path:
            print(
                f"Existing passing evidence kept at {report_path}; this failed run "
                "was written separately",
                file=sys.stderr,
            )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--installed-child":
        asyncio.run(
            installed_shared_flow(
                Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
            )
        )
        raise SystemExit(0)
    raise SystemExit(main())
