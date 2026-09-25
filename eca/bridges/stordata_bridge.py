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

The connection is supervised: if it drops, the bridge reconnects with an
exponential backoff, replays the read-only requests that were in flight (and any
issued while disconnected), answers the rest with a JSON-RPC error, and emits
`notifications/tools/list_changed` to ECA when the remote tool set changed. An
idle keep-alive probe (`--idle-probe`, default 60 s) notices a dead connection
before a tool call does. Because the wire is stateless there is no session or
event stream to resume: `Last-Event-ID` resumption is plumbed for future streams
but is a no-op against this server.

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
import contextlib
import ipaddress
import itertools
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
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
DEFAULT_IDLE_PROBE = 60.0
DEFAULT_PROBE_TIMEOUT = 15.0
DEFAULT_RECONNECT_DELAY = 1.0
DEFAULT_RECONNECT_MAX_DELAY = 60.0
DEFAULT_REPLAY_MAX = 64
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "eca" / "mcp-bridge"
DISCOVER_REQUEST_ID = "stordata-bridge/discover"
TOOLS_REQUEST_ID = "stordata-bridge/tools"
PROBE_ID_PREFIX = "stordata-bridge/probe-"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
BRIDGE_CLIENT_INFO = {"name": "stordata-bridge", "version": "0.0.1"}
# Requests that are safe to send again after a reconnect. `tools/call` is deliberately
# absent: replaying it could execute a side effect twice, so it is failed instead.
REPLAYABLE_METHODS = frozenset(
    {
        "server/discover",
        "tools/list",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "ping",
    }
)


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


def _next_delay(delay: float, maximum: float) -> float:
    """Exponential backoff, capped (`maximum` <= 0 disables the cap)."""
    grown = delay * 2 if delay > 0 else 1.0
    if maximum > 0:
        return min(grown, maximum)
    return grown


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
    LOG.info(
        "AUTHENTICATION REQUIRED: open this URL in a browser (a self-signed-certificate "
        "warning on the callback page is expected):\n\n    %s\n",
        url,
    )
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


