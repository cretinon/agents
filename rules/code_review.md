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
- the **review focus** (bugs, conventions, test parity, security, ...) if the user specified one.

The reviewer's system prompt already instructs it to also read
`${MY_GIT_DIR}/agents/rules/shell.md` (shell coding conventions) when the reviewed
code is Shell, in addition to the project's `AGENTS.md`.

## After the review

- Present the reviewer's report to the user and wait for their decision.
- Do NOT edit code to "fix" review findings before the user approves (this is consistent with the `AI_dev.md` phased workflow — the review belongs to the Quality Assurance phase, and any fix is a new change requiring approval).
- When the user approves a fix, implement it yourself (or via an execution sub-agent), then re-run the quality gate.

## Reviewer capabilities (enforced by `config.json`)

- `code_reviewer` is **read-only**: file editing tools are disabled for it.
- It is allowed to run the project's **own** validation wrapper `${MY_GIT_DIR}/shell/my_warp.sh --lib <LIB> -s|-b|-k` to run lint, tests, and coverage — include that instruction in the spawn task whenever a quality check is part of the review.
