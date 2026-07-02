"""Tool parameter-schema normalization (combiner side).

The Python mirror of the Lua ``mcp_companion.schema`` module. A single function
guarantees a tool's parameter schema is a strict, valid JSON-schema object so a
strict adapter (e.g. the Copilot HTTP adapter) never sees ``[]`` or a malformed
``required``:

  * non-dict / empty            → ``{"type": "object", "properties": {}}``
  * missing ``type``            → ``"object"``
  * missing / non-dict props    → ``{}``
  * ``required`` a bare string  → wrapped in a single-element array (likely intent)
  * ``required`` any other non-list → dropped (uncoercible)

Used both where neovim virtual tools are built (``nvim_proxy``) and at the
tools/list egress (``server._coerce_object_schemas``) so every tool source is
normalized the same way.
"""

from __future__ import annotations

from typing import Any


def normalize_object_schema(params: Any) -> dict[str, Any]:
    """Coerce *params* into a valid object schema. Pure; returns a new dict."""
    if not isinstance(params, dict) or not params:
        return {"type": "object", "properties": {}}

    out = dict(params)
    if out.get("type") is None:
        out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    # `required` must be a JSON array of field names. Coerce a bare string into a
    # single-element array (the likely intent); drop any other non-list value.
    if "required" in out and not isinstance(out["required"], list):
        req = out["required"]
        if isinstance(req, str):
            out["required"] = [req]
        else:
            del out["required"]
    return out
