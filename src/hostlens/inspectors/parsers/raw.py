"""parse_raw — raw format parser.

The `raw` format has two modes:

* `spec.raw_extract_regex is None` — return the entire stdout under a single
  `raw` key (used by trivial inspectors such as `hello.echo`).
* `spec.raw_extract_regex` is a static regex — run `re.search` **once** and
  map the named capture groups onto `spec.columns`. A non-match returns
  `{col: None for col in spec.columns}` so the finding DSL can treat the
  absence-of-data case explicitly.

ReDoS defense is **not** in this layer — the main defense is the four-layer
static gate in `ParseSpec` (length, compile, all-named-groups, AST walk).
The runner may wrap parser calls in `asyncio.wait_for` as a soft fallback,
but this module makes no timeout claim of its own.
"""

from __future__ import annotations

import re
from typing import Any

from hostlens.inspectors.schema import ParseSpec

__all__ = ["parse_raw"]


def parse_raw(stdout: str, spec: ParseSpec) -> dict[str, Any]:
    """Parse `stdout` according to `spec` for the `raw` format.

    Returns either `{"raw": stdout}` (no regex) or a dict mapping each
    column in `spec.columns` to the corresponding named-group value (or
    `None` on miss).
    """

    if spec.raw_extract_regex is None:
        return {"raw": stdout}

    # Regex has already been validated by ParseSpec — re.compile cannot fail
    # here, and every group is guaranteed to be named with a name in columns.
    pattern = re.compile(spec.raw_extract_regex)
    match = pattern.search(stdout)
    if match is None:
        return {col: None for col in spec.columns}
    return {col: match.group(col) for col in spec.columns}
