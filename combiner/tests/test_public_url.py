"""``oauth.public_url``: the portable ``auto`` setting and the env override."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from mcp_combiner.config import (
    PUBLIC_URL_ENV,
    CombinerConfig,
    OAuthConfig,
    resolve_public_url,
)
from mcp_combiner.runtime import RUNTIME
from mcp_combiner.ui_host.advertise import reset_tailnet_info

NAME = "box.tailnet.ts.net"


@pytest.fixture(autouse=True)
def _tailnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PUBLIC_URL_ENV, raising=False)
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_status", lambda: (NAME, None))
    reset_tailnet_info()
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
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_status", lambda: (None, None))
    monkeypatch.setattr("mcp_combiner.tailnet._tailscale_ip", lambda: None)
    monkeypatch.setattr("mcp_combiner.tailnet._interface_scan_ip", lambda: None)
    reset_tailnet_info()
    assert resolve_public_url("auto") is None
    config = OAuthConfig.from_dict({"public_url": "auto"})
    assert config.public_url is None and config.prompt is None


def test_the_config_stores_the_resolved_url() -> None:
    assert OAuthConfig.from_dict({"public_url": "auto"}).public_url == f"http://{NAME}:9741"


# ── {oauth_redirect} in server entries ─────────────────────────────


def _load(tmp_path: Path, doc: dict[str, object]) -> CombinerConfig:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps(doc))
    return CombinerConfig.load(str(path))


def test_oauth_redirect_expands_per_server(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        {
            "oauth": {"public_url": "auto"},
            "servers": {
                "gws": {
                    "command": "gws-mcp",
                    "env": {"REDIRECT_URI": "{oauth_redirect}"},
                    "args": ["--redirect", "{oauth_redirect}"],
                },
                "other": {"url": "http://127.0.0.1:1/mcp?cb={oauth_redirect}"},
            },
        },
    )
    gws = f"http://{NAME}:9741/oauth/callback/gws"
    assert config.servers["gws"].env == {"REDIRECT_URI": gws}
    assert config.servers["gws"].args == ["--redirect", gws]
    assert config.servers["other"].url == (
        f"http://127.0.0.1:1/mcp?cb=http://{NAME}:9741/oauth/callback/other"
    )


def test_oauth_redirect_in_a_backing_entry_names_its_one_user(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        {
            "oauth": {"public_url": "https://auth.example.com"},
            "sharedServers": {
                "gws-proc": {"command": "gws", "env": {"CB": "{oauth_redirect}"}},
                "shared-twice": {"command": "x", "env": {"CB": "{oauth_redirect}"}},
            },
            "servers": {
                "gws": {"url": "http://127.0.0.1:1/mcp", "sharedServer": "gws-proc"},
                "a": {"url": "http://127.0.0.1:2/mcp", "sharedServer": "shared-twice"},
                "b": {"url": "http://127.0.0.1:3/mcp", "sharedServer": "shared-twice"},
            },
        },
    )
    assert config.shared_servers["gws-proc"].env == {
        "CB": "https://auth.example.com/oauth/callback/gws"
    }
    assert config.shared_servers["shared-twice"].env == {"CB": "{oauth_redirect}"}


def test_oauth_redirect_needs_a_public_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
        config = _load(tmp_path, {"servers": {"s": {"command": "x", "args": ["{oauth_redirect}"]}}})
    assert config.servers["s"].args == ["{oauth_redirect}"]
    assert "needs oauth.public_url" in caplog.text


def test_the_env_url_applies_without_an_oauth_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PUBLIC_URL_ENV, "https://auth.example.com")
    config = _load(tmp_path, {"servers": {"s": {"command": "x", "args": ["{oauth_redirect}"]}}})
    assert config.oauth.public_url == "https://auth.example.com"
    assert config.servers["s"].args == ["https://auth.example.com/oauth/callback/s"]
