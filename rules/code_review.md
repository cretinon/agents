---
agent: code
---

# Code Review — ALWAYS via Sub-Agent

Any code review MUST be executed by a dedicated, read-only sub-agent, never inline by the main agent.

## Mandatory delegation

1. Whenever the user asks for a code review — or the workflow requires reviewing changes before a commit, a PR, or declaring a task finished — spawn the `code_reviewer` sub-agent with `eca__spawn_agent`.
2. **Never** perform the review yourself inline. The main agent's job is to prepare the review scope, spawn the sub-agent, wait for its report, and relay it.
3. Do not summarize, paraphrase, or "improve" the reviewer's findings: relay the returned report faithfully to the user.

## Preparing the spawn task

Give the `code_reviewer` a self-contained task prompt containing:
- the **project root** and the **library name** (`LIB`, e.g. `mcp`, `shell`, `storm`) being reviewed;
- the exact **scope**: files changed and/or the embedded diff (`git diff HEAD` output) when reviewing pending changes;
- the **review focus** (bugs, conventions, test parity, security, ...) if the user specified one;
- the instruction to **run the full project quality gate** (`${MY_GIT_DIR}/shell/my_warp.sh --lib <LIB> -s|-b|-k`) and report the three results — the reviewer is the only agent that runs ALL tests, so the gate is part of the review before commit / PR / task completion.

The reviewer's system prompt already instructs it to also read `${MY_GIT_DIR}/agents/rules/shell.md` (shell coding conventions) when the reviewed code is Shell, in addition to the project's `AGENTS.md`.

## After the review

- Present the reviewer's report to the user and wait for their decision.
- Do NOT edit code to "fix" review findings before the user approves (this is consistent with the `AI_dev.md` phased workflow — the review belongs to the Quality Assurance phase, and any fix is a new change requiring approval).
- When the user approves a fix, implement it yourself (or via an execution sub-agent). You never re-run the full suite by yourself: verification of the fix is done by spawning the `code_reviewer` again, which re-runs the full quality gate on the updated scope.

## Reviewer capabilities (enforced by `config.json`)

- `code_reviewer` is **read-only**: file editing tools are disabled for it.
- It is the **only** agent that runs the **full** project validation gate: `${MY_GIT_DIR}/shell/my_warp.sh --lib <LIB> -s|-b|-k` (lint, ALL tests, coverage). Always include that instruction in the spawn task — the reviewer runs the gate as part of the mandatory review before commit / PR / task completion.
- Every other agent (main `code` agent, `general`, `explorer`, ...) runs **only the tests it wrote/modified** (filtered `-b '<regex>'`) and may run `-s`; it never runs the full suite or coverage alone (see `AI_dev.md` Phase 4 and `shell.md` Dry-Run Validation).
