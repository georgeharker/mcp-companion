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
    resolve_advertised_host,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_COMBINER_UI_ADVERTISE", raising=False)


def test_cgnat_range_shape() -> None:
    assert ipaddress.ip_address("100.101.102.103") in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("100.63.0.1") not in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("100.128.0.1") not in _TAILSCALE_V4_NET
    assert ipaddress.ip_address("192.168.1.4") not in _TAILSCALE_V4_NET


def test_mode_loopback_forces_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: "100.1.2.3")
    assert resolve_advertised_host("127.0.0.1", "loopback") == "127.0.0.1"


def test_auto_prefers_tailscale_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 1 outranks the interface scan — it exists to avoid ISP-CGNAT false positives."""
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: "100.64.5.5")
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._interface_scan_ip", lambda: "100.100.0.9")
    assert resolve_advertised_host("127.0.0.1") == "100.64.5.5"


def test_auto_falls_back_to_interface_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: None)
    monkeypatch.setattr(
        "mcp_combiner.ui_host.advertise._interface_scan_ip", lambda: "100.115.92.195"
    )
    assert resolve_advertised_host("127.0.0.1") == "100.115.92.195"


def test_auto_without_tailscale_is_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._interface_scan_ip", lambda: None)
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
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setenv("MCP_COMBINER_UI_ADVERTISE", "loopback")
    assert resolve_advertised_host("127.0.0.1") == "127.0.0.1"


def test_origin_composition() -> None:
    assert (
        advertised_origin("127.0.0.1", 9741, "combiner.example") == "http://combiner.example:9741"
    )
    assert advertised_origin("127.0.0.1", 9741, "loopback") == "http://127.0.0.1:9741"


def test_tailscale_mode_names_the_magicdns_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mcp_combiner.ui_host.advertise._tailscale_dns_name", lambda: "box.tailnet.ts.net"
    )
    assert resolve_advertised_host("127.0.0.1", "tailscale") == "box.tailnet.ts.net"
    assert advertised_origin("127.0.0.1", 9741, "Tailscale") == "http://box.tailnet.ts.net:9741"


def test_tailscale_mode_without_a_name_falls_back_to_the_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_dns_name", lambda: None)
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: "100.64.5.5")
    assert resolve_advertised_host("127.0.0.1", "tailscale") == "100.64.5.5"


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
