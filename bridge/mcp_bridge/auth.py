"""OAuth authentication support for MCP bridge.

Provides:
- File-based key-value store for persistent OAuth token storage
- Auth factory: builds the right httpx.Auth for each server config
- Uses FastMCP's OAuth client for the full Authorization Code + PKCE flow

Token files are stored under ``token_dir/<server>/`` (default
``~/.cache/mcp-companion/oauth-tokens``).  Disk caching can be disabled by
passing ``cache_tokens=False`` to :func:`build_auth`, in which case tokens are
kept in memory only and lost when the bridge restarts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
from fastmcp.client.auth import OAuth
from key_value.aio._utils.managed_entry import ManagedEntry
from key_value.aio.stores.base import BaseStore

logger = logging.getLogger("mcp-bridge")

# Default token storage directory (XDG cache convention)
_DEFAULT_TOKEN_DIR = Path.home() / ".cache" / "mcp-companion" / "oauth-tokens"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _sanitize(s: str) -> str:
    """Replace filesystem-unsafe characters with underscores."""
    return re.sub(r"[^\w\-.]", "_", s)


class FileKeyValueStore(BaseStore):
    """Persistent file-based key-value store implementing ``AsyncKeyValueProtocol``.

    Each entry is stored as a JSON file under::

        base_dir/<collection>/<sanitized_key>.json

    The file format is::

        {
            "value": {...},
            "created_at": "2024-01-01T00:00:00+00:00",   # optional
            "expires_at": "2025-01-01T00:00:00+00:00"    # optional, omitted if no TTL
        }

    Expired entries are treated as missing (deleted lazily on next read).
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(stable_api=True)

    def _entry_path(self, collection: str, key: str) -> Path:
        coll_dir = self.base_dir / _sanitize(collection)
        coll_dir.mkdir(parents=True, exist_ok=True)
        return coll_dir / (_sanitize(key) + ".json")

    async def _get_managed_entry(self, *, key: str, collection: str) -> ManagedEntry | None:
        path = self._entry_path(collection, key)
        if not path.exists():
            return None
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt token file %s, ignoring: %s", path, exc)
            return None

        value: Mapping[str, Any] = raw.get("value", {})
        created_at: datetime | None = None
        expires_at: datetime | None = None

        if raw_ca := raw.get("created_at"):
            try:
                created_at = datetime.fromisoformat(raw_ca)
            except ValueError:
                pass
        if raw_ea := raw.get("expires_at"):
            try:
                expires_at = datetime.fromisoformat(raw_ea)
            except ValueError:
                pass

        entry = ManagedEntry(value=value, created_at=created_at, expires_at=expires_at)

        # Treat expired entries as missing; clean up lazily
        if entry.is_expired:
            path.unlink(missing_ok=True)
            return None

        return entry

    async def _put_managed_entry(
        self, *, key: str, collection: str, managed_entry: ManagedEntry
    ) -> None:
        path = self._entry_path(collection, key)
        data: dict[str, Any] = {"value": dict(managed_entry.value)}
        if managed_entry.created_at:
            data["created_at"] = managed_entry.created_at.isoformat()
        if managed_entry.expires_at:
            data["expires_at"] = managed_entry.expires_at.isoformat()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def _delete_managed_entry(self, *, key: str, collection: str) -> bool:
        path = self._entry_path(collection, key)
        if path.exists():
            path.unlink()
            return True
        return False


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
    cache_tokens: bool = True,
) -> OAuth:
    """Construct a FastMCP ``OAuth`` provider.

    When *cache_tokens* is ``True`` (default) a :class:`FileKeyValueStore` is
    created at *base_dir* and passed as ``token_storage``.  Tokens are persisted
    to disk and survive bridge restarts.

    When *cache_tokens* is ``False`` ``token_storage=None`` is passed, which
    tells FastMCP to use an in-memory store.  Tokens are lost on restart and the
    OAuth browser flow will run again on next startup.
    """
    scope_str: str | None = None
    if isinstance(scopes, list):
        scope_str = " ".join(scopes)
    elif isinstance(scopes, str):
        scope_str = scopes

    if cache_tokens:
        storage: FileKeyValueStore | None = FileKeyValueStore(base_dir)
        logger.info(
            "Configuring OAuth for server '%s' with disk token cache at %s",
            server_name,
            base_dir,
        )
    else:
        storage = None
        logger.info(
            "Configuring OAuth for server '%s' with in-memory token storage (disk cache disabled)",
            server_name,
        )

    return OAuth(
        mcp_url=server_url,
        scopes=scope_str,
        client_name=f"mcp-companion ({server_name})",
        token_storage=storage,
        client_id=client_id,
        client_secret=client_secret,
        client_metadata_url=client_metadata_url,
    )
