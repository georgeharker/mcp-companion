"""Tool-schema sanitization and the named schema fixes.

Everything that rewrites a tool's schema on its way to a client lives here —
moved verbatim from server.py during the decomposition:

- ``_sanitize_tools`` / ``_to_clean_tool``: the ingest guard — rebuild tools
  whose schemas can't serialize (circular ``$ref``) before they enter the
  per-server slice cache.
- ``_apply_schema_fixes`` and friends: the named, opt-in fixes
  (``SCHEMA_FIXES``) selected via ``--schema-fix``.
- ``_finalize_schemas`` / ``_coerce_object_schemas``: the single egress
  cleanup applied to the COMPLETE assembled tools/list (upstream + neovim
  virtual tools) — see each docstring for the issue-#7 history.

The enabled fix set is read from ``RUNTIME.schema_fixes`` (frozen at server
creation).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.tools import Tool

from mcp_combiner.runtime import RUNTIME
from mcp_combiner.schema import normalize_object_schema

logger = logging.getLogger("mcp-combiner")


# Named, individually-selectable schema fixes applied to every tool emitted from
# ``tools/list`` (at cache-fill time). Selected via ``--schema-fix`` /
# ``MCP_COMBINER_SCHEMA_FIXES`` (and ``--normalize-schema`` is a back-compat alias
# for ``anyof_type_hoist``). Empty by default — no behavior change unless opted in.
#   * anyof_type_hoist    — hoist a sibling ``type`` into ``anyOf`` items
#                           (moonshot-ai/kimi reject ``type``+``anyOf`` coexistence)
#   * empty_object        — missing ``type`` -> ``object``; an ``object`` without
#                           ``properties`` gets ``properties: {}`` (Copilot/Joplin
#                           reject ``[]`` where an object schema is required)
#   * drop_invalid_required — drop ``required`` when it isn't a list
SCHEMA_FIXES: tuple[str, ...] = ("anyof_type_hoist", "empty_object", "drop_invalid_required")


def _safe_json_clone(obj: object) -> Any:
    """JSON round-trip to break Python-level circular object identity."""
    return json.loads(json.dumps(obj, default=str))


def _sanitize_tools(raw: list[Tool]) -> list[Tool]:
    """Ensure every tool serializes cleanly, rebuilding circular-$ref schemas.

    The single sanitize step for everything that enters the per-server slice
    cache — both the aggregate fetch (_do_fetch) and per-server primes
    (prime_server_tools) — so a slice is identical regardless of which path
    stored it.
    """
    sanitized: list[Tool] = []
    rebuilt: list[str] = []
    for tool in raw:
        try:
            # Exclude fn/serializer to match how tools are actually serialized
            # for the wire (and _to_clean_tool's own verify dump). Without this
            # exclusion, every locally-registered FunctionTool — including the
            # combiner's own meta-tools — would fail here purely on its `fn`
            # field and be needlessly rebuilt on every fetch. What survives to
            # the except is a genuinely broken schema (circular $refs, e.g.
            # Todoist), where model_dump raises "Circular reference detected".
            tool.model_dump(
                by_alias=True, mode="json", exclude_none=True, exclude={"fn", "serializer"}
            )
            sanitized.append(tool)
        except (ValueError, RecursionError):
            # Schema won't round-trip to JSON (circular $refs). Rebuilding as a
            # clean FunctionTool is the expected recovery, so aggregate into one
            # debug line instead of a per-tool warning.
            rebuilt.append(str(tool.name))
            sanitized.append(_to_clean_tool(tool))
    if rebuilt:
        logger.debug(
            "tools/list: rebuilt %d tool(s) with non-serializable schemas: %s",
            len(rebuilt),
            ", ".join(sorted(rebuilt)),
        )
    return sanitized


def _to_clean_tool(tool: Tool) -> Tool:
    """Build a minimal FunctionTool that serializes cleanly.

    We extract only the wire-format fields (name, description, parameters,
    annotations) and construct a new FunctionTool with a dummy fn.
    The original ProxyTool stays in FastMCP's registry for actual execution.
    """
    from fastmcp.tools.function_tool import FunctionTool

    # Clean the parameters via JSON round-trip, then normalize the schema
    # so it is accepted by strict validators (e.g. Moonshot-ai rejects
    # schemas where "type" and "anyOf" coexist at the same level).
    try:
        clean_params = _normalize_schema(_safe_json_clone(tool.parameters))
        if not isinstance(clean_params, dict):
            clean_params = {"type": "object", "properties": {}}
    except (ValueError, RecursionError, TypeError):
        clean_params = {"type": "object", "properties": {}}

    # Clean annotations if present
    clean_annotations: dict[str, Any] | None
    try:
        clean_annotations = _safe_json_clone(
            tool.annotations.model_dump() if tool.annotations else None
        )
    except (ValueError, RecursionError, TypeError, AttributeError):
        clean_annotations = None

    # Preserve the output schema (JSON-cleaned); if it's the thing that can't
    # serialize, the verify+fallback below drops it along with params.
    clean_output: dict[str, Any] | None
    try:
        clean_output = _safe_json_clone(getattr(tool, "output_schema", None))
    except (ValueError, RecursionError, TypeError):
        clean_output = None

    # Build a fresh FunctionTool with no circular refs
    dummy_fn = lambda: None  # noqa: E731 -- never called, just for FunctionTool ctor
    new_tool = FunctionTool(
        fn=dummy_fn,
        name=str(tool.name) if tool.name else "unknown",
        description=str(tool.description) if tool.description else "",
        parameters=clean_params,
        output_schema=clean_output,
        annotations=mt.ToolAnnotations(**clean_annotations) if clean_annotations else None,
    )

    # Verify it serializes (exclude fn which is not serializable)
    try:
        new_tool.model_dump(
            by_alias=True, mode="json", exclude_none=True, exclude={"fn", "serializer"}
        )
    except Exception as e:
        # Last resort: strip parameters entirely
        logger.warning("Tool %s failed serialization, stripping params: %s", tool.name, e)
        new_tool = FunctionTool(
            fn=dummy_fn,
            name=str(tool.name) if tool.name else "unknown",
            description=str(tool.description) if tool.description else "",
            parameters={"type": "object", "properties": {}},
        )

    return new_tool


# Keywords that semantically belong with a specific "type" declaration.
# When we hoist a parent-level "type" into anyOf items, these travel with it.
_TYPE_SIBLING_KEYWORDS = frozenset(
    (
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "properties",
        "required",
        "additionalProperties",
        "patternProperties",
    )
)


def _normalize_schema(schema: object) -> object:
    """Recursively fix schemas rejected by strict JSON Schema validators.

    Some providers (e.g. Moonshot-ai) reject schemas where ``type`` and
    ``anyOf`` coexist at the same level.  Pydantic generates this for
    ``Optional[list[str]]``::

        {"type": "array", "anyOf": [{"items": {...}}, {"type": "null"}]}

    The fix is to promote ``type`` (plus its sibling keywords such as
    ``items``) into each ``anyOf`` item that lacks its own ``type``::

        {"anyOf": [{"type": "array", "items": {...}}, {"type": "null"}]}
    """
    if isinstance(schema, list):
        return [_normalize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    # Recurse into all values first so nested schemas are also clean.
    result: dict[str, Any] = {k: _normalize_schema(v) for k, v in schema.items()}

    if "type" not in result or "anyOf" not in result:
        return result

    # Pull the parent type and any keywords that travel with it.
    parent_type = result.pop("type")
    hoisted: dict[str, Any] = {"type": parent_type}
    for kw in _TYPE_SIBLING_KEYWORDS:
        if kw in result:
            hoisted[kw] = result.pop(kw)

    # Distribute into anyOf items that don't already declare a type.
    result["anyOf"] = [
        ({**hoisted, **item} if "type" not in item else item) for item in result["anyOf"]
    ]
    return result


def _apply_object_fixes(params: dict[str, Any], fixes: frozenset[str]) -> dict[str, Any]:
    """Apply top-level (non-recursive) object-shape fixes to a parameter schema.

    * ``empty_object`` — a missing/None ``type`` becomes ``"object"``, and an
      ``object`` schema without ``properties`` gets ``properties: {}``, so it
      never serializes to ``[]`` where strict adapters (e.g. Copilot) require an
      object.
    * ``drop_invalid_required`` — a ``required`` that isn't a list is dropped.
    """
    if "empty_object" in fixes:
        if params.get("type") is None:
            params["type"] = "object"
        # Fill a missing OR mis-encoded ``properties`` (an empty dict can arrive
        # as ``[]``, e.g. from Lua/JSON encoding — issue #7's neovim_get_cursor).
        if params.get("type") == "object" and not isinstance(params.get("properties"), dict):
            params["properties"] = {}
    if (
        "drop_invalid_required" in fixes
        and "required" in params
        and not isinstance(params["required"], list)
    ):
        params.pop("required")
    return params


def _apply_schema_fixes(params: object, fixes: frozenset[str]) -> object:
    """Apply the enabled schema fixes to a tool's parameter schema.

    ``anyof_type_hoist`` is recursive (nested ``Optional[...]`` schemas); the
    object-shape fixes apply to the top-level parameter object.
    """
    if "anyof_type_hoist" in fixes:
        params = _normalize_schema(params)
    if isinstance(params, dict):
        params = _apply_object_fixes(params, fixes)
    return params


def _normalize_tool_schema(tool: Tool, fixes: frozenset[str]) -> Tool:
    """Return a copy of *tool* with the enabled schema *fixes* applied to params.

    Assumes the tool parameters are already serializable (no circular refs).
    """
    from fastmcp.tools.function_tool import FunctionTool

    try:
        params = _apply_schema_fixes(_safe_json_clone(tool.parameters), fixes)
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
    except (ValueError, RecursionError, TypeError):
        params = tool.parameters or {"type": "object", "properties": {}}

    # Idempotent: if no fix changed the schema, return the tool untouched rather
    # than rebuilding it. This keeps the egress pass cheap when re-run each
    # tools/list, and preserves the original tool's identity/fn when unaffected.
    if params == tool.parameters:
        return tool

    dummy_fn = lambda: None  # noqa: E731
    return FunctionTool(
        fn=dummy_fn,
        name=str(tool.name) if tool.name else "unknown",
        description=str(tool.description) if tool.description else "",
        parameters=params,
        # Preserve the declared output schema — a params fix must not drop it.
        output_schema=getattr(tool, "output_schema", None),
        annotations=tool.annotations,
    )


def _coerce_object_schemas(tools: Sequence[Tool]) -> list[Tool]:
    """Unconditional final-pass guarantee that every advertised tool's parameter
    schema is object-shaped — belt-and-braces, independent of the opt-in
    ``schema_fixes`` ("clean") path.

    A blank / non-object / ``[]``-encoded ``inputSchema`` is never valid and is
    rejected by strict adapters (e.g. Copilot: ``[] is not of type 'object'`` —
    issue #7). This runs on the FINAL list returned from ``on_list_tools`` —
    after ``append_nvim_tools`` — so proxied tools, stale-reinjected tools AND
    the neovim virtual tools are all covered, regardless of any configured
    schema fix. The source of the ``[]`` is fixed on the Lua side too; this is
    the process-global safety net so a wrong upstream/virtual schema can never
    reach the wire. Only tools that actually need it are rebuilt (via
    ``model_copy``, which preserves ``fn`` so dispatch is unaffected).
    """
    out: list[Tool] = []
    for t in tools:
        fixed = normalize_object_schema(t.parameters)
        if fixed == t.parameters:
            out.append(t)  # already valid — preserve identity/fn, don't rebuild
        else:
            out.append(t.model_copy(update={"parameters": fixed}))
    return out


def _finalize_schemas(tools: Sequence[Tool]) -> list[Tool]:
    """The single, global schema-cleanup path for advertised tools.

    Applied once at the ``on_list_tools`` egress to the COMPLETE assembled list —
    proxied upstream tools AND the appended neovim virtual tools — so every tool
    source gets identical treatment and none can bypass it. (That bypass was the
    root cause of issue #7: neovim tools were appended after the cleanup and so
    never saw ``schema_fixes``.)

      1. Configured global ``schema_fixes`` (``empty_object`` /
         ``anyof_type_hoist`` / ``drop_invalid_required``), if any are enabled —
         now genuinely global, covering neovim tools too. Idempotent, so
         re-running each request is cheap.
      2. Unconditional object-shape + valid-``required`` coercion — the always-on
         net (a blank/``[]`` schema is never valid regardless of config).

    Circular-``$ref`` rebuilding is intentionally NOT here: only proxied upstream
    schemas can be non-serializable, and that guard must run in ``_do_fetch``
    before they enter the cache.
    """
    result = list(tools)
    if RUNTIME.schema_fixes:
        result = [_normalize_tool_schema(t, RUNTIME.schema_fixes) for t in result]
    return _coerce_object_schemas(result)
