"""Cached JSON-Schema validators for the MCP tool-call path.

The MCP SDK's low-level tool handler validates every tool call with
``jsonschema.validate(instance, schema)`` (see
``mcp.server.lowlevel.server`` — input args against ``inputSchema`` and
structured output against ``outputSchema``).  ``jsonschema.validate`` is a
convenience wrapper that, on **every call**, re-runs ``cls.check_schema(schema)``
(validating the schema against its own meta-schema) and constructs a brand-new
validator before checking the instance.  For the large schemas the combiner
proxies (github, todoist, gws, …) that rebuild dominates — measured at
~3.3 ms/call on a moderate schema, ~1.6 ms of which is ``check_schema`` pure
waste — and it repeats on every single tool call.  The actual pydantic
(de)serialisation of the response is ~90 µs by comparison, so this validator
rebuild is the real "validation is slow / the parser gets remade each time"
cost.

This module builds each validator once and reuses it, keyed by the *identity*
of the schema dict.  The SDK caches its ``types.Tool`` definitions (and thus
their schema dicts) for the life of a connection, so identity is stable between
reloads and we never hash large schemas on the hot path.  ``clear_cache()`` is
wired into ``invalidate_tool_cache()`` so a config reload or a freshly-connected
server drops stale validators — a renamed/changed tool schema is a new dict
object anyway, but we clear explicitly so nothing lingers.

``install()`` swaps the ``jsonschema`` reference inside
``mcp.server.lowlevel.server`` for a thin shim whose ``validate`` is cached and
whose every other attribute (notably ``ValidationError``) falls through to the
real module, so behaviour is byte-for-byte identical — only faster.
"""

from __future__ import annotations

import logging
from typing import Any

import jsonschema as _jsonschema
from jsonschema.validators import validator_for

logger = logging.getLogger("mcp-combiner")

# id(schema) -> (schema_obj, validator). The schema_obj reference both pins the
# dict (so its id is not reused while cached) and lets us detect the rare case
# where a new dict happens to land on a recycled id.
_validator_cache: dict[int, tuple[object, Any]] = {}

# Tri-state control of the SDK's *output*-schema validation:
#   None  → express no preference; the SDK default applies (validate whenever a
#           tool declares an outputSchema).
#   True  → force validation on (same effect as the default, stated explicitly).
#   False → force validation off; the upstream server already validated its own
#           structured output, so re-checking it at the proxy is redundant work
#           on every tool call (measurably slow for large structured responses).
#
# Input validation is *not* handled here — it is gated by fastmcp's
# ``strict_input_validation`` (passed to the FastMCP constructor), which is the
# only switch that can actually force the input jsonschema.validate on or off.
_output_validation: bool | None = None

# id(tool) -> (tool, modified-copy). We model_copy each cached Tool at most once
# when output validation is forced off (cleared when the setting changes).
_nulled_tool_cache: dict[int, tuple[object, Any]] = {}


def set_output_validation(value: bool | None) -> None:
    """Set tri-state output-schema validation (None/True/False — see module docs)."""
    global _output_validation
    if value != _output_validation:
        _output_validation = value
        _nulled_tool_cache.clear()
        if value is False:
            logger.info("fastvalidate: output-schema validation disabled (proxy passthrough)")


def _get_validator(schema: object) -> Any:
    """Return a reusable validator for *schema*, building it at most once.

    Raises ``jsonschema.SchemaError`` if the schema itself is invalid — same
    exception ``jsonschema.validate`` would raise, just surfaced at build time
    rather than on every call.
    """
    key = id(schema)
    cached = _validator_cache.get(key)
    if cached is not None and cached[0] is schema:
        return cached[1]

    cls = validator_for(schema)
    cls.check_schema(schema)  # validate the schema once (was: every call)
    validator = cls(schema)
    _validator_cache[key] = (schema, validator)
    return validator


def validate(instance: object, schema: object, *args: Any, **kwargs: Any) -> None:
    """Drop-in replacement for ``jsonschema.validate`` using a cached validator.

    Mirrors the semantics of ``jsonschema.validate``: raises the best-match
    ``jsonschema.ValidationError`` if *instance* is invalid, nothing otherwise.
    """
    validator = _get_validator(schema)
    error = _jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def clear_cache() -> None:
    """Drop all cached validators.

    Called from ``invalidate_tool_cache()`` so a config reload or a newly
    connected server never validates against a stale schema.
    """
    if _validator_cache:
        logger.debug("fastvalidate: clearing %d cached validator(s)", len(_validator_cache))
        _validator_cache.clear()
    _nulled_tool_cache.clear()


class _JsonschemaShim:
    """Stand-in for the ``jsonschema`` module with a cached ``validate``.

    ``validate`` is overridden; every other attribute (``ValidationError``,
    ``SchemaError``, ``exceptions``, …) delegates to the real module so the
    SDK's ``except jsonschema.ValidationError`` keeps working unchanged.
    """

    validate = staticmethod(validate)

    def __getattr__(self, name: str) -> Any:
        return getattr(_jsonschema, name)


_installed = False


def install() -> None:
    """Patch the MCP SDK's tool handler to use the cached validator.

    Idempotent — safe to call from every ``create_combiner``.
    """
    global _installed
    if _installed:
        return
    import mcp.server.lowlevel.server as _srv

    # The SDK handler calls jsonschema.validate / catches jsonschema.ValidationError
    # via this module global; swapping it routes validation through the cache.
    setattr(_srv, "jsonschema", _JsonschemaShim())

    # Wrap _get_cached_tool_definition so that, when output validation is forced
    # off, the Tool the handler validates against has no outputSchema — which
    # makes the SDK skip output validation altogether. This only affects the
    # validation path; the client-facing tools/list response is built elsewhere
    # and still carries the real outputSchema.
    _orig_get_cached = _srv.Server._get_cached_tool_definition

    async def _get_cached_tool_definition(self: Any, tool_name: str) -> Any:
        tool = await _orig_get_cached(self, tool_name)
        if _output_validation is not False or tool is None or tool.outputSchema is None:
            return tool
        key = id(tool)
        cached = _nulled_tool_cache.get(key)
        if cached is not None and cached[0] is tool:
            return cached[1]
        nulled = tool.model_copy(update={"outputSchema": None})
        _nulled_tool_cache[key] = (tool, nulled)
        return nulled

    _srv.Server._get_cached_tool_definition = _get_cached_tool_definition  # type: ignore[method-assign]

    _installed = True
    logger.info("fastvalidate: cached JSON-schema validation installed")
