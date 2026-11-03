"""``oauth.public_url``: the portable ``auto`` setting and the env override."""

from __future__ import annotations

import pytest

from mcp_combiner.config import PUBLIC_URL_ENV, OAuthConfig, resolve_public_url
from mcp_combiner.runtime import RUNTIME

NAME = "box.tailnet.ts.net"


@pytest.fixture(autouse=True)
def _tailnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PUBLIC_URL_ENV, raising=False)
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_dns_name", lambda: NAME)
    monkeypatch.setattr(RUNTIME, "serve_port", 9741)


@pytest.mark.parametrize(
    ("setting", "resolved"),
    [
        ("auto", f"http://{NAME}:9741"),
        ("AUTO", f"http://{NAME}:9741"),
        ("{auto}", f"http://{NAME}:9741"),
        ("http://{auto}", f"http://{NAME}:9741"),
        ("https://{auto}", f"https://{NAME}"),
        ("https://{auto}:8443/", f"https://{NAME}:8443"),
        ("http://{auto}:80/prefix", f"http://{NAME}:80/prefix"),
        ("https://auto", "https://auto"),  # a host named auto is just a host
        ("https://combiner.example.com/", "https://combiner.example.com"),
        ("combiner.example.com:9741", "http://combiner.example.com:9741"),
        ("combiner.example.com", "http://combiner.example.com:9741"),
        ("http://[fd7a::1]", "http://[fd7a::1]:9741"),
        ("http://[fd7a::1]:80", "http://[fd7a::1]:80"),
        ("", None),
        (None, None),
    ],
)
def test_setting_forms(setting: str | None, resolved: str | None) -> None:
    assert resolve_public_url(setting) == resolved


def test_the_env_var_is_a_literal_url_and_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC_URL_ENV, "https://auth.example.com/")
    assert resolve_public_url("auto") == "https://auth.example.com"
    assert OAuthConfig.from_dict({}).public_url == "https://auth.example.com"
    monkeypatch.setenv(PUBLIC_URL_ENV, "https://{auto}")  # not special in the env
    assert resolve_public_url(None) == "https://{auto}"
    # A bare host (as easily typed into a shell profile) is completed.
    monkeypatch.setenv(PUBLIC_URL_ENV, "box.example.ts.net")
    assert resolve_public_url(None) == "http://box.example.ts.net:9741"


def test_auto_without_a_tailnet_means_loopback_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_dns_name", lambda: None)
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.ui_host.advertise._interface_scan_ip", lambda: None)
    assert resolve_public_url("auto") is None
    config = OAuthConfig.from_dict({"public_url": "auto"})
    assert config.public_url is None and config.prompt is None


def test_the_config_stores_the_resolved_url() -> None:
    assert OAuthConfig.from_dict({"public_url": "auto"}).public_url == f"http://{NAME}:9741"
