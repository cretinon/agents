#!/root/.local/share/eca/mcp-bridge-venv/bin/python
"""stdio <-> streamable-HTTP MCP bridge for the StorM (stordata) MCP server.

ECA only speaks stdio to a local MCP server, and its own HTTP MCP client cannot
complete this authorization server's metadata discovery (the RFC 8414 well-known
URL answers a 302 whose "Found. Redirecting to ..." body it parses as JSON).
This bridge does the HTTP + OAuth side with the official `mcp` Python SDK and
relays raw JSON-RPC messages between ECA's stdin/stdout and the remote session.

Authentication uses CIMD (OAuth Client ID Metadata Document): the `client_id`
sent to the authorization server is the HTTPS URL of the client metadata
document hosted for this bridge (`--client-metadata-url`), so no dynamic client
registration is required -- which is the only supported path for this server.

stdout is the MCP channel and carries JSON-RPC messages only; every log line
goes to stderr.

Usage:
    stordata_bridge.py                 # stdio MCP server (ECA uses this)
    stordata_bridge.py --login-only    # one-off sign-in + tools/list check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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
from mcp.shared.message import SessionMessage

try:  # the MCP-tuned httpx2 client factory (timeouts suited to long-lived streams)
    from mcp.shared._httpx_utils import create_mcp_http_client
except ImportError:  # pragma: no cover - re-exported by the transport module
    from mcp.client.streamable_http import create_mcp_http_client

LOG = logging.getLogger("stordata-bridge")

DEFAULT_URL = "https://services.stordata.fr/mcp"
DEFAULT_CLIENT_METADATA_URL = "https://cdn.jsdelivr.net/gh/cretinon/agents@main/eca/client.json"
DEFAULT_SCOPE = "openid mcp"
DEFAULT_CALLBACK_PORT = 3334
DEFAULT_CALLBACK_PATH = "/callback"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "eca" / "mcp-bridge"


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
# OAuth handlers
# --------------------------------------------------------------------------- #


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler capturing the authorization code redirect."""

    server_version = "stordata-bridge"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path != self.server.callback_path:  # type: ignore[attr-defined]
            self.send_error(404, "not the OAuth callback path")
            return

        params = parse_qs(url.query)
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if error:
            self.server.result = RuntimeError(  # type: ignore[attr-defined]
                f"{error}: {params.get('error_description', [''])[0]}"
            )
        elif not code:
            self.server.result = RuntimeError("callback carried no authorization code")  # type: ignore[attr-defined]
        else:
            self.server.result = AuthorizationCodeResult(code=code, state=state)  # type: ignore[attr-defined]

        body = (
            b"<html><body><h3>Authorization received</h3>"
            b"<p>You can close this tab and return to ECA.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:  # silence the default stderr logs
        LOG.debug("callback server: %s", fmt % args)


async def _redirect_handler(url: str, open_browser: bool) -> None:
    LOG.info("authorization required, open this URL:\n\n    %s\n", url)
    if open_browser:
        try:
            await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:  # pragma: no cover - desktop dependent
            LOG.warning("could not open a browser automatically: %s", exc)


def _build_callback_handler(port: int, path: str, timeout: float):
    async def callback_handler() -> AuthorizationCodeResult:
        server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
        server.callback_path = path  # type: ignore[attr-defined]
        server.result = None  # type: ignore[attr-defined]
        server.done = threading.Event()  # type: ignore[attr-defined]

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        LOG.info("waiting for the OAuth redirect on http://127.0.0.1:%d%s", port, path)
        try:
            done = await asyncio.to_thread(server.done.wait, timeout)
            if not done:
                raise TimeoutError(f"no authorization redirect within {timeout:.0f}s")
            result = server.result
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]
        finally:
            server.shutdown()
            server.server_close()

    return callback_handler


