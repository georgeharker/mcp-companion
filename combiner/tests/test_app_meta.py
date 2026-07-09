"""MCP-Apps tool→UI-resource linkage under namespaced mount.

FastMCP's ``Namespace`` transform rewrites resource URIs and tool *names* but
not the ``resourceUri`` pointer inside a tool's ``_meta.ui`` — so through the
combiner the pointer (``ui://svg-mcp/preview``) and the namespaced resource it
targets (``ui://svg-mcp/svg-mcp/preview``) diverge and the widget never renders.

These tests pin the combiner-side egress fix: the tool-meta pointer is
forward-namespaced with the *same* transform applied to resources, so
``tools/list``'s pointer matches ``resources/list``'s URI (and reverse-resolves
back to the real upstream URI on read).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp.server.transforms.namespace import Namespace
from fastmcp.tools import Tool

from mcp_combiner.config import CombinerConfig, ServerConfig
from mcp_combiner.middleware import _namespace_app_meta, _rewrite_app_meta
from mcp_combiner.runtime import RUNTIME


def _tool(name: str, meta: dict[str, Any] | None) -> Tool:
    return Tool.from_function(lambda x: x, name="_").model_copy(update={"name": name, "meta": meta})


# ── _rewrite_app_meta (pure dict logic) ────────────────────────────


class TestRewriteAppMeta:
    def test_rewrites_ui_resource_uri(self) -> None:
        ns = Namespace("svg-mcp")
        out = _rewrite_app_meta({"ui": {"resourceUri": "ui://svg-mcp/preview"}}, ns)
        assert out["ui"]["resourceUri"] == "ui://svg-mcp/svg-mcp/preview"

    def test_rewritten_pointer_reverse_resolves_to_upstream(self) -> None:
        """The whole point: the mount's reverse strips exactly one segment,
        landing back on the real upstream URI the server registered."""
        ns = Namespace("svg-mcp")
        out = _rewrite_app_meta({"ui": {"resourceUri": "ui://svg-mcp/preview"}}, ns)
        assert ns._reverse_uri(out["ui"]["resourceUri"]) == "ui://svg-mcp/preview"

    def test_leaves_other_ui_fields_untouched(self) -> None:
        ns = Namespace("svg-mcp")
        out = _rewrite_app_meta(
            {"ui": {"resourceUri": "ui://svg-mcp/preview", "csp": "x", "visibility": ["model"]}},
            ns,
        )
        assert out["ui"]["csp"] == "x"
        assert out["ui"]["visibility"] == ["model"]

    def test_rewrites_openai_output_template(self) -> None:
        ns = Namespace("app")
        out = _rewrite_app_meta({"openai/outputTemplate": "ui://app/widget"}, ns)
        assert out["openai/outputTemplate"] == "ui://app/widget".replace("ui://", "ui://app/")

    def test_noop_returns_same_object(self) -> None:
        ns = Namespace("svg-mcp")
        meta = {"unrelated": 1, "ui": {"visibility": ["model"]}}  # no resourceUri
        assert _rewrite_app_meta(meta, ns) is meta

    def test_non_uri_resource_uri_left_alone(self) -> None:
        ns = Namespace("svg-mcp")
        meta = {"ui": {"resourceUri": "not-a-uri"}}
        assert _rewrite_app_meta(meta, ns) is meta


# ── _namespace_app_meta (egress pass over the tool list) ───────────


class TestNamespaceAppMeta:
    @pytest.fixture
    def _config(self) -> Iterator[None]:
        prev = RUNTIME.config
        RUNTIME.config = CombinerConfig(
            servers={
                "svg-mcp": ServerConfig(name="svg-mcp", url="http://x/mcp"),
                "gws": ServerConfig(name="gws", url="http://y/mcp"),
            }
        )
        yield
        RUNTIME.config = prev

    def test_tool_pointer_matches_namespaced_resource(self, _config: None) -> None:
        """The bug's crux: after the fix, the tool's meta pointer equals the URI
        the resource is listed under (both are Namespace._transform_uri of the
        upstream URI)."""
        upstream_uri = "ui://svg-mcp/preview"
        tool = _tool("svg-mcp_show_widget", {"ui": {"resourceUri": upstream_uri}})

        (rewritten,) = _namespace_app_meta([tool])

        # What resources/list would show for this resource:
        resource_listed_uri = Namespace("svg-mcp")._transform_uri(upstream_uri)
        assert rewritten.meta["ui"]["resourceUri"] == resource_listed_uri

    def test_recovers_namespace_from_tool_name(self, _config: None) -> None:
        # gws-namespaced tool → gws prefix, not svg-mcp
        tool = _tool("gws_open_widget", {"ui": {"resourceUri": "ui://gws/w"}})
        (out,) = _namespace_app_meta([tool])
        assert out.meta["ui"]["resourceUri"] == "ui://gws/gws/w"

    def test_unowned_tools_pass_through(self, _config: None) -> None:
        # combiner meta-tools and virtual neovim_* have no owning server.
        combiner_tool = _tool("combiner__status", {"ui": {"resourceUri": "ui://x/y"}})
        nvim_tool = _tool("neovim_edit_buffer", {"ui": {"resourceUri": "ui://n/e"}})
        out = _namespace_app_meta([combiner_tool, nvim_tool])
        assert out[0].meta["ui"]["resourceUri"] == "ui://x/y"  # untouched
        assert out[1].meta["ui"]["resourceUri"] == "ui://n/e"  # untouched

    def test_tools_without_meta_pass_through(self, _config: None) -> None:
        tool = _tool("svg-mcp_plain", None)
        (out,) = _namespace_app_meta([tool])
        assert out is tool  # no copy when nothing to do

    def test_applied_once_not_double(self, _config: None) -> None:
        """Egress runs every request over the un-rewritten cache base; a single
        pass must not double-namespace."""
        tool = _tool("svg-mcp_show_widget", {"ui": {"resourceUri": "ui://svg-mcp/preview"}})
        (once,) = _namespace_app_meta([tool])
        assert once.meta["ui"]["resourceUri"] == "ui://svg-mcp/svg-mcp/preview"
        # The cache holds the original (un-rewritten) tool; the pass does not
        # mutate it in place, so re-running from the base yields the same result.
        (again,) = _namespace_app_meta([tool])
        assert again.meta["ui"]["resourceUri"] == "ui://svg-mcp/svg-mcp/preview"
        assert tool.meta["ui"]["resourceUri"] == "ui://svg-mcp/preview"  # base untouched
