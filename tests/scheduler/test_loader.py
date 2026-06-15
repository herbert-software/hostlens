"""Tests for `hostlens.scheduler.loader` — load-time semantic validation.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``
§需求:加载器必须扫描... / M4 每个 manifest 必须恰好一个 target.

Uses a real `TargetRegistry` (built from `LocalEntry` config) so the
injected-registry contract is exercised end-to-end — no mock registry.
"""

from __future__ import annotations

import functools
import textwrap
from pathlib import Path

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.scheduler.loader import load_schedules
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config


def _registry(*names: str) -> TargetRegistry:
    config = TargetsConfig(
        version="1",
        targets=[LocalEntry(name=name, type="local") for name in names],
    )
    return build_registry_from_config(config, Settings())


@functools.cache
def _ireg() -> InspectorRegistry:
    """Real builtin `InspectorRegistry` (carries `net.listening_ports` with its
    `allowed_processes` / `allowed_ports` parameters and the no-parameters
    health inspectors), so loader parameter validation runs against real
    inspector schemas — no mock registry.

    Cached (module-scope) since the loader only reads it (``registry.get``);
    rebuilding the full builtin registry per call across the ~30 call sites is
    wasted work, and the read-only use makes a shared instance safe."""

    return build_registry_from_search_paths([], settings=Settings()).registry


def _write(dir_path: Path, filename: str, body: str) -> None:
    (dir_path / filename).write_text(textwrap.dedent(body).lstrip("\n"))


_VALID_BODY = """
    name: nightly
    schedule:
      interval:
        hours: 1
      timezone: Asia/Shanghai
    targets:
      - web-1
    intent: check disk and load
"""


def test_all_valid_manifests_load(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)
    _write(
        tmp_path,
        "b.yaml",
        """
        name: morning
        schedule:
          cron: "0 6 * * *"
          timezone: UTC
        targets:
          - web-2
        intent: morning check
        """,
    )
    manifests = load_schedules(tmp_path, _registry("web-1", "web-2"), _ireg())

    assert len(manifests) == 2
    assert {m.name for m in manifests} == {"nightly", "morning"}


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_schedules(tmp_path / "nope", _registry(), _ireg()) == []


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert load_schedules(tmp_path, _registry(), _ireg()) == []


