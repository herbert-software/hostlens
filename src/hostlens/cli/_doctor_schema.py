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

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BackendHealthRow",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "InspectorLoadErrorRow",
    "InspectorMissingSecretRow",
    "InspectorsHealth",
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
    type: Literal["local", "ssh", "replay", "docker"]
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


# M1 (`add-inspector-plugin-system`) extension: aggregate inspector
# health rolled up under ``DoctorReport.inspectors``. Kept as a single
# nested model rather than ``list[InspectorHealth]`` because the
# checker computes one summary status across all loaded inspectors
# (errors / missing secrets / count) — there is no per-inspector row.


class InspectorLoadErrorRow(BaseModel):
    """Single inspector manifest load failure surfaced under ``inspectors.errors``.

    Mirrors ``hostlens.inspectors.registry.RegistryLoadError`` but with
    ``path`` rendered as a string for stable JSON output (the source
    dataclass holds a ``Path`` which pydantic would otherwise emit as a
    POSIX-coerced string anyway — keeping it explicit here means the JSON
    schema is self-describing).
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str
    detail: str


class InspectorMissingSecretRow(BaseModel):
    """Single ``(inspector, secret)`` row under ``inspectors.missing_secrets``.

    Carries only the env-var **name** — never the value, which doctor
    never reads (spec §需求:`hostlens doctor` 必须新增 `inspectors` section
    explicitly forbids dereferencing missing secrets to ``os.environ``).
    """

    model_config = ConfigDict(extra="forbid")

    inspector: str
    secret: str


class InspectorsHealth(BaseModel):
    """Aggregate health of the Inspector registry.

    Status mapping (spec §需求:`hostlens doctor` 必须新增 `inspectors` section):

    - ``fail`` : ``errors`` non-empty (registry build collected per-file
      failures, or duplicate_inspector raised fatally).
    - ``warn`` : ``errors`` empty but ``missing_secrets`` non-empty.
    - ``ok``   : both lists empty.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "warn", "fail"]
    loaded: int
    errors: list[InspectorLoadErrorRow] = Field(default_factory=list)
    missing_secrets: list[InspectorMissingSecretRow] = Field(default_factory=list)


class BackendHealthRow(BaseModel):
    """LLM backend section under ``DoctorReport.backend`` (M2).

    Renders as a single row (not a list) because at most one backend is
    configured per Hostlens instance — multi-backend setups are an M10.5
    concern that ships with its own schema bump.

    Field policy:

    - ``type``: surfaced verbatim from ``settings.backend.type`` (one of the
      ``BackendType`` Literal values).
    - ``api_key_set``: boolean derived from ``settings.backend.api_key is
      not None``. Never carries the actual value.
    - ``api_key_fingerprint``: opaque, non-reversible fingerprint produced
      by ``hostlens.agent.backend.api_key_fingerprint`` (``"<unset>"`` /
      ``"<redacted>"`` / ``"<first4>...<last4>"``). The factory must never
      surface the raw secret in this field.
    - ``health_check_*``: populated when the backend implements
      ``BackendDiagnostics``. ``error`` already passed through
      ``redact_text`` at the backend layer; doctor does NOT re-redact (the
      backend is the canonical scrubber).
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    api_key_set: bool
    api_key_fingerprint: str | None = None
    # Optional — only populated when the backend exposes ``BackendDiagnostics``
    # AND the health-check call completed (success OR failure) within the
    # 5-second timeout.
    health_check_is_healthy: bool | None = None
    health_check_latency_ms: float | None = None
    health_check_error: str | None = None


class DoctorReport(BaseModel):
    """Top-level JSON contract emitted by `hostlens doctor --json`."""

    model_config = ConfigDict(extra="forbid")

    version: str = "0.1.0"
    timestamp: datetime
    checks: dict[str, CheckResult]
    ready: bool
    # Optional so a missing ``targets`` key in older snapshots does not
    # break the schema test; defaults to ``[]`` which renders as
    # ``"targets": []`` in JSON.
    targets: list[TargetHealth] = Field(default_factory=list)
    # Optional with a sentinel default so legacy snapshot tests that only
    # assert on ``checks`` / ``ready`` keep working. ``default_factory``
    # ensures a fresh ``InspectorsHealth`` (with its own ``errors`` /
    # ``missing_secrets`` lists) per ``DoctorReport`` rather than a
    # class-level shared instance.
    inspectors: InspectorsHealth = Field(
        default_factory=lambda: InspectorsHealth(status="ok", loaded=0)
    )
    # M2 add-llm-backend-protocol: optional backend section. ``None`` when
    # ``settings.backend is None`` (M0/M1 configs); rendered as a single
    # row when configured.
    backend: BackendHealthRow | None = None
