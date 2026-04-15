---
name: reproducibility-checklist
description: Publication readiness checklist covering code, environment, data provenance, determinism, and validation. Use before declaring any FRESCO run publication-ready or submitting results to a venue.
---

Run through this checklist before declaring any run publication-ready. All items in A–E must pass. F–G must be completed before submitting to a venue.

## A. Code

- [ ] Pipeline code pinned to a specific commit hash (`validation/git_commit.txt`)
- [ ] Analysis code pinned to a specific commit hash (if separate)
- [ ] No uncommitted diffs, OR diffs saved as `validation/git_diff.patch`

## B. Environment

- [ ] Python version recorded (`validation/python_version.txt`)
- [ ] OS and hostname recorded (`validation/host_info.txt`)
- [ ] `pip freeze` saved (`validation/pip_freeze.txt`)
- [ ] `conda env export` saved (`validation/conda_env.yml`) if using conda

## C. Data provenance

- [ ] Input root path recorded in `manifests/run_metadata.json`
- [ ] Date ranges recorded
- [ ] List of input shards processed (`manifests/input_manifest.jsonl`)
- [ ] List of output shards produced (`manifests/output_manifest.jsonl`)

## D. Determinism

- [ ] All random seeds fixed and recorded in config (e.g., `"random_seed": 42`)
- [ ] Sampling procedure documented (e.g., how the N=20 files per cluster were chosen in Phase 1)
- [ ] Any nondeterministic steps identified and noted in the run log's Known Issues section
- [ ] If using sklearn or numpy randomness, confirm `random_state` is set in the config

## E. Validation

- [ ] Schema report saved (`validation/schema_report.json`)
- [ ] Dtype report saved (`validation/dtype_report.json`)
- [ ] Missingness report saved (`validation/missingness_report.json`)
- [ ] Sanity checks saved (`validation/sanity_checks.json`)
- [ ] Validation Level 0 and Level 1 passed (see `validate-outputs` skill)

## F. Paper artifacts

- [ ] Methods text references exact config files and commit hashes
- [ ] Every numerical claim in the paper ties to a specific experiment run ID and artifact path
- [ ] Threats-to-validity documented (`paper/THREATS_TO_VALIDITY.md`)
- [ ] No claim is made from proxy-only data without explicitly labeling it "proxy-only"

## G. Reproduction instructions

- [ ] Step-by-step reproduction guide written (single command preferred)
- [ ] Expected outputs described (key metrics, file sizes, or checksums)
- [ ] Reproduction tested from a clean environment if possible

## How to use

Copy this checklist into the run log (`Known Issues / Caveats` or a dedicated `## Checklist` section) and check off each item. A run is publication-ready only when A–E are all checked.
