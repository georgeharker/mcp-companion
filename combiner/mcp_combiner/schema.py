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


def wire_conformant_schema(schema: Any) -> dict[str, Any]:
    """*schema* repaired to what the MCP wire format accepts for a tool's
    ``inputSchema`` / ``outputSchema`` (mcp 2 validates both, sending and
    receiving, and rejects a whole ``tools/list`` over one bad tool).

    Only what the wire rejects is changed: a non-dict / empty schema becomes
    an empty object schema; the root ``type`` becomes ``"object"``; a
    non-dict ``properties`` becomes ``{}`` and each value that isn't a schema
    (dict) or boolean becomes ``{}`` (accept anything); ``required`` keeps only
    strings (a bare string is wrapped). An absent ``properties`` stays absent
    — it is optional on the wire. Pure; returns *schema* itself when it
    already conforms.
    """
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out["type"] = "object"
    if "properties" in out:
        props = out["properties"]
        if not isinstance(props, dict):
            out["properties"] = {}
        elif any(not isinstance(v, dict | bool) for v in props.values()):
            out["properties"] = {
                k: v if isinstance(v, dict | bool) else {} for k, v in props.items()
            }
    if "required" in out:
        required = out["required"]
        if isinstance(required, str):
            out["required"] = [required]
        elif not isinstance(required, list):
            del out["required"]
        elif any(not isinstance(r, str) for r in required):
            out["required"] = [r for r in required if isinstance(r, str)]
    return schema if out == schema else out
