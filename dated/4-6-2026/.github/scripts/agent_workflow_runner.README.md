# Agent Workflow Runner

`agent_workflow_runner.py` executes the local custom Copilot agents defined in `.github/agents` using the repository's documented workflow order.

## What it does

The runner maps the scratch workflow into these stages:

1. `01-orchestrator`
2. `02-task-generator` + `06-task-plan-evaluator`
3. `03-task-assigner` + `07-assignment-evaluator`
4. `04-software-engineer-hyperagent` + `08-implementation-evaluator`
5. `05-qa` + `09-qa-evaluator`
6. `11-lessons-optimizer-hyperagent`

It writes artifacts under `agent-context/<CHANGE-ID>/` and stores orchestration logs in `agent-context/<CHANGE-ID>/logs/orchestrator/`.

## Dry-run example

Use dry-run mode to validate the control flow without invoking live model calls:

```bash
python3 .github/scripts/agent_workflow_runner.py \
  --repo-root /Users/mckerracher.joshua/Code/mcs-products-mono-ui \
  --artifact-root /tmp/mcs-agent-runner-test \
  --change-id WI-DRY-RUN \
  --context 'Dry-run validation context.' \
  --dry-run \
  --json
```

## Real run example

```bash
python3 .github/scripts/agent_workflow_runner.py \
  --repo-root /Users/mckerracher.joshua/Code/mcs-products-mono-ui \
  --change-id WI-12345 \
  --context-file /absolute/path/to/workflow-context.md \
  --json
```

## Scratch wrapper

The JetBrains scratch wrapper delegates directly to the same module:

- `/Users/mckerracher.joshua/Library/Application Support/JetBrains/WebStorm2025.3/scratches/scratch_1.py`

## Notes

- The implementation is standard-library only; no dependency manifest changes are required.
- If you want the runner to access additional paths during a real run, repeat `--add-dir <path>`.
- In dry-run mode the runner synthesizes representative workflow artifacts so loops and file paths can be tested locally.
