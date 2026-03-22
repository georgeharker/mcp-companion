"""OAuth 2.1 authentication support for MCP bridge.

Provides:
- File-based token storage (persists tokens across bridge restarts)
- Auth factory: builds the right httpx.Auth for each server config
- Integrates with the MCP SDK's ``OAuthClientProvider``

Token files are stored at ``~/.local/share/mcp-companion/oauth-tokens/<server>/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

logger = logging.getLogger("mcp-bridge")

# Default token storage directory
_DEFAULT_TOKEN_DIR = Path.home() / ".local" / "share" / "mcp-companion" / "oauth-tokens"

# Browser auth timeout (seconds)
_AUTH_TIMEOUT = 300


class FileTokenStorage(TokenStorage):
    """Persist OAuth tokens and client registration to JSON files on disk.

    Layout::

        base_dir/
            tokens.json      — OAuthToken (access_token, refresh_token, ...)
            client_info.json — OAuthClientInformationFull (client_id, ...)
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._tokens_path = self.base_dir / "tokens.json"
        self._client_info_path = self.base_dir / "client_info.json"

    async def get_tokens(self) -> OAuthToken | None:
        data = self._load_json(self._tokens_path)
        if data is None:
            return None
        return OAuthToken(**data)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._save_model(self._tokens_path, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._load_json(self._client_info_path)
        if data is None:
            return None
        return OAuthClientInformationFull(**data)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._save_model(self._client_info_path, client_info)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | None:
        """Read a JSON file and return the parsed dict, or ``None``."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Corrupt token file %s, ignoring: %s", path, exc)
            return None

    @staticmethod
    def _save_model(path: Path, model: OAuthToken | OAuthClientInformationFull) -> None:
        path.write_text(
            model.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )


class OAuthFlowError(Exception):
    """Raised when the OAuth browser flow fails."""


class _BearerAuth(httpx.Auth):
    """Simple static Bearer token authentication."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Any:  # Generator[httpx.Request, httpx.Response, None]
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def build_auth(
    server_name: str,
    *,
    auth_config: dict[str, Any] | str | None,
    server_url: str | None = None,
    token_dir: Path | None = None,
) -> httpx.Auth | None:
    """Build an ``httpx.Auth`` from a server's auth configuration.

    Supports three modes:

    1. ``auth: "oauth"`` — OAuth 2.1 Authorization Code + PKCE via browser.
    2. ``auth: {"bearer": "<token>"}`` — Static bearer token.
    3. ``auth: {"oauth": {scopes: [...], client_id: "...", ...}}`` —
       OAuth with pre-registered client or explicit scopes.

    Returns ``None`` if no auth is configured.
    """
    if auth_config is None:
        return None

    base_dir = (token_dir or _DEFAULT_TOKEN_DIR) / server_name

    # Simple "oauth" string → default OAuth flow
    if auth_config == "oauth":
        if not server_url:
            raise ValueError(f"Server '{server_name}': auth='oauth' requires a URL")
        return _build_oauth_provider(
            server_name=server_name,
            server_url=server_url,
            base_dir=base_dir,
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
        return _build_oauth_provider(
            server_name=server_name,
            server_url=server_url,
            base_dir=base_dir,
            scopes=oauth_opts.get("scopes"),
            client_id=oauth_opts.get("client_id"),
            client_secret=oauth_opts.get("client_secret"),
            client_metadata_url=oauth_opts.get("client_metadata_url"),
        )

    raise ValueError(f"Server '{server_name}': unrecognized auth keys: {set(auth_config.keys())}")


def _build_oauth_provider(
    *,
    server_name: str,
    server_url: str,
    base_dir: Path,
    scopes: str | list[str] | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    client_metadata_url: str | None = None,
) -> OAuthClientProvider:
    """Construct an ``OAuthClientProvider`` with file-based token storage.

    Uses a lightweight stdlib HTTP server in a daemon thread for the
    OAuth redirect callback, and ``webbrowser.open()`` for the browser step.
    """
    storage = FileTokenStorage(base_dir)

    # --- Callback server setup ---
    # We start the callback server eagerly so we know the port for redirect_uri.
    # It sits idle until the OAuth flow actually needs it.

    _code_future: asyncio.Future[tuple[str, str | None]] | None = None
    _loop_ref: list[asyncio.AbstractEventLoop] = []

    class _Handler(BaseHTTPRequestHandler):
        """Captures ``code`` and ``state`` from the OAuth redirect."""

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            code = (params.get("code") or [""])[0] or None
            state = (params.get("state") or [""])[0] or None
            error = (params.get("error") or [""])[0] or None

            # Send browser response immediately
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                "<html><body>"
                "<h2>Authorization complete!</h2>"
                "<p>You can close this tab and return to your editor.</p>"
                "</body></html>"
            )
            self.wfile.write(body.encode())

            # Signal the async side via the event loop
            if _code_future is not None and _loop_ref:
                loop = _loop_ref[0]
                if error:
                    loop.call_soon_threadsafe(
                        _code_future.set_exception,
                        OAuthFlowError(f"OAuth error: {error}"),
                    )
                elif code:
                    loop.call_soon_threadsafe(
                        _code_future.set_result,
                        (code, state),
                    )
                else:
                    loop.call_soon_threadsafe(
                        _code_future.set_exception,
                        OAuthFlowError("No authorization code in redirect"),
                    )

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Suppress stdlib HTTP server access logs

    http_server = HTTPServer(("127.0.0.1", 0), _Handler)
    actual_port = http_server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{actual_port}/callback"
    logger.info("OAuth callback for '%s' listening on port %d", server_name, actual_port)

    server_thread = Thread(target=http_server.serve_forever, daemon=True)
    server_thread.start()

    # --- Build client metadata ---
    scope_list: list[str] = []
    if isinstance(scopes, str):
        scope_list = scopes.split()
    elif isinstance(scopes, list):
        scope_list = list(scopes)

    redirect_url = AnyUrl(redirect_uri)

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_url],
        token_endpoint_auth_method="none",  # Public client, PKCE-only
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=f"mcp-companion ({server_name})",
        scope=" ".join(scope_list) if scope_list else None,
    )

    # --- Async callbacks for OAuthClientProvider ---

    async def redirect_handler(url: str) -> None:
        logger.info("Opening browser for OAuth authorization...")
        webbrowser.open(url)

    async def callback_handler() -> tuple[str, str | None]:
        nonlocal _code_future
        loop = asyncio.get_running_loop()
        _loop_ref.clear()
        _loop_ref.append(loop)
        _code_future = loop.create_future()
        try:
            return await asyncio.wait_for(_code_future, timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            raise OAuthFlowError(
                f"OAuth timeout for '{server_name}': "
                "browser authorization not completed within 5 minutes"
            ) from None

    # --- Build provider ---

    provider = OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        client_metadata_url=client_metadata_url,
    )

    # Pre-populate client info if a client_id was provided (skip dynamic registration)
    if client_id:
        pre_reg = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret if client_secret else None,
            redirect_uris=[redirect_url],
            token_endpoint_auth_method=("client_secret_basic" if client_secret else "none"),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=f"mcp-companion ({server_name})",
        )
        # Synchronous write — called during bridge startup before event loop runs
        pre_reg_json = pre_reg.model_dump_json(indent=2, exclude_none=True)
        (base_dir / "client_info.json").write_text(pre_reg_json, encoding="utf-8")

    return provider
