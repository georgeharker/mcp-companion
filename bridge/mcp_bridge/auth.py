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

import getpass
import hashlib
import logging
import os
import platform
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastmcp.client.auth import OAuth
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

logger = logging.getLogger("mcp-bridge")

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
) -> OAuth:
    """Construct a FastMCP ``OAuth`` provider.

    When *cache_tokens* is ``True`` (default) an encrypted file store is
    created at *base_dir* and passed as ``token_storage``.  Tokens are persisted
    to disk with Fernet encryption and survive bridge restarts.

    When *cache_tokens* is ``False`` ``token_storage=None`` is passed, which
    tells FastMCP to use an in-memory store.  Tokens are lost on restart and the
    OAuth browser flow will run again on next startup.
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

    return OAuth(
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
