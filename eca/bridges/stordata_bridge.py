#!/root/.local/share/eca/mcp-bridge-venv/bin/python
"""stdio <-> streamable-HTTP MCP bridge for the StorM (stordata) MCP server.

Why a bridge: ECA only speaks stdio to a local MCP server, and its own HTTP MCP
client cannot complete this authorization server's metadata discovery (the RFC
8414 well-known URL answers a 302 whose "Found. Redirecting to ..." body it
parses as JSON). This bridge does the HTTP + OAuth work with the official `mcp`
Python SDK and translates between ECA's stdio JSON-RPC and the remote endpoint.

The remote server implements the 2026-07-28 MCP revision, which is stateless:
`initialize` is retired (answered here locally from `server/discover`), every
request must carry the `mcp-protocol-version` / `mcp-method` (and, for
name-bearing methods, `mcp-name`) HTTP headers, and an `_meta` envelope with the
protocol version, client capabilities and client info. The bridge stamps all of
that; ECA keeps speaking plain stdio JSON-RPC.

Authentication uses CIMD (OAuth Client ID Metadata Document): the `client_id`
sent to the authorization server is the HTTPS URL of the client metadata
document hosted for this bridge (`--client-metadata-url`), because that server
offers no dynamic client registration. Its redirect matching only accepts the
HTTPS localhost callback listed in the document, so the bridge serves its OAuth
callback over TLS with a self-signed certificate, bound to the loopback
interface only.

stdout is the MCP channel and carries JSON-RPC messages only; every log line
goes to stderr.

Usage:
    stordata_bridge.py                 # stdio MCP server (ECA uses this)
    stordata_bridge.py --login-only    # one-off sign-in + tools/list check
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp import types
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.inbound import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    NAME_BEARING_METHODS,
    PROTOCOL_VERSION_META_KEY,
    encode_header_value,
)
from mcp.shared.message import ClientMessageMetadata, SessionMessage

try:  # the MCP-tuned httpx2 client factory (timeouts suited to long-lived streams)
    from mcp.shared._httpx_utils import create_mcp_http_client
except ImportError:  # pragma: no cover - re-exported by the transport module
    from mcp.client.streamable_http import create_mcp_http_client

LOG = logging.getLogger("stordata-bridge")

DEFAULT_URL = "https://services.stordata.fr/mcp"
DEFAULT_CLIENT_METADATA_URL = "https://cdn.jsdelivr.net/gh/cretinon/agents@main/eca/client.json"
DEFAULT_SCOPE = "openid mcp"
# The remote server is 2026-07-28-only (it rejects 2025-06-18 as unsupported).
DEFAULT_PROTOCOL_VERSION = "2026-07-28"
# Versions the bridge is willing to echo to a legacy client; anything else is clamped
# (and logged) because the upstream traffic is always stamped with the modern version.
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_LEGACY_PROTOCOL_VERSION = "2025-06-18"
# This authorization server only accepts the HTTPS localhost callback listed in the
# CIMD document (a plain http://127.0.0.1 one is rejected as "does not match").
DEFAULT_CALLBACK_HOST = "localhost"
DEFAULT_CALLBACK_PORT = 19284
DEFAULT_CALLBACK_PATH = "/auth/callback"
DEFAULT_DISCOVER_TIMEOUT = 30.0
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "eca" / "mcp-bridge"
DISCOVER_REQUEST_ID = "stordata-bridge/discover"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
BRIDGE_CLIENT_INFO = {"name": "stordata-bridge", "version": "0.0.1"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod: permissions are a hardening step, never a fatal error."""
    try:
        os.chmod(path, mode)
    except OSError as exc:  # pragma: no cover - platform dependent
        LOG.debug("could not chmod %s: %s", path, exc)


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


def _describe(exc: BaseException) -> str:
    """Flatten exception groups: the transports wrap their failure in a TaskGroup."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(sub) for sub in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Token storage (the SDK's TokenStorage protocol)
# --------------------------------------------------------------------------- #


class FileTokenStorage:
    """Persist OAuth tokens and client information in a mode-0600 JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _read(self) -> dict:
        try:
            with self.path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("ignoring unreadable token store %s: %s", self.path, exc)
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_quiet(self.path.parent, 0o700)  # also tightens a pre-existing directory
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(data, handle, indent=2)
        tmp.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            data = self._read()
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            data = self._read()
            data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
            self._write(data)


