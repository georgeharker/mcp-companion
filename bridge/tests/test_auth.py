"""Tests for mcp-bridge authentication module."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_bridge.auth import (
    FileKeyValueStore,
    _BearerAuth,
    build_auth,
)


# ── FileKeyValueStore ──────────────────────────────────────────────


class TestFileKeyValueStore:
    """Unit tests for file-based key-value persistence."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> FileKeyValueStore:
        return FileKeyValueStore(tmp_path / "store")

    @pytest.mark.anyio
    async def test_get_missing_key(self, store: FileKeyValueStore) -> None:
        assert await store.get(key="missing", collection="col") is None

    @pytest.mark.anyio
    async def test_roundtrip(self, store: FileKeyValueStore) -> None:
        await store.put(key="k", value={"a": 1, "b": "two"}, collection="col")
        result = await store.get(key="k", collection="col")
        assert result == {"a": 1, "b": "two"}

    @pytest.mark.anyio
    async def test_delete_existing(self, store: FileKeyValueStore) -> None:
        await store.put(key="k", value={"x": 1}, collection="col")
        deleted = await store.delete(key="k", collection="col")
        assert deleted is True
        assert await store.get(key="k", collection="col") is None

    @pytest.mark.anyio
    async def test_delete_missing(self, store: FileKeyValueStore) -> None:
        deleted = await store.delete(key="missing", collection="col")
        assert deleted is False

    @pytest.mark.anyio
    async def test_creates_directories(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        store = FileKeyValueStore(deep)
        assert deep.exists()
        assert await store.get(key="k", collection="col") is None

    @pytest.mark.anyio
    async def test_corrupt_file_returns_none(self, store: FileKeyValueStore) -> None:
        coll_dir = store.base_dir / "col"
        coll_dir.mkdir(parents=True, exist_ok=True)
        (coll_dir / "k.json").write_text("NOT JSON", encoding="utf-8")
        assert await store.get(key="k", collection="col") is None

    @pytest.mark.anyio
    async def test_multiple_collections_independent(self, store: FileKeyValueStore) -> None:
        await store.put(key="k", value={"v": 1}, collection="col1")
        await store.put(key="k", value={"v": 2}, collection="col2")
        assert (await store.get(key="k", collection="col1")) == {"v": 1}
        assert (await store.get(key="k", collection="col2")) == {"v": 2}

    @pytest.mark.anyio
    async def test_ttl_expiry(self, store: FileKeyValueStore) -> None:
        """Entries with an already-expired TTL are treated as missing."""
        # Write a file with an expires_at in the past
        coll_dir = store.base_dir / "col"
        coll_dir.mkdir(parents=True, exist_ok=True)
        past = (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat()
        (coll_dir / "k.json").write_text(
            json.dumps({"value": {"x": 1}, "expires_at": past}), encoding="utf-8"
        )
        assert await store.get(key="k", collection="col") is None
        # File should be cleaned up lazily
        assert not (coll_dir / "k.json").exists()

    @pytest.mark.anyio
    async def test_no_ttl_does_not_expire(self, store: FileKeyValueStore) -> None:
        await store.put(key="k", value={"x": 1}, collection="col")
        result = await store.get(key="k", collection="col")
        assert result is not None

    @pytest.mark.anyio
    async def test_file_contents_are_valid_json(self, store: FileKeyValueStore) -> None:
        await store.put(key="k", value={"foo": "bar"}, collection="col")
        path = store.base_dir / "col" / "k.json"
        assert path.exists()
        data: dict[str, Any] = json.loads(path.read_text())
        assert data["value"] == {"foo": "bar"}

    @pytest.mark.anyio
    async def test_key_with_special_chars_sanitized(self, store: FileKeyValueStore) -> None:
        """Keys containing slashes/colons are sanitized to valid filenames."""
        key = "http://localhost:8002/mcp/tokens"
        await store.put(key=key, value={"tok": "abc"}, collection="mcp-oauth-token")
        result = await store.get(key=key, collection="mcp-oauth-token")
        assert result == {"tok": "abc"}


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

    def test_oauth_uses_file_storage(self, tmp_path: Path) -> None:
        """OAuth provider is configured with FileKeyValueStore at the right path."""
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

    def test_cache_tokens_false_no_directory(self, tmp_path: Path) -> None:
        """When cache_tokens=False, no token directory is created."""
        build_auth(
            "srv",
            auth_config="oauth",
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
            cache_tokens=False,
        )
        assert not (tmp_path / "srv").exists()

    def test_per_server_cache_tokens_false(self, tmp_path: Path) -> None:
        """Per-server cache_tokens=false inside auth dict overrides global flag."""
        build_auth(
            "srv",
            auth_config={"oauth": {"cache_tokens": False}},
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
            cache_tokens=True,  # global says True, per-server says False
        )
        assert not (tmp_path / "srv").exists()

    def test_per_server_cache_tokens_true_overrides_global_false(self, tmp_path: Path) -> None:
        """Per-server cache_tokens=true overrides global cache_tokens=False."""
        build_auth(
            "srv",
            auth_config={"oauth": {"cache_tokens": True}},
            server_url="http://example.com/mcp",
            token_dir=tmp_path,
            cache_tokens=False,  # global says False, per-server says True
        )
        assert (tmp_path / "srv").exists()
