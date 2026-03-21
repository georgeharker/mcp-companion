"""Tests for mcp-bridge config loading."""

from pathlib import Path

from mcp_bridge.config import BridgeConfig, ServerConfig


FIXTURES = Path(__file__).parent / "fixtures"


def test_load_config():
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    assert "everything" in config.servers
    assert "disabled-server" in config.servers
    assert "http-example" in config.servers


def test_server_config_from_dict_stdio():
    srv = ServerConfig.from_dict(
        "test",
        {
            "command": "npx",
            "args": ["-y", "some-package"],
            "env": {"KEY": "value"},
        },
    )
    assert srv.name == "test"
    assert srv.transport == "stdio"
    assert srv.command == "npx"
    assert srv.args == ["-y", "some-package"]
    assert not srv.disabled


def test_server_config_from_dict_http():
    srv = ServerConfig.from_dict(
        "remote",
        {
            "url": "http://example.com/mcp",
            "headers": {"Authorization": "Bearer token"},
        },
    )
    assert srv.name == "remote"
    assert srv.transport == "http"
    assert srv.url == "http://example.com/mcp"


def test_enabled_servers():
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    enabled = config.get_enabled_servers()
    assert "everything" in enabled
    assert "disabled-server" not in enabled
    assert "http-example" not in enabled


def test_to_fastmcp_config_stdio():
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    fmcp = config.to_fastmcp_config("everything")
    assert "mcpServers" in fmcp
    assert "default" in fmcp["mcpServers"]
    assert fmcp["mcpServers"]["default"]["command"] == "npx"


def test_to_fastmcp_config_http():
    config = BridgeConfig.load(str(FIXTURES / "servers.json"))
    fmcp = config.to_fastmcp_config("http-example")
    assert fmcp["mcpServers"]["default"]["url"] == "http://localhost:9999/mcp"
    assert fmcp["mcpServers"]["default"]["transport"] == "http"
