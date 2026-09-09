# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Launch Claude with verified push support, or force the existing polling mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Launch Claude CLI with omp-tandem channel support. Install the plugin or use scripts/install.py first.",
        epilog="Push uses a development-channel consent dialog unless --approved-plugin is supplied. Organization channel policy still applies. No tool-permission bypass flags are added. Forward Claude arguments after --. Keep the session open to receive events; server output stays polling until a receipt probe is acknowledged. Launch scripts/launch.py with isolated Python through uv as documented in README; use --delivery poll for polling. Plugin users need no additional standalone MCP registration.",
    )
    parser.add_argument("--delivery", choices=("push", "poll"), default="push")
    parser.add_argument(
        "--approved-plugin",
        help="approved plugin@marketplace, e.g. omp-tandem@omp-tandem; uses --channels instead of the development flag",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=0,
        help="127.0.0.1 webhook port after receipt confirmation; default selects a separate port per session",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="push task events without an HTTP listener",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print command/environment plan as JSON without launching Claude",
    )
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Use macOS/Linux, or WSL with Linux-installed Claude and OMP")
    if not 0 <= args.webhook_port <= 65535:
        parser.error("--webhook-port must be 0..65535")
    if args.approved_plugin and not re.fullmatch(
        r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", args.approved_plugin
    ):
        parser.error("--approved-plugin must be plugin@marketplace")
    claude = shutil.which("claude")
    if claude is None:
        parser.error("claude is not available on PATH")
    forwarded = (
        args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
    )
    command = [claude]
    changes = {"OMP_TANDEM_CHANNEL": "0" if args.delivery == "poll" else "1"}
    if args.delivery == "push":
        if args.approved_plugin:
            command.extend(["--channels", "plugin:" + args.approved_plugin])
        else:
            command.extend(
                ["--dangerously-load-development-channels", "server:omp-tandem"]
            )
        changes.update(
            MCP_PROTOCOL_NEGOTIATION="legacy",
            OMP_TANDEM_WEBHOOK="0" if args.no_webhook else "1",
            OMP_TANDEM_WEBHOOK_PORT=str(args.webhook_port),
        )
    command.extend(forwarded)
    if args.check:
        print(
            json.dumps(
                {
                    "delivery_requested": args.delivery,
                    "command": command,
                    "environment_overrides": changes,
                    "organization_policy_bypassed": False,
                    "adds_tool_permission_bypass": False,
                }
            )
        )
        return 0
    try:
        os.execvpe(claude, command, {**os.environ, **changes})
    except OSError as exc:
        print(f"Could not launch Claude: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
