# Phase 1 Benchmark Report

## Executive Summary

Phase 1 established the benchmark infrastructure for measuring agentic workflow quality in the MCS Products Mono UI repository. The benchmark uses the **copilot CLI with GPT-5 mini (high effort)** as the model path and evaluates whether the workflow can reliably accept a realistic Angular software story, produce correct artifacts, pass deterministic validation, and do so consistently across repeated trials.

**Key results:**
- 25 candidate benchmark stories were designed
- 8 candidates were calibrated with 5 trials each (40 total trials)
- 30/40 trials passed (75% overall)
- 4 candidates measured as **easy**, 4 as **medium**, 0 as **hard**
- 6 stories were selected for the permanent benchmark set
- Total calibration runtime: ~3.8 hours

---

## 1. What Was Built

### Benchmark Infrastructure

| Component | Path | Purpose |
|-----------|------|---------|
| Goal statement | `benchmarks/README.md`, `benchmarks/phase1/goal.md` | Durable benchmark purpose |
| Candidate stories | `benchmarks/candidates/BM-001.yaml` - `BM-025.yaml` | 25 candidate benchmark specifications |
| Grading spec | `benchmarks/grading.yaml`, `benchmarks/docs/grading.md` | Deterministic grading rubric |
| Calibration harness | `scripts/run_phase1_calibration.py` | Runnable experiment harness |
| Results | `benchmarks/results/phase1_calibration.json`, `.csv` | Machine-readable calibration data |
| Selected set | `benchmarks/selected/*.yaml`, `manifest.yaml` | Permanent benchmark stories |
| Report | `benchmarks/results/phase1_report.md` | This document |

### Grading System

The grading system uses a three-tier approach:

1. **Hard Gates (HG-01 through HG-05):** Binary pass/fail checks that must all pass for a trial to succeed
   - HG-01: Copilot exit code 0
   - HG-02: Required artifact files exist
   - HG-03: TypeScript compiles (baseline: true - informational only)
   - HG-04: Lint passes (baseline: true - informational only)
   - HG-05: All hidden checks pass

2. **Weighted AC Scoring:** Each visible acceptance criterion scored 1.0 / 0.5 / 0.0, mapped from hidden check results. Pass threshold: >= 0.7 weighted average.

3. **Tracked Metrics:** Runtime, files changed, copilot output length, retries - collected for analysis but not used in pass/fail decisions.

### Calibration Harness

The harness (`scripts/run_phase1_calibration.py`) provides:
- Full CLI with `--candidate`, `--trials`, `--timeout`, `--dry-run` flags
- Git isolation using `git reset --hard` between trials
- Incremental JSON merge so runs can be batched
- Hidden check execution (grep_match, grep_no_match, file_exists, command_succeeds, test_passes)
- Automatic difficulty classification from measured pass rates
- CSV and JSON output formats

---

## 2. What Was Run

### Experimental Conditions

| Parameter | Value |
|-----------|-------|
| Model | GPT-5 mini |
| Effort | high |
| CLI flags | `--model gpt-5-mini --effort high --yolo --silent` |
| Trials per candidate | 5 |
| Timeout per trial | 600 seconds |
| Repo state | Fixed at commit `3c31615d4` |
| Grading | Deterministic (no LLM judge) |

### Calibration Runs

Due to rate limiting and data-loss bugs (resolved), calibration was run in multiple batches:

1. **Batch 1:** BM-001, BM-003, BM-006, BM-010 (20 trials)
2. **Batch 2:** BM-011, BM-015, BM-018, BM-021 (20 trials)

All batches used identical experimental conditions. Results were merged into a single dataset.

### Candidate Selection for Calibration

8 of 25 candidates were selected for calibration based on:
- Coverage of different difficulty estimates (easy, medium, hard)
- Diversity of modification patterns (utility functions, components, CSS, tests)
- Different areas of the codebase (common-testing, pattern-library, features)
- Feasibility within the 600s timeout

