# agent-development

A personal research and experimentation repo for building, running, and improving AI agents and agentic workflows.

## Purpose

This repo is a living lab for:

- Designing multi-agent workflow architectures
- Iterating on agent prompts and evaluation loops
- Wiring together AI backends (Claude Code, GitHub Copilot) with real tooling (Azure DevOps, Discord)
- Testing headless and CI-driven workflow execution

Work here feeds back into production agent configurations stored in `.claude/`.

## Repo structure

```
agent-runner/       Core workflow runner and headless launcher
  run.py              Interactive workflow controller (stage ordering, retries, evaluator loops)
  run_headless.py     Non-interactive launcher used by Discord trigger and CI
  test_runner.py      Unit tests for run.py
  test_run_headless.py Unit tests for run_headless.py

agent-infra/        Infrastructure for external triggers
  discord_trigger_listener.py   Listens for Discord messages and fires workflow runs
  discord_escalation_bridge.py  Routes agent escalations back to Discord

.claude/            Agent prompts, skills, hooks, and scripts
  agents/             Stage agent prompt files (intake, task-generator, QA, etc.)
  skills/             Reusable skill definitions
  hooks/              Claude Code lifecycle hooks
  scripts/            CI and validation scripts

3-27-2026/          Dated experiment snapshot
4-6-2026/           Dated experiment snapshot

docs/               Research notes and design documents
```

## Workflow overview

The core workflow is a multi-stage pipeline driven by `agent-runner/run.py`:

1. **intake** — normalizes the work item into structured artifacts
2. **task-generator** → **task-plan-evaluator** loop
3. **task-assigner** → **assignment-evaluator** loop
4. **software-engineer-hyperagent** → **implementation-evaluator** loop
5. **qa-engineer** → **qa-evaluator** loop
6. **lessons-optimizer-hyperagent** — captures learnings for future runs

Each stage runs an AI agent (Claude Code or GitHub Copilot) against a prompt file in `.claude/agents/`. Evaluator stages loop until the artifact passes or the retry limit is hit.

## Running workflows

**Interactive (local dev):**
```bash
python3 agent-runner/run.py
```
Prompts for backend, change ID or Azure DevOps work item URL, and starts the pipeline.

**Headless (CI / Discord trigger):**
```bash
python3 agent-runner/run_headless.py \
  --change-id WI-4461550 \
  --output-json /tmp/summary.json
```

By default the headless launcher creates an isolated Git worktree for every run so parallel invocations never collide. Use `--cleanup-worktree` to remove it on exit, or `--no-worktree` to skip worktree creation entirely.

## Running tests

```bash
python3 -m unittest agent-runner/test_runner.py -v
python3 -m unittest agent-runner/test_run_headless.py -v
```

## Requirements

- Python 3.9+
- One AI CLI: `claude` (Claude Code) or `copilot` (GitHub Copilot)
- For Azure DevOps intake: `az` + `azure-devops` extension + valid auth
- For Discord triggers: a configured bot token in environment
