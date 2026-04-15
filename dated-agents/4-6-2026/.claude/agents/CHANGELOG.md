# Agent Configuration Changelog

All notable changes to agent prompt files are recorded here in reverse-chronological order.

---

## [2026-03-31]

### Added — Hyperagent dual-phase architecture for self-improvement

Renamed `04-software-engineer.agent.md` → `04-software-engineer-hyperagent.agent.md` and replaced `11-lessons-optimizer.agent.md` with `11-lessons-optimizer-hyperagent.agent.md`. Both agents now implement a **dual-phase metacognitive pattern**:

- **Phase 1 (Task Agent)**: Executes the assigned work as normal.
- **Phase 2 (Meta Agent)**: Activated only on revision attempts (attempt > 1) after an evaluator reports failure or partial-pass. Performs root-cause self-analysis by comparing the evaluator's `root_cause_hypothesis` against its own `metacognitive_context`, then generates self-evolved heuristic rules to prevent the same failure class from recurring.

**Software Engineer Hyperagent** (`04`):

- Captures `metacognitive_context` (decision rationale, discarded alternatives, knowledge gaps, tool anomalies) in `impl_report.yaml`.
- On revision, drafts algorithmic rules appended to a `### Self-Evolved Rules` sub-section in the agent's evolving problem-solving pipelines block.

**Lessons Optimizer Hyperagent** (`11`):

- Performs dual-level analysis: maps failures to both agent-level root causes and system-level failure chains across agents.
- Uses `parse-lessons.py` to extract mistake signatures and compute repeat rates.
- Injects high-confidence prevention rules into targeted agent `### Optimizer-Injected Rules` sub-sections.

**Evaluator enrichments** (agents `06`, `07`, `08`, `09`):

- All evaluators now emit `raw_evidence` (exact code/schema excerpts that triggered a finding) and `root_cause_hypothesis` (categorized as `bad_code_logic`, `hallucinated_tool_usage`, `ignored_constraints`, or `missing_librarian_knowledge` with a confidence level).
- These structured diagnostics feed the hyperagent Phase 2 loop and the Lessons Optimizer's cross-agent failure chain analysis.

---

### Added — OpenViking semantic knowledge backend (optional)

Added `open-viking-cli` skill at `.github/skills/open-viking-cli/SKILL.md`. OpenViking provides a Rust CLI (`ov`) for semantic knowledge management including resource/skill ingestion, VikingFS filesystem operations, tiered content access (L0 abstract → L1 overview → L2 full), semantic retrieval, and session/relation management.

**Conditionally loaded**: Agents detect `ov` availability at session start. If `ov system health` succeeds, agents commit to `openviking` mode for knowledge queries; otherwise they fall back to `flat-file` mode (grep/jq-based access). No agent requires OpenViking — it is an optional enhancement.

**Agents updated:**

- `00-reference-librarian.agent.md`: Uses `ov find` scoped to `viking://resources/knowledge/` for semantic search with tiered content loading.
- `10-information-explorer.agent.md`: Uses `ov find`, `ov abstract`, `ov overview`, `ov read` for knowledge exploration when in `openviking` mode.

---

### Added — Deterministic script-based gates in skills

Integrated Python scripts (`.github/scripts/`) into skill documentation so that agent evaluations and workflow operations produce deterministic, machine-readable JSON output rather than relying on ad-hoc LLM judgment.

**Scripts added per skill:**

| Skill                 | Scripts Integrated                                                                                             | Purpose                                                                                                                                                           |
| --------------------- | -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `evaluator-framework` | `validate-artifact-schema.py`, `check-dependency-cycles.py`, `check-ac-coverage.py`, `check-test-harnesses.py` | Programmatic evaluation gates for task plans, assignments, implementations, and QA reports                                                                        |
| `scope-and-security`  | `validate-scope.py`                                                                                            | Validates file paths against forbidden patterns (`.env*`, `*secret*`, `*credential*`, lock files, `node_modules/`, `dist/`, `.git/`) and artifact-root boundaries |
| `artifact-io`         | `init-artifact-dirs.py`                                                                                        | Creates the standard artifact directory tree for a CHANGE-ID                                                                                                      |
| `session-logging`     | `init-session-log.py`                                                                                          | Creates timestamped, pre-structured JSON log files for agent sessions                                                                                             |

**Orchestrator** (`01-orchestrator.agent.md`) also integrated: `detect-ui-changes.py` (determines if UI QA is needed), `validate-scope.py`, `validate-diff-size.py` (categorizes diff size for scope control), and `init-artifact-dirs.py`.

---

### Changed — Shell scripts converted to Python for cross-platform support

All 6 `.sh` scripts in `.github/scripts/` were converted to Python 3 (stdlib-only) replacements to support Windows and other non-Bash environments. Each Python script preserves the identical CLI interface, JSON output format, and exit codes as its shell predecessor.

| Removed                   | Replacement               | Notes                               |
| ------------------------- | ------------------------- | ----------------------------------- |
| `detect-ui-changes.sh`    | `detect-ui-changes.py`    | —                                   |
| `validate-scope.sh`       | `validate-scope.py`       | —                                   |
| `init-artifact-dirs.sh`   | `init-artifact-dirs.py`   | —                                   |
| `init-session-log.sh`     | `init-session-log.py`     | Eliminates external `jq` dependency |
| `check-test-harnesses.sh` | `check-test-harnesses.py` | —                                   |
| `validate-diff-size.sh`   | `validate-diff-size.py`   | —                                   |

All agent and skill markdown references updated accordingly. The `.github/scripts/` directory is now 100% Python.

---

## [2026-03-26 17:04:07Z]

### Changed — shared `code-comment-standards` skill rollout

Created a new shared skill at `/Users/mckerracher.joshua/.github/skills/code-comment-standards/SKILL.md` and updated the agent prompt fleet to load it as the single source of truth for story-traceable code comments.

**Summary of change:**  
The work-item citation rules for acceptance-criteria and story-linked code comments were moved out of the Software Engineer prompts and into a reusable skill so every agent can follow the same standard. Agent prompts now load `code-comment-standards`, and the duplicated `## Code Comment Standards` sections were removed from the Software Engineer and Software Engineer Hyperagent prompts.

**Result:**  
Any agent that adds or reviews story-specific code comments is now expected to use the shared rule: include the `WI-XXXXXXX` identifier whenever the comment references an acceptance criterion, story requirement, ticket, or work item.

---

## [2026-03-26]

### Changed — `04-software-engineer.agent.md` and `04-software-engineer-hyperagent.agent.md`

**Added `## Code Comment Standards` section** to both the base Software Engineer agent and the Software Engineer Hyperagent.

**Summary of change:**  
The software engineer agents are now required to include the work item ID in any inline or block code comment that references acceptance criteria, story requirements, or work-item context. Previously, AC-linked comments could be written without tracing back to the originating story, making it difficult to audit which requirements drove a given code decision.

**New rule:**  
When a comment explains _why_ code exists or behaves a certain way due to a story AC or requirement, the comment must follow the format:

```
// WI-XXXXXXX [AC ref]: explanation
```

**Applies to:** AC-condition comments, business-rule block comments, TODO/FIXME notes tracing to a story, and any comment using language such as "per story", "per AC", "per requirement", or "per ticket".

**Motivation:** Enables `grep WI-XXXXXXX` to immediately surface all code influenced by a given work item — useful for auditing, reverting story-specific changes, and understanding historical implementation intent.
