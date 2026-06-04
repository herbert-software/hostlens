"""Tests for `hostlens.scheduler.loader` — load-time semantic validation.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``
§需求:加载器必须扫描... / M4 每个 manifest 必须恰好一个 target.

Uses a real `TargetRegistry` (built from `LocalEntry` config) so the
injected-registry contract is exercised end-to-end — no mock registry.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError
from hostlens.scheduler.loader import load_schedules
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config


def _registry(*names: str) -> TargetRegistry:
    config = TargetsConfig(
        version="1",
        targets=[LocalEntry(name=name, type="local") for name in names],
    )
    return build_registry_from_config(config, Settings())


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
    manifests = load_schedules(tmp_path, _registry("web-1", "web-2"))

    assert len(manifests) == 2
    assert {m.name for m in manifests} == {"nightly", "morning"}


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_schedules(tmp_path / "nope", _registry()) == []


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert load_schedules(tmp_path, _registry()) == []


def test_target_not_registered_fail_loud(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)  # references web-1
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("other-host"))

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
        load_schedules(tmp_path, _registry("web-1", "web-2"))

    msg = str(exc.value)
    assert "a.yaml" in msg
    assert "targets" in msg
    assert "single target" in msg.lower()


def test_single_target_loads(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _VALID_BODY)
    manifests = load_schedules(tmp_path, _registry("web-1"))

    assert len(manifests) == 1
    assert manifests[0].targets == ["web-1"]


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
        load_schedules(tmp_path, _registry("web-1"))

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
        load_schedules(tmp_path, _registry("web-1"))

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
        load_schedules(tmp_path, _registry("web-1"))

    assert "bad.yaml" in str(exc.value)


def test_non_mapping_root_fail_loud(tmp_path: Path) -> None:
    (tmp_path / "scalar.yaml").write_text("just a string\n")
    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, _registry("web-1"))

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
    manifests = load_schedules(tmp_path, _registry("web-1"))

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
    manifests = load_schedules(tmp_path, _registry("web-1"))

    assert manifests[0].report.diff_with_last is True
