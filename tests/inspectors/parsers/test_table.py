"""Tests for `hostlens.inspectors.parsers.table.parse_table`.

Coverage per spec §需求:4 种 parse format 解析行为 §parse_table:

  * 3-row normal split with header skipping
  * `skip_header_rows=0` keeps the first row
  * `skip_header_rows=2` drops two header rows
  * lines with too few columns are skipped (not raised)
  * lines with too many columns merge extras into the last column
  * empty stdout returns `{"rows": []}`
"""

from __future__ import annotations

from hostlens.inspectors.parsers import parse_table
from hostlens.inspectors.schema import ParseSpec


class TestParseTableNormal:
    def test_three_row_table_with_header_skip(self) -> None:
        stdout = "PID USER\n1 root\n2 admin\n3 daemon\n"
        spec = ParseSpec(format="table", columns=["pid", "user"], skip_header_rows=1)
        assert parse_table(stdout, spec) == {
            "rows": [
                {"pid": "1", "user": "root"},
                {"pid": "2", "user": "admin"},
                {"pid": "3", "user": "daemon"},
            ]
        }


class TestParseTableHeaderSkipVariants:
    def test_skip_header_rows_zero_keeps_first_row(self) -> None:
        stdout = "1 root\n2 admin\n"
        spec = ParseSpec(format="table", columns=["pid", "user"], skip_header_rows=0)
        assert parse_table(stdout, spec) == {
            "rows": [
                {"pid": "1", "user": "root"},
                {"pid": "2", "user": "admin"},
            ]
        }

    def test_skip_header_rows_two_drops_two_lines(self) -> None:
        stdout = "BANNER\nPID USER\n1 root\n2 admin\n"
        spec = ParseSpec(format="table", columns=["pid", "user"], skip_header_rows=2)
        assert parse_table(stdout, spec) == {
            "rows": [
                {"pid": "1", "user": "root"},
                {"pid": "2", "user": "admin"},
            ]
        }


class TestParseTableMalformedRows:
    def test_row_with_too_few_columns_is_skipped(self) -> None:
        stdout = "PID USER CMD\n1 root bash\nBROKEN\n3 admin sshd\n"
        spec = ParseSpec(
            format="table", columns=["pid", "user", "cmd"], skip_header_rows=1
        )
        result = parse_table(stdout, spec)
        assert result == {
            "rows": [
                {"pid": "1", "user": "root", "cmd": "bash"},
                {"pid": "3", "user": "admin", "cmd": "sshd"},
            ]
        }

    def test_row_with_extra_columns_merged_into_last(self) -> None:
        # `maxsplit=len(columns)-1` collapses any trailing whitespace into the
        # last field — `python -c "print('x y z')"` becomes (pid, user, cmd=
        # "z extra trailing data").
        stdout = "PID USER CMD\n1 root python -c print('hello world')\n"
        spec = ParseSpec(
            format="table", columns=["pid", "user", "cmd"], skip_header_rows=1
        )
        result = parse_table(stdout, spec)
        assert result == {
            "rows": [
                {"pid": "1", "user": "root", "cmd": "python -c print('hello world')"}
            ]
        }


class TestParseTableEmpty:
    def test_empty_stdout_returns_empty_rows(self) -> None:
        spec = ParseSpec(
            format="table", columns=["pid", "user"], skip_header_rows=1
        )
        assert parse_table("", spec) == {"rows": []}
