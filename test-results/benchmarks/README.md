# Agentic Workflow Benchmark

## Purpose

This benchmark measures whether changes to the agentic workflow improve or degrade the workflow's ability to complete moderately difficult software stories reliably within this Nx Angular monorepo.

## Goals

1. **Separate failure modes** — Distinguish workflow failures, grading failures, and model variability so regressions can be attributed to the correct root cause.
2. **Measure repeatability** — Use multiple trials per story to quantify variance and detect flaky outcomes vs. genuine regressions.
3. **Deterministic grading** — Rely on file-existence checks, TypeScript compilation, test execution, lint passes, and structural validation rather than subjective LLM-as-judge evaluation.
4. **Realistic scope** — Benchmark stories exercise planning, implementation, and QA across 2–4 files, requiring at least one test update and covering at least one edge case.
5. **Low overhead** — Stories are small enough to run repeatedly (5 trials per candidate during calibration) and require no network access, secrets, or human escalation.

## Structure

```
benchmarks/
├── README.md                    # This file
├── phase1/
│   └── goal.md                  # Durable benchmark goal statement
├── candidates/                  # 15–30 candidate story specs (YAML)
├── selected/                    # Final permanent benchmark set
│   └── manifest.yaml            # Index of selected stories
├── grading.yaml                 # Deterministic grading specification
├── docs/
│   └── grading.md               # Human-readable grading documentation
└── results/
    ├── phase1_calibration.json  # Machine-readable calibration results
    ├── phase1_calibration.csv   # Tabular calibration results
    └── phase1_report.md         # Final Phase 1 report
scripts/
└── run_phase1_calibration.py    # Calibration harness
```

## Quick Start

```bash
# Run full calibration (5 trials × N candidates)
python3 scripts/run_phase1_calibration.py

# Run a single candidate for debugging
python3 scripts/run_phase1_calibration.py --candidate BM-001 --trials 1

# Run with custom model
python3 scripts/run_phase1_calibration.py --model gpt-5-mini --effort high
```

## Phases

- **Phase 1** (current): Establish benchmark acceptance criteria — candidate generation, calibration, difficulty classification, permanent benchmark selection.
- **Phase 2** (future): Continuous regression detection — integrate into CI, track trends, alert on regressions.
