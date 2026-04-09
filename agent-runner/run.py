#!/usr/bin/env python3
"""Run the local `.claude` workflow agents in the documented workflow order.

This module turns the rough scratch pseudocode into a deterministic workflow runner
for the custom agents stored in `.claude/agents`.

The runner supports two modes:

- real mode: invokes either GitHub Copilot or Claude Code for each stage
- dry-run mode: creates synthetic artifacts so the control-flow can be tested
  without network/model usage

The default stage order normalizes the scratch notes to the repository's canonical
workflow:

1. intake
2. task-generator
3. task-plan-evaluator loop
4. task-assigner
5. assignment-evaluator loop
6. software-engineer-hyperagent
7. implementation-evaluator loop
8. qa-engineer
9. qa-evaluator loop
10. lessons-optimizer-hyperagent
"""

from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote, urlparse

# ---------------------------------------------------------------------------
# Console logging helpers
# ---------------------------------------------------------------------------

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_LEVEL_STYLES: dict[str, str] = {
    "INFO":    "\033[36m",   # cyan
    "OK":      "\033[32m",   # green
    "WARN":    "\033[33m",   # yellow
    "ERROR":   "\033[31m",   # red
    "AGENT":   "\033[35m",   # magenta
    "STAGE":   "\033[34m",   # blue
    "OUTPUT":  "\033[90m",   # dark grey
}

STARTUP_ROBOT_ART = textwrap.dedent(
    r"""
          [::]
        .-:||:-.
       /  _  _  \
      |  (o)(o)  |
      |    __    |
       \  '--'  /
        `-.__.-'
        /|_||_|\
       /_/ /\ \_\
    """
).strip()


def _ts() -> str:
    """Return a short HH:MM:SS timestamp."""
    return datetime.now().strftime("%H:%M:%S")


def log(level: str, message: str) -> None:
    """Print a timestamped, coloured log line to stdout."""
    colour = _LEVEL_STYLES.get(level, "")
    label = f"{colour}{_ANSI_BOLD}[{level:^5}]{_ANSI_RESET}"
    print(f"{_ANSI_BOLD}{_ts()}{_ANSI_RESET} {label} {message}", flush=True)


def print_stage_banner(title: str) -> None:
    """Print a prominent visual separator for a workflow stage."""
    width = 72
    bar = "─" * width
    colour = _LEVEL_STYLES["STAGE"]
    print(flush=True)
    print(f"{colour}{_ANSI_BOLD}┌{bar}┐{_ANSI_RESET}", flush=True)
    print(f"{colour}{_ANSI_BOLD}│  {title:<{width - 2}}│{_ANSI_RESET}", flush=True)
    print(f"{colour}{_ANSI_BOLD}└{bar}┘{_ANSI_RESET}", flush=True)
    print(flush=True)


def _req_sym(command: str) -> str:
    """Return ✓ if *command* is on PATH, ✗ otherwise."""
    return "✓" if shutil.which(command) else "✗"


def print_startup_robot() -> None:
    """Print the interactive launch banner with a concise requirements check."""

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    py_sym = "✓" if sys.version_info >= (3, 9) else "✗"

    req = (
        f"  Requires:  Python ≥3.9 {py_sym} {py_ver}"
        f"  │  git {_req_sym('git')}"
        f"  │  AI: copilot {_req_sym('copilot')}  claude {_req_sym('claude')}  (need ≥1)"
        f"  │  az {_req_sym('az')} (optional, ADO fetch)"
    )

    print(STARTUP_ROBOT_ART, flush=True)
    print(flush=True)
    print(req, flush=True)
    print(flush=True)


def print_agent_output(result: "CommandResult", agent_key: str) -> None:
    """Print the agent's stdout and stderr to the console."""
    colour = _LEVEL_STYLES["OUTPUT"]
    reset = _ANSI_RESET
    if result.stdout.strip():
        print(f"{colour}{'─' * 60}  [{agent_key}] stdout  {'─' * 4}{reset}", flush=True)
        for line in result.stdout.rstrip().splitlines():
            print(f"{colour}  {line}{reset}", flush=True)
        print(f"{colour}{'─' * 76}{reset}", flush=True)
    if result.stderr.strip():
        err_colour = _LEVEL_STYLES["WARN"]
        print(f"{err_colour}{'─' * 60}  [{agent_key}] stderr  {'─' * 4}{reset}", flush=True)
        for line in result.stderr.rstrip().splitlines():
            print(f"{err_colour}  {line}{reset}", flush=True)
        print(f"{err_colour}{'─' * 76}{reset}", flush=True)

WORKFLOW_ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".claude"
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_ADO_ORGANIZATION = "https://dev.azure.com/mclm"
DEFAULT_ADO_PROJECT = "Mayo Collaborative Services"


class WorkflowError(RuntimeError):
    """Raised when the workflow cannot continue safely."""


@dataclass(frozen=True)
class BackendSpec:
    """Describes a supported interactive AI CLI backend."""

    key: str
    label: str
    command: str
    default_model: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    """Describes a discovered custom Copilot agent."""

    key: str
    name: str
    description: str
    path: Path


@dataclass
class WorkflowConfig:
    """Runtime configuration for the workflow runner."""

    repo_root: Path
    workflow_assets_root: Path
    change_id: str
    context: str
    artifact_root: Path
    cli_backend: str = "copilot"
    cli_bin: str = "copilot"
    model: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    dry_run: bool = False
    continue_on_failure: bool = False
    max_task_plan_attempts: int = 3
    max_assignment_attempts: int = 2
    max_implementation_attempts: int = 3
    max_qa_attempts: int = 3
    additional_dirs: list[Path] = field(default_factory=list)
    reuse_existing_intake: bool = False


@dataclass(frozen=True)
class ResumeCandidate:
    """A change with complete intake artifacts that can be reused."""

    change_id: str
    base_path: Path
    updated_at: float


@dataclass(frozen=True)
class WorkItemReference:
    """The Azure DevOps coordinates required to fetch a work item."""

    organization_url: str
    project: str
    work_item_id: str


@dataclass
class CommandResult:
    """Captures a completed subprocess invocation."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class StageResult:
    """Result metadata for a stage or evaluator loop."""

    stage_name: str
    passed: bool
    attempts: int
    artifact_paths: list[Path]
    details: dict[str, Any] = field(default_factory=dict)


STAGE_AGENT_ALIASES: dict[str, tuple[str, ...]] = {
    "intake": ("01-intake", "intake-agent"),
    "task_generator": ("02-task-generator", "task-generator"),
    "task_plan_evaluator": ("06-task-plan-evaluator", "task-plan-evaluator"),
    "task_assigner": ("03-task-assigner", "task-assigner"),
    "assignment_evaluator": ("07-assignment-evaluator", "assignment-evaluator"),
    "software_engineer": (
        "04-software-engineer-hyperagent",
        "software-engineer-hyperagent",
    ),
    "implementation_evaluator": ("08-implementation-evaluator", "implementation-evaluator"),
    "qa": ("05-qa", "qa-engineer"),
    "qa_evaluator": ("09-qa-evaluator", "qa-evaluator"),
    "lessons_optimizer": (
        "11-lessons-optimizer-hyperagent",
        "lessons-optimizer-hyperagent",
    ),
}

BACKEND_SPECS: tuple[BackendSpec, ...] = (
    BackendSpec(key="copilot", label="GitHub Copilot", command="copilot", default_model="claude-haiku-4.5"),
    BackendSpec(key="claude", label="Claude Code", command="claude"),
)

def iso_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_timestamp() -> str:
    """Return a filesystem-safe UTC timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def normalize_change_id(raw_value: str) -> str:
    """Normalize a change identifier from either WI-123 or bare digits."""

    value = raw_value.strip()
    match = re.fullmatch(r"WI-(\d+)", value, flags=re.IGNORECASE)
    if match:
        return f"WI-{match.group(1)}"
    if value.isdigit():
        return f"WI-{value}"
    return value


def intake_artifact_paths(artifact_root: Path, change_id: str) -> list[Path]:
    """Return the canonical intake artifact paths for a change."""

    base = artifact_root / change_id / "intake"
    return [base / "story.yaml", base / "config.yaml", base / "constraints.md"]


def intake_artifacts_exist(artifact_root: Path, change_id: str) -> bool:
    """Return whether all intake artifacts already exist for a change."""

    return all(path.is_file() for path in intake_artifact_paths(artifact_root, change_id))


def list_resume_candidates(artifact_root: Path) -> list[ResumeCandidate]:
    """List changes with complete intake artifacts that can be reused."""

    if not artifact_root.is_dir():
        return []

    candidates: list[ResumeCandidate] = []
    for change_dir in artifact_root.iterdir():
        if not change_dir.is_dir():
            continue
        if not intake_artifacts_exist(artifact_root, change_dir.name):
            continue
        updated_at = max(path.stat().st_mtime for path in intake_artifact_paths(artifact_root, change_dir.name))
        candidates.append(ResumeCandidate(change_id=change_dir.name, base_path=change_dir, updated_at=updated_at))

    return sorted(candidates, key=lambda candidate: candidate.updated_at, reverse=True)


