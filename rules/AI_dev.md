---
agent: code
---
# AI Agent Development Workflow & Rules

You must strictly adhere to the following phased workflow for any development task, modification, or bug fix. Do not skip any steps or execute code before receiving explicit user approval.

> **Trivial changes** — typos, renames, or one-line fixes with no behavioral impact — may skip Phases 1–2. Announce the change in one line and implement it directly, then still run Phase 4 (Quality Assurance) and Phase 5 (Final Summary).

---

## Phase 1: Planning & Blueprint
Before writing, editing, or running any code or commands, you must construct a step-by-step implementation plan.

1. **Analyze:** Carefully review the request, codebase context, and requirements.
2. **Draft Plan:** Formulate a structured plan covering:
   - High-level approach and objective.
   - Files to create, modify, or delete.
   - Core implementation steps.
   - Strategy for linting, testing, and verifying code coverage.
3. **Present Plan:** Output the plan clearly to the user using the following format:

> ### Proposed Implementation Plan
> **Objective:** [Brief statement of the goal]
> **Affected Files:**
> - `path/to/file1`: [Action]
> - `path/to/file2`: [Action]
> **Steps:**
> 1. [Step 1]
> 2. [Step 2]
> **Testing Strategy:** [How tests, linting, and coverage will be validated]
>
> ---
> *Please reply to validate this plan or request adjustments before proceeding.*

4. **Pause:** Stop execution immediately and wait for user input. **Do NOT run implementation steps until approved.**

---

## Phase 2: Plan Revision (Interactive Loop)
- If the user requests changes to the plan, update the proposal accordingly.
- Present the updated plan and request validation again.
- Proceed to **Phase 3** ONLY when the user explicitly approves the plan (e.g., "approved", "ok", "go ahead").

---

## Phase 3: Execution & Implementation
Once approved, execute the plan precisely as agreed upon.

1. Make the necessary code modifications and write new features/fixes.
2. Ensure clean, readable, and well-structured code adhering to established project patterns.
3. **If the plan turns out to be wrong** during execution (missing file, failed approach, new constraints): **stop and report** the discrepancy to the user instead of improvising. Propose an updated plan and wait for approval before continuing.

---

## Phase 4: Quality Assurance & Verification
After code changes are complete, run the project's **own** validation suite as defined in the project's `AGENTS.md` — never the raw binaries when a project mandates a wrapper (e.g. `my_warp.sh --lib <lib> -s|-b|-k`).

1. **Linter:** Run the project's linter and resolve all errors and warnings introduced by your changes.
2. **Test Suite:** Run the project's test suite and verify all tests pass. If existing tests fail or new code requires coverage, add/update tests accordingly.
3. **Code Coverage:** Run the coverage tool and verify that new and modified code meets the project's coverage threshold.

Report each verification result to the user.

---

## Phase 5: Final Summary
Conclude the process by presenting a concise final summary of the work done:

- **Summary of Changes:** High-level description of what was implemented.
- **Verification Results:**
  - Linter status (Passed/Clean)
  - Test suite status (Pass count / Fail count)
  - Code coverage status (Percentage / Metrics, vs. the project threshold)
- **Next Steps / Recommendations:** (If applicable)
