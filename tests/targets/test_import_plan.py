"""Tests for ``ImportPlan`` + ``PendingAdd`` / ``FailedProbe`` / ``InvalidCandidate``.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`ImportPlan` 必须四分类、可序列化 round-trip、渲染禁泄露.

Covers the four named buckets, the empty plan, JSON round-trip, redaction of
``failed_probe`` / ``invalid_candidate`` renders, the ``to_add`` address
audit列表, and the ``0o600`` persistence reuse of ``_atomic_write_yaml``.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from hostlens.targets.config import LocalEntry, SSHEntry
from hostlens.targets.import_plan import (
    FailedProbe,
    ImportPlan,
    InvalidCandidate,
    PendingAdd,
)
from hostlens.targets.inventory.models import CandidateTarget
from hostlens.targets.probe import ProbeResult

# ---------------------------------------------------------------------------
# Bucket model shapes + four-classification
# ---------------------------------------------------------------------------


def _sample_plan() -> ImportPlan:
    return ImportPlan(
        to_add=[
            PendingAdd(
                entry=SSHEntry(name="web", type="ssh", host="10.0.0.5", user="alice"),
                password_env="WEB_PWD",
            ),
            PendingAdd(entry=LocalEntry(name="here", type="local")),
        ],
        skipped=["existing-box"],
        failed_probe=[
            FailedProbe(
                entry=SSHEntry(name="down", type="ssh", host="10.0.0.9", user="bob"),
                result=ProbeResult(reachable=False, error_kind="unreachable"),
            )
        ],
        invalid_candidate=[
            InvalidCandidate(
                candidate=CandidateTarget(name="weird", type="ssh", host="h", user="u"),
                error_summary="host: field required",
            )
        ],
    )


def test_four_named_buckets_typed() -> None:
    """Spec §场景:四分类 + 元素类型 — each bucket is a named model (not tuple)."""

    plan = _sample_plan()
    assert all(isinstance(item, PendingAdd) for item in plan.to_add)
    assert all(isinstance(name, str) for name in plan.skipped)
    assert all(isinstance(item, FailedProbe) for item in plan.failed_probe)
    assert all(isinstance(item, InvalidCandidate) for item in plan.invalid_candidate)


def test_pending_add_entry_has_no_inline_password() -> None:
    """``PendingAdd.entry.password`` stays None — only env refs carry creds."""

    item = _sample_plan().to_add[0]
    assert isinstance(item.entry, SSHEntry)
    assert item.entry.password is None
    assert item.password_env == "WEB_PWD"


def test_empty_inventory_yields_empty_plan() -> None:
    """Spec §场景:空 inventory → 空 plan → exit 0 (render says nothing-to-import)."""

    plan = ImportPlan()
    assert plan.is_empty is True
    assert plan.render_diff() == "nothing to import"
    obj = plan.to_json_obj()
    assert obj == {
        "to_add": [],
        "skipped": [],
        "failed_probe": [],
        "invalid_candidate": [],
    }


# ---------------------------------------------------------------------------
# JSON round-trip (named fields, not positional)
# ---------------------------------------------------------------------------


def test_plan_json_round_trip_equivalent() -> None:
    """Spec §场景:plan 可 JSON round-trip — named fields, no positional drift."""

    plan = _sample_plan()
    restored = ImportPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    # discriminated union survives round-trip (SSH stays SSH, local stays local)
    assert isinstance(restored.to_add[0].entry, SSHEntry)
    assert isinstance(restored.to_add[1].entry, LocalEntry)


# ---------------------------------------------------------------------------
# Redaction of failed_probe / invalid_candidate renders
# ---------------------------------------------------------------------------


def test_render_diff_redacts_failed_and_invalid_but_lists_to_add_host() -> None:
    """Spec §场景:渲染不泄露 host/凭据 + to_add 完整列出连接地址供审计."""

    plan = _sample_plan()
    diff = plan.render_diff()

    # to_add lists the connection address (audit need).
    assert "alice@10.0.0.5" in diff
    # failed_probe surfaces only name + error_kind, never the host.
    assert "down" in diff
    assert "unreachable" in diff
    assert "10.0.0.9" not in diff
    # invalid_candidate surfaces name + summary, never a host.
    assert "weird" in diff
    assert "host: field required" in diff


def test_render_diff_strips_control_chars_from_host() -> None:
    """A crafted inventory host/user with control bytes cannot spoof the audit line."""

    plan = ImportPlan(
        to_add=[
            PendingAdd(
                entry=SSHEntry(
                    name="spoof",
                    type="ssh",
                    host="10.0.0.5\r   + fake -> attacker",
                    user="al\tice",
                ),
            )
        ],
    )
    diff = plan.render_diff()

    # No raw control bytes reach the terminal (no carriage-return overwrite).
    assert "\r" not in diff
    assert "\t" not in diff
    # Printable text survives — only the control bytes are dropped.
    assert "10.0.0.5" in diff
    assert "alice@" in diff


def test_render_diff_strips_c1_and_bidi_control_chars() -> None:
    """C1 (single-byte CSI \\x9b), NEL \\x85, and bidi override U+202E are stripped.

    The two-byte ESC form is C0, but the single-byte C1 CSI / bidi overrides
    would otherwise survive and still spoof the audit line.
    """

    plan = ImportPlan(
        to_add=[
            PendingAdd(
                entry=SSHEntry(
                    name="spoof",
                    type="ssh",
                    host="1.2.3.4\x9b2K\x85" + chr(0x202E) + "evil",
                    user="root",
                ),
            )
        ],
    )
    diff = plan.render_diff()

    assert "\x9b" not in diff
    assert "\x85" not in diff
    assert chr(0x202E) not in diff
    assert "1.2.3.4" in diff


def test_render_json_redacts_failed_and_invalid() -> None:
    """``--json`` surface drops host / fingerprint for the failure buckets."""

    plan = _sample_plan()
    obj = json.loads(plan.render_json())

    # to_add carries host (operator audit).
    assert obj["to_add"][0]["host"] == "10.0.0.5"
    assert obj["to_add"][0]["password_env"] == "WEB_PWD"
    # failure buckets carry name + error_kind / error_summary only.
    assert obj["failed_probe"] == [{"name": "down", "error_kind": "unreachable"}]
    assert obj["invalid_candidate"] == [{"name": "weird", "error_summary": "host: field required"}]
    # the failed host never appears anywhere in the json surface.
    assert "10.0.0.9" not in plan.render_json()


def test_render_json_is_stable_sorted() -> None:
    """``--json`` is sorted-keys so snapshots are stable across runs."""

    plan = _sample_plan()
    first = plan.render_json()
    second = plan.render_json()
    assert first == second
    # sorted keys → top-level keys come out alphabetically.
    parsed = json.loads(first)
    assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# Persistence — 0600 via _atomic_write_yaml
# ---------------------------------------------------------------------------


def test_plan_save_is_0600(tmp_path: Path) -> None:
    """Spec §场景:plan 落盘必须 0600 (reuse save_targets_config atomic write)."""

    plan = _sample_plan()
    out = tmp_path / "plan.yaml"
    plan.save(out)

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    # parent dir tightened to 0700 by the shared atomic-write primitive.
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_plan_save_round_trips_through_disk(tmp_path: Path) -> None:
    """The persisted plan reloads equivalently (model_validate of the dump)."""

    plan = _sample_plan()
    out = tmp_path / "plan.yaml"
    plan.save(out)

    import yaml

    raw = yaml.safe_load(out.read_text())
    restored = ImportPlan.model_validate(raw)
    assert restored == plan
