"""Unit tests for the advertised UI origin resolver (ui_host/advertise.py).

The tiers are exercised with the subprocess probes monkeypatched — no real
tailscale/ifconfig dependency in CI. URL construction only: the resolver
never affects binding.
"""

from __future__ import annotations

import ipaddress

import pytest

from mcp_combiner.ui_host.advertise import (
    _TAILSCALE_V4_NET,
    advertised_origin,
    reset_tailnet_info,
    resolve_advertised_host,
    tailnet_info,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_COMBINER_UI_ADVERTISE", raising=False)
    # No tailscale status unless a test gives one (never the real machine's),
    # and a fresh lookup each test (it is cached per process).
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_status", lambda: (None, None))
    reset_tailnet_info()


def test_cgnat_range_shape() -> None:
    assert ipaddress.ip_address("100.101.102.103") in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("100.63.0.1") not in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("100.128.0.1") not in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("192.168.1.4") not in _TAILSCALE_V4_NET


def test_mode_loopback_forces_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: "100.1.2.3")
    assert resolve_advertised_host("127.0.0.1", "loopback") == "127.0.0.1"


def test_auto_prefers_tailscale_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 1 outranks the interface scan — it exists to avoid ISP-CGNAT false positives."""
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: "100.64.5.5")
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: "100.100.0.9")
    assert resolve_advertised_host("127.0.0.1") == "100.64.5.5"


def test_auto_falls_back_to_interface_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: "100.115.92.195")
    assert resolve_advertised_host("127.0.0.1") == "100.115.92.195"


def test_auto_without_tailscale_is_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: None)
    assert resolve_advertised_host("127.0.0.1") == "127.0.0.1"


def test_explicit_bare_host() -> None:
    assert (
        resolve_advertised_host("127.0.0.1", "combiner.tailnet.ts.net") == "combiner.tailnet.ts.net"
    )


def test_explicit_origin_strips_scheme() -> None:
    assert resolve_advertised_host("127.0.0.1", "https://combiner.tailnet.ts.net") == (
        "combiner.tailnet.ts.net"
    )


def test_env_var_selects_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setenv("MCP_COMBINER_UI_ADVERTISE", "loopback")
    assert resolve_advertised_host("127.0.0.1") == "127.0.0.1"


def test_origin_composition() -> None:
    assert (
        advertised_origin("127.0.0.1", 9741, "combiner.example") == "http://combiner.example:9741"
    )
    assert advertised_origin("127.0.0.1", 9741, "loopback") == "http://127.0.0.1:9741"


def test_auto_prefers_the_magicdns_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mcp_combiner.tailnet._tailscale_status",
        lambda: ("box.tailnet.ts.net", "100.64.5.5"),
    )
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: "100.64.5.5")
    assert resolve_advertised_host("127.0.0.1") == "box.tailnet.ts.net"
    assert advertised_origin("127.0.0.1", 9741, "AUTO") == "http://box.tailnet.ts.net:9741"


def test_auto_placeholder_inside_an_explicit_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mcp_combiner.tailnet._tailscale_status",
        lambda: ("box.tailnet.ts.net", "100.64.5.5"),
    )
    assert advertised_origin("127.0.0.1", 9741, "https://{auto}:443") == (
        "http://box.tailnet.ts.net:443"
    )
    assert advertised_origin("127.0.0.1", 9741, "{auto}") == "http://box.tailnet.ts.net:9741"
    # A host that happens to be called "auto" is just a host.
    assert advertised_origin("127.0.0.1", 9741, "http://auto:8000") == "http://auto:8000"


def test_auto_placeholder_without_a_tailnet_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: None)
    assert resolve_advertised_host("0.0.0.0", "https://{auto}") == "127.0.0.1"


@pytest.mark.parametrize(
    ("mode", "origin"),
    [
        ("http://box.tailnet.ts.net", "http://box.tailnet.ts.net:9741"),
        ("http://box.tailnet.ts.net:443", "http://box.tailnet.ts.net:443"),
        ("box.tailnet.ts.net:8080/", "http://box.tailnet.ts.net:8080"),
        ("http://[fd7a::1]:9000", "http://[fd7a::1]:9000"),
        ("fd7a::1", "http://[fd7a::1]:9741"),
    ],
)
def test_an_explicit_origin_keeps_its_own_port(mode: str, origin: str) -> None:
    assert advertised_origin("127.0.0.1", 9741, mode) == origin


def test_one_status_call_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def status() -> tuple[str | None, str | None]:
        calls.append(1)
        return "box.tailnet.ts.net", "100.64.5.5"

    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_status", status)
    reset_tailnet_info()
    for _ in range(3):
        assert tailnet_info() == ("box.tailnet.ts.net", "100.64.5.5")
    assert resolve_advertised_host("127.0.0.1") == "box.tailnet.ts.net"
    assert calls == [1]


def test_status_without_an_ip_falls_back_to_the_other_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_status", lambda: ("box.ts.net", None))
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: "100.115.92.195")
    reset_tailnet_info()
    assert tailnet_info() == ("box.ts.net", "100.115.92.195")
