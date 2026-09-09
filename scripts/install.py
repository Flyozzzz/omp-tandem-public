# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prepare a private runtime and optionally register a standalone MCP server."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_tandem.bootstrap import BootstrapError, doctor, prepare_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare omp-tandem and optionally register it with Claude Code or Codex.",
        epilog=(
            "Launch scripts/install.py with isolated Python through uv as documented in README. "
            "Plugin users need no second standalone registration. --client none returns a standard "
            "MCP command/environment plan for other clients. Dependencies use the same private "
            "cache as server.py, outside the checkout. Install uv and OMP once; provider login "
            "belongs to you and is never read or changed here. --check does not prepare dependencies "
            "or change client configuration (the outer uv launcher may download Python). "
            "Claude local scope uses your invocation directory; Codex supports user scope only. "
            "The launcher inherits its actual cwd; task cwd never selects project isolation. "
            "Existing registrations are never automatically removed. --json emits one object; "
            "subprocess output goes to stderr. For Claude push use scripts/launch.py; "
            "scripts/launch.py --delivery poll keeps polling. Channel consent and organization "
            "policy still apply; no permission bypass is added."
        ),
    )
    parser.add_argument(
        "--client", choices=("claude", "codex", "none"), default="claude"
    )
    parser.add_argument(
        "--scope",
        choices=("user", "local"),
        help="Claude: user (default) or local; Codex: user only; none: omit",
    )
    parser.add_argument(
        "--model", help="OMP model override; otherwise inherit OMP configuration"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable plan/status; diagnostics on stderr",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--no-register",
        action="store_true",
        help="prepare dependencies and print a plan without changing client configuration",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="inspect prerequisites only; no dependency installation, login checks or registration",
    )
    return parser


