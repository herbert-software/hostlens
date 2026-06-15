"""Tests for the shared `coerce_and_validate_parameters` helper.

This helper is the single parameter gate used by BOTH `InspectorRunner.run`
(runtime) and the schedule loader (load-time). The two tests below anchor the
two contract invariants that motivate sharing one helper:

  1. Defaults are injected before validation, so a field that is both
     ``required`` and carries a ``default`` is ACCEPTED when omitted. A
     raw-validate-only loader would reject it while the runner accepts it
     (direction-reversal hole) — sharing this helper makes the loader and
     runner accept sets equal.
  2. The exception contract is two-class:
     ``(jsonschema.ValidationError, jsonschema.exceptions.SchemaError)``. A
     malformed inspector schema raises ``SchemaError`` (not ``ValidationError``),
     which both callers must catch.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest

from hostlens.inspectors.runner import coerce_and_validate_parameters
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)


def _make_manifest(parameters: dict[str, Any] | None) -> InspectorManifest:
    return InspectorManifest(
        name="test.params",
        version="1.0.0",
        description="test",
        targets=["local"],  # type: ignore[arg-type]
        parameters=parameters,
        collect=CollectSpec(command="echo hello"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object", "properties": {"raw": {"type": "string"}}},
    )


def test_required_field_with_default_accepts_omission() -> None:
    manifest = _make_manifest(
        {
            "type": "object",
            "properties": {"x": {"type": "integer", "default": 5}},
            "required": ["x"],
        }
    )

    # Omitting `x` must NOT raise: the default is injected before validate, so
    # the `required` constraint is satisfied. This anchors loader == runner
    # accept sets (not the raw-validate ⊆ direction).
    result = coerce_and_validate_parameters({}, manifest)

    assert result == {"x": 5}


def test_malformed_schema_raises_schema_error() -> None:
    # `model_construct` bypasses the manifest's own well-formedness validator
    # so we can hand the helper a malformed inspector schema. A malformed
    # schema raises `SchemaError`, NOT `ValidationError` — anchoring the
    # two-class exception contract both callers must catch.
    manifest = InspectorManifest.model_construct(
        name="test.params",
        version="1.0.0",
        description="test",
        targets=["local"],
        parameters={"type": "nonsense"},
        collect=CollectSpec(command="echo hello"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object", "properties": {"raw": {"type": "string"}}},
    )

    with pytest.raises(jsonschema.exceptions.SchemaError):
        coerce_and_validate_parameters({"x": 1}, manifest)
