# Benchmark Grading Documentation

## Overview

The benchmark grading system uses a three-layer approach:

1. **Hard Gates** — Binary pass/fail checks that must ALL pass before any scoring occurs
2. **Weighted AC Scoring** — Visible acceptance criteria scored on a 0.0 / 0.5 / 1.0 scale
3. **Tracked Metrics** — Operational measurements that are recorded but not scored

## Hard Gates

Every trial must pass all hard gates to receive a non-zero score:

| Gate  | Name                  | Description                                       |
| ----- | --------------------- | ------------------------------------------------- |
| HG-01 | workflow_exit_success | The copilot CLI exited with code 0                |
| HG-02 | required_files_exist  | All required artifact files exist after the trial |
| HG-03 | typescript_compiles   | Affected projects compile without errors          |
| HG-04 | lint_passes           | Affected projects pass linting                    |
| HG-05 | hidden_checks_pass    | All hidden structural/behavioral checks pass      |

If any gate fails, the trial status is `gate_fail` and the score is 0.0.

## Visible AC Scoring

Each visible acceptance criterion is scored:

- **1.0** — Fully satisfied
- **0.5** — Partially satisfied (e.g., implemented but with minor issues)
- **0.0** — Not satisfied

The final score is a weighted average: `sum(score × weight) / sum(weight)`.

A trial **passes** if:

- All hard gates pass, AND
- Weighted AC score ≥ 0.7

## Hidden Checks

Hidden checks are deterministic validations the agent never sees. They verify structural correctness:

- `file_contains` / `file_not_contains` — String/regex presence in files
- `file_exists` — File existence at expected paths
- `grep_match` / `grep_no_match` — Regex matching
- `command_succeeds` — Shell command exit code 0
- `test_passes` — Specific test suite passes

## Tracked Metrics

These are recorded for analysis but do not affect pass/fail:

| Metric                | Description                              |
| --------------------- | ---------------------------------------- |
| runtime_seconds       | Wall-clock execution time                |
| files_changed         | Number of files modified/created/deleted |
| lines_changed         | Total lines added + removed              |
| copilot_output_length | Character count of agent output          |
| retry_count           | Number of retries on transient failures  |

## Difficulty Classification

Based on measured 5-trial pass rate:

| Difficulty | Pass Rate       | Description                       |
| ---------- | --------------- | --------------------------------- |
| Easy       | 80–100% (4–5/5) | Agent reliably completes these    |
| Medium     | 40–79% (2–3/5)  | Core benchmark stories            |
| Hard       | 0–39% (0–1/5)   | Currently difficult for the agent |

## Trial Statuses

| Status      | Meaning                                 |
| ----------- | --------------------------------------- |
| `pass`      | All gates passed, AC score ≥ 0.7        |
| `low_score` | All gates passed, AC score < 0.7        |
| `gate_fail` | One or more hard gates failed           |
| `error`     | Infrastructure failure (timeout, crash) |
