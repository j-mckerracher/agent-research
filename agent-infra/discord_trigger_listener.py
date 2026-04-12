#!/usr/bin/env python3
"""Discord Trigger Listener — thin launcher.

The listener logic now lives in ``trigger_api/``.  This script starts the
FastAPI app via uvicorn, which runs the Discord polling loop as a background
task alongside the HTTP API.

Usage (unchanged from v1)::

    DISCORD_BOT_TOKEN=<token> python3 discord_trigger_listener.py \\
        [--repo /abs/path/to/repo] \\
        [--backend copilot|claude] \\
        [--host 0.0.0.0] \\
        [--port 8000]

Or run the app directly with uvicorn for production::

    uvicorn trigger_api.app:app --host 0.0.0.0 --port 8000

Environment variables (same as before, plus API additions)::

    DISCORD_BOT_TOKEN        Required for Discord polling
    DISCORD_GUILD_NAME       Discord server name      (default: arigato-mr-roboto)
    DISCORD_TRIGGER_CHANNEL  Channel to watch         (default: trigger-agents)
    DISCORD_POLL_SECONDS     Poll interval in seconds (default: 10)
    ADO_WEBHOOK_SECRET       Basic-auth password for Azure DevOps service hooks
    DEFAULT_REPO             Repo root when a trigger omits a path
    BACKEND                  copilot | claude (auto-detected if unset)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent Trigger API — Discord listener + HTTP API"
    )
    parser.add_argument("--repo", metavar="PATH", help="Default repository root")
    parser.add_argument(
        "--backend",
        choices=["copilot", "claude"],
        help="AI backend (auto-detected if not set)",
    )
    parser.add_argument("--host", default="0.0.0.0", metavar="HOST")
    parser.add_argument("--port", type=int, default=8000, metavar="PORT")
    args = parser.parse_args(argv)

    # Surface CLI args as env vars so create_app() picks them up
    if args.repo:
        os.environ["DEFAULT_REPO"] = str(Path(args.repo).resolve())
    if args.backend:
        os.environ["BACKEND"] = args.backend

    if not os.environ.get("DISCORD_BOT_TOKEN", "").strip():
        print("ERROR: DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
        print("  export DISCORD_BOT_TOKEN=<your-bot-token>", file=sys.stderr)
        return 1

    try:
        import uvicorn
    except ImportError:
        print(
            "ERROR: uvicorn is not installed.\n"
            "  pip install 'uvicorn[standard]'",
            file=sys.stderr,
        )
        return 1

    uvicorn.run(
        "trigger_api.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
