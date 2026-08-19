"""Tests for inbound bearer auth on the /mcp surface (asgi.BearerAuthMiddleware
and resolve_auth_token).

The middleware is exercised in isolation over a trivial Starlette app so the
gate's behaviour is tested without standing up the full combiner: only the path
scope, the 401 shape, and the token comparison matter here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_combiner.asgi import BearerAuthMiddleware, resolve_auth_token

TOKEN = "s3cr3t-token-value"


def _app(token: str | None) -> Starlette:
    async def ok(_request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/mcp/{rest:path}", ok, methods=["GET", "POST"]),
            Route("/health", ok, methods=["GET"]),
            Route("/sessions/map", ok, methods=["GET"]),
        ]
    )
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def test_mcp_requires_bearer_when_configured() -> None:
    client = TestClient(_app(TOKEN))
    r = client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_mcp_rejects_missing_bearer() -> None:
    client = TestClient(_app(TOKEN))
    r = client.post("/mcp")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    # Must NOT advertise a Bearer challenge, or a standards client would fall
    # into OAuth/Dynamic Client Registration instead of surfacing the failure.
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_mcp_rejects_wrong_bearer() -> None:
    client = TestClient(_app(TOKEN))
    r = client.post("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_mcp_rejects_non_bearer_scheme() -> None:
    client = TestClient(_app(TOKEN))
    r = client.post("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_token_in_url_path_is_also_gated() -> None:
    client = TestClient(_app(TOKEN))
    # A grouping-token URL path is still under /mcp — must carry the bearer too.
    assert client.post("/mcp/pi-abcdefgh").status_code == 401
    assert (
        client.post("/mcp/pi-abcdefgh", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )


def test_health_and_control_routes_stay_open() -> None:
    client = TestClient(_app(TOKEN))
    # Liveness and the localhost control plane are not gated (the nvim host / ops
    # drive them without a bearer).
    assert client.get("/health").status_code == 200
    assert client.get("/sessions/map").status_code == 200


def test_no_token_leaves_endpoint_open() -> None:
    # Default: no token configured => middleware not installed => /mcp is open.
    client = TestClient(_app(None))
    assert client.post("/mcp").status_code == 200


# --- resolve_auth_token precedence -------------------------------------------


def test_resolve_prefers_file_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_COMBINER_AUTH_TOKEN", "from-env")
    f = tmp_path / "tok"
    f.write_text("  from-file\n")  # surrounding whitespace is stripped
    assert resolve_auth_token(str(f)) == "from-file"


def test_resolve_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_COMBINER_AUTH_TOKEN", "from-env")
    assert resolve_auth_token(None) == "from-env"


def test_resolve_blank_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_COMBINER_AUTH_TOKEN", "   ")
    assert resolve_auth_token(None) is None


def test_resolve_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_COMBINER_AUTH_TOKEN", raising=False)
    assert resolve_auth_token(None) is None


def test_resolve_empty_file_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_COMBINER_AUTH_TOKEN", "from-env")
    f = tmp_path / "empty"
    f.write_text("\n")
    assert resolve_auth_token(str(f)) == "from-env"


def test_resolve_missing_file_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_COMBINER_AUTH_TOKEN", "from-env")
    assert resolve_auth_token("/no/such/file/here") == "from-env"
