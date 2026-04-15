# Agent Configuration Changelog

All notable changes to agent prompt files are recorded here in reverse-chronological order.

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
When a comment explains *why* code exists or behaves a certain way due to a story AC or requirement, the comment must follow the format:

```
// WI-XXXXXXX [AC ref]: explanation
```

**Applies to:** AC-condition comments, business-rule block comments, TODO/FIXME notes tracing to a story, and any comment using language such as "per story", "per AC", "per requirement", or "per ticket".

**Motivation:** Enables `grep WI-XXXXXXX` to immediately surface all code influenced by a given work item — useful for auditing, reverting story-specific changes, and understanding historical implementation intent.
