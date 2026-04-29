"""OAuth authentication support for MCP bridge.

Provides:
- Encrypted file-based key-value store for persistent OAuth token storage
- Auth factory: builds the right httpx.Auth for each server config
- Uses FastMCP's OAuth client for the full Authorization Code + PKCE flow

Token files are stored under ``token_dir/<server>/`` (default
``~/.cache/mcp-companion/oauth-tokens``), encrypted with Fernet.
Disk caching can be disabled by passing ``cache_tokens=False`` to
:func:`build_auth`, in which case tokens are kept in memory only
and lost when the bridge restarts.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import logging
import os
import platform
import time
import typing
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import anyio
import enum
import httpx
from cryptography.fernet import Fernet
from fastmcp.client.auth import OAuth
from fastmcp.client.oauth_callback import OAuthCallbackResult, create_oauth_callback_server
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
)
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

logger = logging.getLogger("mcp-bridge")


class _RefreshOutcome(enum.Enum):
    """Result of a proactive token-refresh attempt."""

    SUCCESS = "success"
    NETWORK_ERROR = "network_error"  # transient — token may still be usable
    AUTH_ERROR = "auth_error"  # permanent — need full re-auth


class _ProbeOutcome(enum.Enum):
    """Result of probing Google directly to classify an upstream 401.

    Used to decide whether a 401 from a downstream MCP server (e.g.
    workspace_mcp under EXTERNAL_OAUTH21_PROVIDER) indicates a real
    credential failure or just an outage in the validator's side of the
    network.
    """

    VALID = "valid"  # Google accepts our token — 401 is downstream's problem
    INVALID = "invalid"  # Google rejects our token — real auth failure
    UNKNOWN = "unknown"  # Couldn't tell (network error, 5xx, etc.)


# Endpoint we probe to classify upstream 401s.  Same endpoint workspace_mcp
# uses inside its verify_token, so a 200 here is a strong signal that the
# downstream's verify_token is failing for a non-credential reason.
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_GOOGLE_PROBE_TIMEOUT_SECONDS = 5.0


def _is_network_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a transient network failure.

    These are errors where the token itself is not at fault — the request
    could not be sent or received at all.  We distinguish them from auth
    rejections (4xx) so callers can avoid triggering a full re-auth when
    the network is merely temporarily unavailable.
    """
    # httpx transport-level errors (no TCP connection, DNS failure, etc.)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.TimeoutException)):
        return True
    # anyio / trio / asyncio transport errors bubble up as ConnectionError
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    return False


# When a proactive token refresh fails due to network unavailability we
# grant the existing (expired) token a short grace window so the SDK does
# not immediately fall back to a full browser re-auth.  The next real
# request will trigger a normal refresh attempt through the SDK flow.
_NETWORK_ERROR_GRACE_SECONDS = 300.0  # 5 minutes

# Refresh proactively when the access token is within this many seconds of
# expiry.  Gives requests in flight 2 minutes of headroom against a slow
# refresh round-trip and keeps the SDK's destructive refresh path (which
# wipes the refresh_token on any non-200 response) from firing in normal
# operation — our pre-flight uses ``oauth_metadata.token_endpoint`` directly
# and preserves tokens on transient failures.
_REFRESH_MARGIN_SECONDS = 120.0

# If more than this many seconds have elapsed since the OAuth instance last
# saw activity, treat the next request as a wake-up: force a pre-flight
# refresh regardless of what the local clock says about expiry.  Catches the
# laptop-was-asleep case where wall-clock time advanced past expiry while
# the bridge was suspended.
_WAKE_GAP_SECONDS = 300.0  # 5 minutes


# Default token storage directory (XDG cache convention)
_DEFAULT_TOKEN_DIR = Path.home() / ".cache" / "mcp-companion" / "oauth-tokens"

# Environment variable for encryption key (optional - derived from machine ID if not set)
_ENCRYPTION_KEY_ENV = "MCP_BRIDGE_TOKEN_KEY"


def _get_or_create_encryption_key(token_dir: Path) -> bytes:
    """Get or derive a Fernet encryption key for token storage.

    Key sources (in priority order):
    1. MCP_BRIDGE_TOKEN_KEY environment variable - for explicit key management
    2. Derived from machine ID + username - stable across restarts, unique per user

    **Security Note**: The derived key (option 2) provides obfuscation, not strong
    security. Anyone with read access to your home directory can derive the same
    key. For stronger security, set MCP_BRIDGE_TOKEN_KEY to a securely stored secret.

    The derived key approach is a reasonable default that:
    - Prevents casual inspection of token files
    - Is stable across bridge restarts (no key file to manage)
    - Is unique per user on multi-user systems
    """
    # Check environment first - explicit key takes priority
    if env_key := os.environ.get(_ENCRYPTION_KEY_ENV):
        logger.debug("Using encryption key from %s environment variable", _ENCRYPTION_KEY_ENV)
        return derive_jwt_key(
            low_entropy_material=env_key,
            salt="mcp-bridge-token-encryption",
        )

    # Derive from machine ID + username (stable, unique per user)
    machine_id = platform.node()  # hostname
    username = getpass.getuser()
    material = f"{machine_id}:{username}:mcp-companion-tokens"

    logger.debug("Deriving encryption key from machine ID + username")
    return derive_jwt_key(
        low_entropy_material=material,
        salt="mcp-bridge-token-encryption",
    )


