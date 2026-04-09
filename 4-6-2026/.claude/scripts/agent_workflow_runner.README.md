# Agent Workflow Runner

`agent_workflow_runner.py` is the workflow controller for the local agents in `.claude/agents`.

## What it orchestrates

The runner executes these stages in order:

1. `01-intake`
2. `02-task-generator` + `06-task-plan-evaluator`
3. `03-task-assigner` + `07-assignment-evaluator`
4. `04-software-engineer-hyperagent` + `08-implementation-evaluator`
5. `05-qa` + `09-qa-evaluator`
6. `11-lessons-optimizer-hyperagent`

The runner owns:

- stage ordering and retries
- evaluator feedback loops
- workflow-level logging under `agent-context/<CHANGE-ID>/logs/workflow_runner/`
- dry-run artifact synthesis

The intake agent is now a **stage-local normalizer** only. It creates `intake/*` artifacts but does not orchestrate later stages.

## Inputs

- `--repo-root`: code repository the stage agents may read and modify
- `--artifact-root`: optional artifact directory, defaulting to `<repo-root>/agent-context`
- `--change-id`: workflow run identifier
- `--context` or `--context-file`: workflow context consumed by the intake stage

The runner discovers agent prompts and helper scripts relative to its own `.claude` installation, not relative to `--repo-root`.

## Dry-run example

```bash
python3 /path/to/.claude/scripts/agent_workflow_runner.py \
  --repo-root /path/to/code-repo \
  --artifact-root /tmp/agent-runner-test \
  --change-id WI-DRY-RUN \
  --context 'Dry-run validation context.' \
  --dry-run \
  --json
```

## Real run example

```bash
python3 /path/to/.claude/scripts/agent_workflow_runner.py \
  --repo-root /path/to/code-repo \
  --change-id WI-12345 \
  --context-file /absolute/path/to/workflow-context.md \
  --json
```

## Notes

- The implementation is standard-library only.
- Use `--add-dir <path>` to grant additional directories to the Copilot CLI during real runs.
- In automation prompts, the runner supplies `workflow_assets_root`, `code_repo`, `artifact_root`, and `change_id` so stage agents can resolve shared helper scripts and prompt files correctly.
