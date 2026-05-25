"""Tests for `InspectorRunner._parse_and_validate` (task 8.4).

We verify dispatch on the four `parse_spec.format` values and the
exception-propagation contract:

  * `json.JSONDecodeError` and `InspectorError(parse_json_not_object)`
    bubble out to the caller (runner's `run` maps to status="exception").
  * `jsonschema.ValidationError` likewise propagates.
"""

from __future__ import annotations

import json

import jsonschema
import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import ParseSpec
from hostlens.targets.registry import TargetRegistry


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )


def test_dispatch_raw_format() -> None:
    runner = _runner()
    spec = ParseSpec(format="raw")
    schema = {"type": "object", "properties": {"raw": {"type": "string"}}}
    result = runner._parse_and_validate("hello\n", spec, schema)
    assert result == {"raw": "hello\n"}


def test_dispatch_table_format() -> None:
    runner = _runner()
    spec = ParseSpec(format="table", columns=["pid", "user"], skip_header_rows=1)
    schema = {"type": "object"}
    result = runner._parse_and_validate("HEADER\n1 root\n2 admin\n", spec, schema)
    assert result == {
        "rows": [
            {"pid": "1", "user": "root"},
            {"pid": "2", "user": "admin"},
        ]
    }


def test_dispatch_json_format() -> None:
    runner = _runner()
    spec = ParseSpec(format="json")
    schema = {"type": "object"}
    result = runner._parse_and_validate('{"a": 1}', spec, schema)
    assert result == {"a": 1}


def test_dispatch_kv_format() -> None:
    runner = _runner()
    spec = ParseSpec(format="kv", delimiter=":")
    schema = {"type": "object"}
    result = runner._parse_and_validate("a: 1\nb: 2\n", spec, schema)
    assert result == {"a": "1", "b": "2"}


def test_json_decode_error_propagates() -> None:
    runner = _runner()
    spec = ParseSpec(format="json")
    schema = {"type": "object"}
    with pytest.raises(json.JSONDecodeError):
        runner._parse_and_validate("not json", spec, schema)


def test_json_not_object_raises_inspector_error() -> None:
    runner = _runner()
    spec = ParseSpec(format="json")
    schema = {"type": "object"}
    with pytest.raises(InspectorError) as exc_info:
        runner._parse_and_validate("[1, 2, 3]", spec, schema)
    assert exc_info.value.kind == "parse_json_not_object"


def test_jsonschema_validation_error_propagates() -> None:
    runner = _runner()
    spec = ParseSpec(format="raw")
    # output_schema requires `processes: array` but parse_raw returns
    # `{"raw": "..."}` — the validation must fail.
    schema = {
        "type": "object",
        "properties": {"processes": {"type": "array"}},
        "required": ["processes"],
    }
    with pytest.raises(jsonschema.ValidationError):
        runner._parse_and_validate("hello\n", spec, schema)
