"""parse_table — whitespace-table format parser.

Splits each non-blank line on whitespace into exactly `len(spec.columns)`
fields (using `maxsplit=len(columns)-1` so trailing data falls into the
last column rather than being silently dropped). Lines whose column count
is **less** than expected are skipped with a structured warning so the
inspector author can see which rows failed during a `hostlens doctor` run.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from hostlens.inspectors.schema import ParseSpec

__all__ = ["parse_table"]

_log = structlog.get_logger(__name__)

# Pre-compiled whitespace splitter — pattern is constant, compile once.
_WHITESPACE_SPLIT = re.compile(r"\s+")


def parse_table(stdout: str, spec: ParseSpec) -> dict[str, Any]:
    """Parse `stdout` as a whitespace-delimited table.

    Returns `{"rows": [{col: val, ...}, ...]}`. Lines with fewer columns
    than expected are dropped with a warning; extra columns are merged
    into the last expected column (by `maxsplit`).
    """

    expected = len(spec.columns)
    lines = stdout.splitlines()[spec.skip_header_rows :]
    rows: list[dict[str, str]] = []

    for line_no, raw_line in enumerate(lines, start=spec.skip_header_rows + 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        parts = _WHITESPACE_SPLIT.split(stripped, maxsplit=expected - 1)
        if len(parts) < expected:
            _log.warning(
                "parser.row.skipped",
                line_no=line_no,
                actual_cols=len(parts),
                expected_cols=expected,
            )
            continue
        rows.append({col: parts[i] for i, col in enumerate(spec.columns)})

    return {"rows": rows}