def create_encrypted_store(storage_dir: Path) -> AsyncKeyValue:
    """Create an encrypted file-based key-value store for a server.

    Uses FastMCP's FileTreeStore wrapped with FernetEncryptionWrapper.

    Args:
        storage_dir: Directory for this server's token storage.
            Caller is responsible for constructing the correct path
            (e.g. ``token_dir / server_name``).

    Returns:
        AsyncKeyValue store with encryption.
    """
    encryption_key = _get_or_create_encryption_key(storage_dir.parent)

    storage_dir.mkdir(parents=True, exist_ok=True)

    file_store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )

    # Wrap with encryption - decryption errors are treated as cache misses
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=encryption_key),
        raise_on_decryption_error=False,
    )


_GOOGLE_ACCOUNTS_HOST = "accounts.google.com"


class _RefreshTokenOAuth(OAuth):
    """OAuth subclass that ensures a ``refresh_token`` is issued where possible.

    The standard MCP OAuth client never requests offline access, so providers
    like Google do not issue a ``refresh_token``.  Without one the bridge must
    open a browser every time the short-lived access token expires.

    Currently handles:

    * **Google** (``accounts.google.com``) — injects ``access_type=offline``
      and ``prompt=consent``.  ``access_type=offline`` is a Google-specific
      extension (not part of the OAuth 2.0 spec); other providers use the
      standard ``offline_access`` scope instead and would need separate
      handling.  ``prompt=consent`` forces Google to re-issue a refresh token
      even when the user previously consented.
    """

    # Class-level tracking of active OAuth callback flows per port.
    # When a callback server is already listening on a port (waiting for the
    # user to complete the browser flow), a second request for the same port
    # piggy-backs on the existing server instead of launching a duplicate.
    _active_flows: ClassVar[dict[int, tuple[anyio.Event, OAuthCallbackResult]]] = {}
    _flow_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    async def callback_handler(self) -> tuple[str, str | None]:
        """Handle OAuth callback, reusing an existing server if one is active.

        The parent implementation starts a new uvicorn callback server on
        ``redirect_port`` every time.  If a previous flow is still waiting
        for the user to complete the browser authorisation (e.g. the machine
        was idle), a second ``callback_handler`` call would try to bind the
        same port and crash the bridge (``sys.exit(1)`` from uvicorn).

        This override keeps a class-level registry of active flows keyed by
        port.  The first caller owns the server; any concurrent caller for
        the same port simply waits for the first caller's result.
        """
        port = self.redirect_port

        # Check if another flow is already active on this port.
        async with self._flow_lock:
            existing = self._active_flows.get(port)

        if existing is not None:
            # Another flow is already listening — wait for its result.
            event, result = existing
            logger.info(
                "OAuth callback server already active on port %d — reusing",
                port,
            )
            await event.wait()
            if result.error:
                raise result.error
            return result.code, result.state  # type: ignore[return-value]

        # We are the first caller — set up tracking, then run the real handler.
        result = OAuthCallbackResult()
        event = anyio.Event()
        async with self._flow_lock:
            # Double-check in case another coroutine raced us.
            existing = self._active_flows.get(port)
            if existing is not None:
                # Lost the race — fall through to waiting on the other flow.
                pass
            else:
                self._active_flows[port] = (event, result)

        if existing is not None:
            ev, res = existing
            logger.info(
                "OAuth callback server already active on port %d — reusing (race)",
                port,
            )
            await ev.wait()
            if res.error:
                raise res.error
            return res.code, res.state  # type: ignore[return-value]

        try:
            code, state = await super().callback_handler()
            result.code = code
            result.state = state
            return code, state
        except Exception as exc:
            result.error = exc
            raise
        finally:
            event.set()
            async with self._flow_lock:
                self._active_flows.pop(port, None)

    # ------------------------------------------------------------------
    # Sidecar key for persisting the absolute token expiry timestamp.
    # The upstream SDK only stores ``expires_in`` (relative seconds from
    # issuance) in the ``OAuthToken``.  On reload it naively recalculates
    # the expiry as ``time.time() + expires_in`` which makes stale tokens
    # look fresh.  We store the real absolute expiry alongside the token
    # so we can restore it correctly.
    # ------------------------------------------------------------------
    _EXPIRY_COLLECTION = "mcp-oauth-token-expiry"

    def _expiry_key(self) -> str:
        return f"{self.token_storage_adapter._server_url}/token_expiry"

    async def _save_token_expiry(self) -> None:
        """Persist the current absolute ``token_expiry_time`` to disk."""
        ctx = self.context
        if ctx.token_expiry_time is not None and ctx.token_expiry_time > 0:
            store = self.token_storage_adapter._key_value_store
            await store.put(
                key=self._expiry_key(),
                value={"expires_at": ctx.token_expiry_time},
                collection=self._EXPIRY_COLLECTION,
                ttl=60 * 60 * 24 * 365,  # 1 year, same as token TTL
            )
            remaining = ctx.token_expiry_time - time.time()
            logger.info(
                "Persisted absolute token expiry (%.0fs remaining) for %s",
                remaining,
                self.token_storage_adapter._server_url,
            )
        else:
            logger.info(
                "Skipped persisting token expiry (value=%s) for %s",
                ctx.token_expiry_time,
                self.token_storage_adapter._server_url,
            )

    async def _load_token_expiry(self) -> float | None:
        """Load the persisted absolute expiry timestamp, or ``None``."""
        try:
            store = self.token_storage_adapter._key_value_store
            data = await store.get(
                key=self._expiry_key(),
                collection=self._EXPIRY_COLLECTION,
            )
            if data and "expires_at" in data:
                return float(data["expires_at"])
        except Exception:
            logger.debug("Failed to load persisted token expiry", exc_info=True)
        return None

    async def _proactive_refresh(self) -> _RefreshOutcome:
        """Refresh the access token proactively using the refresh_token.

        This is called during ``_initialize()`` when we have cached tokens
        with a refresh_token but either no sidecar (bootstrap) or the sidecar
        shows the token is expired.  Instead of waiting for the SDK's
        ``async_auth_flow()`` to discover the token is stale (which can be
        unreliable — e.g. the MCP proxy may not validate token expiry), we
        send the refresh request immediately and update the context.

        Returns a :class:`_RefreshOutcome` indicating success, a transient
        network failure, or a permanent auth rejection.

        * ``SUCCESS`` — token refreshed and persisted.
        * ``NETWORK_ERROR`` — the network was unreachable; the existing token
          is preserved and callers should **not** trigger a full re-auth.
        * ``AUTH_ERROR`` — the server rejected the refresh (4xx); the caller
          may fall through to the SDK's normal re-auth flow.
        """
        ctx = self.context

        # We must have oauth_metadata to know the token endpoint.
        # If metadata discovery hasn't happened yet, do it now.
        if not ctx.oauth_metadata:
            try:
                await self._discover_oauth_metadata()
            except Exception as exc:
                if _is_network_error(exc):
                    logger.warning(
                        "Cannot proactively refresh — metadata discovery failed (network error)"
                    )
                    return _RefreshOutcome.NETWORK_ERROR
                logger.warning("Cannot proactively refresh — metadata discovery failed")
                return _RefreshOutcome.AUTH_ERROR

        if not ctx.oauth_metadata or not ctx.oauth_metadata.token_endpoint:
            logger.warning("Cannot proactively refresh — no token endpoint in metadata")
            return _RefreshOutcome.AUTH_ERROR

        token_url = str(ctx.oauth_metadata.token_endpoint)
        if not ctx.current_tokens or not ctx.current_tokens.refresh_token:
            logger.warning("Cannot proactively refresh — no refresh token available")
            return _RefreshOutcome.AUTH_ERROR
        if not ctx.client_info or not ctx.client_info.client_id:
            logger.warning("Cannot proactively refresh — no client_id available")
            return _RefreshOutcome.AUTH_ERROR
        refresh_data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": ctx.current_tokens.refresh_token,
            "client_id": ctx.client_info.client_id,
        }

        # Include resource param if protocol version supports it
        if ctx.should_include_resource_param(ctx.protocol_version):
            refresh_data["resource"] = ctx.get_resource_url()

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        refresh_data, headers = ctx.prepare_token_auth(refresh_data, headers)

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(token_url, data=refresh_data, headers=headers)

            if resp.status_code != 200:
                body = resp.content.decode("utf-8", errors="replace")[:500]
                logger.warning(
                    "Proactive token refresh failed: %s url=%s body=%s",
                    resp.status_code,
                    token_url,
                    body,
                )
                return _RefreshOutcome.AUTH_ERROR

            from mcp.shared.auth import OAuthToken

            token_response = OAuthToken.model_validate_json(resp.content)

            # Google (and some other providers) don't re-issue
            # refresh_token in refresh responses.  Preserve the
            # original so we can keep refreshing silently.
            if not token_response.refresh_token and ctx.current_tokens:
                original_rt = ctx.current_tokens.refresh_token
                if original_rt:
                    token_response = token_response.model_copy(
                        update={"refresh_token": original_rt}
                    )
                    logger.debug("Preserved original refresh_token in proactive refresh")

            ctx.current_tokens = token_response
            ctx.update_token_expiry(token_response)
            await ctx.storage.set_tokens(token_response)

            # Persist the sidecar immediately
            await self._save_token_expiry()

            logger.info(
                "Proactive token refresh succeeded (expires in %.0fs) for %s",
                (ctx.token_expiry_time or 0) - time.time(),
                self.token_storage_adapter._server_url,
            )
            return _RefreshOutcome.SUCCESS
        except Exception as exc:
            if _is_network_error(exc):
                logger.warning(
                    "Proactive token refresh failed — network unreachable (will retry later): %s",
                    exc,
                )
                return _RefreshOutcome.NETWORK_ERROR
            logger.warning("Proactive token refresh error", exc_info=True)
            return _RefreshOutcome.AUTH_ERROR

    async def _apply_network_grace_window(self) -> None:
        """Push token expiry forward when the network is unreachable.

        Preserves the cached (expired) access token so the SDK does not fall
        through to a full browser re-auth.  The next real request will trigger
        a refresh attempt through the normal SDK flow.

        Only extends — if the existing expiry is already further out than the
        grace window (e.g. the wake-up pre-flight fired against a token that
        still has tens of minutes left), we leave it alone instead of taking
        the headroom away on a transient network failure.

        **The synthetic expiry is intentionally NOT persisted to disk.**  The
        grace bridges a transient outage within one bridge-process lifetime;
        persisting would propagate the bumped value across restarts and trick
        ``_initialize()`` into trusting an expiry the access token doesn't
        actually have at the OAuth provider.  The on-disk sidecar always
        reflects the most recent *real* refresh response.
        """
        ctx = self.context
        grace_until = time.time() + _NETWORK_ERROR_GRACE_SECONDS
        existing = ctx.token_expiry_time
        if existing is not None and existing > grace_until:
            logger.debug(
                "Network unreachable but existing token expiry (%.0fs out) is past "
                "the grace window — leaving expiry untouched (server: %s)",
                existing - time.time(),
                self.token_storage_adapter._server_url,
            )
            return
        ctx.token_expiry_time = grace_until
        logger.warning(
            "Network unreachable during init — granting %.0fs grace window "
            "to avoid triggering full re-auth (server: %s).  Not persisted; "
            "next restart will see the real expiry and refresh.",
            _NETWORK_ERROR_GRACE_SECONDS,
            self.token_storage_adapter._server_url,
        )

    async def _initialize(self) -> None:
        """Load cached tokens and ensure OAuth metadata is available.

        Fixes two upstream issues:

        1. **Stale ``expires_in``**: The upstream ``_initialize()`` recalculates
           ``token_expiry_time`` as ``time.time() + expires_in`` — but
           ``expires_in`` is a *relative* value from the original token response
           (e.g. 3600 s).  When loaded from disk hours later the token appears
           freshly minted, so ``is_token_valid()`` returns ``True`` and the SDK
           sends the expired access token instead of refreshing first.  We
           restore the correct absolute expiry from a sidecar key on disk.

        2. **Missing OAuth metadata**: The upstream ``_initialize()`` restores
           tokens and client info from disk but **not** the OAuth AS metadata.
           Without it the SDK falls back to ``urljoin(server_url, "/token")``
           for refresh — which 404s when the MCP server is a proxy.  We perform
           a lightweight metadata discovery so ``_get_token_endpoint()`` uses
           the real provider token endpoint.
        """
        await super()._initialize()

        ctx = self.context

        # Discover OAuth metadata first — we need it before any refresh
        # attempt so ``_proactive_refresh()`` can reach the real token
        # endpoint.
        if ctx.current_tokens and not ctx.oauth_metadata:
            try:
                await self._discover_oauth_metadata()
            except Exception:
                logger.debug(
                    "OAuth metadata discovery failed during init — will be resolved on next 401",
                    exc_info=True,
                )

        # Restore the correct absolute token expiry from our sidecar store.
        # The parent ``_initialize()`` wrongly recalculates it from the
        # relative ``expires_in``, making expired tokens appear fresh.
        if ctx.current_tokens:
            stored_expiry = await self._load_token_expiry()
            if stored_expiry is not None:
                ctx.token_expiry_time = stored_expiry
                remaining = stored_expiry - time.time()
                if remaining > 0:
                    logger.info(
                        "Restored absolute token expiry (%.0fs remaining)",
                        remaining,
                    )
                elif ctx.can_refresh_token():
                    logger.info(
                        "Restored absolute token expiry (expired %.0fs ago — refreshing)",
                        -remaining,
                    )
                    outcome = await self._proactive_refresh()
                    if outcome == _RefreshOutcome.NETWORK_ERROR:
                        await self._apply_network_grace_window()
                else:
                    logger.info(
                        "Restored absolute token expiry (expired %.0fs ago — no refresh token)",
                        -remaining,
                    )
            elif ctx.can_refresh_token():
                # No sidecar yet (first run after fix, or cleared cache).
                # Proactively refresh so we get a fresh access token AND a
                # real absolute expiry that we can persist.
                logger.info("No persisted token expiry — proactively refreshing")
                outcome = await self._proactive_refresh()
                if outcome == _RefreshOutcome.NETWORK_ERROR:
                    await self._apply_network_grace_window()

    def _delegated_validator_host(self) -> str | None:
        """The host of the upstream MCP server's stated authorization server,
        when it differs from the MCP server's own host.

        This is the PRM-level signal: ``.well-known/oauth-protected-resource``
        carries an ``authorization_servers`` list, and a server *delegates*
        validation when that list points somewhere other than itself.

        * workspace_mcp under ``EXTERNAL_OAUTH21_PROVIDER`` advertises
          ``["https://accounts.google.com"]`` while the server itself runs at
          ``localhost:8002`` → returns ``"accounts.google.com"``.
        * ClickUp advertises ``["https://mcp.clickup.com"]`` while the server
          itself runs at ``mcp.clickup.com`` → returns ``None`` (self-validating).

        Returning ``None`` means the upstream is its own OAuth server: its
        401 is authoritative, no probe makes sense.
        """
        prm = self.context.protected_resource_metadata
        if not prm or not prm.authorization_servers:
            return None
        from urllib.parse import urlparse  # local — only needed here

        server_host = (urlparse(str(self.context.server_url)).hostname or "").lower()
        if not server_host:
            return None
        # A server can list multiple AS URLs; if any are external we treat
        # the upstream as delegating.  Pick the first external one — the SDK
        # will have picked authorization_servers[0] for OASM discovery, so
        # that's the validator actually in play.
        for as_url in prm.authorization_servers:
            as_host = (urlparse(str(as_url)).hostname or "").lower()
            if as_host and as_host != server_host:
                return as_host
        return None

    def _third_party_probe_url(self) -> str | None:
        """URL to probe the delegated validator with our access token, or None.

        Returns a userinfo-style endpoint when the validator is a provider we
        know how to ask directly (currently Google).  Returns ``None`` when
        either there is no delegated validator (PRM lists the server itself
        as the AS) or the third party is one we don't know how to probe —
        in which case the 401 handler falls back to the safe-default
        "treat as transient" behaviour rather than guessing.
        """
        validator_host = self._delegated_validator_host()
        if validator_host is None:
            return None
        if "googleapis.com" in validator_host or "google.com" in validator_host:
            return _GOOGLE_USERINFO_URL
        # Other delegated-validator providers can be added here as we learn
        # their userinfo / introspection endpoints.
        return None

    def _has_third_party_validator(self) -> bool:
        """``True`` iff the upstream MCP server's PRM lists an AS on a different host."""
        return self._delegated_validator_host() is not None

    async def _probe_token_at(self, probe_url: str) -> _ProbeOutcome:
        """Probe a third-party validator with our access token.

        Used to classify upstream 401s: 200 means the third party still
        accepts our token (so the 401 came from the upstream's own
        validation infrastructure being briefly unable to reach it); 401
        means the token is genuinely revoked/expired; anything else is
        ambiguous.

        Cost: one ~200ms HTTP request, paid only when an upstream 401
        actually happens and we have a known probe URL.
        """
        ctx = self.context
        if not ctx.current_tokens or not ctx.current_tokens.access_token:
            return _ProbeOutcome.UNKNOWN

        token = ctx.current_tokens.access_token
        try:
            async with httpx.AsyncClient(
                timeout=_GOOGLE_PROBE_TIMEOUT_SECONDS
            ) as http:
                resp = await http.get(
                    probe_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as exc:
            if _is_network_error(exc):
                logger.debug(
                    "Validator probe (%s) network-failed: %s — treating as UNKNOWN",
                    probe_url,
                    exc,
                )
            else:
                logger.warning("Validator probe (%s) error", probe_url, exc_info=True)
            return _ProbeOutcome.UNKNOWN

        if resp.status_code == 200:
            return _ProbeOutcome.VALID
        if resp.status_code == 401:
            return _ProbeOutcome.INVALID
        # 5xx, 403 (rate-limited or similar) — ambiguous.
        logger.debug(
            "Validator probe (%s) returned %d — treating as UNKNOWN",
            probe_url,
            resp.status_code,
        )
        return _ProbeOutcome.UNKNOWN

    async def _preflight_refresh_if_needed(self) -> None:
        """Refresh the access token *before* the SDK sees it expired.

        Three triggers:

        * **Margin** — token has less than ``_REFRESH_MARGIN_SECONDS`` of life
          left.  Refreshing here gives concurrent requests a fresh token and
          keeps the SDK's normal flow on the happy path.
        * **Wake-up** — wall-clock has advanced more than ``_WAKE_GAP_SECONDS``
          since the last successful auth flow.  Catches the laptop-was-asleep
          case where the access token expired during suspend and the network
          may also be reassociating.
        * **First request** — the OAuth instance has never seen activity yet
          (``_last_seen_at is None``).  This is the bridge-just-started case:
          the on-disk expiry could be stale (e.g. by a synthetic grace value
          set in a previous session), and we have no live signal about how
          long the wall clock has advanced since the token was issued.  Cost
          is one extra refresh on bridge startup; it's the only way to be
          sure the persisted expiry hasn't been doctored by an earlier grace
          window or pre-empted by external token revocation.

        On a transient network failure we apply a short grace window so the
        SDK doesn't immediately try to refresh again (which would hit the
        same network problem and, on non-200 from its fallback URL, wipe the
        refresh_token).  On auth failure we leave the token alone so the
        SDK's inline 401 path can drive a clean re-auth.
        """
        ctx = self.context
        if not ctx.can_refresh_token() or ctx.token_expiry_time is None:
            return

        now = time.time()
        remaining = ctx.token_expiry_time - now
        last_seen = getattr(self, "_last_seen_at", None)
        first_request = last_seen is None
        wake_detected = (
            first_request or (now - last_seen) > _WAKE_GAP_SECONDS
        )

        if not wake_detected and remaining >= _REFRESH_MARGIN_SECONDS:
            return

        logger.info(
            "Pre-flight refresh: remaining=%.0fs margin=%.0fs wake=%s "
            "(first_request=%s)",
            remaining,
            _REFRESH_MARGIN_SECONDS,
            wake_detected,
            first_request,
        )
        outcome = await self._proactive_refresh()
        if outcome == _RefreshOutcome.NETWORK_ERROR:
            # Existing token preserved; bump expiry so the SDK doesn't pile a
            # second (failing) refresh attempt on top of the same outage.
            await self._apply_network_grace_window()
        # SUCCESS: tokens updated, SDK will see fresh state.
        # AUTH_ERROR: tokens left as-is so the SDK's inline 401 path can run.

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> typing.AsyncGenerator[httpx.Request, httpx.Response]:
        """Wrap the parent auth flow to persist the absolute token expiry.

        Adds a pre-flight refresh (margin + wake-up heuristic) so the SDK's
        destructive refresh path rarely fires.  After the parent flow
        completes the ``token_expiry_time`` may have been updated by a
        refresh or re-auth — we persist it so subsequent ``_initialize()``
        calls can restore it, and we record the completion timestamp so the
        next request can detect a long sleep gap.
        """
        ctx = self.context

        # Pre-flight: refresh proactively when within margin or after wake.
        # Done before the diagnostic logging below so the snapshot reflects
        # the post-refresh state where applicable.
        await self._preflight_refresh_if_needed()

        tv = ctx.is_token_valid()
        cr = ctx.can_refresh_token()
        exp = ctx.token_expiry_time
        now = time.time()
        remaining = (exp - now) if exp else None
        has_access = bool(ctx.current_tokens and ctx.current_tokens.access_token)
        has_refresh = bool(ctx.current_tokens and ctx.current_tokens.refresh_token)
        has_ci = bool(ctx.client_info)

        if not tv:
            logger.info(
                "Token NOT valid before auth flow: "
                "can_refresh=%s expiry=%s remaining=%.0fs "
                "has_access=%s has_refresh=%s has_client_info=%s",
                cr,
                exp,
                remaining or 0,
                has_access,
                has_refresh,
                has_ci,
            )
        else:
            logger.debug(
                "Token valid before auth flow: remaining=%.0fs",
                remaining or 0,
            )

        # Snapshot refresh_token before the SDK flow — Google (and some
        # other providers) don't re-issue it in refresh responses, but
        # the SDK replaces current_tokens wholesale, losing it.
        original_refresh_token = ctx.current_tokens.refresh_token if ctx.current_tokens else None

        try:
            flow = super().async_auth_flow(request)
            request_out = await flow.__anext__()
            cycle = 0
            while True:
                response = yield request_out
                cycle += 1
                logger.debug(
                    "Auth flow cycle %d: %s %s → %d",
                    cycle,
                    request_out.method,
                    request_out.url,
                    response.status_code,
                )

                # Classify the *first* 401 from the upstream MCP server.
                #
                # Decision tree:
                #
                # * No third-party validator (server host == token-endpoint
                #   host, e.g. clickup): the upstream IS the OAuth server,
                #   its 401 is authoritative.  Fall through to the SDK's
                #   default — full re-auth via the inline 401 path.
                #
                # * Third-party validator that we know how to probe (Google):
                #   ask the validator directly.
                #     200 → token is fine; the 401 is downstream's fault
                #       (typically validator unreachable from downstream's
                #       side).  Propagate as transient; no popup.
                #     401 → token is genuinely revoked/expired.  Fall through
                #       to SDK so the user gets a deliberate browser flow.
                #     network/5xx → ambiguous; propagate as transient.
                #
                # * Third-party validator we don't know how to probe: assume
                #   transient and propagate.  We'd rather miss an
                #   auto-recovery than pop browsers we can't classify;
                #   :MCPToggleServer is always the manual recovery path.
                if (
                    cycle == 1
                    and response.status_code == 401
                    and ctx.current_tokens
                    and ctx.current_tokens.access_token
                    and ctx.current_tokens.refresh_token
                    and self._has_third_party_validator()
                ):
                    probe_url = self._third_party_probe_url()
                    if probe_url is None:
                        logger.warning(
                            "Upstream %s returned 401; the validator is a "
                            "third party we don't know how to probe — "
                            "treating as transient and propagating to caller. "
                            "Use :MCPToggleServer to force re-authentication "
                            "if this persists.",
                            request.url,
                        )
                        await flow.aclose()
                        return

                    probe = await self._probe_token_at(probe_url)
                    if probe == _ProbeOutcome.INVALID:
                        logger.warning(
                            "Upstream %s returned 401 and the third-party "
                            "validator (%s) rejected our token — letting SDK "
                            "drive full re-auth.",
                            request.url,
                            probe_url,
                        )
                        # Fall through: let the SDK process the 401 normally.
                    else:
                        if probe == _ProbeOutcome.VALID:
                            reason = (
                                f"third-party validator ({probe_url}) accepts "
                                "our token; 401 is downstream's validator "
                                "problem (likely transient)"
                            )
                        else:
                            reason = (
                                f"third-party probe ({probe_url}) inconclusive; "
                                "treating as transient"
                            )
                        logger.warning(
                            "Upstream %s returned 401 — %s. Propagating to "
                            "caller without triggering full re-auth.  Use "
                            ":MCPToggleServer to force re-authentication if "
                            "this persists.",
                            request.url,
                            reason,
                        )
                        await flow.aclose()
                        return

                try:
                    request_out = await flow.asend(response)
                except StopAsyncIteration:
                    break

            # Restore refresh_token if the SDK lost it during a token refresh.
            if (
                original_refresh_token
                and ctx.current_tokens
                and not ctx.current_tokens.refresh_token
            ):
                ctx.current_tokens = ctx.current_tokens.model_copy(
                    update={"refresh_token": original_refresh_token}
                )
                await ctx.storage.set_tokens(ctx.current_tokens)
                logger.info("Preserved refresh_token after SDK token refresh")
        finally:
            # Wake-detection heartbeat — set even on error paths so a request
            # that fails doesn't make the *next* request misclassify the gap
            # as a wake-up.
            self._last_seen_at = time.time()
            try:
                await self._save_token_expiry()
            except Exception:
                logger.debug("Failed to persist token expiry", exc_info=True)

    async def _discover_oauth_metadata(self) -> None:
        """Fetch protected-resource + OAuth AS metadata for the server."""
        ctx = self.context
        server_url = str(ctx.server_url)

        async with httpx.AsyncClient() as http:
            # 1. Discover protected resource metadata
            prm_urls = build_protected_resource_metadata_discovery_urls(
                www_auth_url=None,
                server_url=server_url,
            )
            prm = None
            for url in prm_urls:
                try:
                    resp = await http.send(create_oauth_metadata_request(url))
                    prm = await handle_protected_resource_response(resp)
                    if prm:
                        break
                except Exception as exc:
                    if _is_network_error(exc):
                        raise
                    continue

            if not prm or not prm.authorization_servers:
                logger.debug(
                    "No protected-resource metadata or authorization servers found for %s",
                    server_url,
                )
                return

            auth_server = str(prm.authorization_servers[0])
            ctx.protected_resource_metadata = prm
            ctx.auth_server_url = auth_server

            # 2. Discover OAuth authorization-server metadata
            as_urls = build_oauth_authorization_server_metadata_discovery_urls(
                auth_server_url=auth_server,
                server_url=server_url,
            )
            for url in as_urls:
                try:
                    resp = await http.send(create_oauth_metadata_request(url))
                    ok, metadata = await handle_auth_metadata_response(resp)
                    if ok and metadata:
                        ctx.oauth_metadata = metadata
                        logger.info(
                            "Discovered OAuth metadata for '%s': token_endpoint=%s",
                            server_url,
                            metadata.token_endpoint,
                        )
                        return
                except Exception as exc:
                    if _is_network_error(exc):
                        raise
                    continue

            logger.debug(
                "Could not discover OAuth AS metadata for auth server %s",
                auth_server,
            )

    async def redirect_handler(self, authorization_url: str) -> None:
        parsed = urlparse(authorization_url)
        if _GOOGLE_ACCOUNTS_HOST in parsed.netloc:
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.setdefault("access_type", ["offline"])
            params.setdefault("prompt", ["consent"])
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorization_url = urlunparse(parsed._replace(query=new_query))
            logger.debug(
                "Injected access_type=offline into Google authorization URL for refresh token"
            )
        await super().redirect_handler(authorization_url)


class _BearerAuth(httpx.Auth):
    """Simple static Bearer token authentication."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def build_auth(
    server_name: str,
    *,
    auth_config: dict[str, Any] | str | None,
    server_url: str | None = None,
    token_dir: Path | None = None,
    cache_tokens: bool = True,
) -> httpx.Auth | None:
    """Build an ``httpx.Auth`` from a server's auth configuration.

    Supports three modes:

    1. ``auth: "oauth"`` — OAuth 2.1 Authorization Code + PKCE via FastMCP's
       OAuth client, with file-based token persistence.
    2. ``auth: {"bearer": "<token>"}`` — Static bearer token.
    3. ``auth: {"oauth": {scopes: [...], client_id: "...", ...}}`` —
       OAuth with pre-registered client or explicit scopes.

    Token caching:
    - ``cache_tokens=True`` (default): tokens persisted to ``token_dir/<server>/``
      (default ``~/.cache/mcp-companion/oauth-tokens``).  Tokens survive bridge
      restarts and are refreshed automatically.
    - ``cache_tokens=False``: tokens kept in memory only.  The OAuth browser flow
      will be triggered on every bridge restart.

    The ``cache_tokens`` flag can also be set per-server inside the auth dict:
    ``auth: {oauth: {cache_tokens: false}}``.  Per-server setting overrides the
    global flag passed here.

    Returns ``None`` if no auth is configured.
    """
    if auth_config is None:
        return None

    base_dir = (token_dir or _DEFAULT_TOKEN_DIR) / server_name

    # Simple "oauth" string → default OAuth flow
    if auth_config == "oauth":
        if not server_url:
            raise ValueError(f"Server '{server_name}': auth='oauth' requires a URL")
        return _build_oauth(
            server_name=server_name,
            server_url=server_url,
            base_dir=base_dir,
            cache_tokens=cache_tokens,
        )

    if not isinstance(auth_config, dict):
        raise ValueError(
            f"Server '{server_name}': auth must be 'oauth', "
            f"{{'bearer': '...'}}, or {{'oauth': {{...}}}}"
        )

    # {"bearer": "token-value"}
    if "bearer" in auth_config:
        token = auth_config["bearer"]
        if not isinstance(token, str):
            raise ValueError(f"Server '{server_name}': bearer token must be a string")
        return _BearerAuth(token)

    # {"oauth": {scopes: [...], client_id: "...", ...}}
    if "oauth" in auth_config:
        if not server_url:
            raise ValueError(f"Server '{server_name}': OAuth auth requires a URL")
        oauth_opts = auth_config["oauth"]
        if not isinstance(oauth_opts, dict):
            raise ValueError(f"Server '{server_name}': auth.oauth must be a dict")
        # Per-server cache_tokens overrides the global flag
        effective_cache = oauth_opts.get("cache_tokens", cache_tokens)
        if not isinstance(effective_cache, bool):
            effective_cache = bool(effective_cache)
        return _build_oauth(
            server_name=server_name,
            server_url=server_url,
            base_dir=base_dir,
            scopes=oauth_opts.get("scopes"),
            client_id=oauth_opts.get("client_id"),
            client_secret=oauth_opts.get("client_secret"),
            client_metadata_url=oauth_opts.get("client_metadata_url"),
            callback_port=oauth_opts.get("callback_port"),
            cache_tokens=effective_cache,
        )

    raise ValueError(f"Server '{server_name}': unrecognized auth keys: {set(auth_config.keys())}")


def _build_oauth(
    *,
    server_name: str,
    server_url: str,
    base_dir: Path,
    scopes: str | list[str] | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    client_metadata_url: str | None = None,
    callback_port: int | None = None,
    cache_tokens: bool = True,
) -> _RefreshTokenOAuth:
    """Construct a FastMCP ``OAuth`` provider.

    When *cache_tokens* is ``True`` (default) an encrypted file store is
    created at *base_dir* and passed as ``token_storage``.  Tokens are persisted
    to disk with Fernet encryption and survive bridge restarts.

    When *cache_tokens* is ``False`` ``token_storage=None`` is passed, which
    tells FastMCP to use an in-memory store.  Tokens are lost on restart and the
    OAuth browser flow will run again on next startup.

    Returns a :class:`_RefreshTokenOAuth` instance which automatically injects
    ``access_type=offline`` for Google authorization URLs so that a
    ``refresh_token`` is included in the response.
    """
    scope_str: str | None = None
    if isinstance(scopes, list):
        scope_str = " ".join(scopes)
    elif isinstance(scopes, str):
        scope_str = scopes

    storage: AsyncKeyValue | None = None
    if cache_tokens:
        storage = create_encrypted_store(base_dir)
        logger.info(
            "Configuring OAuth for server '%s' with encrypted disk token cache at %s",
            server_name,
            base_dir,
        )
    else:
        logger.info(
            "Configuring OAuth for server '%s' with in-memory token storage (disk cache disabled)",
            server_name,
        )

    return _RefreshTokenOAuth(
        mcp_url=server_url,
        scopes=scope_str,
        client_name=f"mcp-companion ({server_name})",
        token_storage=storage,
        client_id=client_id,
        client_secret=client_secret,
        client_metadata_url=client_metadata_url,
        callback_port=callback_port,
    )


def has_cached_oauth_token(
    server_name: str,
    token_dir: Path | None = None,
) -> bool:
    """Check whether *server_name* has an OAuth token cached on disk.

    This is a **filesystem-level** check — it looks for any non-empty files
    inside the server's token directory.  It avoids touching private FastMCP
    internals so it's safe against upstream changes.

    Returns ``True`` when at least one token file exists (we don't validate
    expiry or encryption — the OAuth flow handles refresh).
    """
    base_dir = (token_dir or _DEFAULT_TOKEN_DIR) / server_name
    if not base_dir.is_dir():
        return False
    # Any non-empty file inside the token dir counts
    return any(f.is_file() and f.stat().st_size > 0 for f in base_dir.rglob("*"))


def clear_oauth_cache(
    server_name: str,
    token_dir: Path | str,
) -> bool:
    """Clear all cached OAuth data for a server.

    This should be called when we detect that the OAuth server has lost
    its client registration (e.g., server restarted and cached client_id
    is no longer valid). Clearing the cache forces a fresh OAuth flow
    with new client registration on next attempt.

    Returns True if any files were deleted.
    """
    import shutil

    token_dir = Path(token_dir) if isinstance(token_dir, str) else token_dir
    server_cache_dir = token_dir / server_name

    if not server_cache_dir.exists():
        logger.debug("No cache directory for server '%s' at %s", server_name, server_cache_dir)
        return False

    try:
        # Remove entire server cache directory (tokens + client registration)
        shutil.rmtree(server_cache_dir)
        logger.info(
            "Cleared OAuth cache for server '%s' at %s",
            server_name,
            server_cache_dir,
        )
        return True
    except Exception as e:
        logger.warning("Failed to clear OAuth cache for server '%s': %s", server_name, e)
        return False


def is_stale_client_error(error: Exception) -> bool:
    """Check if an error indicates stale OAuth client registration.

    Returns True if the error suggests the OAuth server has lost the
    dynamic client registration (e.g., server restarted). In this case,
    clearing the token cache and retrying with fresh registration may help.
    """
    error_str = str(error).lower()
    stale_indicators = [
        "unregistered client",
        "invalid_client",
        "client not found",
        "client_id",
        "invalid client",
        "unknown client",
    ]
    return any(indicator in error_str for indicator in stale_indicators)
