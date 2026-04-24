---
name: write-experiment-log
description: Create and fill out a run log for a FRESCO experiment or production run. Use when starting or completing any experiment to ensure all required sections are recorded correctly.
---

## Dynamic context to inject

Use Claude Code's `!` pre-execution syntax so the log template starts with a live commit hash and UTC timestamp.

```text
!`git rev-parse HEAD`
!`date -u +"%Y-%m-%dT%H:%M:%SZ"`
```

Use the injected values to fill the log header fields instead of retyping them.

Every experiment or production run must have a run log. Use this skill to create and fill one out correctly.

## When to create a log

Create the log file **before** the job starts (fill what you know) and complete it **immediately after** results are available.

## File location and naming

```
experiments/<EXP-XXX_description>/<EXP-XXX>_RUN_LOG.md
```

For production runs:
```
runs/<PROD-YYYYMMDD-tag>/<PROD-YYYYMMDD-tag>_RUN_LOG.md
```

## Required sections

```markdown
# <EXP-XXX or PROD-YYYYMMDD-tag> Run Log — <one-line description>

**Run ID**: <EXP-XXX_full_name or PROD-YYYYMMDD-tag>
**Date**: YYYY-MM-DD

## Objective
One sentence: what question does this run answer or what output does it produce?

## Hypothesis (if experiment)
What outcome do you expect, and why?

## Inputs
- Dataset label: `<local_job_partials_snapshot | chunks-v3 | ...>`
- Input manifest: `<path to input_files.json>`
- Clusters: anvil / conte / stampede (circle which apply)
- Date range: YYYY-MM-DD to YYYY-MM-DD

## Code & Environment
- Script: `scripts/<script_name>.py`
- Config: `experiments/<EXP-XXX>/config/<config_file>.json`
- Git commit (pipeline): <full SHA>
- Git commit (analysis): <full SHA, if different>
- Conda env: fresco_v2 (or note if different)
- Python: <version>
- Package lock: `experiments/<EXP-XXX>/validation/pip_freeze.txt`

## Execution
- Cluster: Gilbreth
- Submission command: `sbatch --partition=a100-80gb --account=sbagchi <script>.slurm`
- Job IDs: <SLURM job IDs>
- Start / end time (UTC): YYYY-MM-DDTHH:MM:SSZ / YYYY-MM-DDTHH:MM:SSZ

## Outputs
- Output root: `experiments/<EXP-XXX>/results/`
- Manifests: `experiments/<EXP-XXX>/manifests/`
- Validation reports: `experiments/<EXP-XXX>/validation/`

## Results Summary
Key numbers from `results/*.json` — metrics, overlap coverage, AUC, R², etc.
Always cite the exact artifact path that supports each claim.

## Validation Summary
Did validation pass? Any Level 0/1 failures?

## Known Issues / Caveats
List any proxy-only limitations, missing features, or caveats that affect interpretation.

## Repro Steps
1. `<exact command to reproduce from scratch>`
2. `<any required setup steps>`
```

## Rules

- **Never leave Results Summary blank** after a completed run — if metrics weren't computed, state why explicitly (e.g., "Zero overlap → modeling skipped").
- **Every claim must cite an artifact path** — do not state a number without pointing to the file it came from.
- **Proxy-only caveat is mandatory** for any run on `local_job_partials_snapshot` data rather than the authoritative `/depot/.../chunks-v3/` shards.
- Do not mark a run as "complete" without filling Known Issues / Caveats.
