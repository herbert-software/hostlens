"""Unit tests for ``hostlens inspect`` runner-invocation invariants.

Specifically: the ``--timeout`` injection path must rebuild the manifest
through ``CollectSpec(**{...})`` so Pydantic Field validation fires.

Three assertions (spec §需求:`hostlens inspect` 命令必须支持 6 个选项与
1 个位置参数 ``--timeout 必须经 CollectSpec 重构注入触发 validation``):

  (a) No ``--timeout`` → the manifest reference passed to the runner
      is the **same object** the registry returned.
  (b) ``--timeout 5`` → runner sees a new manifest whose
      ``collect.timeout_seconds == 5``; the registry's manifest stays
      untouched (no mutation).
  (c) Monkey-patching the CLI [1, 300] check away and passing 9999 must
      still raise ``pydantic.ValidationError`` from CollectSpec
      construction (defense-in-depth second gate).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from hostlens.cli import inspect as inspect_module
from hostlens.cli.inspect import _apply_timeout_override
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)


def _build_manifest(timeout_seconds: int = 60) -> InspectorManifest:
    """Build a minimal valid InspectorManifest for unit testing."""

    return InspectorManifest(
        name="hello.echo",
        version="1.0.0",
        description="test inspector",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        secrets=[],
        parameters=None,
        collect=CollectSpec(command="echo hello", timeout_seconds=timeout_seconds),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object", "properties": {"raw": {"type": "string"}}},
        findings=[],
    )


# --------------------------------------------------------------------------- #
# _apply_timeout_override — pure unit tests
# --------------------------------------------------------------------------- #


def test_apply_timeout_none_returns_same_reference() -> None:
    """``cli_timeout=None`` must NOT clone the manifest.

    Assertion (a) of the spec — registry-held manifest stays untouched
    when the operator doesn't pass ``--timeout``.
    """

    manifest = _build_manifest()
    result = _apply_timeout_override(manifest, None)
    assert result is manifest


def test_apply_timeout_clones_manifest_with_new_collect() -> None:
    """``cli_timeout=5`` clones the manifest with a new CollectSpec.

    Assertion (b) of the spec — the runner sees timeout_seconds=5 but
    the original manifest (from the registry) is unchanged.
    """

    original = _build_manifest(timeout_seconds=60)
    clone = _apply_timeout_override(original, 5)

    # Clone has the new timeout.
    assert clone.collect.timeout_seconds == 5
    # Original is untouched (frozen Pydantic model + new instance).
    assert original.collect.timeout_seconds == 60
    # Different object identity (clone is a copy, not a re-export).
    assert clone is not original
    assert clone.collect is not original.collect


def test_apply_timeout_out_of_range_raises_validation_error() -> None:
    """Bypassing CLI [1, 300] check still triggers CollectSpec validation.

    Assertion (c) of the spec — even if a future regression skips
    ``_validate_timeout``, the CollectSpec rebuild step rejects
    out-of-range values via Pydantic Field(ge=1, le=300).
    """

    manifest = _build_manifest()
    with pytest.raises(ValidationError):
        _apply_timeout_override(manifest, 9999)


def test_cli_timeout_validation_bypass_exits_3_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """Monkeypatch ``_validate_timeout`` away → CLI maps the resulting
    Pydantic ``ValidationError`` from ``_apply_timeout_override`` to
    exit 3 with a one-line stderr message; **no** Python traceback
    leaks (spec §需求: 不输出 Python traceback).

    Guards against a future regression where the CLI [1, 300] gate is
    removed or refactored: the defense-in-depth ``CollectSpec`` field
    validator still rejects 9999, but the CLI must catch the
    ``ValidationError`` instead of letting it propagate to ``main()``.
    """

    import sys

    import yaml

    from hostlens.cli import inspect as inspect_module
    from hostlens.cli import main as cli_main_fn

    # Make ``_validate_timeout`` a no-op so 9999 reaches CollectSpec.
    monkeypatch.setattr(inspect_module, "_validate_timeout", lambda v: v)

    # Wire a real local target so resolution succeeds before timeout
    # injection runs (otherwise we'd exit 3 on target-not-found first).
    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local-host", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(targets_path))
    user_dir = tmp_path / "inspectors"
    user_dir.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_dir))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hostlens",
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--timeout",
            "9999",
        ],
    )
    try:
        cli_main_fn()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0

    captured = capsys.readouterr()
    assert code == 3, captured.err
    assert "invalid --timeout: violates CollectSpec field constraint" in captured.err
    assert "Traceback" not in captured.err


def test_apply_timeout_zero_raises_validation_error() -> None:
    """``cli_timeout=0`` hits CollectSpec.Field(ge=1) and raises.

    Mirror of the above for the lower bound — defense in depth covers
    both ends of the [1, 300] window.
    """

    manifest = _build_manifest()
    with pytest.raises(ValidationError):
        _apply_timeout_override(manifest, 0)


# --------------------------------------------------------------------------- #
# Integration: end-to-end manifest identity through the dispatch path
# --------------------------------------------------------------------------- #


def test_dispatch_receives_original_manifest_when_no_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``--timeout`` is absent, the dispatch path forwards the
    original manifest reference unchanged.

    We can't easily call the full Typer entry with monkey-patched
    runner internals without going through ``main()``; instead this
    test exercises the helper directly, which is what the CLI uses.
    """

    manifest = _build_manifest(timeout_seconds=60)
    forwarded = _apply_timeout_override(manifest, None)
    assert forwarded is manifest
    assert forwarded.collect.timeout_seconds == 60


