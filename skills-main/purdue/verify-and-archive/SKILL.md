---
name: verify-and-archive
description: Spot-check and archive a completed FRESCO production run. Use after the pipeline finishes and validation passes to confirm output integrity and copy artifacts to the depot archive.
---

After the pipeline completes and validation artifacts are written, perform a final spot-check and archive the run.

## Post-run verification

Randomly sample **N=100 output shards** across years and clusters and verify basic invariants:

```bash
# Run the dedicated verification job
sbatch --partition=a100-80gb --account=sbagchi verify_v3.slurm

# Or run interactively (small sample):
cd /home/jmckerra/Code/FRESCO-Pipeline
python verify_v3_output.py
```

`verify_v3_output.py` checks each sampled shard for:
- Schema conformance (required columns present)
- Dtype stability (no object/mixed columns)
- `cluster` column valid (`{anvil, conte, stampede}`)
- No all-null required columns
- Timestamp columns parseable

A failed verification is a **stop condition** — do not proceed to archival or paper claims.

## Archival

Copy the full run artifact bundle to a persistent depot location:

```bash
RUN_ID="PROD-20260203-v3"
ARCHIVE=/depot/sbagchi/data/josh/FRESCO-Research/runs/$RUN_ID

mkdir -p $ARCHIVE
cp -r /depot/sbagchi/data/josh/FRESCO/chunks-v3/manifests  $ARCHIVE/
cp -r /depot/sbagchi/data/josh/FRESCO/chunks-v3/validation $ARCHIVE/
cp -r /depot/sbagchi/data/josh/FRESCO/chunks-v3/logs       $ARCHIVE/
cp production_v3.json                                       $ARCHIVE/config/
```

## Required records for any paper citation

The following must all exist and be retrievable by run ID:

- [ ] `run_metadata.json` (git commit, config, env)
- [ ] `input_manifest.jsonl`
- [ ] `output_manifest.jsonl`
- [ ] `validation/` artifacts (schema, dtype, missingness, sanity)
- [ ] `pip_freeze.txt` / `conda_env.yml`
- [ ] SLURM job logs (`.out` / `.err`)

See `runbooks/REPRODUCIBILITY_CHECKLIST.md` for the full publication checklist.