The remaining 17 candidates remain available for future calibration.

---

## 3. Assumptions Made

1. **Copilot `--yolo` commits changes:** The `--yolo` flag causes copilot to commit its work. Trials must compare `HEAD` vs baseline SHA, not use `git diff` on working tree.

2. **600s timeout is sufficient for most stories:** Some stories naturally take 300-500s. The 600s timeout causes some medium-difficulty stories to appear as failures when they might succeed with more time.

3. **Rate limiting is transient:** After ~20 consecutive invocations, copilot returns exit_code=1 with empty output in ~21s. This is treated as a transient error, not a story failure.

4. **HG-03 and HG-04 baseline to true:** TypeScript compilation and lint checks are recorded but default to passing because the full build toolchain is expensive to run per trial. These gates become meaningful in Phase 2 when build validation is integrated.

5. **Hidden checks are sufficient proxies for ACs:** Each visible acceptance criterion maps to one or more hidden checks (grep patterns, file existence, etc.). The mapping is approximate but deterministic.

6. **5 trials provide adequate signal:** With 5 trials, we can distinguish 0%%, 20%%, 40%%, 60%%, 80%%, and 100%% pass rates. This is sufficient for coarse difficulty classification but not for fine-grained regression detection (Phase 2 concern).

---

## 4. Results

### Trial-Level Summary

| Story ID | Name | Est. | Measured | Pass Rate | Avg Score | Avg Time | Min Time | Max Time | Flakiness |
|----------|------|------|----------|-----------|-----------|----------|----------|----------|-----------|
| BM-001 | Add maxLength input to TextLinkComponent | easy | easy | 100%% (5/5) | 1.00 | 301s | 155s | 485s | 0.00 |
| BM-003 | Add character counter to note-input | medium | medium | 60%% (3/5) | 0.60 | 448s | 203s | 600s | 0.96 |
| BM-006 | Add search/filter to notes component | hard | medium | 40%% (2/5) | 0.75 | 467s | 335s | 585s | 0.96 |
| BM-010 | Add note sorting to notes-container | medium | medium | 60%% (3/5) | 0.60 | 421s | 240s | 600s | 0.96 |
| BM-011 | Add byTestIdNth helper to common-testing | easy | easy | 100%% (5/5) | 1.00 | 140s | 122s | 163s | 0.00 |
| BM-015 | Add read/unread state to note component | medium | easy | 100%% (5/5) | 1.00 | 307s | 261s | 334s | 0.00 |
| BM-018 | Add CSS theme variable to text-link | easy | medium | 40%% (2/5) | 0.40 | 477s | 208s | 600s | 0.96 |
| BM-021 | Add locator constants to testing library | easy | easy | 100%% (5/5) | 1.00 | 185s | 138s | 241s | 0.00 |

### Aggregate Statistics

- **Total trials:** 40
- **Total passes:** 30 (75%%)
- **Total failures/errors:** 10 (25%%)
- **Total calibration runtime:** 13,728 seconds (3.8 hours)
- **Average trial runtime:** 343 seconds (5.7 minutes)
- **Fastest trial:** 122s (BM-011, trial 4)
- **Slowest trial:** 600s (timeout - multiple candidates)

### Difficulty Distribution

| Difficulty | Count | Stories |
|------------|-------|---------|
| Easy | 4 | BM-001, BM-011, BM-015, BM-021 |
| Medium | 4 | BM-003, BM-006, BM-010, BM-018 |
| Hard | 0 | (none measured) |

### Estimate vs. Measured Accuracy

| | Measured Easy | Measured Medium |
|---|---|---|
| **Estimated Easy** | BM-001, BM-011, BM-021 | BM-018 |
| **Estimated Medium** | BM-015 | BM-003, BM-010 |
| **Estimated Hard** | - | BM-006 |

