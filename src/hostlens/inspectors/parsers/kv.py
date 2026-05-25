"""parse_kv — key/value line format parser.

Splits each line on `spec.delimiter` with `maxsplit=1` so values containing
the delimiter (e.g. `MemTotal: 1024 kB`) are preserved intact. Both key
and value are stripped of surrounding whitespace. Lines without the
delimiter are skipped with a structured warning; duplicate keys log a
warning and keep the **last** seen value (last-write-wins matches the
intuition of overriding earlier definitions).
"""

from __future__ import annotations

import structlog

from hostlens.inspectors.schema import ParseSpec

__all__ = ["parse_kv"]

_log = structlog.get_logger(__name__)


def parse_kv(stdout: str, spec: ParseSpec) -> dict[str, str]:
    """Parse `stdout` line-by-line as `key<delim>value` pairs."""

    result: dict[str, str] = {}
    delimiter = spec.delimiter

    for line_no, raw_line in enumerate(stdout.splitlines(), start=1):
        if not raw_line.strip():
            continue
        parts = raw_line.split(delimiter, maxsplit=1)
        if len(parts) < 2:
            _log.warning(
                "parser.row.skipped",
                line_no=line_no,
                reason="missing_delimiter",
                delimiter=delimiter,
            )
            continue
        key = parts[0].strip()
        value = parts[1].strip()
        if key in result:
            _log.warning(
                "parser.key.duplicate",
                line_no=line_no,
                key=key,
            )
        result[key] = value

    return result
