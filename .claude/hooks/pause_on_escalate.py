#!/usr/bin/env python3
"""Pause-on-escalation hook for Claude Code.

Registered for Stop and SubagentStop lifecycle events in .claude/settings.json.
When an escalation artifact (escalated.json) exists for the active change,
this hook:
  - Writes paused.json if not present
  - Prints a clear pause message to stderr
  - Exits with code 2 to block the Stop event

When resume.json exists (created by a human), it clears the escalation
and allows normal operation to continue (exit 0).

Hook input: JSON on stdin with session_id, cwd, hook_event_name, etc.
Exit codes: 0 = allow, 2 = block the lifecycle event.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ARTIFACT_ROOT_DEFAULT = "agent-context"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_repo_root() -> Path | None:
    """Walk up from cwd looking for .git directory."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def find_change_id(artifact_root: Path) -> str | None:
    """Detect the active CHANGE-ID from intake config or env var."""
    # 1. Environment variable
    change_id = os.environ.get("CHANGE_ID")
    if change_id:
        return change_id

    # 2. Scan artifact_root for directories containing intake/config.yaml
    if artifact_root.is_dir():
        for child in sorted(artifact_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if child.is_dir() and (child / "intake" / "config.yaml").exists():
                return child.name
            if child.is_dir() and (child / "intake" / "config.json").exists():
                return child.name

    return None


def timestamp_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def timestamp_file() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Discord Bridge integration
# ---------------------------------------------------------------------------


def _spawn_discord_bridge(
    repo_root: Path,
    change_id: str,
    status_dir: Path,
    escalated_path: Path,
) -> bool:
    """Spawn the Discord escalation bridge in the background (once per escalation).

    Returns True if the bridge was spawned or is already running, False if skipped.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return False  # No bot token — fall back to manual resume.json creation

    bridge_script = repo_root / ".claude" / "scripts" / "discord_escalation_bridge.py"
    if not bridge_script.exists():
        return False

    pid_file = status_dir / "discord_bridge.pid"

    # Check if already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)  # Signal 0 = existence check
            return True  # Already running
        except (ProcessLookupError, ValueError, OSError):
            pid_file.unlink(missing_ok=True)  # Stale PID — respawn

    log_path = status_dir / "discord_bridge.log"
    status_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(bridge_script),
            "--escalated-path", str(escalated_path),
            "--status-dir", str(status_dir),
            "--change-id", change_id,
        ],
        start_new_session=True,
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------


def main() -> int:
    # Read hook event JSON from stdin (Claude Code sends this)
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # Determine paths
    repo_root = find_repo_root()
    if repo_root is None:
        # Cannot determine repo root — allow normal operation
        return 0

    artifact_root = repo_root / ARTIFACT_ROOT_DEFAULT
    change_id = find_change_id(artifact_root)

    if not change_id:
        # No active workflow — nothing to check
        return 0

    status_dir = artifact_root / change_id / "status"
    escalated_path = status_dir / "escalated.json"
    paused_path = status_dir / "paused.json"
    resume_path = status_dir / "resume.json"
    archive_dir = status_dir / "escalated_archive"

    # ---- No escalation — allow normal operation ----
    if not escalated_path.exists():
        return 0

    # ---- Resume exists — clear escalation and allow ----
    if resume_path.exists():
        # Archive the escalation
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{timestamp_file()}_escalated.json"
        shutil.move(str(escalated_path), str(archive_dir / archive_name))

        # Clean up pause/resume markers
        if paused_path.exists():
            paused_path.unlink()
        resume_path.unlink()

        print(
            f"[RESUMED] Escalation cleared. Archived to status/escalated_archive/{archive_name}",
            file=sys.stderr,
        )
        return 0

    # ---- Escalation active, no resume — PAUSE ----
    # Read escalation details for the message
    try:
        escalation = json.loads(escalated_path.read_text())
    except (json.JSONDecodeError, OSError):
        escalation = {}

    # Write paused.json if not present
    if not paused_path.exists():
        status_dir.mkdir(parents=True, exist_ok=True)
        paused_path.write_text(
            json.dumps(
                {
                    "paused_at": timestamp_iso(),
                    "triggered_by": "escalated.json",
                    "escalation_file": str(escalated_path.relative_to(artifact_root / change_id)),
                },
                indent=2,
            )
        )

    # Try to spawn the Discord bridge (non-blocking background process)
    discord_active = _spawn_discord_bridge(repo_root, change_id, status_dir, escalated_path)

    # Build pause message for stderr (Claude sees this as error on exit 2)
    blocking_questions = escalation.get("blocking_questions", [])
    reason = escalation.get("reason", "No reason specified")
    stage = escalation.get("stage_key", "unknown")

    questions_text = "\n".join(f"  • {q}" for q in blocking_questions) if blocking_questions else "  (none specified)"

    if discord_active:
        resume_instructions = (
            "A notification has been posted to Discord (#general in Agent-Escalations).\n"
            "  Reply in the thread with:  RESUME: Q1=your answer, Q2=another answer\n"
            "  The workflow will continue automatically once your reply is received.\n"
            "\n"
            f"  Bridge log: {status_dir / 'discord_bridge.log'}"
        )
    else:
        resume_instructions = (
            f"To resume, create:\n"
            f"  {resume_path}\n"
            "\n"
            f'  With JSON: {{"responder": "<name>", "answers": {{"Q1": "..."}}, "constraints": [], "extra_context": ""}}\n'
            "\n"
            "  (Set DISCORD_BOT_TOKEN to enable automatic Discord notifications.)"
        )

    msg = f"""
╔══════════════════════════════════════════════════════════════╗
║  ⏸  WORKFLOW PAUSED — Human Input Required                  ║
╚══════════════════════════════════════════════════════════════╝

Stage:   {stage}
Reason:  {reason}

Blocking Questions:
{questions_text}

{resume_instructions}

Escalation details:
  {escalated_path}
"""
    print(msg, file=sys.stderr)

    # Exit 2 = block the Stop event
    return 2


if __name__ == "__main__":
    sys.exit(main())
