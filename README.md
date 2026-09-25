# agents

Shared **ECA agent rules and skills** consumed by the ECA (Editor Code Assistant) configuration.

This repository is not a code library — it is the single source for the agent-facing
guidelines that ECA loads on top of the project-specific documentation (`AGENTS.md` files,
the `shell`/`mcp`/... libraries).

## Layout

```
README.md          This file
rules/             Path-scoped agent rules (loaded per file-path glob)
  shell.md         Rule applied when reading/writing **.sh files
  bats.md          Rule applied when reading/writing **.bats files
  language.md      Global project rule: always respond in English
  AI_dev.md        Global project rule: phased AI development workflow
skills/            Agent skills (loaded by name when the task matches)
  bats/            BATS test-suite management skill (SKILL.md)
eca/               ECA configuration assets
  config.json      The ECA configuration consumed by ~/.config/eca/config.json
  client.json      PUBLIC OAuth (CIMD) client metadata document, served by jsDelivr
  bridges/         stdio <-> HTTP bridges ECA starts as local MCP servers
    stordata_bridge.py   Bridges ECA to https://services.stordata.fr/mcp (OAuth/CIMD)
```

## How ECA loads these

`~/.config/eca/config.json` (a symlink to `$MY_GIT_DIR/agents/eca/config.json`)
references these paths:

```json
{
  "rules": [
    { "path": "~/git/agents/rules/shell.md" },
    { "path": "~/git/agents/rules/bats.md" },
    { "path": "~/git/agents/rules/language.md" },
    { "path": "~/git/agents/rules/AI_dev.md" }
  ],
  "skills": [
    { "path": "~/git/agents/skills/bats" }
  ]
}
```

- **Rules** are applied automatically by ECA based on the file path being read or edited:
  `shell.md` matches `**.sh`, `bats.md` matches `**.bats` (frontmatter `paths` glob).
- **Skills** are loaded on demand when a task matches the skill `description`
  (e.g. the `bats` skill for writing/refactoring `.bats` test files).
- **Rules without a `paths` glob** (e.g. `language.md`, `AI_dev.md`) load as
  project/workspace rules and apply to the whole session regardless of the file
  being touched.

## Conventions

- **YAML frontmatter** is recommended on every rule (`agent`, `paths`, `enforce`) and skill
  (`name`, `description`). `paths` is what makes a rule path-scoped; a rule **without** a
  `paths` glob is loaded as a project/workspace rule (e.g. `language.md`).
- **Enforcement**: `enforce: read, modify` means ECA must surface the rule before both
  reading and editing a matching file.
- **Keep rules aligned with the authoritative project docs**: the real development rules
  live in `${MY_GIT_DIR}/shell/AGENTS.md`; these agent rules restate/enforce subsets of
  them (e.g. tests MUST be run through `my_warp.sh --lib <lib> -b` — developers run only
  the tests they wrote via a filter regex, while the `code_reviewer` sub-agent runs the
  full `-s|-b|-k` gate — never raw `bats`). When the project rules change, update these
  files in the same commit.
- **Testing**: these are markdown assets, not code — the `shell`/`mcp` quality gate
  (`-s`/`-b`/`-k`) does not apply. Validate by re-reading the file and checking ECA loads
  it (`eca-info` skill) after changes.

## StorM (`stordata`) MCP bridge

ECA speaks **stdio** to local MCP servers, while the StorM MCP server
(`https://services.stordata.fr/mcp`) is **remote**, OAuth-protected, and implements the
stateless **`2026-07-28`** MCP revision. `eca/bridges/stordata_bridge.py` bridges the two:
streamable HTTP + OAuth towards StorM (official `mcp` Python SDK), plain stdio JSON-RPC
towards ECA. The remote `initialize` no longer exists, so the bridge answers it locally from
`server/discover`, and it stamps every request with the `mcp-protocol-version` / `mcp-method`
(and, for name-bearing methods, `mcp-name`) headers plus the `_meta` envelope.

Authentication uses **CIMD** (OAuth Client ID Metadata Document): the `client_id` is the
HTTPS URL of `eca/client.json`, because that authorization server offers no dynamic client
registration. Its redirect matching only accepts the HTTPS localhost callback listed in that
document, so the bridge serves its OAuth callback over TLS with a **self-signed certificate**,
bound to the loopback interface only.

### Start everything (first time)

