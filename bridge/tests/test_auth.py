"""Tests for mcp-bridge authentication module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mcp_bridge.auth import (
    _NETWORK_ERROR_GRACE_SECONDS,
    _REFRESH_MARGIN_SECONDS,
    _WAKE_GAP_SECONDS,
    _RefreshOutcome,
    _is_network_error,
    _BearerAuth,
    build_auth,
    create_encrypted_store,
)


# ── create_encrypted_store ─────────────────────────────────────────


class TestEncryptedStore:
    """Unit tests for encrypted file-based key-value persistence."""

    @pytest.fixture
    def store(self, tmp_path: Path):
        return create_encrypted_store(tmp_path / "test-server")

    @pytest.mark.anyio
    async def test_get_missing_key(self, store) -> None:
        assert await store.get(key="missing", collection="col") is None

    @pytest.mark.anyio
    async def test_roundtrip(self, store) -> None:
        await store.put(key="k", value={"a": 1, "b": "two"}, collection="col")
        result = await store.get(key="k", collection="col")
        assert result == {"a": 1, "b": "two"}

    @pytest.mark.anyio
    async def test_delete_existing(self, store) -> None:
        await store.put(key="k", value={"x": 1}, collection="col")
        deleted = await store.delete(key="k", collection="col")
        assert deleted is True
        assert await store.get(key="k", collection="col") is None

    @pytest.mark.anyio
    async def test_delete_missing(self, store) -> None:
        deleted = await store.delete(key="missing", collection="col")
        assert deleted is False

    @pytest.mark.anyio
    async def test_multiple_collections_independent(self, store) -> None:
        await store.put(key="k", value={"v": 1}, collection="col1")
        await store.put(key="k", value={"v": 2}, collection="col2")
        assert (await store.get(key="k", collection="col1")) == {"v": 1}
        assert (await store.get(key="k", collection="col2")) == {"v": 2}

    @pytest.mark.anyio
    async def test_key_with_special_chars(self, store) -> None:
        """Keys containing slashes/colons work via sanitization."""
        key = "http://localhost:8002/mcp/tokens"
        await store.put(key=key, value={"tok": "abc"}, collection="mcp-oauth-token")
        result = await store.get(key=key, collection="mcp-oauth-token")
        assert result == {"tok": "abc"}

    def test_derives_encryption_key_from_machine_id(self, tmp_path: Path) -> None:
        """Encryption key is derived deterministically from machine ID + username."""
        # Create two stores - they should use the same derived key
        store1 = create_encrypted_store(tmp_path / "srv1")
        store2 = create_encrypted_store(tmp_path / "srv2")
        # No .key file should be created (key is derived, not stored)
        key_file = tmp_path / ".key"
        assert not key_file.exists()


# ── _BearerAuth ────────────────────────────────────────────────────


class TestBearerAuth:
    """Unit tests for static bearer token auth."""

    def test_injects_header(self) -> None:
        auth = _BearerAuth("my-token")
        request = httpx.Request("GET", "http://example.com/api")
        flow = auth.auth_flow(request)
        modified = next(flow)
        assert modified.headers["Authorization"] == "Bearer my-token"


# ── build_auth ─────────────────────────────────────────────────────


class TestBuildAuth:
    """Tests for the auth factory function."""

    def test_none_returns_none(self) -> None:
        assert build_auth("srv", auth_config=None) is None

    def test_bearer_dict(self) -> None:
        result = build_auth("srv", auth_config={"bearer": "tok123"})
        assert isinstance(result, _BearerAuth)
        request = httpx.Request("GET", "http://example.com")
        flow = result.auth_flow(request)
        modified = next(flow)
        assert modified.headers["Authorization"] == "Bearer tok123"

    def test_bearer_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="bearer token must be a string"):
            build_auth("srv", auth_config={"bearer": 12345})

    def test_invalid_string_auth(self) -> None:
        """Only ``"oauth"`` is a valid string — but requires a URL."""
        with pytest.raises(ValueError, match="requires a URL"):
            build_auth("srv", auth_config="oauth", server_url=None)

    def test_invalid_dict_keys(self) -> None:
        with pytest.raises(ValueError, match="unrecognized auth keys"):
            build_auth("srv", auth_config={"unknown": True})

    def test_invalid_type(self) -> None:
        with pytest.raises(ValueError, match="auth must be"):
            build_auth("srv", auth_config=42)  # type: ignore[arg-type]

    def test_oauth_string_requires_url(self) -> None:
        with pytest.raises(ValueError, match="requires a URL"):
            build_auth("srv", auth_config="oauth")

    def test_oauth_dict_requires_url(self) -> None:
        with pytest.raises(ValueError, match="requires a URL"):
            build_auth("srv", auth_config={"oauth": {"scopes": ["read"]}})

    def test_oauth_dict_non_dict_opts(self) -> None:
        with pytest.raises(ValueError, match="auth.oauth must be a dict"):
            build_auth(
                "srv",
                auth_config={"oauth": "invalid"},
                server_url="http://example.com",
            )

    def test_oauth_string_returns_fastmcp_oauth(self, tmp_path: Path) -> None:
        """``auth: "oauth"`` creates a FastMCP OAuth provider."""
        from fastmcp.client.auth import OAuth

        result = build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        assert isinstance(result, OAuth)

    def test_oauth_dict_returns_fastmcp_oauth(self, tmp_path: Path) -> None:
        """``auth: {"oauth": {...}}`` creates a FastMCP OAuth provider."""
        from fastmcp.client.auth import OAuth

        result = build_auth(
            "srv",
            auth_config={"oauth": {"scopes": ["read", "write"]}},
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        assert isinstance(result, OAuth)

    def test_oauth_uses_encrypted_storage(self, tmp_path: Path) -> None:
        """OAuth provider is configured with encrypted storage at the right path."""
        result = build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        # The store directory should be created under token_dir/server_name
        expected_dir = tmp_path / "srv"
        assert expected_dir.exists()

    def test_oauth_with_client_id(self, tmp_path: Path) -> None:
        """``client_id`` is forwarded to FastMCP OAuth."""
        from fastmcp.client.auth import OAuth

        result = build_auth(
            "srv",
            auth_config={
                "oauth": {
                    "client_id": "my-id",
                    "client_secret": "my-secret",
                    "scopes": ["read"],
                },
            },
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        assert isinstance(result, OAuth)
        assert result._client_id == "my-id"
        assert result._client_secret == "my-secret"

    def test_cache_tokens_true_creates_directory(self, tmp_path: Path) -> None:
        """When cache_tokens=True (default), token dir is created on disk."""
        build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
            cache_tokens=True,
        )
        assert (tmp_path / "srv").exists()


# ── _is_network_error ──────────────────────────────────────────────


class TestIsNetworkError:
    """Unit tests for the network-error classifier."""

    def test_httpx_connect_error(self) -> None:
        assert _is_network_error(httpx.ConnectError("refused"))

    def test_httpx_timeout(self) -> None:
        assert _is_network_error(httpx.ConnectTimeout("timed out"))

    def test_httpx_read_timeout(self) -> None:
        assert _is_network_error(httpx.ReadTimeout("read timeout"))

    def test_connection_error(self) -> None:
        assert _is_network_error(ConnectionError("broken pipe"))

    def test_os_error(self) -> None:
        assert _is_network_error(OSError("network unreachable"))

    def test_timeout_error(self) -> None:
        assert _is_network_error(TimeoutError("timed out"))

    def test_value_error_not_network(self) -> None:
        assert not _is_network_error(ValueError("bad value"))

    def test_http_status_error_401_not_network(self) -> None:
        req = httpx.Request("GET", "https://example.com")
        resp = httpx.Response(401, request=req)
        assert not _is_network_error(httpx.HTTPStatusError("401", request=req, response=resp))

    def test_runtime_error_not_network(self) -> None:
        assert not _is_network_error(RuntimeError("some bug"))


# ── _RefreshOutcome / network-graceful handling ────────────────────


class TestProactiveRefreshNetworkHandling:
    """Tests that network errors during proactive refresh don't cause full re-auth."""

    def _make_oauth(self, tmp_path: Path):
        """Build a minimal _RefreshTokenOAuth bound to a fake server URL."""
        from mcp_bridge.auth import _build_oauth

        return _build_oauth(
            server_name="test-srv",
            server_url="https://mcp.example.com/mcp",
            base_dir=tmp_path / "test-srv",
            cache_tokens=False,
        )

    @pytest.mark.anyio
    async def test_proactive_refresh_network_error_returns_outcome(self, tmp_path: Path) -> None:
        """_proactive_refresh returns NETWORK_ERROR when httpx raises ConnectError."""
        import time

        from mcp.shared.auth import OAuthToken

        oauth = self._make_oauth(tmp_path)
        # Force-initialise the context so current_tokens / client_info exist
        await oauth._initialize()

        # Inject fake tokens and client info so the refresh path is taken
        ctx = oauth.context
        fake_token = OAuthToken(
            access_token="old-access",
            token_type="Bearer",
            refresh_token="old-refresh",
            expires_in=3600,
        )
        ctx.current_tokens = fake_token
        ctx.token_expiry_time = time.time() - 100  # expired

        # Fake oauth_metadata with a token endpoint
        fake_meta = MagicMock()
        fake_meta.token_endpoint = "https://auth.example.com/token"
        ctx.oauth_metadata = fake_meta

        # Fake client_info
        fake_ci = MagicMock()
        fake_ci.client_id = "client-123"
        ctx.client_info = fake_ci

        # Patch httpx to raise a ConnectError
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value = mock_http

            outcome = await oauth._proactive_refresh()

        assert outcome == _RefreshOutcome.NETWORK_ERROR
        # The old tokens should still be intact
        assert ctx.current_tokens.access_token == "old-access"
        assert ctx.current_tokens.refresh_token == "old-refresh"

    @pytest.mark.anyio
    async def test_initialize_sets_grace_window_on_network_error(self, tmp_path: Path) -> None:
        """_initialize sets token_expiry_time to grace window when network is down.

        Scenario: sidecar shows token expired, proactive refresh fails with a
        network error.  The token_expiry_time should be bumped to a short future
        window so the SDK does not fall through to a full browser re-auth.
        """
        import time

        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthToken

        oauth = self._make_oauth(tmp_path)

        ctx = oauth.context
        fake_token = OAuthToken(
            access_token="old-access",
            token_type="Bearer",
            refresh_token="old-refresh",
            expires_in=3600,
        )
        expired_ts = time.time() - 500  # 500 seconds ago

        async def _fake_super_init(self_inner):
            # Simulate parent loading tokens and setting _initialized
            self_inner.context.current_tokens = fake_token
            # Also inject client_info so can_refresh_token() returns True
            fake_ci = MagicMock()
            fake_ci.client_id = "client-123"
            self_inner.context.client_info = fake_ci
            # Parent wrongly recalculates expiry from relative expires_in,
            # but we override it in our _initialize — set it to None here
            # (we override via stored_expiry below).
            self_inner.context.token_expiry_time = None
            self_inner._initialized = True

        with patch.object(
            oauth, "_proactive_refresh", new=AsyncMock(return_value=_RefreshOutcome.NETWORK_ERROR)
        ):
            # Return an expired sidecar timestamp so the "expired — refreshing" branch fires
            with patch.object(oauth, "_load_token_expiry", new=AsyncMock(return_value=expired_ts)):
                with patch.object(
                    oauth, "_discover_oauth_metadata", new=AsyncMock(return_value=None)
                ):
                    with patch.object(OAuthClientProvider, "_initialize", new=_fake_super_init):
                        oauth._initialized = False
                        before = time.time()
                        await oauth._initialize()
                        after = time.time()

        # token_expiry_time should be set to approximately now + grace window
        assert ctx.token_expiry_time is not None
        expected_lo = before + _NETWORK_ERROR_GRACE_SECONDS - 1
        expected_hi = after + _NETWORK_ERROR_GRACE_SECONDS + 1
        assert expected_lo <= ctx.token_expiry_time <= expected_hi, (
            f"token_expiry_time={ctx.token_expiry_time} not in grace window "
            f"[{expected_lo}, {expected_hi}]"
        )
        # Token should appear valid (not triggering re-auth)
        assert ctx.is_token_valid()


