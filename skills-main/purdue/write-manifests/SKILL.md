---
name: write-manifests
description: Write run manifests (input_manifest.jsonl, output_manifest.jsonl, run_metadata.json) for a FRESCO production run or experiment. Use when a run completes or to verify that traceability records are present.
---

Every production run must produce three manifest files. These are the traceability record required for publication.

## Required files

### `manifests/input_manifest.jsonl`

One JSON line per input shard processed:

```jsonl
{"path": "/depot/.../chunks/2021/01/01/00.parquet", "sha256": "abc123...", "cluster": "anvil", "rows": 4821}
{"path": "/depot/.../chunks/2021/01/01/01.parquet", "sha256": "def456...", "cluster": "conte", "rows": 3104}
```

### `manifests/output_manifest.jsonl`

One JSON line per output shard written:

```jsonl
{"path": "/depot/.../chunks-v3/2021/01/01/00.parquet", "sha256": "ghi789...", "rows": 7925, "clusters": ["anvil", "conte"]}
```

### `manifests/run_metadata.json`

Single JSON file capturing the full run context:

```json
{
  "run_id": "PROD-20260203-v3",
  "pipeline_git_commit": "<full SHA>",
  "pipeline_git_dirty": false,
  "config_path": "production_v3.json",
  "input_root": "/depot/sbagchi/data/josh/FRESCO/chunks",
  "output_root": "/depot/sbagchi/data/josh/FRESCO/chunks-v3",
  "clusters": ["anvil", "conte", "stampede"],
  "date_range": {"start": "2015-01-01", "end": "2023-12-31"},
  "python_version": "3.10.x",
  "conda_env": "fresco_v2",
  "slurm_job_id": "10387798",
  "host": "gilbreth-k001.rcac.purdue.edu",
  "started_at": "2026-03-08T00:00:00Z",
  "completed_at": "2026-03-08T08:00:00Z"
}
```

## Where they live

- On the cluster: `/depot/sbagchi/data/josh/FRESCO/chunks-v3/manifests/`
- Archived copy: `/depot/.../FRESCO-Research/runs/<RUN_ID>/manifests/`

## Why

Without manifests, you cannot verify which input shards contributed to a given output, and the run cannot be cited in the paper. The reproducibility checklist (`runbooks/REPRODUCIBILITY_CHECKLIST.md`) will not pass without all three files.
