"""Cached JSON-Schema validators for the MCP tool-call path.

The MCP SDK's low-level tool handler validates every tool call with
``jsonschema.validate(instance, schema)`` (see
``mcp.server.lowlevel.server`` — input args against ``inputSchema`` and
structured output against ``outputSchema``).  ``jsonschema.validate`` is a
convenience wrapper that, on **every call**, re-runs ``cls.check_schema(schema)``
(validating the schema against its own meta-schema) and constructs a brand-new
validator before checking the instance.  For the large schemas the bridge
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

logger = logging.getLogger("mcp-bridge")

# id(schema) -> (schema_obj, validator). The schema_obj reference both pins the
# dict (so its id is not reused while cached) and lets us detect the rare case
# where a new dict happens to land on a recycled id.
_validator_cache: dict[int, tuple[object, Any]] = {}

# When True, the proxy skips the SDK's input/output schema validation entirely.
# The upstream server already validates both, so re-checking at the proxy is
# redundant work on every tool call. Both off by default.
#
# Note: input validation is already off in the bridge by default
# (fastmcp's strict_input_validation defaults to False, so the SDK never runs
# the input jsonschema.validate). The input flag is a hard override that keeps
# it off even if strict input validation is otherwise enabled.
_skip_input_validation = False
_skip_output_validation = False

# A permissive JSON schema (no constraints → matches any instance). Used as a
# shared object so its cached validator is built exactly once.
_ANY_SCHEMA: dict[str, Any] = {}

# id(tool) -> (tool, modified-copy). We model_copy each cached Tool at most once
# when input/output validation is skipped (cleared when a flag changes).
_nulled_tool_cache: dict[int, tuple[object, Any]] = {}


def set_skip_input_validation(skip: bool) -> None:
    """Enable/disable skipping input-schema validation at the proxy."""
    global _skip_input_validation
    if skip != _skip_input_validation:
        _skip_input_validation = skip
        _nulled_tool_cache.clear()
        if skip:
            logger.info("fastvalidate: input-schema validation disabled (proxy passthrough)")


def set_skip_output_validation(skip: bool) -> None:
    """Enable/disable skipping output-schema validation at the proxy."""
    global _skip_output_validation
    if skip != _skip_output_validation:
        _skip_output_validation = skip
        _nulled_tool_cache.clear()
        if skip:
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

    Idempotent — safe to call from every ``create_bridge``.
    """
    global _installed
    if _installed:
        return
    import mcp.server.lowlevel.server as _srv

    # The SDK handler calls jsonschema.validate / catches jsonschema.ValidationError
    # via this module global; swapping it routes validation through the cache.
    setattr(_srv, "jsonschema", _JsonschemaShim())

    # Wrap _get_cached_tool_definition so that, when validation is disabled, the
    # Tool the handler validates against has its schema(s) neutralised — nulling
    # outputSchema makes the SDK skip output validation altogether, and replacing
    # inputSchema with a permissive schema makes input validation a no-op. This
    # only affects the validation path; the client-facing tools/list response is
    # built elsewhere and still carries the real schemas.
    _orig_get_cached = _srv.Server._get_cached_tool_definition

    async def _get_cached_tool_definition(self: Any, tool_name: str) -> Any:
        tool = await _orig_get_cached(self, tool_name)
        if tool is None:
            return tool
        update: dict[str, Any] = {}
        if _skip_output_validation and tool.outputSchema is not None:
            update["outputSchema"] = None
        if _skip_input_validation and tool.inputSchema is not _ANY_SCHEMA:
            update["inputSchema"] = _ANY_SCHEMA
        if not update:
            return tool
        key = id(tool)
        cached = _nulled_tool_cache.get(key)
        if cached is not None and cached[0] is tool:
            return cached[1]
        modified = tool.model_copy(update=update)
        _nulled_tool_cache[key] = (tool, modified)
        return modified

    _srv.Server._get_cached_tool_definition = _get_cached_tool_definition  # type: ignore[method-assign]

    _installed = True
    logger.info("fastvalidate: cached JSON-schema validation installed")
