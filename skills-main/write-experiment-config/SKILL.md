---
name: write-experiment-config
description: Write a correctly structured experiment or production config JSON file for the FRESCO pipeline. Use when creating a new experiment or production run to ensure all required fields are present.
---

Every run must be fully parameterized by a single config file stored with its artifacts.

## File location

```
experiments/<EXP-XXX_description>/config/<exp_id>_<description>.json
```

For production:
```
config/production_v3.json        (local repo)
/home/jmckerra/Code/FRESCO-Pipeline/production_v3.json   (on Gilbreth)
```

## Required fields (all runs)

```json
{
  "run_id": "EXP-XXX_description | PROD-YYYYMMDD-tag",
  "dataset_version": "v3.0",
  "input_root": "/depot/sbagchi/data/josh/FRESCO/chunks",
  "output_root": "/depot/sbagchi/data/josh/FRESCO/chunks-v3",
  "clusters": ["anvil", "conte", "stampede"],
  "date_range": {
    "start": "YYYY-MM-DD",
    "end": "YYYY-MM-DD"
  },
  "random_seed": 42,
  "write_manifests": true,
  "write_validation_reports": true,
  "validation_level": "strict"
}
```

## Additional fields (experiment-specific)

### Phase 1 — Feature matrix

```json
{
  "n_sample_files_per_cluster": 20,
  "missingness_threshold": 0.0
}
```

### Phase 2 — Regime matching

```json
{
  "source_cluster": "anvil",
  "target_cluster": "conte",
  "regime_definition": "cpu_standard",
  "feature_set": ["ncores", "nhosts", "timelimit_sec", "runtime_sec"],
  "overlap_band": [0.2, 0.8],
  "domain_classifier": "LogisticRegression"
}
```

### Phase 3 — Transfer modeling

```json
{
  "model": "Ridge",
  "model_params": {"alpha": 1.0},
  "label_col": "value_memused_max",
  "label_transform": "log1p",
  "adaptation": "none",
  "source_cohort_path": "experiments/<EXP-XXX-regime>/results/matched_source_indices.parquet",
  "target_cohort_path": "experiments/<EXP-XXX-regime>/results/matched_target_indices.parquet"
}
```

## Run naming conventions

| Run type | Format | Example |
|---|---|---|
| Production | `PROD-YYYYMMDD-<tag>` | `PROD-20260203-v3` |
| Experiment | `EXP-XXX` (sequential) | `EXP-039` |

The config `run_id` must match the experiment folder name exactly.

## Rules

- **Config must be committed** (or saved as an artifact) before the run starts.
- **Do not mutate a config** after the run has started — create a new EXP-XXX instead.
- The config file stored in `experiments/<EXP-XXX>/config/` is the authoritative record; the one used at runtime must be identical.
