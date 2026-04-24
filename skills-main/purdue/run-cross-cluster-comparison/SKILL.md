---
name: run-cross-cluster-comparison
description: Perform regime matching and overlap analysis for cross-cluster transfer comparisons. Use when making or evaluating any cross-cluster transfer claim, or when setting up a Phase 2/3 experiment.
---

Cross-cluster claims are only defensible when restricted to overlapping workload regimes. Follow this procedure exactly.

## Non-negotiable rule

**No global cross-cluster claims without regime matching + overlap reporting.**  
If you skip this and make a claim like "transfer from Anvil to Conte works," it will be rejected in review.

## Step 1: Define the workload regime

Assign each job a `workload_regime` label using the shared taxonomy:

| Label | Criteria |
|---|---|
| `cpu_standard` | `gpu_count_per_node == 0` AND `node_memory_gb < threshold` |
| `cpu_largemem` | `gpu_count_per_node == 0` AND `node_memory_gb >= threshold` |
| `gpu_standard` | `gpu_count_per_node > 0` AND `node_memory_gb < threshold` |
| `gpu_largemem` | `gpu_count_per_node > 0` AND `node_memory_gb >= threshold` |
| `unknown` | metadata missing |

> ⚠️ **Proxy caveat**: When running on local snapshots (not full `/depot/.../chunks-v3/`), partition/node_type metadata may be missing. Use the conservative proxy:
> `cpu_standard := (value_gpu_cnt <= 0) AND (value_gpu_sum <= 0)`
> Mark all results as "proxy-only" in the run log.

## Step 2: Choose a feature set

Use only "safe" features — those with 0% missingness across all clusters (from Phase 1 / EXP-016):

```
ncores, nhosts, timelimit_sec, runtime_sec, queue_time_sec, runtime_fraction
```

Do not include `value_memused_max`, `value_memused_minus_diskcache_max`, `value_gpu_max` — these have non-zero missingness in at least one cluster.

## Step 3: Compute overlap

Train a domain classifier (logistic regression) to predict source vs target from the feature set:

```python
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)
clf = LogisticRegression(random_state=config["random_seed"])
clf.fit(X_scaled, y_domain)  # y_domain: 0=source, 1=target

propensity = clf.predict_proba(X_scaled)[:, 1]  # P(target | x)
```

Define the overlap region using the propensity band (default `[0.2, 0.8]`):

```python
lo, hi = config["overlap_band"]   # e.g., [0.2, 0.8]
in_overlap = (propensity >= lo) & (propensity <= hi)
```

## Step 4: Evaluate the overlap

Every run must report:

| Metric | How to compute |
|---|---|
| Domain classifier AUC | `roc_auc_score(y_domain, propensity)` |
| Target overlap coverage | `n_target_in_overlap / n_target_total` |
| KS statistics | `scipy.stats.ks_2samp` per feature |

**Interpret AUC:**
- AUC ≈ 1.0 → domains are nearly perfectly separable → very little overlap → transfer claim is not defensible globally
- AUC ≈ 0.5 → domains are indistinguishable → strong overlap → transfer may generalize

## Step 5: Restrict evaluation to overlap

Only evaluate transfer models on jobs in the overlap region. Report separately:
- Primary: within-overlap performance
- Secondary: full-distribution performance (clearly labeled as outside overlap)

## Step 6: Required reporting in the run log and paper

```
- Regime definition: cpu_standard (proxy | authoritative)
- Feature set: [list of features]
- Overlap band: [0.2, 0.8]
- Domain classifier AUC: <value>
- Target overlap coverage: <value> (n=<count> of <total>)
- KS statistics: [per-feature table]
```

## Reference runs

| EXP | Source→Target | Feature set | Band | AUC | Coverage |
|---|---|---|---|---|---|
| EXP-022 | Anvil→Conte | alloc-only | [0.2,0.8] | 0.80 | 1.0 |
| EXP-031 | Anvil→Conte | alloc+perf (no mem) | [0.2,0.8] | 0.99 | 0.32 |
| EXP-024 | Anvil→Conte | alloc+perf (no mem) | [0.1,0.9] | 0.99 | 0.42 |
