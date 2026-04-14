#!/usr/bin/env python3
"""Non-interactive launcher for the agent workflow runner.

Used by the Discord trigger listener (and CI/CD) to run workflows without a TTY.
Builds WorkflowConfig programmatically from CLI args and delegates to run_workflow().

Usage:
    python3 run_headless.py \\
        --change-id WI-4461550 \\
        [--repo /abs/path/to/repo] \\
        [--backend copilot|claude] \\
        [--output-json /path/to/summary.json] \\
        [--cleanup-worktree] \\
        [--no-worktree]

Exit codes:
    0  Workflow completed with status=pass
    1  Fatal configuration or workflow error, or status=fail
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Ensure the sibling run.py is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import (  # noqa: E402
    WORKFLOW_ASSETS_ROOT,
    WorkflowConfig,
    WorkflowError,
    create_pull_request,
    detect_available_backends,
    emit_event,
    fetch_ado_context,
    format_summary,
    intake_artifacts_exist,
    log,
    normalize_change_id,
    parse_work_item_reference,
    resolve_ado_defaults,
    resolve_repo_root,
    run_workflow,
)


# ---------------------------------------------------------------------------
# Worktree support
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeInfo:
    """Metadata for a freshly-created Git worktree."""

    path: Path
    name: str
    branch: str
    base_ref: str


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command under *repo_root*, logging it and raising on failure."""
    cmd = ["git", "-C", str(repo_root)] + args
    log("INFO", f"git: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=30,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise WorkflowError(
            f"git command failed (exit {result.returncode}): {' '.join(args)}"
            + (f"\n  {stderr}" if stderr else "")
        )
    return result


def _resolve_base_ref(repo_root: Path) -> str:
    """Return a valid base ref for creating the worktree.

    Tries, in order:
    1. ``origin/HEAD`` as-is
    2. Re-syncs ``origin/HEAD`` via ``git remote set-head origin -a`` and retries
    3. ``origin/main``
    4. ``origin/master``

    Raises WorkflowError if none are resolvable.
    """
    for ref in ("origin/HEAD",):
        result = _run_git(
            repo_root,
            ["rev-parse", "--verify", ref],
            check=False,
        )
        if result.returncode == 0:
            log("INFO", f"Base ref resolved: {ref}")
            return ref

    # origin/HEAD missing or stale — try to auto-detect
    log("INFO", "origin/HEAD not found; running 'git remote set-head origin -a'")
    _run_git(repo_root, ["remote", "set-head", "origin", "-a"], check=False)

    result = _run_git(
        repo_root,
        ["rev-parse", "--verify", "origin/HEAD"],
        check=False,
    )
    if result.returncode == 0:
        log("INFO", "Base ref resolved after set-head: origin/HEAD")
        return "origin/HEAD"

    for fallback in ("origin/main", "origin/master"):
        result = _run_git(
            repo_root,
            ["rev-parse", "--verify", fallback],
            check=False,
        )
        if result.returncode == 0:
            log("INFO", f"Base ref resolved via fallback: {fallback}")
            return fallback

    raise WorkflowError(
        "Cannot determine a base ref for the worktree. "
        "Run 'git remote set-head origin -a' or ensure origin/main or origin/master exists."
    )


def _make_worktree_name(change_id: str) -> str:
    """Generate a unique worktree name from a change-id."""
    normalized = normalize_change_id(change_id)
    # Strip WI- prefix, lowercase, replace non-alphanum sequences with -
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower().removeprefix("wi-")).strip("-")
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"{slug}-{ts}-{rand}"


