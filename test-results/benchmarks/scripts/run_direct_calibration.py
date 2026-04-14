#!/usr/bin/env python3
"""Phase 1 Direct Model Calibration Harness

Calls the configured model directly via OpenRouter API for maximum token efficiency.
This avoids the full multi-agent workflow overhead, allowing calibration
to run within constrained credit budgets.

The harness:
1. Reads candidate story YAMLs
2. Reads relevant source files from the repo
3. Sends a focused coding prompt to the configured model (via copilot-or)
4. Parses the response for code changes
5. Applies changes to files
6. Runs hidden checks
7. Reverts changes
8. Records structured results

Usage:
    # Run calibration on specific candidates
    python3 benchmarks/scripts/run_direct_calibration.py --only BM-001,BM-003,BM-020

    # Run on all candidates
    python3 benchmarks/scripts/run_direct_calibration.py

    # Dry-run (validate harness without calling model)
    python3 benchmarks/scripts/run_direct_calibration.py --dry-run

    # Custom trials count
    python3 benchmarks/scripts/run_direct_calibration.py --trials 3 --only BM-001
"""

from __future__ import annotations

import argparse
import csv
import glob as globmod
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

try:
    import requests  # type: ignore[import-untyped]
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BENCHMARK_ROOT = REPO_ROOT / "benchmarks"
RESULTS_DIR = BENCHMARK_ROOT / "results"
DEFAULT_TRIALS = 5
MAX_OUTPUT_TOKENS = 2048
MAX_PROMPT_TOKENS = 4096


# ---------------------------------------------------------------------------
# Data structures (reused from run_calibration.py)
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
class TrialResult:
    trial_id: str
    candidate_id: str
    trial_number: int
    timestamp: str
    hard_gates: dict[str, bool]
    all_gates_passed: bool
    ac_scores: dict[str, dict[str, Any]]
    weighted_score: float
    classification: str
    operational_metrics: dict[str, Any]
    response_text: str = ""
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
    difficulty: str
    is_flaky: bool
    trial_results: list[TrialResult] = field(default_factory=list)
    api_failure_count: int = 0
    model_pass_rate: float = 0.0


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_candidate(path: Path) -> CandidateStory:
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    visible_acs = []
    for key, val in data.get("visible_acceptance_criteria", {}).items():
        visible_acs.append(AcceptanceCriterion(key=key, text=val["text"], weight=val["weight"]))

    hidden_checks = []
    for hc in data.get("hidden_checks", []):
        hidden_checks.append(HiddenCheck(
            **{k: v for k, v in hc.items() if k != "description"},
            description=hc.get("description", ""),
        ))

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
    candidates = []
    for path in sorted(directory.glob("*.yaml")):
        candidate = load_candidate(path)
        if only is None or candidate.id in only:
            candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------------------
# OpenRouter API
# ---------------------------------------------------------------------------


