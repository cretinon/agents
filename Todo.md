# ToDo

Open points for this repository. Nothing listed here blocks the current setup: the
`stordata` bridge works and ECA exposes its 4 tools. Items are ordered by value.

## 1. Fix before the next `stordata_bridge.py` change — code review findings

Reported by the `code_reviewer` sub-agent (2026-09-25), reviewing the working tree that
first made the bridge work. Secret scan came back clean.

| # | Item | Severity | Where |
|---|---|---|---|
| 1 | The OAuth callback binds **all** interfaces (`::` with `IPV6_V6ONLY=0`) instead of loopback: a host on the LAN can race the redirect with a bogus `code`/`state` and make the login unrecoverable (`state`+PKCE still prevent token theft). Bind `127.0.0.1` + `::1`, or reject non-loopback `client_address`. | Major | `_DualStackServer`, `_make_callback_server` |
| 2 | `notifications/cancelled` is dropped, so a cancelled tool call keeps running remotely and its late response arrives as an orphan id. The SDK transport turns that frame into aborting the in-flight POST, but it never sees it. Drop only `notifications/initialized`; forward cancellations. | Major | `_local_response` |
| 3 | One non-object JSON line on stdin (`123`, `[]`, `"x"`) raises `AttributeError` and kills the bridge (ECA sees the server die). Guard with `isinstance(payload, dict)`. | Major | `_pump_stdin` |
| 4 | `--verbose` writes the authorization `code`/`state` (query string) to the log: log the path only. | Minor | `_CallbackHandler.log_message` |
| 5 | File modes are enforced only at creation: a pre-existing key file is never re-`chmod`ed and an existing state dir is not tightened to `0700`. | Minor | `_ensure_certificate`, `FileTokenStorage._write` |
| 6 | `_discover` has no timeout and ignores error frames whose id differs (e.g. `id: null` parse errors): ECA would hang at startup with nothing on stdout. Add a `wait_for` and fail loudly. | Minor | `_discover` |
| 7 | Cold start blocks: `server/discover` (which can trigger the interactive login) completes **before** the stdio pump starts, so ECA's `initialize` waits for the browser. Read stdin concurrently and queue, or fail with an explicit message. | Minor | `_run_stdio` |
| 8 | `client.json` still lists the unused plain-HTTP redirect `http://127.0.0.1:3334/callback` (the bridge uses the HTTPS one). | Minor | `eca/client.json` |
| 9 | Dead constant `LOCAL_METHODS`; `_http_headers` also sends `mcp-protocol-version` to the authorization server's discovery/token calls (harmless, but scope it to the MCP endpoint). | Nit | bridge |
| 10 | The bridged `initialize` echoes whatever protocol version the client asked for, while upstream traffic is always stamped `2026-07-28`: clamp/validate it. | Nit | `legacy_initialize_result` |
| 11 | `server.shutdown()` / `server_close()` block the event loop (use `asyncio.to_thread`). | Nit | `_build_callback_handler` |
| 12 | `_login_only` can raise `KeyError` on a frame with neither `result` nor `error` (use `.get("result") or {}`). | Nit | `_login_only` |

## 2. Missing capabilities (bridge)

- **Server → client notifications and subscriptions**: the `2026-07-28` wire delivers them on
  a `subscriptions/listen` stream, which the bridge does not open at all — so
  `notifications/tools/list_changed` (and every other server notification, including
  progress/logging) never reaches ECA. ECA therefore only sees the state captured at startup.
- **Reconnection / resumption / keep-alive**: no `Last-Event-ID` resumption, no `ping`
  interval, no retry policy if the remote session is dropped mid-call.
- **Progress and cancellation surface**: no `progress_callback` plumbing towards ECA, so long
  tool calls show no progress.
- **CLI / packaging**: the bridge is StorM-specific (URL, scope, CIMD URL are defaults), has no
  console entry point (`pyproject.toml`), no `--status` (token expiry), no `--logout` (delete
  the token store), and no unit tests — verification today is `--login-only` plus the manual
  stdio snippet in `README.md`.
- **Multi-server**: only `stordata` is bridged; making the script generic (any CIMD-protected
  remote MCP server) would let it cover future remote servers too.

## 3. Operational

- **Re-authentication is interactive and alarming**: the access token lives ~24 h and refreshes
  automatically, but any refresh failure (or revoked grant) makes the bridge open a browser and
  present the **self-signed certificate warning** again; until a human completes it, ECA has no
  `stordata` tools and only a `failed` server row to show. A clearer ECA-side error, a
  notification on startup, or a headless/service path is missing.
- **CDN staleness**: every edit of `eca/client.json` requires commit + push **and** a manual
  jsDelivr purge (the authorization server fetches the document live, so a stale copy makes the
  redirect matching fail). Automation (hook or script) is missing.
- **Certificate lifetime**: the self-signed callback certificate is generated with a 10-year
  validity and never rotated or validated.
- **Token revocation**: no documented way to revoke the stored grant server-side.

## 4. Repository

- **No `.gitignore`**: `__pycache__/`, `*.pyc`, `.venv/` can be committed by accident (an
  untracked `__pycache__` already appeared twice during this work).
- **No lint/CI for the Python bridge**: nothing runs `ruff`/`black`/`mypy`, and the repository
  has no test runner for it (the `shell`/`mcp` gate deliberately does not apply here).
