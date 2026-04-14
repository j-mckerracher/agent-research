# Phase 2 — Definition of Done

Phase 2 validates the **multi-agent workflow** itself, not the benchmark.
All Phase 1 benchmark assets are frozen and reused without modification.

## Required Conditions

All conditions below must be true for Phase 2 to be considered complete:

### 1. Phase 1 Assets Frozen and Reused

- `benchmarks/selected/*.yaml` — read-only
- `benchmarks/grading.yaml` — read-only
- Phase 1 scoring logic — unchanged

### 2. Workflow Execution Coverage

Selected benchmark stories are executed through the **full workflow**:

- intake (synthetic, from benchmark story)
- task-generator + task-plan-evaluator loop
- task-assigner + assignment-evaluator loop
- software-engineer + implementation-evaluator loop
- qa-engineer + qa-evaluator loop
- lessons-optimizer (terminal)

### 3. Phase 2 Hard Gates (per trial)

| Gate ID  | Description                          |
| -------- | ------------------------------------ |
| P2-HG-01 | Workflow reaches terminal success    |
| P2-HG-02 | Build succeeds (`tsc` or `nx build`) |
| P2-HG-03 | Tests pass (if applicable)           |
| P2-HG-04 | Required artifacts exist             |
| P2-HG-05 | Artifact schemas valid               |
| P2-HG-06 | No unresolved escalations            |
| P2-HG-07 | No forbidden file changes            |
| P2-HG-08 | Phase 1 hidden checks pass           |

### 4. Phase 1 Grading Reused Verbatim

- AC scoring: 1.0 / 0.5 / 0.0
- Hard-gate failure overrides AC score → 0.0

### 5. Trial Count

- Minimum 3 trials per selected story

### 6. Workflow-Level Metrics Captured

- Retries per stage
- Evaluator loop counts
- Escalation frequency
- Wall-clock runtime per stage
- Token usage per stage (if available)
- Failure stage attribution

### 7. Phase 2 Baseline Established

- pass@1 and pass@3
- Mean AC score for passing trials
- Median retries
- Median runtime
- Known failure modes

### 8. Machine-Readable Results

- `benchmarks/results/phase2_trials.json`
- `benchmarks/results/phase2_summary.json`
- `benchmarks/phase2/baseline.json`

### 9. Phase 2 Report

- `benchmarks/phase2/report.md`
- Covers: what was run, pass/fail, workflow struggles, baseline, CI readiness