def test_target_not_registered_fail_loud(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)  # references web-1
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("other-host"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "web-1" in msg
    assert "registered" in msg.lower()


def test_multi_target_fail_loud(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
          - web-2
        intent: check
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1", "web-2"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "targets" in msg
    assert "single target" in msg.lower()


def test_single_target_loads(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)
    manifests = load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert len(manifests) == 1
    assert manifests[0].targets == ["web-1"]
    # No `mode` field in _VALID_BODY → defaults to agent.
    assert manifests[0].mode == "agent"


def test_agent_mode_multi_target_fail_loud(tmp_path: Path) -> None:
    # Explicit `mode: agent` with >=2 targets is fail-loud — even when every
    # member is registered (spec §场景:agent 模式多 target 仍 fail-loud).
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        mode: agent
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
          - web-2
        intent: check
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1", "web-2"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "targets" in msg
    assert "single target" in msg.lower()


def test_deterministic_mode_multi_target_loads(tmp_path: Path) -> None:
    # deterministic mode allows >=1 target (multi-target is its core use) —
    # all registered members load (spec §场景:deterministic 模式多 target 正常
    # 加载).
    _write(
        tmp_path,
        "a.yaml",
        """
        name: fleet-health
        mode: deterministic
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
          - web-2
          - web-3
        intent: fleet health sweep
        """,
    )
    manifests = load_schedules(tmp_path, _registry("web-1", "web-2", "web-3"), _ireg())

    assert len(manifests) == 1
    assert manifests[0].mode == "deterministic"
    assert manifests[0].targets == ["web-1", "web-2", "web-3"]


def test_single_target_loads_in_both_modes(tmp_path: Path) -> None:
    # A single registered target loads under either mode (spec §场景:单 target
    # manifest 在两种 mode 均正常加载).
    for mode in ("agent", "deterministic"):
        body = f"""
        name: nightly
        mode: {mode}
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: check
        """
        sub = tmp_path / mode
        sub.mkdir()
        _write(sub, "a.yaml", body)
        manifests = load_schedules(sub, _registry("web-1"), _ireg())

        assert len(manifests) == 1
        assert manifests[0].mode == mode
        assert manifests[0].targets == ["web-1"]


def test_deterministic_mode_empty_targets_fail_loud(tmp_path: Path) -> None:
    # An empty `targets` list is rejected in deterministic mode too — the
    # schema's min_length=1 fails at parse time, surfacing the file name.
    _write(
        tmp_path,
        "a.yaml",
        """
        name: fleet-health
        mode: deterministic
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets: []
        intent: fleet health sweep
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert "a.yaml" in str(exc.value)


def test_deterministic_mode_empty_inspectors_fail_loud(tmp_path: Path) -> None:
    # An explicit empty `inspectors: []` (distinct from omitting it, which uses
    # the default health set) resolves to "run nothing" → every fire would fail.
    # The loader rejects it at load so a typo fails loud here, not at fire time.
    _write(
        tmp_path,
        "a.yaml",
        """
        name: fleet-health
        mode: deterministic
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets: [web-1]
        inspectors: []
        intent: fleet health sweep
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert exc.value.kind == "schedule_deterministic_empty_inspectors"
    assert "a.yaml" in str(exc.value)


def test_deterministic_mode_target_not_registered_fail_loud(tmp_path: Path) -> None:
    # Unregistered members are fail-loud in deterministic mode too (the
    # registry membership check is mode-independent).
    _write(
        tmp_path,
        "a.yaml",
        """
        name: fleet-health
        mode: deterministic
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
          - ghost-host
        intent: fleet health sweep
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "ghost-host" in msg
    assert "registered" in msg.lower()


def test_duplicate_name_across_files_fail_loud(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)  # name: nightly
    _write(
        tmp_path,
        "b.yaml",
        """
        name: nightly
        schedule:
          interval:
            minutes: 30
          timezone: UTC
        targets:
          - web-1
        intent: another
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "nightly" in msg
    # Both the duplicating file and the first-seen file are surfaced.
    assert "b.yaml" in msg
    assert "a.yaml" in msg


def test_blank_intent_fail_loud(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: "   "
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "intent" in msg


def test_invalid_schema_fail_loud_with_filename(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "bad.yaml",
        """
        name: nightly
        schedule:
          timezone: UTC
        targets:
          - web-1
        intent: check
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert "bad.yaml" in str(exc.value)


def test_non_mapping_root_fail_loud(tmp_path: Path) -> None:
    (tmp_path / "scalar.yaml").write_text("just a string\n")
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert "scalar.yaml" in str(exc.value)


def test_notify_placeholder_loads_without_send(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: check
        notify:
          - channel: telegram
            only_if: "severity == 'critical'"
        """,
    )
    manifests = load_schedules(tmp_path, _registry("web-1"), _ireg())

    # Loading a manifest with notify must succeed; the loader neither
    # evaluates only_if nor instantiates any Notifier (M4 placeholder).
    assert len(manifests) == 1
    assert manifests[0].notify[0].channel == "telegram"


def test_diff_with_last_parses_but_inert_through_loader(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: check
        report:
          diff_with_last: true
        """,
    )
    manifests = load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert manifests[0].report.diff_with_last is True


# --------------------------------------------------------------------------- #
# inspector_parameters fail-loud validation (add-schedule-inspector-parameters)
# --------------------------------------------------------------------------- #


def _no_param_inspector(name: str) -> InspectorManifest:
    """A registered inspector with no ``parameters:`` block (step 4 target)."""

    return InspectorManifest.model_construct(
        name=name,
        version="1.0.0",
        description="no-parameters fake inspector",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        parameters=None,
        secrets=[],
        collect=CollectSpec(command="echo hi", timeout_seconds=5),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[],
    )


def _malformed_param_inspector(name: str) -> InspectorManifest:
    """A registered inspector whose ``parameters`` schema is itself malformed.

    ``model_construct`` bypasses the manifest validator (which would reject a
    bad schema at registration time), so this drives the loader's
    ``jsonschema.exceptions.SchemaError`` translation branch (step 5).
    """

    return InspectorManifest.model_construct(
        name=name,
        version="1.0.0",
        description="malformed-parameters fake inspector",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
        # `type` must be a string / list of strings; an int is a schema error.
        parameters={"type": 123, "properties": {"x": {"type": "string"}}},
        secrets=[],
        collect=CollectSpec(command="echo hi", timeout_seconds=5),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[],
    )


def _write_deterministic(
    tmp_path: Path,
    *,
    inspectors_line: str = "",
    inspector_parameters_block: str,
) -> None:
    """Write a deterministic manifest with already-zero-indented YAML.

    ``inspector_parameters_block`` is the body under ``inspector_parameters:``
    indented from column 0 (this writer does NOT run ``textwrap.dedent``, so the
    caller controls the exact YAML indentation of the nested mapping).
    """

    head = "name: fleet-health\nmode: deterministic\n"
    schedule = "schedule:\n  interval:\n    hours: 1\n  timezone: UTC\n"
    targets = "targets:\n  - web-1\nintent: fleet health sweep\n"
    inspectors = f"{inspectors_line}\n" if inspectors_line else ""
    params = f"inspector_parameters:\n{inspector_parameters_block}\n"
    (tmp_path / "a.yaml").write_text(head + schedule + targets + inspectors + params)


def test_agent_mode_inspector_parameters_fail_loud(tmp_path: Path) -> None:
    # mode: agent (default) + non-empty inspector_parameters → rejected (step 1).
    _write(
        tmp_path,
        "a.yaml",
        """
        name: nightly
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: check
        inspector_parameters:
          net.listening_ports:
            allowed_processes: [derper]
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "deterministic" in msg


def test_deterministic_key_not_in_default_set_fail_loud(tmp_path: Path) -> None:
    # key not in the default health set (no explicit inspectors:) → rejected
    # with the offending key surfaced (step 2).
    _write_deterministic(
        tmp_path,
        inspector_parameters_block="  mysql.deadlocks:\n    x: 1",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "mysql.deadlocks" in msg


def test_deterministic_explicit_set_key_outside_fail_loud(tmp_path: Path) -> None:
    # explicit inspectors: [linux.disk.usage] is authoritative — a param key for
    # a registered-but-not-listed inspector is out of the run set (step 2).
    _write_deterministic(
        tmp_path,
        inspectors_line="inspectors: [linux.disk.usage]",
        inspector_parameters_block="  net.listening_ports:\n    allowed_processes: [derper]",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "net.listening_ports" in msg


def test_deterministic_explicit_set_unregistered_name_fail_loud(tmp_path: Path) -> None:
    # explicit inspectors: returned verbatim — a typo'd, unregistered name used as
    # a param key must surface as ConfigError, NOT a bare InspectorError (step 3).
    _write_deterministic(
        tmp_path,
        inspectors_line="inspectors: [net.listening_prots]",
        inspector_parameters_block="  net.listening_prots:\n    allowed_processes: [derper]",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "net.listening_prots" in msg


def test_deterministic_no_param_inspector_with_params_fail_loud(tmp_path: Path) -> None:
    # a registered inspector with no parameters: block + non-empty params → rejected
    # (step 4). linux.disk.usage is in the default set and declares no parameters.
    _write_deterministic(
        tmp_path,
        inspector_parameters_block="  linux.disk.usage:\n    x: 1",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "linux.disk.usage" in msg
    assert "not accept" in msg.lower() or "does not accept" in msg.lower()


def test_deterministic_param_key_typo_fail_loud(tmp_path: Path) -> None:
    # a typo'd parameter key under a parameterised inspector
    # (additionalProperties:false) → rejected at load time (step 5, ValidationError).
    _write_deterministic(
        tmp_path,
        inspector_parameters_block="  net.listening_ports:\n    allowed_procesess: [x]",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), _ireg())

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "net.listening_ports" in msg


def test_deterministic_malformed_param_schema_fail_loud(tmp_path: Path) -> None:
    # an inspector whose own parameters schema is malformed → ConfigError, NOT a
    # bare SchemaError (step 5 catches jsonschema.exceptions.SchemaError too).
    registry = _ireg()
    registry.register(_malformed_param_inspector("fake.bad_schema"), source_path=None)
    _write_deterministic(
        tmp_path,
        inspectors_line="inspectors: [fake.bad_schema]",
        inspector_parameters_block="  fake.bad_schema:\n    x: 1",
    )
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"), registry)

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "fake.bad_schema" in msg


def test_deterministic_no_param_inspector_empty_dict_passes(tmp_path: Path) -> None:
    # a no-parameters inspector with an empty {} is a no-op → loads fine (step 4
    # only triggers on non-empty params).
    _write_deterministic(
        tmp_path,
        inspectors_line="inspectors: [fake.noparam]",
        inspector_parameters_block="  fake.noparam: {}",
    )
    registry = _ireg()
    registry.register(_no_param_inspector("fake.noparam"), source_path=None)
    manifests = load_schedules(tmp_path, _registry("web-1"), registry)

    assert len(manifests) == 1
    assert manifests[0].inspector_parameters == {"fake.noparam": {}}


def test_deterministic_valid_params_loads(tmp_path: Path) -> None:
    # key in the default set + valid params → loads (happy path).
    _write_deterministic(
        tmp_path,
        inspector_parameters_block="  net.listening_ports:\n    allowed_processes: [derper]",
    )
    manifests = load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert len(manifests) == 1
    assert manifests[0].inspector_parameters["net.listening_ports"]["allowed_processes"] == [
        "derper"
    ]


def test_deterministic_empty_inspector_parameters_loads(tmp_path: Path) -> None:
    # omitted / empty inspector_parameters → loads with no validation triggered.
    _write(
        tmp_path,
        "a.yaml",
        """
        name: fleet-health
        mode: deterministic
        schedule:
          interval:
            hours: 1
          timezone: UTC
        targets:
          - web-1
        intent: fleet health sweep
        """,
    )
    manifests = load_schedules(tmp_path, _registry("web-1"), _ireg())

    assert len(manifests) == 1
    assert manifests[0].inspector_parameters == {}
