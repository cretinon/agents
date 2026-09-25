# ToDo

Open points for this repository. Nothing listed here blocks the current setup: the
`stordata` bridge works and ECA exposes its 4 tools. Items are ordered by value.

## 1. Fixed — first code-review follow-up (`stordata_bridge.py`)

All 12 findings of the first `code_reviewer` pass (2026-09-25; secret scan clean) are fixed:

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
  cached, `--auth-timeout + 60` while an interactive login may run) and it now fails loudly on an
  error frame that does not echo the discover id.
- **Startup no longer blocks the handshake**: the stdin pump starts first, so ECA's `initialize`
  (answered locally) is served immediately while the remote session — which may be waiting for
  the browser login — opens in parallel; remote requests wait on a ready event.
- **`eca/client.json` no longer carries the dead plain-HTTP redirect**
  (`http://127.0.0.1:3334/callback`); that authorization server only ever accepted the HTTPS
  localhost callback. *Takes effect in the published document only after commit + push + CDN
  purge.*
- **Dead constant `LOCAL_METHODS` removed**, and the client-wide `mcp-protocol-version` header is
  gone: the per-request metadata headers are the only ones sent, so they no longer leak into the
  authorization server's discovery/token calls.
- **The bridged `initialize` clamps the protocol version** it echoes to a known 2025-era list
  (logging the mismatch) instead of claiming whatever a client asks for.
- **Shutdown no longer blocks the event loop** (`asyncio.to_thread`).
- **`_login_only` tolerates a result-less frame** (`.get("result") or {}`), and exception groups
  are flattened so a transport failure reports its real cause instead of a `TaskGroup` wrapper.

Verified by an offline self-test covering all 12 fixes plus live runs: `--login-only`, a full
stdio session (`initialize`, `tools/list`, `tools/call`, `ping`, plus garbage on stdin) and
startup against an unreachable endpoint (handshake answered in ~300 ms, failure reported instead
of hanging). The harness lives outside the repository (`/tmp/ECA/bridge_selftest.py`).

## 2. Missing capabilities (bridge)

- **Server → client notifications and subscriptions**: the `2026-07-28` wire delivers them on
  a `subscriptions/listen` stream, which the bridge does not open at all — so
  `notifications/tools/list_changed` (and every other server notification, including
  progress/logging) never reaches ECA. ECA therefore only sees the state captured at startup.
- **Reconnection / resumption / keep-alive**: no `Last-Event-ID` resumption, no ping interval,
  no retry policy if the remote session is dropped mid-call.
- **Progress surface**: no `progress_callback` plumbing towards ECA, so long tool calls show no
  progress.
- **CLI / packaging**: the bridge is StorM-specific (URL, scope and CIMD URL are defaults), has
  no console entry point (`pyproject.toml`), no `--status` (token expiry), no `--logout` (delete
  the token store), and no unit tests in the repository — verification today is `--login-only`
  plus the manual stdio snippet in `README.md`.
- **Multi-server**: only `stordata` is bridged; making the script generic (any CIMD-protected
  remote MCP server) would let it cover future remote servers too.

## 3. Operational

- **Re-authentication is interactive and alarming**: the access token lives ~24 h and refreshes
  automatically, but any refresh failure (or revoked grant) makes the bridge open a browser and
  present the **self-signed certificate warning** again; until a human completes it, ECA has no
  `stordata` tools and only a `failed` server row to show. A clearer ECA-side error, a startup
  notification, or a headless/service path is missing.
- **CDN staleness**: every edit of `eca/client.json` requires commit + push **and** a manual
  jsDelivr purge (the authorization server fetches the document live, so a stale copy makes the
  redirect matching fail). Automation (hook or script) is missing.
- **Certificate lifetime**: the self-signed callback certificate is generated with a 10-year
  validity and is never rotated or validated.
- **Token revocation**: no documented way to revoke the stored grant server-side.

## 4. Repository

- **No lint/CI for the Python bridge**: nothing runs `ruff`/`black`/`mypy`, and the repository
  has no test runner for it (the `shell`/`mcp` gate deliberately does not apply here).
