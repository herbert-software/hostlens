"""parse_json — JSON format parser.

Uses `json.loads` and enforces that the top-level value is a JSON object
(dict). Lists and scalars at the top level are rejected via
`InspectorError(kind="parse_json_not_object")` so the runner can map the
failure onto `status="exception"` without exposing stdout content.

`json.JSONDecodeError` is allowed to propagate — the runner catches it at
the parser-call site and maps it to the same `exception` status.
"""

from __future__ import annotations

import json
from typing import Any

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.schema import ParseSpec

__all__ = ["parse_json"]


def parse_json(stdout: str, spec: ParseSpec) -> dict[str, Any]:
    """Parse `stdout` as JSON; require top-level object.

    `spec` is accepted for parser-signature uniformity but unused — the
    JSON parser has no per-Inspector configuration.
    """

    del spec  # accepted for signature uniformity with the other parsers
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise InspectorError(kind="parse_json_not_object")
    return data