# --------------------------------------------------------------------------- #
# OAuth: loopback callback server, handlers
# --------------------------------------------------------------------------- #


class _CallbackState:
    """State shared by the loopback callback servers (IPv4 and IPv6)."""

    def __init__(self) -> None:
        self.result: AuthorizationCodeResult | RuntimeError | None = None
        self.done = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler capturing the authorization code redirect."""

    server_version = "stordata-bridge"
    server: ThreadingHTTPServer  # set by _make_callback_servers

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        client = self.client_address[0]
        if not _is_loopback(client):
            LOG.warning("rejecting non-loopback callback request from %s", client)
            self.send_error(403, "the OAuth callback is loopback-only")
            return

        url = urlparse(self.path)
        if url.path != self.server.callback_path:  # type: ignore[attr-defined]
            LOG.info("ignoring non-callback request: %s", url.path)
            self.send_error(404, "not the OAuth callback path")
            return

        params = parse_qs(url.query)
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if not code and not error:
            # a browser (or a proxy) may probe the redirect URI before the real
            # redirect arrives -- log it and keep waiting for the authorization code
            LOG.warning("ignoring callback request without code/error: %s", url.path)
            self.send_response(204)
            self.end_headers()
            return

        callback_state: _CallbackState = self.server.state  # type: ignore[attr-defined]
        if error:
            callback_state.result = RuntimeError(
                f"{error}: {params.get('error_description', [''])[0]}"
            )
        else:
            callback_state.result = AuthorizationCodeResult(code=code, state=state)

        body = (
            b"<html><body><h3>Authorization received</h3>"
            b"<p>You can close this tab and return to ECA.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        callback_state.done.set()

    def log_message(self, fmt: str, *args: object) -> None:
        """Log the request line without its query string.

        The query carries the authorization `code` and `state`; `--verbose` must
        not write them to ECA's stderr log.
        """
        status = args[1] if len(args) > 1 else "-"
        LOG.debug("callback server: %s %s -> %s", self.command, urlparse(self.path).path, status)


async def _redirect_handler(url: str, open_browser: bool) -> None:
    LOG.info("authorization required, open this URL:\n\n    %s\n", url)
    if open_browser:
        try:
            await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:  # pragma: no cover - desktop dependent
            LOG.warning("could not open a browser automatically: %s", exc)


def _make_callback_servers(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> list[ThreadingHTTPServer]:
    """Bind the callback on the loopback interface(s) only.

    `localhost` may resolve to `127.0.0.1` or `::1` depending on the browser, so
    both loopback addresses are served -- but never a wildcard address, which
    would let any host on the network race the redirect.
    """
    if host != "localhost":
        return [ThreadingHTTPServer((host, port), handler)]  # explicit host: honour it
    servers: list[ThreadingHTTPServer] = []
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        class _LoopbackServer(ThreadingHTTPServer):
            address_family = family

        try:
            servers.append(_LoopbackServer((address, port), handler))
        except OSError as exc:
            LOG.debug("cannot bind the callback on [%s]:%d: %s", address, port, exc)
    if not servers:
        raise OSError(f"could not bind a loopback callback server on port {port}")
    return servers


def _ensure_certificate(cert_file: Path, key_file: Path) -> None:
    """Create a self-signed localhost certificate on first use (openssl)."""
    cert_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_quiet(cert_file.parent, 0o700)
    if cert_file.exists() and key_file.exists():
        _chmod_quiet(key_file, 0o600)  # also tighten a key created by an older version
        return
    LOG.info("generating a self-signed certificate for localhost in %s", cert_file.parent)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3650", "-nodes",
            "-keyout", str(key_file), "-out", str(cert_file),
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _chmod_quiet(key_file, 0o600)


def _redirect_uri(args: argparse.Namespace) -> str:
    scheme = "https" if args.tls else "http"
    return f"{scheme}://{args.callback_host}:{args.callback_port}{args.callback_path}"


def _build_callback_handler(args: argparse.Namespace):
    async def callback_handler() -> AuthorizationCodeResult:
        servers = _make_callback_servers(args.callback_host, args.callback_port, _CallbackHandler)
        state = _CallbackState()
        if args.tls:
            _ensure_certificate(args.cert_file, args.key_file)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=args.cert_file, keyfile=args.key_file)
            for server in servers:
                server.socket = context.wrap_socket(server.socket, server_side=True)
        for server in servers:
            server.state = state  # type: ignore[attr-defined]
            server.callback_path = args.callback_path  # type: ignore[attr-defined]

        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        LOG.info("waiting for the OAuth redirect on %s (loopback only)", _redirect_uri(args))
        try:
            done = await asyncio.to_thread(state.done.wait, args.auth_timeout)
            if not done:
                raise TimeoutError(f"no authorization redirect within {args.auth_timeout:.0f}s")
            if isinstance(state.result, Exception):
                raise state.result
            return state.result  # type: ignore[return-value]
        finally:
            for server in servers:
                await asyncio.to_thread(server.shutdown)
                server.server_close()

    return callback_handler


def _build_oauth_provider(args: argparse.Namespace, storage: FileTokenStorage) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=args.url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[_redirect_uri(args)],
            client_name="ECA StorM bridge",
            scope=args.scope,
            token_endpoint_auth_method="none",
            application_type="native",
        ),
        storage=storage,
        redirect_handler=lambda url: _redirect_handler(url, args.open_browser),
        callback_handler=_build_callback_handler(args),
        client_metadata_url=args.client_metadata_url,
    )


# --------------------------------------------------------------------------- #
# The 2026-07-28 (stateless) wire: envelope, headers, local methods
# --------------------------------------------------------------------------- #


def _message_to_dict(message: types.JSONRPCMessage) -> dict:
    return message.model_dump(by_alias=True, exclude_none=True)


def _write_message(payload: dict) -> None:
    """stdout is the MCP channel: JSON-RPC only, one message per line."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def _send(write, payload: dict, headers: dict[str, str]) -> None:
    message = types.jsonrpc_message_adapter.validate_json(json.dumps(payload), by_name=False)
    await write.send(SessionMessage(message, ClientMessageMetadata(headers=headers)))


