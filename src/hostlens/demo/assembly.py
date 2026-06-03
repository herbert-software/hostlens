"""Self-contained demo assembly — feeds the shared diagnosis pipeline (design D1/D7).

``build_demo_pipeline`` assembles the offline inputs the shared
``cli/_intent.py::run_diagnosis_pipeline`` core consumes (a single
``PlaybackBackend`` + a demo ``ToolContext`` factory + the ``ReplayTarget`` + the
env-stripped ``_DemoSettings``), over the packaged demo assets, reading no user
config and needing no API key. The pipeline core itself builds the
``PlannerAgent`` / ``DiagnosticianAgent`` and wires the per-run collector +
frozen tool clock; this module only supplies the offline plumbing, so the demo
and ``--intent`` paths share the same two-loop assembly contract (design D1).

Request-key invariant (design D7): the cassette is matched on a key derived from
``model`` + ``messages`` + ``tools_count``. This assembly MUST produce a request
key byte-identical to the recording harness — same ``_DemoSettings`` model
default, same default tool set, same frozen tool clock (so ``tool_result``
content matches). Any divergence (a different ``Settings``, an extra registered
tool, a demo-specific model default) yields a silent ``CassetteMiss`` at runtime.
Callers guard this by asserting ``replay_target.misses == []`` after the run.

Lifecycle (design D2): ``ReplayTarget`` and ``PlaybackBackend`` are constructed
inside the caller-provided ``ExitStack`` that holds the reader ``as_file()``
context managers. The caller MUST keep that ``ExitStack`` open until the WHOLE
``run_diagnosis_pipeline`` call (both loops) returns, then close it (e.g. in a
``try/finally``), so the temporary asset paths stay valid for the whole run —
the single cassette reader serves the Diagnostician loop too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from hostlens.core.config import AgentSettings, Settings
from hostlens.demo.assets import reader_path
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.config import ReplayEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import ExitStack

    from hostlens.agent.backends.playback import PlaybackBackend
    from hostlens.targets.replay import ReplayTarget

__all__ = [
    "DEMO_TARGET_NAME",
    "FROZEN_DT",
    "build_demo_pipeline",
]

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


def build_demo_pipeline(
    scenario_key: str,
    *,
    exit_stack: ExitStack,
) -> tuple[PlaybackBackend, Callable[[], ToolContext], ReplayTarget, Settings]:
    """Assemble the demo full-chain inputs for ``run_diagnosis_pipeline`` (design D1/D2).

    Returns the 4-tuple ``(backend, context_factory, replay_target, settings)``:

    - ``backend`` — a SINGLE ``PlaybackBackend`` over the scenario cassette,
      served to BOTH the Planner and the Diagnostician loop inside
      ``run_diagnosis_pipeline`` (design D2: one cassette, matched by request key
      across both phases, not by order). Returned concrete (not cast to the
      ``LLMBackend`` Protocol) so the caller can assert it ``is`` the instance the
      pipeline runs and that it is a ``PlaybackBackend`` (the structural "never
      touches the API" proof, spec §不触达 API).
    - ``context_factory`` — the demo ``ToolContext`` factory closure (target
      registry holding the ``ReplayTarget`` registered as ``DEMO_TARGET_NAME`` +
      the built-in inspector registry + the env-stripped ``_DemoSettings``). The
      pipeline builds its OWN tool registries (Planner + Diagnostician) and wires
      the per-run collector / frozen clock there, so this assembly does NOT build
      a tool registry itself.
    - ``replay_target`` — for the ``replay_target.misses == []`` drift guard and
      the ``replay_target.type`` (the ``impersonate`` value, a construction-time
      constant safe to read outside the ExitStack) fed as ``target_type``.
    - ``settings`` — the SAME ``_DemoSettings`` instance built here; the caller
      MUST pass this verbatim to the pipeline (the cassette request key's
      ``model`` is taken from ``settings.agent.primary_model``, so a caller-built
      second ``_DemoSettings`` could drift the model and miss — design D1).

    The ``ReplayTarget`` and ``PlaybackBackend`` are built inside ``exit_stack``,
    which holds the reader ``as_file()`` context managers. The caller owns
    ``exit_stack`` and MUST keep it open until the WHOLE pipeline (both loops)
    returns (design D2 lifecycle): the cassette reader must stay open for the
    Diagnostician's ``messages_create`` too.

    Reads no user config (constructs ``Settings`` / ``TargetsConfig`` in-process,
    never ``load_settings`` / ``load_targets_config`` / ``create_backend`` —
    design D7).
    """

    from hostlens.agent.backends.playback import PlaybackBackend

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

    backend = PlaybackBackend(cassette_path=cassette_path)
    return backend, context_factory, replay_target, settings
