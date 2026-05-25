"""Pydantic schema for `hostlens doctor --json` output.

The shape defined here is the **stable contract** documented in
`openspec/changes/bootstrap-project-skeleton/specs/cli-foundation/spec.md`
and `design.md` D-9.

Schema evolution policy (D-9):
- Required fields (top-level `version` / `timestamp` / `checks` / `ready`;
  each check's `status`) are snapshot-locked. Any change is breaking and
  MUST bump `DoctorReport.version`.
- Optional fields (`detail` / `path` / additional metadata) may be added
  without bumping `version`. Deletion or semantic change is breaking.

`extra="forbid"` rejects undeclared fields so accidental drift in producers
fails loudly instead of leaking through into the JSON contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "TargetConnectivity",
    "TargetCredentialSource",
    "TargetHealth",
]


CheckStatus = Literal["ok", "present", "missing", "unreadable", "error"]
"""Enum of valid `status` values for any check.

- `ok`         : health-style check passed (e.g. python_version, config_dir)
- `present`    : existence-style check found the resource (e.g. env var set)
- `missing`    : existence-style check did NOT find the resource
- `unreadable` : resource exists but cannot be accessed (e.g. perms)
- `error`      : unexpected failure inside the checker itself
"""


class CheckResult(BaseModel):
    """Single check entry inside `DoctorReport.checks`."""

    model_config = ConfigDict(extra="forbid")

    status: CheckStatus
    detail: str | None = None
    path: str | None = None


# M1 (`add-execution-target-abstraction`) extension: per-target health
# row reported under ``DoctorReport.targets``. Kept separate from
# ``CheckResult`` because targets carry several structured fields
# (``connectivity`` / ``credential_source`` / ``capabilities``) that do
# not fit the M0 single-``status`` shape — encoding them as one
# ``CheckResult`` per field would lose the per-target grouping that
# doctor's human render relies on.

TargetConnectivity = Literal["ok", "failed", "skipped"]
"""Outcome of doctor's lightweight connectivity probe (`echo` over the target).

- ``ok``      : probe ran and returned exit 0
- ``failed``  : probe raised ``TargetError`` or returned non-zero / timed out
- ``skipped`` : target is disabled in targets.yaml; no probe attempted
"""

TargetCredentialSource = Literal["env_var", "inline_plaintext", "key_only", "none"]
"""How the target's credentials are sourced (spec §需求:`hostlens doctor`).

- ``env_var``         : ``password`` / ``passphrase`` is a ``${VAR}`` placeholder
- ``inline_plaintext``: literal secret in yaml (doctor warns; not exit 1)
- ``key_only``        : ``key_path`` set with no password / passphrase
- ``none``            : no credentials configured (e.g. LocalTarget, or
                        an SSH target relying purely on agent forwarding —
                        which Hostlens disables, so this is mostly LocalTarget)
"""


class TargetHealth(BaseModel):
    """Single target row inside ``DoctorReport.targets``.

    ``extra="forbid"`` so accidental new fields fail loudly instead of
    silently leaking into the JSON contract that downstream Agent
    callers depend on.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["local", "ssh"]
    enabled: bool
    connectivity: TargetConnectivity
    credential_source: TargetCredentialSource
    capabilities: list[str]
    # When ``connectivity == "failed"`` this carries the ``TargetError``
    # ``kind`` so consumers (Agent, CI) can branch on the error class
    # without parsing free text. Never carries the raw ``original``
    # exception's stringification — credential scrubbing happens at the
    # throw site inside SSHTarget.
    error_kind: str | None = None


class DoctorReport(BaseModel):
    """Top-level JSON contract emitted by `hostlens doctor --json`."""

    model_config = ConfigDict(extra="forbid")

    version: str = "0.1.0"
    timestamp: datetime
    checks: dict[str, CheckResult]
    ready: bool
    # M1 additive field — optional so a missing ``targets`` key in older
    # snapshots does not break the schema test (the field defaults to
    # an empty list, which renders as ``"targets": []`` in JSON).
    targets: list[TargetHealth] = []
