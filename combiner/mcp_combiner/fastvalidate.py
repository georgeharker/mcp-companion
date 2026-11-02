"""Cached JSON-Schema validation for proxied tool calls.

The combiner can check a proxied tool call's arguments against the tool's
``inputSchema`` before forwarding it (``validation.input``, on by default — a
malformed call is refused at the combiner without an upstream round trip), and
a structured result against its ``outputSchema`` (``validation.output``, off by
default — the end client validates what it consumes). fastmcp 4 / mcp 2 do
neither for proxied tools: the SDK's low-level server no longer validates, and
fastmcp's proxy relays results without policing the backend's schema. The gate
lives in ``ToolProcessingMiddleware``; this module only makes it cheap.

``jsonschema.validate`` is a convenience wrapper that, on **every call**,
re-runs ``check_schema`` (validating the schema against its meta-schema) and
builds a brand-new validator. For the large schemas the combiner proxies
(github, todoist, gws, …) that rebuild dominates — measured at ~3.3 ms/call on
a moderate schema, ~1.6 ms of which is ``check_schema`` alone. Here each
validator is built once, keyed by the *identity* of the schema dict: published
``Tool`` objects (and their schema dicts) live until the tool cache is rebuilt,
so identity is stable and large schemas are never hashed on the hot path.
``clear_cache()`` is wired into ``invalidate_tool_cache()``.
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for

# A tool's JSON-object schema dict.
_Schema = dict[str, Any]

logger = logging.getLogger("mcp-combiner")

# id(schema) -> (schema_obj, validator | None). The schema_obj reference both
# pins the dict (so its id is not reused while cached) and lets us detect the
# rare case where a new dict lands on a recycled id. ``None`` records a schema
# that is itself invalid, so it is reported once rather than per call.
_validator_cache: dict[int, tuple[object, Any]] = {}


def _lenient(schema: _Schema) -> _Schema:
    """*schema* without top-level ``required`` names its own ``properties``
    never declare (a sloppy upstream schema would otherwise refuse every call;
    a real missing argument is still caught). Unchanged when the object may
    carry undeclared keys by pattern."""
    props = schema.get("properties")
    required = schema.get("required")
    if (
        not isinstance(props, dict)
        or not isinstance(required, list)
        or "patternProperties" in schema
    ):
        return schema
    declared = [name for name in required if name in props]
    if len(declared) == len(required):
        return schema
    return {**schema, "required": declared}


def _get_validator(schema: _Schema) -> Any:
    """A reusable validator for *schema* (built at most once), or ``None``
    when the schema itself is not a valid JSON Schema."""
    key = id(schema)
    cached = _validator_cache.get(key)
    if cached is not None and cached[0] is schema:
        return cached[1]
    effective = _lenient(schema)
    cls = validator_for(effective)
    try:
        cls.check_schema(effective)  # once per schema, not once per call
        validator: Any = cls(effective)
    except SchemaError as exc:
        # An upstream's broken schema must not block every call to it: the
        # gate fails open for that tool, and says so once.
        logger.warning("fastvalidate: skipping validation against an invalid schema: %s", exc)
        validator = None
    _validator_cache[key] = (schema, validator)
    return validator


def first_error(instance: object, schema: _Schema) -> str | None:
    """The best-match validation error for *instance* against *schema*, or
    ``None`` when it is valid (or the schema itself is invalid)."""
    validator = _get_validator(schema)
    if validator is None:
        return None
    error = best_match(validator.iter_errors(instance))
    return None if error is None else str(error.message)


def clear_cache() -> None:
    """Drop all cached validators (a reload / reconnect may change schemas)."""
    if _validator_cache:
        logger.debug("fastvalidate: clearing %d cached validator(s)", len(_validator_cache))
        _validator_cache.clear()
