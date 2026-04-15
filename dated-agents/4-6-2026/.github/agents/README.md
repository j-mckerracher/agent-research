# Workflow type: Sequential Task Decomposition with Evaluator-Optimizer Loop

### A multi-agent workflow for implementing user stories from acceptance criteria through implementation, testing, and QA.

## Pre-requisites

1. GitHub Copilot with access to all models.
2. GitHub Copilot CLI installed and authenticated. See [GitHub Docs](https://github.com/features/copilot/cli) for setup instructions.
3. Azure DevOps CLI installed and authenticated. See [Microsoft Docs](https://learn.microsoft.com/en-us/azure/devops/cli/?view=azure-devops) for setup instructions.

## Overview

This workflow uses specialized AI agents to:

1. Decompose a user story into tasks and units of work
2. Schedule and assign implementation work with dependency awareness
3. Implement changes with scope control and documentation-first discipline
4. Validate acceptance criteria with evidence-based QA
5. Extract lessons and generate prevention rules for continuous improvement

Each stage includes an **Evaluator-Optimizer loop** that iteratively refines outputs until quality gates pass.

## Quick Start

### 1. Start the Orchestrator

Invoke the Orchestrator agent. It will immediately provide you with a copiable YAML template to fill out.

### 2. Fill Out the Configuration

The Orchestrator will give you this template:

```yaml
workflow:
  change_id: '' # Required: Unique ID (e.g., "4729040")
  code_repo: '' # Required: Path to your code repository
  project_type: '' # Required: "brownfield" or "greenfield"
  planning_docs_root: '' # Greenfield only: path to PRD/plan docs
  planning_docs_paths: [] # Greenfield only: explicit doc paths

story:
  title: '' # Required: Brief title
  description: '' # Required: User story statement
  acceptance_criteria_raw: | # Required: List your ACs
    AC - ...
    AC - ...

models: # Optional - defaults shown
  task-generator: 'claude-opus-4.6'
  task-assigner: 'claude-opus-4.6'
  software-engineer: 'claude-opus-4.6'
  qa-engineer: 'claude-opus-4.6'
  ui_qa: 'claude-opus-4.6'
  reference-librarian: 'gpt-5.2 high-reasoning'
  evaluators: 'claude-opus-4.6'
  information-explorer: 'claude-opus-4.6'
  lessons-optimizer: 'gpt-5.3-codex extra-high-reasoning'

iteration_limits: # Optional
  task_plan: 3
  assignment: 2
  implementation: 3
  qa: 2

options:
  parallel_uows: true
  auto_escalate: true
  preserve_attempt_artifacts: true
```

### 3. Paste Your Completed YAML

Fill in your story details and paste it back. The Orchestrator will:

1. Validate your configuration
2. Normalize acceptance criteria to `AC1`, `AC2`, etc.
3. Create intake artifacts
4. Begin the workflow

### 4. Monitor Progress

Artifacts are written to:

```
{{artifact_root}}{CHANGE-ID}/
```

Check `intake/config.yaml` for current status and stage.

---

## Agent Hierarchy

The Orchestrator controls the workflow and delegates to specialized agents. **All agents must query the Reference Librarian first** before accessing any knowledge or doing codebase exploration.

```
                                 ┌─────────────────────┐
                                 │    ORCHESTRATOR     │
                                 │  (State Machine)    │
                                 └──────────┬──────────┘
                                            │
            ┌───────────────────────────────┼───────────────────────────────┐
            │                               │                               │
            ▼                               ▼                               ▼
    ┌───────────────┐              ┌───────────────┐              ┌───────────────┐
    │   PLANNING    │              │  EXECUTION    │              │  VALIDATION   │
    └───────┬───────┘              └───────┬───────┘              └───────┬───────┘
            │                               │                               │
    ┌───────┴───────┐                       │                       ┌───────┴───────┐
    │               │                       │                       │               │
    ▼               ▼                       ▼                       ▼               ▼
┌────────┐   ┌───────────┐           ┌──────────┐            ┌──────────┐   ┌───────────┐
│  Task  │   │   Task    │           │ Software │            │    QA    │   │  Lessons  │
│Generat.│   │  Assigner │           │ Engineer │            │ Engineer │   │ Optimizer │
└───┬────┘   └─────┬─────┘           └────┬─────┘            └────┬─────┘   └─────┬─────┘
    │              │                      │                       │               │
    ▼              ▼                      ▼                       ▼               ▼
┌────────┐   ┌──────────┐           ┌──────────┐            ┌──────────┐   ┌──────────┐
│TaskPlan│   │Assignment│           │  Impl    │            │    QA    │   │  Report  │
│  Eval  │   │   Eval   │           │   Eval   │            │   Eval   │   │  Output  │
└────────┘   └──────────┘           └──────────┘            └──────────┘   └──────────┘

         ┌──────────────────────────────────────────────────────────────────────┐
         │                      REFERENCE LIBRARIAN                             │
         │    (Mandatory first point of contact for ALL knowledge queries)      │
         │         ┌──────────────────────────────────┐                         │
         │         │     INFORMATION EXPLORER          │                        │
         │         │  (Invoked only by Librarian)      │                        │
         │         └──────────────────────────────────┘                         │
         │   accumulated-knowledge.md │ learnings.json │ standing-questions.md  │
         └──────────────────────────────────────────────────────────────────────┘

                            EVALUATORS (one per stage)
         ┌───────────────────────────────────────────────────────────────────┐
         │  Task Plan    │  Assignment  │  Implementation  │      QA        │
         │  Evaluator    │  Evaluator   │    Evaluator     │   Evaluator    │
         └───────────────────────────────────────────────────────────────────┘
                    ▲                          ▲                      ▲
                    └──── Feedback Loop ───────┴───── Revisions ──────┘
```

### Agent Invocation Summary

| Orchestrator Invokes | Must Query Librarian First             |
| -------------------- | -------------------------------------- |
| Task Generator       | ✅ Yes                                 |
| Task Assigner        | ✅ Yes                                 |
| Software Engineer    | ✅ Yes                                 |
| QA Engineer          | ✅ Yes                                 |
| Information Explorer | N/A (invoked by Librarian only)        |
| Lessons Optimizer    | No (parses lessons directly)           |
| _All Evaluators_     | No (evaluators don't access knowledge) |

---

## Workflow Stages

```
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐    ┌────┐    ┌─────────┐
│ Intake  │───▶│ TaskPlan │───▶│  Assign  │───▶│ Execution │───▶│ QA │───▶│ Lessons │
└─────────┘    └──────────┘    └──────────┘    └───────────┘    └────┘    └─────────┘
                    │               │              │              │
                    ▼               ▼              ▼              ▼
               [Evaluator]    [Evaluator]    [Evaluator]    [Evaluator]
                    │               │              │              │
                    └───revise──────┴───revise─────┴───revise─────┘
```

### Stage 1: Intake

- **Agent**: Orchestrator
- **Output**: `story.yaml`, `config.yaml`, `constraints.md`
- **What happens**: Validates config, normalizes acceptance criteria to `AC1..ACn`, extracts constraints

### Stage 2: Task Planning

- **Agent**: Task Generator
- **Evaluator**: Task Plan Evaluator
- **Output**: `planning/tasks.yaml`
- **What happens**: Parses all ACs, creates 3–8 broad implementation tasks, maps dependencies

### Stage 3: Assignment

- **Agent**: Task Assigner
- **Evaluator**: Assignment Evaluator
- **Output**: `planning/assignments.json`
- **What happens**: Schedules UoWs respecting dependencies, identifies safe parallelization opportunities, orders high-risk items early

### Stage 4: Execution (per UoW)

- **Agent**: Software Engineer
- **Evaluator**: Implementation Evaluator
- **Output**: `execution/{UOW-ID}/impl_report.yaml`
- **What happens**: Implements code changes with documentation-first discipline, runs existing tests, queries librarian for patterns

### Stage 5: QA

- **Agent**: QA Engineer
- **Evaluator**: QA Evaluator
- **Output**: `qa/qa_report.yaml`
- **What happens**: Validates all ACs with evidence, assesses regression risk, classifies and routes failures

### Stage 6: Lessons Optimization

- **Agent**: Lessons Optimizer
- **Output**: `summary/lessons_optimizer_report.yaml`
- **What happens**: Parses recorded lessons, extracts mistake signatures, drafts prevention rules, recommends prompt updates

---

## Knowledge Management System

All knowledge flows through the **Reference Librarian Agent**. Agents do NOT access knowledge files directly—the librarian is the mandatory gateway to reduce context bloat.

### How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ Agent needs to  │────▶│ Queries the      │────▶│ Librarian       │
│ know something  │     │ Reference        │     │ responds with   │
└─────────────────┘     │ Librarian FIRST  │     │ answer/hint     │
                        └──────────────────┘     └────────┬────────┘
                                                          │
                               ┌──────────────────────────┴──────────────────────────┐
                               │                                                      │
                               ▼                                                      ▼
                  ┌────────────────────────┐                         ┌────────────────────────┐
                  │ Confidence: FULL       │                         │ Confidence: PARTIAL/   │
                  │ Agent uses answer      │                         │ NONE - Librarian       │
                  │ directly               │                         │ invokes Explorer       │
                  └────────────────────────┘                         └────────────┬───────────┘
                                                                                   │
                                                                                   ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ Future agents   │◀────│ Librarian adds   │◀────│ Explorer returns│
│ can query this  │     │ to accumulated-  │     │ findings to     │
│ knowledge       │     │ knowledge.md     │     │ librarian       │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

### Knowledge Files

| File                         | Purpose                                                | Who Writes                                                                                            |
| ---------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `accumulated-knowledge.md`   | Cumulative knowledge discovered during workflows       | Reference Librarian (when agents report findings)                                                     |
| `learnings.json`             | Structured retrieval-optimized learnings with metadata | Reference Librarian                                                                                   |
| `information-index.json`     | Knowledge taxonomy and organization schema             | Reference Librarian                                                                                   |
| `questions.json`             | Active questions being explored with status tracking   | Reference Librarian                                                                                   |
| `standing-questions.md`      | Questions that could NOT be answered                   | Reference Librarian (when exploration fails)                                                          |
| `rls-system-architecture.md` | System architecture documentation                      | Reference Librarian (maintain)                                                                        |
| `lessons.md`                 | Canonical captured lessons source (append-only)        | All agents (append/fallback capture), Reference Librarian (scoped routing), Lessons Optimizer (parse) |
| `lessons-index.json`         | Derived lesson metadata for scoped routing             | Reference Librarian (maintain), Lessons Optimizer (optional enrichment)                               |

### Librarian-Mediated Knowledge Flow

1. **Agent queries librarian**: Agent asks for information FIRST (required)
2. **Librarian responds**: With answer and confidence level (`full | partial | none`)
3. **If `partial`**: Librarian invokes Information Explorer with search hints
4. **Explorer searches**: Knowledge files → repo → official docs → secondary sources
5. **Explorer reports back**: Returns structured findings to librarian
6. **Librarian accumulates**: Adds to `accumulated-knowledge.md` and responds to agent
7. **If not found**: Librarian adds question to `standing-questions.md`

### Agent Knowledge Requirements

Every agent MUST:

1. **Before starting any task:**
   - Query the Reference Librarian FIRST for any information needs
   - DO NOT access knowledge files directly

2. **When encountering unknowns:**
   - Query librarian first
   - If librarian requests exploration, only the Information Explorer handles it
   - Findings flow back through librarian (not directly to agents)

3. **In their output reports:**
   - Include `librarian_queries` section listing questions asked
   - Include `exploration_reports` section for findings reported back to librarian

---

## Model Configuration

### Default Models

| Agent                | Default Model                        |
| -------------------- | ------------------------------------ |
| Task Generator       | `claude-sonnet-4-6`                  |
| Task Assigner        | `claude-sonnet-4-6`                  |
| Software Engineer    | `gpt-5.3-codex extra-high-reasoning` |
| QA Engineer          | `gpt-5.3-codex extra-high-reasoning` |
| Reference Librarian  | `gpt-5.3-codex extra-high-reasoning` |
| Information Explorer | `claude-sonnet-4-6`                  |
| All Evaluators       | `claude-opus-4-6`                    |
| Lessons Optimizer    | `gpt-5.3-codex extra-high-reasoning` |

### Recommended Configurations

**Default** (maximum quality):

- Agents: defaults as listed above
- Evaluators: `claude-opus-4-6`

**Speed-optimized** (faster, cheaper):

```yaml
models:
  software-engineer: 'claude-sonnet-4-6'
  qa-engineer: 'claude-sonnet-4-6'
  evaluators: 'claude-haiku-4-5'
```

**Balanced** (good quality, reasonable speed):

```yaml
models:
  task-generator: 'claude-sonnet-4-6'
  task-assigner: 'claude-sonnet-4-6'
  software-engineer: 'claude-opus-4-6'
  qa-engineer: 'claude-opus-4-6'
  evaluators: 'claude-sonnet-4-6'
```

---

## Directory Structure

```
{{artifact_root}}{CHANGE-ID}/
│
├── intake/
│   ├── story.yaml          # Normalized story with numbered ACs
│   ├── config.yaml         # Model assignments, run metadata
│   └── constraints.md      # Technical context, examples
│
├── logs/                   # Agent execution logs
│   ├── orchestrator/       # Workflow state transitions
│   ├── reference_librarian/# Knowledge queries and accumulation
│   ├── task_generator/     # Task planning sessions
│   ├── task_assigner/      # Work scheduling sessions
│   ├── software_engineer/  # Implementation sessions
│   ├── qa/                 # QA validation sessions
│   ├── information_explorer/ # Exploration sessions
│   └── lessons_optimizer/  # Lessons extraction sessions
│
├── planning/
│   ├── tasks.yaml          # Broad task plan
│   ├── assignments.json    # Execution schedule
│   ├── eval_tasks_k.json   # Evaluation attempts (k = attempt #)
│   └── eval_assignments_k.json
│
├── execution/
│   ├── UOW-001/
│   │   ├── uow_spec.yaml
│   │   ├── impl_report.yaml
│   │   ├── eval_impl_k.json
│   │   └── logs/
│   └── UOW-002/
│       └── ...
│
├── qa/
│   ├── qa_report.yaml
│   ├── eval_qa_k.json
│   └── evidence/
│       ├── screenshots/
│       ├── test_output/
│       └── logs/
│
└── summary/
    └── lessons_optimizer_report.yaml

# Knowledge Directory (separate from per-change artifacts):
# Located in agent-context/knowledge/ (or configured knowledge_root)
│
├── accumulated-knowledge.md  # Cumulative knowledge (librarian writes)
├── learnings.json            # Structured learnings (librarian writes)
├── information-index.json    # Knowledge taxonomy (librarian writes)
├── questions.json            # Active questions (librarian writes)
├── standing-questions.md     # Unanswered questions (librarian writes)
├── rls-system-architecture.md # System architecture docs
├── lessons.md                # Canonical captured lessons (append-only source)
└── lessons-index.json        # Derived metadata for scoped lesson routing
```

---

## Agent Logging

**Every agent produces a log file each time it runs.** Logs are stored in `{CHANGE-ID}/logs/{agent_name}/`.

### Log File Naming

All logs use the format: `{YYYYMMDD_HHMMSS}_{identifier}_session.json`

| Agent                | Log Location                 | Example Filename                        |
| -------------------- | ---------------------------- | --------------------------------------- |
| Orchestrator         | `logs/orchestrator/`         | `20260127_143000_state_transition.json` |
| Reference Librarian  | `logs/reference_librarian/`  | `20260127_143052_query.json`            |
| Task Generator       | `logs/task_generator/`       | `20260127_143500_session.json`          |
| Task Assigner        | `logs/task_assigner/`        | `20260127_150000_session.json`          |
| Software Engineer    | `logs/software_engineer/`    | `20260127_160000_UOW-001_session.json`  |
| QA Engineer          | `logs/qa/`                   | `20260127_180000_session.json`          |
| Information Explorer | `logs/information_explorer/` | `20260127_143100_exploration.json`      |
| Lessons Optimizer    | `logs/lessons_optimizer/`    | `20260127_190000_session.json`          |

### What Logs Contain

Each log includes:

- **Timestamp and identifiers**: When, which agent, which iteration
- **Input/output artifacts**: What was read, what was written
- **Librarian queries**: Questions asked, confidence received
- **Decisions made**: Key choices with rationale
- **Issues encountered**: Problems and how they were handled

### Using Logs for Debugging

1. **Trace workflow execution**: Follow orchestrator logs to see state transitions
2. **Understand agent decisions**: Each agent logs its reasoning
3. **Debug Reference Librarian**: See what queries got what confidence levels
4. **Track knowledge flow**: See how findings flow from Explorer → Librarian → accumulated-knowledge.md

---

## How Agents Communicate

Agents don't call each other directly. The **Orchestrator** mediates all communication (with one exception: the Reference Librarian may invoke the Information Explorer):

1. Orchestrator reads the current stage's input artifact
2. Orchestrator dispatches to the appropriate agent with context
3. Agent queries Reference Librarian for knowledge needs
4. Agent produces output artifact → writes to artifact directory
5. Evaluator reads output → assesses against rubric → writes evaluation
6. Orchestrator reads evaluation:
   - **Pass** → advance to next stage
   - **Revise** → feed evaluation feedback back to agent
   - **Escalate** → pause and notify human

```
┌─────────────┐
│ Orchestrator│
└──────┬──────┘
       │
       ├──dispatch──▶ [Agent] ──queries──▶ [Ref Librarian] ──may invoke──▶ [Explorer]
       │                  │
       │                  └──writes──▶ artifact.yaml
       │                                      │
       ├──dispatch──▶ [Evaluator] ◀──reads────┘
       │                   │
       │◀──────────────────┘ (evaluation result)
       │
       └── decide: pass | revise | escalate
```

---

## Evaluator-Optimizer Loop

Each stage runs through this control loop:

1. **Generation**: Agent produces initial artifact
2. **Evaluation**: Evaluator checks against rubric + programmatic gates
3. **Refinement**: If failed, agent revises based on actionable feedback
4. **Iteration**: Repeat until pass or stopping criteria

### Stopping Criteria

| Criteria               | Action                |
| ---------------------- | --------------------- |
| Quality gate pass      | Advance to next stage |
| Max iterations reached | Stop and escalate     |
| Token budget exceeded  | Stop and escalate     |
| Similarity plateau     | Stop and escalate     |

### Evaluator Behavior

- **Programmatic gates run first** (schema valid, AC coverage, dependencies, etc.)
- **Gate failure → Immediate FAIL** (no subjective review)
- **All gates must pass** before rubric evaluation proceeds
- **Actionable feedback**: Evaluators reference specific IDs and provide concrete fixes

---

## Escalation Handling

The workflow pauses and notifies you when:

- **Ambiguous requirements**: ACs are unclear or contradictory
- **Breaking change detected**: Code changes affect existing contracts
- **Max iterations reached**: Agent can't satisfy evaluator after configured attempts
- **Spec clarification needed**: Implementation requires human decision
- **Unresolvable patterns**: Lessons Optimizer flags recurring issues that can't be auto-resolved

When escalated, check:

1. `config.yaml` → `run_metadata.status` shows `escalated`
2. Latest evaluation file shows `escalation_recommendation`
3. Address the issue, then resume the workflow

---

## Agent Prompts Reference

| File                                   | Agent                    | Purpose                                                                            |
| -------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------- |
| `00-reference-librarian.agent.md`      | Reference Librarian      | **Mandatory first contact** for all knowledge queries; manages knowledge lifecycle |
| `01-orchestrator.agent.md`             | Orchestrator             | Controls workflow state machine, manages stage transitions                         |
| `02-task-generator.agent.md`           | Task Generator           | Creates broad task plan from acceptance criteria                                   |
| `03-task-assigner.agent.md`            | Task Assigner            | Schedules UoWs with dependency and parallelization awareness                       |
| `04-software-engineer.agent.md`        | Software Engineer        | Implements code changes with scope control                                         |
| `05-qa.agent.md`                       | QA Engineer              | Validates acceptance criteria with evidence                                        |
| `06-task-plan-evaluator.agent.md`      | Task Plan Evaluator      | Evaluates task plans for completeness and correctness                              |
| `07-assignment-evaluator.agent.md`     | Assignment Evaluator     | Evaluates execution schedules for safety                                           |
| `08-implementation-evaluator.agent.md` | Implementation Evaluator | Evaluates code changes against DoD                                                 |
| `09-qa-evaluator.agent.md`             | QA Evaluator             | Evaluates QA reports for thoroughness                                              |
| `10-information-explorer.agent.md`     | Information Explorer     | Focused research specialist invoked by Librarian                                   |
| `11-lessons-optimizer.agent.md`        | Lessons Optimizer        | Extracts lessons and generates prevention rules                                    |

---

## Required Skills

Agents in this workflow depend on shared skills that define cross-cutting protocols. These skills are progressively loaded — the platform reads each skill's `description` frontmatter to decide when to activate it.

**Canonical location**: `.github/skills/{skill-name}/SKILL.md`

### Skill Inventory

| Skill                        | Description                                                                              | Used By                                                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| **execution-discipline**     | Planning, verification, replan-on-drift, progress tracking                               | All 12 agents                                                                                            |
| **librarian-query-protocol** | Query-first knowledge access through Reference Librarian                                 | Task Generator, Task Assigner, Software Engineer, QA Engineer                                            |
| **scope-and-security**       | Forbidden actions, file access boundaries, secrets handling                              | All 12 agents                                                                                            |
| **session-logging**          | Per-spawn structured log entries, file naming conventions                                | Reference Librarian, Task Generator, Task Assigner, QA Engineer, Information Explorer, Lessons Optimizer |
| **evaluator-framework**      | Programmatic gates, rubric evaluation, pass/fail logic, actionable feedback              | Task Plan Evaluator, Assignment Evaluator, Implementation Evaluator, QA Evaluator                        |
| **lessons-capture**          | Scoped lessons retrieval + post-correction capture protocol for agent-context/lessons.md | All 12 agents                                                                                            |
| **artifact-io**              | Artifact root conventions, CHANGE-ID path construction                                   | All agents except standalone invocations                                                                 |

### Agent → Skill Matrix

| Agent                    | execution-discipline | librarian-query | scope-and-security | session-logging | evaluator-framework | lessons-capture | artifact-io |
| ------------------------ | :------------------: | :-------------: | :----------------: | :-------------: | :-----------------: | :-------------: | :---------: |
| Reference Librarian      |          ✅          |        —        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| Orchestrator             |          ✅          |        —        |         ✅         |        —        |          —          |       ✅        |     ✅      |
| Task Generator           |          ✅          |       ✅        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| Task Assigner            |          ✅          |       ✅        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| Software Engineer        |          ✅          |       ✅        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| QA Engineer              |          ✅          |       ✅        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| Task Plan Evaluator      |          ✅          |        —        |         ✅         |        —        |         ✅          |       ✅        |     ✅      |
| Assignment Evaluator     |          ✅          |        —        |         ✅         |        —        |         ✅          |       ✅        |     ✅      |
| Implementation Evaluator |          ✅          |        —        |         ✅         |        —        |         ✅          |       ✅        |     ✅      |
| QA Evaluator             |          ✅          |        —        |         ✅         |        —        |         ✅          |       ✅        |     ✅      |
| Information Explorer     |          ✅          |        —        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |
| Lessons Optimizer        |          ✅          |        —        |         ✅         |       ✅        |          —          |       ✅        |     ✅      |

### How Skill Loading Works

1. **Discovery**: The platform reads all `SKILL.md` frontmatter (`name` + `description`) at startup
2. **Matching**: When an agent is invoked, the platform matches skill descriptions against the agent's context
3. **Loading**: Matched skills are loaded into the agent's context alongside its `.agent.md` prompt
4. **Enforcement**: Each agent's `## Required Skills` section lists its dependencies and mandates following them

---

## Reference Librarian Agent

The **Reference Librarian** is the **mandatory first point of contact** for all knowledge queries. It runs on `gpt-5.3-codex extra-high-reasoning` for intelligent query handling.

### Why?

- **Reduces context bloat**: Agents only receive relevant answers, not entire files
- **Mandatory gateway**: ALL knowledge access goes through the librarian
- **Knowledge accumulation**: Librarian adds agent findings to persistent knowledge
- **Confidence levels**: Tells agents when to explore vs. use answers directly
- **Standing questions**: Tracks unanswered questions for future resolution
- **Delegated exploration**: Only the librarian can invoke the Information Explorer

### Query-First Workflow

1. Agent has a question
2. Agent queries the Reference Librarian (REQUIRED FIRST STEP)
3. Librarian responds with `confidence: full | partial | none`
4. **If `full`**: Agent uses the answer directly
5. **If `partial`**: Librarian invokes Information Explorer with hints
6. **Explorer returns findings**: Librarian updates knowledge and responds to agent
7. **If answer not found**: Librarian adds query to `standing-questions.md`

### Example Query — Has Answer

```
Query: "What database patterns exist in this codebase?"
```

Response:

```yaml
answer: 'The codebase uses Entity Framework with repository pattern...'
source_files: ['accumulated-knowledge.md']
confidence: 'full'
requires_exploration: false
```

### Example Query — Needs Exploration

```
Query: "Where is the PersonService repository pattern implemented?"
```

Response:

```yaml
answer: null
confidence: 'partial'
requires_exploration: true
exploration_request:
  action: 'explore_and_report'
  hint: 'Search for PersonService in the codebase and trace its database calls'
  report_format: 'Provide: file paths, code patterns, and a summary of findings'
```

---

## Troubleshooting

### Workflow stuck in a loop

Check the evaluation files (`eval_*_k.json`) for:

- Repeated issues that aren't being addressed
- Conflicting feedback
- Missing context the agent needs

### Tests failing repeatedly

1. Check `execution/{UOW-ID}/logs/` for build/test output
2. Review `eval_impl_*.json` for implementation evaluator feedback
3. Consider if the AC is actually testable as written

### Agent producing invalid output

The orchestrator will re-prompt for schema-compliant output. If persistent:

1. Check if the story input has unusual formatting
2. Simplify complex ACs into smaller pieces
3. Try a different model for that agent

---

## Example Workflow Run

```bash
# 1. Start workflow
--change-id 4729040 \
--story-input ./story-4729040.txt \
--code-repo ~/projects/my-app \
--project-type brownfield

# 2. Orchestrator creates:
#    - intake/story.yaml (6 ACs normalized)
#    - intake/config.yaml
#    - intake/constraints.md

# 3. Task Generator produces tasks.yaml (4 tasks)
#    - Task Plan Evaluator: PASS

# 4. Task Assigner produces assignments.json
#    - Assignment Evaluator: REVISE (unsafe parallelism)
#    - Task Assigner revises → safe batches
#    - Assignment Evaluator: PASS

# 5. Execution loop (for each UoW batch):
#    - Software Engineer implements → build passes
#    - Implementation Evaluator: PASS

# 6. QA validates all ACs with evidence
#    - QA Evaluator: PASS

# 7. Lessons Optimizer extracts learnings
#    - Prevention rules generated
#    - Rule recommendations written

# Done! All artifacts in {{artifact_root}}4729040/
```
