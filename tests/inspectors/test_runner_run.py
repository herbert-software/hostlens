"""Tests for `InspectorRunner.run`.

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
    target.capabilities = capabilities if capabilities is not None else {Capability.SHELL}
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


async def test_status_exception_render_failed_sh_filter_none() -> None:
    """`sh` filter on `None` must NOT escape `run()` — it raises a
    `jinja2.TemplateRuntimeError` that the runner catches as render_failed.
    """

    runner = _runner()
    manifest = _make_manifest(command="ping {{ host | sh }}")
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"host": None})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("render_failed:")


async def test_status_exception_render_failed_sh_filter_empty_list() -> None:
    """`sh` filter on an empty list must NOT escape `run()` — it raises a
    `jinja2.TemplateRuntimeError` that the runner catches as render_failed.
    """

    runner = _runner()
    manifest = _make_manifest(command="ping {{ endpoints | sh }}")
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"endpoints": []})
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


async def test_status_exception_output_schema_invalid_via_model_construct() -> None:
    """Adversarial: a caller using ``InspectorManifest.model_construct`` bypasses
    every Pydantic validator (including ``_validate_jsonschema_well_formed``),
    so a malformed ``output_schema`` reaches ``jsonschema.validate`` at runtime
    and raises ``jsonschema.exceptions.SchemaError``. The runner must collapse
    this to ``status="exception"`` rather than let ``SchemaError`` escape.
    """

    runner = _runner()
    manifest = InspectorManifest.model_construct(
        name="test.bad_schema",
        version="1.0.0",
        description="test",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        parameters=None,
        secrets=[],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        # Malformed JSON Schema: ``type`` is not a JSON Schema primitive.
        output_schema={"type": "bogus"},
        findings=[],
    )
    target = _make_target()
    result = await runner.run(manifest, target)
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("output_schema_invalid:")


# ---------------------------------------------------------------------- #
# Status: exception — parameter validation against manifest.parameters
# ---------------------------------------------------------------------- #


def _make_manifest_with_parameters(
    *,
    command: str,
    parameters: dict[str, Any] | None,
) -> InspectorManifest:
    """Build an ``InspectorManifest`` with an arbitrary ``parameters`` schema.

    Uses the public constructor (which goes through Pydantic) so the loader's
    well-formedness gate runs — the malformed-schema test below uses
    ``model_construct`` separately to skip Pydantic and exercise the runtime
    defense-in-depth path.
    """

    return InspectorManifest(
        name="test.run.params",
        version="1.0.0",
        description="test",
        targets=["local"],
        privilege="none",
        parameters=parameters,
        collect=CollectSpec(command=command),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object", "properties": {"raw": {"type": "string"}}},
        findings=[],
    )


async def test_parameter_validation_type_confusion_attack_blocked() -> None:
    """Manifest declares ``port`` as an integer; an attacker / buggy
    dispatcher passing a string with a shell-injection payload must be
    rejected before any subprocess is spawned. The static loader gate
    trusts the manifest's declared types; this is the runtime check that
    actually enforces them on caller-supplied values.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="psql -p {{ port }}",
        parameters={
            "type": "object",
            "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            "required": ["port"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"port": "5432; rm -rf /"})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed:")
    # Critical: NO subprocess invocation. The malicious payload never
    # reached ``target.exec``.
    assert target.exec.call_count == 0


