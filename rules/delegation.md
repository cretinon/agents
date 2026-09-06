---
agent: code
---

# Delegation & Knowledge Source Priority

## Classify first — before using any tool

Before searching, grepping, reading files, or spawning anything, determine the type of question:

1. **"How to ...?" questions** — about ECA, its configuration, editors, tools, the OS, or any practical how-to → **MUST delegate to the `howto_master` sub-agent**: call `eca__spawn_agent` with `agent="howto_master"` and relay its answer verbatim. Do NOT answer how-to questions inline, and do NOT first gather context with web tools, skills, or file reads.
   - Exception: when the answer depends on facts from the user's own files/codebase, gather those facts locally first (items 2–5 below), include them in the `howto_master` spawn task, and let it produce the final how-to answer.
2. If any agent or sub-agent needs to search **outside of any workspace**, it can only use the `howto_master` or `web_search` or `web_fetch`. Do not grep/read/search arbitrary local files outside the workspace roots, and do not guess paths.
3. **About the user's codebase / projects** (their files, their behavior) → local exploration (`grep`, `read_file`, `directory_tree`).
4. **About ECA itself / agent behavior** → if the user asks "how to ...?", delegate to `howto_master` (item 1); otherwise answer inline or load `eca-info`.
5. **Code change requests** → read local files, plan (see `AI_dev.md`), wait for approval, implement.