```shell
# 1. the bridge's private virtualenv (kept outside the repo)
python3 -m venv /root/.local/share/eca/mcp-bridge-venv
/root/.local/share/eca/mcp-bridge-venv/bin/pip install mcp

# 2. publish the CIMD document — it is PUBLIC: commit, push, then refresh the CDN cache
#    (after ANY edit of eca/client.json; the authorization server fetches it live)
git -C "$MY_GIT_DIR/agents" add eca/client.json eca/config.json eca/bridges/stordata_bridge.py
git -C "$MY_GIT_DIR/agents" commit -m "…"
git -C "$MY_GIT_DIR/agents" push
#    then purge: https://purge.jsdelivr.net/gh/cretinon/agents@main/eca/client.json

# 3. check what ECA will read: eca/config.json -> mcpServers.stordata
#    command = /root/.local/share/eca/mcp-bridge-venv/bin/python
#    args    = ["$MY_GIT_DIR/agents/eca/bridges/stordata_bridge.py"]
#    (all bridge defaults apply: TLS on, https://localhost:19284/auth/callback, 2026-07-28)

# 4. sign in once: prints the authorization URL (and tries to open a browser),
#    waits on the TLS callback, then caches the tokens
/root/.local/share/eca/mcp-bridge-venv/bin/python \
  "$MY_GIT_DIR/agents/eca/bridges/stordata_bridge.py" --login-only

# 5. restart ECA (or restart the `stordata` server from the MCP settings buffer),
#    then verify it reports the server as running with 4 tools
```

`--login-only` prints the server info and the tool list to **stderr** (`getInventoryModel`,
`getInventoryFields`, `getPerformanceCounters`, `getMetrics`). A quick end-to-end check of the
stdio side (ECA keeps stdin open, so hold it open here too):

```shell
{ printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'; sleep 20; } \
  | /root/.local/share/eca/mcp-bridge-venv/bin/python "$MY_GIT_DIR/agents/eca/bridges/stordata_bridge.py"
```

### Supervision: reconnect, keep-alive and in-flight requests

The bridge keeps ECA connected across remote outages — the stdio session and its tools survive
a dropped connection, a restarted server or a network blip:

| Event | What the bridge does |
|---|---|
| connection lost / remote unreachable | logs it and reconnects with an exponential backoff (`--reconnect-delay`, default 1 s, up to `--reconnect-max-delay`, default 60 s), re-running `server/discover` |
| request in flight when it drops | **read-only** methods (`tools/list`, `prompts/*`, `resources/*`, `server/discover`) are replayed after the reconnect; everything else — notably `tools/call` — is answered with JSON-RPC `-32603` so ECA can retry (no double execution) |
| request issued while disconnected | same rule: read-only ones are queued (up to `--replay-max`, default 64), the rest fail immediately |
| remote tool set changed | after every reconnect the bridge compares `tools/list` and sends `notifications/tools/list_changed` when it differs |
| idle connection | after `--idle-probe` seconds (default 60; `0` disables) the bridge sends a `server/discover` probe; a timeout forces a reconnect, so a dead link is noticed *before* a tool call is |

Retrying is unbounded by default (`--max-reconnects N` bounds it). Because the wire is
stateless — no session id, no event stream — there is nothing to *resume*: `Last-Event-ID`
resumption is plumbed for future streams but is a no-op against this server. When the token has
expired or been revoked, a reconnect triggers the interactive login again (same browser and
self-signed-certificate warning as the first run).

### State (never in this repository)

| Path | Content | Mode |
|---|---|---|
| `/root/.local/state/eca/mcp-bridge/stordata.json` | OAuth tokens + client info | `0600` |
| `/root/.local/state/eca/mcp-bridge/callback-key.pem` | self-signed callback private key | `0600` |
| `/root/.local/state/eca/mcp-bridge/callback-cert.pem` | matching certificate | `0644` |

`eca/client.json` is published on a public CDN: it must never contain a secret.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `method not found` / `404` on `initialize` | the server only speaks `2026-07-28`; the bridge answers `initialize` locally |
| browser: `redirect_uri … does not match` | `eca/client.json` was edited without commit+push (+CDN purge), or it no longer lists `https://localhost:19284/auth/callback` |
| `Missing required header "mcp-protocol-version"` / `"mcp-method"` | a wrong `--protocol-version` is being stamped; keep the default `2026-07-28` |
| browser certificate warning after consent | expected: the callback is served with a self-signed localhost certificate (Advanced → Proceed) |
| ECA shows the server `failed` with 0 tools | run `--login-only` to (re)authenticate, then restart the server in ECA |
| browser sign-in prompt appears again | the token expired or was revoked and the bridge reconnected: complete the login (or run `--login-only` beforehand) |
| ECA keeps the server `running` during an outage | expected: the bridge stays up and replays; check its stderr for `connection lost` / `reconnecting in` |
| no keep-alive traffic wanted | start the bridge with `--idle-probe 0` |
| bridge stopped with `giving up after N failed attempt(s)` | `--max-reconnects` is set; raise it or remove the flag to retry forever |
| need diagnostics | add `--verbose` to the bridge `args` — logs go to stderr, stdout stays the MCP channel |

Open points and known limitations are tracked in `Todo.md`.
