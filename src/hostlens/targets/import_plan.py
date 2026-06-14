"""``ImportPlan`` — the four-bucket, serialisable, redaction-safe import preview.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`ImportPlan` 必须四分类、可序列化 round-trip、渲染禁泄露.

``ImportPlan`` is the last **read-only** artefact before a write. It sorts
candidates into four named buckets (each element is a named Pydantic model,
not a bare tuple — tuples deserialise positionally in Pydantic v2 and clash
with the ``TargetEntry`` discriminated union):

- ``to_add``      → ``PendingAdd``      (probe OK + name free; entry + env refs)
- ``skipped``     → ``str``             (name already in ``targets.yaml``)
- ``failed_probe``→ ``FailedProbe``     (promoted but unreachable)
- ``invalid_candidate`` → ``InvalidCandidate`` (promotion failed; redacted summary)

The whole model is pure Pydantic so it ``model_dump_json`` /
``model_validate_json`` round-trips (for dry-run artefact persistence and
proposal B's ``--from-plan`` reuse). Rendering (diff + ``--json``) for the
``failed_probe`` / ``invalid_candidate`` buckets emits only ``error_kind`` +
candidate name — never a raw host / ``user@host`` / traceback / fingerprint
value. ``to_add`` deliberately lists every connection address so an operator
can audit for unexpected hosts before passing ``--yes``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hostlens.targets.config import LocalEntry, SSHEntry, _atomic_write_yaml
from hostlens.targets.inventory.models import CandidateTarget
from hostlens.targets.probe import ProbeResult

__all__ = [
    "FailedProbe",
    "ImportPlan",
    "InvalidCandidate",
    "PendingAdd",
]


class PendingAdd(BaseModel):
    """A candidate that probed reachable and whose name is free to add.

    ``entry.password`` / ``passphrase`` are always ``None`` (credentials are
    env references); the ``password_env`` / ``passphrase_env`` names are
    threaded separately so ``save_targets_config`` re-derives the ``${VAR}``
    placeholder from the env name (never from an inlined entry field).
    """

    model_config = ConfigDict(extra="forbid")

    entry: LocalEntry | SSHEntry
    password_env: str | None = None
    passphrase_env: str | None = None


class FailedProbe(BaseModel):
    """A promoted entry whose probe failed (unreachable / auth / timeout).

    Carries the same ``password_env`` / ``passphrase_env`` refs as
    ``PendingAdd``: with ``--include-unreachable`` a failed entry is still
    written (``enabled=False``), and its ``${VAR}`` credential placeholder
    must be preserved so re-enabling the host later does not lose its auth.
    """

    model_config = ConfigDict(extra="forbid")

    entry: LocalEntry | SSHEntry
    result: ProbeResult
    password_env: str | None = None
    passphrase_env: str | None = None


class InvalidCandidate(BaseModel):
    """A candidate that failed promotion to a ``TargetEntry``.

    ``error_summary`` is a redacted scalar — a short ``ValidationError``
    digest with field names only, never host / credential values. Rendering
    surfaces only this summary + the candidate name.
    """

    model_config = ConfigDict(extra="forbid")

    candidate: CandidateTarget
    error_summary: str


class ImportPlan(BaseModel):
    """The four-bucket import preview — pure Pydantic, round-trippable."""

    model_config = ConfigDict(extra="forbid")

    to_add: list[PendingAdd] = []
    skipped: list[str] = []
    failed_probe: list[FailedProbe] = []
    invalid_candidate: list[InvalidCandidate] = []

    @property
    def is_empty(self) -> bool:
        """True when every bucket is empty (e.g. empty inventory → empty plan)."""

        return not (self.to_add or self.skipped or self.failed_probe or self.invalid_candidate)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_diff(self) -> str:
        """Render a human-readable, redaction-safe diff of the plan.

        - ``to_add`` lists each entry's connection address (the pre-write
          audit point so an operator spots an unexpected host).
        - ``skipped`` lists names only.
        - ``failed_probe`` / ``invalid_candidate`` surface only
          ``error_kind`` / ``error_summary`` + name — never a raw host,
          ``user@host``, traceback, or fingerprint value.
        """

        if self.is_empty:
            return "nothing to import"

        lines: list[str] = []

        if self.to_add:
            lines.append(f"to_add ({len(self.to_add)}):")
            for item in self.to_add:
                lines.append(f"  + {_pending_add_label(item)}")
        if self.skipped:
            lines.append(f"skipped ({len(self.skipped)}):")
            for name in self.skipped:
                lines.append(f"  = {name} (already in targets.yaml)")
        if self.failed_probe:
            lines.append(f"failed_probe ({len(self.failed_probe)}):")
            for failed in self.failed_probe:
                kind = failed.result.error_kind or "unknown"
                lines.append(f"  ! {failed.entry.name} ({kind})")
        if self.invalid_candidate:
            lines.append(f"invalid_candidate ({len(self.invalid_candidate)}):")
            for invalid in self.invalid_candidate:
                lines.append(f"  x {invalid.candidate.name} ({invalid.error_summary})")

        return "\n".join(lines)

    def to_json_obj(self) -> dict[str, Any]:
        """Return a redaction-safe ``--json`` object for stdout.

        ``to_add`` includes the connection address (operator audit need);
        ``failed_probe`` / ``invalid_candidate`` carry only the name +
        ``error_kind`` / ``error_summary`` (no host / traceback / fingerprint
        value). Capabilities (non-sensitive) are included for ``failed_probe``
        but the fingerprint dict is dropped from the JSON surface to avoid
        leaking a smuggled value.
        """

        return {
            "to_add": [
                {
                    "name": item.entry.name,
                    "type": item.entry.type,
                    "host": item.entry.host if isinstance(item.entry, SSHEntry) else None,
                    "password_env": item.password_env,
                    "passphrase_env": item.passphrase_env,
                }
                for item in self.to_add
            ],
            "skipped": list(self.skipped),
            "failed_probe": [
                {"name": failed.entry.name, "error_kind": failed.result.error_kind}
                for failed in self.failed_probe
            ],
            "invalid_candidate": [
                {"name": invalid.candidate.name, "error_summary": invalid.error_summary}
                for invalid in self.invalid_candidate
            ],
        }

    def render_json(self) -> str:
        """Serialise ``to_json_obj`` to a stable, sorted JSON string."""

        return json.dumps(self.to_json_obj(), indent=2, sort_keys=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist the full plan (incl. ``to_add`` hosts) to ``path`` at ``0o600``.

        The serialised plan carries ``to_add``'s plaintext host (a
        lateral-movement map), so it reuses ``save_targets_config``'s atomic
        ``0o600`` write discipline (``_atomic_write_yaml``) — it must never be
        world-readable. This is the persistence path for dry-run artefacts /
        proposal B's ``--from-plan``; the ``--json`` stdout surface (operator
        audit) is separate and may show hosts.
        """

        raw = json.loads(self.model_dump_json())
        _atomic_write_yaml(path, raw)


def _strip_control_chars(value: str) -> str:
    """Drop C0 control characters + DEL from an operator-supplied string.

    ``host`` / ``user`` come from the inventory (ssh_config ``HostName`` / yaml
    ``host``) and are echoed verbatim in the dry-run audit diff. A crafted
    inventory could embed ``\\r`` / ANSI bytes to overwrite or spoof the very
    preview line the operator inspects before passing ``--yes``; stripping
    controls makes the audit line unforgeable.
    """

    return "".join(ch for ch in value if ch >= " " and ch != "\x7f")


def _pending_add_label(item: PendingAdd) -> str:
    """Render one ``to_add`` row including its connection address.

    SSH entries show ``name -> user@host:port`` so the operator can audit the
    final connection target before the write; local entries show the name only.
    The host / user are control-char-stripped so a crafted inventory cannot
    spoof the audit line.
    """

    entry = item.entry
    if isinstance(entry, SSHEntry):
        port = "" if entry.port == 22 else f":{entry.port}"
        user = f"{_strip_control_chars(entry.user)}@" if entry.user else ""
        return f"{entry.name} -> {user}{_strip_control_chars(entry.host)}{port}"
    return f"{entry.name} (local)"
