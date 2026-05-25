"""Parse-format parsers package.

M1 exposes exactly four parsers — `raw` / `table` / `json` / `kv`. The
runner dispatches on `ParseSpec.format` to the matching function. New
formats (e.g. `sql_result`) require a separate OpenSpec proposal and
schema change; this `__all__` is a deliberate closed set.
"""

from __future__ import annotations

from hostlens.inspectors.parsers.json import parse_json
from hostlens.inspectors.parsers.kv import parse_kv
from hostlens.inspectors.parsers.raw import parse_raw
from hostlens.inspectors.parsers.table import parse_table

__all__ = ["parse_json", "parse_kv", "parse_raw", "parse_table"]
