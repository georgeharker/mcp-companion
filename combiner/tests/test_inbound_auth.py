"""Tests for inbound bearer auth: the reusable middleware/resolver
(mcp_combiner.inbound_auth) and the combiner's protected-path predicate
(mcp_combiner.asgi.combiner_protected_path).

The middleware is exercised in isolation over a trivial Starlette app so the
gate's behaviour is tested without standing up the full combiner: only the path
scope, the 401 shape, and the token comparison matter here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_combiner.asgi import combiner_protected_path
from mcp_combiner.inbound_auth import BearerAuthMiddleware, resolve_auth_token

TOKEN = "s3cr3t-token-value"
ENV = "MCP_COMBINER_AUTH_TOKEN"


def _app(
    token: str | None, is_protected: Callable[[str], bool] = combiner_protected_path
) -> Starlette:
    async def ok(_request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/mcp/{rest:path}", ok, methods=["GET", "POST"]),
            Route("/health", ok, methods=["GET"]),
            Route("/sessions/map", ok, methods=["GET"]),
            Route("/sessions/token/{tok}/filter", ok, methods=["GET", "POST", "DELETE"]),
            Route("/handover/prepare", ok, methods=["POST"]),
        ]
    )
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token, is_protected=is_protected)
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
    assert (
        TestClient(_app(TOKEN)).post("/mcp", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_mcp_rejects_non_bearer_scheme() -> None:
    assert (
        TestClient(_app(TOKEN))
        .post("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
        .status_code
        == 401
    )


def test_token_in_url_path_is_also_gated() -> None:
    client = TestClient(_app(TOKEN))
    assert client.post("/mcp/pi-abcdefgh").status_code == 401
    assert (
        client.post("/mcp/pi-abcdefgh", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )


def test_control_routes_are_gated() -> None:
    # The widened scope: /sessions* and /handover* now require the bearer too.
    client = TestClient(_app(TOKEN))
    assert client.get("/sessions/map").status_code == 401
    assert client.post("/sessions/token/abc/filter").status_code == 401
    assert client.post("/handover/prepare").status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/sessions/map", headers=auth).status_code == 200
    assert client.post("/handover/prepare", headers=auth).status_code == 200


def test_health_stays_open() -> None:
    # Liveness probe is never gated (ops / nvim host / sharedserver poll it).
    assert TestClient(_app(TOKEN)).get("/health").status_code == 200


def test_no_token_leaves_endpoint_open() -> None:
    client = TestClient(_app(None))
    assert client.post("/mcp").status_code == 200
    assert client.get("/sessions/map").status_code == 200


# --- combiner_protected_path predicate ---------------------------------------


@pytest.mark.parametrize(
    "path,protected",
    [
        ("/mcp", True),
        ("/mcp/", True),
        ("/mcp/pi-token", True),
        ("/sessions", True),
        ("/sessions/map", True),
        ("/sessions/token/abc/filter", True),
        ("/handover", True),
        ("/handover/prepare", True),
        ("/health", False),
        ("/", False),
        ("/other", False),
    ],
)
def test_combiner_protected_path(path: str, protected: bool) -> None:
    assert combiner_protected_path(path) is protected


# --- resolve_auth_token precedence -------------------------------------------


def test_resolve_prefers_file_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "from-env")
    f = tmp_path / "tok"
    f.write_text("  from-file\n")  # surrounding whitespace is stripped
    assert resolve_auth_token(ENV, str(f)) == "from-file"


def test_resolve_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "from-env")
    assert resolve_auth_token(ENV) == "from-env"


def test_resolve_reads_the_named_var(monkeypatch: pytest.MonkeyPatch) -> None:
    # The var name is a parameter — each server (combiner/cribsheet/svg-mcp) names
    # its own; here a distinct name resolves independently.
    monkeypatch.setenv("CRIBSHEET_AUTH_TOKEN", "crib-tok")
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_auth_token("CRIBSHEET_AUTH_TOKEN") == "crib-tok"
    assert resolve_auth_token(ENV) is None


def test_resolve_blank_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "   ")
    assert resolve_auth_token(ENV) is None


def test_resolve_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_auth_token(ENV) is None


def test_resolve_empty_file_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, "from-env")
    f = tmp_path / "empty"
    f.write_text("\n")
    assert resolve_auth_token(ENV, str(f)) == "from-env"


def test_resolve_missing_file_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "from-env")
    assert resolve_auth_token(ENV, "/no/such/file/here") == "from-env"
