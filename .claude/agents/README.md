# `.claude` workflow agents

This directory contains the stage prompts used by `../../agent-runner/run.py`.

## Architecture

The workflow runner is the **only orchestrator**. Agents are stage-local specialists:

1. `01-intake.agent.md` normalizes workflow context into `intake/*`
2. `02-task-generator.agent.md` creates the task plan
3. `06-task-plan-evaluator.agent.md` evaluates the task plan
4. `03-task-assigner.agent.md` creates execution assignments
5. `07-assignment-evaluator.agent.md` evaluates assignments
6. `04-software-engineer-hyperagent.agent.md` implements each UoW
7. `08-implementation-evaluator.agent.md` evaluates each implementation attempt
8. `05-qa.agent.md` validates the completed work
9. `09-qa-evaluator.agent.md` evaluates the QA report
10. `11-lessons-optimizer-hyperagent.agent.md` extracts lessons and proposes prompt improvements

Support agents:

- `00-reference-librarian.agent.md`
- `10-information-explorer.agent.md`

## Workflow order

```text
intake
  -> task_generator
  -> task_plan_evaluator loop
  -> task_assigner
  -> assignment_evaluator loop
  -> software_engineer / implementation_evaluator loop
  -> qa / qa_evaluator loop
  -> lessons_optimizer
```

## Quick start

Run the workflow through the runner, not by invoking an orchestrator agent:

```bash
python3 agent-runner/run.py
```

The runner now starts interactively. It will:

- choose **GitHub Copilot** or **Claude Code**
- accept a bare Azure DevOps work item id or full work item URL
- fetch context directly from Azure DevOps when possible
- reuse existing intake artifacts when resuming a prior change
- fall back to pasted workflow context when automation cannot supply it

## Artifact contract

Artifacts are written under `{artifact_root}/{CHANGE-ID}/`:

```text
{CHANGE-ID}/
├── intake/
│   ├── story.yaml
│   ├── config.yaml
│   └── constraints.md
├── planning/
│   ├── tasks.yaml
│   ├── assignments.json
│   ├── eval_tasks_k.json
│   └── eval_assignments_k.json
├── execution/
│   └── {UOW-ID}/
│       ├── uow_spec.yaml
│       ├── impl_report.yaml
│       ├── eval_impl_k.json
│       └── logs/
├── qa/
│   ├── qa_report.yaml
│   ├── eval_qa_k.json
│   └── evidence/
├── summary/
│   └── lessons_optimizer_report.yaml
└── logs/
    ├── workflow_runner/
    ├── intake/
    ├── reference_librarian/
    ├── task_generator/
    ├── assignment/
    ├── software_engineer/
    ├── qa/
    ├── information_explorer/
    └── lessons_optimizer/
```

## Communication model

- The workflow runner dispatches every stage.
- Agents do **not** orchestrate one another.
- The Reference Librarian remains the mandatory gateway for knowledge queries.
- The Information Explorer is only invoked by the Reference Librarian.
- Evaluators assess artifacts and feed actionable fixes back through the runner-owned retry loop.

## Prompt conventions

- `workflow_assets_root` is the `.claude` installation root and points to the sibling `agents/` and `scripts/` directories.
- `code_repo` is the repository being changed.
- `artifact_root` is the base path for per-run workflow artifacts.
- `change_id` identifies the run.

## Shared skills

The prompts rely on shared skills in `../skills/`, especially:

- `artifact-io`
- `execution-discipline`
- `librarian-query-protocol`
- `lessons-capture`
- `scope-and-security`
- `session-logging`

Evaluators also rely on `evaluator-framework`.
