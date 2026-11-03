"""Binding to a list of addresses (``bind.host`` / ``--host`` / ``MCP_COMBINER_HOST``)."""

from __future__ import annotations

import errno
import logging
import socket
from pathlib import Path

import httpx2
import pytest
from conftest import ProcFactory, free_port, free_port_pair, write_servers_config

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
        assert binding.parse_hosts(["127.0.0.1", " auto "]) == ["127.0.0.1", "auto"]
        assert binding.parse_hosts("") == []
        assert binding.parse_hosts(None) == []

    def test_auto_resolves_to_the_tailnet_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(binding, "tailnet_address", lambda: "100.101.102.103")
        assert binding.resolve_hosts(["127.0.0.1", "auto"]) == [
            "127.0.0.1",
            "100.101.102.103",
        ]

    def test_auto_without_a_tailnet_is_skipped_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(binding, "tailnet_address", lambda: None)
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            assert binding.resolve_hosts(["127.0.0.1", "Auto"]) == ["127.0.0.1"]
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


@pytest.mark.e2e
async def test_a_busy_relay_port_does_not_stop_the_combiner(
    procs: ProcFactory, tmp_path: Path
) -> None:
    """The UI relay (port + 1) only serves widgets: losing it must not keep the
    combiner from serving — as before the relay was bound up front."""
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", port + 1))
        squatter.listen(1)
        config = write_servers_config(tmp_path / "servers.json", {})
        combiner = await procs.start_combiner(
            config, port=port, extra_args=["--log-file", str(tmp_path / "combiner.log")]
        )
        async with httpx2.AsyncClient(timeout=5.0) as http:
            assert (await http.get(f"http://127.0.0.1:{combiner.port}/health")).status_code == 200
    assert "UI relay: can't listen" in (tmp_path / "combiner.log").read_text()


def test_an_unresolvable_name_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    port = free_port()
    with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
        sockets = binding.bind_sockets(["127.0.0.1", "no-such-host.invalid"], port)
    try:
        assert len(sockets) == 1
        assert "no-such-host.invalid" in caplog.text
    finally:
        for s in sockets:
            s.close()


class TestUnreachableOrigins:
    PORT = 9741

    def _check(self, url: str, hosts: list[str]) -> list[str]:
        return binding.unreachable_origins([("oauth.public_url", url)], hosts, self.PORT)

    def test_a_bound_address_is_fine(self) -> None:
        assert self._check("http://127.0.0.1:9741", ["127.0.0.1"]) == []
        assert self._check("http://localhost:9741", ["127.0.0.1", "::1"]) == []

    def test_an_unbound_address_warns(self) -> None:
        (warning,) = self._check(f"http://{TEST_NET}:9741", ["127.0.0.1"])
        assert TEST_NET in warning and "listens only on 127.0.0.1" in warning

    def test_an_unresolvable_name_warns(self) -> None:
        (warning,) = self._check("http://no-such-host.invalid:9741", ["127.0.0.1"])
        assert "doesn't resolve" in warning

    def test_proxied_and_wildcard_setups_are_not_checked(self) -> None:
        assert self._check(f"https://{TEST_NET}", ["127.0.0.1"]) == []  # e.g. tailscale serve
        assert self._check(f"http://{TEST_NET}:8080", ["127.0.0.1"]) == []  # another port
        assert self._check(f"http://{TEST_NET}:9741", ["0.0.0.0"]) == []  # every interface


@pytest.mark.e2e
async def test_startup_warns_about_an_unreachable_public_url(
    procs: ProcFactory, tmp_path: Path
) -> None:
    port = free_port_pair()
    config = write_servers_config(
        tmp_path / "servers.json", {}, oauth={"public_url": f"http://{TEST_NET}:{port}"}
    )
    await procs.start_combiner(
        config, port=port, extra_args=["--log-file", str(tmp_path / "combiner.log")]
    )
    log = (tmp_path / "combiner.log").read_text()
    assert f"bind: oauth.public_url http://{TEST_NET}:{port}" in log
    assert "add it to bind.host" in log
