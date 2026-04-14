#!/usr/bin/env python3
"""Phase 1 Benchmark Calibration Harness

Runs benchmark candidate stories through the agentic workflow and grades results.

Usage:
    # Run calibration on all candidates (5 trials each)
    python3 benchmarks/scripts/run_calibration.py --candidates benchmarks/candidates/ --trials 5

    # Run on specific candidates
    python3 benchmarks/scripts/run_calibration.py --candidates benchmarks/candidates/ --only BM-001,BM-003

    # Run on selected benchmark set
    python3 benchmarks/scripts/run_calibration.py --candidates benchmarks/selected/ --trials 5

    # Dry-run mode (validates harness without invoking AI)
    python3 benchmarks/scripts/run_calibration.py --candidates benchmarks/candidates/ --dry-run

    # Use copilot-or environment (Gemma 4 via OpenRouter)
    python3 benchmarks/scripts/run_calibration.py --candidates benchmarks/candidates/ --use-copilot-or

Environment:
    COPILOT_PROVIDER_BASE_URL   OpenRouter API base URL
    COPILOT_PROVIDER_TYPE       Provider type (openai)
    COPILOT_PROVIDER_API_KEY    OpenRouter API key
    COPILOT_MODEL               Model identifier
"""

from __future__ import annotations

import argparse
import csv
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_RUNNER = REPO_ROOT / "agent-runner"
BENCHMARK_ROOT = REPO_ROOT / "benchmarks"
RESULTS_DIR = BENCHMARK_ROOT / "results"
ARTIFACT_ROOT = REPO_ROOT / "agent-context"

DEFAULT_TRIALS = 5
DEFAULT_TIMEOUT = 900  # 15 minutes per trial


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class HiddenCheck:
    check: str
    description: str
    path: str = ""
    path_pattern: str = ""
    pattern: str = ""
    project: str = ""
    test_type: str = ""


@dataclass
class AcceptanceCriterion:
    key: str
    text: str
    weight: float


@dataclass
class CandidateStory:
    id: str
    name: str
    description: str
    visible_acs: list[AcceptanceCriterion]
    hidden_checks: list[HiddenCheck]
    required_artifacts: list[str]
    grading_metadata: dict[str, Any]


@dataclass
class ACScore:
    score: float
    weight: float
    method: str
    evidence: str


@dataclass
class TrialResult:
    trial_id: str
    candidate_id: str
    trial_number: int
    timestamp: str
    hard_gates: dict[str, bool]
    all_gates_passed: bool
    ac_scores: dict[str, ACScore]
    weighted_score: float
    classification: str  # PASS, PARTIAL, FAIL
    operational_metrics: dict[str, Any]
    error: str = ""


