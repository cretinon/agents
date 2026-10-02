#!/bin/bash
#
# ECA `preToolCall` hook for the graphify MCP server.
#
# Contract with ECA:
#   stdin  : the hook JSON (`tool_input.project_path` first, then `cwd` and `workspaces`)
#   stdout : silent on success, otherwise one JSON object (approval / systemMessage)
#   stderr : diagnostics only
#
# Graphs live in <GRAPH_OUTPUT_ROOT>/<project>/graphify-out/, outside every repository.
# The hook never writes inside a repository and never asks the caller to cooperate.
#
# Standalone: ECA executes this file without the my_warp runtime, so the validation helpers of
# `rules/shell.md` (`_exist`, `_fileexist`, `_installed`, `_error`) and its telemetry hooks do not apply.

set -o pipefail

GRAPHIFY_BIN="${GRAPHIFY_BIN:-graphify}"
GRAPHIFY_ROOT="${GRAPHIFY_ROOT:-/root/git}"
GRAPH_OUTPUT_ROOT="${GRAPH_OUTPUT_ROOT:-/root/.cache/graphify}"
REBUILD_TIMEOUT="${REBUILD_TIMEOUT:-120}"
HOOK_NAME="graphify-refresh"

# call: __hook_log ($1:message)
# description: Writes one diagnostic line to stderr so stdout stays reserved for the hook answer.
# example: __hook_log "rebuilt /root/git/mcp"
# return: `0` — always.
__hook_log () {
    local __message="$1"

    printf '[%s] %s\n' "$HOOK_NAME" "$__message" >&2

    return 0
}