Notable: BM-018 (estimated easy) measured as medium due to timeout sensitivity. BM-015 (estimated medium) measured as easy - the model handles state-addition stories well. BM-006 (estimated hard) measured as medium - it partially succeeds more often than expected.

---

## 5. Difficulty Classification

Classification uses the grading spec thresholds:
- **Easy:** >= 80%% pass rate
- **Medium:** 40-79%% pass rate
- **Hard:** < 40%% pass rate

No candidates fell below 40%%, so no stories classified as hard. The medium-difficulty stories (40-60%% pass rate) serve as the primary benchmark discriminators, and the lowest (BM-006, BM-018 at 40%%) serve as stretch goals.

### Flakiness Analysis

Flakiness score uses `4 * rate * (1 - rate)`:
- Easy stories: 0.00 (perfectly consistent)
- Medium stories: 0.96 (high variance - expected for stories near the pass/fail boundary)

The 0.96 flakiness for medium stories is inherent to having pass rates near 50%%. This is acceptable for benchmark purposes because the *aggregate* pass rate across trials is the metric of interest, not individual trial outcomes.

---

## 6. Selected Benchmark Set

### Selection Criteria

| Role | Criteria | Count |
|------|----------|-------|
| Easy smoke | >= 80%% pass rate, low runtime, zero flakiness | 2 |
| Medium benchmark | 40-79%% pass rate, exercises real reasoning | 3 |
| Hard stretch | Lowest pass rate, timeout-prone | 1 |

### Selected Stories

| ID | Name | Role | Pass Rate | Avg Runtime |
|----|------|------|-----------|-------------|
| BM-011 | Add byTestIdNth helper to common-testing | easy-smoke | 100%% | 140s |
| BM-021 | Add locator constants to testing library | easy-smoke | 100%% | 185s |
| BM-003 | Add character counter to note-input | medium-benchmark | 60%% | 448s |
| BM-010 | Add note sorting to notes-container | medium-benchmark | 60%% | 421s |
| BM-006 | Add search/filter to notes component | medium-benchmark | 40%% | 467s |
| BM-018 | Add CSS theme variable to text-link | hard-stretch | 40%% | 477s |

### Selection Rationale

**Easy smoke stories** (BM-011, BM-021) were chosen because:
- 100%% pass rate across all trials
- Fast execution (140s and 185s average)
- Zero flakiness - any regression is immediately detectable
- They exercise different patterns (utility functions vs. constant exports)
- They validate basic workflow mechanics: prompt acceptance, file modification, deterministic grading

**Medium benchmark stories** (BM-003, BM-010, BM-006) were chosen because:
- 40-60%% pass rate provides meaningful discrimination
- They require non-trivial reasoning (character counting, sorting logic, search/filter)
- They exercise component + template + test file modifications
- BM-006 produces partial scores on failures, validating the grading granularity
- Workflow improvements should measurably increase their pass rates

**Hard stretch story** (BM-018) was chosen because:
- 40%% pass rate with 3/5 trials hitting the 600s timeout
- CSS theme variable work requires understanding the project style architecture
- Timeout sensitivity means even small workflow inefficiencies surface as failures
- Useful for detecting when a workflow change makes complex tasks worse

### Reserve Stories

| ID | Reason |
|----|--------|
| BM-001 | 100%% pass rate easy story, available as backup smoke if BM-011 or BM-021 break |
| BM-015 | 100%% pass rate, estimated medium but measured easy, useful if the set needs rebalancing |

---

## 7. Limitations

1. **Only 8 of 25 candidates calibrated:** 17 candidates remain uncalibrated. Future phases should calibrate more to expand the benchmark pool and find genuine hard stories.

2. **No hard-difficulty stories found:** All calibrated candidates measured as easy or medium. The remaining uncalibrated candidates (especially BM-007, BM-008, BM-012, BM-016) may yield hard stories.