def _run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, cwd=cwd, check=False, capture_output=True, text=True
    )
    for output in (result.stdout, result.stderr):
        if output:
            print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package = Path(__file__).resolve().parents[1]
    invocation = Path.cwd()
    scope = args.scope or ("user" if args.client != "none" else None)
    report = {
        "schema_version": 1,
        "status": "blocked",
        "code": "preflight",
        "package": str(package),
        "client": args.client,
        "scope": scope,
        "omp_login": "required_not_checked",
        "authentication": "not_checked",
        "tools": {},
        "missing": [],
        "commands": [],
        "launch": None,
        "mcp": None,
    }

    def finish(exit_code, status, code, message, next_action):
        report.update(
            status=status,
            code=code,
            message=message,
            next_action=next_action,
            exit_code=exit_code,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            stream = sys.stderr if exit_code else sys.stdout
            print(message, file=stream)
            for action in report["commands"]:
                print(f"Run from {shlex.quote(action['cwd'])}:", file=stream)
                print(shlex.join(action["argv"]), file=stream)
            if report["mcp"] is not None:
                print(
                    "MCP configuration (preserve the client's project cwd):",
                    file=stream,
                )
                print(json.dumps(report["mcp"], ensure_ascii=False), file=stream)
        return exit_code

    if (args.client == "codex" and scope != "user") or (
        args.client == "none" and args.scope is not None
    ):
        return finish(
            2,
            "blocked",
            "unsupported_scope",
            "Codex supports user scope only; --client none does not accept --scope.",
            "choose_supported_scope",
        )
    prerequisites = doctor(package)
    report["tools"] = prerequisites["tools"]
    report["missing"] = prerequisites["missing"]
    report["requirements"] = prerequisites["requirements"]
    if not (package / "server.py").is_file():
        report["missing"].append("server.py")
    client = shutil.which(args.client) if args.client != "none" else None
    if args.client != "none":
        report["tools"][args.client] = client
        if client is None and not args.no_register:
            report["missing"].append(args.client)
    tools = report["tools"]
    server_args = [
        "run",
        "--no-project",
        "--python",
        ">=3.12",
        "python",
        "-I",
        str(package / "server.py"),
    ]
    if tools["omp"]:
        # Preserve the stable launcher symlink instead of a version-specific target.
        server_args.extend(["--omp", str(Path(tools["omp"]).absolute())])
    if args.model is not None:
        server_args.extend(["--model", args.model])
    report["mcp"] = {"command": tools["uv"] or "uv", "args": server_args, "env": {}}
    server_command = [report["mcp"]["command"], *server_args]
    if args.client != "none":
        command = [client or args.client, "mcp", "add"]
        if args.client == "claude":
            command.extend(["--scope", scope, "--transport", "stdio"])
        command.extend(["omp-tandem", "--", *server_command])
        report["commands"] = [
            {"action": "register", "argv": command, "cwd": str(invocation)}
        ]
    if args.client == "claude" and client:
        report["launch"] = {
            "poll": {"argv": [client], "cwd": str(invocation)},
            "push": {
                "argv": [
                    tools["uv"] or "uv",
                    "run",
                    "--no-project",
                    "--python",
                    ">=3.12",
                    "python",
                    "-I",
                    str(package / "scripts" / "launch.py"),
                ],
                "cwd": str(invocation),
                "requires_channel_consent": True,
                "organization_policy_applies": True,
            },
        }
    if report["missing"]:
        return finish(
            2,
            "blocked",
            "missing_prerequisites",
            "Missing prerequisites: "
            + ", ".join(report["missing"])
            + ". Install tools once and use a complete checkout; provider authentication was not checked.",
            "install_prerequisites",
        )
    if args.check:
        return finish(
            0,
            "ready",
            "ok",
            "Local prerequisites available. Provider authentication was not checked; nothing was installed or registered.",
            "install",
        )
    try:
        report["python"] = str(prepare_runtime(package))
    except (BootstrapError, OSError) as exc:
        return finish(
            3, "failed", "dependency_install_failed", str(exc), "inspect_error"
        )
    if args.no_register or args.client == "none":
        return finish(
            0,
            "prepared",
            "ok",
            "Runtime prepared. Client configuration was not changed; plugin users need no standalone registration.",
            "configure_client" if args.client == "none" else "register",
        )
    if args.client == "codex":
        # Codex's add command upserts names. Inspect quietly; never print a registry
        # that may contain another server's private environment values.
        try:
            registry = subprocess.run(
                [client, "mcp", "list", "--json"],
                cwd=invocation,
                capture_output=True,
                text=True,
                check=False,
            )
            if registry.returncode:
                raise ValueError("Registry inspection failed")
            entries = json.loads(registry.stdout)
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                for entry in entries
            ):
                raise ValueError("Unrecognized registry format")
        except (OSError, ValueError):
            return finish(
                4,
                "blocked",
                "registration_inspection_failed",
                "Could not safely inspect Codex registrations; no registration was changed.",
                "inspect_registration",
            )
        if any(entry["name"] == "omp-tandem" for entry in entries):
            return finish(
                4,
                "blocked",
                "registration_exists",
                "Codex already has an omp-tandem registration; it was not replaced. Resolve it explicitly after active work finishes.",
                "inspect_registration_conflict",
            )
    try:
        registered = _run_command(command, invocation)
    except OSError as exc:
        return finish(
            4,
            "failed",
            "registration_failed",
            f"Could not run {args.client}: {exc}",
            "inspect_error",
        )
    if registered.returncode:
        report["command_exit_code"] = registered.returncode
        return finish(
            4,
            "failed",
            "registration_failed",
            "Registration failed. No existing registration was removed. Inspect stderr and resolve conflicts explicitly before retrying.",
            "inspect_registration_conflict",
        )
    return finish(
        0,
        "installed",
        "ok",
        f"Registered omp-tandem with {args.client} ({scope} scope). Start a new client session.",
        "restart_client",
    )


if __name__ == "__main__":
    raise SystemExit(main())
