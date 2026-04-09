# Agent Workflow Runner

`run.py` is the workflow controller for the local agents in `.claude/agents`.

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

## Startup flow

Run the script with **no arguments**:

```bash
python3 agent-runner/run.py
```

The runner now launches interactively and will:

1. detect the repo root and artifact root
2. ask which AI backend to use (**GitHub Copilot** or **Claude Code**)
3. ask how to start the workflow:
   - **Azure DevOps work item** (recommended)
   - **resume existing intake artifacts**
   - **paste workflow context manually**
4. fetch workflow context from Azure DevOps when given only a work item id or URL
5. skip the intake stage when reusable intake artifacts already exist for that change

## Azure DevOps mode

When you choose Azure DevOps startup, the runner accepts either:

- a bare work item id like `4461550`
- a `WI-4461550` identifier
- a full work item URL like `https://dev.azure.com/{org}/{project}/_workitems/edit/{id}`

The runner builds the intake context automatically from the work item title,
description, acceptance criteria, and related metadata. No manual context file is required.

## Requirements

- one supported AI CLI installed locally:
  - **GitHub Copilot** (`copilot`)
  - **Claude Code** (`claude`)
- for Azure DevOps startup:
  - `az`
  - `azure-devops` CLI extension
  - valid Azure DevOps authentication (for example `az devops login` or `AZURE_DEVOPS_EXT_PAT`)

## Notes

- The implementation is standard-library only.
- The runner discovers agent prompts and helper scripts relative to its own `.claude` installation.
- In automation prompts, the runner supplies `workflow_assets_root`, `code_repo`, `artifact_root`, and `change_id` so stage agents can resolve shared helper scripts and prompt files correctly.
- Direct CLI arguments are no longer supported; startup happens through interactive prompts.
