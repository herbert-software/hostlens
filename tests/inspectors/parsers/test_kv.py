"""Tests for `hostlens.inspectors.parsers.kv.parse_kv`.

Coverage per spec §需求:4 种 parse format 解析行为 §parse_kv:

  * default `=` delimiter
  * custom `:` delimiter (e.g. `/proc/meminfo` style)
  * line without delimiter is skipped (not raised)
  * duplicate key — later value wins
"""

from __future__ import annotations

from hostlens.inspectors.parsers import parse_kv
from hostlens.inspectors.schema import ParseSpec


class TestParseKvDefaultDelimiter:
    def test_equals_sign_default(self) -> None:
        stdout = "FOO=bar\nBAZ=qux\n"
        spec = ParseSpec(format="kv")
        assert parse_kv(stdout, spec) == {"FOO": "bar", "BAZ": "qux"}

    def test_value_containing_delimiter_split_only_once(self) -> None:
        # Values may legitimately contain the delimiter — `maxsplit=1` is
        # what preserves the `key=value=with=equals` case.
        stdout = "KEY=value=with=equals\n"
        spec = ParseSpec(format="kv")
        assert parse_kv(stdout, spec) == {"KEY": "value=with=equals"}


class TestParseKvCustomDelimiter:
    def test_colon_delimiter_with_meminfo_style(self) -> None:
        stdout = "MemTotal:        1024 kB\nMemFree:         512 kB\n"
        spec = ParseSpec(format="kv", delimiter=":")
        assert parse_kv(stdout, spec) == {
            "MemTotal": "1024 kB",
            "MemFree": "512 kB",
        }


class TestParseKvSkipMalformedLines:
    def test_line_without_delimiter_skipped(self) -> None:
        stdout = "FOO=bar\nNO_DELIMITER_HERE\nBAZ=qux\n"
        spec = ParseSpec(format="kv")
        assert parse_kv(stdout, spec) == {"FOO": "bar", "BAZ": "qux"}

    def test_empty_lines_skipped_silently(self) -> None:
        stdout = "FOO=bar\n\n\nBAZ=qux\n"
        spec = ParseSpec(format="kv")
        assert parse_kv(stdout, spec) == {"FOO": "bar", "BAZ": "qux"}


class TestParseKvDuplicateKey:
    def test_duplicate_key_later_value_wins(self) -> None:
        stdout = "KEY=first\nKEY=second\nKEY=third\n"
        spec = ParseSpec(format="kv")
        assert parse_kv(stdout, spec) == {"KEY": "third"}
