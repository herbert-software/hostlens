"""Schema stability snapshot for `hostlens doctor --json`.

This file encodes the **schema evolution policy** (project-skeleton spec
§schema 演进 / `design.md` D-9):

- Required fields are *locked*. Any change to the required surface is a
  breaking contract change and MUST be accompanied by a bump of
  `DoctorReport.version` and an explicit spec update.
- Optional fields (e.g. `detail`, `path`) and **newly added** check
  entries (e.g. M1+ `target_connectivity`) are allowed to evolve
  **additively** without touching this file — assertions deliberately
  use subset (`<=`) comparisons so add-only growth does not break the
  snapshot.

The companion file ``test_doctor_schema.py`` exercises broader behaviour
(human/JSON parity, exit codes, env-driven readiness). This file is the
**minimal pin** of the contract surface that downstream Agent callers
depend on; if you find yourself updating it, you're making a breaking
change and the spec / version bump must land in the same commit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from hostlens.cli import app

_REQUIRED_TOP: frozenset[str] = frozenset({"version", "timestamp", "checks", "ready"})
_REQUIRED_CHECKS: frozenset[str] = frozenset(
    {"python_version", "anthropic_key", "config_dir"},
)
_VALID_STATUS: frozenset[str] = frozenset(
    {"ok", "present", "missing", "unreadable", "error"},
)
_PINNED_VERSION: str = "0.1.0"


@pytest.fixture
def report() -> dict[str, Any]:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    # Allow either ready / not-ready exit code; the **schema** is what
    # this file pins, not local environment health.
    assert result.exit_code in (0, 1), result.stdout + result.stderr
    return json.loads(result.stdout)


def test_required_top_level_keys_locked(report: dict[str, Any]) -> None:
    """Top-level required fields must be a subset of the emitted payload.

    Subset semantics (``<=``) intentionally permit **additive** evolution:
    a future M1+ release may add optional top-level metadata without
    touching this assertion. Removing or renaming any of the four pinned
    keys is breaking and requires a `version` bump + spec update.
    """

    actual_top = set(report.keys())
    assert actual_top >= _REQUIRED_TOP, (
        f"required top-level keys missing: {sorted(_REQUIRED_TOP - actual_top)}"
    )


def test_required_check_keys_locked(report: dict[str, Any]) -> None:
    """The three M0 checks must be present; new checks are allowed."""

    actual = set(report["checks"].keys())
    assert actual >= _REQUIRED_CHECKS, (
        f"required check keys missing: {sorted(_REQUIRED_CHECKS - actual)}"
    )


def test_each_check_status_in_enum(report: dict[str, Any]) -> None:
    """Every check entry must expose a `status` from the locked enum.

    `status` is the only field guaranteed on every check (D-9). `detail`
    and `path` are optional and explicitly *not* asserted here so that
    additive metadata stays unobstructed.
    """

    for name, check in report["checks"].items():
        assert isinstance(check, dict), f"check {name!r} not a dict: {check!r}"
        assert "status" in check, f"check {name!r} missing required `status`"
        assert check["status"] in _VALID_STATUS, (
            f"check {name!r} status {check['status']!r} not in {sorted(_VALID_STATUS)}"
        )


def test_version_pinned_to_contract(report: dict[str, Any]) -> None:
    """`DoctorReport.version` is the breaking-change signal.

    Bumping this value MUST coincide with an explicit update to this
    constant **and** the cli-foundation spec. Failing this test on
    purpose is the gate that forces reviewers to acknowledge the break.
    """

    assert report["version"] == _PINNED_VERSION, (
        f"doctor schema version drifted from pinned {_PINNED_VERSION!r}; "
        "if intentional, bump _PINNED_VERSION and update the spec in the "
        "same commit"
    )
