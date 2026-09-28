#!/root/.local/share/eca/mcp-bridge-venv/bin/python
"""Offline regression tests for the stordata bridge's OAuth token handling.

Run them with the bridge's own virtualenv -- no network, no credentials, and no live bridge
run: the live token store is only *read* (a read-only invariant check that is skipped when
the file is absent), never consumed.

    /root/.local/share/eca/mcp-bridge-venv/bin/python eca/bridges/test_stordata_bridge.py

Covered: the absolute-expiry detection (`_jwt_expiry`, `_access_token_expired`), the token
store's behaviour when the access token has expired (the fix for the bridge asking for an
interactive login after its 24 h token expired), and the OAuth metadata priming plus its
cache. Exit code 0 means every check passed; `<n> passed, <m> failed` is printed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stordata_bridge as b
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.oauth2 import OAuthContext
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken

LIVE_STORE = Path("/root/.local/state/eca/mcp-bridge/stordata.json")
REDIRECT_URI = ["https://localhost:19284/auth/callback"]
CHECKS = {"pass": 0, "fail": 0}


def check(label: str, got: object, want: object) -> None:
    ok = got == want
    CHECKS["pass" if ok else "fail"] += 1
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def jwt(exp: int) -> str:
    """A fake (unsigned) JWT: only its `exp` claim is ever read."""

    def segment(claims: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment({'exp': exp})}.sig"


def provider_args(url: str) -> SimpleNamespace:
    """The subset of parsed args the provider and the priming need."""
    scratch = Path(tempfile.gettempdir()) / "eca-bridge-test"
    return SimpleNamespace(
        url=url,
        scope="openid mcp",
        callback_host="localhost",
        callback_port=1,
        callback_path="/auth/callback",
        tls=False,
        cert_file=scratch / "probe.pem",
        key_file=scratch / "probe.key",
        open_browser=False,
        client_metadata_url="https://example.invalid/client.json",
        discover_timeout=30.0,
    )


def context_for(store: b.FileTokenStorage) -> OAuthContext:
    """An `OAuthContext` the way the SDK builds it, to read its own verdicts."""
    return OAuthContext(
        server_url="https://services.stordata.fr/mcp",
        client_metadata=OAuthClientMetadata(redirect_uris=REDIRECT_URI),
        storage=store,
        redirect_handler=None,
        callback_handler=None,
    )


# --------------------------------------------------------------------------- #
# Expiry detection
# --------------------------------------------------------------------------- #


def expiry_checks() -> None:
    now = int(time.time())
    check("_jwt_expiry reads the exp claim", b._jwt_expiry(jwt(1700000000)), 1700000000.0)
    check("_jwt_expiry on garbage", b._jwt_expiry("not-a-jwt"), None)
    check("_jwt_expiry on an empty token", b._jwt_expiry(""), None)

    expired = OAuthToken(access_token=jwt(now - 10), expires_in=86400, refresh_token="r")
    fresh = OAuthToken(access_token=jwt(now + 86400), expires_in=86400, refresh_token="r")
    check("expired, no saved_at (JWT fallback)", b._access_token_expired(expired, None), True)
    check("fresh, no saved_at (JWT fallback)", b._access_token_expired(fresh, None), False)
    check("expired, saved_at 2 days ago", b._access_token_expired(expired, time.time() - 172800), True)
    check("fresh, saved_at now", b._access_token_expired(fresh, time.time()), False)
    check("stale saved_at cannot mask an expired JWT", b._access_token_expired(expired, time.time()), True)
    check(
        "the margin refreshes 30s before the expiry",
        b._access_token_expired(OAuthToken(access_token=jwt(now + 30), expires_in=86400), None),
        True,
    )
    check(
        "no expiry record at all -> trusted (pre-existing behaviour)",
        b._access_token_expired(OAuthToken(access_token="opaque"), None),
        False,
    )


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #


async def storage_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tokens.json"
        store = b.FileTokenStorage(path)
        now = int(time.time())

        fresh = OAuthToken(access_token=jwt(now + 86400), expires_in=86400, refresh_token="r")
        await store.set_tokens(fresh)
        raw = json.loads(path.read_text())
        check("set_tokens persists the tokens", isinstance(raw.get("tokens"), dict), True)
        check("set_tokens persists the receive time", isinstance(raw.get("tokens_saved_at"), float), True)
        reloaded = await store.get_tokens()
        check("a fresh access token is handed back as-is", reloaded.access_token, fresh.access_token)
        check("the refresh token is kept", reloaded.refresh_token, "r")
        check("the store stays mode 0600", oct(path.stat().st_mode)[-3:], "600")

        # the incident: an expired access token must be handed back without it, so that the
        # SDK refreshes silently instead of demanding an interactive login
        await store.set_tokens(OAuthToken(access_token=jwt(now - 10), expires_in=86400, refresh_token="r"))
        handed = await store.get_tokens()
        check("an expired access token is dropped", handed.access_token, "")
        check("but its refresh token survives", handed.refresh_token, "r")
        check("the doctored tokens still validate", OAuthToken.model_validate(handed.model_dump(mode="json")).access_token, "")

        context = context_for(store)
        context.current_tokens = handed
        context.client_info = await store.get_client_info()
        check("expired -> is_token_valid() is False", context.is_token_valid(), False)
        check("expired, no stored client info -> the SDK cannot refresh", context.can_refresh_token(), False)

        await store.set_client_info(
            OAuthClientInformationFull(
                client_id="https://example.test/client.json",
                redirect_uris=REDIRECT_URI,
                token_endpoint_auth_method="none",
            )
        )
        context.client_info = await store.get_client_info()
        check("expired + client info -> is_token_valid() is False", context.is_token_valid(), False)
        check("expired + client info -> the SDK refreshes instead of prompting", context.can_refresh_token(), True)


# --------------------------------------------------------------------------- #
# Metadata priming (offline: the discovery URL is unreachable by construction)
# --------------------------------------------------------------------------- #


async def priming_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = b.FileTokenStorage(Path(tmp) / "tokens.json")
        args = provider_args("https://127.0.0.1:1/mcp")

        provider, cache = await b._prepare_oauth_provider(args, store)
        check("an unreachable discovery does not raise", isinstance(provider, OAuthClientProvider), True)
        check("an unreachable discovery primes nothing", cache, None)
        check("the provider stays usable without metadata", provider.context.oauth_metadata, None)

        primed = b._OAuthMetadataCache(
            oauth_metadata=OAuthMetadata(
                issuer="https://example.test",
                authorization_endpoint="https://example.test/auth",
                token_endpoint="https://example.test/token",
            ),
            auth_server_url="https://example.test",
        )
        provider, reused = await b._prepare_oauth_provider(args, store, primed)
        check("a cached discovery is reused as-is", reused is primed, True)
        check("the cached metadata reaches the provider", provider.context.oauth_metadata is primed.oauth_metadata, True)
        check("the cached authorization server reaches the provider", provider.context.auth_server_url, "https://example.test")


# --------------------------------------------------------------------------- #
# The live store, when it exists (read-only)
# --------------------------------------------------------------------------- #


async def live_store_checks() -> None:
    if not LIVE_STORE.exists():
        print(f"[skip] no live token store at {LIVE_STORE}")
        return
    store = b.FileTokenStorage(LIVE_STORE)
    tokens = await store.get_tokens()
    info = await store.get_client_info()
    state = "expired" if not tokens.access_token else "fresh"
    print(f"live store: access_token={state}, refresh_token={'present' if tokens.refresh_token else 'ABSENT'}")
    context = context_for(store)
    context.current_tokens = tokens
    context.client_info = info
    # whatever the state, the bridge must never be driven into the interactive login while a
    # refresh is possible: (is_token_valid, can_refresh_token) is (False, True) once expired
    if state == "expired":
        check("live: expired -> refresh path", (context.is_token_valid(), context.can_refresh_token()), (False, True))
    else:
        check("live: fresh -> token reused, no refresh", (context.is_token_valid(), context.can_refresh_token()), (True, True))


def main() -> int:
    for expiry_check in (expiry_checks,):
        expiry_check()
    asyncio.run(storage_checks())
    asyncio.run(priming_checks())
    asyncio.run(live_store_checks())
    print(f"\n{CHECKS['pass']} passed, {CHECKS['fail']} failed")
    return 1 if CHECKS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
