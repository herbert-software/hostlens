"""Shared pytest fixtures for the Hostlens test suite.

`tool_registry` and `tool_context_factory` are the M2 fixtures used by
multiple test modules — each test that depends on them receives an
independent instance (function scope, no module-level state).

M1 migration: `tool_context_factory` allocates a real
`hostlens.targets.registry.TargetRegistry` (with one `stub-target`
LocalTarget by default) **and** a real
`hostlens.inspectors.registry.InspectorRegistry` populated by
`build_registry_from_search_paths([], settings=Settings())` (builtin
hello.echo + system.uptime). Both stub fallbacks (`_StubTargetRegistry`,
`_StubInspectorRegistry`) are gone — per
`add-inspector-plugin-system` spec §需求:M2 首批 ToolSpec... §场景:
list_inspectors handler 投影真实 InspectorRegistry 数据, tests must use
the real registry types so the `ToolContext` field-type contract is
exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.demo.assets import reader_path, source_tree_path
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend


# Directory holding committed cassettes. ``llm_cassette(name)`` maps a
# semantic name to ``<this dir>/<name>.jsonl`` (design.md D-6: explicit name,
# never nodeid-derived).
_CASSETTES_DIR = Path(__file__).parent / "fixtures" / "cassettes"

# Incident cassettes migrated into the demo package (single SOT, design D1):
# the 9 call sites still pass ``incident_<key>``; the ``incident_`` prefix is
# stripped HERE to the bare key and resolved through the asset bridge. Non-
# incident cassettes (planner_health_check / list_inspectors_demo / deepseek_*)
# keep living under ``tests/fixtures/cassettes/`` unchanged.
_INCIDENT_PREFIX = "incident_"

_VALID_LLM_MODES = ("replay", "record", "live")


def _cassette_record_path(name: str) -> Path:
    """Resolve the write target for record mode (writer #2, design D2).

    ``incident_<key>`` names write the committed asset in the SOURCE TREE via
    ``source_tree_path`` (never an ``as_file`` read-only temp copy — that would
    silently drop a re-recording). Non-incident names keep their flat path under
    ``tests/fixtures/cassettes/``. Pure (no side effects) so a path-only test can
    assert the incident branch lands in ``src/hostlens/demo/scenarios``.
    """

    if name.startswith(_INCIDENT_PREFIX):
        return source_tree_path(name[len(_INCIDENT_PREFIX) :], "cassette")
    return _CASSETTES_DIR / f"{name}.jsonl"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    """Stash each phase's report on the test item so fixture teardown can tell
    whether the test passed. Used by ``llm_cassette`` to refuse persisting a
    record-mode cassette from a FAILED test (which would overwrite a good
    committed cassette with a truncated/wrong recording).
    """

    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"_hostlens_rep_{rep.when}", rep)


def _resolve_llm_mode() -> Literal["replay", "record", "live"]:
    """Resolve the cassette test mode from ``HOSTLENS_LLM_MODE``.

    Per design.md D-5 this resolution lives ONLY in the test fixture layer —
    production ``create_backend`` neither reads nor knows about
    ``HOSTLENS_LLM_MODE``. An unset or empty value means ``replay`` (CI
    default, zero API consumption). Any other value fails fast with the legal
    set named, never silently falling back to a default backend (spec §需求:
    `HOSTLENS_LLM_MODE` §场景:非法 mode 值 fail-fast).
    """

    raw = os.environ.get("HOSTLENS_LLM_MODE", "replay")
    if raw == "":
        return "replay"
    if raw not in _VALID_LLM_MODES:
        raise ValueError(
            f"invalid HOSTLENS_LLM_MODE={raw!r}; legal values are {'|'.join(_VALID_LLM_MODES)}"
        )
    return raw  # type: ignore[return-value]


def _default_target_registry() -> TargetRegistry:
    """Build a registry with a single safe LocalTarget so the default
    `list_targets_handler` path returns a non-empty list under the
    fixture. Callers needing custom topology pass their own registry
    via `target_registry=`.
    """
    config = TargetsConfig(
        version="1",
        targets=[LocalEntry(name="stub-target", type="local", enabled=True)],
    )
    return build_registry_from_config(config, Settings())


def _default_inspector_registry() -> InspectorRegistry:
    """Build the real `InspectorRegistry` from the builtin search path
    only (no user paths). M1 builtins are `hello.echo` + `system.uptime`,
    so the default fixture has two inspectors available — enough to
    exercise `list_inspectors_handler` without forcing each test to wire
    its own registry.
    """
    return build_registry_from_search_paths([], settings=Settings()).registry


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """A fresh `ToolRegistry` with the M2 default ToolSpec batch
    pre-registered. Each test receives its own instance — mutating the
    fixture cannot leak to other tests.
    """
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


@pytest.fixture
def tool_context_factory() -> Callable[..., ToolContext]:
    """Return a callable that produces a fresh `ToolContext` per call.

    Each invocation allocates a fresh real `TargetRegistry` (with one
    `stub-target` LocalTarget by default), a real `InspectorRegistry`
    populated from the builtin search path, a new `asyncio.Event`, and a
    new `NoopApprovalService`. Callers can pass `target_registry=` /
    `inspector_registry=` to override either while keeping the other
    dependencies fixture-provided.
    """

    def _make(
        *,
        target_registry: TargetRegistry | None = None,
        inspector_registry: InspectorRegistry | None = None,
    ) -> ToolContext:
        return ToolContext(
            target_registry=target_registry or _default_target_registry(),
            inspector_registry=inspector_registry or _default_inspector_registry(),
            config=Settings(),
            logger=structlog.get_logger("tool_context_factory"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


@pytest.fixture
def llm_cassette(request: pytest.FixtureRequest) -> Iterator[Callable[..., LLMBackend]]:
    """Return a factory producing an ``LLMBackend`` selected by the current mode.

    Usage: ``llm_cassette("planner_health_check", target_registry=<registry>)``
    maps the explicit semantic ``name`` to a cassette path (design.md D-6: never
    nodeid-derived) and dispatches on ``_resolve_llm_mode()``. ``incident_<key>``
    names are migrated into the demo package: the ``incident_`` prefix is
    stripped HERE (the 9 call sites are unchanged) and resolved through the asset
    bridge — replay reads via ``reader_path`` (as_file, zip-safe), record writes
    via ``source_tree_path`` (source tree, design D2). Non-incident names keep
    their flat ``tests/fixtures/cassettes/<name>.jsonl`` path.

    - **replay** → ``PlaybackBackend`` over the cassette file; a missing file
      raises with the expected path. ``target_registry`` is ignored. For
      ``incident_`` names the ``reader_path`` ``as_file`` context is held in an
      ``ExitStack`` for the whole test (the temp path must outlive the call —
      design D2 lifecycle).
    - **record** → wraps a live ``AnthropicAPIBackend`` in ``RecordingBackend``.
      Requires ``ANTHROPIC_API_KEY`` and ``target_registry`` (both ``pytest.fail``
      when absent). BEFORE returning the recorder the factory calls
      ``guard_record_targets`` so the assembly-layer real-target gate is
      structurally enforced (spec §需求:record 模式必须由 fixture 强制...拒绝真实
      target — never a "test author calls a helper" downgrade). Each recorder is
      ``flush()``ed at teardown. ``incident_`` names write to the source tree via
      ``_cassette_record_path`` (never an as_file temp copy).
    - **live** → a raw ``AnthropicAPIBackend`` (no cassette written).

    The whole mode dispatch lives here, never in production ``create_backend``
    (design.md D-5).
    """

    recorders: list[object] = []
    # Holds each replay-mode ``reader_path`` as_file context for the whole test
    # so the materialized temp cassette outlives the ``_make`` call.
    reader_stack = contextlib.ExitStack()

    def _make(name: str, target_registry: TargetRegistry | None = None) -> LLMBackend:
        # Resolve mode lazily on each call so a test that ``monkeypatch``es
        # ``HOSTLENS_LLM_MODE`` in its body (after fixture setup) is honored.
        mode = _resolve_llm_mode()
        is_incident = name.startswith(_INCIDENT_PREFIX)

        if mode == "replay":
            from hostlens.agent.backends.playback import PlaybackBackend

            if is_incident:
                key = name[len(_INCIDENT_PREFIX) :]
                cassette_path = reader_stack.enter_context(reader_path(key, "cassette"))
            else:
                cassette_path = _CASSETTES_DIR / f"{name}.jsonl"
            if not cassette_path.exists():
                raise FileNotFoundError(
                    f"cassette not found for replay: expected {cassette_path} "
                    f"(name={name!r}). Record it with "
                    f"HOSTLENS_LLM_MODE=record."
                )
            return PlaybackBackend(cassette_path=cassette_path)

        cassette_path = _cassette_record_path(name)

        if mode == "live":
            from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                pytest.fail("live mode requires ANTHROPIC_API_KEY")
            return AnthropicAPIBackend(api_key=api_key)

        # mode == "record"
        from support.cassette_recording import (
            RecordingBackend,
            guard_record_targets,
        )

        from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.fail(
                "record mode requires ANTHROPIC_API_KEY; refusing to return a "
                "backend that would only 401 on first call"
            )
        if target_registry is None:
            pytest.fail(
                "record mode requires target_registry so the assembly-layer "
                "real-target guard can run before recording; refusing to return "
                "an un-guarded RecordingBackend"
            )

        # Structural guard enforcement: this runs BEFORE the recorder is
        # returned, so simply obtaining the record backend has already passed
        # the real-target gate (spec §场景:fixture 强制守门, 无法绕过).
        guard_record_targets(
            target_registry,
            allow_real=os.environ.get("HOSTLENS_ALLOW_REAL_TARGET_RECORD") == "1",
        )

        recorder = RecordingBackend(
            cassette_path=cassette_path,
            inner=AnthropicAPIBackend(api_key=api_key),
        )
        recorders.append(recorder)
        return recorder

    yield _make

    # Release every replay-mode ``reader_path`` temp file now the test is done.
    reader_stack.close()

    # Teardown: persist each recorder built this test ONLY if the test passed.
    # A single test may ``_make`` multiple scenarios. If the test failed/errored,
    # ``persist=False`` makes ``flush`` skip the write (deregister only) so a
    # recording from a failing run never overwrites a good committed cassette
    # (Bugbot/Copilot: "failed test persists partial cassette"). ``flush`` is
    # idempotent and also no-ops on poisoned/empty recordings.
    call_rep = getattr(request.node, "_hostlens_rep_call", None)
    test_passed = call_rep is not None and call_rep.passed
    # Flush EVERY recorder even if one raises (e.g. an os.replace failure):
    # a bare loop would stop at the first exception and leak the remaining
    # recorders' active paths (Copilot: teardown loop stops on first raise).
    errors: list[BaseException] = []
    for recorder in recorders:
        try:
            recorder.flush(persist=test_passed)  # type: ignore[attr-defined]
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
