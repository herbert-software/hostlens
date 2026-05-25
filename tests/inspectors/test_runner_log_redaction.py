"""Tests for runner log redaction (task 8.7).

The contract: `inspector_started` / `inspector_finished` events must
contain ONLY a fixed set of non-sensitive fields (name, version, target,
status, duration, findings_count, stdout_length, stderr_length). The
runner must NEVER log:

  * `parameters` (may contain sensitive caller-supplied values),
  * parsed `output` (may contain command stdout that includes secrets),
  * `secrets_env` values (always sensitive by definition).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import structlog
from structlog.testing import capture_logs

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _make_target() -> Any:
    target = MagicMock()
    target.name = "t1"
    target.type = "local"
    target.capabilities = {Capability.SHELL}
    target.exec = AsyncMock(
        return_value=ExecResult(
            exit_code=0,
            stdout="leaked-stdout-payload\n",
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )
    )
    return target


def _make_manifest() -> InspectorManifest:
    return InspectorManifest(
        name="test.redaction",
        version="1.0.0",
        description="t",
        targets=["local"],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
        },
        findings=[],
    )


async def test_logs_do_not_contain_parameter_values() -> None:
    """Parameter values (potentially sensitive) must not be logged."""

    runner = InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )
    manifest = _make_manifest()
    target = _make_target()

    secret_payload = "literal-secret-12345"
    with capture_logs() as captured:
        await runner.run(manifest, target, parameters={"password": secret_payload})

    # Verify NO log event mentions the literal secret in any value.
    for event in captured:
        for key, value in event.items():
            assert secret_payload not in str(value), (
                f"redaction failed: secret value found in log field {key!r} "
                f"of event {event.get('event')!r}"
            )


async def test_logs_do_not_contain_full_output() -> None:
    """The parser-produced `output` dict must NOT appear in logs."""

    runner = InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )
    manifest = _make_manifest()
    target = _make_target()

    with capture_logs() as captured:
        await runner.run(manifest, target)

    # The output contains "leaked-stdout-payload"; verify it is NOT in any log.
    for event in captured:
        for key, value in event.items():
            assert "leaked-stdout-payload" not in str(value), (
                f"redaction failed: stdout content found in log field {key!r}"
            )


async def test_logs_include_required_safe_fields() -> None:
    """The two events must include the documented closed field set."""

    runner = InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )
    manifest = _make_manifest()
    target = _make_target()

    with capture_logs() as captured:
        await runner.run(manifest, target)

    started = [e for e in captured if e.get("event") == "inspector_started"]
    finished = [e for e in captured if e.get("event") == "inspector_finished"]
    assert len(started) == 1
    assert len(finished) == 1

    started_event = started[0]
    assert started_event["inspector_name"] == "test.redaction"
    assert started_event["inspector_version"] == "1.0.0"
    assert started_event["target_name"] == "t1"

    finished_event = finished[0]
    assert finished_event["inspector_name"] == "test.redaction"
    assert finished_event["status"] == "ok"
    assert "duration_seconds" in finished_event
    assert finished_event["findings_count"] == 0
    assert "stdout_length" in finished_event
    assert "stderr_length" in finished_event

    # No `parameters` / `output` / `secrets_env` keys are present.
    forbidden_keys = {"parameters", "output", "secrets_env"}
    assert forbidden_keys.isdisjoint(started_event.keys())
    assert forbidden_keys.isdisjoint(finished_event.keys())
