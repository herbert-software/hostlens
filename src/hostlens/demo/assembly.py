"""Self-contained demo assembly — mirrors the incident harness shape (design D7).

Net-new assembly (NOT a reuse of ``cli/_intent.py::build_planner``, which binds
``create_backend`` to a real Anthropic backend and so cannot run offline). It
builds the exact same ``PlannerAgent`` → ``AgentLoop`` → ``ToolsAdapter`` →
``run_inspector`` → ``InspectorRunner`` pipeline as the incident snapshot tests,
but over the packaged demo assets, reading no user config and needing no API key.

Request-key invariant (design D7): the cassette is matched on a key derived from
``model`` + ``messages`` + ``tools_count``. This assembly MUST produce a request
key byte-identical to the recording harness — same ``Settings(agent=AgentSettings())``
model default, same default tool set, same frozen tool clock (so ``tool_result``
content matches). Any divergence (a different ``Settings``, an extra registered
tool, a demo-specific model default) yields a silent ``CassetteMiss`` at runtime.
Callers guard this by asserting ``replay_target.misses == []`` after the run.

Lifecycle (design D2): ``ReplayTarget`` and ``PlaybackBackend`` are constructed
inside the caller-provided ``ExitStack`` that holds the reader ``as_file()``
context managers. The caller MUST keep that ``ExitStack`` open until
``PlannerAgent.run()`` returns, then close it (e.g. in a ``try/finally``), so the
temporary asset paths stay valid for the whole run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import structlog
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from hostlens.core.config import AgentSettings, Settings
from hostlens.demo.assets import reader_path
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.config import ReplayEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from contextlib import ExitStack

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.planner import PlannerAgent
    from hostlens.targets.replay import ReplayTarget

__all__ = ["DEMO_TARGET_NAME", "FROZEN_DT", "build_demo_planner"]

# Must equal ``tests/incidents/_harness.FROZEN_DT`` byte-for-byte: the frozen
# tool clock drives ``sampling_window`` inspector commands into the same
# byte-stable string the cassette/fixture were recorded against. A mismatch
# desyncs the request key (cassette miss) and the ReplayTarget command key
# (replay miss), so this constant is the recording clock, not a free choice.
FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)

# Matches the ExecutionTarget name regex (``^[a-z][a-z0-9_\-]{0,63}$``). MUST
# equal the target name baked into the recorded cassettes' ``run_inspector``
# tool_use blocks (``incident-host``): the loop replays turn 1's recorded
# tool_use referencing this name, dispatches against the registry under it, and
# the resulting tool_result feeds turn 2's request key. A divergent name makes
# the inspector lookup fail, mutating the tool_result and desyncing the request
# key (CassetteMiss) — the D7 invariant the ``misses == []`` guard protects.
DEMO_TARGET_NAME = "incident-host"


def _frozen_clock() -> datetime:
    return FROZEN_DT


class _DemoSettings(Settings):
    """Settings built from code defaults only — demo ignores HOSTLENS_* env / .env (design D7 self-containment).

    Demo must replay offline on any machine regardless of the user's HOSTLENS_*
    environment; isolating env sources also pins the model default to the
    recording default, preserving the cassette request-key invariant (D7).
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def build_demo_planner(
    scenario_key: str,
    *,
    exit_stack: ExitStack,
) -> tuple[PlannerAgent, ReplayTarget]:
    """Assemble a ``PlannerAgent`` over the packaged assets for ``scenario_key``.

    Returns ``(planner, replay_target)`` so the caller can assert
    ``replay_target.misses == []`` after the run (the request-key /
    strict-consumption drift guard, design D7).

    The ``ReplayTarget`` (over the fixture) and ``PlaybackBackend`` (over the
    cassette) are built inside ``exit_stack``, which holds the reader
    ``as_file()`` context managers. The caller owns ``exit_stack`` and MUST keep
    it open until ``PlannerAgent.run()`` returns (design D2 lifecycle).

    Reads no user config: constructs ``Settings`` / ``TargetsConfig`` in-process
    and never calls ``load_settings`` / ``load_targets_config`` / ``create_backend``
    (design D7).
    """

    # Imported lazily so the platform-specific target imports inside
    # ``build_registry_from_config`` / the backend module stay off the import
    # path of callers that only need the registry metadata.
    from hostlens.agent.backends.playback import PlaybackBackend
    from hostlens.agent.planner import PlannerAgent

    settings = _DemoSettings(agent=AgentSettings())

    fixture_path = exit_stack.enter_context(reader_path(scenario_key, "fixture"))
    cassette_path = exit_stack.enter_context(reader_path(scenario_key, "cassette"))

    target_registry = build_registry_from_config(
        TargetsConfig(
            version="1",
            targets=[
                ReplayEntry(
                    name=DEMO_TARGET_NAME,
                    type="replay",
                    fixture=str(fixture_path),
                )
            ],
        ),
        settings,
    )
    replay_target: ReplayTarget = target_registry.get(DEMO_TARGET_NAME)  # type: ignore[assignment]

    inspector_registry = build_registry_from_search_paths([], settings=settings).registry

    tool_registry = ToolRegistry()
    register_default_tools(tool_registry, clock=_frozen_clock)

    logger = structlog.get_logger("demo")

    def context_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    # ``PlaybackBackend`` declares ``name`` / ``capabilities`` as ``ClassVar``,
    # which mypy refuses to match against the ``LLMBackend`` Protocol's instance
    # members; the cast mirrors the codebase convention at every other
    # backend → PlannerAgent boundary (e.g. ``tests/agent/test_planner.py``).
    backend = cast("LLMBackend", PlaybackBackend(cassette_path=cassette_path))
    planner = PlannerAgent(backend, tool_registry, settings, context_factory)
    return planner, replay_target
