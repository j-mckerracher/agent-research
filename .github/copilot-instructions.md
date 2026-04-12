# Copilot Instructions for agent-development

## Repo purpose

This repository is a research lab for multi-agent workflow architectures, prompt iteration, and integrations around GitHub Copilot, Claude Code, Azure DevOps, and Discord.

## Where to make changes

- `agent-runner/run.py`: interactive workflow orchestration, stage ordering, retry loops, backend selection, Azure DevOps intake flow
- `agent-runner/run_headless.py`: non-interactive runner for CI and Discord-triggered runs, worktree lifecycle, JSON summary output
- `.claude/agents/*.agent.md`: live stage prompts used by the current workflow runner
- `agent-infra/`: Discord listeners, escalation bridge, trigger API, and infra-side tests
- `dated/*/`: historical experiment snapshots kept for reproducibility; do not update snapshots unless the task explicitly targets one

## Workflow model

The current runner orchestrates this stage order:

1. `01-intake`
2. `02-task-generator` + `06-task-plan-evaluator`
3. `03-task-assigner` + `07-assignment-evaluator`
4. `04-software-engineer-hyperagent` + `08-implementation-evaluator`
5. `05-qa` + `09-qa-evaluator`
6. `11-lessons-optimizer-hyperagent`

The runner is the only orchestrator. Stage agents are stage-local specialists, and evaluator stages loop until pass or retry limits are hit.

## Running and testing

- Interactive runner: `python3 agent-runner/run.py`
- Headless runner: `python3 agent-runner/run_headless.py --change-id WI-4461550 --output-json /tmp/summary.json`
- Runner tests:
  - `python3 -m unittest agent-runner/test_runner.py -v`
  - `python3 -m unittest agent-runner/test_run_headless.py -v`
- Infra tests: `pytest agent-infra/tests`

Key runtime expectations:

- Python 3.9+
- One AI CLI installed locally: `copilot` or `claude`
- For Azure DevOps-driven startup: `az`, the `azure-devops` CLI extension, and valid auth

There is no dedicated lint step in this repository today.

## Practical guidance

- `run.py` starts interactively; use `run_headless.py` for scripted or CI execution.
- `run_headless.py` creates an isolated Git worktree by default so concurrent runs do not collide.
- When changing workflow order, agent discovery, retry logic, or backend invocation, update the `agent-runner` tests in the same change.
- When changing headless execution or Git worktree behavior, update `agent-runner/test_run_headless.py`.
- Keep the numbered `.claude/agents/*.agent.md` filenames stable unless you are intentionally changing stage structure and tests.
- Prefer updating the live prompts in `.claude/agents/` instead of editing dated snapshots.
- Preserve the artifact-driven design: each stage writes artifacts that later stages and evaluators consume.
