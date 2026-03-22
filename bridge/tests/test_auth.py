"""Tests for mcp-bridge authentication module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_bridge.auth import (
    FileTokenStorage,
    OAuthFlowError,
    _BearerAuth,
    build_auth,
)


# ── FileTokenStorage ───────────────────────────────────────────────


class TestFileTokenStorage:
    """Unit tests for file-based token persistence."""

    @pytest.fixture
    def storage(self, tmp_path: Path) -> FileTokenStorage:
        return FileTokenStorage(tmp_path / "tokens")

    @pytest.mark.anyio
    async def test_get_tokens_missing(self, storage: FileTokenStorage) -> None:
        assert await storage.get_tokens() is None

    @pytest.mark.anyio
    async def test_get_client_info_missing(self, storage: FileTokenStorage) -> None:
        assert await storage.get_client_info() is None

    @pytest.mark.anyio
    async def test_roundtrip_tokens(self, storage: FileTokenStorage) -> None:
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(
            access_token="acc_123",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="ref_456",
        )
        await storage.set_tokens(token)

        loaded = await storage.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "acc_123"
        assert loaded.refresh_token == "ref_456"
        assert loaded.token_type == "Bearer"

    @pytest.mark.anyio
    async def test_roundtrip_client_info(self, storage: FileTokenStorage) -> None:
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        info = OAuthClientInformationFull(
            client_id="cid_789",
            client_secret="sec_000",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            token_endpoint_auth_method="client_secret_basic",
            grant_types=["authorization_code"],
            response_types=["code"],
            client_name="test-client",
        )
        await storage.set_client_info(info)

        loaded = await storage.get_client_info()
        assert loaded is not None
        assert loaded.client_id == "cid_789"
        assert loaded.client_secret == "sec_000"

    @pytest.mark.anyio
    async def test_corrupt_tokens_file(self, storage: FileTokenStorage) -> None:
        storage._tokens_path.write_text("NOT JSON", encoding="utf-8")
        assert await storage.get_tokens() is None

    @pytest.mark.anyio
    async def test_corrupt_client_info_file(self, storage: FileTokenStorage) -> None:
        storage._client_info_path.write_text("{}", encoding="utf-8")
        # Empty dict is missing required fields — should fail validation
        # Pydantic validation error is a ValueError subclass
        with pytest.raises(Exception):
            await storage.get_client_info()

    @pytest.mark.anyio
    async def test_creates_directory(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        storage = FileTokenStorage(deep)
        assert deep.exists()
        assert await storage.get_tokens() is None

    @pytest.mark.anyio
    async def test_token_file_contents(self, storage: FileTokenStorage) -> None:
        """Verify the on-disk format is valid JSON with expected fields."""
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(access_token="tok", token_type="Bearer")
        await storage.set_tokens(token)

        raw: dict[str, Any] = json.loads(storage._tokens_path.read_text())
        assert raw["access_token"] == "tok"
        assert raw["token_type"] == "Bearer"


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
        # Verify the token works via the auth flow
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

    def test_oauth_string_returns_provider(self, tmp_path: Path) -> None:
        """``auth: "oauth"`` creates an OAuthClientProvider."""
        from mcp.client.auth import OAuthClientProvider

        result = build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        assert isinstance(result, OAuthClientProvider)

    def test_oauth_dict_returns_provider(self, tmp_path: Path) -> None:
        """``auth: {"oauth": {...}}`` creates an OAuthClientProvider."""
        from mcp.client.auth import OAuthClientProvider

        result = build_auth(
            "srv",
            auth_config={"oauth": {"scopes": ["read", "write"]}},
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        assert isinstance(result, OAuthClientProvider)

    def test_oauth_with_client_id_pre_populates(self, tmp_path: Path) -> None:
        """When ``client_id`` is provided, ``client_info.json`` is written eagerly."""
        build_auth(
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
        ci_path = tmp_path / "srv" / "client_info.json"
        assert ci_path.exists()
        data: dict[str, Any] = json.loads(ci_path.read_text())
        assert data["client_id"] == "my-id"
        assert data["client_secret"] == "my-secret"
        assert data["token_endpoint_auth_method"] == "client_secret_basic"

    def test_oauth_without_client_id_no_prewrite(self, tmp_path: Path) -> None:
        """Without ``client_id``, no ``client_info.json`` is written."""
        build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
        )
        ci_path = tmp_path / "srv" / "client_info.json"
        assert not ci_path.exists()


# ── OAuthFlowError ─────────────────────────────────────────────────


def test_oauth_flow_error_is_exception() -> None:
    err = OAuthFlowError("test")
    assert isinstance(err, Exception)
    assert str(err) == "test"