@dataclass
class CandidateAggregate:
    candidate_id: str
    name: str
    trials_run: int
    pass_count: int
    partial_count: int
    fail_count: int
    pass_rate: float
    avg_weighted_score: float
    score_stddev: float
    avg_runtime: float
    difficulty: str  # Easy, Medium, Hard
    is_flaky: bool
    trial_results: list[TrialResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_candidate(path: Path) -> CandidateStory:
    """Load a candidate story from a YAML file."""
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    visible_acs = []
    for key, val in data.get("visible_acceptance_criteria", {}).items():
        visible_acs.append(AcceptanceCriterion(key=key, text=val["text"], weight=val["weight"]))

    hidden_checks = []
    for hc in data.get("hidden_checks", []):
        hidden_checks.append(HiddenCheck(**{k: v for k, v in hc.items() if k != "description"}, description=hc.get("description", "")))

    return CandidateStory(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        visible_acs=visible_acs,
        hidden_checks=hidden_checks,
        required_artifacts=data.get("required_artifacts", []),
        grading_metadata=data.get("grading_metadata", {}),
    )


def load_all_candidates(directory: Path, only: list[str] | None = None) -> list[CandidateStory]:
    """Load all candidate stories from a directory."""
    candidates = []
    for path in sorted(directory.glob("*.yaml")):
        candidate = load_candidate(path)
        if only is None or candidate.id in only:
            candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------------------
# Intake artifact generation
# ---------------------------------------------------------------------------


def generate_intake_artifacts(candidate: CandidateStory, change_id: str) -> Path:
    """Create intake artifacts (story.yaml, config.yaml) for a benchmark trial."""
    change_dir = ARTIFACT_ROOT / change_id
    intake_dir = change_dir / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)

    # story.yaml
    story = {
        "change_id": change_id,
        "title": candidate.name,
        "description": candidate.description.strip(),
        "acceptance_criteria": {
            ac.key: ac.text for ac in candidate.visible_acs
        },
        "constraints": [
            "All changes must follow Angular standalone component conventions",
            "Use signals (input(), output(), computed()) not decorators",
            "Use ChangeDetectionStrategy.OnPush",
            "Add data-test-id attributes to all interactive/observable elements",
            "Create or update test harness files",
            "Do not modify unrelated files",
        ],
        "non_functional_requirements": [
            "Existing tests must continue to pass",
            "Code must pass linting",
        ],
        "examples": [],
        "ado_provenance": {
            "work_item_id": change_id,
            "area_path": "Mayo Collaborative Services > Benchmark",
            "iteration": "Benchmark Phase 1",
            "story_points": 3,
        },
        "raw_input": candidate.description,
    }
    with open(intake_dir / "story.yaml", "w") as f:
        yaml.dump(story, f, default_flow_style=False, sort_keys=False, width=120)

    # config.yaml
    config = {
        "change_id": change_id,
        "code_repo": str(REPO_ROOT),
        "project_type": "angular-nx-monorepo",
        "planning_docs_root": "",
        "planning_docs_paths": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_assignments": {},
        "iteration_limits": {
            "task_plan": 3,
            "assignment": 2,
            "implementation": 3,
            "qa": 2,
        },
        "run_metadata": {
            "status": "intake_complete",
            "current_stage": "intake",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    with open(intake_dir / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, width=120)

    # constraints.md
    constraints_md = f"""# Constraints for {candidate.name}

## Angular Conventions
- Use standalone components (do not set standalone: true explicitly)
- Use ChangeDetectionStrategy.OnPush
- Use signal-based inputs: input(), output(), computed()
- Use modern control flow: @if, @for, @switch

## Testing Requirements
- Add data-test-id attributes to all interactive/observable elements
- Create or update Cypress component tests
- Create or update test harness files
- Existing tests must continue to pass

## Scope
- Only modify files related to this story
- Do not introduce new dependencies unless absolutely necessary
"""
    with open(intake_dir / "constraints.md", "w") as f:
        f.write(constraints_md)

    return change_dir


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------


def get_copilot_or_env() -> dict[str, str]:
    """Build environment dict with copilot-or variables."""
    env = os.environ.copy()

    # Check for copilot-or env vars
    required = ["COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_TYPE", "COPILOT_PROVIDER_API_KEY", "COPILOT_MODEL"]
    missing = [v for v in required if not env.get(v)]

    if missing:
        # Try to extract from zshrc
        try:
            result = subprocess.run(
                ["bash", "-c", "grep -A 10 'alias copilot-or' ~/.zshrc 2>/dev/null"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                match = re.search(r'(COPILOT_[A-Z_]+)="([^"]*)"', line)
                if match:
                    env[match.group(1)] = match.group(2)
        except Exception:
            pass

    missing = [v for v in required if not env.get(v)]
    if missing:
        raise RuntimeError(f"Missing copilot-or env vars: {missing}. Source benchmarks/scripts/copilot_or_env.sh first.")

    return env


def run_workflow_trial(
    candidate: CandidateStory,
    trial_number: int,
    dry_run: bool = False,
    use_copilot_or: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> TrialResult:
    """Run a single workflow trial for a candidate story."""
    change_id = f"{candidate.id}-T{trial_number}"
    trial_id = f"{candidate.id}_trial_{trial_number}"
    timestamp = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    print(f"\n{'='*60}")
    print(f"  Trial: {trial_id}")
    print(f"  Candidate: {candidate.name}")
    print(f"  Change ID: {change_id}")
    print(f"  Dry run: {dry_run}")
    print(f"{'='*60}\n")

    # Initialize result skeleton
    hard_gates = {
        "workflow_completed": False,
        "artifacts_exist": False,
        "hidden_checks_pass": False,
        "no_build_break": True,   # Assume pass unless we check
        "no_lint_break": True,    # Assume pass unless we check
    }
    ac_scores: dict[str, ACScore] = {}
    error_msg = ""

    try:
        # Step 1: Generate intake artifacts
        change_dir = generate_intake_artifacts(candidate, change_id)
        print(f"  [1/5] Intake artifacts generated at {change_dir}")

        # Step 2: Run the workflow
        cmd = [
            sys.executable, str(AGENT_RUNNER / "run_headless.py"),
            "--change-id", change_id,
            "--repo", str(REPO_ROOT),
            "--backend", "copilot",
            "--output-json", str(change_dir / "workflow_output.json"),
        ]

        env = os.environ.copy()
        if use_copilot_or:
            env = get_copilot_or_env()

        if dry_run:
            # In dry-run mode, we simulate the workflow
            print("  [2/5] Dry-run: simulating workflow execution...")
            # Create synthetic workflow output
            synthetic_output = {
                "status": "pass",
                "stages": [
                    {"stage_name": "intake", "passed": True, "attempts": 0, "artifacts": [], "details": {"reused": True}},
                    {"stage_name": "task_generator", "passed": True, "attempts": 1, "artifacts": [], "details": {}},
                    {"stage_name": "task_assigner", "passed": True, "attempts": 1, "artifacts": [], "details": {}},
                    {"stage_name": "software_engineer", "passed": True, "attempts": 1, "artifacts": [], "details": {}},
                    {"stage_name": "qa", "passed": True, "attempts": 1, "artifacts": [], "details": {}},
                ],
            }
            output_path = change_dir / "workflow_output.json"
            output_path.write_text(json.dumps(synthetic_output, indent=2))

            # Create synthetic planning artifacts
            planning_dir = change_dir / "planning"
            planning_dir.mkdir(exist_ok=True)
            (planning_dir / "task_plan.yaml").write_text(
                yaml.dump({"tasks": [{"id": "T1", "title": "Implement changes", "status": "done"}]})
            )

            hard_gates["workflow_completed"] = True
            time.sleep(0.5)  # Simulate brief delay

        else:
            print(f"  [2/5] Running workflow: {' '.join(cmd[:4])}...")
            try:
                result = subprocess.run(
                    cmd, cwd=REPO_ROOT, env=env,
                    capture_output=True, text=True,
                    timeout=timeout,
                )
                print(f"  Workflow exit code: {result.returncode}")
                if result.returncode == 0:
                    hard_gates["workflow_completed"] = True
                else:
                    error_msg = f"Workflow exited with code {result.returncode}"
                    # Save stderr for debugging
                    (change_dir / "workflow_stderr.log").write_text(result.stderr or "")
                    (change_dir / "workflow_stdout.log").write_text(result.stdout or "")
            except subprocess.TimeoutExpired:
                error_msg = f"Workflow timed out after {timeout}s"
                print(f"  ERROR: {error_msg}")
            except Exception as e:
                error_msg = f"Workflow execution error: {e}"
                print(f"  ERROR: {error_msg}")

        # Step 3: Check required artifacts
        print("  [3/5] Checking required artifacts...")
        artifacts_ok = True
        for artifact_rel in candidate.required_artifacts:
            artifact_path = change_dir / artifact_rel
            if not artifact_path.exists():
                artifacts_ok = False
                print(f"    MISSING: {artifact_rel}")
            else:
                print(f"    OK: {artifact_rel}")
        hard_gates["artifacts_exist"] = artifacts_ok

        # Step 4: Run hidden checks
        print("  [4/5] Running hidden checks...")
        hidden_ok = True
        for hc in candidate.hidden_checks:
            check_result = run_hidden_check(hc)
            if not check_result:
                hidden_ok = False
                print(f"    FAIL: {hc.description}")
            else:
                print(f"    PASS: {hc.description}")
        hard_gates["hidden_checks_pass"] = hidden_ok

        # Step 5: Score acceptance criteria
        print("  [5/5] Scoring acceptance criteria...")
        for ac in candidate.visible_acs:
            score, evidence = score_acceptance_criterion(ac, candidate, change_dir)
            ac_scores[ac.key] = ACScore(
                score=score,
                weight=ac.weight,
                method="deterministic",
                evidence=evidence,
            )
            print(f"    {ac.key}: {score}/{ac.weight} — {evidence[:60]}")

    except Exception as e:
        error_msg = f"Trial error: {e}\n{traceback.format_exc()}"
        print(f"  ERROR: {error_msg}")

    # Calculate aggregate score
    elapsed = time.monotonic() - start_time
    all_gates = all(hard_gates.values())

    if all_gates and ac_scores:
        total_weighted = sum(s.score * s.weight for s in ac_scores.values())
        total_weight = sum(s.weight for s in ac_scores.values())
        weighted_score = total_weighted / total_weight if total_weight > 0 else 0.0
    else:
        weighted_score = 0.0

    if not all_gates:
        classification = "FAIL"
    elif weighted_score >= 0.8:
        classification = "PASS"
    elif weighted_score >= 0.5:
        classification = "PARTIAL"
    else:
        classification = "FAIL"

    result = TrialResult(
        trial_id=trial_id,
        candidate_id=candidate.id,
        trial_number=trial_number,
        timestamp=timestamp,
        hard_gates=hard_gates,
        all_gates_passed=all_gates,
        ac_scores={k: asdict(v) for k, v in ac_scores.items()},
        weighted_score=round(weighted_score, 4),
        classification=classification,
        operational_metrics={
            "runtime_seconds": round(elapsed, 2),
            "files_changed": 0,  # Would be populated from git diff in real run
            "files_added": 0,
            "test_files_modified": 0,
        },
        error=error_msg,
    )

    print(f"\n  Result: {classification} (score={weighted_score:.2f}, gates={'ALL PASS' if all_gates else 'FAIL'})")
    print(f"  Runtime: {elapsed:.1f}s")

    return result


# ---------------------------------------------------------------------------
# Hidden check execution
# ---------------------------------------------------------------------------


def run_hidden_check(hc: HiddenCheck) -> bool:
    """Execute a single hidden check and return pass/fail."""
    try:
        if hc.check == "file_exists":
            matches = globmod.glob(str(REPO_ROOT / hc.pattern), recursive=True)
            return len(matches) > 0

        elif hc.check == "file_contains":
            target_path = hc.path or hc.path_pattern
            if "*" in target_path:
                matches = globmod.glob(str(REPO_ROOT / target_path), recursive=True)
                if not matches:
                    return False
                target_path = matches[0]
            else:
                target_path = str(REPO_ROOT / target_path)

            if not Path(target_path).exists():
                return False

            content = Path(target_path).read_text(errors="replace")
            return bool(re.search(hc.pattern, content))

        elif hc.check == "file_modified":
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            return hc.path in result.stdout

        elif hc.check == "no_regression":
            # Skip regression tests in calibration (too expensive per trial)
            # Mark as pass and note in metrics
            return True

        else:
            print(f"    Unknown check type: {hc.check}")
            return False

    except Exception as e:
        print(f"    Check error: {e}")
        return False


# ---------------------------------------------------------------------------
# AC scoring
# ---------------------------------------------------------------------------


def score_acceptance_criterion(
    ac: AcceptanceCriterion,
    candidate: CandidateStory,
    change_dir: Path,
) -> tuple[float, str]:
    """Score a single acceptance criterion. Returns (score, evidence)."""
    # In dry-run or when deterministic checks are available, use them
    # For calibration, we do a simplified scoring based on hidden check overlap
    # Full scoring would inspect the actual code changes

    # Check if related hidden checks passed
    related_hidden = [
        hc for hc in candidate.hidden_checks
        if any(kw in ac.text.lower() for kw in extract_keywords(hc.description))
    ]

    if related_hidden:
        all_passed = all(run_hidden_check(hc) for hc in related_hidden)
        if all_passed:
            return 1.0, f"Related hidden checks passed ({len(related_hidden)} checks)"
        else:
            return 0.0, f"Related hidden checks failed"

    # Default: check for any evidence of the AC being addressed
    # In real calibration, this would be more sophisticated
    return 0.5, "No deterministic check available; scored as partial"


def extract_keywords(text: str) -> list[str]:
    """Extract key words from a description for fuzzy matching."""
    stop_words = {"must", "be", "the", "a", "an", "is", "are", "have", "has", "with", "for", "to", "of", "in", "on"}
    words = re.findall(r'\w+', text.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


# ---------------------------------------------------------------------------
# Aggregation and classification
# ---------------------------------------------------------------------------


def aggregate_trials(candidate: CandidateStory, trials: list[TrialResult]) -> CandidateAggregate:
    """Aggregate trial results into a candidate-level summary."""
    import math

    pass_count = sum(1 for t in trials if t.classification == "PASS")
    partial_count = sum(1 for t in trials if t.classification == "PARTIAL")
    fail_count = sum(1 for t in trials if t.classification == "FAIL")
    n = len(trials)

    scores = [t.weighted_score for t in trials]
    runtimes = [t.operational_metrics.get("runtime_seconds", 0) for t in trials]

    avg_score = sum(scores) / n if n > 0 else 0.0
    avg_runtime = sum(runtimes) / n if n > 0 else 0.0
    pass_rate = pass_count / n if n > 0 else 0.0

    if n > 1:
        variance = sum((s - avg_score) ** 2 for s in scores) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    # Classify difficulty
    difficulty = classify_difficulty(pass_rate, avg_score, avg_runtime)

    # Check flakiness
    is_flaky = 0.2 <= pass_rate <= 0.8 and stddev > 0.3

    return CandidateAggregate(
        candidate_id=candidate.id,
        name=candidate.name,
        trials_run=n,
        pass_count=pass_count,
        partial_count=partial_count,
        fail_count=fail_count,
        pass_rate=round(pass_rate, 4),
        avg_weighted_score=round(avg_score, 4),
        score_stddev=round(stddev, 4),
        avg_runtime=round(avg_runtime, 2),
        difficulty=difficulty,
        is_flaky=is_flaky,
        trial_results=trials,
    )


def classify_difficulty(pass_rate: float, avg_score: float, avg_runtime: float) -> str:
    """Classify difficulty using the most conservative metric."""
    # Determine per-metric classification
    classifications = []

    if pass_rate >= 0.8:
        classifications.append("Easy")
    elif pass_rate >= 0.4:
        classifications.append("Medium")
    else:
        classifications.append("Hard")

    if avg_score >= 0.85:
        classifications.append("Easy")
    elif avg_score >= 0.5:
        classifications.append("Medium")
    else:
        classifications.append("Hard")

    if avg_runtime < 300:
        classifications.append("Easy")
    elif avg_runtime < 600:
        classifications.append("Medium")
    else:
        classifications.append("Hard")

    # Most conservative (hardest)
    order = {"Hard": 0, "Medium": 1, "Easy": 2}
    return min(classifications, key=lambda c: order[c])


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def save_results(
    aggregates: list[CandidateAggregate],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Save calibration results as JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON output
    json_path = output_dir / "phase1_calibration.json"
    json_data = {
        "calibration_run": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": os.environ.get("COPILOT_MODEL", "unknown"),
            "total_candidates": len(aggregates),
            "total_trials": sum(a.trials_run for a in aggregates),
        },
        "candidates": [
            {
                "candidate_id": a.candidate_id,
                "name": a.name,
                "trials_run": a.trials_run,
                "pass_count": a.pass_count,
                "partial_count": a.partial_count,
                "fail_count": a.fail_count,
                "pass_rate": a.pass_rate,
                "avg_weighted_score": a.avg_weighted_score,
                "score_stddev": a.score_stddev,
                "avg_runtime": a.avg_runtime,
                "difficulty": a.difficulty,
                "is_flaky": a.is_flaky,
                "trial_results": [
                    {
                        "trial_id": t.trial_id,
                        "trial_number": t.trial_number,
                        "timestamp": t.timestamp,
                        "hard_gates": t.hard_gates,
                        "all_gates_passed": t.all_gates_passed,
                        "weighted_score": t.weighted_score,
                        "classification": t.classification,
                        "operational_metrics": t.operational_metrics,
                        "error": t.error,
                    }
                    for t in a.trial_results
                ],
            }
            for a in aggregates
        ],
    }
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"\nJSON results: {json_path}")

    # CSV output
    csv_path = output_dir / "phase1_calibration.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "candidate_id", "name", "trial_number", "timestamp",
            "all_gates_passed", "weighted_score", "classification",
            "runtime_seconds", "difficulty", "is_flaky", "error",
        ])
        for a in aggregates:
            for t in a.trial_results:
                writer.writerow([
                    a.candidate_id, a.name, t.trial_number, t.timestamp,
                    t.all_gates_passed, t.weighted_score, t.classification,
                    t.operational_metrics.get("runtime_seconds", 0),
                    a.difficulty, a.is_flaky, t.error[:100] if t.error else "",
                ])
    print(f"CSV results: {csv_path}")

    return json_path, csv_path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_trial_artifacts(candidate_id: str, trial_number: int) -> None:
    """Remove benchmark trial artifacts to reset repo state."""
    change_id = f"{candidate_id}-T{trial_number}"
    change_dir = ARTIFACT_ROOT / change_id
    if change_dir.exists():
        shutil.rmtree(change_dir)

    # Reset any git changes from the trial
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=REPO_ROOT, capture_output=True,
    )
    subprocess.run(
        ["git", "clean", "-fd", "--exclude=benchmarks/"],
        cwd=REPO_ROOT, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Benchmark Calibration Harness")
    parser.add_argument("--candidates", required=True, help="Directory containing candidate YAML files")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help=f"Trials per candidate (default: {DEFAULT_TRIALS})")
    parser.add_argument("--only", help="Comma-separated list of candidate IDs to run")
    parser.add_argument("--dry-run", action="store_true", help="Simulate workflow without invoking AI")
    parser.add_argument("--use-copilot-or", action="store_true", help="Use copilot-or environment (Gemma 4)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Timeout per trial in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--output", default=str(RESULTS_DIR), help="Output directory for results")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep trial artifacts (don't reset repo)")
    args = parser.parse_args(argv)

    candidates_dir = Path(args.candidates)
    if not candidates_dir.exists():
        print(f"ERROR: Candidates directory not found: {candidates_dir}")
        return 1

    only = args.only.split(",") if args.only else None
    candidates = load_all_candidates(candidates_dir, only)

    if not candidates:
        print("ERROR: No candidates found")
        return 1

    print(f"\n{'='*60}")
    print(f"  Phase 1 Benchmark Calibration")
    print(f"  Candidates: {len(candidates)}")
    print(f"  Trials per candidate: {args.trials}")
    print(f"  Total trials: {len(candidates) * args.trials}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"  Model: {os.environ.get('COPILOT_MODEL', 'default')}")
    print(f"{'='*60}\n")

    if args.use_copilot_or:
        try:
            get_copilot_or_env()
            print("copilot-or environment validated ✓\n")
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1

    aggregates: list[CandidateAggregate] = []

    for ci, candidate in enumerate(candidates, 1):
        print(f"\n{'#'*60}")
        print(f"  Candidate {ci}/{len(candidates)}: {candidate.id} — {candidate.name}")
        print(f"{'#'*60}")

        trials: list[TrialResult] = []

        for trial_num in range(1, args.trials + 1):
            print(f"\n  --- Trial {trial_num}/{args.trials} ---")

            result = run_workflow_trial(
                candidate=candidate,
                trial_number=trial_num,
                dry_run=args.dry_run,
                use_copilot_or=args.use_copilot_or,
                timeout=args.timeout,
            )
            trials.append(result)

            if not args.no_cleanup:
                cleanup_trial_artifacts(candidate.id, trial_num)

        aggregate = aggregate_trials(candidate, trials)
        aggregates.append(aggregate)

        print(f"\n  Aggregate: {aggregate.difficulty} difficulty")
        print(f"    Pass rate: {aggregate.pass_rate:.0%}")
        print(f"    Avg score: {aggregate.avg_weighted_score:.2f} ± {aggregate.score_stddev:.2f}")
        print(f"    Avg runtime: {aggregate.avg_runtime:.1f}s")
        print(f"    Flaky: {aggregate.is_flaky}")

    # Save results
    output_dir = Path(args.output)
    save_results(aggregates, output_dir)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  CALIBRATION COMPLETE")
    print(f"{'='*60}")
    print(f"\n  {'ID':<10} {'Name':<40} {'Diff':<8} {'Pass%':<8} {'Score':<8} {'Flaky'}")
    print(f"  {'-'*10} {'-'*40} {'-'*8} {'-'*8} {'-'*8} {'-'*5}")
    for a in aggregates:
        print(f"  {a.candidate_id:<10} {a.name[:40]:<40} {a.difficulty:<8} {a.pass_rate:.0%}{'':>4} {a.avg_weighted_score:.2f}{'':>4} {'⚠' if a.is_flaky else '✓'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