def test_dispatch_receives_new_manifest_when_timeout_set() -> None:
    """``--timeout 5`` reaches the dispatch path with a new manifest.

    Confirms the helper produces a clone that carries the override
    while leaving the registry-held manifest intact.
    """

    manifest = _build_manifest(timeout_seconds=60)
    forwarded = _apply_timeout_override(manifest, 5)
    assert forwarded is not manifest
    assert forwarded.collect.timeout_seconds == 5
    assert manifest.collect.timeout_seconds == 60


# --------------------------------------------------------------------------- #
# _dispatch — wiring smoke test (runner construction + run() invocation)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_invokes_runner_with_forwarded_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_dispatch`` calls ``InspectorRunner.run`` with the supplied manifest.

    This locks the wiring: any future regression that swaps the
    forwarded manifest for the original (or vice-versa) fails this
    assertion before the integration tests even spin up.
    """

    captured: dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["init_args"] = args
            captured["init_kwargs"] = kwargs

        async def run(
            self,
            manifest: InspectorManifest,
            target: Any,
            parameters: dict[str, Any] | None = None,
            *,
            allow_privileged: bool = False,
            cancel: Any = None,
        ) -> InspectorResult:
            captured["manifest"] = manifest
            captured["parameters"] = parameters
            captured["allow_privileged"] = allow_privileged
            return InspectorResult(
                name=manifest.name,
                version=manifest.version,
                status="ok",
                target_name="local-host",
                duration_seconds=0.0,
                output={},
                findings=[],
                error=None,
                missing=[],
            )

    monkeypatch.setattr(inspect_module, "InspectorRunner", _FakeRunner)

    manifest = _build_manifest(timeout_seconds=5)
    target = MagicMock()
    target.name = "local-host"
    target_registry = MagicMock()

    result = await inspect_module._dispatch(
        manifest,
        target,
        {"k": "v"},
        allow_privileged=False,
        target_registry=target_registry,
    )
    assert result.status == "ok"
    assert captured["manifest"] is manifest
    assert captured["manifest"].collect.timeout_seconds == 5
    assert captured["parameters"] == {"k": "v"}
    assert captured["allow_privileged"] is False


def test_build_report_empty_inspector_results_exits_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §需求:`hostlens inspect` 退出码: an empty inspector_results list
    triggers ``Report.from_inspector_results`` invariant ``ValueError``,
    which must map to exit code 3 (usage/invariant path) rather than
    falling through to exit 2.

    M1 CLI never passes an empty list, so this branch is dead code on
    the happy path; the test exercises ``_build_report`` directly to
    pin the spec contract for M2 Planner Agent's future multi-inspector
    dispatch.
    """
    from datetime import datetime

    import typer

    from hostlens.cli.inspect import _build_report
    from hostlens.reporting.models import Report

    # Monkeypatch from_inspector_results to forward to the real classmethod
    # with empty list (default _build_report wraps it with [single_result],
    # so we patch the inner call site to inject an empty list).
    real_from = Report.from_inspector_results

    def _empty_from_inspector_results(
        target_name: str,
        inspector_results: list,
        **kwargs: object,
    ) -> Report:
        return real_from(target_name, [], **kwargs)  # force empty path

    monkeypatch.setattr(
        Report,
        "from_inspector_results",
        classmethod(
            lambda cls, target_name, inspector_results, **kw: _empty_from_inspector_results(
                target_name, inspector_results, **kw
            )
        ),
    )

    # Build a dummy InspectorResult — content is irrelevant because the
    # patched factory ignores it and constructs with [].
    ir = InspectorResult(
        name="x.y",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.0,
        output={},
        findings=[],
        error=None,
        missing=[],
    )

    with pytest.raises(typer.Exit) as exc_info:
        _build_report(
            "t",
            "local",
            ir,
            datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
        )

    assert exc_info.value.exit_code == 3


def test_build_report_propagates_target_type() -> None:
    """The resolved target type must reach ``meta.target_type`` (Copilot:
    a hard-coded ``"local"`` default mislabels ssh/docker/k8s persisted
    reports). ``target_id`` stays equal to the target name (M3 contract).
    """
    from datetime import datetime

    from hostlens.cli.inspect import _build_report

    ir = InspectorResult(
        name="x.y",
        version="1.0.0",
        status="ok",
        target_name="h",
        duration_seconds=0.0,
        output={},
        findings=[],
        error=None,
        missing=[],
    )
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    report = _build_report("h", "ssh", ir, ts, ts)

    assert report.meta.target_type == "ssh"
    assert report.meta.target_id == "h"
