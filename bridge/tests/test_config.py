"""Tests for mcp-bridge config loading."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from mcp_bridge.config import (
    BridgeConfig,
    ServerConfig,
    _interpolate_str,
    _interpolate_dict,
    _interpolate_list,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ── Config loading ─────────────────────────────────────────────────


def test_load_config() -> None:
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    assert "everything" in config.servers
    assert "disabled-server" in config.servers
    assert "http-example" in config.servers


def test_server_config_from_dict_stdio() -> None:
    srv = ServerConfig.from_dict(
        "test",
        {
            "command": "npx",
            "args": ["-y", "some-package"],
            "env": {"KEY": "value"},
        },
    )
    assert srv.name == "test"
    assert srv.transport.value == "stdio"
    assert srv.command == "npx"
    assert srv.args == ["-y", "some-package"]
    assert not srv.disabled


def test_server_config_from_dict_http() -> None:
    srv = ServerConfig.from_dict(
        "remote",
        {
            "url": "http://example.com/mcp",
            "headers": {"Authorization": "Bearer token"},
        },
    )
    assert srv.name == "remote"
    assert srv.transport.value == "http"
    assert srv.url == "http://example.com/mcp"


def test_enabled_servers() -> None:
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    enabled = config.get_enabled_servers()
    assert "everything" in enabled
    assert "disabled-server" not in enabled
    assert "http-example" not in enabled


def test_to_fastmcp_config_stdio() -> None:
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    fmcp = config.to_fastmcp_config("everything")
    dumped = fmcp.model_dump(exclude_none=True)
    assert "mcpServers" in dumped
    assert "default" in dumped["mcpServers"]
    assert dumped["mcpServers"]["default"]["command"] == "npx"


def test_to_fastmcp_config_http() -> None:
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    fmcp = config.to_fastmcp_config("http-example")
    dumped = fmcp.model_dump(exclude_none=True)
    assert dumped["mcpServers"]["default"]["url"] == "http://localhost:9999/mcp"
    assert dumped["mcpServers"]["default"]["transport"] == "http"


# ── Environment variable expansion ─────────────────────────────────


class TestInterpolateStr:
    """Unit tests for ``_interpolate_str``."""

    def test_simple_var(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _interpolate_str("${MY_VAR}") == "hello"

    def test_env_prefix(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _interpolate_str("${env:MY_VAR}") == "hello"

    def test_var_with_default_set(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "real"}):
            assert _interpolate_str("${MY_VAR:-fallback}") == "real"

    def test_var_with_default_unset(self) -> None:
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            assert _interpolate_str("${UNSET_VAR_XYZ:-fallback}") == "fallback"

    def test_env_prefix_with_default(self) -> None:
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            assert _interpolate_str("${env:UNSET_VAR_XYZ:-fallback}") == "fallback"

    def test_unset_no_default_returns_empty(self) -> None:
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            assert _interpolate_str("${UNSET_VAR_XYZ}") == ""

    def test_embedded_in_string(self) -> None:
        with patch.dict(os.environ, {"HOST": "example.com", "PORT": "8080"}):
            assert _interpolate_str("http://${HOST}:${PORT}/mcp") == "http://example.com:8080/mcp"

    def test_multiple_vars(self) -> None:
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            assert _interpolate_str("${A}-${B}") == "1-2"

    def test_no_vars_passthrough(self) -> None:
        assert _interpolate_str("plain string") == "plain string"

    def test_empty_string(self) -> None:
        assert _interpolate_str("") == ""

    def test_default_with_special_chars(self) -> None:
        """Default values can contain paths, colons, etc."""
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            assert _interpolate_str("${UNSET_VAR_XYZ:-/usr/local/bin}") == "/usr/local/bin"

    def test_default_empty_string(self) -> None:
        """``${VAR:-}`` with empty default is same as ``${VAR}``."""
        env = os.environ.copy()
        env.pop("UNSET_VAR_XYZ", None)
        with patch.dict(os.environ, env, clear=True):
            assert _interpolate_str("${UNSET_VAR_XYZ:-}") == ""


class TestInterpolateList:
    """Unit tests for ``_interpolate_list``."""

    def test_list_expansion(self) -> None:
        with patch.dict(os.environ, {"PKG": "my-pkg"}):
            result = _interpolate_list(["-y", "${PKG}", "plain"])
            assert result == ["-y", "my-pkg", "plain"]

    def test_empty_list(self) -> None:
        assert _interpolate_list([]) == []


class TestInterpolateDict:
    """Unit tests for ``_interpolate_dict``."""

    def test_dict_values_expanded(self) -> None:
        with patch.dict(os.environ, {"TOKEN": "secret123"}):
            result = _interpolate_dict({"Authorization": "Bearer ${TOKEN}"})
            assert result == {"Authorization": "Bearer secret123"}

    def test_dict_keys_not_expanded(self) -> None:
        with patch.dict(os.environ, {"K": "key"}):
            result = _interpolate_dict({"${K}": "val"})
            assert result == {"${K}": "val"}  # keys are NOT interpolated

    def test_empty_dict(self) -> None:
        assert _interpolate_dict({}) == {}


class TestExpansionInConfig:
    """Integration tests: env vars expanded through ``to_fastmcp_config``."""

    def test_command_expanded(self) -> None:
        """``command`` field expands ``${VAR}``."""
        with patch.dict(os.environ, {"MY_CMD": "/usr/local/bin/my-server"}):
            srv = ServerConfig.from_dict("t", {"command": "${MY_CMD}", "args": []})
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["command"] == "/usr/local/bin/my-server"

    def test_args_expanded(self) -> None:
        """``args`` list entries expand ``${VAR}``."""
        with patch.dict(os.environ, {"PKG": "cool-pkg"}):
            srv = ServerConfig.from_dict("t", {"command": "npx", "args": ["-y", "${PKG}"]})
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["args"] == ["-y", "cool-pkg"]

    def test_env_expanded(self) -> None:
        """``env`` dict values expand ``${VAR}``."""
        with patch.dict(os.environ, {"SECRET": "s3cr3t"}):
            srv = ServerConfig.from_dict("t", {"command": "npx", "env": {"API_KEY": "${SECRET}"}})
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["env"]["API_KEY"] == "s3cr3t"

    def test_url_expanded(self) -> None:
        """``url`` field expands ``${VAR}``."""
        with patch.dict(os.environ, {"MCP_HOST": "remote.example.com"}):
            srv = ServerConfig.from_dict(
                "t", {"url": "https://${MCP_HOST}/mcp", "transport": "http"}
            )
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["url"] == "https://remote.example.com/mcp"

    def test_headers_expanded(self) -> None:
        """``headers`` dict values expand ``${VAR}``."""
        with patch.dict(os.environ, {"TOKEN": "tok123"}):
            srv = ServerConfig.from_dict(
                "t",
                {
                    "url": "http://localhost/mcp",
                    "transport": "http",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                },
            )
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["headers"]["Authorization"] == "Bearer tok123"

    def test_default_fallback_in_config(self) -> None:
        """``${VAR:-default}`` works end-to-end through config."""
        env = os.environ.copy()
        env.pop("MISSING_PORT", None)
        with patch.dict(os.environ, env, clear=True):
            srv = ServerConfig.from_dict(
                "t",
                {"url": "http://localhost:${MISSING_PORT:-3000}/mcp", "transport": "http"},
            )
            config = BridgeConfig(servers={"t": srv})
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["url"] == "http://localhost:3000/mcp"

    def test_raw_config_not_mutated(self) -> None:
        """Expansion happens at ``to_fastmcp_config`` time, not at load time."""
        with patch.dict(os.environ, {"MY_CMD": "resolved"}):
            srv = ServerConfig.from_dict("t", {"command": "${MY_CMD}", "args": []})
            config = BridgeConfig(servers={"t": srv})
            # Raw value still has the template
            assert config.servers["t"].command == "${MY_CMD}"
            # Expanded value is different
            dumped = config.to_fastmcp_config("t").model_dump(exclude_none=True)
            assert dumped["mcpServers"]["default"]["command"] == "resolved"
