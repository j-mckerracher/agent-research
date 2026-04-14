# Phase 2 Workflow Validation Report

## What was run

- **Model:** gpt-5-mini
- **Scope:** medium
- **Trials per story:** 1
- **Timeout per trial:** 1200s
- **Mode:** real workflow

| Story  | Role             | Difficulty | Trials | pass@1 | pass@3 | Pass rate | Avg passing AC score | Avg runtime |
| ------ | ---------------- | ---------- | -----: | -----: | -----: | --------: | -------------------: | ----------: |
| BM-003 | medium-benchmark | medium     |      1 |   0.00 |   0.00 |      0.00 |                 0.00 |      350.5s |

## What passed and failed

- **Total trials:** 1
- **Passing trials:** 0
- **Overall pass rate:** 0.00
- **Mean AC score for passing trials:** 0.00

## Workflow struggle points

- **unknown:** 1 failing trial(s)

## Baseline

- **pass@1:** 0.00
- **pass@3:** 0.00
- **Median retries:** 0.00
- **Median runtime:** 350.48s

## CI gating readiness

- Not yet ready for CI gating. Failure modes remain too common in the real workflow baseline.

_Generated: 2026-04-11T23:20:14Z_
