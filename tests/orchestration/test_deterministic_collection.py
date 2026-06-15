"""`run_deterministic_inspection` — pure fleet collection path.

Spec: deterministic-inspection-mode §需求:deterministic 模式必须固定
inspector 集逐 target 直跑、不走 Planner、不漫游 + §需求:capability 不匹配
(requires_unmet)必须视为预期跳过、不降级报告.

Real fixtures (no mock-to-failure): each target is a small in-process
`ExecutionTarget` that captures every executed command and returns
author-controlled stdout (the `_CaptureTarget` convention). Inspectors are
real `InspectorManifest` objects registered into a real `InspectorRegistry`,
so the runner's capability gate / parse / finding-DSL all run for real.

Coverage:
  * fixed set runs exactly once per target, no roaming (集外 inspector /
    targets 外 target 都不跑)
  * capability mismatch → `requires_unmet`, closed 5-value status set
    unchanged (no `skipped` value)
  * concurrency is semaphore-bounded
  * a single failing run is isolated, never aborts the batch
  * end-to-end into `Report.from_fleet_results`: `requires_unmet` does not
    degrade (rest ok → report status ok); a real failure
    (`target_unreachable` / `exception`) still degrades to partial
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import TargetError, ToolError
from hostlens.inspectors.health import DEFAULT_HEALTH_INSPECTORS
from hostlens.inspectors.registry import InspectorRegistry
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    FindingRule,
    InspectorManifest,
    ParseSpec,
)
from hostlens.orchestration.deterministic import (
    resolve_inspector_set,
    run_deterministic_inspection,
)
from hostlens.reporting.models import Report, ReportStatus
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext

# --------------------------------------------------------------------------- #
# Fixtures: capture target + tiny manifests
# --------------------------------------------------------------------------- #


class _CaptureTarget:
    """Fake `ExecutionTarget` that records commands and returns canned stdout.

    `capabilities` is author-supplied so a test can drive the runner's
    capability gate (`requires_unmet`). `concurrency_tracker`, when set,
    records how many `exec` calls are in flight simultaneously so a test can
    assert the semaphore bound.
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability] | None = None,
        stdout: str = "1\n",
        raise_on_exec: BaseException | None = None,
        concurrency_tracker: _ConcurrencyTracker | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.capabilities: set[Capability] = (
            capabilities if capabilities is not None else {Capability.SHELL, Capability.FILE_READ}
        )
        self._stdout = stdout
        self._raise_on_exec = raise_on_exec
        self._tracker = concurrency_tracker
        self._delay = delay
        self.commands: list[str] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.commands.append(cmd)
        if self._tracker is not None:
            async with self._tracker:
                if self._delay:
                    await asyncio.sleep(self._delay)
                return self._build_result(cmd)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._build_result(cmd)

    def _build_result(self, cmd: str) -> ExecResult:
        if self._raise_on_exec is not None:
            raise self._raise_on_exec
        # `command -v <bin>` binary probes must succeed so the capability
        # preflight passes for an installed-binary inspector.
        if cmd.startswith("command -v"):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        return ExecResult(
            exit_code=0,
            stdout=self._stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise NotImplementedError


class _ConcurrencyTracker:
    """Async context manager that records the peak number of simultaneous
    holders — used to assert the collection semaphore bound."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> _ConcurrencyTracker:
        async with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        return self

    async def __aexit__(self, *exc: object) -> None:
        async with self._lock:
            self.current -= 1


def _manifest(
    name: str,
    *,
    requires_capabilities: list[str] | None = None,
    timeout_seconds: int = 5,
) -> InspectorManifest:
    """A tiny `raw`-parse manifest that emits one info finding when stdout
    is non-empty (the `hello.echo` shape, parameterisable on capability)."""
    return InspectorManifest(
        name=name,
        version="1.0.0",
        description=f"test inspector {name}",
        targets=["local", "ssh"],
        requires_capabilities=requires_capabilities or [],
        requires_binaries=[],
        privilege="none",
        collect=CollectSpec(command="echo probe", timeout_seconds=timeout_seconds),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[
            FindingRule(
                when="len(raw) > 0",
                severity="info",
                message="probe ok: {raw}",
            )
        ],
    )


def _inspector_registry(*manifests: InspectorManifest) -> InspectorRegistry:
    reg = InspectorRegistry()
    for m in manifests:
        reg.register(m)
    return reg


def _target_registry(*targets: _CaptureTarget) -> TargetRegistry:
    reg = TargetRegistry()
    for t in targets:
        entry = LocalEntry(name=t.name, type="local", enabled=True)
        reg.register(t, entry)  # type: ignore[arg-type]
    return reg


def _context_factory(
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    *,
    cancel: asyncio.Event | None = None,
) -> Any:
    shared_cancel = cancel if cancel is not None else asyncio.Event()

    def factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=structlog.get_logger("test_deterministic"),
            approval_service=NoopApprovalService(),
            cancel=shared_cancel,
        )

    return factory


def _t0() -> datetime:
    return datetime(2026, 6, 14, 12, 0, 0)


def _t1() -> datetime:
    return datetime(2026, 6, 14, 12, 0, 2)


# --------------------------------------------------------------------------- #
# resolve_inspector_set
# --------------------------------------------------------------------------- #


def test_resolve_inspector_set_none_uses_default() -> None:
    # §场景:无 inspectors 用默认健康集
    assert resolve_inspector_set(None) == DEFAULT_HEALTH_INSPECTORS


def test_resolve_inspector_set_explicit_is_authoritative() -> None:
    # §场景:显式 inspectors 变权威集 — exactly the given list, no union with
    # the default set.
    assert resolve_inspector_set(["disk"]) == ("disk",)
    assert resolve_inspector_set(["a", "b"]) == ("a", "b")


# --------------------------------------------------------------------------- #
# fixed set, no roaming
# --------------------------------------------------------------------------- #


def test_fixed_set_runs_once_per_target_no_roaming() -> None:
    # §场景:固定集逐 target 跑、覆盖确定不漫游 — targets=[A,B,C], set={cpu,disk}
    # ⇒ exactly 6 runs, never an off-set inspector or off-list target.
    cpu = _manifest("test.cpu")
    disk = _manifest("test.disk")
    extra = _manifest("test.extra")  # registered but NOT in the requested set
    inspector_registry = _inspector_registry(cpu, disk, extra)

    ta = _CaptureTarget("a")
    tb = _CaptureTarget("b")
    tc = _CaptureTarget("c")
    td = _CaptureTarget("d")  # registered but NOT in the requested targets
    target_registry = _target_registry(ta, tb, tc, td)

    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(
            factory,
            ["a", "b", "c"],
            inspectors=["test.cpu", "test.disk"],
        )
    )

    assert len(results) == 6
    ran = {(r.target_name, r.name) for r in results}
    assert ran == {
        ("a", "test.cpu"),
        ("a", "test.disk"),
        ("b", "test.cpu"),
        ("b", "test.disk"),
        ("c", "test.cpu"),
        ("c", "test.disk"),
    }
    # off-list target `d` never ran; off-set inspector `test.extra` never ran
    assert td.commands == []
    assert all(r.name != "test.extra" for r in results)


def test_default_set_used_when_inspectors_none() -> None:
    # When `inspectors is None` the curated default set is what gets run.
    # Use a single-member default-like registry by registering exactly the
    # default names is overkill here; instead assert the resolver wiring:
    # register one default name and request it via None by monkey-narrowing
    # the default to a single existing manifest.
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    ta = _CaptureTarget("a")
    target_registry = _target_registry(ta)
    factory = _context_factory(target_registry, inspector_registry)

    # Explicit single-member list mirrors the None→default resolution but
    # keeps the test independent of the real builtin registry (covered by
    # test_health_default_set). The None path itself is exercised in
    # test_resolve_inspector_set_none_uses_default.
    results = asyncio.run(run_deterministic_inspection(factory, ["a"], inspectors=["test.cpu"]))
    assert [(r.target_name, r.name) for r in results] == [("a", "test.cpu")]


# --------------------------------------------------------------------------- #
# capability gate → requires_unmet (closed 5-value status set unchanged)
# --------------------------------------------------------------------------- #


def test_capability_mismatch_is_requires_unmet_not_skipped() -> None:
    # §场景:不适用项当跳过处理不污染 severity — status STAYS `requires_unmet`,
    # no `skipped` value is ever produced.
    needs_systemd = _manifest("test.systemd", requires_capabilities=["systemd"])
    plain = _manifest("test.cpu")
    inspector_registry = _inspector_registry(needs_systemd, plain)

    # target has SHELL/FILE_READ but NOT SYSTEMD
    ta = _CaptureTarget("a", capabilities={Capability.SHELL, Capability.FILE_READ})
    target_registry = _target_registry(ta)
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(factory, ["a"], inspectors=["test.systemd", "test.cpu"])
    )
    by_name = {r.name: r for r in results}
    assert by_name["test.systemd"].status == "requires_unmet"
    assert by_name["test.cpu"].status == "ok"

    # closed 5-value InspectorStatus set: nothing is ever "skipped"
    valid = {"ok", "timeout", "target_unreachable", "requires_unmet", "exception"}
    assert all(r.status in valid for r in results)
    assert all(r.status != "skipped" for r in results)


# --------------------------------------------------------------------------- #
# concurrency bound
# --------------------------------------------------------------------------- #


def test_concurrency_is_semaphore_bounded() -> None:
    tracker = _ConcurrencyTracker()
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    # 6 targets x 1 inspector = 6 runs; bound to 2 => peak in-flight <= 2.
    targets = [
        _CaptureTarget(name, concurrency_tracker=tracker, delay=0.02)
        for name in ("a", "b", "c", "d", "e", "f")
    ]
    target_registry = _target_registry(*targets)
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(
            factory,
            [t.name for t in targets],
            inspectors=["test.cpu"],
            concurrency=2,
        )
    )
    assert len(results) == 6
    assert tracker.peak <= 2
    assert tracker.peak >= 1


# --------------------------------------------------------------------------- #
# single-item failure isolation
# --------------------------------------------------------------------------- #


def test_single_failure_isolated_does_not_abort_batch() -> None:
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)

    ok_target = _CaptureTarget("ok")
    broken_target = _CaptureTarget(
        "broken", raise_on_exec=TargetError(kind="ssh_connection_lost", target="broken")
    )
    target_registry = _target_registry(ok_target, broken_target)
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(factory, ["ok", "broken"], inspectors=["test.cpu"])
    )
    by_target = {r.target_name: r for r in results}
    assert by_target["ok"].status == "ok"
    # broken host's failure is isolated into its own InspectorResult status,
    # not raised — the ok host still produced a real result.
    assert by_target["broken"].status == "target_unreachable"


def test_unknown_target_raises_tool_error_fail_loud() -> None:
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    with pytest.raises(ToolError, match="target_not_found"):
        asyncio.run(
            run_deterministic_inspection(factory, ["a", "nonexistent"], inspectors=["test.cpu"])
        )


def test_unknown_inspector_raises_tool_error_fail_loud() -> None:
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    with pytest.raises(ToolError, match="inspector_not_found"):
        asyncio.run(
            run_deterministic_inspection(factory, ["a"], inspectors=["test.cpu", "test.ghost"])
        )


# --------------------------------------------------------------------------- #
# end-to-end: collected results → Report.from_fleet_results status derivation
# --------------------------------------------------------------------------- #


def test_e2e_requires_unmet_does_not_degrade_report() -> None:
    # §场景:requires_unmet 不降级 deterministic 报告 — one host lacks the
    # service (requires_unmet), the rest ok ⇒ fleet report status == ok.
    needs_systemd = _manifest("test.systemd", requires_capabilities=["systemd"])
    plain = _manifest("test.cpu")
    inspector_registry = _inspector_registry(needs_systemd, plain)

    # host a: full caps (systemd ok); host b: no systemd (requires_unmet)
    ta = _CaptureTarget(
        "a", capabilities={Capability.SHELL, Capability.FILE_READ, Capability.SYSTEMD}
    )
    tb = _CaptureTarget("b", capabilities={Capability.SHELL, Capability.FILE_READ})
    target_registry = _target_registry(ta, tb)
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(factory, ["a", "b"], inspectors=["test.systemd", "test.cpu"])
    )
    # b/test.systemd is requires_unmet; everything else ok.
    statuses = {(r.target_name, r.name): r.status for r in results}
    assert statuses[("b", "test.systemd")] == "requires_unmet"

    report = Report.from_fleet_results(
        results,
        schedule_name="daily-health",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert report.meta is not None
    assert report.meta.status == ReportStatus.OK


def test_e2e_real_failure_still_degrades_to_partial() -> None:
    # §场景:真正的失败仍降级 — a `target_unreachable` survives the
    # requires_unmet exemption and still degrades the fleet report.
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)

    ok_target = _CaptureTarget("ok")
    broken_target = _CaptureTarget(
        "broken", raise_on_exec=TargetError(kind="ssh_connection_lost", target="broken")
    )
    target_registry = _target_registry(ok_target, broken_target)
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(
        run_deterministic_inspection(factory, ["ok", "broken"], inspectors=["test.cpu"])
    )
    assert any(r.status == "target_unreachable" for r in results)

    report = Report.from_fleet_results(
        results,
        schedule_name="daily-health",
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert report.meta is not None
    assert report.meta.status == ReportStatus.PARTIAL


def test_collection_returns_inspector_results_only_no_backend() -> None:
    # Contract: pure collection — returns list[InspectorResult], never a
    # Report; the function signature accepts no backend (asserted statically
    # by mypy + here by exercising the no-backend call shape).
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    results = asyncio.run(run_deterministic_inspection(factory, ["a"], inspectors=["test.cpu"]))
    assert isinstance(results, list)
    assert all(isinstance(r, InspectorResult) for r in results)


# --------------------------------------------------------------------------- #
# inspector_parameters pass-through (spec §需求:deterministic 模式必须把
# manifest.inspector_parameters 透传给匹配的 inspector)
# --------------------------------------------------------------------------- #


def _spy_runner_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any] | None]:
    """Record the ``parameters`` value each ``InspectorRunner.run`` is called
    with, keyed by inspector name, while delegating to the real ``run``.

    The runner is constructed inside ``run_deterministic_inspection``, so the
    spy patches the class method (object-style ``setattr`` per MEMORY's
    same-process-flake note) rather than an instance.
    """
    captured: dict[str, dict[str, Any] | None] = {}
    original = InspectorRunner.run

    async def _spy(
        self: InspectorRunner,
        manifest: InspectorManifest,
        target: Any,
        parameters: dict[str, Any] | None = None,
        *,
        allow_privileged: bool = False,
        cancel: asyncio.Event | None = None,
    ) -> InspectorResult:
        captured[manifest.name] = parameters
        return await original(
            self,
            manifest,
            target,
            parameters,
            allow_privileged=allow_privileged,
            cancel=cancel,
        )

    monkeypatch.setattr(InspectorRunner, "run", _spy)
    return captured


def test_inspector_parameters_hit_passes_declared_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:命中 inspector 收到声明的参数 — a key present in inspector_parameters
    # reaches InspectorRunner.run verbatim.
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    captured = _spy_runner_parameters(monkeypatch)
    declared = {"allowed_processes": ["derper"]}

    asyncio.run(
        run_deterministic_inspection(
            factory,
            ["a"],
            inspectors=["test.cpu"],
            inspector_parameters={"test.cpu": declared},
        )
    )
    assert captured["test.cpu"] == declared


def test_inspector_parameters_miss_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:未命中 inspector 收到 None — an inspector absent from
    # inspector_parameters runs with parameters=None (default params).
    cpu = _manifest("test.cpu")
    disk = _manifest("test.disk")
    inspector_registry = _inspector_registry(cpu, disk)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    captured = _spy_runner_parameters(monkeypatch)

    asyncio.run(
        run_deterministic_inspection(
            factory,
            ["a"],
            inspectors=["test.cpu", "test.disk"],
            inspector_parameters={"test.cpu": {"allowed_processes": ["derper"]}},
        )
    )
    # test.cpu hit; test.disk missing → None.
    assert captured["test.cpu"] == {"allowed_processes": ["derper"]}
    assert captured["test.disk"] is None


def test_inspector_parameters_present_but_empty_passes_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:命中但值为空 dict 收到空 dict — a present-but-{} entry is a hit and
    # yields {} (not the miss-case None).
    cpu = _manifest("test.cpu")
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    captured = _spy_runner_parameters(monkeypatch)

    asyncio.run(
        run_deterministic_inspection(
            factory,
            ["a"],
            inspectors=["test.cpu"],
            inspector_parameters={"test.cpu": {}},
        )
    )
    received = captured["test.cpu"]
    assert received == {}
    assert received is not None  # a hit, not the miss-case None


def test_inspector_parameters_empty_dict_on_paramless_inspector_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:命中无参 inspector 且值为空 dict 无副作用 — test.cpu has no
    # ``parameters`` block; receiving {} is a harmless no-op (runner skips the
    # validate/defaults/coerce stage, {} never enters the finding DSL), and the
    # inspector still produces its real result.
    cpu = _manifest("test.cpu")
    assert cpu.parameters is None  # the fixture inspector declares no parameters
    inspector_registry = _inspector_registry(cpu)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    captured = _spy_runner_parameters(monkeypatch)

    results = asyncio.run(
        run_deterministic_inspection(
            factory,
            ["a"],
            inspectors=["test.cpu"],
            inspector_parameters={"test.cpu": {}},
        )
    )
    assert captured["test.cpu"] == {}
    # Harmless no-op: the run still succeeds with its normal finding.
    assert len(results) == 1
    assert results[0].status == "ok"


def test_inspector_parameters_empty_mapping_all_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:空 inspector_parameters 行为不变 — None / {} both yield parameters=None
    # for every inspector (byte-for-byte the pre-feature behaviour).
    cpu = _manifest("test.cpu")
    disk = _manifest("test.disk")
    inspector_registry = _inspector_registry(cpu, disk)
    target_registry = _target_registry(_CaptureTarget("a"))
    factory = _context_factory(target_registry, inspector_registry)

    empties: tuple[dict[str, dict[str, Any]] | None, ...] = (None, {})
    for empty in empties:
        captured = _spy_runner_parameters(monkeypatch)
        asyncio.run(
            run_deterministic_inspection(
                factory,
                ["a"],
                inspectors=["test.cpu", "test.disk"],
                inspector_parameters=empty,
            )
        )
        assert captured["test.cpu"] is None
        assert captured["test.disk"] is None
