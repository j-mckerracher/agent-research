#!/usr/bin/env python3
"""Run the local `.claude` Copilot agents in the documented workflow order.

This module turns the rough scratch pseudocode into a deterministic workflow runner
for the custom agents stored in `.claude/agents`.

The runner supports two modes:

- real mode: invokes `copilot --agent <agent-id>` for each stage
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

import argparse
import hashlib
import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

WORKFLOW_ASSETS_ROOT = Path(__file__).resolve().parents[1]


class WorkflowError(RuntimeError):
    """Raised when the workflow cannot continue safely."""


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
    copilot_bin: str = "copilot"
    model: str | None = None
    timeout_seconds: int = 900
    dry_run: bool = False
    continue_on_failure: bool = False
    max_task_plan_attempts: int = 3
    max_assignment_attempts: int = 2
    max_implementation_attempts: int = 3
    max_qa_attempts: int = 2
    additional_dirs: list[Path] = field(default_factory=list)


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


def iso_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_timestamp() -> str:
    """Return a filesystem-safe UTC timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


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


def run_command(command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
    """Run a subprocess and capture stdout/stderr."""

    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
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


def call_helper_script(config: WorkflowConfig, script_name: str, *args: str) -> CommandResult:
    """Run one of the repository's helper scripts."""

    script_path = config.workflow_assets_root / "scripts" / script_name
    if not script_path.is_file():
        raise WorkflowError(f"Helper script not found: {script_path}")

    command = [sys.executable, str(script_path), *args]
    result = run_command(command, cwd=config.repo_root, timeout_seconds=config.timeout_seconds)
    if result.exit_code != 0:
        raise WorkflowError(
            f"Helper script failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
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


def build_copilot_command(config: WorkflowConfig, agent: AgentSpec, prompt: str) -> list[str]:
    """Build the `copilot` CLI command for a single agent invocation."""

    command = [
        config.copilot_bin,
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
    ]
    seen_roots: set[Path] = set()
    for root in [config.repo_root, config.workflow_assets_root, config.artifact_root, *config.additional_dirs]:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        command.extend(["--add-dir", str(root)])
    if config.model:
        command.extend(["--model", config.model])
    return command


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
) -> str:
    """Build a prompt for a non-evaluator stage agent."""

    artifact_lines = "\n".join(f"- {path}" for path in artifact_paths)
    feedback_line = "This is the first attempt for this stage."
    if feedback_path is not None:
        feedback_line = f"Previous evaluator feedback is available at `{feedback_path}`. Address it fully."
        if feedback_summary:
            feedback_line += f"\n\nEvaluator fixes to apply:\n{feedback_summary}"
    uow_line = f"Target only `{uow_id}`. Stay within that UoW's scope." if uow_id else ""

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

        {feedback_line}

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
                "execution_schedule": [
                    {
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
) -> CommandResult:
    """Invoke an agent or synthesize its outputs in dry-run mode."""

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
        dry_run_artifacts = materialize_dry_run_artifacts(config, stage_key, attempt, uow_id=uow_id)
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
            command=[config.copilot_bin, "--dry-run", agent.key],
            exit_code=0,
            stdout=f"dry-run completed for {agent.key}",
            stderr="",
        )

    command = build_copilot_command(config, agent, prompt)
    result = run_command(command, cwd=config.repo_root, timeout_seconds=config.timeout_seconds)
    write_runner_log(
        config,
        "agent_result",
        {
            "stage_key": stage_key,
            "attempt": attempt,
            "uow_id": uow_id,
            "agent": agent.key,
            "exit_code": result.exit_code,
            "stdout_preview": strip_agent_bullet_prefix(result.stdout)[:1000],
            "stderr_preview": result.stderr[:1000],
            "command": command,
        },
    )

    if result.exit_code != 0:
        raise WorkflowError(
            f"Agent '{agent.key}' failed for stage '{stage_key}' attempt {attempt}.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    return result


def read_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON document with a clear error message on failure."""

    if not path.is_file():
        raise WorkflowError(f"Expected JSON artifact does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Failed to parse JSON artifact {path}: {exc}") from exc


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


def load_execution_schedule(assignments_path: Path) -> list[dict[str, Any]]:
    """Load the execution schedule batches from `assignments.json`."""

    payload = read_json_file(assignments_path)
    schedule = payload.get("execution_schedule")
    if not isinstance(schedule, list):
        raise WorkflowError(f"Invalid assignments.json: expected execution_schedule list in {assignments_path}")
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

    for attempt in range(1, max_attempts + 1):
        producer_prompt = build_producer_prompt(
            config=config,
            stage_label=producer_stage_key,
            attempt=attempt,
            artifact_paths=producer_artifacts,
            feedback_path=feedback_path,
            feedback_summary=feedback_summary,
        )
        invoke_agent(config, producer, producer_prompt, producer_stage_key, attempt)

        evaluator_prompt = build_evaluator_prompt(
            config=config,
            stage_label=evaluator_stage_key,
            attempt=attempt,
            artifact_paths=evaluator_artifacts,
        )
        invoke_agent(config, evaluator, evaluator_prompt, evaluator_stage_key, attempt)

        evaluation_path = evaluation_path_for_attempt(attempt)
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

        if passed:
            return StageResult(
                stage_name=producer_stage_key,
                passed=True,
                attempts=attempt,
                artifact_paths=[*producer_artifacts, evaluation_path],
                details={"evaluation": payload},
            )

        if signature == previous_signature:
            raise WorkflowError(
                f"Similarity plateau detected in stage '{producer_stage_key}' after attempt {attempt}."
            )
        previous_signature = signature
        feedback_path = evaluation_path
        feedback_summary = extract_feedback_summary(payload)

    raise WorkflowError(
        f"Stage '{producer_stage_key}' exceeded max attempts ({max_attempts}) without passing evaluation."
    )


def run_execution_loop(config: WorkflowConfig, agents: dict[str, AgentSpec]) -> StageResult:
    """Execute each scheduled UoW with an implementation evaluator loop."""

    assignments_path = config.artifact_root / config.change_id / "planning" / "assignments.json"
    schedule = load_execution_schedule(assignments_path)
    software_engineer = resolve_agent(agents, "software_engineer")
    implementation_evaluator = resolve_agent(agents, "implementation_evaluator")

    completed_uows: list[str] = []
    produced_paths: list[Path] = [assignments_path]

    for batch in schedule:
        for uow in batch.get("uows", []):
            uow_id = uow.get("uow_id")
            if not isinstance(uow_id, str) or not uow_id:
                raise WorkflowError(f"Invalid UoW entry in assignments.json: {uow}")

            feedback_path: Path | None = None
            previous_signature: str | None = None
            feedback_summary: str | None = None
            for attempt in range(1, config.max_implementation_attempts + 1):
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
                )
                invoke_agent(
                    config,
                    software_engineer,
                    producer_prompt,
                    "software_engineer",
                    attempt,
                    uow_id=uow_id,
                )

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
                invoke_agent(
                    config,
                    implementation_evaluator,
                    evaluator_prompt,
                    "implementation_evaluator",
                    attempt,
                    uow_id=uow_id,
                )

                evaluation_path = (
                    config.artifact_root / config.change_id / "execution" / uow_id / f"eval_impl_{attempt}.json"
                )
                passed, payload = read_evaluation_result(evaluation_path)
                signature = evaluation_signature(payload)
                produced_paths.append(evaluation_path)

                if passed:
                    completed_uows.append(uow_id)
                    produced_paths.extend(
                        [
                            config.artifact_root / config.change_id / "execution" / uow_id / "uow_spec.yaml",
                            config.artifact_root / config.change_id / "execution" / uow_id / "impl_report.yaml",
                        ]
                    )
                    break

                if signature == previous_signature:
                    raise WorkflowError(
                        f"Similarity plateau detected for UoW '{uow_id}' after attempt {attempt}."
                    )
                previous_signature = signature
                feedback_path = evaluation_path
                feedback_summary = extract_feedback_summary(payload)
            else:
                raise WorkflowError(
                    f"UoW '{uow_id}' exceeded max attempts ({config.max_implementation_attempts})."
                )

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

    lessons_optimizer = resolve_agent(agents, "lessons_optimizer")
    prompt = build_lessons_prompt(config)
    invoke_agent(config, lessons_optimizer, prompt, "lessons_optimizer", 1)
    report_path = config.artifact_root / config.change_id / "summary" / "lessons_optimizer_report.yaml"
    if not report_path.exists():
        raise WorkflowError(f"Lessons optimizer did not produce the expected artifact: {report_path}")
    return StageResult(
        stage_name="lessons_optimizer",
        passed=True,
        attempts=1,
        artifact_paths=[report_path],
    )


def run_workflow(config: WorkflowConfig) -> list[StageResult]:
    """Execute the full custom-agent workflow and return stage results."""

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
            "dry_run": config.dry_run,
        },
    )

    intake_agent = resolve_agent(agents, "intake")
    invoke_agent(config, intake_agent, build_intake_prompt(config), "intake", 1)
    intake_artifacts = [
        config.artifact_root / config.change_id / "intake" / "story.yaml",
        config.artifact_root / config.change_id / "intake" / "config.yaml",
        config.artifact_root / config.change_id / "intake" / "constraints.md",
    ]
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

    execution_result = run_execution_loop(config, agents)
    results.append(execution_result)

    qa_result = run_qa_loop(config, agents)
    results.append(qa_result)

    lessons_result = run_lessons_stage(config, agents)
    results.append(lessons_result)

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Code repository root that agents may read and modify",
    )
    parser.add_argument("--artifact-root", type=Path, help="Artifact root directory. Defaults to <repo-root>/agent-context")
    parser.add_argument("--change-id", required=True, help="Workflow change identifier, for example WI-12345")
    context_group = parser.add_mutually_exclusive_group(required=True)
    context_group.add_argument("--context", help="Inline workflow context passed to the intake stage")
    context_group.add_argument("--context-file", type=Path, help="File containing workflow context passed to the intake stage")
    parser.add_argument("--copilot-bin", default="copilot", help="Copilot CLI executable to use")
    parser.add_argument("--model", help="Optional model override passed to the Copilot CLI")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Per-agent timeout")
    parser.add_argument("--dry-run", action="store_true", help="Do not invoke real models; synthesize stage artifacts instead")
    parser.add_argument("--continue-on-failure", action="store_true", help="Return a non-zero summary instead of raising immediately")
    parser.add_argument("--max-task-plan-attempts", type=int, default=3)
    parser.add_argument("--max-assignment-attempts", type=int, default=2)
    parser.add_argument("--max-implementation-attempts", type=int, default=3)
    parser.add_argument("--max-qa-attempts", type=int, default=2)
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        type=Path,
        help="Additional directory to grant to the Copilot CLI (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="Print the final summary as JSON")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> WorkflowConfig:
    """Create a validated workflow config from parsed CLI args."""

    repo_root = args.repo_root.resolve()
    artifact_root = args.artifact_root.resolve() if args.artifact_root else repo_root / "agent-context"
    if args.context_file:
        context = args.context_file.read_text(encoding="utf-8")
    else:
        context = args.context

    return WorkflowConfig(
        repo_root=repo_root,
        workflow_assets_root=WORKFLOW_ASSETS_ROOT,
        change_id=args.change_id,
        context=context,
        artifact_root=artifact_root,
        copilot_bin=args.copilot_bin,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
        continue_on_failure=args.continue_on_failure,
        max_task_plan_attempts=args.max_task_plan_attempts,
        max_assignment_attempts=args.max_assignment_attempts,
        max_implementation_attempts=args.max_implementation_attempts,
        max_qa_attempts=args.max_qa_attempts,
        additional_dirs=[path.resolve() for path in args.add_dir],
    )


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
    """CLI entry point."""

    args = parse_args(argv)
    config = build_config(args)

    try:
        results = run_workflow(config)
        summary = format_summary(results)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            for stage in summary["stages"]:
                print(f"[PASS] {stage['stage_name']} (attempts={stage['attempts']})")
            print(f"Workflow status: {summary['status']}")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should present the full failure reason.
        failure_summary = {
            "status": "fail",
            "error": str(exc),
            "change_id": config.change_id,
        }
        if args.json:
            print(json.dumps(failure_summary, indent=2), file=sys.stderr)
        else:
            print(f"Workflow failed: {exc}", file=sys.stderr)

        if config.continue_on_failure:
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
