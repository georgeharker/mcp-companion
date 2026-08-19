"""Reusable inbound bearer auth for FastMCP HTTP servers.

DROP-IN: this module is deliberately self-contained (stdlib + starlette only) so
it can be **vendored byte-identical** into any FastMCP server that fronts an HTTP
surface — the combiner here, and sibling servers like cribsheet / svg-mcp. Copy
the file in, then in the app assembly:

    from inbound_auth import BearerAuthMiddleware, resolve_auth_token

    token = resolve_auth_token("CRIBSHEET_AUTH_TOKEN")   # or a --auth-token-file
    if token:
        app.add_middleware(
            BearerAuthMiddleware,
            token=token,
            is_protected=lambda path: path != "/health",  # gate everything but health
        )

Design (kept identical across servers so the whole ecosystem behaves the same):

- OFF by default: install the middleware only when a token is configured, so an
  unset env var leaves the endpoint open (zero-config localhost unchanged).
- "Send-if-present, enforce-if-configured": clients send the header whenever their
  token env var is set; the server enforces only when its own token is configured.
- PLAIN 401 with **no** ``WWW-Authenticate: Bearer`` — deliberately does not
  advertise OAuth / protected-resource metadata, so standards clients (e.g.
  pi-mcp-adapter, whose probe keys on a Bearer challenge) surface an honest auth
  error instead of falling into Dynamic Client Registration. Clients present the
  token pre-emptively.
- ``is_protected(path)`` decides the gated surface per-server: a plain FastMCP
  server gates everything but ``/health``; the combiner also exempts ``/health``
  but gates ``/mcp*`` plus its control routes. Constant-time token compare.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


def resolve_auth_token(env_var: str, token_file: str | None = None) -> str | None:
    """Resolve the inbound bearer token for a server.

    Precedence: an explicit ``token_file`` (a daemon-side ``--auth-token-file``)
    wins over the ``env_var`` environment variable. Blank / whitespace-only / an
    unreadable file / an unset var all resolve to ``None`` — which leaves the
    endpoint OPEN (the default; nothing changes until a token is provisioned).
    Env delivery is the operator's job; nothing is generated or persisted here.
    """
    if token_file:
        try:
            tok = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.error("inbound-auth: could not read --auth-token-file %s (%s)", token_file, e)
            tok = ""
        if tok:
            return tok
        logger.warning(
            "inbound-auth: --auth-token-file %s yielded no token; endpoint stays open", token_file
        )
    env_tok = (os.environ.get(env_var) or "").strip()
    return env_tok or None


def _extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on the protected surface.

    Install ONLY when a token is configured; when absent, do not add it at all so
    the default remains an unauthenticated endpoint. ``is_protected(path)`` picks
    which requests are gated. A missing/wrong token returns a plain 401 with NO
    ``WWW-Authenticate`` header (see module docstring). Clients present the token
    pre-emptively, so no challenge is needed.
    """

    def __init__(self, app: Any, token: str, is_protected: Callable[[str], bool]) -> None:
        super().__init__(app)
        self._token = token
        self._is_protected = is_protected

    async def dispatch(
        self, request: StarletteRequest, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if self._is_protected(path):
            provided = _extract_bearer(request.headers.get("authorization"))
            # Constant-time compare; the guard keeps compare_digest off ``None``.
            if provided is None or not hmac.compare_digest(provided, self._token):
                logger.warning(
                    "inbound-auth: rejected %s %s (missing/invalid bearer)", request.method, path
                )
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
