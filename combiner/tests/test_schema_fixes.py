"""Tests for the named tool-schema fixes (server._apply_schema_fixes)."""

from mcp_combiner.server import (
    SCHEMA_FIXES,
    _apply_object_fixes,
    _apply_schema_fixes,
)

ALL = frozenset(SCHEMA_FIXES)
NONE: frozenset[str] = frozenset()


def test_no_fixes_is_a_noop():
    schema = {"foo": "bar", "type": None}
    assert _apply_schema_fixes(dict(schema), NONE) == schema


def test_empty_object_fills_missing_type_and_properties():
    out = _apply_object_fixes({}, frozenset({"empty_object"}))
    assert out == {"type": "object", "properties": {}}


def test_empty_object_fills_properties_on_object():
    out = _apply_object_fixes({"type": "object"}, frozenset({"empty_object"}))
    assert out["properties"] == {}


def test_empty_object_coerces_list_properties_to_dict():
    # issue #7: an empty dict can arrive encoded as [] where {} is required.
    out = _apply_object_fixes({"type": "object", "properties": []}, frozenset({"empty_object"}))
    assert out["properties"] == {}


def test_empty_object_leaves_non_object_type_alone():
    out = _apply_object_fixes({"type": "string"}, frozenset({"empty_object"}))
    assert out == {"type": "string"}


def test_empty_object_off_by_default():
    assert _apply_object_fixes({}, NONE) == {}


def test_drop_invalid_required_drops_non_list():
    out = _apply_object_fixes(
        {"type": "object", "required": "name"}, frozenset({"drop_invalid_required"})
    )
    assert "required" not in out


def test_drop_invalid_required_keeps_list():
    out = _apply_object_fixes(
        {"type": "object", "required": ["name"]}, frozenset({"drop_invalid_required"})
    )
    assert out["required"] == ["name"]


def test_anyof_type_hoist_distributes_parent_type():
    schema = {"type": "array", "anyOf": [{"items": {"type": "string"}}, {"type": "null"}]}
    out = _apply_schema_fixes(schema, frozenset({"anyof_type_hoist"}))
    assert isinstance(out, dict)
    assert "type" not in out  # parent type was hoisted out
    assert out["anyOf"][0] == {"type": "array", "items": {"type": "string"}}
    assert out["anyOf"][1] == {"type": "null"}


def test_anyof_hoist_does_not_force_object():
    # empty_object is NOT enabled, so a typeless leaf stays typeless.
    out = _apply_schema_fixes({"description": "x"}, frozenset({"anyof_type_hoist"}))
    assert out == {"description": "x"}


def test_all_fixes_combine():
    schema = {"required": "x"}  # no type, invalid required
    out = _apply_schema_fixes(schema, ALL)
    assert isinstance(out, dict)
    assert out["type"] == "object"
    assert out["properties"] == {}
    assert "required" not in out


def test_non_dict_schema_returned_unchanged():
    assert _apply_schema_fixes("not-a-dict", ALL) == "not-a-dict"
    assert _apply_schema_fixes([1, 2], frozenset({"anyof_type_hoist"})) == [1, 2]