3. **600s timeout conflates difficulty with speed:** Stories that the model *could* solve but takes too long are classified the same as genuinely unsolvable stories. Increasing timeout or measuring partial progress would improve classification.

4. **5 trials per candidate:** Sufficient for coarse classification but produces high confidence intervals. 10+ trials per candidate would give better statistical power for regression detection.

5. **HG-03 and HG-04 are baseline-true:** TypeScript compilation and lint checks are not actually run due to cost. Phase 2 should integrate lightweight build validation.

6. **Single model/effort level:** Only GPT-5 mini (high) was tested. The benchmark set difficulty may differ substantially for other models.

7. **Rate limiting:** After ~20 consecutive copilot invocations, rate limiting causes transient failures. Large calibration runs must be batched with cooldown periods.

8. **Flakiness in medium stories:** 0.96 flakiness means individual trial results are unreliable. The benchmark measures aggregate pass rates, not individual outcomes.

---

## 8. Phase 2 Recommendations

1. **Calibrate remaining 17 candidates** to find hard-difficulty stories and expand the benchmark pool.

2. **Increase trials to 10+** for selected stories to improve statistical confidence and enable smaller regression detection.

3. **Integrate build validation** - run `nx build` or `tsc --noEmit` as part of HG-03 to catch compilation errors.

4. **Add LLM-judge for subjective quality** as a secondary scorer (not primary) to evaluate code style, documentation, and solution elegance.

5. **Implement regression detection** - compare new run results against the Phase 1 baseline to detect statistically significant changes.

6. **Increase timeout to 900s** for medium/hard stories to separate "slow but correct" from "genuinely wrong."

7. **Add worktree isolation** instead of `git reset --hard` to enable parallel trial execution.

8. **Create a CI integration** so benchmark runs trigger automatically on workflow changes.

9. **Test additional models** (GPT-5, Claude Sonnet) to validate that the benchmark set discriminates across model capabilities.

---

## Appendix A: File Inventory

```
benchmarks/
  README.md                            Benchmark purpose and quick-start
  phase1/
    goal.md                            Phase 1 goal statement
  candidates/
    BM-001.yaml through BM-025.yaml    25 candidate story specs
  selected/
    BM-003.yaml                        Medium benchmark
    BM-006.yaml                        Medium benchmark
    BM-010.yaml                        Medium benchmark
    BM-011.yaml                        Easy smoke
    BM-018.yaml                        Hard stretch
    BM-021.yaml                        Easy smoke
    manifest.yaml                      Selection manifest
  grading.yaml                         Grading specification
  docs/
    grading.md                         Grading documentation
  results/
    phase1_calibration.json            Full calibration results (gitignored)
    phase1_calibration.csv             Trial-level CSV (gitignored)
    phase1_report.md                   This report (gitignored)
scripts/
  run_phase1_calibration.py            Calibration harness
```

## Appendix B: Copilot CLI Invocation

```bash
copilot -p "<story_prompt>" --model gpt-5-mini --effort high --yolo --silent
```

- `-p`: Non-interactive prompt mode
- `--model gpt-5-mini`: GPT-5 mini model
- `--effort high`: High effort/quality setting
- `--yolo`: Enable all permissions (causes copilot to commit changes)
- `--silent`: Suppress output except agent response
- Exit code 0 = success, 1 = failure

## Appendix C: Reproducing Calibration

```bash
# Run full calibration (all 8 calibrated candidates)
python3 scripts/run_phase1_calibration.py \
  --candidate BM-001 BM-003 BM-006 BM-010 BM-011 BM-015 BM-018 BM-021 \
  --trials 5 --timeout 600

# Run selected benchmark set only
python3 scripts/run_phase1_calibration.py \
  --candidate BM-003 BM-006 BM-010 BM-011 BM-018 BM-021 \
  --trials 5 --timeout 600

# Dry run (no copilot invocation, synthetic results)
python3 scripts/run_phase1_calibration.py --dry-run --trials 2
```