class _RemoteSession:
    """Builds the per-request envelope/headers the remote (modern) wire expects."""

    def __init__(self, args: argparse.Namespace, discover: dict) -> None:
        self.args = args
        self.discover = discover
        self.client_capabilities: dict = {}

    @property
    def server_info(self) -> dict:
        meta = self.discover.get("_meta") or {}
        if isinstance(meta.get(SERVER_INFO_META_KEY), dict):
            return meta[SERVER_INFO_META_KEY]
        for key, value in meta.items():
            if key.endswith("serverInfo") and isinstance(value, dict):
                return value
        return {"name": "stordata", "version": "0.0.0"}

    def envelope_meta(self) -> dict:
        return {
            PROTOCOL_VERSION_META_KEY: self.args.protocol_version,
            CLIENT_CAPABILITIES_META_KEY: self.client_capabilities,
            CLIENT_INFO_META_KEY: BRIDGE_CLIENT_INFO,
        }

    def stamp(self, payload: dict) -> tuple[dict, dict[str, str]]:
        """Add the `_meta` envelope and the routing headers to an outgoing request."""
        method = payload.get("method")
        headers = {"mcp-protocol-version": self.args.protocol_version}
        if not method:
            return payload, headers
        headers["mcp-method"] = method
        params = dict(payload.get("params") or {})
        name_key = NAME_BEARING_METHODS.get(method)
        if name_key and isinstance(params.get(name_key), str):
            headers["mcp-name"] = encode_header_value(params[name_key])
        params["_meta"] = {**self.envelope_meta(), **(params.get("_meta") or {})}
        return {**payload, "params": params}, headers

    def legacy_initialize_result(self, requested_version: str | None) -> dict:
        """Shape a 2025-era initialize result out of the modern `server/discover`."""
        capabilities: dict = {}
        for key, value in (self.discover.get("capabilities") or {}).items():
            if isinstance(value, dict):
                capabilities[key] = dict(value)
                if key in ("tools", "resources", "prompts"):
                    capabilities[key].setdefault("listChanged", False)
        if requested_version in LEGACY_PROTOCOL_VERSIONS:
            protocol_version = requested_version
        else:
            protocol_version = DEFAULT_LEGACY_PROTOCOL_VERSION
            LOG.warning(
                "client asked for protocol version %r; answering %s (the remote speaks only %s)",
                requested_version,
                protocol_version,
                self.args.protocol_version,
            )
        result = {
            "protocolVersion": protocol_version,
            "capabilities": capabilities,
            "serverInfo": self.server_info,
        }
        if self.discover.get("instructions"):
            result["instructions"] = self.discover["instructions"]
        return result