def get_openrouter_config() -> dict[str, str]:
    """Extract OpenRouter config from copilot-or alias in ~/.zshrc."""
    config: dict[str, str] = {}

    # Check env vars first
    for var in ["COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY", "COPILOT_MODEL"]:
        val = os.environ.get(var)
        if val:
            config[var] = val

    # Fall back to extracting from ~/.zshrc
    if not config.get("COPILOT_PROVIDER_API_KEY"):
        try:
            result = subprocess.run(
                ["bash", "-c", "grep -A 10 'alias copilot-or' ~/.zshrc 2>/dev/null"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                m = re.search(r'(COPILOT_[A-Z_]+)="([^"]*)"', line)
                if m:
                    config[m.group(1)] = m.group(2)
        except Exception:
            pass

    required = ["COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY", "COPILOT_MODEL"]
    missing = [v for v in required if not config.get(v)]
    if missing:
        raise RuntimeError(f"Missing OpenRouter config: {missing}")

    return config


def call_model(prompt: str, config: dict[str, str], max_tokens: int = MAX_OUTPUT_TOKENS,
               max_retries: int = 3, retry_backoff: float = 8.0) -> tuple[str, dict[str, Any]]:
    """Call the configured model via OpenRouter API using curl with retry logic.

    Retries on transient errors (provider errors, timeouts, aborts).
    Returns (response_text, usage_stats).
    """
    import tempfile

    base_url = config["COPILOT_PROVIDER_BASE_URL"].rstrip("/")
    api_key = config["COPILOT_PROVIDER_API_KEY"]
    model = config.get("COPILOT_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    payload_json = json.dumps(payload)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload_json)
        payload_file = f.name

    last_error = None
    try:
        for attempt in range(1, max_retries + 1):
            try:
                result = subprocess.run(
                    [
                        "curl", "-s", "--max-time", "120",
                        "-X", "POST", url,
                        "-H", f"Authorization: Bearer {api_key}",
                        "-H", "Content-Type: application/json",
                        "-H", "HTTP-Referer: https://github.com/mcs-products-mono-ui",
                        "-d", f"@{payload_file}",
                    ],
                    capture_output=True, text=True, timeout=130,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr}")
                if not result.stdout.strip():
                    raise RuntimeError("Empty response from OpenRouter API")
                data = json.loads(result.stdout)
                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    # Non-retryable errors
                    if "402" in msg or "credits" in msg.lower() or "quota" in msg.lower():
                        raise RuntimeError(f"OpenRouter API error: {msg}")
                    raise RuntimeError(f"OpenRouter API error: {msg}")
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                if attempt > 1:
                    print(f"    ✓ Succeeded on attempt {attempt}")
                return text, usage
            except RuntimeError as e:
                last_error = e
                err_str = str(e)
                # Don't retry on credit exhaustion
                if "402" in err_str or "credits" in err_str.lower():
                    raise
                if attempt < max_retries:
                    wait = retry_backoff * attempt
                    print(f"    ⚡ Attempt {attempt}/{max_retries} failed: {err_str[:80]}. Retrying in {wait:.0f}s...")
                    time.sleep(wait)
        # All retries exhausted
        raise last_error  # type: ignore[misc]
    finally:
        os.unlink(payload_file)


def call_model_via_copilot(prompt: str, config: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Call the configured model via copilot CLI with copilot-or env vars."""
    env = os.environ.copy()
    for k, v in config.items():
        env[k] = v
    env["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"] = str(MAX_OUTPUT_TOKENS)
    env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = str(MAX_PROMPT_TOKENS)

    result = subprocess.run(
        ["copilot", "-p", prompt, "--output-format", "text", "-s", "--stream", "off"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"copilot-or failed (exit {result.returncode}): {result.stderr}")

    # Strip the "● " prefix copilot adds
    text = result.stdout.strip()
    if text.startswith("● "):
        text = text[2:]

    return text, {"method": "copilot-cli"}


# ---------------------------------------------------------------------------
# Source file reading
# ---------------------------------------------------------------------------


def get_component_path(candidate: CandidateStory) -> str:
    """Get the component path from grading metadata."""
    return candidate.grading_metadata.get("component_path", "")


def read_source_files(candidate: CandidateStory) -> dict[str, str]:
    """Read relevant source files for a candidate story."""
    files: dict[str, str] = {}
    comp_path = get_component_path(candidate)

    if comp_path:
        full_path = REPO_ROOT / comp_path
        if full_path.exists():
            for f in full_path.iterdir():
                if f.is_file() and f.suffix in (".ts", ".html", ".scss", ".css"):
                    rel = str(f.relative_to(REPO_ROOT))
                    try:
                        files[rel] = f.read_text(errors="replace")
                    except Exception:
                        pass

    # Also look at hidden check paths for context
    for hc in candidate.hidden_checks:
        target = hc.path or hc.path_pattern
        if target and "*" not in target:
            full = REPO_ROOT / target
            if full.exists():
                rel = str(full.relative_to(REPO_ROOT))
                if rel not in files:
                    try:
                        files[rel] = full.read_text(errors="replace")
                    except Exception:
                        pass

    return files


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def build_prompt(candidate: CandidateStory, source_files: dict[str, str]) -> str:
    """Build a focused coding prompt for the model."""
    parts = []
    parts.append("You are an Angular developer. Complete the following task.\n")
    parts.append(f"## Task: {candidate.name}\n")
    parts.append(candidate.description.strip())
    parts.append("")

    # Add acceptance criteria
    parts.append("## Acceptance Criteria")
    for ac in candidate.visible_acs:
        parts.append(f"- {ac.key}: {ac.text}")
    parts.append("")

    # Add source files (only if small enough)
    if source_files:
        parts.append("## Existing Source Files\n")
        total_chars = sum(len(v) for v in source_files.values())
        if total_chars < 6000:  # Keep prompt under ~1500 tokens
            for path, content in source_files.items():
                parts.append(f"### {path}")
                parts.append(f"```\n{content}\n```")
                parts.append("")
        else:
            # Only include the most relevant files
            for path, content in sorted(source_files.items(), key=lambda x: len(x[1])):
                if sum(len(p) for p in parts) + len(content) < 8000:
                    parts.append(f"### {path}")
                    parts.append(f"```\n{content}\n```")
                    parts.append("")

    # Instructions for response format
    parts.append("## Response Instructions")
    parts.append("Output ONLY the modified/new file contents. For each file:")
    parts.append("1. Write the relative file path on its own line prefixed with FILE:")
    parts.append("2. Follow with the complete file content in a code block")
    parts.append("3. Include ALL files that need to be created or modified")
    parts.append("")
    parts.append("Example format:")
    parts.append("FILE: path/to/file.ts")
    parts.append("```typescript")
    parts.append("// complete file content here")
    parts.append("```")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing and file application
# ---------------------------------------------------------------------------


def parse_file_changes(response: str) -> dict[str, str]:
    """Parse model response for file changes. Returns {path: content}."""
    changes: dict[str, str] = {}

    # Pattern 1: FILE: path\n```...\n```
    pattern1 = r'FILE:\s*(.+?)\s*\n```(?:\w*)\n(.*?)```'
    for match in re.finditer(pattern1, response, re.DOTALL):
        path = match.group(1).strip()
        content = match.group(2)
        changes[path] = content

    if changes:
        return changes

    # Pattern 2: ### path\n```...\n```
    pattern2 = r'###?\s+(.+?\.(?:ts|html|scss|css|spec\.ts|cy\.ts))\s*\n```(?:\w*)\n(.*?)```'
    for match in re.finditer(pattern2, response, re.DOTALL):
        path = match.group(1).strip()
        content = match.group(2)
        changes[path] = content

    if changes:
        return changes

    # Pattern 3: Just code blocks with filenames in comments
    pattern3 = r'```(?:typescript|html|scss|css)\s*\n(?://\s*(.+?\.(?:ts|html|scss|css))\s*\n)?(.*?)```'
    for match in re.finditer(pattern3, response, re.DOTALL):
        path = match.group(1)
        content = match.group(2)
        if path:
            changes[path.strip()] = content

    return changes


def apply_file_changes(changes: dict[str, str]) -> dict[str, str]:
    """Apply parsed file changes to the repo. Returns {path: status}."""
    results: dict[str, str] = {}

    for rel_path, content in changes.items():
        full_path = REPO_ROOT / rel_path
        try:
            if full_path.exists():
                full_path.write_text(content)
                results[rel_path] = "modified"
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content)
                results[rel_path] = "created"
        except Exception as e:
            results[rel_path] = f"error: {e}"

    return results


def revert_changes() -> None:
    """Revert all git changes."""
    subprocess.run(["git", "checkout", "--", "."], cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "clean", "-fd", "--exclude=benchmarks/"], cwd=REPO_ROOT, capture_output=True)


# ---------------------------------------------------------------------------
# Hidden check execution
# ---------------------------------------------------------------------------


def run_hidden_check(hc: HiddenCheck) -> bool:
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
            return True  # Skip in calibration

        else:
            return False
    except Exception as e:
        print(f"    Check error: {e}")
        return False


# ---------------------------------------------------------------------------
# AC scoring
# ---------------------------------------------------------------------------


def extract_keywords(text: str) -> list[str]:
    stop_words = {"must", "be", "the", "a", "an", "is", "are", "have", "has", "with", "for", "to", "of", "in", "on"}
    words = re.findall(r'\w+', text.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


def score_ac(ac: AcceptanceCriterion, candidate: CandidateStory, changes_applied: dict[str, str]) -> tuple[float, str]:
    """Score a single AC based on hidden checks and applied changes."""
    related_hidden = [
        hc for hc in candidate.hidden_checks
        if any(kw in ac.text.lower() for kw in extract_keywords(hc.description))
    ]

    if related_hidden:
        passed = [run_hidden_check(hc) for hc in related_hidden]
        if all(passed):
            return 1.0, f"All {len(related_hidden)} related hidden checks passed"
        elif any(passed):
            return 0.5, f"{sum(passed)}/{len(passed)} related hidden checks passed"
        else:
            return 0.0, "Related hidden checks failed"

    # Check if any files were changed at all
    if changes_applied:
        return 0.5, "Files were modified but no deterministic check available"

    return 0.0, "No changes applied"


# ---------------------------------------------------------------------------
# Aggregation and classification
# ---------------------------------------------------------------------------


def classify_difficulty(pass_rate: float, avg_score: float, avg_runtime: float) -> str:
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

    # For direct mode, runtime is less meaningful; use generous thresholds
    if avg_runtime < 60:
        classifications.append("Easy")
    elif avg_runtime < 120:
        classifications.append("Medium")
    else:
        classifications.append("Hard")

    order = {"Hard": 0, "Medium": 1, "Easy": 2}
    return min(classifications, key=lambda c: order[c])


def aggregate_trials(candidate: CandidateStory, trials: list[TrialResult]) -> CandidateAggregate:
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

    # Compute model-only metrics (exclude API failures)
    model_responded_trials = [t for t in trials if t.hard_gates.get("model_responded", False)]
    api_failure_count = n - len(model_responded_trials)
    model_pass_count = sum(1 for t in model_responded_trials if t.all_gates_passed)
    model_pass_rate = model_pass_count / len(model_responded_trials) if model_responded_trials else 0.0

    # Use model_pass_rate for difficulty classification when we have enough model responses
    if len(model_responded_trials) >= 2:
        # Model scores only (when API actually responded)
        model_scores = [t.weighted_score for t in model_responded_trials]
        model_avg = sum(model_scores) / len(model_scores)
        model_runtimes = [t.operational_metrics.get("runtime_seconds", 0) for t in model_responded_trials]
        model_avg_runtime = sum(model_runtimes) / len(model_runtimes)
        difficulty = classify_difficulty(model_pass_rate, model_avg, model_avg_runtime)
    else:
        difficulty = classify_difficulty(pass_rate, avg_score, avg_runtime)

    is_flaky = n > 1 and 0.2 <= pass_rate <= 0.8 and stddev > 0.3

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
        api_failure_count=api_failure_count,
        model_pass_rate=round(model_pass_rate, 4),
    )


# ---------------------------------------------------------------------------
# Trial execution
# ---------------------------------------------------------------------------


def run_trial(
    candidate: CandidateStory,
    trial_number: int,
    or_config: dict[str, str],
    dry_run: bool = False,
    use_api: bool = True,
) -> TrialResult:
    """Run a single direct calibration trial."""
    trial_id = f"{candidate.id}_trial_{trial_number}"
    timestamp = datetime.now(timezone.utc).isoformat()
    start_time = time.monotonic()

    print(f"\n  {'='*56}")
    print(f"  Trial: {trial_id}")
    print(f"  Candidate: {candidate.name}")
    print(f"  {'='*56}\n")

    hard_gates = {
        "model_responded": False,
        "response_contains_code": False,
        "files_applied": False,
        "hidden_checks_pass": False,
    }
    ac_scores: dict[str, dict[str, Any]] = {}
    response_text = ""
    error_msg = ""
    changes_applied: dict[str, str] = {}
    usage_stats: dict[str, Any] = {}

    try:
        # Step 1: Read source files
        source_files = read_source_files(candidate)
        print(f"  [1/6] Read {len(source_files)} source files")

        # Step 2: Build prompt
        prompt = build_prompt(candidate, source_files)
        prompt_chars = len(prompt)
        print(f"  [2/6] Built prompt ({prompt_chars} chars, ~{prompt_chars // 4} tokens)")

        # Step 3: Call model
        if dry_run:
            print("  [3/6] DRY RUN - simulating model response...")
            response_text = "FILE: example.ts\n```typescript\n// dry run response\n```"
            usage_stats = {"dry_run": True}
            hard_gates["model_responded"] = True
        else:
            model_name = or_config.get("COPILOT_MODEL", "unknown")
            print(f"  [3/6] Calling {model_name} via OpenRouter...")
            try:
                if use_api and requests is not None:
                    response_text, usage_stats = call_model(prompt, or_config)
                else:
                    response_text, usage_stats = call_model_via_copilot(prompt, or_config)
                hard_gates["model_responded"] = True
                print(f"    Response: {len(response_text)} chars")
                if usage_stats:
                    print(f"    Usage: {usage_stats}")
            except RuntimeError as e:
                error_msg = str(e)
                print(f"    ERROR: {error_msg}")

        # Step 4: Parse and apply changes
        if hard_gates["model_responded"]:
            changes = parse_file_changes(response_text)
            has_code = bool(changes) or bool(re.findall(r'```\w*\n', response_text))
            hard_gates["response_contains_code"] = has_code
            print(f"  [4/6] Parsed {len(changes)} file changes (code blocks: {has_code})")

            if changes and not dry_run:
                changes_applied = apply_file_changes(changes)
                hard_gates["files_applied"] = any(
                    s in ("modified", "created") for s in changes_applied.values()
                )
                for path, status in changes_applied.items():
                    print(f"    {status}: {path}")
            elif dry_run:
                hard_gates["files_applied"] = True
                changes_applied = {"example.ts": "dry_run"}
        else:
            print("  [4/6] Skipped (no response)")

        # Step 5: Run hidden checks
        print("  [5/6] Running hidden checks...")
        hidden_results = []
        for hc in candidate.hidden_checks:
            result = run_hidden_check(hc)
            hidden_results.append(result)
            status = "PASS" if result else "FAIL"
            print(f"    {status}: {hc.description}")

        if hidden_results:
            hard_gates["hidden_checks_pass"] = all(hidden_results)
        else:
            hard_gates["hidden_checks_pass"] = True

        # Step 6: Score ACs
        print("  [6/6] Scoring acceptance criteria...")
        for ac in candidate.visible_acs:
            score, evidence = score_ac(ac, candidate, changes_applied)
            ac_scores[ac.key] = {
                "score": score,
                "weight": ac.weight,
                "method": "deterministic",
                "evidence": evidence,
            }
            print(f"    {ac.key}: {score}/{ac.weight} — {evidence[:60]}")

    except Exception as e:
        error_msg = f"Trial error: {e}\n{traceback.format_exc()}"
        print(f"  ERROR: {error_msg}")

    finally:
        # Always revert
        if not dry_run:
            revert_changes()

    # Calculate aggregate score
    elapsed = time.monotonic() - start_time
    all_gates = all(hard_gates.values())

    if ac_scores:
        total_weighted = sum(s["score"] * s["weight"] for s in ac_scores.values())
        total_weight = sum(s["weight"] for s in ac_scores.values())
        weighted_score = total_weighted / total_weight if total_weight > 0 else 0.0
    else:
        weighted_score = 0.0

    # Gate failure always means FAIL
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
        ac_scores=ac_scores,
        weighted_score=round(weighted_score, 4),
        classification=classification,
        operational_metrics={
            "runtime_seconds": round(elapsed, 2),
            "prompt_chars": len(build_prompt(candidate, read_source_files(candidate))) if not error_msg else 0,
            "response_chars": len(response_text),
            "files_changed": len(changes_applied),
            "usage": usage_stats,
        },
        response_text=response_text[:2000],  # Truncate for storage
        error=error_msg,
    )

    print(f"\n  Result: {classification} (score={weighted_score:.2f}, gates={'ALL PASS' if all_gates else 'FAIL'})")
    print(f"  Runtime: {elapsed:.1f}s")

    return result


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def save_results(aggregates: list[CandidateAggregate], output_dir: Path, model_name: str = "unknown") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON output
    json_path = output_dir / "phase1_calibration.json"
    json_data = {
        "calibration_run": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model_name,
            "method": "direct_api_call",
            "harness": "run_direct_calibration.py",
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
                "model_pass_rate": a.model_pass_rate,
                "api_failure_count": a.api_failure_count,
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
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Direct Model Calibration")
    parser.add_argument("--candidates", default=str(BENCHMARK_ROOT / "candidates"),
                        help="Directory containing candidate YAML files")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                        help=f"Trials per candidate (default: {DEFAULT_TRIALS})")
    parser.add_argument("--only", help="Comma-separated list of candidate IDs to run")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without calling the model")
    parser.add_argument("--use-copilot-cli", action="store_true",
                        help="Use copilot CLI instead of direct API calls")
    parser.add_argument("--output", default=str(RESULTS_DIR), help="Output directory")
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

    # Get OpenRouter config
    or_config: dict[str, str] = {}
    model_name = "dry-run"
    if not args.dry_run:
        try:
            or_config = get_openrouter_config()
            model_name = or_config.get("COPILOT_MODEL", "unknown")
            print(f"OpenRouter config validated ✓ (model: {model_name})")
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1
    
    print(f"\n{'='*60}")
    print(f"  Phase 1 Direct Calibration — {model_name}")
    print(f"  Candidates: {len(candidates)}")
    print(f"  Trials per candidate: {args.trials}")
    print(f"  Total planned trials: {len(candidates) * args.trials}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else f'LIVE ({model_name})'}")
    print(f"  API: {'copilot CLI' if args.use_copilot_cli else 'direct OpenRouter'}")
    print(f"{'='*60}\n")

    aggregates: list[CandidateAggregate] = []
    credits_exhausted = False

    for ci, candidate in enumerate(candidates, 1):
        if credits_exhausted:
            print(f"\n  SKIPPING {candidate.id} — credits exhausted")
            continue

        print(f"\n{'#'*60}")
        print(f"  Candidate {ci}/{len(candidates)}: {candidate.id} — {candidate.name}")
        print(f"{'#'*60}")

        trials: list[TrialResult] = []

        for trial_num in range(1, args.trials + 1):
            if credits_exhausted:
                break

            print(f"\n  --- Trial {trial_num}/{args.trials} ---")

            result = run_trial(
                candidate=candidate,
                trial_number=trial_num,
                or_config=or_config,
                dry_run=args.dry_run,
                use_api=not args.use_copilot_cli,
            )
            trials.append(result)

            # Check for credit exhaustion
            if "402" in result.error or "credits" in result.error.lower():
                print("\n  ⚠️  CREDITS EXHAUSTED — stopping further trials")
                credits_exhausted = True

        if trials:
            aggregate = aggregate_trials(candidate, trials)
            aggregates.append(aggregate)

            # Compute model-only stats (exclude API failures)
            model_trials = [t for t in trials if t.hard_gates.get("model_responded", False)]
            model_pass = sum(1 for t in model_trials if t.all_gates_passed)
            model_rate = model_pass / len(model_trials) if model_trials else 0
            api_failures = len(trials) - len(model_trials)

            print(f"\n  Aggregate: {aggregate.difficulty} difficulty")
            print(f"    Raw pass rate: {aggregate.pass_rate:.0%}")
            print(f"    Model pass rate: {model_rate:.0%} ({model_pass}/{len(model_trials)} when API responded)")
            print(f"    API failures: {api_failures}/{len(trials)}")
            print(f"    Avg score: {aggregate.avg_weighted_score:.2f} ± {aggregate.score_stddev:.2f}")
            print(f"    Avg runtime: {aggregate.avg_runtime:.1f}s")
            print(f"    Flaky: {aggregate.is_flaky}")

            # Incremental save after each candidate
            save_results(aggregates, Path(args.output), model_name)
            print(f"    (results saved incrementally)")

    # Final save
    if aggregates:
        json_path, csv_path = save_results(aggregates, Path(args.output), model_name)

        print(f"\n{'='*60}")
        print(f"  CALIBRATION COMPLETE")
        if credits_exhausted:
            print(f"  ⚠️  Stopped early due to credit exhaustion")
        print(f"{'='*60}\n")

        print(f"  {'ID':<10} {'Name':<40} {'Diff':<8} {'Pass%':<8} {'Score':<8} {'Flaky':<5}")
        print(f"  {'-'*10} {'-'*40} {'-'*8} {'-'*8} {'-'*8} {'-'*5}")
        for a in aggregates:
            flaky = "⚠" if a.is_flaky else "✓"
            print(f"  {a.candidate_id:<10} {a.name[:40]:<40} {a.difficulty:<8} {a.pass_rate:.0%}{'':>4} {a.avg_weighted_score:.2f}{'':>4} {flaky}")
    else:
        print("\nNo results collected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
