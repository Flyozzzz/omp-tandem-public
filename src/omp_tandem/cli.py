"""Operator commands and the stdio MCP entry point."""

import argparse
import json
import os
import shutil
from pathlib import Path

from . import migration
from .api import build_server
from .binding import RuntimeOptions
from .bridge import Bridge
from .prompts import INSTRUCTIONS
from .workspace import resolve_scope


def main(argv=None):
    parser = argparse.ArgumentParser(description=INSTRUCTIONS)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "OMP_TANDEM_STATE_DIR", str(Path.home() / ".local/state/omp-tandem")
            )
        ),
        help="Shared state base; each launch project gets a separate hashed namespace",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Operator override; otherwise use CLAUDE_PROJECT_DIR or the launch directory",
    )
    parser.add_argument(
        "--scope-info",
        action="store_true",
        help="Print scope identity without importing history or starting MCP",
    )
    parser.add_argument(
        "--no-legacy-import",
        action="store_true",
        help="Keep legacy global history untouched and do not copy it into this project",
    )
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="Explicit operator migration without the automatic 64 MiB copy budget; print JSON and exit",
    )
    parser.add_argument(
        "--legacy-cwd",
        type=Path,
        action="append",
        default=[],
        help="With --migrate-only: explicitly assign an old cwd/worktree to this project (repeatable)",
    )
    parser.add_argument(
        "--legacy-context",
        action="append",
        default=[],
        help="With --migrate-only: explicitly assign an otherwise unattached legacy context ID",
    )
    parser.add_argument("--omp", default=shutil.which("omp") or "omp")
    parser.add_argument(
        "--model",
        default=os.environ.get("OMP_TANDEM_MODEL"),
        help="Provider/model override; otherwise use the model configured in OMP.",
    )
    parser.add_argument(
        "--disable-channel",
        action="store_true",
        help="Force polling; never probe or open a webhook listener",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="Keep Claude Code Channels task events but disable the HTTP webhook",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=os.environ.get("OMP_TANDEM_WEBHOOK_PORT", "0"),
        help="Loopback webhook port after channel acknowledgment; 0 selects a per-session port",
    )
    parser.add_argument(
        "--work-participant",
        choices=("claude", "omp"),
        default="claude",
        help="Operator-selected participant seat for shared tasks; not a model identity claim",
    )
    parser.add_argument(
        "--work-token-file",
        type=Path,
        help="Private managed-attempt capability; exposes only the restricted shared-work API",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    if not 0 <= args.webhook_port <= 65535:
        parser.error("--webhook-port must be 0..65535")
    if (args.legacy_cwd or args.legacy_context) and not args.migrate_only:
        parser.error("--legacy-cwd/--legacy-context require --migrate-only")
    if args.scope_info and args.migrate_only:
        parser.error("--scope-info and --migrate-only are mutually exclusive")
    if args.scope_info:
        scope = resolve_scope(args.state_dir, args.project_root)
        print(json.dumps({**scope.info(), "state_directory": str(scope.directory)}))
        return

    def enabled_setting(name, default):
        value = os.environ.get(name, default).lower()
        if value not in ("auto", "1", "true", "yes", "on", "0", "false", "no", "off"):
            parser.error(f"{name} must be auto, 1 or 0")
        return value not in ("0", "false", "no", "off")

    channel_enabled = not args.disable_channel and enabled_setting(
        "OMP_TANDEM_CHANNEL", "auto"
    )
    webhook_enabled = not args.no_webhook and enabled_setting("OMP_TANDEM_WEBHOOK", "1")
    options = RuntimeOptions(
        args.state_dir,
        args.omp,
        args.model,
        channel_enabled=channel_enabled,
        webhook_enabled=webhook_enabled,
        webhook_port=args.webhook_port,
        project_root=args.project_root,
        migrate_legacy=not args.no_legacy_import and not args.migrate_only,
        work_participant=args.work_participant,
        work_token_file=args.work_token_file,
    )
    if args.migrate_only:
        bridge = Bridge(
            options.state_dir,
            options.executable,
            options.model,
            project_root=options.project_root,
            channel_enabled=False,
            webhook_enabled=False,
            migrate_legacy=False,
        )
        result = migration.migrate_legacy(
            bridge.scope,
            bridge.tasks.path,
            legacy_cwds=args.legacy_cwd,
            context_ids=args.legacy_context,
            max_bytes=None,
        )
        print(
            json.dumps({**bridge.scope.info(), "migration": result}, ensure_ascii=False)
        )
        return
    build_server(options).run(transport="stdio", show_banner=False)