# call: __hook_answer ($1:approval) ($2:message)
# description: Prints the single JSON object ECA reads from the hook — the context for the model plus the user-facing message, an approval being added only when one is given.
# example: __hook_answer "deny" "graphify refresh failed for /root/git/mcp"
# example: __hook_answer "" "no graph could be built for /root/git/mcp"
# return: `0` — the answer was printed as a single line of JSON.
# return: `1` — the message is empty or `jq` is missing, nothing was printed.
__hook_answer () {
    local __approval="$1"
    local __message="$2"
    local __result

    if [ -z "$__message" ]; then
        __hook_log "MESSAGE: empty, unable to answer"
        return 1
    fi

    if ! command -v jq >/dev/null 2>&1; then
        __hook_log "JQ: not installed, unable to answer"
        return 1
    fi

    __result=$(jq -cn --arg approval "$__approval" --arg message "$__message" '
        if $approval == "" then
            {additionalContext: $message, systemMessage: $message}
        else
            {approval: $approval, additionalContext: $message, systemMessage: $message}
        end')

    echo "$__result"

    return 0
}

# call: __hook_cwd ($1:payload)
# description: Returns the `cwd` field of the hook JSON.
# example: __hook_cwd "$__payload"
# return: `0` — the directory was printed.
# return: `1` — empty payload, `jq` missing, or no usable `cwd`.
__hook_cwd () {
    local __payload="$1"
    local __result

    if [ -z "$__payload" ]; then
        return 1
    fi

    if ! command -v jq >/dev/null 2>&1; then
        return 1
    fi

    __result=$(printf '%s' "$__payload" | jq -r '.cwd // empty' 2>/dev/null)

    if [ -z "$__result" ]; then
        return 1
    fi

    echo "$__result"

    return 0
}

# call: __hook_project_root ($1:start_dir)
# description: Walks up from a directory to the first ancestor holding a `.git` entry, without invoking git.
# example: __hook_project_root "/root/git/mcp/bats"  # -> /root/git/mcp
# return: `0` — the repository root was printed.
# return: `1` — the directory is unusable or no ancestor holds a `.git` entry.
__hook_project_root () {
    local LC_ALL=C
    local __start="$1"
    local __result

    if [ -z "$__start" ] || [ ! -d "$__start" ]; then
        return 1
    fi

    __result="${__start%/}"

    while [ -n "$__result" ] && [ "$__result" != "/" ]; do
        if [ -d "$__result/.git" ]; then
            echo "$__result"
            return 0
        fi
        __result=$(dirname "$__result")
    done

    return 1
}

# call: __hook_workspace_match ($1:payload) ($2:directory)
# description: Returns the longest `workspaces` entry containing a directory (a workspace, which is not necessarily the project), accepting plain strings or objects with a path.
# example: __hook_workspace_match "$__payload" "/root/git/mcp/bats"  # -> /root/git when the workspaces list holds that base directory
# return: `0` — the workspace was printed.
# return: `1` — empty arguments, `jq` missing, or no workspace contains the directory.
__hook_workspace_match () {
    local LC_ALL=C
    local __payload="$1"
    local __directory="${2%/}"
    local __workspace
    local __result=""

    if [ -z "$__payload" ] || [ -z "$__directory" ]; then
        return 1
    fi

    if ! command -v jq >/dev/null 2>&1; then
        return 1
    fi

    while IFS= read -r __workspace; do
        __workspace="${__workspace%/}"
        if [ -z "$__workspace" ]; then
            continue
        fi
        case "$__directory/" in
            "$__workspace"/*)
                if [ "${#__workspace}" -gt "${#__result}" ]; then
                    __result="$__workspace"
                fi
                ;;
        esac
    done < <(printf '%s' "$__payload" | jq -r '.workspaces[]? | if type == "string" then . else (.path // .root // .uri // .folder // empty) end' 2>/dev/null)

    if [ -z "$__result" ]; then
        return 1
    fi

    echo "$__result"

    return 0
}

# call: __hook_check_config ()
# description: Validates the environment knobs (path normalization, a timeout above zero) and falls back to the documented defaults, so a malformed value can never disable the hook silently.
# example: __hook_check_config
# return: `0` — always, the globals hold usable values.
__hook_check_config () {
    local LC_ALL=C
    local __value

    __value="$GRAPHIFY_ROOT"
    while [ "${__value%/}" != "$__value" ]; do
        __value="${__value%/}"
    done
    if [ -z "$__value" ]; then
        __hook_log "GRAPHIFY_ROOT: empty, falling back to /root/git"
        __value="/root/git"
    fi
    GRAPHIFY_ROOT="$__value"

    __value="$GRAPH_OUTPUT_ROOT"
    while [ "${__value%/}" != "$__value" ]; do
        __value="${__value%/}"
    done
    if [ -z "$__value" ]; then
        __hook_log "GRAPH_OUTPUT_ROOT: empty, falling back to /root/.cache/graphify"
        __value="/root/.cache/graphify"
    fi
    GRAPH_OUTPUT_ROOT="$__value"

    case "$REBUILD_TIMEOUT" in
        ''|*[!0-9]*|0)
            __hook_log "REBUILD_TIMEOUT: $REBUILD_TIMEOUT is not a usable number of seconds, falling back to 120"
            REBUILD_TIMEOUT=120
            ;;
    esac

    return 0
}

# call: __hook_call_target ($1:payload)
# description: Returns the project a call targets, read from its `tool_input.project_path` and accepted only when it is a direct child of the watched root.
# example: __hook_call_target "$__payload"  # -> /root/git/shell when the call passes /root/.cache/graphify/shell
# return: `0` — the project was printed.
# return: `1` — empty payload, `jq` missing, no `project_path`, or a target that is not a direct child of the watched root.
__hook_call_target () {
    local LC_ALL=C
    local __payload="$1"
    local __target
    local __result

    if [ -z "$__payload" ]; then
        return 1
    fi

    if ! command -v jq >/dev/null 2>&1; then
        return 1
    fi

    __target=$(printf '%s' "$__payload" | jq -r '.tool_input.project_path // empty' 2>/dev/null)
    __target="${__target%/}"

    if [ -z "$__target" ]; then
        return 1
    fi

    if [ "${__target#"$GRAPH_OUTPUT_ROOT"/}" != "$__target" ]; then
        __result="$GRAPHIFY_ROOT/$(basename "$__target")"
    else
        __result="$__target"
    fi

    if [ ! -d "$__result" ]; then
        return 1
    fi

    if ! __hook_under_root "$__result"; then
        return 1
    fi

    if [ "$(dirname "$__result")" != "$GRAPHIFY_ROOT" ]; then
        return 1
    fi

    echo "$__result"

    return 0
}

# call: __hook_under_root ($1:path)
# description: Reports whether a directory is a project of the watched root, the root itself being excluded.
# example: __hook_under_root "/root/git/mcp"      # -> true
# example: __hook_under_root "/root/.cache/mcp"   # -> false
# return: `0` — the directory is a project inside the watched root.
# return: `1` — the directory is empty, is the root itself, or is outside it.
__hook_under_root () {
    local LC_ALL=C
    local __path="${1%/}"

    if [ -z "$__path" ]; then
        return 1
    fi

    if [ "$__path" = "$GRAPHIFY_ROOT" ]; then
        return 1
    fi

    case "$__path/" in
        "$GRAPHIFY_ROOT"/*) return 0 ;;
    esac

    return 1
}

# call: __hook_out_dir ($1:project)
# description: Returns the graphify output parent directory of a project, which is kept outside the repository.
# example: __hook_out_dir "/root/git/mcp"  # -> /root/.cache/graphify/mcp
# return: `0` — the directory was printed.
# return: `1` — the project argument is empty.
__hook_out_dir () {
    local __project="${1%/}"
    local __result

    if [ -z "$__project" ]; then
        return 1
    fi

    __result="$GRAPH_OUTPUT_ROOT/$(basename "$__project")"

    echo "$__result"

    return 0
}

# call: __hook_needs_rebuild ($1:project) ($2:graph_file)
# description: Reports whether the graph is missing or older than a file of the project, the tooling directories being excluded.
# example: __hook_needs_rebuild "/root/git/mcp" "/root/.cache/graphify/mcp/graphify-out/graph.json"
# return: `0` — the graph must be rebuilt.
# return: `1` — the graph exists and is newer than every file of the project.
__hook_needs_rebuild () {
    local __project="${1%/}"
    local __graph_file="$2"
    local __newer

    if [ -z "$__project" ] || [ -z "$__graph_file" ]; then
        return 0
    fi

    if [ ! -d "$__project" ]; then
        return 0
    fi

    if [ ! -f "$__graph_file" ]; then
        return 0
    fi

    __newer=$(find "$__project" \
        -path "$__project/.git" -prune -o \
        -path "$__project/.terraform" -prune -o \
        -path "$__project/.venv" -prune -o \
        -path "$__project/__pycache__" -prune -o \
        -path "$__project/node_modules" -prune -o \
        -path "$__project/graphify-out" -prune -o \
        -path "$__project/.serena" -prune -o \
        -type f -newer "$__graph_file" -print -quit 2>/dev/null)

    if [ -n "$__newer" ]; then
        return 0
    fi

    return 1
}

# call: __hook_rebuild ($1:project)
# description: Runs a code-only graphify extraction into the cache directory, every graphify byte being sent to stderr.
# example: __hook_rebuild "/root/git/mcp"
# return: `0` — the graph was rebuilt.
# return: `1` — unusable project, unusable output directory, or graphify not installed.
# return: `$__return` — the status of the graphify run, `124` when the rebuild timed out.
__hook_rebuild () {
    local __project="${1%/}"
    local __out
    local __return

    if [ -z "$__project" ] || [ ! -d "$__project" ]; then
        return 1
    fi

    if ! command -v "$GRAPHIFY_BIN" >/dev/null 2>&1; then
        __hook_log "GRAPHIFY_BIN: $GRAPHIFY_BIN not found"
        return 1
    fi

    if ! __out=$(__hook_out_dir "$__project"); then
        return 1
    fi

    mkdir -p "$__out"

    timeout "$REBUILD_TIMEOUT" "$GRAPHIFY_BIN" extract "$__project" --code-only --out "$__out" >&2
    __return=$?

    return "$__return"
}

# call: main ()
# description: Reads the hook JSON, resolves the project, rebuilds a stale graph and answers ECA on stdout.
# example: echo '{"cwd":"/root/git/mcp","workspaces":["/root/git/mcp"]}' | graphify-refresh.sh
# return: `0` — silent when nothing was needed, otherwise the graph was rebuilt or a notice was printed; an unusable answer is logged on stderr.
main () {
    local __payload
    local __cwd
    local __project
    local __out
    local __graph_file
    local __graph_existed=0
    local __return
    local __message

    __payload=$(cat)

    __hook_check_config

    if ! __project=$(__hook_call_target "$__payload"); then
        __project=""
    fi

    if [ -z "$__project" ]; then
        if ! __cwd=$(__hook_cwd "$__payload"); then
            return 0
        fi

        if ! __project=$(__hook_project_root "$__cwd"); then
            __project=""
        fi

        if [ -z "$__project" ]; then
            if ! __project=$(__hook_workspace_match "$__payload" "$__cwd"); then
                return 0
            fi
        fi
    fi

    if [ "${__project%/}" = "$GRAPHIFY_ROOT" ]; then
        __hook_log "PROJECT: $__project is the watched root itself, skipped"
        return 0
    fi

    if ! __hook_under_root "$__project"; then
        __hook_log "PROJECT: $__project is outside $GRAPHIFY_ROOT, skipped"
        return 0
    fi

    if ! __out=$(__hook_out_dir "$__project"); then
        return 0
    fi

    __graph_file="$__out/graphify-out/graph.json"

    if [ -f "$__graph_file" ]; then
        __graph_existed=1
    fi

    if ! __hook_needs_rebuild "$__project" "$__graph_file"; then
        return 0
    fi

    __hook_log "PROJECT: $__project has a stale graph, rebuilding"

    __hook_rebuild "$__project"
    __return=$?

    if [ "$__return" = "0" ]; then
        __hook_log "GRAPH: rebuilt for $__project"
        return 0
    fi

    if [ "$__graph_existed" = "1" ]; then
        __message="The graphify graph of $__project could not be refreshed (status $__return) and the graph on disk is stale. Do not trust the graph: tell the user the refresh failed and work without it."
        if ! __hook_answer "deny" "$__message"; then
            __hook_log "ANSWER: the denial could not be printed, the call proceeds"
        fi
        return 0
    fi

    __message="No graphify graph existed for $__project and none could be built (status $__return). The call is allowed, but the graph tools will find nothing."
    if ! __hook_answer "" "$__message"; then
        __hook_log "ANSWER: the notice could not be printed, the call proceeds"
    fi

    return 0
}

main "$@"