def prompt_text(
    prompt: str,
    *,
    input_fn: Callable[[str], str] = input,
    default: str | None = None,
    allow_empty: bool = False,
) -> str:
    """Prompt until a valid string is provided."""

    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input_fn(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or allow_empty:
            return value
        log("WARN", "A value is required.")


def prompt_yes_no(
    prompt: str,
    *,
    input_fn: Callable[[str], str] = input,
    default: bool = True,
) -> bool:
    """Prompt for a yes/no answer."""

    default_hint = "Y/n" if default else "y/N"
    while True:
        raw = input_fn(f"{prompt} [{default_hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        log("WARN", "Please answer yes or no.")


def prompt_option(
    title: str,
    options: Sequence[str],
    *,
    input_fn: Callable[[str], str] = input,
    default_index: int = 1,
) -> int:
    """Prompt the user to select a numbered option."""

    print()
    print(title)
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}")
    while True:
        raw = prompt_text("Select an option", input_fn=input_fn, default=str(default_index))
        if raw.isdigit():
            selected = int(raw)
            if 1 <= selected <= len(options):
                return selected
        log("WARN", f"Choose a number from 1 to {len(options)}.")


def prompt_multiline(
    prompt: str,
    *,
    input_fn: Callable[[str], str] = input,
    end_marker: str = "END",
) -> str:
    """Collect multi-line input terminated by a sentinel line."""

    print()
    print(prompt)
    print(f"Finish with a line containing only {end_marker!r}.")
    lines: list[str] = []
    while True:
        line = input_fn("")
        if line.strip() == end_marker:
            break
        lines.append(line)
    content = "\n".join(lines).strip()
    if not content:
        raise WorkflowError("Workflow context cannot be empty.")
    return content


def detect_available_backends() -> list[BackendSpec]:
    """Return the supported AI backends available on the local machine."""

    available: list[BackendSpec] = []
    for backend in BACKEND_SPECS:
        if shutil.which(backend.command):
            available.append(backend)
    if not available:
        raise WorkflowError("No supported AI CLI was found. Install GitHub Copilot or Claude Code.")
    return available


def select_backend(*, input_fn: Callable[[str], str] = input) -> BackendSpec:
    """Choose which AI backend should drive the workflow."""

    available = detect_available_backends()
    if len(available) == 1:
        backend = available[0]
        log("INFO", f"Using {backend.label} ({backend.command})")
        return backend

    selected = prompt_option(
        "Choose the AI backend for this workflow:",
        [f"{backend.label} ({backend.command})" for backend in available],
        input_fn=input_fn,
        default_index=1,
    )
    backend = available[selected - 1]
    log("INFO", f"Using {backend.label} ({backend.command})")
    return backend


def resolve_repo_root() -> Path:
    """Resolve the repository root for the interactive launcher."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        completed = None

    if completed and completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return WORKFLOW_ASSETS_ROOT.parent.resolve()


def resolve_ado_defaults(repo_root: Path) -> tuple[str, str]:
    """Resolve Azure DevOps defaults without mutating local CLI configuration."""

    organization = DEFAULT_ADO_ORGANIZATION
    project = DEFAULT_ADO_PROJECT
    az_bin = shutil.which("az")
    if az_bin is None:
        return organization, project

    try:
        completed = subprocess.run(
            [az_bin, "devops", "configure", "--list"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return organization, project

    if completed.returncode != 0:
        return organization, project

    organization_match = re.search(r"^\s*organization\s*=\s*(.+?)\s*$", completed.stdout, flags=re.MULTILINE)
    project_match = re.search(r"^\s*project\s*=\s*(.+?)\s*$", completed.stdout, flags=re.MULTILINE)
    if organization_match:
        organization = organization_match.group(1).strip()
    if project_match:
        project = project_match.group(1).strip()
    return organization, project


def parse_work_item_reference(
    raw_value: str,
    *,
    default_organization: str,
    default_project: str,
) -> WorkItemReference:
    """Parse either an Azure DevOps work item URL or a bare work item id."""

    value = raw_value.strip()
    if not value:
        raise WorkflowError("A work item id or URL is required.")

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if (
            parsed.netloc != "dev.azure.com"
            or len(parts) < 5
            or parts[2] != "_workitems"
            or parts[3] != "edit"
            or not parts[4].isdigit()
        ):
            raise WorkflowError(
                "Azure DevOps links must look like "
                "https://dev.azure.com/{organization}/{project}/_workitems/edit/{id}"
            )
        organization_url = f"{parsed.scheme}://{parsed.netloc}/{parts[0]}"
        return WorkItemReference(
            organization_url=organization_url,
            project=parts[1],
            work_item_id=parts[4],
        )

    match = re.fullmatch(r"(?:WI-)?(\d+)", value, flags=re.IGNORECASE)
    if not match:
        raise WorkflowError("Enter either a numeric work item id, WI-12345, or a full Azure DevOps work item URL.")

    return WorkItemReference(
        organization_url=default_organization,
        project=default_project,
        work_item_id=match.group(1),
    )


def build_work_item_url(reference: WorkItemReference) -> str:
    """Construct the canonical Azure DevOps work item URL."""

    project = quote(reference.project, safe="")
    return f"{reference.organization_url}/{project}/_workitems/edit/{reference.work_item_id}"


def strip_html_to_text(raw_html: str | None) -> str:
    """Convert Azure DevOps rich text into readable plain text."""

    if not raw_html:
        return ""

    text = html.unescape(raw_html)
    substitutions = [
        (r"(?i)<br\s*/?>", "\n"),
        (r"(?i)</p\s*>", "\n\n"),
        (r"(?i)<p\s*>", ""),
        (r"(?i)</div\s*>", "\n"),
        (r"(?i)<div\s*>", ""),
        (r"(?i)</li\s*>", "\n"),
        (r"(?i)<li\s*>", "- "),
        (r"(?i)</ul\s*>", "\n"),
        (r"(?i)</ol\s*>", "\n"),
    ]
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_acceptance_criteria_from_text(text: str) -> list[str]:
    """Extract acceptance criteria embedded in free-form text."""

    criteria: list[str] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"(?i)^AC\s*[-:]\s*(.+)$", line)
        if match:
            criteria.append(match.group(1).strip())
            continue
        if re.match(r"(?i)^acceptance criteria\s*:?\s*$", line):
            in_section = True
            continue
        if in_section:
            bullet = re.match(r"^(?:[-*]|\d+\.)\s*(.+)$", line)
            if bullet:
                candidate = re.sub(r"(?i)^AC\s*[-:]\s*", "", bullet.group(1).strip())
                criteria.append(candidate)
                continue
            if criteria:
                break
    return criteria


def build_ado_context(work_item_payload: dict[str, Any], reference: WorkItemReference) -> str:
    """Build plain-text workflow context from an Azure DevOps work item."""

    fields = work_item_payload.get("fields", {})
    title = strip_html_to_text(str(fields.get("System.Title", "")))
    description = strip_html_to_text(str(fields.get("System.Description", "")))
    acceptance_text = strip_html_to_text(
        str(
            fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            or fields.get("Custom.AcceptanceCriteria")
            or ""
        )
    )
    if acceptance_text:
        acceptance_lines = [line.strip() for line in acceptance_text.splitlines() if line.strip()]
        acceptance_block = "\n".join(acceptance_lines)
    else:
        extracted = extract_acceptance_criteria_from_text(description)
        acceptance_block = "\n".join(f"- {item}" for item in extracted)

    lines = [title]
    if description:
        lines.extend(["", "Description:", description])
    if acceptance_block:
        lines.extend(["", "Acceptance Criteria:", acceptance_block])

    for label, field_name in (
        ("Work Item Type", "System.WorkItemType"),
        ("Area Path", "System.AreaPath"),
        ("Iteration", "System.IterationPath"),
        ("Story Points", "Microsoft.VSTS.Scheduling.StoryPoints"),
        ("Effort", "Microsoft.VSTS.Scheduling.Effort"),
        ("State", "System.State"),
        ("Tags", "System.Tags"),
    ):
        value = fields.get(field_name)
        if value not in {None, ""}:
            lines.append(f"{label}: {value}")

    lines.extend(
        [
            "",
            f"Azure DevOps Organization: {reference.organization_url}",
            f"Azure DevOps Project: {reference.project}",
            f"Azure DevOps Work Item ID: {reference.work_item_id}",
            f"Azure DevOps URL: {build_work_item_url(reference)}",
        ]
    )
    return "\n".join(lines).strip()


def fetch_ado_context(reference: WorkItemReference, repo_root: Path) -> str:
    """Fetch workflow context directly from Azure DevOps via the Azure CLI."""

    az_bin = shutil.which("az")
    if az_bin is None:
        raise WorkflowError("Azure CLI is not installed. Install `az` and the azure-devops extension, or paste context manually.")

    command = [
        az_bin,
        "boards",
        "work-item",
        "show",
        "--id",
        reference.work_item_id,
        "--org",
        reference.organization_url,
        "--project",
        reference.project,
        "--expand",
        "Relations",
        "--output",
        "json",
        "--only-show-errors",
    ]
    result = run_command(command, cwd=repo_root, timeout_seconds=60)
    if result.exit_code != 0:
        raise WorkflowError(
            "Azure DevOps fetch failed. Ensure `az` is authenticated for this organization/project.\n"
            f"stderr:\n{result.stderr.strip() or '(no stderr)'}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Azure DevOps returned invalid JSON: {exc}") from exc
    return build_ado_context(payload, reference)


def _get_repo_name(repo_root: Path) -> str:
    """Extract the repository name from the origin remote URL."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError("Cannot determine repo name: 'git remote get-url origin' failed")
    url = result.stdout.strip()
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def create_pull_request(
    repo_root: Path,
    source_branch: str,
    base_ref: str,
    change_id: str,
    org_url: str,
    project: str,
    worktree_path: Path | None = None,
) -> None:
    """Push *source_branch* and create an Azure DevOps pull request.

    Non-fatal: callers should catch WorkflowError / subprocess errors.

    Args:
        repo_root: Absolute path to the main repository checkout.
        source_branch: The branch to push and use as the PR source.
        base_ref: The base ref the branch was created from (e.g. ``origin/main``).
        change_id: Work item identifier (e.g. ``WI-4461550``).
        org_url: Azure DevOps organisation URL.
        project: Azure DevOps project name.
        worktree_path: If the branch lives in a worktree, pass its path so the
            push runs from there; otherwise the push runs from *repo_root*.
    """
    repo_name = _get_repo_name(repo_root)
    work_item_id = change_id.removeprefix("WI-")
    push_cwd = str(worktree_path) if worktree_path else str(repo_root)

    log("INFO", f"Pushing branch '{source_branch}' to origin")
    push_result = subprocess.run(
        ["git", "-C", push_cwd, "push", "origin", source_branch],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if push_result.returncode != 0:
        raise WorkflowError(
            f"Failed to push branch '{source_branch}': {push_result.stderr.strip()}"
        )

    target_branch = base_ref.removeprefix("origin/")
    pr_title = f"{change_id}: Automated implementation"
    pr_description = (
        f"## {change_id}\n\n"
        f"Automated implementation generated by the agent workflow runner.\n\n"
        f"Work item: #{work_item_id}\n\n"
        f"---\n"
        f"\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
        f"Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    )

    az_bin = shutil.which("az") or "az"
    cmd = [
        az_bin, "repos", "pr", "create",
        "--repository", repo_name,
        "--source-branch", source_branch,
        "--target-branch", target_branch,
        "--title", pr_title,
        "--description", pr_description,
        "--work-items", work_item_id,
        "--org", org_url,
        "--project", project,
        "--output", "json",
    ]
    log("INFO", f"Creating PR via az repos pr create")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise WorkflowError(
            f"az repos pr create failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        pr_data = json.loads(result.stdout)
        pr_id = pr_data.get("pullRequestId", "?")
        pr_url = pr_data.get("repository", {}).get("remoteUrl", org_url)
        log("OK", f"PR created: #{pr_id}  url={pr_url}")
    except (json.JSONDecodeError, KeyError):
        log("OK", "PR created (could not parse PR URL from az output)")


def choose_resume_candidate(candidates: Sequence[ResumeCandidate], *, input_fn: Callable[[str], str] = input) -> ResumeCandidate:
    """Choose which existing intake artifacts to reuse."""

    if not candidates:
        raise WorkflowError("No reusable intake artifacts were found.")
    if len(candidates) == 1:
        candidate = candidates[0]
        log("INFO", f"Reusing existing intake artifacts for {candidate.change_id}")
        return candidate

    selected = prompt_option(
        "Select a change to resume:",
        [
            f"{candidate.change_id} — updated {datetime.fromtimestamp(candidate.updated_at).strftime('%Y-%m-%d %H:%M:%S')}"
            for candidate in candidates
        ],
        input_fn=input_fn,
        default_index=1,
    )
    candidate = candidates[selected - 1]
    log("INFO", f"Reusing existing intake artifacts for {candidate.change_id}")
    return candidate


def collect_interactive_config(
    *,
    input_fn: Callable[[str], str] = input,
    require_tty: bool = True,
    repo_root: Path | None = None,
    artifact_root: Path | None = None,
) -> WorkflowConfig:
    """Prompt the user for any required startup information."""

    if require_tty and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise WorkflowError("This runner is interactive only. Run it in a terminal without CLI arguments or redirected stdin.")

    print_startup_robot()
    resolved_repo_root = (repo_root or resolve_repo_root()).resolve()
    resolved_artifact_root = (artifact_root or resolved_repo_root / "agent-context").resolve()
    backend = select_backend(input_fn=input_fn)

    print()
    print(f"Repo root:     {resolved_repo_root}")
    print(f"Artifact root: {resolved_artifact_root}")

    resume_candidates = list_resume_candidates(resolved_artifact_root)
    start_options = ["Start from Azure DevOps work item (recommended)"]
    start_keys = ["ado"]
    if resume_candidates:
        start_options.append("Resume using existing intake artifacts")
        start_keys.append("resume")
    start_options.append("Paste workflow context manually")
    start_keys.append("manual")
    start_mode = start_keys[
        prompt_option("How would you like to start?", start_options, input_fn=input_fn, default_index=1) - 1
    ]

    base_config = {
        "repo_root": resolved_repo_root,
        "workflow_assets_root": WORKFLOW_ASSETS_ROOT,
        "artifact_root": resolved_artifact_root,
        "cli_backend": backend.key,
        "cli_bin": backend.command,
        "model": backend.default_model,
    }

    if start_mode == "resume":
        candidate = choose_resume_candidate(resume_candidates, input_fn=input_fn)
        return WorkflowConfig(change_id=candidate.change_id, context="", reuse_existing_intake=True, **base_config)

    if start_mode == "manual":
        change_id = normalize_change_id(prompt_text("Change ID (for example WI-4461550)", input_fn=input_fn))
        if intake_artifacts_exist(resolved_artifact_root, change_id) and prompt_yes_no(
            f"Existing intake artifacts were found for {change_id}. Reuse them and skip intake?",
            input_fn=input_fn,
            default=True,
        ):
            return WorkflowConfig(change_id=change_id, context="", reuse_existing_intake=True, **base_config)
        context = prompt_multiline("Paste workflow context.", input_fn=input_fn)
        return WorkflowConfig(change_id=change_id, context=context, **base_config)

    default_organization, default_project = resolve_ado_defaults(resolved_repo_root)
    raw_reference = prompt_text("Azure DevOps work item ID or URL", input_fn=input_fn)
    reference = parse_work_item_reference(
        raw_reference,
        default_organization=default_organization,
        default_project=default_project,
    )
    change_id = normalize_change_id(reference.work_item_id)

    if intake_artifacts_exist(resolved_artifact_root, change_id) and prompt_yes_no(
        f"Existing intake artifacts were found for {change_id}. Reuse them and skip intake?",
        input_fn=input_fn,
        default=True,
    ):
        return WorkflowConfig(change_id=change_id, context="", reuse_existing_intake=True, **base_config)

    try:
        context = fetch_ado_context(reference, resolved_repo_root)
    except WorkflowError as exc:
        log("WARN", str(exc))
        if not prompt_yes_no("Paste workflow context manually instead?", input_fn=input_fn, default=True):
            raise
        context = prompt_multiline("Paste workflow context.", input_fn=input_fn)

    return WorkflowConfig(change_id=change_id, context=context, **base_config)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML frontmatter from a markdown file.

    The agent prompt frontmatter is intentionally simple (`key: value` pairs), so a
    minimal parser avoids adding a PyYAML dependency.
    """

    if not text.startswith("---\n"):
        return {}

    end_index = text.find("\n---\n", 4)
    if end_index == -1:
        return {}

    frontmatter = text[4:end_index]
    result: dict[str, str] = {}
    for raw_line in frontmatter.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def discover_agents(workflow_assets_root: Path) -> dict[str, AgentSpec]:
    """Discover custom agents and index them by file stem and frontmatter name."""

    agents_dir = workflow_assets_root / "agents"
    if not agents_dir.is_dir():
        raise WorkflowError(f"Agent directory not found: {agents_dir}")

    discovered: dict[str, AgentSpec] = {}
    for path in sorted(agents_dir.glob("*.agent.md")):
        text = path.read_text(encoding="utf-8")
        metadata = parse_frontmatter(text)
        key = path.stem.replace(".agent", "")
        name = metadata.get("name", key)
        description = metadata.get("description", "")
        spec = AgentSpec(key=key, name=name, description=description, path=path)
        discovered[key] = spec
        discovered[name] = spec

    unique_keys = {spec.key for spec in discovered.values()}
    log("INFO", f"Discovered {len(unique_keys)} agent(s): {', '.join(sorted(unique_keys))}")
    return discovered


def resolve_agent(agents: dict[str, AgentSpec], stage_key: str) -> AgentSpec:
    """Resolve the preferred agent for a workflow stage."""

    for alias in STAGE_AGENT_ALIASES[stage_key]:
        spec = agents.get(alias)
        if spec is not None:
            return spec
    raise WorkflowError(
        f"Unable to resolve agent for stage '{stage_key}'. Looked for: {STAGE_AGENT_ALIASES[stage_key]}"
    )


def run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    heartbeat_interval: int = 10,
    early_exit_paths: list[Path] | None = None,
) -> CommandResult:
    """Run a subprocess and capture stdout/stderr, logging a heartbeat every *heartbeat_interval* seconds.

    If *early_exit_paths* is provided the process is killed and a successful
    CommandResult is returned as soon as any of those paths exist on disk,
    without waiting for the full *timeout_seconds* to expire.
    """

    display_cmd = " ".join(command[:6]) + (" ..." if len(command) > 6 else "")
    log("INFO", f"$ {display_cmd}")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        log("ERROR", f"Failed to start process: {exc}")
        return CommandResult(command=command, exit_code=1, stdout="", stderr=str(exc))

    last_heartbeat = t0
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=heartbeat_interval)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - t0
                if elapsed >= timeout_seconds:
                    proc.kill()
                    stdout_partial, stderr_partial = proc.communicate()
                    timeout_message = f"Command timed out after {timeout_seconds} seconds."
                    combined_stderr = "\n".join(
                        part for part in [stderr_partial.strip(), timeout_message] if part
                    ).strip()
                    log("ERROR", f"timeout after {timeout_seconds}s  elapsed={elapsed:.1f}s  cmd={display_cmd}")
                    return CommandResult(
                        command=command,
                        exit_code=124,
                        stdout=stdout_partial,
                        stderr=combined_stderr,
                    )
                if early_exit_paths:
                    for path in early_exit_paths:
                        if path.is_file():
                            log("OK", f"Early exit: artifact found at {path}  elapsed={elapsed:.0f}s — killing process")
                            proc.kill()
                            stdout_partial, _ = proc.communicate()
                            return CommandResult(
                                command=command,
                                exit_code=0,
                                stdout=stdout_partial,
                                stderr="",
                            )
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_interval:
                    log("INFO", f"still running…  elapsed={elapsed:.0f}s  cmd={display_cmd}")
                    last_heartbeat = now
    except KeyboardInterrupt:
        log("WARN", "Interrupted — killing agent process…")
        proc.kill()
        proc.communicate()
        raise

    elapsed = time.monotonic() - t0
    level = "OK" if proc.returncode == 0 else "ERROR"
    log(level, f"exit={proc.returncode}  elapsed={elapsed:.1f}s  cmd={display_cmd}")
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file, creating parent directories when needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    """Write a UTF-8 text file, creating parent directories when needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_runner_log(config: WorkflowConfig, event_type: str, payload: dict[str, Any]) -> Path:
    """Write a workflow-runner log entry into the workflow-runner log folder."""

    log_path = (
        config.artifact_root
        / config.change_id
        / "logs"
        / "workflow_runner"
        / f"{file_timestamp()}_{event_type}.json"
    )
    enriched = {
        "log_type": "workflow_runner",
        "event_type": event_type,
        "timestamp": iso_now(),
        "change_id": config.change_id,
        **payload,
    }
    write_json(log_path, enriched)
    return log_path


# ---------------------------------------------------------------------------
# Escalation pause / resume helpers
# ---------------------------------------------------------------------------


def _status_dir(config: WorkflowConfig) -> Path:
    """Return the status directory for the active change."""
    return config.artifact_root / config.change_id / "status"


def write_escalation_artifact(
    config: WorkflowConfig,
    producer_stage_key: str,
    evaluator_stage_key: str,
    attempt: int,
    eval_payload: dict[str, Any],
    uow_id: str | None = None,
) -> Path:
    """Write a machine-routable ``escalated.json`` from evaluator payload.

    Returns the path to the written file.
    """
    status = _status_dir(config)
    status.mkdir(parents=True, exist_ok=True)
    escalated_path = status / "escalated.json"

    # Extract blocking questions from issues that require escalation
    issues = eval_payload.get("issues") or []
    blocking_questions: list[str] = []
    for issue in issues:
        desc = issue.get("description", "")
        if issue.get("requires_escalation") or issue.get("severity") == "critical":
            blocking_questions.append(desc)
    if not blocking_questions:
        # Fall back to escalation_recommendation reason
        esc_rec = eval_payload.get("escalation_recommendation", {})
        reason = esc_rec.get("reason") if isinstance(esc_rec, dict) else None
        if reason:
            blocking_questions.append(reason)
        else:
            blocking_questions.append("Evaluator escalated without specific questions.")

    esc_rec = eval_payload.get("escalation_recommendation", {})
    reason = (esc_rec.get("reason") if isinstance(esc_rec, dict) else None) or "Evaluator recommended escalation"

    artifact = {
        "stage_key": producer_stage_key,
        "producer_stage_key": producer_stage_key,
        "evaluator_stage_key": evaluator_stage_key,
        "uow_id": uow_id,
        "attempt": attempt,
        "reason": reason,
        "blocking_questions": blocking_questions,
        "recommended_next_action": (
            esc_rec.get("recommended_next_action") if isinstance(esc_rec, dict) else "Provide clarification"
        ) or "Provide clarification",
        "timestamp": iso_now(),
    }

    write_json(escalated_path, artifact)
    write_runner_log(config, "escalation_written", {
        "stage_key": producer_stage_key,
        "evaluator_stage_key": evaluator_stage_key,
        "uow_id": uow_id,
        "attempt": attempt,
        "blocking_questions": blocking_questions,
    })
    log("WARN", f"Escalation artifact written: {escalated_path}")
    return escalated_path


def format_human_resolution(resolution: dict[str, Any]) -> str:
    """Format a resume.json resolution dict into a prompt-friendly text block."""
    lines = ["HUMAN RESOLUTION (authoritative — overrides prior assumptions)"]
    if resolution.get("responder"):
        lines.append(f"- Responder: {resolution['responder']}")
    if resolution.get("timestamp"):
        lines.append(f"- Timestamp: {resolution['timestamp']}")

    answers = resolution.get("answers", {})
    if answers:
        lines.append("- Answers:")
        for key, value in answers.items():
            lines.append(f"  - {key}: {value}")

    constraints = resolution.get("constraints", [])
    if constraints:
        lines.append("- Constraints:")
        for constraint in constraints:
            lines.append(f"  - {constraint}")

    extra = resolution.get("extra_context")
    if extra:
        lines.append(f"- Extra context: {extra}")

    return "\n".join(lines)


def _start_discord_bridge(
    config: WorkflowConfig,
    escalated_path: Path,
    status_dir: Path,
) -> "subprocess.Popen[str] | None":
    """Start the Discord escalation bridge as a background subprocess.

    Returns the Popen object, or None if the bridge script is not found or
    DISCORD_BOT_TOKEN is not configured (graceful fallback to manual wait).
    """
    bridge_script = config.workflow_assets_root / "scripts" / "discord_escalation_bridge.py"
    if not bridge_script.is_file():
        log("WARN", f"Discord bridge script not found: {bridge_script}  (manual resume only)")
        return None

    import os as _os
    if not _os.environ.get("DISCORD_BOT_TOKEN") and not _os.environ.get("DISCORD_DRY_RUN"):
        log("WARN", "DISCORD_BOT_TOKEN not set — Discord escalation disabled (manual resume only)")
        return None

    cmd = [
        sys.executable,
        str(bridge_script),
        "--escalated-path", str(escalated_path),
        "--status-dir", str(status_dir),
        "--change-id", config.change_id,
    ]
    if _os.environ.get("DISCORD_DRY_RUN"):
        cmd.append("--dry-run")

    log("INFO", "Starting Discord escalation bridge...")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(config.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log("OK", f"Discord bridge started (pid={proc.pid})")
        return proc
    except OSError as exc:
        log("WARN", f"Failed to start Discord bridge: {exc}  (manual resume only)")
        return None


def wait_for_resume(config: WorkflowConfig, poll_seconds: int = 5) -> dict[str, Any] | None:
    """Check for ``escalated.json`` and block until ``resume.json`` appears.

    When DISCORD_BOT_TOKEN is set (or DISCORD_DRY_RUN for testing), the Discord
    escalation bridge is launched automatically to post the escalation to Discord
    and listen for a RESUME: reply, creating resume.json without manual file editing.

    Falls back to manual resume.json creation when Discord is not configured.

    Returns the parsed resume dict when resolved, or *None* if no escalation
    was active.
    """
    status = _status_dir(config)
    escalated_path = status / "escalated.json"
    paused_path = status / "paused.json"
    resume_path = status / "resume.json"
    archive_dir = status / "escalated_archive"

    if not escalated_path.exists():
        return None

    # Read escalation details
    try:
        escalation = json.loads(escalated_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        escalation = {}

    # Ensure paused marker
    if not paused_path.exists():
        status.mkdir(parents=True, exist_ok=True)
        write_json(paused_path, {
            "paused_at": iso_now(),
            "triggered_by": "escalated.json",
            "escalation_file": "status/escalated.json",
        })

    # Print pause message
    questions = escalation.get("blocking_questions", [])
    questions_text = "\n".join(f"  • {q}" for q in questions) if questions else "  (none specified)"
    stage = escalation.get("stage_key", "unknown")
    reason = escalation.get("reason", "No reason specified")

    log("WARN", "=" * 64)
    log("WARN", "⏸  WORKFLOW PAUSED — Human Input Required")
    log("WARN", "=" * 64)
    log("WARN", f"Stage:   {stage}")
    log("WARN", f"Reason:  {reason}")
    log("WARN", f"Blocking Questions:\n{questions_text}")
    log("WARN", f"To resume, create:\n  {resume_path}")
    log("WARN", '  With JSON: {"responder": "<name>", "answers": {...}, "constraints": [...]}')
    log("WARN", "  OR reply RESUME: in the Discord thread (if Discord is configured)")
    log("WARN", "=" * 64)

    write_runner_log(config, "workflow_paused", {
        "stage_key": stage,
        "reason": reason,
        "blocking_questions": questions,
        "escalated_path": str(escalated_path),
        "resume_path": str(resume_path),
    })

    # ── Start Discord bridge (non-blocking, falls back gracefully) ────────────
    bridge_proc = _start_discord_bridge(config, escalated_path, status)

    # ── Poll loop ─────────────────────────────────────────────────────────────
    try:
        while not resume_path.exists():
            # Stream bridge output to console so the operator can see what Discord bridge is doing
            if bridge_proc is not None and bridge_proc.poll() is None:
                # Non-blocking readline drain
                try:
                    import select as _select
                    if bridge_proc.stdout and _select.select([bridge_proc.stdout], [], [], 0)[0]:
                        line = bridge_proc.stdout.readline()
                        if line.strip():
                            log("INFO", f"[discord] {line.rstrip()}")
                except Exception:
                    pass
            elif bridge_proc is not None and bridge_proc.poll() is not None:
                # Bridge has exited — drain remaining output then fall back to manual poll
                if bridge_proc.stdout:
                    for line in bridge_proc.stdout:
                        if line.strip():
                            log("INFO", f"[discord] {line.rstrip()}")
                bridge_exit = bridge_proc.returncode
                if bridge_exit == 0:
                    # Bridge wrote resume.json — loop condition handles it
                    break
                elif bridge_exit == 2:
                    # Bridge saw externally-created resume.json
                    break
                else:
                    log("WARN", f"Discord bridge exited with code {bridge_exit} — waiting for manual resume.json")
                    bridge_proc = None  # Stop trying to drain; fall through to plain sleep poll
            time.sleep(poll_seconds)
    finally:
        # Always clean up the bridge process
        if bridge_proc is not None and bridge_proc.poll() is None:
            log("INFO", "Terminating Discord bridge process")
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge_proc.kill()

    # Load resolution
    try:
        resolution = json.loads(resume_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log("ERROR", f"Failed to read resume.json: {exc}")
        resolution = {"responder": "unknown", "answers": {}, "constraints": []}

    # Archive escalated.json
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{file_timestamp()}_escalated.json"
    shutil.move(str(escalated_path), str(archive_dir / archive_name))

    # Clean up markers
    if paused_path.exists():
        paused_path.unlink()
    resume_path.unlink()

    log("OK", f"[RESUMED] Escalation cleared. Archived → {archive_dir / archive_name}")
    write_runner_log(config, "workflow_resumed", {
        "resolution": resolution,
        "archive_path": str(archive_dir / archive_name),
    })

    return resolution


def call_helper_script(config: WorkflowConfig, script_name: str, *args: str) -> CommandResult:
    """Run one of the repository's helper scripts."""

    script_path = config.workflow_assets_root / "scripts" / script_name
    if not script_path.is_file():
        raise WorkflowError(f"Helper script not found: {script_path}")

    log("INFO", f"Running helper script: {script_name}  args={args}")
    command = [sys.executable, str(script_path), *args]
    result = run_command(command, cwd=config.repo_root, timeout_seconds=config.timeout_seconds)
    if result.exit_code != 0:
        raise WorkflowError(
            f"Helper script failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    log("OK", f"Helper script succeeded: {script_name}")
    return result


def ensure_artifact_dirs(config: WorkflowConfig) -> None:
    """Create the artifact directory tree using the repository helper script."""

    call_helper_script(
        config,
        "init-artifact-dirs.py",
        str(config.artifact_root),
        config.change_id,
    )


def strip_agent_bullet_prefix(text: str) -> str:
    """Normalize common CLI bullet prefixes from Copilot responses."""

    lines = text.splitlines()
    if len(lines) == 1 and lines[0].startswith("● "):
        return lines[0][2:].strip()
    return text.strip()


def build_agent_command(config: WorkflowConfig, agent: AgentSpec, prompt: str) -> list[str]:
    """Build the backend-specific CLI command for a single agent invocation."""

    seen_roots: set[Path] = set()
    add_dirs: list[str] = []
    for root in [config.repo_root, config.workflow_assets_root, config.artifact_root, *config.additional_dirs]:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        add_dirs.extend(["--add-dir", str(root)])

    if config.cli_backend == "copilot":
        command = [
            config.cli_bin,
            "-p",
            prompt,
            "--agent",
            agent.key,
            "--allow-all",
            "--no-ask-user",
            "-s",
            "--stream",
            "off",
            "--output-format",
            "text",
            *add_dirs,
        ]
        if config.model:
            command.extend(["--model", config.model])
        return command

    if config.cli_backend == "claude":
        command = [
            config.cli_bin,
            "--print",
            "--agent",
            agent.name,
            "--output-format",
            "text",
            "--permission-mode",
            "bypassPermissions",
            *add_dirs,
        ]
        if config.model:
            command.extend(["--model", config.model])
        command.extend(["--", prompt])  # -- prevents variadic --add-dir from consuming the prompt
        return command

    raise WorkflowError(f"Unsupported AI backend: {config.cli_backend}")


def build_intake_prompt(config: WorkflowConfig) -> str:
    """Create the non-interactive prompt for the intake agent."""

    return textwrap.dedent(
        f"""
        Automation run for stage `intake`. Do not ask the user questions.

        Workflow assets root: {config.workflow_assets_root}
        Code repo: {config.repo_root}
        Artifact root: {config.artifact_root}
        Change ID: {config.change_id}

        Treat the following context as already supplied by the workflow runner.
        Your job in this stage is only to normalize the intake and create or
        refresh these artifacts under `{config.artifact_root / config.change_id}`:

        - `intake/story.yaml`
        - `intake/config.yaml`
        - `intake/constraints.md`

        Do not orchestrate later stages, run evaluator loops, invoke other
        agents, or tell the user how to continue the workflow. Capture missing
        details as open questions inside `constraints.md`.

        Context:
        {config.context}
        """
    ).strip()


def build_producer_prompt(
    config: WorkflowConfig,
    stage_label: str,
    attempt: int,
    artifact_paths: Iterable[Path],
    feedback_path: Path | None = None,
    feedback_summary: str | None = None,
    uow_id: str | None = None,
    human_resolution: dict[str, Any] | None = None,
) -> str:
    """Build a prompt for a non-evaluator stage agent."""

    artifact_lines = "\n".join(f"- {path}" for path in artifact_paths)
    feedback_line = "This is the first attempt for this stage."
    if feedback_path is not None:
        feedback_line = f"Previous evaluator feedback is available at `{feedback_path}`. Address it fully."
        if feedback_summary:
            feedback_line += f"\n\nEvaluator fixes to apply:\n{feedback_summary}"
    elif feedback_summary:
        feedback_line = (
            "The previous attempt did not complete successfully. "
            "Address the runner-captured failure details below."
        )
        feedback_line += f"\n\nRunner-captured failure details:\n{feedback_summary}"
    uow_line = f"Target only `{uow_id}`. Stay within that UoW's scope." if uow_id else ""

    # Format human resolution block if present
    resolution_block = ""
    if human_resolution:
        resolution_block = "\n\n" + format_human_resolution(human_resolution)

    # Add schema specifications for known artifact types
    schema_section = ""
    if stage_label == "task_assigner":
        schema_section = textwrap.dedent(
            """

            ## ASSIGNMENTS.JSON SCHEMA (Required Format)

            The assignments.json artifact MUST conform to this exact structure:

            ```json
            {
              "batches": [
                {
                  "batch_id": 1,
                  "uows": [
                    {
                      "uow_id": "UOW-001",
                      "source_task_id": "T1",
                      "title": "Optional title",
                      "dependencies": [],
                      "definition_of_done": []
                    }
                  ]
                }
              ]
            }
            ```

            CRITICAL REQUIREMENTS:
            - Root key MUST be "batches" (NOT "execution_schedule")
            - batches MUST be an array of batch objects
            - Each batch MUST have "batch_id" (integer) and "uows" (array)
            - Each UoW MUST have "uow_id" (string) and "source_task_id" (string)
            - All string fields must be non-empty
            - All required fields must be present in every object
            """
        ).strip()

    return textwrap.dedent(
        f"""
        Automation run for stage `{stage_label}`.
        Workflow assets root: {config.workflow_assets_root}
        Code repo: {config.repo_root}
        Artifact root: {config.artifact_root}
        Change ID: {config.change_id}
        Attempt number: {attempt}
        {uow_line}

        Required artifacts for this stage:
        {artifact_lines}

        {feedback_line}{resolution_block}{schema_section}

        Do the documented work for your agent and write or update the expected
        artifacts in the change folder. Return a concise status summary only.
        """
    ).strip()


def build_evaluator_prompt(
    config: WorkflowConfig,
    stage_label: str,
    attempt: int,
    artifact_paths: Iterable[Path],
    uow_id: str | None = None,
) -> str:
    """Build a prompt for an evaluator stage agent."""

    artifact_lines = "\n".join(f"- {path}" for path in artifact_paths)
    uow_line = f"Evaluate only `{uow_id}`." if uow_id else ""

    return textwrap.dedent(
        f"""
        Automation run for evaluator stage `{stage_label}`.
        Workflow assets root: {config.workflow_assets_root}
        Code repo: {config.repo_root}
        Artifact root: {config.artifact_root}
        Change ID: {config.change_id}
        Attempt number: {attempt}
        {uow_line}

        Evaluate the current stage artifacts:
        {artifact_lines}

        Write the evaluation artifact for attempt {attempt} in the documented
        location and return a concise pass/fail summary only.
        """
    ).strip()


def build_lessons_prompt(config: WorkflowConfig) -> str:
    """Build a prompt for the lessons optimizer stage."""

    return textwrap.dedent(
        f"""
        Automation run for the lessons optimization stage.
        Workflow assets root: {config.workflow_assets_root}
        Code repo: {config.repo_root}
        Artifact root: {config.artifact_root}
        Change ID: {config.change_id}

        Read the workflow artifacts for this change and run the terminal lessons
        optimization stage. Write the summary artifact and return a concise status
        summary only.
        """
    ).strip()


def dry_run_story_yaml(config: WorkflowConfig) -> str:
    """Generate a minimal story artifact for dry-run mode."""

    escaped_context = config.context.strip().replace('"', "'")
    return textwrap.dedent(
        f"""
        change_id: "{config.change_id}"
        title: "Dry-run workflow"
        description: "Synthetic story created by agent_workflow_runner dry-run mode"
        acceptance_criteria:
          - id: "AC1"
            description: "Workflow runner completes all documented stages"
            testable: true
            notes: null
        examples: []
        constraints: []
        non_functional_requirements: []
        raw_input: "{escaped_context}"
        ado_provenance:
          work_item_id: null
          organization: null
          project: null
          fields_auto_filled: []
        planning_docs: []
        """
    ).strip() + "\n"


def dry_run_config_yaml(config: WorkflowConfig) -> str:
    """Generate a minimal config artifact for dry-run mode."""

    return textwrap.dedent(
        f"""
        change_id: "{config.change_id}"
        code_repo: "{config.repo_root}"
        project_type: "brownfield"
        planning_docs_root: ""
        planning_docs_paths: []
        created_at: "{iso_now()}"
        model_assignments: {{}}
        iteration_limits:
          task_plan: {config.max_task_plan_attempts}
          assignment: {config.max_assignment_attempts}
          implementation: {config.max_implementation_attempts}
          qa: {config.max_qa_attempts}
        run_metadata:
          status: "intake_complete"
          current_stage: "intake"
          started_at: "{iso_now()}"
        """
    ).strip() + "\n"


def materialize_dry_run_artifacts(
    config: WorkflowConfig,
    stage_key: str,
    attempt: int,
    uow_id: str | None = None,
) -> list[Path]:
    """Create synthetic artifacts so dry-run mode exercises control flow end-to-end."""

    base = config.artifact_root / config.change_id
    created: list[Path] = []

    if stage_key == "intake":
        story_path = base / "intake" / "story.yaml"
        config_path = base / "intake" / "config.yaml"
        constraints_path = base / "intake" / "constraints.md"
        write_text(story_path, dry_run_story_yaml(config))
        write_text(config_path, dry_run_config_yaml(config))
        write_text(constraints_path, "# Dry-run constraints\n\n- No additional constraints.\n")
        created.extend([story_path, config_path, constraints_path])
    elif stage_key == "task_generator":
        tasks_path = base / "planning" / "tasks.yaml"
        write_text(
            tasks_path,
            textwrap.dedent(
                f"""
                story_id: "{config.change_id}"
                tasks:
                  - task_id: "T1"
                    title: "Dry-run task"
                    description: "Validate the workflow runner path"
                    acceptance_criteria_mapped: ["AC1"]
                    dependencies: []
                    priority: "high"
                    estimated_complexity: "simple"
                ac_coverage_matrix:
                  AC1: ["T1"]
                notes: "Synthetic dry-run tasks"
                """
            ).strip()
            + "\n",
        )
        created.append(tasks_path)
    elif stage_key == "task_plan_evaluator":
        eval_path = base / "planning" / f"eval_tasks_{attempt}.json"
        write_json(
            eval_path,
            {
                "evaluation_id": f"DRY-TASK-{attempt}",
                "artifact_evaluated": "planning/tasks.yaml",
                "attempt_number": attempt,
                "overall_result": "pass",
                "score": 100,
                "programmatic_gates": {"all_gates_passed": True},
                "rubric_results": {},
                "issues": [],
                "actionable_fixes_summary": [],
                "escalation_recommendation": {"required": False, "reason": None},
                "notes": "Dry-run evaluation passed.",
            },
        )
        created.append(eval_path)
    elif stage_key == "task_assigner":
        assignments_path = base / "planning" / "assignments.json"
        write_json(
            assignments_path,
            {
                "story_id": config.change_id,
                "batches": [
                    {
                        "batch_id": 1,
                        "batch": 1,
                        "parallel_execution": False,
                        "batch_rationale": "Dry-run batch",
                        "uows": [
                            {
                                "uow_id": "UOW-001",
                                "source_task_id": "T1",
                                "assigned_role": "software-engineer",
                                "priority_in_batch": 1,
                                "rationale": "Dry-run execution",
                            }
                        ],
                    }
                ],
                "critical_path": ["UOW-001"],
                "estimated_total_batches": 1,
            },
        )
        created.append(assignments_path)
    elif stage_key == "assignment_evaluator":
        eval_path = base / "planning" / f"eval_assignments_{attempt}.json"
        write_json(
            eval_path,
            {
                "evaluation_id": f"DRY-ASG-{attempt}",
                "artifact_evaluated": "planning/assignments.json",
                "attempt_number": attempt,
                "overall_result": "pass",
                "score": 100,
                "programmatic_gates": {"all_gates_passed": True},
                "rubric_results": {},
                "issues": [],
                "actionable_fixes_summary": [],
                "escalation_recommendation": {"required": False, "reason": None},
                "notes": "Dry-run evaluation passed.",
            },
        )
        created.append(eval_path)
    elif stage_key == "software_engineer":
        if not uow_id:
            raise WorkflowError("dry-run software_engineer stage requires a uow_id")
        uow_dir = base / "execution" / uow_id
        uow_spec_path = uow_dir / "uow_spec.yaml"
        impl_report_path = uow_dir / "impl_report.yaml"
        write_text(
            uow_spec_path,
            textwrap.dedent(
                f"""
                uow_id: "{uow_id}"
                source_task_id: "T1"
                description: "Dry-run UoW"
                definition_of_done:
                  - "Runner reaches implementation stage"
                """
            ).strip()
            + "\n",
        )
        write_text(
            impl_report_path,
            textwrap.dedent(
                f"""
                uow_id: "{uow_id}"
                summary: "Dry-run implementation completed"
                files_modified: []
                definition_of_done_status:
                  - item: "Runner reaches implementation stage"
                    met: true
                    evidence: "Synthetic dry-run evidence"
                """
            ).strip()
            + "\n",
        )
        created.extend([uow_spec_path, impl_report_path])
    elif stage_key == "implementation_evaluator":
        if not uow_id:
            raise WorkflowError("dry-run implementation_evaluator stage requires a uow_id")
        eval_path = base / "execution" / uow_id / f"eval_impl_{attempt}.json"
        write_json(
            eval_path,
            {
                "evaluation_id": f"DRY-IMPL-{uow_id}-{attempt}",
                "artifact_evaluated": f"execution/{uow_id}/impl_report.yaml",
                "attempt_number": attempt,
                "overall_result": "pass",
                "score": 100,
                "programmatic_gates": {"all_gates_passed": True},
                "rubric_results": {},
                "issues": [],
                "actionable_fixes_summary": [],
                "escalation_recommendation": {"required": False, "reason": None},
                "notes": "Dry-run implementation evaluation passed.",
            },
        )
        created.append(eval_path)
    elif stage_key == "qa":
        qa_report_path = base / "qa" / "qa_report.yaml"
        write_text(
            qa_report_path,
            textwrap.dedent(
                f"""
                change_id: "{config.change_id}"
                overall_status: "pass"
                acceptance_criteria_validation:
                  - id: "AC1"
                    result: "pass"
                    evidence: "Dry-run workflow completed."
                regression_risk: "low"
                """
            ).strip()
            + "\n",
        )
        created.append(qa_report_path)
    elif stage_key == "qa_evaluator":
        eval_path = base / "qa" / f"eval_qa_{attempt}.json"
        write_json(
            eval_path,
            {
                "evaluation_id": f"DRY-QA-{attempt}",
                "artifact_evaluated": "qa/qa_report.yaml",
                "attempt_number": attempt,
                "overall_result": "pass",
                "score": 100,
                "programmatic_gates": {"all_gates_passed": True},
                "rubric_results": {},
                "issues": [],
                "actionable_fixes_summary": [],
                "escalation_recommendation": {"required": False, "reason": None},
                "notes": "Dry-run QA evaluation passed.",
            },
        )
        created.append(eval_path)
    elif stage_key == "lessons_optimizer":
        report_path = base / "summary" / "lessons_optimizer_report.yaml"
        write_text(
            report_path,
            textwrap.dedent(
                f"""
                change_id: "{config.change_id}"
                status: "complete"
                summary: "Dry-run lessons stage completed"
                recommendations: []
                """
            ).strip()
            + "\n",
        )
        created.append(report_path)

    return created


def invoke_agent(
    config: WorkflowConfig,
    agent: AgentSpec,
    prompt: str,
    stage_key: str,
    attempt: int,
    uow_id: str | None = None,
    raise_on_error: bool = True,
    early_exit_paths: list[Path] | None = None,
) -> CommandResult:
    """Invoke an agent or synthesize its outputs in dry-run mode."""

    uow_label = f"  uow={uow_id}" if uow_id else ""
    log("AGENT", f"Dispatching agent '{agent.key}'  stage={stage_key}  attempt={attempt}{uow_label}")

    write_runner_log(
        config,
        "agent_dispatch",
        {
            "stage_key": stage_key,
            "attempt": attempt,
            "uow_id": uow_id,
            "agent": {"key": agent.key, "name": agent.name, "path": str(agent.path)},
        },
    )

    if config.dry_run:
        log("INFO", f"[dry-run] Materializing synthetic artifacts for stage={stage_key}{uow_label}")
        dry_run_artifacts = materialize_dry_run_artifacts(config, stage_key, attempt, uow_id=uow_id)
        log("OK", f"[dry-run] Created {len(dry_run_artifacts)} artifact(s)")
        for artifact in dry_run_artifacts:
            log("INFO", f"  → {artifact}")
        write_runner_log(
            config,
            "dry_run_stage_materialized",
            {
                "stage_key": stage_key,
                "attempt": attempt,
                "uow_id": uow_id,
                "artifacts": [str(path) for path in dry_run_artifacts],
            },
        )
        return CommandResult(
            command=[config.cli_bin, "--dry-run", agent.key],
            exit_code=0,
            stdout=f"dry-run completed for {agent.key}",
            stderr="",
        )

    command = build_agent_command(config, agent, prompt)
    log("INFO", f"Prompt preview: {prompt[:200].strip().replace(chr(10), ' ')!r}…")
    result = run_command(
        command,
        cwd=config.repo_root,
        timeout_seconds=config.timeout_seconds,
        early_exit_paths=early_exit_paths,
    )

    print_agent_output(result, agent.key)

    write_runner_log(
        config,
        "agent_result",
        {
            "stage_key": stage_key,
            "attempt": attempt,
            "uow_id": uow_id,
            "agent": agent.key,
            "exit_code": result.exit_code,
            "stdout": strip_agent_bullet_prefix(result.stdout),
            "stderr": result.stderr,
            "command": command,
        },
    )

    if result.exit_code != 0:
        log("ERROR", f"Agent '{agent.key}' failed for stage '{stage_key}' attempt {attempt} (exit={result.exit_code})")
        if raise_on_error:
            raise WorkflowError(
                f"Agent '{agent.key}' failed for stage '{stage_key}' attempt {attempt}.\n"
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            )
        return result

    log("OK", f"Agent '{agent.key}' completed successfully for stage={stage_key} attempt={attempt}{uow_label}")
    return result


def read_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON document with a clear error message on failure."""

    if not path.is_file():
        raise WorkflowError(f"Expected JSON artifact does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Failed to parse JSON artifact {path}: {exc}") from exc


def validate_artifact_schema(
    config: WorkflowConfig,
    artifact_type: str,
    artifact_path: Path,
) -> tuple[bool, str]:
    """Validate an artifact against its schema before evaluation.

    Returns (is_valid, error_message).
    """

    validation_script = config.workflow_assets_root / "scripts" / "validate-artifact-schema.py"
    if not validation_script.is_file():
        log("WARN", f"Schema validation script not found: {validation_script}")
        return True, ""

    result = run_command(
        [sys.executable, str(validation_script), "--type", artifact_type, str(artifact_path)],
        cwd=config.repo_root,
        timeout_seconds=30,
    )

    if result.exit_code == 0:
        return True, ""

    # Parse the validation JSON output to extract issues
    try:
        output = json.loads(result.stdout) if result.stdout else {}
        issues = output.get("issues", [])
        error_msg = "Schema validation failed:\n"
        for issue in issues:
            error_msg += f"  - {issue.get('path', '?')}: {issue.get('issue', '?')}\n"
        return False, error_msg.strip()
    except Exception:
        return False, f"Schema validation failed (exit code {result.exit_code})"


def evaluation_signature(payload: dict[str, Any]) -> str:
    """Build a stable signature for plateau detection across evaluator attempts."""

    normalized = dict(payload)
    normalized.pop("evaluation_id", None)
    normalized.pop("attempt_number", None)
    normalized.pop("score", None)
    normalized.pop("notes", None)
    serialized = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def read_evaluation_result(path: Path) -> tuple[bool, dict[str, Any]]:
    """Return the evaluation payload and whether it passed."""

    payload = read_json_file(path)
    return payload.get("overall_result") == "pass", payload


def extract_feedback_summary(payload: dict[str, Any]) -> str:
    """Convert evaluator output into prompt-friendly feedback text."""

    fixes = payload.get("actionable_fixes_summary") or []
    issues = payload.get("issues") or []
    lines: list[str] = []
    for item in fixes:
        lines.append(f"- {item}")
    if not lines:
        for issue in issues:
            description = issue.get("description", "unspecified issue")
            action = issue.get("actionable_fix", "no actionable fix provided")
            location = issue.get("location", "unknown location")
            lines.append(f"- {location}: {description} | Fix: {action}")
    if not lines:
        lines.append("- Evaluator reported failure without actionable details.")
    return "\n".join(lines)


def extract_command_failure_summary(result: CommandResult) -> str:
    """Convert a failed agent command into retry guidance."""

    lines = [f"- Previous attempt exited with code {result.exit_code}."]
    stderr_excerpt = result.stderr.strip()
    stdout_excerpt = strip_agent_bullet_prefix(result.stdout).strip()
    if stderr_excerpt:
        lines.append(f"- stderr: {stderr_excerpt}")
    if stdout_excerpt:
        lines.append(f"- stdout: {stdout_excerpt}")
    return "\n".join(lines)


def load_execution_schedule(assignments_path: Path) -> list[dict[str, Any]]:
    """Load the execution schedule batches from `assignments.json`."""

    payload = read_json_file(assignments_path)
    schedule = payload.get("batches")
    if schedule is None:
        schedule = payload.get("execution_schedule")
    if not isinstance(schedule, list):
        raise WorkflowError(
            f"Invalid assignments.json: expected batches list (or legacy execution_schedule list) in {assignments_path}"
        )
    return schedule


def run_stage_loop(
    config: WorkflowConfig,
    agents: dict[str, AgentSpec],
    producer_stage_key: str,
    evaluator_stage_key: str,
    producer_artifacts: list[Path],
    evaluator_artifacts: list[Path],
    evaluation_path_for_attempt: Callable[[int], Path],
    max_attempts: int,
) -> StageResult:
    """Run a producer/evaluator loop until pass or until retries are exhausted."""

    producer = resolve_agent(agents, producer_stage_key)
    evaluator = resolve_agent(agents, evaluator_stage_key)
    previous_signature: str | None = None
    feedback_path: Path | None = None
    feedback_summary: str | None = None
    human_resolution: dict[str, Any] | None = None

    log("INFO", f"Starting producer/evaluator loop: {producer_stage_key} ↔ {evaluator_stage_key}  max_attempts={max_attempts}")

    for attempt in range(1, max_attempts + 1):
        log("INFO", f"[{producer_stage_key}] Attempt {attempt}/{max_attempts} — running producer '{producer.key}'")
        producer_prompt = build_producer_prompt(
            config=config,
            stage_label=producer_stage_key,
            attempt=attempt,
            artifact_paths=producer_artifacts,
            feedback_path=feedback_path,
            feedback_summary=feedback_summary,
            human_resolution=human_resolution,
        )
        producer_result = invoke_agent(
            config,
            producer,
            producer_prompt,
            producer_stage_key,
            attempt,
            raise_on_error=False,
        )
        if producer_result.exit_code != 0:
            feedback_path = None
            feedback_summary = extract_command_failure_summary(producer_result)
            log(
                "WARN",
                f"[{producer_stage_key}] Producer command failed on attempt {attempt} "
                f"(exit={producer_result.exit_code})",
            )
            log("INFO", f"[{producer_stage_key}] Feedback for next attempt:\n{feedback_summary}")
            continue

        # Pre-validate artifact schema before evaluator runs
        schema_validation_enabled = {
            "task_generator": "tasks",
            "task_assigner": "assignments",
        }
        if producer_stage_key in schema_validation_enabled:
            artifact_type = schema_validation_enabled[producer_stage_key]
            # Determine which artifact to validate based on stage
            if producer_stage_key == "task_generator":
                artifact_to_validate = config.artifact_root / config.change_id / "planning" / "tasks.yaml"
            elif producer_stage_key == "task_assigner":
                artifact_to_validate = config.artifact_root / config.change_id / "planning" / "assignments.json"
            else:
                artifact_to_validate = None

            if artifact_to_validate and artifact_to_validate.exists():
                is_valid, validation_error = validate_artifact_schema(config, artifact_type, artifact_to_validate)
                if not is_valid:
                    feedback_path = None
                    feedback_summary = f"CRITICAL: Schema validation failed before evaluation:\n{validation_error}"
                    log(
                        "WARN",
                        f"[{producer_stage_key}] Artifact schema validation FAILED on attempt {attempt}",
                    )
                    log("INFO", f"[{producer_stage_key}] Feedback for next attempt:\n{feedback_summary}")
                    continue
                else:
                    log("OK", f"[{producer_stage_key}] Artifact schema validation PASSED")

        evaluation_path = evaluation_path_for_attempt(attempt)
        log("INFO", f"[{producer_stage_key}] Attempt {attempt}/{max_attempts} — running evaluator '{evaluator.key}'")
        evaluator_prompt = build_evaluator_prompt(
            config=config,
            stage_label=evaluator_stage_key,
            attempt=attempt,
            artifact_paths=evaluator_artifacts,
        )
        evaluator_result = invoke_agent(
            config,
            evaluator,
            evaluator_prompt,
            evaluator_stage_key,
            attempt,
            raise_on_error=False,
            early_exit_paths=[evaluation_path],
        )
        if evaluator_result.exit_code != 0:
            if evaluation_path.is_file():
                log(
                    "WARN",
                    f"[{producer_stage_key}] Evaluator exited {evaluator_result.exit_code} "
                    f"but artifact already exists — using it",
                )
            else:
                feedback_path = None
                feedback_summary = extract_command_failure_summary(evaluator_result)
                log(
                    "WARN",
                    f"[{producer_stage_key}] Evaluator command failed on attempt {attempt} "
                    f"(exit={evaluator_result.exit_code})",
                )
                log("INFO", f"[{producer_stage_key}] Feedback for next attempt:\n{feedback_summary}")
                continue
        passed, payload = read_evaluation_result(evaluation_path)
        signature = evaluation_signature(payload)
        write_runner_log(
            config,
            "evaluation_result",
            {
                "stage_key": producer_stage_key,
                "evaluator_stage_key": evaluator_stage_key,
                "attempt": attempt,
                "passed": passed,
                "evaluation_path": str(evaluation_path),
            },
        )

        score = payload.get("score", "?")
        result_label = payload.get("overall_result", "?")
        if passed:
            log("OK", f"[{producer_stage_key}] Evaluator PASSED on attempt {attempt}  score={score}  result={result_label}")
            return StageResult(
                stage_name=producer_stage_key,
                passed=True,
                attempts=attempt,
                artifact_paths=[*producer_artifacts, evaluation_path],
                details={"evaluation": payload},
            )

        log("WARN", f"[{producer_stage_key}] Evaluator FAILED on attempt {attempt}  score={score}  result={result_label}")
        issues = payload.get("issues") or []
        for issue in issues:
            log("WARN", f"  Issue: {issue.get('description', issue)}")

        # ── Escalation check ──────────────────────────────────────────
        escalation_rec = payload.get("escalation_recommendation", {})
        escalation_required = isinstance(escalation_rec, dict) and escalation_rec.get("required")
        escalated_on_disk = (_status_dir(config) / "escalated.json").exists()

        if escalation_required or escalated_on_disk:
            if not escalated_on_disk:
                write_escalation_artifact(
                    config, producer_stage_key, evaluator_stage_key, attempt, payload,
                )
            resolution = wait_for_resume(config)
            if resolution:
                human_resolution = resolution
                # Don't update previous_signature — let human resolution guide next attempt
                feedback_path = evaluation_path
                feedback_summary = extract_feedback_summary(payload)
                log("INFO", f"[{producer_stage_key}] Retrying with human resolution")
                continue
        # ── End escalation check ──────────────────────────────────────

        if signature == previous_signature:
            log("ERROR", f"[{producer_stage_key}] Similarity plateau detected — aborting after attempt {attempt}")
            raise WorkflowError(
                f"Similarity plateau detected in stage '{producer_stage_key}' after attempt {attempt}."
            )
        previous_signature = signature
        feedback_path = evaluation_path
        feedback_summary = extract_feedback_summary(payload)
        log("INFO", f"[{producer_stage_key}] Feedback summary for next attempt:\n{feedback_summary}")

    raise WorkflowError(
        f"Stage '{producer_stage_key}' exceeded max attempts ({max_attempts}) without passing evaluation."
    )


def run_execution_loop(config: WorkflowConfig, agents: dict[str, AgentSpec]) -> StageResult:
    """Execute each scheduled UoW with an implementation evaluator loop."""

    assignments_path = config.artifact_root / config.change_id / "planning" / "assignments.json"
    schedule = load_execution_schedule(assignments_path)
    software_engineer = resolve_agent(agents, "software_engineer")
    implementation_evaluator = resolve_agent(agents, "implementation_evaluator")

    total_uows = sum(len(batch.get("uows", [])) for batch in schedule)
    log("INFO", f"Execution loop: {len(schedule)} batch(es), {total_uows} UoW(s) total")

    completed_uows: list[str] = []
    produced_paths: list[Path] = [assignments_path]

    for batch_index, batch in enumerate(schedule, start=1):
        uows = batch.get("uows", [])
        log("INFO", f"Batch {batch_index}/{len(schedule)}: {len(uows)} UoW(s)  parallel={batch.get('parallel_execution', False)}")
        for uow in uows:
            uow_id = uow.get("uow_id")
            if not isinstance(uow_id, str) or not uow_id:
                raise WorkflowError(f"Invalid UoW entry in assignments.json: {uow}")

            log("INFO", f"  Starting UoW '{uow_id}'  role={uow.get('assigned_role', '?')}")
            feedback_path: Path | None = None
            previous_signature: str | None = None
            feedback_summary: str | None = None
            human_resolution: dict[str, Any] | None = None
            for attempt in range(1, config.max_implementation_attempts + 1):
                log("INFO", f"  [UoW {uow_id}] Attempt {attempt}/{config.max_implementation_attempts} — engineer")
                producer_prompt = build_producer_prompt(
                    config=config,
                    stage_label="software_engineer",
                    attempt=attempt,
                    artifact_paths=[
                        config.artifact_root / config.change_id / "planning" / "tasks.yaml",
                        assignments_path,
                        config.artifact_root / config.change_id / "intake" / "story.yaml",
                    ],
                    feedback_path=feedback_path,
                    feedback_summary=feedback_summary,
                    uow_id=uow_id,
                    human_resolution=human_resolution,
                )
                producer_result = invoke_agent(
                    config,
                    software_engineer,
                    producer_prompt,
                    "software_engineer",
                    attempt,
                    uow_id=uow_id,
                    raise_on_error=False,
                )
                if producer_result.exit_code != 0:
                    feedback_path = None
                    feedback_summary = extract_command_failure_summary(producer_result)
                    log(
                        "WARN",
                        f"  [UoW {uow_id}] Engineer command failed on attempt {attempt} "
                        f"(exit={producer_result.exit_code})",
                    )
                    log("INFO", f"  [UoW {uow_id}] Feedback for next attempt:\n{feedback_summary}")
                    continue

                evaluation_path = (
                    config.artifact_root / config.change_id / "execution" / uow_id / f"eval_impl_{attempt}.json"
                )
                log("INFO", f"  [UoW {uow_id}] Attempt {attempt}/{config.max_implementation_attempts} — evaluator")
                evaluator_prompt = build_evaluator_prompt(
                    config=config,
                    stage_label="implementation_evaluator",
                    attempt=attempt,
                    artifact_paths=[
                        config.artifact_root / config.change_id / "execution" / uow_id / "uow_spec.yaml",
                        config.artifact_root / config.change_id / "execution" / uow_id / "impl_report.yaml",
                    ],
                    uow_id=uow_id,
                )
                evaluator_result = invoke_agent(
                    config,
                    implementation_evaluator,
                    evaluator_prompt,
                    "implementation_evaluator",
                    attempt,
                    uow_id=uow_id,
                    raise_on_error=False,
                    early_exit_paths=[evaluation_path],
                )
                if evaluator_result.exit_code != 0:
                    if evaluation_path.is_file():
                        log(
                            "WARN",
                            f"  [UoW {uow_id}] Evaluator exited {evaluator_result.exit_code} "
                            f"but artifact already exists — using it",
                        )
                    else:
                        feedback_path = None
                        feedback_summary = extract_command_failure_summary(evaluator_result)
                        log(
                            "WARN",
                            f"  [UoW {uow_id}] Evaluator command failed on attempt {attempt} "
                            f"(exit={evaluator_result.exit_code})",
                        )
                        log("INFO", f"  [UoW {uow_id}] Feedback for next attempt:\n{feedback_summary}")
                        continue
                passed, payload = read_evaluation_result(evaluation_path)
                signature = evaluation_signature(payload)
                produced_paths.append(evaluation_path)

                score = payload.get("score", "?")
                if passed:
                    log("OK", f"  [UoW {uow_id}] Implementation PASSED on attempt {attempt}  score={score}")
                    completed_uows.append(uow_id)
                    produced_paths.extend(
                        [
                            config.artifact_root / config.change_id / "execution" / uow_id / "uow_spec.yaml",
                            config.artifact_root / config.change_id / "execution" / uow_id / "impl_report.yaml",
                        ]
                    )
                    break

                log("WARN", f"  [UoW {uow_id}] Implementation FAILED on attempt {attempt}  score={score}")
                issues = payload.get("issues") or []
                for issue in issues:
                    log("WARN", f"    Issue: {issue.get('description', issue)}")

                # ── Escalation check (UoW) ────────────────────────────────
                escalation_rec = payload.get("escalation_recommendation", {})
                escalation_required = isinstance(escalation_rec, dict) and escalation_rec.get("required")
                escalated_on_disk = (_status_dir(config) / "escalated.json").exists()

                if escalation_required or escalated_on_disk:
                    if not escalated_on_disk:
                        write_escalation_artifact(
                            config, "software_engineer", "implementation_evaluator",
                            attempt, payload, uow_id=uow_id,
                        )
                    resolution = wait_for_resume(config)
                    if resolution:
                        human_resolution = resolution
                        feedback_path = evaluation_path
                        feedback_summary = extract_feedback_summary(payload)
                        log("INFO", f"  [UoW {uow_id}] Retrying with human resolution")
                        continue
                # ── End escalation check ──────────────────────────────────

                if signature == previous_signature:
                    log("ERROR", f"  [UoW {uow_id}] Similarity plateau — aborting")
                    raise WorkflowError(
                        f"Similarity plateau detected for UoW '{uow_id}' after attempt {attempt}."
                    )
                previous_signature = signature
                feedback_path = evaluation_path
                feedback_summary = extract_feedback_summary(payload)
                log("INFO", f"  [UoW {uow_id}] Feedback for next attempt:\n{feedback_summary}")
            else:
                raise WorkflowError(
                    f"UoW '{uow_id}' exceeded max attempts ({config.max_implementation_attempts})."
                )

    log("OK", f"Execution loop complete: {len(completed_uows)} UoW(s) implemented: {', '.join(completed_uows)}")
    return StageResult(
        stage_name="software_engineer",
        passed=True,
        attempts=len(completed_uows),
        artifact_paths=produced_paths,
        details={"completed_uows": completed_uows},
    )


def run_qa_loop(config: WorkflowConfig, agents: dict[str, AgentSpec]) -> StageResult:
    """Run the QA and QA evaluator stages with retries."""

    return run_stage_loop(
        config=config,
        agents=agents,
        producer_stage_key="qa",
        evaluator_stage_key="qa_evaluator",
        producer_artifacts=[
            config.artifact_root / config.change_id / "intake" / "story.yaml",
            config.artifact_root / config.change_id / "planning" / "tasks.yaml",
            config.artifact_root / config.change_id / "planning" / "assignments.json",
            config.artifact_root / config.change_id / "execution",
            config.artifact_root / config.change_id / "qa" / "qa_report.yaml",
        ],
        evaluator_artifacts=[
            config.artifact_root / config.change_id / "qa" / "qa_report.yaml",
            config.artifact_root / config.change_id / "intake" / "story.yaml",
        ],
        evaluation_path_for_attempt=lambda attempt: config.artifact_root
        / config.change_id
        / "qa"
        / f"eval_qa_{attempt}.json",
        max_attempts=config.max_qa_attempts,
    )


def run_lessons_stage(config: WorkflowConfig, agents: dict[str, AgentSpec]) -> StageResult:
    """Run the terminal lessons optimization stage."""

    log("INFO", "Running lessons optimization stage")
    lessons_optimizer = resolve_agent(agents, "lessons_optimizer")
    prompt = build_lessons_prompt(config)
    invoke_agent(config, lessons_optimizer, prompt, "lessons_optimizer", 1)
    report_path = config.artifact_root / config.change_id / "summary" / "lessons_optimizer_report.yaml"
    if not report_path.exists():
        raise WorkflowError(f"Lessons optimizer did not produce the expected artifact: {report_path}")
    log("OK", f"Lessons optimization complete  artifact={report_path}")
    return StageResult(
        stage_name="lessons_optimizer",
        passed=True,
        attempts=1,
        artifact_paths=[report_path],
    )


def _inter_stage_escalation_check(config: WorkflowConfig, label: str) -> None:
    """Safety-net check: if an agent wrote escalated.json outside the evaluator
    flow, catch it between stages."""
    resolution = wait_for_resume(config)
    if resolution:
        log("WARN", f"Inter-stage escalation detected and resolved ({label})")
        write_runner_log(config, "inter_stage_escalation_resolved", {
            "label": label,
            "resolution": resolution,
        })


def run_workflow(config: WorkflowConfig) -> list[StageResult]:
    """Execute the full custom-agent workflow and return stage results."""

    print_stage_banner(f"WORKFLOW START  change_id={config.change_id}  dry_run={config.dry_run}")
    log("INFO", f"repo_root          = {config.repo_root}")
    log("INFO", f"workflow_assets_root = {config.workflow_assets_root}")
    log("INFO", f"artifact_root      = {config.artifact_root}")
    log("INFO", f"cli_backend        = {config.cli_backend}")
    log("INFO", f"cli_bin            = {config.cli_bin}")
    log("INFO", f"model              = {config.model or '(default)'}")
    log("INFO", f"timeout_seconds    = {config.timeout_seconds}")
    log("INFO", f"reuse_existing_intake = {config.reuse_existing_intake}")
    log("INFO", f"max_task_plan_attempts      = {config.max_task_plan_attempts}")
    log("INFO", f"max_assignment_attempts     = {config.max_assignment_attempts}")
    log("INFO", f"max_implementation_attempts = {config.max_implementation_attempts}")
    log("INFO", f"max_qa_attempts             = {config.max_qa_attempts}")

    workflow_start = time.monotonic()
    ensure_artifact_dirs(config)
    agents = discover_agents(config.workflow_assets_root)
    results: list[StageResult] = []

    write_runner_log(
        config,
        "session_start",
        {
            "code_repo": str(config.repo_root),
            "workflow_assets_root": str(config.workflow_assets_root),
            "artifact_root": str(config.artifact_root),
            "cli_backend": config.cli_backend,
            "cli_bin": config.cli_bin,
            "reuse_existing_intake": config.reuse_existing_intake,
            "dry_run": config.dry_run,
        },
    )

    # ── Stage 1: intake ────────────────────────────────────────────────────
    print_stage_banner("STAGE 1/6 — intake")
    t0 = time.monotonic()
    intake_artifacts = intake_artifact_paths(config.artifact_root, config.change_id)
    if config.reuse_existing_intake:
        missing_intake_artifacts = [path for path in intake_artifacts if not path.exists()]
        if missing_intake_artifacts:
            missing_display = ", ".join(str(path) for path in missing_intake_artifacts)
            raise WorkflowError(f"Requested intake reuse, but these artifacts are missing: {missing_display}")
        write_runner_log(
            config,
            "intake_reused",
            {"artifacts": [str(path) for path in intake_artifacts]},
        )
        intake_result = StageResult(
            stage_name="intake",
            passed=True,
            attempts=0,
            artifact_paths=intake_artifacts,
            details={"reused": True},
        )
        results.append(intake_result)
        log("OK", f"Stage 'intake' reused existing artifacts  elapsed={time.monotonic() - t0:.1f}s")
    else:
        intake_agent = resolve_agent(agents, "intake")
        invoke_agent(config, intake_agent, build_intake_prompt(config), "intake", 1)
        missing_intake_artifacts = [path for path in intake_artifacts if not path.exists()]
        if missing_intake_artifacts:
            missing_display = ", ".join(str(path) for path in missing_intake_artifacts)
            raise WorkflowError(f"Intake stage did not produce the expected artifacts: {missing_display}")
        intake_result = StageResult(
            stage_name="intake",
            passed=True,
            attempts=1,
            artifact_paths=intake_artifacts,
        )
        results.append(intake_result)
        log("OK", f"Stage 'intake' complete  elapsed={time.monotonic() - t0:.1f}s")

    # ── Stage 2+3: task-generator + task-plan-evaluator loop ───────────────
    print_stage_banner("STAGE 2+3/6 — task-generator ↔ task-plan-evaluator")
    t0 = time.monotonic()
    task_plan_result = run_stage_loop(
        config=config,
        agents=agents,
        producer_stage_key="task_generator",
        evaluator_stage_key="task_plan_evaluator",
        producer_artifacts=[
            config.artifact_root / config.change_id / "intake" / "story.yaml",
            config.artifact_root / config.change_id / "intake" / "constraints.md",
        ],
        evaluator_artifacts=[
            config.artifact_root / config.change_id / "planning" / "tasks.yaml",
            config.artifact_root / config.change_id / "intake" / "story.yaml",
        ],
        evaluation_path_for_attempt=lambda attempt: config.artifact_root
        / config.change_id
        / "planning"
        / f"eval_tasks_{attempt}.json",
        max_attempts=config.max_task_plan_attempts,
    )
    results.append(task_plan_result)
    log("OK", f"Stage 'task_generator' complete  attempts={task_plan_result.attempts}  elapsed={time.monotonic() - t0:.1f}s")

    # Safety net: check for inter-stage escalation
    _inter_stage_escalation_check(config, "after task_generator")

    # ── Stage 4+5: task-assigner + assignment-evaluator loop ───────────────
    print_stage_banner("STAGE 4+5/6 — task-assigner ↔ assignment-evaluator")
    t0 = time.monotonic()
    assignment_result = run_stage_loop(
        config=config,
        agents=agents,
        producer_stage_key="task_assigner",
        evaluator_stage_key="assignment_evaluator",
        producer_artifacts=[
            config.artifact_root / config.change_id / "planning" / "tasks.yaml",
            config.artifact_root / config.change_id / "intake" / "story.yaml",
            config.artifact_root / config.change_id / "intake" / "constraints.md",
        ],
        evaluator_artifacts=[
            config.artifact_root / config.change_id / "planning" / "assignments.json",
            config.artifact_root / config.change_id / "planning" / "tasks.yaml",
        ],
        evaluation_path_for_attempt=lambda attempt: config.artifact_root
        / config.change_id
        / "planning"
        / f"eval_assignments_{attempt}.json",
        max_attempts=config.max_assignment_attempts,
    )
    results.append(assignment_result)
    log("OK", f"Stage 'task_assigner' complete  attempts={assignment_result.attempts}  elapsed={time.monotonic() - t0:.1f}s")

    # Safety net: check for inter-stage escalation
    _inter_stage_escalation_check(config, "after task_assigner")

    # ── Stage 6: software-engineer + implementation-evaluator loop ─────────
    print_stage_banner("STAGE 6/6 — software-engineer ↔ implementation-evaluator")
    t0 = time.monotonic()
    execution_result = run_execution_loop(config, agents)
    results.append(execution_result)
    log("OK", f"Stage 'software_engineer' complete  uows={execution_result.attempts}  elapsed={time.monotonic() - t0:.1f}s")

    # Safety net: check for inter-stage escalation
    _inter_stage_escalation_check(config, "after software_engineer")

    # ── QA + QA evaluator loop ─────────────────────────────────────────────
    print_stage_banner("QA — qa-engineer ↔ qa-evaluator")
    t0 = time.monotonic()
    qa_result = run_qa_loop(config, agents)
    results.append(qa_result)
    log("OK", f"Stage 'qa' complete  attempts={qa_result.attempts}  elapsed={time.monotonic() - t0:.1f}s")

    # Safety net: check for inter-stage escalation
    _inter_stage_escalation_check(config, "after qa")

    # ── Lessons optimizer ──────────────────────────────────────────────────
    print_stage_banner("LESSONS OPTIMIZER")
    t0 = time.monotonic()
    lessons_result = run_lessons_stage(config, agents)
    results.append(lessons_result)
    log("OK", f"Stage 'lessons_optimizer' complete  elapsed={time.monotonic() - t0:.1f}s")

    total_elapsed = time.monotonic() - workflow_start
    print_stage_banner(f"WORKFLOW COMPLETE  total_elapsed={total_elapsed:.1f}s  stages={len(results)}")

    write_runner_log(
        config,
        "session_end",
        {
            "stages": [
                {
                    "stage_name": result.stage_name,
                    "passed": result.passed,
                    "attempts": result.attempts,
                    "artifacts": [str(path) for path in result.artifact_paths],
                }
                for result in results
            ]
        },
    )
    return results


def format_summary(results: list[StageResult]) -> dict[str, Any]:
    """Convert stage results into a serializable summary."""

    return {
        "status": "pass" if all(result.passed for result in results) else "fail",
        "stages": [
            {
                "stage_name": result.stage_name,
                "passed": result.passed,
                "attempts": result.attempts,
                "artifacts": [str(path) for path in result.artifact_paths],
                "details": result.details,
            }
            for result in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    """Interactive CLI entry point."""

    provided_args = list(sys.argv[1:] if argv is None else argv)
    if provided_args:
        print("agent_workflow_runner.py is now interactive-only. Run it without arguments.", file=sys.stderr)
        return 2

    try:
        config = collect_interactive_config()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        log("WARN", "Startup cancelled by user.")
        return 130
    except EOFError:
        print(file=sys.stderr)
        log("ERROR", "Startup aborted before all required input was provided.")
        return 1
    except WorkflowError as exc:
        log("ERROR", f"Startup failed: {exc}")
        return 1

    try:
        results = run_workflow(config)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        log("WARN", "Workflow cancelled by user.")
        return 130
    except WorkflowError as exc:
        log("ERROR", f"Workflow failed: {exc}")
        return 1

    summary = format_summary(results)
    print()
    for stage in summary["stages"]:
        status = "✓ PASS" if stage["passed"] else "✗ FAIL"
        log("OK" if stage["passed"] else "ERROR", f"{status}  {stage['stage_name']}  (attempts={stage['attempts']})")
    print()
    log("OK" if summary["status"] == "pass" else "ERROR", f"Workflow status: {summary['status'].upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