def _local_response(payload: dict, session: _RemoteSession) -> tuple[bool, dict | None]:
    """Answer locally what the modern wire no longer serves; return (handled, response)."""
    method = payload.get("method")
    if method == "initialize":
        params = payload.get("params") or {}
        session.client_capabilities = params.get("capabilities") or {}
        LOG.info(
            "answering initialize locally; remote speaks %s",
            ", ".join(session.discover.get("supportedVersions") or [session.args.protocol_version]),
        )
        return True, {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": session.legacy_initialize_result(params.get("protocolVersion")),
        }
    if method == "ping":
        return True, {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}
    if method == "notifications/cancelled":
        # Forwarded: the SDK transport turns this frame into aborting the in-flight
        # POST (the 2026-07-28 wire has no client->server notification to send).
        return False, None
    if method and method.startswith("notifications/"):
        LOG.debug("dropping client notification %s (the 2026-07-28 wire has none)", method)
        return True, None
    return False, None


# --------------------------------------------------------------------------- #
# Session plumbing and relay
# --------------------------------------------------------------------------- #


async def _discover(write, read, session: _RemoteSession, timeout: float) -> dict:
    """Open the modern session; `timeout` bounds it so startup can never hang silently."""
    payload, headers = session.stamp(
        {"jsonrpc": "2.0", "id": DISCOVER_REQUEST_ID, "method": "server/discover", "params": {}}
    )
    await _send(write, payload, headers)

    async def _await_response() -> dict:
        while True:
            item = await read.receive()
            if isinstance(item, Exception):
                raise item
            data = _message_to_dict(item.message)
            if data.get("id") == DISCOVER_REQUEST_ID:
                return data
            if "error" in data and data.get("id") is None:
                # a parse/dispatch error that does not echo our id: fail instead of looping
                raise RuntimeError(f"server/discover rejected: {data['error']}")
            LOG.debug("ignoring pre-handshake message: %s", data)

    try:
        data = await asyncio.wait_for(_await_response(), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"no server/discover response within {timeout:.0f}s") from None
    if "error" in data:
        raise RuntimeError(f"server/discover failed: {data['error']}")
    return data.get("result") or {}


async def _pump_stdin(session: _RemoteSession, write, ready: asyncio.Event) -> None:
    """Forward ECA's requests to the remote session, stamping the modern envelope."""
    waiting_logged = False
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            LOG.info("stdin closed, stopping")
            return
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            LOG.error("ignoring unparsable stdin message: %s", exc)
            continue
        if not isinstance(payload, dict):
            LOG.error("ignoring non-object stdin message: %.60s", text)
            continue
        try:
            handled, response = _local_response(payload, session)
            if handled:
                if response is not None:
                    _write_message(response)
                continue
            if not ready.is_set():
                if not waiting_logged:
                    LOG.warning(
                        "remote session still opening (first run: complete the browser sign-in, "
                        "or run --login-only once); holding the request"
                    )
                    waiting_logged = True
                await ready.wait()
            stamped, headers = session.stamp(payload)
            await _send(write, stamped, headers)
        except Exception as exc:  # keep the session alive on a bad message
            LOG.error("could not forward %s: %s", payload.get("method"), exc)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": -32603, "message": f"bridge: {exc}"},
                }
            )


async def _pump_remote(read) -> None:
    """Forward every remote message to ECA (stdout)."""
    async for item in read:
        if isinstance(item, Exception):
            LOG.error("transport error: %s", item)
            continue
        _write_message(_message_to_dict(item.message))


async def _discover_timeout(args: argparse.Namespace, storage: FileTokenStorage) -> float:
    """Short bound when a token is cached, generous while an interactive login may run."""
    if await storage.get_tokens():
        return args.discover_timeout
    return args.auth_timeout + 60.0


