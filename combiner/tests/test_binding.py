"""Binding to a list of addresses (``bind.host`` / ``--host`` / ``MCP_COMBINER_HOST``)."""

from __future__ import annotations

import errno
import logging
import socket
from pathlib import Path

import httpx2
import pytest
from conftest import ProcFactory, free_port, write_servers_config

from mcp_combiner import binding
from mcp_combiner.asgi import resolve_bind_hosts

TEST_NET = "192.0.2.1"  # RFC 5737: never configured on a real interface


def _has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


class TestParseAndResolve:
    def test_parse_accepts_strings_lists_and_dedupes(self) -> None:
        assert binding.parse_hosts("127.0.0.1, ::1,127.0.0.1") == ["127.0.0.1", "::1"]
        assert binding.parse_hosts(["127.0.0.1", " tailscale "]) == ["127.0.0.1", "tailscale"]
        assert binding.parse_hosts("") == []
        assert binding.parse_hosts(None) == []

    def test_tailscale_resolves_to_the_tailnet_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(binding, "tailnet_address", lambda: "100.101.102.103")
        assert binding.resolve_hosts(["127.0.0.1", "tailscale"]) == [
            "127.0.0.1",
            "100.101.102.103",
        ]

    def test_missing_tailscale_is_skipped_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(binding, "tailnet_address", lambda: None)
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            assert binding.resolve_hosts(["127.0.0.1", "Tailscale"]) == ["127.0.0.1"]
        assert "no Tailscale address" in caplog.text

    def test_a_wildcard_covers_the_rest(self) -> None:
        assert binding.resolve_hosts(["127.0.0.1", "0.0.0.0", "::1"]) == ["0.0.0.0"]
        assert binding.resolve_hosts(["0.0.0.0", "::"]) == ["0.0.0.0", "::"]

    def test_empty_means_loopback(self) -> None:
        assert binding.resolve_hosts([]) == ["127.0.0.1"]

    def test_precedence_cli_env_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = write_servers_config(
            tmp_path / "servers.json", {}, bind={"host": ["::1", "127.0.0.1"]}
        )
        monkeypatch.delenv("MCP_COMBINER_HOST", raising=False)
        assert resolve_bind_hosts(None, str(config)) == ["::1", "127.0.0.1"]
        monkeypatch.setenv("MCP_COMBINER_HOST", "127.0.0.1,::1")
        assert resolve_bind_hosts(None, str(config)) == ["127.0.0.1", "::1"]
        assert resolve_bind_hosts("::1", str(config)) == ["::1"]

    def test_loopback_detection(self) -> None:
        assert binding.is_loopback("127.0.0.1") and binding.is_loopback("::1")
        assert binding.is_loopback("localhost")
        assert not binding.is_loopback("100.101.102.103") and not binding.is_loopback("0.0.0.0")


class TestBindSockets:
    def test_unavailable_addresses_are_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        port = free_port()
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            sockets = binding.bind_sockets(["127.0.0.1", TEST_NET], port)
        try:
            assert [s.getsockname()[:2] for s in sockets] == [("127.0.0.1", port)]
            assert f"{TEST_NET}:{port} isn't available" in caplog.text
        finally:
            for s in sockets:
                s.close()

    def test_nothing_available_is_an_error(self) -> None:
        with pytest.raises(OSError) as caught:
            binding.bind_sockets([TEST_NET], free_port())
        assert caught.value.errno == errno.EADDRNOTAVAIL

    def test_a_port_in_use_is_fatal_and_releases_what_bound(self) -> None:
        port = free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
            squatter.bind(("127.0.0.1", port))
            squatter.listen(1)
            with pytest.raises(OSError) as caught:
                binding.bind_sockets(["127.0.0.1"], port)
        assert caught.value.errno == errno.EADDRINUSE


@pytest.mark.e2e
@pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback here")
async def test_a_combiner_serves_on_every_listed_address(
    procs: ProcFactory, tmp_path: Path
) -> None:
    config = write_servers_config(
        tmp_path / "servers.json", {}, bind={"host": ["127.0.0.1", "::1", TEST_NET]}
    )
    combiner = await procs.start_combiner(
        config, extra_args=["--log-file", str(tmp_path / "combiner.log")]
    )
    async with httpx2.AsyncClient(timeout=5.0) as http:
        for base in (f"http://127.0.0.1:{combiner.port}", f"http://[::1]:{combiner.port}"):
            response = await http.get(f"{base}/health")
            assert response.status_code == 200, base
        # The UI relay (port + 1) follows the same bind.
        relay = await http.get(f"http://[::1]:{combiner.port + 1}/")
        assert relay.status_code < 500
    assert f"{TEST_NET}:{combiner.port} isn't available" in (tmp_path / "combiner.log").read_text()