async def test_parameter_validation_missing_required_parameter() -> None:
    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="ping {{ host }}",
        parameters={
            "type": "object",
            "properties": {"host": {"type": "string", "pattern": "^[a-zA-Z0-9.-]+$"}},
            "required": ["host"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed:")
    assert target.exec.call_count == 0


async def test_parameter_validation_pattern_violation() -> None:
    """Even though the ``sh`` filter would quote ``'; rm -rf /``, the
    manifest's pattern explicitly says such values are out of contract.
    Rejecting at validation time gives a clearer error and never even
    reaches the rendering layer.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="ping {{ host | sh }}",
        parameters={
            "type": "object",
            "properties": {"host": {"type": "string", "pattern": "^[a-zA-Z0-9.-]+$"}},
            "required": ["host"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"host": "'; rm -rf /"})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed:")
    assert target.exec.call_count == 0


async def test_parameter_validation_valid_integer_passes() -> None:
    """A valid integer value must pass validation and reach the renderer.

    ``RunInspectorInput.parameters`` is typed ``dict[str, str]`` at the
    ToolRegistry boundary, but the runner accepts ``dict[str, Any]`` and
    must correctly handle integer parameter values passed directly.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="psql -p {{ port }}",
        parameters={
            "type": "object",
            "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            "required": ["port"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"port": 5432})
    assert result.status == "ok"
    assert result.error is None
    target.exec.assert_awaited_once()
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "psql -p 5432"


async def test_parameter_validation_skipped_when_manifest_has_no_parameters() -> None:
    """When the manifest declares no ``parameters`` schema, callers may
    still pass arbitrary key/values (e.g. for use inside the template);
    validation must not run and the rendered command must reach exec.
    """

    runner = _runner()
    manifest = _make_manifest()  # parameters: None (default)
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"anything": "goes"})
    assert result.status == "ok"
    target.exec.assert_awaited_once()


# ---------------------------------------------------------------------- #
# Parameter schema defaults (Fix 14)
# ---------------------------------------------------------------------- #


async def test_parameter_default_injected_when_caller_omits() -> None:
    """A schema-declared ``default`` must be injected into the rendered command
    and the DSL evaluation context when the caller omits the parameter.

    Without this, ``jsonschema.validate`` would accept the missing parameter but
    the template / ``when:`` expression would crash on the unbound variable.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="curl -o /dev/null -w {{ '%{http_code}' }} ; echo status={{ expected_status }}",
        parameters={
            "type": "object",
            "properties": {"expected_status": {"type": "integer", "default": 200}},
        },
    )
    # Attach a finding rule that references the defaulted parameter, proving
    # the DSL context received the default value.
    manifest = InspectorManifest(
        **{
            **manifest.model_dump(),
            "findings": [
                FindingRule(
                    when="expected_status == 200",
                    severity="info",
                    message="expected_status defaulted to {expected_status}",
                )
            ],
        }
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={})
    assert result.status == "ok"
    assert result.error is None
    assert len(result.findings) == 1
    assert result.findings[0].message == "expected_status defaulted to 200"
    rendered_cmd = target.exec.await_args.args[0]
    assert "status=200" in rendered_cmd


async def test_parameter_default_does_not_override_caller_supplied_value() -> None:
    """When the caller passes an explicit value, the schema default must NOT
    overwrite it.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo status={{ expected_status }}",
        parameters={
            "type": "object",
            "properties": {"expected_status": {"type": "integer", "default": 200}},
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"expected_status": 404})
    assert result.status == "ok"
    rendered_cmd = target.exec.await_args.args[0]
    assert "status=404" in rendered_cmd


async def test_parameter_default_no_change_when_schema_has_no_defaults() -> None:
    """A schema declaring properties without ``default`` keys must not inject
    anything (no spurious None / KeyError).
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo name={{ name }}",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"name": "foo"})
    assert result.status == "ok"
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "echo name=foo"


# ---------------------------------------------------------------------- #
# Parameter type coercion (Fix 15)
# ---------------------------------------------------------------------- #


async def test_parameter_coercion_string_to_integer_passes_validation() -> None:
    """``RunInspectorInput.parameters`` is ``dict[str, str]`` at the
    ToolRegistry boundary, so a caller passing ``parameters={"port": "5432"}``
    against a manifest declaring ``port: {type: integer}`` must be coerced
    before validation, not rejected as a type mismatch.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="psql -p {{ port }}",
        parameters={
            "type": "object",
            "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            "required": ["port"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"port": "5432"})
    assert result.status == "ok"
    assert result.error is None
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "psql -p 5432"


async def test_parameter_coercion_failed_int_cast_still_rejected() -> None:
    """Security invariant: a malformed string for an ``integer`` parameter must
    NOT slip through coercion. ``int("5432; rm -rf /")`` raises ``ValueError``;
    the helper leaves the value as a string; ``jsonschema.validate`` rejects it;
    the runner surfaces ``parameter_validation_failed`` and ``target.exec`` is
    never called. This mirrors the existing round-4 security test path under
    the new coercion pipeline.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="psql -p {{ port }}",
        parameters={
            "type": "object",
            "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
            "required": ["port"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"port": "5432; rm -rf /"})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed:")
    assert target.exec.call_count == 0


async def test_parameter_coercion_string_to_float() -> None:
    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo cpu={{ cpu_pct }}",
        parameters={
            "type": "object",
            "properties": {"cpu_pct": {"type": "number"}},
            "required": ["cpu_pct"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"cpu_pct": "0.85"})
    assert result.status == "ok"
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "echo cpu=0.85"


async def test_parameter_coercion_boolean_true() -> None:
    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo verbose={{ verbose }}",
        parameters={
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
            "required": ["verbose"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"verbose": "true"})
    assert result.status == "ok"
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "echo verbose=True"


async def test_parameter_coercion_boolean_invalid_token_rejected() -> None:
    """A boolean parameter receiving a string that is neither ``true|1`` nor
    ``false|0`` must NOT be silently coerced; the value stays as a string and
    ``jsonschema.validate`` rejects it.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo verbose={{ verbose }}",
        parameters={
            "type": "object",
            "properties": {"verbose": {"type": "boolean"}},
            "required": ["verbose"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"verbose": "maybe"})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed:")
    assert target.exec.call_count == 0


async def test_parameter_coercion_string_type_no_coercion() -> None:
    """``string`` typed parameters must not be touched — a numeric-looking
    string like ``"42"`` stays as ``"42"`` and passes validation as a string.
    """

    runner = _runner()
    manifest = _make_manifest_with_parameters(
        command="echo name={{ name }}",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"name": "foo"})
    assert result.status == "ok"
    rendered_cmd = target.exec.await_args.args[0]
    assert rendered_cmd == "echo name=foo"