async def _run_stdio(args: argparse.Namespace) -> int:
    storage = FileTokenStorage(args.state_dir / "stordata.json")
    provider = _build_oauth_provider(args, storage)
    async with create_mcp_http_client(auth=provider) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (read, write):
            session = _RemoteSession(args, discover={})
            ready = asyncio.Event()
            # The stdin pump starts first so ECA's `initialize` (answered locally) never
            # waits for the remote session -- which may need an interactive login.
            stdin_task = asyncio.create_task(_pump_stdin(session, write, ready))
            try:
                session.discover = await _discover(write, read, session, await _discover_timeout(args, storage))
            finally:
                ready.set()
            LOG.info(
                "connected to %s %s (%s)",
                session.server_info.get("name"),
                session.server_info.get("version"),
                ", ".join(session.discover.get("supportedVersions") or []),
            )
            remote_task = asyncio.create_task(_pump_remote(read))
            done, pending = await asyncio.wait({stdin_task, remote_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
    return 0


async def _login_only(args: argparse.Namespace) -> int:
    """Sign in once, discover the server, list its tools, then exit."""
    storage = FileTokenStorage(args.state_dir / "stordata.json")
    provider = _build_oauth_provider(args, storage)
    async with create_mcp_http_client(auth=provider) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (read, write):
            session = _RemoteSession(args, discover={})
            session.discover = await _discover(write, read, session, await _discover_timeout(args, storage))
            payload, headers = session.stamp({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            await _send(write, payload, headers)
            tools: list[str] = []
            while True:
                item = await read.receive()
                if isinstance(item, Exception):
                    raise item
                data = _message_to_dict(item.message)
                if data.get("id") == 1:
                    if "error" in data:
                        raise RuntimeError(f"tools/list failed: {data['error']}")
                    tools = [tool["name"] for tool in ((data.get("result") or {}).get("tools") or [])]
                    break
    print(
        json.dumps({"url": args.url, "serverInfo": session.server_info, "tools": tools}, indent=2),
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"remote MCP endpoint (default: {DEFAULT_URL})")
    parser.add_argument(
        "--client-metadata-url",
        default=DEFAULT_CLIENT_METADATA_URL,
        help="HTTPS URL of the CIMD document used as OAuth client_id",
    )
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help=f"OAuth scope (default: {DEFAULT_SCOPE!r})")
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT, help="OAuth callback port")
    parser.add_argument("--callback-path", default=DEFAULT_CALLBACK_PATH, help="OAuth callback path")
    parser.add_argument(
        "--callback-host", default=DEFAULT_CALLBACK_HOST, help="OAuth callback host (must match the CIMD document)"
    )
    parser.add_argument(
        "--tls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve the OAuth callback over HTTPS with a self-signed localhost certificate",
    )
    parser.add_argument("--cert-file", type=Path, help="callback certificate (default: <state-dir>/callback-cert.pem)")
    parser.add_argument("--key-file", type=Path, help="callback private key (default: <state-dir>/callback-key.pem)")
    parser.add_argument("--auth-timeout", type=float, default=300.0, help="seconds to wait for the redirect")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="where tokens are stored")
    parser.add_argument(
        "--protocol-version",
        default=DEFAULT_PROTOCOL_VERSION,
        help=f"protocol version stamped on remote requests (default: {DEFAULT_PROTOCOL_VERSION})",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=DEFAULT_DISCOVER_TIMEOUT,
        help="seconds to wait for server/discover when a token is already cached",
    )
    parser.add_argument("--no-browser", action="store_true", help="only print the authorization URL")
    parser.add_argument("--login-only", action="store_true", help="sign in, list tools, exit")
    parser.add_argument("--verbose", action="store_true", help="debug logs on stderr")
    args = parser.parse_args()
    args.open_browser = not args.no_browser
    args.cert_file = args.cert_file or (args.state_dir / "callback-cert.pem")
    args.key_file = args.key_file or (args.state_dir / "callback-key.pem")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[stordata-bridge] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        return asyncio.run(_login_only(args) if args.login_only else _run_stdio(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOG.error("fatal: %s", _describe(exc))
        if args.verbose:
            LOG.exception("details")
        return 1


if __name__ == "__main__":
    sys.exit(main())
