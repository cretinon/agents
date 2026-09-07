---
agent: code
---
# AI Agent Development Workflow & Rules

You must strictly adhere to the following phased workflow for any development task, modification, or bug fix. Do not skip any steps or execute code before receiving explicit user approval.

> **Trivial changes** — typos, renames, or one-line fixes with no behavioral impact — may skip Phases 1–2. Announce the change in one line and implement it directly, then still run the scoped Phase 4 (Quality Assurance — your own tests only) and Phase 5 (Final Summary).

---

## Phase 1: Analysis (Interactive Loop)
Before writing, editing, or running any code or commands, you must construct a step-by-step implementation plan.

- **Do not** take any decision : Pause your analysis then ask questions when you have to make a choice or if anything is not straight forward.
- **Do not** gather more facts before asking targeted questions : Pause your analysis then ask questions as soon as you have one.
- **Analyze:** Carefully review the request, codebase context, and requirements. 
- Proceed to **Phase 2** ONLY when your analysis is done and you have no more questions.

---

## Phase 2: Blueprint
Construct the step-by-step implementation plan

1. **Draft Plan:** Formulate a structured plan covering:
   - High-level approach and objective.
   - Resume all decisions taken by the user.
   - Files to create, modify, or delete.
   - Core implementation steps.
   - Strategy for linting and for testing the code you write — scoped to your **own** tests (the full suite and code coverage are run only by the `code_reviewer` sub-agent, see Phase 4).
2. **Present Plan:** Output the plan clearly to the user using the following format:

> ### Proposed Implementation Plan
> **Objective:** [Brief statement of the goal]
> **Decisions:** 
> - `question 1`: [User choice]
> - `question 2`: [User choice]
> **Affected Files:**
> - `path/to/file1`: [Action]
> - `path/to/file2`: [Action]
> **Steps:**
> 1. [Step 1]
> 2. [Step 2]
> **Testing Strategy:** [How YOUR tests and lint will be validated (scoped, never the full suite); full-suite and coverage are run by the `code_reviewer` sub-agent during the review]
>
> ---
> *Please reply to validate this plan or request adjustments before proceeding.*

3. **Pause:** Stop execution immediately and wait for user input. **Do NOT run implementation steps until approved.**

---

## Phase 3: Plan Revision (Interactive Loop)
- If the user requests changes to the plan, update the proposal accordingly.
- Present the updated plan and request validation again.
- Proceed to **Phase 4** ONLY when the user explicitly approves the plan (e.g., "approved", "ok", "go ahead").

---

## Phase 4: Execution & Implementation
Once approved, execute the plan precisely as agreed upon.

1. Make the necessary code modifications and write new features/fixes.
2. Ensure clean, readable, and well-structured code adhering to established project patterns.
3. **If the plan turns out to be wrong** during execution (missing file, failed approach, new constraints): **stop and report** the discrepancy to the user instead of improvising. Propose an updated plan and wait for approval before continuing.

---

## Phase 5: Quality Assurance & Verification (scoped)

Run only the tests **you** wrote or modified — never the whole project suite, unless you are the `code_reviewer` sub-agent. All checks go through the project's **own** wrapper as defined in the project's `AGENTS.md` (e.g. `my_warp.sh --lib <lib> -s|-b|-k`) — never the raw binaries when a project mandates a wrapper.

1. **Linter:** Run the project's linter (`-s`) and resolve all errors and warnings introduced by your changes. Full-library linting is allowed for any agent.
2. **Your tests only:** Run the BATS subset matching the `@test` blocks you added or modified, through the wrapper with a filter regex:
   ```shell
   ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -b '^<name-of-the-test-you-wrote>$'  # exact test
   ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -b '<unique-substring>'              # one test
   ${MY_GIT_DIR}/shell/my_warp.sh --lib "$LIB" -b '<test-a>|<test-b>'               # several tests
   ```
   Verify your tests pass (exit code `0`). If you need to debug interactions with neighbouring tests you may run any **filtered** subset — never the full `-b` without a filter.
3. **Coverage:** Do **not** run the coverage tool (`-k`) yourself — coverage is run only by the `code_reviewer` sub-agent.

Report the results of the checks **you** ran to the user. The full-suite and coverage results are reported by the `code_reviewer` sub-agent during the review that precedes commit / PR / task completion (see `code_review.md`).

---

## Phase 6: Final Summary
Conclude the process by presenting a concise final summary of the work done:

- **Summary of Changes:** High-level description of what was implemented.
- **Verification Results:**
  - Linter status (Passed/Clean)
  - Own-tests status (Pass count / Fail count — the filtered subset you ran)
  - Full suite & code coverage: run by the `code_reviewer` sub-agent during the review (results reported there)
- **Next Steps / Recommendations:** (If applicable)