async def test_parameter_schema_invalid_via_model_construct() -> None:
    """Adversarial: caller uses ``model_construct`` to bypass Pydantic
    (including the loader's ``_validate_jsonschema_well_formed`` gate),
    so a malformed ``parameters`` schema reaches ``jsonschema.validate``
    at runtime and raises ``SchemaError``. The runner must collapse this
    to ``status="exception"`` rather than let ``SchemaError`` escape.
    """

    runner = _runner()
    manifest = InspectorManifest.model_construct(
        name="test.bad_param_schema",
        version="1.0.0",
        description="test",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        # Malformed JSON Schema: ``type`` is not a JSON Schema primitive.
        parameters={"type": "bogus"},
        secrets=[],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )
    target = _make_target()
    result = await runner.run(manifest, target, parameters={"x": 1})
    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_schema_invalid:")
    assert target.exec.call_count == 0


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


async def test_cancel_set_before_run_raises_cancelled_error() -> None:
    """``cancel`` is the cooperative cancellation channel from
    ``ToolContext.cancel``. Setting it before ``run()`` is entered must
    surface as ``asyncio.CancelledError`` at the first phase check.
    """

    import asyncio

    runner = _runner()
    manifest = _make_manifest()
    target = _make_target()
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await runner.run(manifest, target, cancel=cancel)


async def test_cancel_set_mid_run_raises_cancelled_error() -> None:
    """When the cancel event fires during ``target.exec`` (simulated by
    the exec side-effect itself setting the event), the next phase
    boundary check observes it and raises ``CancelledError``.
    """

    import asyncio

    runner = _runner()
    manifest = _make_manifest()
    cancel = asyncio.Event()

    async def _exec_then_cancel(cmd: str, *, timeout: int, env: Any = None) -> ExecResult:
        cancel.set()
        return ExecResult(
            exit_code=0,
            stdout="hello\n",
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )

    target = MagicMock()
    target.name = "t1"
    target.type = "local"
    target.capabilities = {Capability.SHELL}
    target.exec = AsyncMock(side_effect=_exec_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await runner.run(manifest, target, cancel=cancel)


async def test_cancel_not_set_completes_normally() -> None:
    """A non-set ``cancel`` event must not interfere with normal completion."""

    import asyncio

    runner = _runner()
    manifest = _make_manifest()
    target = _make_target()
    cancel = asyncio.Event()  # never set
    result = await runner.run(manifest, target, cancel=cancel)
    assert result.status == "ok"


async def test_cancel_none_completes_normally() -> None:
    """Passing ``cancel=None`` (default) must not error."""

    runner = _runner()
    manifest = _make_manifest()
    target = _make_target()
    result = await runner.run(manifest, target, cancel=None)
    assert result.status == "ok"


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
