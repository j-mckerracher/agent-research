#!/usr/bin/env python3
"""Thin CLI wrapper for general-purpose agent runs.

Invokes the ``claude`` or ``copilot`` CLI with the supplied prompt, model,
repo, and optional agent file.  Designed to be called by the trigger API's
GeneralRunHandler in the same way that ``run_headless.py`` is called by
RunWorkflowHandler.

Usage::

    python3 run_general.py \\
        --backend claude \\
        --model sonnet \\
        --prompt "Fix the failing unit tests" \\
        --repo /abs/path/to/repo \\
        [--agent spike.agent.md] \\
        [--output-json /tmp/result.json]

Exit codes:
    0  CLI subprocess exited successfully
    1  Configuration error or subprocess failure
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[run_general] {_ts()} {msg}", flush=True)


def _build_claude_cmd(args: argparse.Namespace) -> list[str]:
    """Build the ``claude`` CLI command list."""
    cmd = ["claude", "--print"]
    if args.model:
        cmd += ["--model", args.model]
    if args.agent:
        cmd += ["--agent", args.agent]
    cmd += ["--dangerously-skip-permissions"]
    cmd.append(args.prompt)
    return cmd


def _build_copilot_cmd(args: argparse.Namespace) -> list[str]:
    """Build the ``copilot`` CLI command list."""
    cmd = ["copilot"]
    if args.model:
        cmd += ["--model", args.model]
    if args.agent:
        cmd += ["--agent", args.agent]
    cmd.append(args.prompt)
    return cmd


_BACKEND_BUILDERS = {
    "claude": _build_claude_cmd,
    "copilot": _build_copilot_cmd,
}


def _write_summary(path: Path, *, exit_code: int, elapsed: float, backend: str) -> None:
    summary = {
        "status": "pass" if exit_code == 0 else "fail",
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed, 2),
        "backend": backend,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"Warning: could not write summary to {path}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an AI agent against a repository with a freeform prompt.",
        epilog=(
            "example:\n"
            "  python3 run_general.py \\\n"
            "    --backend copilot --model \"sonnet 4.6\" \\\n"
            "    --prompt \"Fix the failing unit tests\" \\\n"
            "    --repo /Users/mckerracher.joshua/Code/mcs-products-mono-ui"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backend",
        required=True,
        choices=sorted(_BACKEND_BUILDERS),
        help="AI backend to invoke (claude or copilot)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model identifier passed to the backend CLI (e.g. sonnet, opus, gpt-4.1)",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="The prompt text to send to the agent",
    )
    parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="Absolute path to the repository to run in (used as cwd)",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Path to an .agent.md file (relative to repo or absolute)",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        type=Path,
        help="Path to write a JSON summary on completion",
    )
    args = parser.parse_args(argv)

    # Validate repo exists
    repo = args.repo.resolve()
    if not repo.is_dir():
        log(f"Error: repo directory does not exist: {repo}")
        return 1

    # Validate backend CLI is available
    if shutil.which(args.backend) is None:
        log(f"Error: {args.backend!r} CLI not found on PATH")
        return 1

    # Build command
    builder = _BACKEND_BUILDERS[args.backend]
    cmd = builder(args)
    log(f"Running: {' '.join(cmd)}")
    log(f"CWD: {repo}")

    start = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        print(line, flush=True)

    proc.wait()
    elapsed = time.monotonic() - start
    exit_code = proc.returncode

    log(f"Finished: exit_code={exit_code}  elapsed={elapsed:.1f}s")

    if args.output_json:
        _write_summary(args.output_json, exit_code=exit_code, elapsed=elapsed, backend=args.backend)

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
