# Phase 1 — Benchmark Goal Statement

## Objective

Establish a permanent, repeatable benchmark suite that measures the agentic workflow's ability to accept a realistic Angular/TypeScript story, produce correct artifacts, implement a working solution, and pass deterministic validation — with stable enough performance across repeated trials that regressions can be detected over time.

## Success Criteria

A successful Phase 1 produces:

1. A pool of 15–30 candidate benchmark stories calibrated against GPT-5 mini (high).
2. Measured difficulty classifications (Easy / Medium / Hard) based on 5-trial pass rates.
3. A permanent selected benchmark set: 1–2 Easy, 3–5 Medium, 1–2 Hard.
4. A deterministic grading system with hard gates, weighted AC scoring, and tracked metrics.
5. A runnable calibration harness that integrates with the repo's existing workflow runner.
6. Machine-readable results and a human-readable report.

## Design Principles

- **Deterministic over subjective**: File existence, compilation, test results, and lint checks are primary grading signals.
- **Repeatable**: Fixed model, prompt template, repo state, timeouts, and grading logic per batch.
- **Realistic**: Stories modify existing code, require test updates, and exercise planning + implementation + QA.
- **Efficient**: Stories are small enough to run 5+ times without excessive cost or time.
- **Separable**: Workflow failures, grading failures, and model variance are independently measurable.

## Non-Goals (Phase 1)

- CI integration (Phase 2)
- Trend tracking across workflow versions (Phase 2)
- Multi-model comparison (Phase 2)
- Subjective quality scoring (out of scope)

## Model Path

- **Model**: GPT-5 mini (high reasoning effort)
- **CLI**: `copilot -p "<prompt>" --model gpt-5-mini --effort high --yolo --silent`
- **Rationale**: GPT-5 mini (high) is the target model for the workflow. Using it directly ensures calibration reflects production behavior.
