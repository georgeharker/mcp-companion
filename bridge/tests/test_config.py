"""Tests for mcp-bridge config loading."""

from __future__ import annotations

from pathlib import Path

from mcp_bridge.config import BridgeConfig, ServerConfig


FIXTURES = Path(__file__).parent / "fixtures"


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