class TestPreflightRefresh:
    """Tests for the pre-flight refresh helper.

    Together they verify that:
      * Tokens still well clear of expiry are NOT refreshed.
      * Tokens within the margin ARE refreshed.
      * A long gap since last activity (sleep/wake) triggers a forced refresh.
      * A NETWORK_ERROR outcome leaves tokens intact and bumps the grace window.
    """

    def _make_oauth(self, tmp_path: Path):
        from mcp_bridge.auth import _build_oauth

        return _build_oauth(
            server_name="test-srv",
            server_url="https://mcp.example.com/mcp",
            base_dir=tmp_path / "test-srv",
            cache_tokens=False,
        )

    def _seed(self, oauth, *, expires_in: float = 3600.0):
        """Wire the context with refreshable tokens, client info, metadata."""
        import time

        from mcp.shared.auth import OAuthToken

        ctx = oauth.context
        ctx.current_tokens = OAuthToken(
            access_token="A",
            token_type="Bearer",
            refresh_token="R",
            expires_in=int(expires_in),
        )
        ctx.token_expiry_time = time.time() + expires_in
        fake_meta = MagicMock()
        fake_meta.token_endpoint = "https://auth.example.com/token"
        ctx.oauth_metadata = fake_meta
        fake_ci = MagicMock()
        fake_ci.client_id = "client-123"
        ctx.client_info = fake_ci

    @pytest.mark.anyio
    async def test_token_well_inside_margin_is_left_alone(self, tmp_path: Path) -> None:
        """A token with plenty of life left should not trigger a pre-flight refresh."""
        oauth = self._make_oauth(tmp_path)
        self._seed(oauth, expires_in=_REFRESH_MARGIN_SECONDS + 600.0)

        with patch.object(oauth, "_proactive_refresh", new=AsyncMock()) as ref:
            await oauth._preflight_refresh_if_needed()

        ref.assert_not_called()

    @pytest.mark.anyio
    async def test_token_within_margin_triggers_refresh(self, tmp_path: Path) -> None:
        """A token within the margin window should be refreshed proactively."""
        oauth = self._make_oauth(tmp_path)
        self._seed(oauth, expires_in=_REFRESH_MARGIN_SECONDS - 30.0)

        ref = AsyncMock(return_value=_RefreshOutcome.SUCCESS)
        with patch.object(oauth, "_proactive_refresh", new=ref):
            await oauth._preflight_refresh_if_needed()

        ref.assert_awaited_once()

    @pytest.mark.anyio
    async def test_wake_up_forces_refresh_even_with_time_left(self, tmp_path: Path) -> None:
        """Long idle gap is treated as a wake-up — refresh fires regardless of margin."""
        import time

        oauth = self._make_oauth(tmp_path)
        # Token is fresh (way outside margin), but last_seen is ancient.
        self._seed(oauth, expires_in=3600.0)
        oauth._last_seen_at = time.time() - (_WAKE_GAP_SECONDS + 60.0)

        ref = AsyncMock(return_value=_RefreshOutcome.SUCCESS)
        with patch.object(oauth, "_proactive_refresh", new=ref):
            await oauth._preflight_refresh_if_needed()

        ref.assert_awaited_once()

    @pytest.mark.anyio
    async def test_no_last_seen_does_not_force_refresh(self, tmp_path: Path) -> None:
        """A first-ever request must not be misclassified as a wake-up."""
        oauth = self._make_oauth(tmp_path)
        self._seed(oauth, expires_in=3600.0)
        # _last_seen_at is unset on a fresh instance.
        assert not hasattr(oauth, "_last_seen_at")

        ref = AsyncMock()
        with patch.object(oauth, "_proactive_refresh", new=ref):
            await oauth._preflight_refresh_if_needed()

        ref.assert_not_called()

    @pytest.mark.anyio
    async def test_network_error_applies_grace_window(self, tmp_path: Path) -> None:
        """NETWORK_ERROR from refresh extends expiry by the grace window."""
        import time

        oauth = self._make_oauth(tmp_path)
        # Within margin so refresh is attempted.
        self._seed(oauth, expires_in=_REFRESH_MARGIN_SECONDS - 30.0)

        ref = AsyncMock(return_value=_RefreshOutcome.NETWORK_ERROR)
        save = AsyncMock()  # avoid reaching the disk store
        with patch.object(oauth, "_proactive_refresh", new=ref), patch.object(
            oauth, "_save_token_expiry", new=save
        ):
            before = time.time()
            await oauth._preflight_refresh_if_needed()
            after = time.time()

        ref.assert_awaited_once()
        ctx = oauth.context
        assert ctx.token_expiry_time is not None
        lo = before + _NETWORK_ERROR_GRACE_SECONDS - 1
        hi = after + _NETWORK_ERROR_GRACE_SECONDS + 1
        assert lo <= ctx.token_expiry_time <= hi
        # Tokens themselves untouched
        assert ctx.current_tokens.access_token == "A"
        assert ctx.current_tokens.refresh_token == "R"

    @pytest.mark.anyio
    async def test_auth_error_leaves_tokens_for_sdk_to_handle(self, tmp_path: Path) -> None:
        """AUTH_ERROR from refresh must not bump expiry — let SDK drive re-auth."""
        oauth = self._make_oauth(tmp_path)
        self._seed(oauth, expires_in=_REFRESH_MARGIN_SECONDS - 30.0)
        original_expiry = oauth.context.token_expiry_time

        ref = AsyncMock(return_value=_RefreshOutcome.AUTH_ERROR)
        with patch.object(oauth, "_proactive_refresh", new=ref):
            await oauth._preflight_refresh_if_needed()

        # Expiry unchanged: SDK will see expired token and follow its 401 path.
        assert oauth.context.token_expiry_time == original_expiry

    @pytest.mark.anyio
    async def test_no_refresh_capability_skips_preflight(self, tmp_path: Path) -> None:
        """Without a refresh_token / client_info there's nothing to do."""
        oauth = self._make_oauth(tmp_path)
        self._seed(oauth, expires_in=10.0)
        # Strip client_info so can_refresh_token() returns False
        oauth.context.client_info = None

        ref = AsyncMock()
        with patch.object(oauth, "_proactive_refresh", new=ref):
            await oauth._preflight_refresh_if_needed()

        ref.assert_not_called()
