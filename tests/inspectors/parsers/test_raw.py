"""Tests for `hostlens.inspectors.parsers.raw.parse_raw`.

Four cases per spec §需求:4 种 parse format 解析行为 §parse_raw:

  * no regex (`spec.raw_extract_regex is None`) → single `{"raw": ...}` key
  * regex matches → named-group → column map
  * regex misses → all columns mapped to None
  * multi named-group: groups appear in regex in different order than
    columns; result dict still keys by column name
"""

from __future__ import annotations

from hostlens.inspectors.parsers import parse_raw
from hostlens.inspectors.schema import ParseSpec


class TestParseRawNoRegex:
    def test_no_regex_returns_full_stdout_under_raw_key(self) -> None:
        spec = ParseSpec(format="raw")
        assert parse_raw("hello world\n", spec) == {"raw": "hello world\n"}

    def test_no_regex_with_empty_stdout(self) -> None:
        spec = ParseSpec(format="raw")
        assert parse_raw("", spec) == {"raw": ""}


class TestParseRawWithRegex:
    def test_regex_match_maps_named_groups_to_columns(self) -> None:
        spec = ParseSpec(
            format="raw",
            raw_extract_regex=r"load: (?P<l1>[\d.]+), (?P<l5>[\d.]+)",
            columns=["l1", "l5"],
        )
        result = parse_raw("load: 0.42, 0.50", spec)
        assert result == {"l1": "0.42", "l5": "0.50"}

    def test_regex_miss_returns_none_for_each_column(self) -> None:
        spec = ParseSpec(
            format="raw",
            raw_extract_regex=r"load: (?P<l1>[\d.]+), (?P<l5>[\d.]+)",
            columns=["l1", "l5"],
        )
        result = parse_raw("garbage", spec)
        assert result == {"l1": None, "l5": None}

    def test_multi_named_groups_ordered_by_columns_list(self) -> None:
        # Regex has groups in order (b, a, c); columns list is also (b, a, c)
        # — result must contain all three by name.
        spec = ParseSpec(
            format="raw",
            raw_extract_regex=r"(?P<b>\d+)-(?P<a>\d+)-(?P<c>\d+)",
            columns=["b", "a", "c"],
        )
        result = parse_raw("10-20-30", spec)
        assert result == {"b": "10", "a": "20", "c": "30"}
