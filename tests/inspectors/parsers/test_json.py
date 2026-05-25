"""Tests for `hostlens.inspectors.parsers.json.parse_json`.

Coverage per spec §需求:4 种 parse format 解析行为 §parse_json:

  * normal top-level object → returned as-is
  * top-level array → `InspectorError(kind="parse_json_not_object")`
  * top-level scalar → same
  * malformed JSON → `json.JSONDecodeError` propagates (runner catches at
    parser-call site)
"""

from __future__ import annotations

import json

import pytest

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.parsers import parse_json
from hostlens.inspectors.schema import ParseSpec


def _spec() -> ParseSpec:
    return ParseSpec(format="json")


class TestParseJsonHappyPath:
    def test_top_level_dict_returned_verbatim(self) -> None:
        result = parse_json('{"a": 1, "nested": {"b": [1, 2]}}', _spec())
        assert result == {"a": 1, "nested": {"b": [1, 2]}}


class TestParseJsonNonObjectRejection:
    def test_top_level_list_raises_parse_json_not_object(self) -> None:
        with pytest.raises(InspectorError) as exc_info:
            parse_json("[1, 2, 3]", _spec())
        assert exc_info.value.kind == "parse_json_not_object"

    def test_top_level_scalar_raises_parse_json_not_object(self) -> None:
        with pytest.raises(InspectorError) as exc_info:
            parse_json("42", _spec())
        assert exc_info.value.kind == "parse_json_not_object"

    def test_top_level_string_raises_parse_json_not_object(self) -> None:
        with pytest.raises(InspectorError) as exc_info:
            parse_json('"plain string"', _spec())
        assert exc_info.value.kind == "parse_json_not_object"


class TestParseJsonMalformed:
    def test_invalid_json_propagates_decode_error(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_json("{not valid json", _spec())