def _build_oauth_provider(args: argparse.Namespace, storage: FileTokenStorage) -> OAuthClientProvider:
    redirect_uri = f"http://127.0.0.1:{args.callback_port}{args.callback_path}"
    return OAuthClientProvider(
        server_url=args.url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],
            client_name="ECA StorM bridge",
            scope=args.scope,
            token_endpoint_auth_method="none",
            application_type="native",
        ),
        storage=storage,
        redirect_handler=lambda url: _redirect_handler(url, args.open_browser),
        callback_handler=_build_callback_handler(args.callback_port, args.callback_path, args.auth_timeout),
        client_metadata_url=args.client_metadata_url,
    )


# --------------------------------------------------------------------------- #
# Relay
# --------------------------------------------------------------------------- #


def _write_message(message: types.JSONRPCMessage) -> None:
    sys.stdout.write(message.model_dump_json(by_alias=True, exclude_none=True) + "\n")
    sys.stdout.flush()


async def _send_json(write, payload: dict) -> None:
    """Send a plain JSON-RPC dict (as used by the hand-run helpers below)."""
    message = types.jsonrpc_message_adapter.validate_json(json.dumps(payload), by_name=False)
    await write.send(SessionMessage(message))


async def _pump_stdin_to_remote(write) -> None:
    """Forward every JSON-RPC line read on stdin to the remote session."""
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            LOG.info("stdin closed, stopping")
            return
        text = line.strip()
        if not text:
            continue
        try:
            message = types.jsonrpc_message_adapter.validate_json(text, by_name=False)
        except Exception as exc:
            LOG.error("ignoring unparsable stdin message: %s", exc)
            continue
        await write.send(SessionMessage(message))


async def _pump_remote_to_stdout(read) -> None:
    """Forward every message received from the remote session to stdout."""
    async for item in read:
        if isinstance(item, Exception):
            LOG.error("transport error: %s", item)
            continue
        if not isinstance(item, SessionMessage):
            LOG.error("unexpected item from transport: %r", item)
            continue
        _write_message(item.message)


async def _login_only(args: argparse.Namespace) -> int:
    """Sign in once, list the remote tools, then exit (setup/verification helper)."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": args.protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "stordata-bridge", "version": "0.0.1"},
        },
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    notified = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}

    storage = FileTokenStorage(args.state_dir / "stordata.json")
    provider = _build_oauth_provider(args, storage)

    async with create_mcp_http_client(auth=provider) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (read, write):
            await _send_json(write, init)
            names: list[str] = []
            while True:
                item = await read.receive()
                if isinstance(item, Exception):
                    raise item
                root = item.message.root
                if getattr(root, "id", None) == 1:
                    LOG.info("initialize ok")
                    await _send_json(write, notified)
                    await _send_json(write, listed)
                elif getattr(root, "id", None) == 2:
                    result = getattr(root, "result", None) or {}
                    tools = getattr(result, "tools", None) or []
                    names = [tool.name for tool in tools]
                    break
    print(json.dumps({"url": args.url, "tools": names}, indent=2), file=sys.stderr)
    return 0


async def _run_stdio(args: argparse.Namespace) -> int:
    storage = FileTokenStorage(args.state_dir / "stordata.json")
    provider = _build_oauth_provider(args, storage)
    async with create_mcp_http_client(auth=provider) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as (read, write):
            stdin_task = asyncio.create_task(_pump_stdin_to_remote(write))
            remote_task = asyncio.create_task(_pump_remote_to_stdout(read))
            done, pending = await asyncio.wait(
                {stdin_task, remote_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
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
    parser.add_argument("--auth-timeout", type=float, default=300.0, help="seconds to wait for the redirect")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="where tokens are stored")
    parser.add_argument("--protocol-version", default="2025-06-18", help="protocol version sent with initialize")
    parser.add_argument("--no-browser", action="store_true", help="only print the authorization URL")
    parser.add_argument("--login-only", action="store_true", help="sign in, list tools, exit")
    parser.add_argument("--verbose", action="store_true", help="debug logs on stderr")
    args = parser.parse_args()
    args.open_browser = not args.no_browser

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
        LOG.error("fatal: %s", exc)
        if args.verbose:
            LOG.exception("details")
        return 1


if __name__ == "__main__":
    sys.exit(main())
