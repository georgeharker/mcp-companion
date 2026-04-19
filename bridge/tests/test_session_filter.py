"""Tests for per-session server filtering and REST session endpoints.

Tests the following bridge features:
  - _session_disabled dict management
  - _token_sessions ACP token registry
  - REST endpoints: /sessions, /sessions/{id}/filter, /sessions/token/{token}
  - Middleware filtering in on_list_tools and on_call_tool
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


# ── Helpers ────────────────────────────────────────────────────────


def _make_bridge_app():
    """Create a bridge FastMCP app with test config for REST endpoint testing.

    Returns (app, config) where app is the Starlette-compatible ASGI app
    and config is the BridgeConfig used.
    """
    from mcp_bridge.config import BridgeConfig
    from mcp_bridge.server import create_bridge

    config_path = str(FIXTURES / "servers.json")
    config = BridgeConfig.load(config_path)
    bridge = create_bridge(config_path)
    return bridge, config


def _reset_session_state():
    """Reset module-level session state between tests."""
    import mcp_bridge.server as srv

    srv._session_disabled.clear()
    srv._token_sessions.clear()
    srv._pending_token_filters.clear()
    srv._active_sessions.clear()


# ── _session_disabled dict ─────────────────────────────────────────


class TestSessionDisabledDict:
    """Unit tests for the _session_disabled module-level dict."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_bridge.server as srv

        assert srv._session_disabled == {}

    def test_set_and_get(self):
        import mcp_bridge.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        assert srv._session_disabled.get("session-1") == {"everything"}

    def test_missing_session_returns_none(self):
        import mcp_bridge.server as srv

        assert srv._session_disabled.get("nonexistent") is None

    def test_clear_session(self):
        import mcp_bridge.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        del srv._session_disabled["session-1"]
        assert srv._session_disabled.get("session-1") is None

    def test_multiple_sessions_independent(self):
        import mcp_bridge.server as srv

        srv._session_disabled["session-1"] = {"everything"}
        srv._session_disabled["session-2"] = {"http-example"}
        assert srv._session_disabled["session-1"] == {"everything"}
        assert srv._session_disabled["session-2"] == {"http-example"}


# ── Token registry ─────────────────────────────────────────────────


