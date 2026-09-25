# ToDo

Open points for this repository. Nothing listed here blocks the current setup: the
`stordata` bridge works and ECA exposes its 4 tools. Items are ordered by value.

## 1. Fixed / implemented

### Code-review follow-up (2026-09-25) — `stordata_bridge.py`

All 12 findings of the first `code_reviewer` pass (secret scan clean) are fixed:

- **OAuth callback is loopback-only** — binds `127.0.0.1` *and* `::1` (never a wildcard, since
  the browser may resolve `localhost` to either) and rejects any non-loopback `client_address`
  with `403`, closing the LAN race on the redirect.
- **`notifications/cancelled` is forwarded** to the transport (which turns it into aborting the
  in-flight POST), so a cancelled tool call stops remotely instead of returning as an orphan id;
  only the remaining client notifications are dropped.
- **Non-object stdin is rejected safely** (`123`, `[]`, `"x"`, unparsable JSON are logged and
  ignored) — a single bad line can no longer kill the bridge.
- **`--verbose` no longer logs the authorization `code`/`state`**: the callback logs the request
  line without its query string.
- **Permissions are enforced, not assumed**: the token directory is re-`chmod`ed to `0700` and a
  pre-existing private key to `0600` (not only at creation time).
- **`server/discover` cannot hang**: bounded by `--discover-timeout` (short once a token is
  cached, `--auth-timeout + 60` while an interactive login may run) and it fails loudly on an
  error frame that does not echo the discover id.
- **Startup no longer blocks the handshake**: the stdin pump starts first, so ECA's `initialize`
  is served immediately; it now *also* waits for the remote session details before answering
  (so the advertised capabilities and server info are the real ones, not placeholders).
- **`eca/client.json` no longer carries the dead plain-HTTP redirect**
  (`http://127.0.0.1:3334/callback`); that authorization server only ever accepted the HTTPS
  localhost callback.
- **Dead constant `LOCAL_METHODS` removed**, and the client-wide `mcp-protocol-version` header is
  gone: the per-request metadata headers are the only ones sent, so they no longer leak into the
  authorization server's discovery/token calls.
- **The bridged `initialize` clamps the protocol version** it echoes to a known 2025-era list
  (logging the mismatch) instead of claiming whatever a client asks for.
- **Shutdown no longer blocks the event loop** (`asyncio.to_thread`).
- **`_login_only` tolerates a result-less frame** (`.get("result") or {}`), and exception groups
  are flattened so a transport failure reports its real cause instead of a `TaskGroup` wrapper.

### Connection supervision (2026-09-25)

- **Automatic reconnect** with capped exponential backoff (`--reconnect-delay` 1 s →
  `--reconnect-max-delay` 60 s, reset on success; `--max-reconnects` bounds it, `0` = forever).
  The stdio session and ECA's tool list survive an outage/restart; the supervisor re-runs
  `server/discover` and re-arms the same `_RemoteSession` (so ECA's declared capabilities carry
  over).
- **In-flight policy**: read-only methods (`tools/list`, `prompts/*`, `resources/*`,
  `server/discover`, `ping`) are replayed after the reconnect, and requests issued while
  disconnected are queued (`--replay-max`, default 64). Everything else — notably `tools/call` —
  is answered with JSON-RPC `-32603 "bridge: remote connection lost, retry the request"` so
  nothing is executed twice.
- **Idle keep-alive probe**: after `--idle-probe` seconds of inactivity (default 60, `0`
  disables) the bridge sends a `server/discover` whose response is intercepted (never forwarded
  to ECA); a timeout forces a reconnect, so a dead link is noticed before a tool call is.
- **Tool-change notification**: after every reconnect the bridge compares `tools/list` with the
  previous set and emits `notifications/tools/list_changed` when it differs.
- **Resumption is deliberately a no-op for this server**: the `2026-07-28` wire here is
  stateless (no `Mcp-Session-Id`, responses are plain JSON, no SSE stream), so there is no event
  id to resume from. The SDK's own `Last-Event-ID` reconnection stays available for the day a
  stream appears (it is transport-internal); see §2.

Verified by an offline self-test (32 checks) plus an end-to-end test that runs the real bridge
against a local mock MCP server, kills it mid-session and restarts it with a changed tool set
(asserting the error frame, the queued-listing replay, the reconnect and the change
notification), and by live runs against StorM (`--login-only`, a full stdio session, and ECA
reporting the server `running` with its 4 tools). Harnesses live outside the repository:
`/tmp/ECA/bridge_selftest.py`, `/tmp/ECA/mock_mcp_server.py`, `/tmp/ECA/reconnect_test.py`.

## 2. Missing capabilities (bridge)

- **Server → client notifications and subscriptions**: the `2026-07-28` wire delivers them on a
  `subscriptions/listen` stream, which the bridge does not open — so nothing but the
  reconnect-time `tools/list_changed` reaches ECA (no resource/prompt change notifications, no
  server logging, no progress).
- **Progress surface**: no `progress_callback` plumbing towards ECA, so long tool calls show no
  progress.
- **CLI / packaging**: the bridge is StorM-specific (URL, scope and CIMD URL are defaults), has
  no console entry point (`pyproject.toml`), no `--status` (token expiry), no `--logout` (delete
  the token store), and no unit tests inside the repository — verification today is
  `--login-only`, the manual stdio snippet in `README.md` and the harnesses above.
- **Multi-server**: only `stordata` is bridged; making the script generic (any CIMD-protected
  remote MCP server) would let it cover future remote servers too.

## 3. Operational

- **Re-authentication is interactive and alarming**: the access token lives ~24 h and refreshes
  automatically, but any refresh failure (or revoked grant) makes the bridge open a browser and
  present the **self-signed certificate warning** — now also on a *reconnect*, at any time. Until
  a human completes it, ECA has no `stordata` tools (the server row may still show `running`,
  since the bridge process survives). A clearer ECA-side error or a headless/service path is
  missing.
- **CDN staleness**: every edit of `eca/client.json` requires commit + push **and** a manual
  jsDelivr purge (the authorization server fetches the document live, so a stale copy makes the
  redirect matching fail). Automation (hook or script) is missing.
- **Certificate lifetime**: the self-signed callback certificate is generated with a 10-year
  validity and is never rotated or validated.
- **Token revocation**: no documented way to revoke the stored grant server-side.

## 4. Repository

- **No lint/CI for the Python bridge**: nothing runs `ruff`/`black`/`mypy`, and the repository
  has no test runner for it (the `shell`/`mcp` gate deliberately does not apply here). The test
  harnesses above would be the natural seed of a `eca/bridges/tests/` directory.
