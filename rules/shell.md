---
agent: code
paths: "**.sh"
enforce:
  - read
  - modify
---

#  AI Agent Guidelines & Rules: Rules for Shell Scripting

You are an expert systems engineer and Shell scripting authority. 
Always follow these explicit rules when writing, refactoring or reviewing Shell code in this project.
Read `${MY_GIT_DIR}/shell/functions.md` if you need definitions of functions used in this document.

---
## Conventions
### Naming Conventions
- Library functions must start with a single underscore (e.g. `_usage`).
- Local variables must start with a double underscore (e.g. `__line`).
- Global variables must be written in capital (e.g. GREP).

### Comment Conventions
- **`# usage:` Comments**: Every function that is reachable from the orchestrator CLI must be documented with a `# usage:` comment line directly above its definition, e.g. `# usage: _decrypt_file --file ($1) --passphrase ($2) --remove-src ($3)`. These lines are parsed by `_usage` and `_getopt_long` to build the CLI help and option list. Keep `# usage:` lines short and with a consistent shape — they are consumed by `cut`/`sed` pipelines that strip `($1)`, `($2)`, etc.
- **`# call:` Comments**: Every function that is not reachable from the orchestrator CLI must be documented with a `# call:` comment line directly above its definition in order to quickly see what are the function input, e.g. `# call: _load_conf ($1:file)`
- `# usage:` and `# call:` are mutually exclusive 
- **`# description:` Comments**: Every function must be documented with a `# description:` comment line directly below its `# usage:` or `# call:` in order to resume in 2 lines maximum what the function do.
- **`# example:` Comments**: Every function must be documented with at least one `# example:` comment line directly below its `# description:` and above its `# return:` lines, showing concrete calls, e.g. `# example: _get_techno "vmware,veeam"` — list every techno except those matching `vmware` or `veeam`.
- **`# return:` Comments**: Every function must be documented with `# return:` comment lines directly below its `# description:` / `# example:` lines — one line per possible exit code (see the Return Codes section: `0` success, `1` generic error/failure, `$ERROR_ARGV` argument/validation error), each stating the code and the condition that produces it. Examples:

  ```bash
  # return: `0` — success.
  # return: `1` — API request failed.
  # return: `10` (`ERROR_ARGV`) — `STORM_TOKEN`/`STORM_BASE_URL` missing, or `jq` not installed.
  ```

  A function that always returns `0` and echoes its result still documents that single outcome, e.g. `# return: 0` — outputs the PIN on stdout. The `# return-inline:` tag is **not allowed** — always use `# return:`.
- **`# doc-*:` Markers (functions.md generation)**: optional file/section-level documentation markers parsed by `_doc` when the `functions.md` reference is regenerated:
   * `# doc-section: <name>`: starts a new section in the generated reference; the functions below are grouped under `<name>` until the next `# doc-section:` marker.
   * `# doc-top:`: block of lines inserted at the top of the generated document.
   * `# doc-bottom:`: block of lines appended at the end of the generated document.
   * `# doc-intro:`: block of lines inserted at the top of the current `# doc-section`.
   * `# doc-verbatim:`: block of lines copied verbatim into the generated document (e.g. required runtime setup shown before the documented functions).

---

## Shell best practices
- Always quote variable expansions to prevent word splitting/globbing (e.g. use `"$__dashboard_id"` instead of `$__dashboard_id`).
- Use `local LC_ALL=C` in functions that do case conversion, regex matching, or locale-sensitive formatting so behavior is deterministic regardless of the environment locale.
- There is no max-length for line length

---

## Validation Primitive
- Check for variable presence using the utility functions `_exist`.
- Check for file existence using `_fileexist`.
- Check for function existence using `_func_exist`.
- Check for installed binaries using `_installed`.
- Every function must validate all its arguments at the top, before doing any work, using validation primitive
- Optional arguments (those with a default) must still be validated **when present**: check their value against the allowed set using `[ ]` or a dedicated `_json_*`-style helper
- Wrapper functions must validate their own arguments before delegating; a callee's validation is not a substitute.

---

## Local variable
- If the function needs to output any data, a local variable `__result` must be declared at the beginning of the function. 
- The only way to output result of the function is `echo "$__result"` 
- Logs emitted via `_info`, `_warning` and `_error` are **not** the function result and are not subject to this rule

---

## Return Codes
- `0` — success.
- `1` — generic error/failure.
- `$ERROR_ARGV` — argument/validation error.
- Always `return` a non-zero code on error; never silently swallow a failure.

---

## Commands to avoid
- Avoid raw `grep` in favor of the preconfigured `$GREP` (enforced by lint).
- Avoid raw `curl` in favor of the `_curl` function (except in `_curl` function itself).
- Avoid raw `jq` in favor of a `_json_*` helper function (except in `_json_*` functions themselves).
- Avoid raw `git` in favor of a `_git_*` helper function (except in `_git_*` functions themselves).
- Use as often as possible functions defined in `${MY_GIT_DIR}/shell/functions.md` 

---

## Logger & Output Helpers
### Logger
- Output standardized logs using `_info` or `_warning` depending on severity
- Output error using `_error`. Never use `echo` or `print` to output an error
- Standard error message must include the uppercase argument name (e.g. if there is an error on argument or variable `__pass` then use `_error "PASS: then your message"`)

### Telemetry hook
- Every library function must invoke `_func_start "$@"` at its entry point. 
- Every library function must invoke, on the same line, `_func_end` before **every** `return` including error and early-exit paths.
- The `_func_end` must always take the same parameter as the `return` call.
- When returning a non-zero value we must invoke, on the same line, `_error` or `_warning` before any `_func_end`.
- Examples :
    - a function that return a success value : `_func_end "0" ; return "0"`
    - a function that return a generic error/failure : `_error "FILE: $__file not found" ; _func_end "1" ; return "1"`
    - a function that return an  argument/validation error :  `_error "FILE: $__file not found" ; _func_end "$ERROR_ARGV" ; return "$ERROR_ARGV"`
    - a function that forward a return code : `_func_end "$__return" ; return "$__return"`

---

## Lint Exemption
- When a line intentionally violates a lint rule there will be a `# no _shellcheck` comment to that line so the custom lint rules skip it. Never ever add a lint exemption yourself. Lint exemption is done only by code owner.
- Existing exemptions are already marked and must be preserved during refactors


---

## Test Synchronization Rules
- **Test-Code Parity**: Every change to a shell script in requires a matching update or addition in the `bats/` directory.
- **Regression Prevention**: Do not modify existing script behavior without updating the corresponding `@test` blocks in the relevant `.bats` file.
- **Dry-Run Validation (scoped)**: After modifying any script or test, run **only the tests you wrote or modified** through the sanctioned wrapper with a filter regex matching their `@test` names — `${MY_GIT_DIR}/shell/my_warp.sh --lib <lib> -b '<filter>'` — never by invoking the raw `bats` binary directly. You may also run the linter (`-s`). You MUST NOT run the full suite (`-b` without a filter) or the coverage tool (`-k`): the full project gate is run by the `code_reviewer` sub-agent before commit / PR / task completion (see `code_review.md` and `shell/AGENTS.md`).