class TestTokenSessions:
    """Unit tests for the _token_sessions ACP token registry."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_bridge.server as srv

        assert srv._token_sessions == {}

    def test_set_and_get(self):
        import mcp_bridge.server as srv

        srv._token_sessions["token-abc"] = "bridge-session-xyz"
        assert srv._token_sessions["token-abc"] == "bridge-session-xyz"

    def test_missing_token_returns_none(self):
        import mcp_bridge.server as srv

        assert srv._token_sessions.get("nonexistent") is None

    def test_multiple_tokens_independent(self):
        import mcp_bridge.server as srv

        srv._token_sessions["token-1"] = "session-a"
        srv._token_sessions["token-2"] = "session-b"
        assert srv._token_sessions["token-1"] == "session-a"
        assert srv._token_sessions["token-2"] == "session-b"

    def test_clear_token(self):
        import mcp_bridge.server as srv

        srv._token_sessions["token-1"] = "session-a"
        del srv._token_sessions["token-1"]
        assert "token-1" not in srv._token_sessions


# ── REST endpoint tests ────────────────────────────────────────────


class TestSessionRESTEndpoints:
    """Integration tests for session management REST endpoints.

    Uses Starlette TestClient to test the HTTP layer directly.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        bridge, _config = _make_bridge_app()
        app = bridge.http_app()
        return TestClient(app, raise_server_exceptions=False)

    # -- GET /sessions --

    def test_list_sessions_empty(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    # -- GET /sessions/token/{token} --

    def test_token_lookup_found(self, client):
        import mcp_bridge.server as srv

        token = "aaaabbbb-cccc-4ddd-8eee-ffffffffffff"
        srv._token_sessions[token] = "bridge-session-xyz"
        resp = client.get(f"/sessions/token/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == token
        assert data["session_id"] == "bridge-session-xyz"

    def test_token_lookup_not_found(self, client):
        resp = client.get("/sessions/token/00000000-0000-4000-8000-000000000000")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_token_lookup_does_not_remove_entry(self, client):
        import mcp_bridge.server as srv

        token = "aaaabbbb-cccc-4ddd-8eee-ffffffffffff"
        srv._token_sessions[token] = "bridge-session-xyz"
        client.get(f"/sessions/token/{token}")
        # Token remains for subsequent lookups
        assert token in srv._token_sessions

    # -- POST /sessions/{id}/filter --

    def test_post_session_filter(self, client):
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test-session-123"
        assert "everything" in data["disabled_servers"]

    def test_post_session_filter_unknown_server(self, client):
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["nonexistent"]},
        )
        assert resp.status_code == 400

    def test_post_session_filter_empty_clears(self, client):
        # Set filter
        client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        # Clear with empty list
        resp = client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": []},
        )
        assert resp.status_code == 200
        assert resp.json()["disabled_servers"] == []

    # -- DELETE /sessions/{id}/filter --

    def test_delete_session_filter(self, client):
        # Set then delete
        client.post(
            "/sessions/test-session-123/filter",
            json={"disabled_servers": ["everything"]},
        )
        resp = client.delete("/sessions/test-session-123/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert "everything" in data["previously_disabled"]

    def test_delete_session_filter_nonexistent(self, client):
        resp = client.delete("/sessions/nonexistent/filter")
        assert resp.status_code == 200
        assert resp.json()["previously_disabled"] == []


# ── Middleware unit tests ──────────────────────────────────────────


class TestMiddlewareFiltering:
    """Unit tests for session filtering in on_list_tools and on_call_tool."""

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_session_disabled_blocks_tools_lookup(self):
        """Verify that _session_disabled entries are keyed by session_id string."""
        import mcp_bridge.server as srv

        srv._session_disabled["test-sid"] = {"everything"}
        assert "everything" in srv._session_disabled.get("test-sid", set())
        assert srv._session_disabled.get("other-sid") is None


# ── _pending_token_filters dict ────────────────────────────────────


class TestPendingTokenFilters:
    """Unit tests for the _pending_token_filters module-level dict.

    This dict stores filter state for tokens whose ACP clients have not
    yet connected.  When the client later connects via /mcp/<token>,
    TokenRewriteMiddleware applies the pending filter.
    """

    def setup_method(self):
        _reset_session_state()

    def teardown_method(self):
        _reset_session_state()

    def test_empty_by_default(self):
        import mcp_bridge.server as srv

        assert srv._pending_token_filters == {}

    def test_set_and_get(self):
        import mcp_bridge.server as srv

        srv._pending_token_filters["token-abc"] = {"everything"}
        assert srv._pending_token_filters.get("token-abc") == {"everything"}

    def test_missing_token_returns_none(self):
        import mcp_bridge.server as srv

        assert srv._pending_token_filters.get("nonexistent") is None

    def test_clear_pending(self):
        import mcp_bridge.server as srv

        srv._pending_token_filters["token-abc"] = {"everything"}
        del srv._pending_token_filters["token-abc"]
        assert srv._pending_token_filters.get("token-abc") is None

    def test_multiple_tokens_independent(self):
        import mcp_bridge.server as srv

        srv._pending_token_filters["token-1"] = {"everything", "http-example"}
        srv._pending_token_filters["token-2"] = {"http-example"}
        assert srv._pending_token_filters["token-1"] == {"everything", "http-example"}
        assert srv._pending_token_filters["token-2"] == {"http-example"}


# ── Token filter REST endpoints ────────────────────────────────────


class TestTokenFilterRESTEndpoints:
    """Integration tests for /sessions/token/{token}/filter endpoints.

    Tests both the "session already connected" path (token in _token_sessions)
    and the "pending" path (token not yet mapped to a session).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        bridge, _config = _make_bridge_app()
        app = bridge.http_app()
        return TestClient(app, raise_server_exceptions=False)

    # -- GET /sessions/token/{token}/filter --

    def test_get_filter_pending_empty(self, client):
        """GET on unconnected token returns pending=True, empty disabled list."""
        resp = client.get("/sessions/token/tok-aaa/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == "tok-aaa"
        assert data["session_id"] is None
        assert data["pending"] is True
        assert data["disabled_servers"] == []

    def test_get_filter_pending_with_state(self, client):
        """GET on unconnected token returns pending filter state."""
        import mcp_bridge.server as srv

        srv._pending_token_filters["tok-aaa"] = {"everything"}
        resp = client.get("/sessions/token/tok-aaa/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        assert "everything" in data["disabled_servers"]

    def test_get_filter_connected(self, client):
        """GET on connected token returns real session filter state."""
        import mcp_bridge.server as srv

        srv._token_sessions["tok-bbb"] = "session-123"
        srv._session_disabled["session-123"] = {"http-example"}
        resp = client.get("/sessions/token/tok-bbb/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "session-123"
        assert "http-example" in data["disabled_servers"]
        assert "pending" not in data

    # -- POST /sessions/token/{token}/filter (pending path) --

    def test_post_filter_pending_disabled_servers(self, client):
        """POST with disabled_servers on unconnected token stores as pending."""
        import mcp_bridge.server as srv

        resp = client.post(
            "/sessions/token/tok-ccc/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        assert "everything" in data["disabled_servers"]
        # Verify pending dict was populated
        assert "everything" in srv._pending_token_filters.get("tok-ccc", set())

    def test_post_filter_pending_allowed_servers(self, client):
        """POST with allowed_servers on unconnected token inverts to disabled."""
        resp = client.post(
            "/sessions/token/tok-ddd/filter",
            json={"allowed_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending"] is True
        # everything is allowed, so other enabled servers should be disabled
        assert "everything" not in data["disabled_servers"]

    def test_post_filter_pending_empty_clears(self, client):
        """POST with empty disabled_servers on pending token clears pending."""
        import mcp_bridge.server as srv

        srv._pending_token_filters["tok-eee"] = {"everything"}
        resp = client.post(
            "/sessions/token/tok-eee/filter",
            json={"disabled_servers": []},
        )
        assert resp.status_code == 200
        # Pending should be cleared
        assert srv._pending_token_filters.get("tok-eee") is None

    # -- POST /sessions/token/{token}/filter (connected path) --

    def test_post_filter_connected_applies_immediately(self, client):
        """POST on connected token applies filter to session directly."""
        import mcp_bridge.server as srv

        srv._token_sessions["tok-fff"] = "session-456"
        resp = client.post(
            "/sessions/token/tok-fff/filter",
            json={"disabled_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "session-456"
        assert "everything" in data["disabled_servers"]
        # Verify _session_disabled was set
        assert "everything" in srv._session_disabled.get("session-456", set())

    def test_post_filter_connected_enable_single(self, client):
        """POST enable on connected token removes server from disabled set."""
        import mcp_bridge.server as srv

        srv._token_sessions["tok-ggg"] = "session-789"
        srv._session_disabled["session-789"] = {"everything", "http-example"}
        resp = client.post(
            "/sessions/token/tok-ggg/filter",
            json={"enable": "everything"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "everything" not in data["disabled_servers"]
        assert "http-example" in data["disabled_servers"]

    def test_post_filter_connected_disable_single(self, client):
        """POST disable on connected token adds server to disabled set."""
        import mcp_bridge.server as srv

        srv._token_sessions["tok-hhh"] = "session-101"
        resp = client.post(
            "/sessions/token/tok-hhh/filter",
            json={"disable": "everything"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "everything" in data["disabled_servers"]
        assert "everything" in srv._session_disabled.get("session-101", set())

    # -- DELETE /sessions/token/{token}/filter --

    def test_delete_filter_pending_clears(self, client):
        """DELETE on unconnected token clears pending state."""
        import mcp_bridge.server as srv

        srv._pending_token_filters["tok-iii"] = {"everything"}
        resp = client.delete("/sessions/token/tok-iii/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert data["session_id"] is None
        assert srv._pending_token_filters.get("tok-iii") is None

    def test_delete_filter_connected_clears(self, client):
        """DELETE on connected token clears the session filter."""
        import mcp_bridge.server as srv

        srv._token_sessions["tok-jjj"] = "session-202"
        srv._session_disabled["session-202"] = {"everything"}
        resp = client.delete("/sessions/token/tok-jjj/filter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "cleared"
        assert "everything" in data["previously_disabled"]
        assert srv._session_disabled.get("session-202") is None


# ── allowed_servers → disabled inversion ───────────────────────────


class TestAllowedToDisabledConversion:
    """Tests for the allowed_servers → disabled_servers inversion logic
    in POST /sessions/{id}/filter.

    The fixture config has servers: everything, disabled-server,
    http-example, sharedserver-example.  Only 'everything' is enabled
    by default.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_session_state()
        yield
        _reset_session_state()

    @pytest.fixture
    def client(self):
        bridge, _config = _make_bridge_app()
        app = bridge.http_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_allowed_inverts_to_disabled(self, client):
        """allowed_servers=['everything'] disables all others."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": ["everything"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        disabled = data["disabled_servers"]
        assert "everything" not in disabled
        # Other configured servers should be disabled
        for name in ("disabled-server", "http-example", "sharedserver-example"):
            assert name in disabled

    def test_allowed_empty_disables_all(self, client):
        """allowed_servers=[] disables every server."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        disabled = data["disabled_servers"]
        for name in ("everything", "disabled-server", "http-example", "sharedserver-example"):
            assert name in disabled

    def test_allowed_all_disables_none(self, client):
        """allowed_servers with all servers disables nothing."""
        all_servers = ["everything", "disabled-server", "http-example", "sharedserver-example"]
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": all_servers},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["disabled_servers"] == []

    def test_allowed_invalid_type(self, client):
        """allowed_servers must be a list."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"allowed_servers": "everything"},
        )
        assert resp.status_code == 400

    def test_disabled_unknown_server_rejected(self, client):
        """disabled_servers with unknown server names returns 400."""
        resp = client.post(
            "/sessions/test-session/filter",
            json={"disabled_servers": ["nonexistent-server"]},
        )
        assert resp.status_code == 400
        assert "Unknown servers" in resp.json()["error"]