def create_fresh_worktree(repo_root: Path, change_id: str) -> WorktreeInfo:
    """Create a new Git worktree under ``<repo_root>/.claude/worktrees/<name>``.

    Args:
        repo_root: Absolute path to the main repository checkout.
        change_id: Work item ID used to build a human-readable directory name.

    Returns:
        A :class:`WorktreeInfo` describing the created worktree.

    Raises:
        WorkflowError: If the repo is not a Git repository, if a base ref cannot
            be resolved, or if ``git worktree add`` fails.
    """
    # Verify this is a git repo
    result = _run_git(
        repo_root,
        ["rev-parse", "--is-inside-work-tree"],
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise WorkflowError(
            f"'{repo_root}' does not appear to be inside a Git repository."
        )

    log("INFO", f"Base repo root: {repo_root}")

    base_ref = _resolve_base_ref(repo_root)

    worktrees_root = repo_root / ".claude" / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)

    # Generate a unique name (retry once on collision, though extremely unlikely)
    for _ in range(2):
        name = _make_worktree_name(change_id)
        worktree_path = worktrees_root / name
        if not worktree_path.exists():
            break
    else:
        raise WorkflowError(
            "Could not generate a unique worktree name after 2 attempts."
        )

    branch = f"worktree-{name}"

    _run_git(
        repo_root,
        ["worktree", "add", "-b", branch, str(worktree_path), base_ref],
    )

    log("INFO", f"Worktree created: {worktree_path}")
    log("INFO", f"Worktree branch:  {branch}")

    return WorktreeInfo(
        path=worktree_path,
        name=name,
        branch=branch,
        base_ref=base_ref,
    )


def _cleanup_worktree(repo_root: Path, info: WorktreeInfo) -> None:
    """Remove the worktree and its branch (best-effort; errors are logged only)."""
    try:
        _run_git(
            repo_root,
            ["worktree", "remove", "--force", str(info.path)],
        )
        log("INFO", f"Worktree removed: {info.path}")
    except (WorkflowError, subprocess.SubprocessError, OSError) as exc:
        log("WARN", f"Failed to remove worktree '{info.path}': {exc}")

    try:
        _run_git(repo_root, ["branch", "-D", info.branch])
        log("INFO", f"Worktree branch deleted: {info.branch}")
    except (WorkflowError, subprocess.SubprocessError, OSError) as exc:
        log("WARN", f"Failed to delete branch '{info.branch}': {exc}")


# ---------------------------------------------------------------------------
# Headless config builder
# ---------------------------------------------------------------------------


