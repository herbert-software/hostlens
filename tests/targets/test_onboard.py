"""``onboard`` pipeline credential handling (PR #102 Cursor findings).

Two invariants:

- ``assemble_save_entries``: with ``--include-unreachable`` a failed probe is
  written ``enabled=False`` but its ``${VAR}`` credential refs must survive so
  re-enabling the host later does not silently lose its auth.
- ``_resolve_probe_entry``: the *probe* needs the actual credential to reach a
  cred-ful host, but the *plan* entry must stay credential-free so the
  ``ImportPlan`` never carries a secret.
"""

from __future__ import annotations

import pytest

from hostlens.targets.config import LocalEntry, SSHEntry
from hostlens.targets.import_plan import FailedProbe, ImportPlan
from hostlens.targets.inventory.models import CandidateTarget
from hostlens.targets.onboard import _resolve_probe_entry, assemble_save_entries
from hostlens.targets.probe import ProbeResult


def _unreachable_failed() -> FailedProbe:
    return FailedProbe(
        entry=SSHEntry(name="web1", type="ssh", host="10.0.0.1", user="root"),
        result=ProbeResult(
            reachable=False, capabilities=[], fingerprint={}, error_kind="unreachable"
        ),
        password_env="WEB1_PW",
        passphrase_env="WEB1_PASS",
    )


def test_include_unreachable_threads_credential_env() -> None:
    plan = ImportPlan(
        to_add=[], skipped=[], failed_probe=[_unreachable_failed()], invalid_candidate=[]
    )
    entries = assemble_save_entries(plan, include_unreachable=True)
    assert len(entries) == 1
    entry, password_env, passphrase_env = entries[0]
    assert entry.enabled is False
    assert password_env == "WEB1_PW"
    assert passphrase_env == "WEB1_PASS"


def test_skip_unreachable_omits_failed_entirely() -> None:
    plan = ImportPlan(
        to_add=[], skipped=[], failed_probe=[_unreachable_failed()], invalid_candidate=[]
    )
    assert assemble_save_entries(plan, include_unreachable=False) == []


def test_resolve_probe_entry_injects_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe entry carries the resolved secret; the plan entry stays clean."""
    monkeypatch.setenv("WEB1_PW", "s3cret")
    monkeypatch.setenv("WEB1_PASS", "keypp")
    plan_entry = SSHEntry(name="web1", type="ssh", host="10.0.0.1", user="root")
    candidate = CandidateTarget(
        name="web1",
        type="ssh",
        host="10.0.0.1",
        user="root",
        password_env="WEB1_PW",
        passphrase_env="WEB1_PASS",
    )

    probe_entry = _resolve_probe_entry(candidate, plan_entry)

    assert isinstance(probe_entry, SSHEntry)
    assert probe_entry.password == "s3cret"
    assert probe_entry.passphrase == "keypp"
    # The plan entry is never mutated — the ImportPlan must not carry a secret.
    assert plan_entry.password is None
    assert plan_entry.passphrase is None


def test_resolve_probe_entry_missing_env_var_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared env ref whose variable is unset resolves to ``None`` (no crash)."""
    monkeypatch.delenv("ABSENT_PW", raising=False)
    plan_entry = SSHEntry(name="web1", type="ssh", host="10.0.0.1", user="root")
    candidate = CandidateTarget(
        name="web1", type="ssh", host="10.0.0.1", user="root", password_env="ABSENT_PW"
    )

    probe_entry = _resolve_probe_entry(candidate, plan_entry)

    assert isinstance(probe_entry, SSHEntry)
    assert probe_entry.password is None


def test_resolve_probe_entry_without_env_ref_is_identity() -> None:
    """A cred-less ssh candidate (tizi / Tailscale SSH) is returned unchanged."""
    plan_entry = SSHEntry(name="bwg", type="ssh", host="1.2.3.4", user="root")
    candidate = CandidateTarget(name="bwg", type="ssh", host="1.2.3.4", user="root")

    assert _resolve_probe_entry(candidate, plan_entry) is plan_entry


def test_resolve_probe_entry_local_is_identity() -> None:
    """A local entry has no credential surface — returned unchanged."""
    plan_entry = LocalEntry(name="loc", type="local")
    candidate = CandidateTarget(name="loc", type="local")

    assert _resolve_probe_entry(candidate, plan_entry) is plan_entry
