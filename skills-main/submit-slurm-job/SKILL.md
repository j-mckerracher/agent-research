---
name: submit-slurm-job
description: Submit and monitor a SLURM job on Gilbreth for the FRESCO pipeline. Use when submitting any production or experiment job, or when diagnosing a stuck/failed job.
---

## Dynamic context to inject

Use Claude Code's `!` pre-execution syntax so this skill receives the current job state before deciding whether to submit immediately or wait.

```text
!`ssh jmckerra@gilbreth.rcac.purdue.edu squeue -u jmckerra -o "%.18i %.9P %.30j %.8T %.10M" --noheader`
```

Treat the injected job list as the source of truth for pending and running work.

## Required SLURM header (production build)

```bash
#SBATCH --job-name=fresco_v3_production
#SBATCH --output=logs/production_v3_%j.out
#SBATCH --error=logs/production_v3_%j.err
#SBATCH --time=12:00:00
#SBATCH --mem=128G
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --partition=a100-80gb        # ONLY valid partition for sbagchi account
#SBATCH --mail-type=ALL
#SBATCH --mail-user=mckerracher@gmail.com
```

> ⚠️ **Never use `--partition=a10`, `a30`, `a100-40gb`, or `h100`** — the `sbagchi` account has zero allocation on those. The job will queue forever with `AssocGrpGRES`. See `gilbreth-preflight` skill.

## Submission

Use `sbbest` (auto-selects least-busy compatible node) when partition flexibility is acceptable:

```bash
cd /home/jmckerra/Code/FRESCO-Pipeline
sbbest production_v3.slurm
```

Or submit explicitly to `a100-80gb`:

```bash
sbatch --partition=a100-80gb --account=sbagchi production_v3.slurm
```

For short non-production jobs when `a100-80gb` is blocked by group quota, a verified fallback is:

```bash
sbatch --partition=training --qos=training --account=sbagchi --gres=gpu:1 <job>
```

Use `training` only for development / validation / experiment runs, not for production dataset builds.

## Monitoring commands

```bash
squeue -u $USER                         # your jobs and their state
squeue -u $USER --start                 # estimated start times
squeue -A sbagchi                       # all group jobs (see who's using GPU quota)
slist                                   # group GPU allocation summary

sacct -j <JOBID> --format=JobID,JobName,State,Elapsed,Timelimit,Start,End,ExitCode -X
scontrol show job <JOBID>               # full job details including reason for pending

scancel <JOBID>                         # cancel a job
```

## Runbook constraint

After submission, **wait 10 minutes**, then **only proceed if the job is RUNNING or COMPLETED**. If still PENDING, stop and wait for user signal before taking further action.

## Common failure: AssocGrpGRES

If a job shows `Reason=AssocGrpGRES`:
1. Check `scontrol show job <JOBID>` → look at `ReqTRES` to see what GPU type was requested.
2. Check `slist` to see the group's current GPU usage.
3. If the wrong GPU type was requested, cancel the job, fix the `--partition` and `--gres` in the script, and resubmit.
4. If the right GPU type was requested but quota is full, wait for a running group job to finish.
