"""Tests for `InspectorRunner.run` (task 8.6).

Every one of the five `InspectorStatus` values must be reachable from
this entry point without `run()` raising business exceptions. Plus the
contract: caller programming errors (None args) raise `ValueError`;
runner-internal AttributeError/KeyError/TypeError propagate (NOT
swallowed by a blanket `except Exception`).

A regression-style grep gate at the bottom asserts the production module
contains no `except Exception` / `except (AttributeError` / `except
(KeyError` patterns.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import TargetError
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    FindingRule,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )


def _make_manifest(
    *,
    command: str = "echo hello",
    findings: list[FindingRule] | None = None,
    output_schema: dict[str, Any] | None = None,
    parse: ParseSpec | None = None,
    privilege: str = "none",
    targets: list[str] | None = None,
) -> InspectorManifest:
    return InspectorManifest(
        name="test.run",
        version="1.0.0",
        description="test",
        targets=targets or ["local"],  # type: ignore[arg-type]
        privilege=privilege,  # type: ignore[arg-type]
        collect=CollectSpec(command=command),
        parse=parse or ParseSpec(format="raw"),
        output_schema=output_schema
        or {"type": "object", "properties": {"raw": {"type": "string"}}},
        findings=findings or [],
    )


def _make_target(
    *,
    name: str = "t1",
    type_: str = "local",
    capabilities: set[Capability] | None = None,
    exec_result: ExecResult | None = None,
    exec_side_effect: Any = None,
) -> Any:
    target = MagicMock()
    target.name = name
    target.type = type_
    target.capabilities = (
        capabilities if capabilities is not None else {Capability.SHELL}
    )
    if exec_side_effect is not None:
        target.exec = AsyncMock(side_effect=exec_side_effect)
    else:
        target.exec = AsyncMock(
            return_value=exec_result
            or ExecResult(
                exit_code=0,
                stdout="hello\n",
                stderr="",
                duration_seconds=0.01,
                timed_out=False,
            )
        )
    return target


# ---------------------------------------------------------------------- #
# Status: ok
# ---------------------------------------------------------------------- #


async def test_status_ok_with_findings() -> None:
    runner = _runner()
    manifest = _make_manifest(
        findings=[
            FindingRule(
                when="len(raw) > 0",
                severity="info",
                message="hello received: {raw}",
            )
        ]
    )
    target = _make_target()
    result = await runner.run(manifest, target)
    assert result.status == "ok"
    assert result.error is None
    assert result.missing == []
    assert result.output == {"raw": "hello\n"}
    assert len(result.findings) == 1
    assert result.findings[0].message == "hello received: hello\n"


# ---------------------------------------------------------------------- #
# Status: requires_unmet
# ---------------------------------------------------------------------- #


async def test_status_requires_unmet_target_type() -> None:
    runner = _runner()
    manifest = _make_manifest(targets=["ssh"])
    target = _make_target(type_="local")
    result = await runner.run(manifest, target)
    assert result.status == "requires_unmet"
    assert result.missing == ["target_type"]
    # `target.exec` MUST NOT have been called.
    assert target.exec.call_count == 0


# ---------------------------------------------------------------------- #
# Status: timeout
# ---------------------------------------------------------------------- #


async def test_status_timeout() -> None:
    runner = _runner()
    manifest = _make_manifest()
    target = _make_target(
        exec_result=ExecResult(
            exit_code=None,
            stdout="",
            stderr="",
            duration_seconds=60.0,
            timed_out=True,
        ),
    )
    result = await runner.run(manifest, target)
    assert result.status == "timeout"
    assert result.error is None
    assert result.missing == []


# ---------------------------------------------------------------------- #
# Status: target_unreachable
# ---------------------------------------------------------------------- #


async def test_status_target_unreachable() -> None:
    runner = _runner()
    manifest = _make_manifest()
    target = _make_target(
        exec_side_effect=TargetError(kind="ssh_connection_lost"),
    )
    result = await runner.run(manifest, target)
    assert result.status == "target_unreachable"
    assert result.error == "ssh_connection_lost"


# ---------------------------------------------------------------------- #
# Status: exception — render / parse / schema paths
# ---------------------------------------------------------------------- #


async def test_status_exception_render_failed() -> None:
    runner = _runner()
    # Undefined variable in template -> jinja2.UndefinedError under
    # StrictUndefined.
    manifest = _make_manifest(command="echo {{ missing | sh }}")
    target = _make_target()
    result = await runner.run(manifest, target)
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("render_failed:")


async def test_status_exception_parse_failed_json() -> None:
    runner = _runner()
    manifest = _make_manifest(
        parse=ParseSpec(format="json"),
        output_schema={"type": "object"},
    )
    target = _make_target(
        exec_result=ExecResult(
            exit_code=0,
            stdout="not json",
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )
    )
    result = await runner.run(manifest, target)
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parse_failed:")


async def test_status_exception_output_schema_mismatch() -> None:
    runner = _runner()
    manifest = _make_manifest(
        output_schema={
            "type": "object",
            "properties": {"processes": {"type": "array"}},
            "required": ["processes"],
        }
    )
    target = _make_target()
    result = await runner.run(manifest, target)
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("output_schema_mismatch:")


# ---------------------------------------------------------------------- #
# Programmer errors → ValueError
# ---------------------------------------------------------------------- #


async def test_manifest_none_raises_value_error() -> None:
    runner = _runner()
    target = _make_target()
    with pytest.raises(ValueError):
        await runner.run(None, target)  # type: ignore[arg-type]


async def test_target_none_raises_value_error() -> None:
    runner = _runner()
    manifest = _make_manifest()
    with pytest.raises(ValueError):
        await runner.run(manifest, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
# Runner-internal bugs propagate
# ---------------------------------------------------------------------- #


async def test_runner_internal_attribute_error_propagates() -> None:
    """If runner has a bug accessing a missing attribute on a manifest-like
    object, the AttributeError must propagate, NOT be coerced to
    status="exception"."""

    runner = _runner()
    target = _make_target()

    # We fabricate a manifest-like object that fails on access to
    # `manifest.targets` (because the orchestrator reads `manifest.targets`
    # in step 1). The resulting AttributeError must propagate.

    class BrokenManifest:
        # Intentionally missing `targets` and other attributes — accessing
        # any of them raises AttributeError.
        name = "broken"
        version = "1.0.0"
        # Don't provide `targets` — first access in step 1 raises
        # AttributeError on Python's default protocol.

    with pytest.raises(AttributeError):
        # type: ignore[arg-type] — we deliberately pass a non-Manifest.
        await runner.run(BrokenManifest(), target)  # type: ignore[arg-type]


async def test_format_message_keyerror_does_not_propagate() -> None:
    """KeyError inside format_message → finding skip + ok status."""

    runner = _runner()
    manifest = _make_manifest(
        findings=[
            FindingRule(
                when="len(raw) > 0",
                severity="info",
                message="missing {nonexistent_var}",
            )
        ]
    )
    target = _make_target()
    result = await runner.run(manifest, target)
    # The KeyError was caught at format_message; status remains ok with
    # the single rule skipped (zero findings).
    assert result.status == "ok"
    assert result.findings == []


# ---------------------------------------------------------------------- #
# Grep-gate: no bare `except Exception` / bare AttributeError/KeyError
# ---------------------------------------------------------------------- #


def test_no_blanket_excepts_in_runner_module() -> None:
    """The strict-except contract is grep-enforceable. This test runs the
    grep on the production module and asserts zero matches for the three
    forbidden patterns (per spec §需求 + design.md Decision 7)."""

    src = Path("src/hostlens/inspectors/runner.py")
    text = src.read_text()
    # Strip docstring / comments — they may legitimately mention the
    # forbidden patterns inside narrative text.
    code_lines: list[str] = []
    in_block_doc = False
    for line in text.splitlines():
        stripped = line.lstrip()
        # Toggle on triple-quoted blocks (very simple heuristic; module
        # uses only `"""` doc strings).
        triple_count = stripped.count('"""')
        if triple_count and not in_block_doc:
            in_block_doc = True
            if triple_count >= 2:
                in_block_doc = False
            continue
        if in_block_doc:
            if '"""' in line:
                in_block_doc = False
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    assert re.search(r"\bexcept\s+Exception\b", code) is None, (
        "runner.py must not use bare `except Exception`"
    )
    assert re.search(r"except\s+\(\s*AttributeError", code) is None, (
        "runner.py must not catch AttributeError globally"
    )
    assert re.search(r"except\s+\(\s*KeyError", code) is None, (
        "runner.py must not catch KeyError globally (allowed only inside "
        "_FORMAT_MESSAGE_EXCEPTIONS tuple)"
    )