def _error_frame(request_id: object, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": message}}


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


async def _local_response(
    payload: dict,
    session: _RemoteSession,
    wait_for_session=None,
) -> tuple[bool, dict | None]:
    """Answer locally what the modern wire no longer serves; return (handled, response)."""
    method = payload.get("method")
    if method == "initialize":
        params = payload.get("params") or {}
        session.client_capabilities = params.get("capabilities") or {}
        if wait_for_session is not None and not session.discover:
            await wait_for_session()
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


async def _request_once(write, read, session: _RemoteSession, method: str, params: dict, request_id: str, timeout: float) -> dict:
    """One request/response exchange, used before the read pump takes over the stream."""
    payload, headers = session.stamp({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    await _send(write, payload, headers)

    async def _await_response() -> dict:
        while True:
            item = await read.receive()
            if isinstance(item, Exception):
                raise item
            data = _message_to_dict(item.message)
            if data.get("id") == request_id:
                return data
            if "error" in data and data.get("id") is None:
                # a parse/dispatch error that does not echo our id: fail instead of looping
                raise RuntimeError(f"{method} rejected: {data['error']}")
            LOG.debug("ignoring message while waiting for %s: %s", method, data)

    try:
        data = await asyncio.wait_for(_await_response(), timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"no {method} response within {timeout:.0f}s") from None
    if "error" in data:
        raise RuntimeError(f"{method} failed: {data['error']}")
    return data.get("result") or {}


async def _discover_timeout(args: argparse.Namespace, storage: FileTokenStorage) -> float:
    """Short bound when a token is cached, generous while an interactive login may run."""
    if await storage.get_tokens():
        return args.discover_timeout
    return args.auth_timeout + 60.0


# --------------------------------------------------------------------------- #
# Connection supervision
# --------------------------------------------------------------------------- #


class _Connection:
    """One live transport: the HTTP client, its streams and the requests in flight."""

    def __init__(self, args: argparse.Namespace, session: _RemoteSession, read, write, stack) -> None:
        self.args = args
        self.session = session
        self.read = read
        self.write = write
        self.stack = stack
        self.inflight: dict[object, dict] = {}
        self.probes: dict[str, asyncio.Future] = {}
        self.dead = asyncio.Event()
        self.pump_task: asyncio.Task | None = None

    async def send(self, payload: dict, *, track: bool = True) -> None:
        stamped, headers = self.session.stamp(payload)
        if track and payload.get("id") is not None:
            self.inflight[payload["id"]] = payload
        await _send(self.write, stamped, headers)

    def start(self) -> asyncio.Task:
        self.pump_task = asyncio.create_task(self.pump())
        return self.pump_task

    def kill(self) -> None:
        """Close the connection from the inside (keep-alive timeout)."""
        if self.pump_task is not None and not self.pump_task.done():
            self.pump_task.cancel()

    async def aclose(self) -> None:
        await self.stack.aclose()

    async def pump(self) -> None:
        """Forward remote messages to ECA until the stream ends or fails."""
        try:
            async for item in self.read:
                if isinstance(item, Exception):
                    raise item
                data = _message_to_dict(item.message)
                request_id = data.get("id")
                if isinstance(request_id, str) and request_id.startswith(PROBE_ID_PREFIX):
                    future = self.probes.get(request_id)
                    if future is not None and not future.done():
                        if "error" in data:
                            future.set_exception(RuntimeError(str(data["error"])))
                        else:
                            future.set_result(data)
                    continue
                if request_id is not None:
                    self.inflight.pop(request_id, None)
                _write_message(data)
        finally:
            self.dead.set()


class _Bridge:
    """Keeps ECA connected across remote outages."""

    def __init__(self, args: argparse.Namespace, storage: FileTokenStorage) -> None:
        self.args = args
        self.storage = storage
        self.session = _RemoteSession(args, discover={})
        self.connection: _Connection | None = None
        self.first_session = asyncio.Event()
        self.replay: list[dict] = []
        self.last_tools: list[str] | None = None
        self.last_activity = time.monotonic()
        self.stop = asyncio.Event()
        self.stdin_done = asyncio.Event()

    # -- stdio side -------------------------------------------------------- #

    def note_activity(self) -> None:
        self.last_activity = time.monotonic()

    async def _wait_first_session(self) -> None:
        LOG.info("holding initialize until the remote session details are known")
        try:
            await asyncio.wait_for(self.first_session.wait(), self.args.auth_timeout + 60.0)
        except asyncio.TimeoutError:
            LOG.warning("remote session details still unknown; answering initialize with what we have")

    def _queue_or_fail(self, payload: dict) -> None:
        """A request that cannot be sent right now (no connection)."""
        method = payload.get("method")
        if method in REPLAYABLE_METHODS and len(self.replay) < self.args.replay_max:
            LOG.info("queuing %s (id=%s) until the connection is back", method, payload.get("id"))
            self.replay.append(payload)
            return
        LOG.warning("failing %s (id=%s): no remote connection", method, payload.get("id"))
        _write_message(_error_frame(payload.get("id"), "bridge: no remote connection, retry the request"))

    async def _read_stdin(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                LOG.info("stdin closed, stopping")
                self.stdin_done.set()
                self.stop.set()
                if self.connection is not None:
                    self.connection.kill()  # release the supervisor's wait on the pump
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
                handled, response = await _local_response(payload, self.session, self._wait_first_session)
                if handled:
                    if response is not None:
                        _write_message(response)
                    continue
                connection = self.connection
                if connection is None or connection.dead.is_set():
                    self._queue_or_fail(payload)
                    continue
                await connection.send(payload)
                self.note_activity()
            except Exception as exc:  # keep the session alive on a bad message
                LOG.error("could not forward %s: %s", payload.get("method"), _describe(exc))
                _write_message(_error_frame(payload.get("id"), f"bridge: {_describe(exc)}"))

    # -- keep-alive -------------------------------------------------------- #

    async def _keepalive(self) -> None:
        if self.args.idle_probe <= 0:
            LOG.info("keep-alive probing disabled")
            return
        counter = itertools.count(1)
        while not self.stop.is_set():
            await asyncio.sleep(min(self.args.idle_probe, 5.0))
            connection = self.connection
            if connection is None or connection.dead.is_set():
                continue
            idle = time.monotonic() - self.last_activity
            if idle < self.args.idle_probe:
                continue
            probe_id = f"{PROBE_ID_PREFIX}{next(counter)}"
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            connection.probes[probe_id] = future
            try:
                LOG.debug("keep-alive probe after %.0fs of inactivity", idle)
                await connection.send(
                    {"jsonrpc": "2.0", "id": probe_id, "method": "server/discover", "params": {}}, track=False
                )
                await asyncio.wait_for(future, self.args.probe_timeout)
                LOG.debug("keep-alive probe answered")
            except Exception as exc:
                LOG.warning("keep-alive probe failed (%s); forcing a reconnect", _describe(exc))
                connection.kill()
            finally:
                connection.probes.pop(probe_id, None)

    # -- remote side ------------------------------------------------------- #

    async def _connect(self) -> _Connection:
        stack = contextlib.AsyncExitStack()
        try:
            provider = _build_oauth_provider(self.args, self.storage)
            client = await stack.enter_async_context(create_mcp_http_client(auth=provider))
            read, write = await stack.enter_async_context(streamable_http_client(self.args.url, http_client=client))
        except BaseException:
            await stack.aclose()
            raise

        connection = _Connection(self.args, self.session, read, write, stack)
        timeout = await _discover_timeout(self.args, self.storage)
        self.session.discover = await _request_once(
            write, read, self.session, "server/discover", {}, DISCOVER_REQUEST_ID, timeout
        )
        self.first_session.set()
        self.note_activity()
        LOG.info(
            "connected to %s %s (%s)",
            self.session.server_info.get("name"),
            self.session.server_info.get("version"),
            ", ".join(self.session.discover.get("supportedVersions") or []),
        )
        self.connection = connection
        await self._after_connect(connection)
        return connection

    async def _after_connect(self, connection: _Connection) -> None:
        """Detect a changed tool set, then replay what could not be sent."""
        try:
            result = await _request_once(
                connection.write, connection.read, self.session, "tools/list", {}, TOOLS_REQUEST_ID,
                self.args.probe_timeout,
            )
            names = sorted(str(tool.get("name")) for tool in (result.get("tools") or []) if tool.get("name"))
        except Exception as exc:
            LOG.warning("could not list the remote tools: %s", _describe(exc))
            names = None
        if names is not None:
            if self.last_tools is not None and names != self.last_tools:
                LOG.info("remote tools changed: %s -> %s", self.last_tools, names)
                _write_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            self.last_tools = names
        pending, self.replay = self.replay, []
        for payload in pending:
            LOG.info("replaying %s (id=%s) after reconnect", payload.get("method"), payload.get("id"))
            try:
                await connection.send(payload)
            except Exception as exc:
                LOG.error("replay of %s failed: %s", payload.get("method"), _describe(exc))
                _write_message(_error_frame(payload.get("id"), f"bridge: replay failed: {_describe(exc)}"))

    def _fail_or_replay(self, connection: _Connection) -> None:
        """Apply the in-flight policy to the requests the drop left unanswered."""
        for request_id, payload in list(connection.inflight.items()):
            connection.inflight.pop(request_id, None)
            method = payload.get("method")
            if method in REPLAYABLE_METHODS and len(self.replay) < self.args.replay_max:
                LOG.info("will replay %s (id=%s) after the reconnect", method, request_id)
                self.replay.append(payload)
            else:
                LOG.warning("failing %s (id=%s): remote connection lost", method, request_id)
                _write_message(
                    _error_frame(request_id, "bridge: remote connection lost, retry the request")
                )

    async def _supervise(self) -> None:
        delay = self.args.reconnect_delay
        failures = 0
        while not self.stop.is_set():
            connection: _Connection | None = None
            try:
                connection = await self._connect()
                delay = self.args.reconnect_delay
                failures = 0
                try:
                    await connection.start()
                except asyncio.CancelledError:
                    if self.stop.is_set():
                        break  # shutdown requested: leave the loop cleanly
                    LOG.warning("connection closed by the bridge (keep-alive timeout)")
                except Exception as exc:
                    LOG.warning("connection lost: %s", _describe(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                LOG.error("connect failed (attempt %d): %s", failures, _describe(exc))
            finally:
                if connection is not None:
                    if connection is self.connection:
                        self.connection = None
                    self._fail_or_replay(connection)
                    with contextlib.suppress(Exception):
                        await connection.aclose()
            if self.stop.is_set():
                break
            if self.args.max_reconnects and failures >= self.args.max_reconnects:
                LOG.error("giving up after %d failed attempt(s) (--max-reconnects)", failures)
                break
            LOG.info("reconnecting in %.1fs", delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            delay = _next_delay(delay, self.args.reconnect_max_delay)

    async def run(self) -> int:
        stdin_task = asyncio.create_task(self._read_stdin())
        keepalive_task = asyncio.create_task(self._keepalive())
        try:
            await self._supervise()
        finally:
            for task in (stdin_task, keepalive_task):
                task.cancel()
            await asyncio.gather(stdin_task, keepalive_task, return_exceptions=True)
            connection = self.connection
            if connection is not None:
                self.connection = None
                with contextlib.suppress(Exception):
                    await connection.aclose()
        return 0


# --------------------------------------------------------------------------- #
# One-off login helper
# --------------------------------------------------------------------------- #


async def _login_only(args: argparse.Namespace) -> int:
    """Sign in once, discover the server, list its tools, then exit."""
    storage = FileTokenStorage(args.state_dir / "stordata.json")
    provider = _build_oauth_provider(args, storage)
    async with create_mcp_http_client(auth=provider) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (read, write):
            session = _RemoteSession(args, discover={})
            timeout = await _discover_timeout(args, storage)
            session.discover = await _request_once(
                write, read, session, "server/discover", {}, DISCOVER_REQUEST_ID, timeout
            )
            result = await _request_once(
                write, read, session, "tools/list", {}, TOOLS_REQUEST_ID, args.probe_timeout
            )
            tools = [tool["name"] for tool in (result.get("tools") or [])]
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
    parser.add_argument(
        "--idle-probe",
        type=float,
        default=DEFAULT_IDLE_PROBE,
        help="send a keep-alive server/discover after this many idle seconds (0 disables)",
    )
    parser.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT, help="keep-alive probe timeout")
    parser.add_argument(
        "--reconnect-delay", type=float, default=DEFAULT_RECONNECT_DELAY, help="first reconnect delay in seconds"
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=DEFAULT_RECONNECT_MAX_DELAY,
        help="cap of the exponential reconnect backoff",
    )
    parser.add_argument(
        "--max-reconnects",
        type=int,
        default=0,
        help="give up after this many consecutive failed connects (0 = retry forever)",
    )
    parser.add_argument(
        "--replay-max",
        type=int,
        default=DEFAULT_REPLAY_MAX,
        help="how many read-only requests may wait for a reconnect",
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
        if args.login_only:
            return asyncio.run(_login_only(args))
        storage = FileTokenStorage(args.state_dir / "stordata.json")
        return asyncio.run(_Bridge(args, storage).run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOG.error("fatal: %s", _describe(exc))
        if args.verbose:
            LOG.exception("details")
        return 1


if __name__ == "__main__":
    sys.exit(main())