def build_headless_config(
    change_id: str,
    repo_root: Path,
    backend_key: str | None = None,
) -> WorkflowConfig:
    """Build a WorkflowConfig without interactive prompts."""

    resolved_repo_root = repo_root.resolve()
    resolved_artifact_root = (resolved_repo_root / "agent-context").resolve()

    # Auto-select backend (prefer what's available; caller can override)
    backends = detect_available_backends()
    if not backends:
        raise WorkflowError(
            "No AI backend found. Install the 'copilot' or 'claude' CLI."
        )

    if backend_key:
        backend = next((b for b in backends if b.key == backend_key), None)
        if not backend:
            available = [b.key for b in backends]
            raise WorkflowError(
                f"Backend '{backend_key}' is not available. Available: {available}"
            )
    else:
        backend = backends[0]

    log("INFO", f"Backend: {backend.label} ({backend.command})")

    base_config: dict = {
        "repo_root": resolved_repo_root,
        "workflow_assets_root": WORKFLOW_ASSETS_ROOT,
        "artifact_root": resolved_artifact_root,
        "cli_backend": backend.key,
        "cli_bin": backend.command,
        "model": backend.default_model,
    }

    normalized_id = normalize_change_id(change_id)

    # Reuse existing intake artifacts when present
    if intake_artifacts_exist(resolved_artifact_root, normalized_id):
        log("INFO", f"Reusing existing intake artifacts for {normalized_id}")
        return WorkflowConfig(
            change_id=normalized_id,
            context="",
            reuse_existing_intake=True,
            **base_config,
        )

    # Attempt to fetch context from Azure DevOps
    try:
        org_url, project = resolve_ado_defaults(resolved_repo_root)
        work_item_id = normalized_id.removeprefix("WI-")
        reference = parse_work_item_reference(
            work_item_id,
            default_organization=org_url,
            default_project=project,
        )
        context = fetch_ado_context(reference, resolved_repo_root)
        log("INFO", f"ADO context fetched for {normalized_id}")
    except WorkflowError as exc:
        log("WARN", f"Could not fetch ADO context: {exc} — using minimal context")
        context = f"Work item: {normalized_id}"

    return WorkflowConfig(change_id=normalized_id, context=context, **base_config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Non-interactive agent workflow launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--change-id",
        required=True,
        metavar="WI-XXXX",
        help="Work item ID (e.g. WI-4461550 or bare 4461550)",
    )
    parser.add_argument(
        "--repo",
        metavar="PATH",
        help="Absolute path to the repository root (default: git root of cwd)",
    )
    parser.add_argument(
        "--backend",
        choices=["copilot", "claude"],
        help="AI backend to use (auto-detected if not specified)",
    )
    parser.add_argument(
        "--output-json",
        metavar="PATH",
        help="Write a JSON summary to this path on completion",
    )
    parser.add_argument(
        "--cleanup-worktree",
        action="store_true",
        default=False,
        help="Remove the worktree and its branch after the workflow completes (even on failure)",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        default=False,
        help="Skip worktree creation and run directly in the main repo (legacy behaviour)",
    )
    args = parser.parse_args(argv)

    main_repo_root = Path(args.repo).resolve() if args.repo else resolve_repo_root()

    worktree_info: WorktreeInfo | None = None

    if not args.no_worktree:
        try:
            worktree_info = create_fresh_worktree(main_repo_root, args.change_id)
        except WorkflowError as exc:
            log("ERROR", f"Worktree creation failed: {exc}")
            _write_error_json(args.output_json, str(exc), worktree_info=None)
            return 1

    effective_repo_root = worktree_info.path if worktree_info else main_repo_root

    try:
        try:
            config = build_headless_config(
                args.change_id, effective_repo_root, backend_key=args.backend
            )
        except WorkflowError as exc:
            log("ERROR", f"Config error: {exc}")
            emit_event("workflow_error", error=str(exc))
            _write_error_json(args.output_json, str(exc), worktree_info=worktree_info)
            return 1

        try:
            results = run_workflow(config)
        except WorkflowError as exc:
            log("ERROR", f"Workflow failed: {exc}")
            emit_event("workflow_error", error=str(exc))
            _write_error_json(args.output_json, str(exc), worktree_info=worktree_info)
            return 1

        summary = format_summary(results)
        if worktree_info:
            summary["worktree"] = {
                "path": str(worktree_info.path),
                "branch": worktree_info.branch,
                "name": worktree_info.name,
                "base_ref": worktree_info.base_ref,
            }

        if args.output_json:
            Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output_json).write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )

        overall = summary["status"]
        log(
            "OK" if overall == "pass" else "ERROR",
            f"Workflow status: {overall.upper()}",
        )

        if overall == "pass" and worktree_info:
            try:
                org_url, project = resolve_ado_defaults(main_repo_root)
                create_pull_request(
                    main_repo_root,
                    source_branch=worktree_info.branch,
                    base_ref=worktree_info.base_ref,
                    change_id=args.change_id,
                    org_url=org_url,
                    project=project,
                    worktree_path=worktree_info.path,
                )
            except (WorkflowError, subprocess.SubprocessError, OSError) as exc:
                log("WARN", f"PR creation failed (non-fatal): {exc}")

        return 0 if overall == "pass" else 1

    finally:
        if worktree_info and args.cleanup_worktree:
            _cleanup_worktree(main_repo_root, worktree_info)


def _write_error_json(
    output_json: str | None,
    error: str,
    *,
    worktree_info: WorktreeInfo | None,
) -> None:
    if not output_json:
        return
    payload: dict = {"status": "fail", "error": error, "stages": []}
    if worktree_info:
        payload["worktree"] = {
            "path": str(worktree_info.path),
            "branch": worktree_info.branch,
            "name": worktree_info.name,
            "base_ref": worktree_info.base_ref,
        }
    Path(output_json).parent.mkdir(parents=True, exist_ok=True)
    Path(output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
