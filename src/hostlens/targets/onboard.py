"""Import-pipeline orchestration: source → promote → probe → classify → plan.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`hostlens target import` 必须 dry-run 默认预览...

The CLI ``hostlens target import`` command is a thin shell; the pipeline that
turns an inventory ``ref`` into an ``ImportPlan`` lives here so the integration
tests can drive it directly without going through Typer. The orchestration is
the read-only half of the four-layer pipeline (parse → promote → probe →
classify); the write half (``save_targets_config``) stays in the CLI behind the
``--yes`` / root-refusal gate.

``build_import_plan`` does NOT touch ``targets.yaml`` other than reading the
existing names (so it can bucket name collisions into ``skipped``). It is the
caller's job to load + pre-validate the existing config and to perform the
write.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import ValidationError

from hostlens.targets.config import LocalEntry, SSHEntry
from hostlens.targets.import_plan import (
    FailedProbe,
    ImportPlan,
    InvalidCandidate,
    PendingAdd,
)
from hostlens.targets.inventory.base import (
    InventorySourceRegistry,
    register_default_sources,
)
from hostlens.targets.probe import TargetProbe, promote_candidate

if TYPE_CHECKING:
    from hostlens.core.config import Settings
    from hostlens.targets.inventory.models import CandidateTarget

__all__ = [
    "assemble_save_entries",
    "build_import_plan",
    "default_source_registry",
]

# A save entry is ``(entry, password_env, passphrase_env)`` — the shape
# ``save_targets_config`` expects so it re-derives the ``${VAR}`` placeholder
# from the env name (never from an inlined ``entry.password``).
SaveEntry = tuple["LocalEntry | SSHEntry", "str | None", "str | None"]


def default_source_registry() -> InventorySourceRegistry:
    """Return a freshly-assembled registry with the default sources.

    Assembled explicitly via ``register_default_sources`` (no module-level
    singleton) so the dispatch surface is a deliberate wiring point.
    """

    registry = InventorySourceRegistry()
    register_default_sources(registry)
    return registry


def _resolve_probe_entry(
    candidate: CandidateTarget, entry: LocalEntry | SSHEntry
) -> LocalEntry | SSHEntry:
    """Return a transient, credential-resolved entry for the probe only.

    The promoted (plan) entry keeps ``password`` / ``passphrase`` ``None`` so
    the ``ImportPlan`` never carries a secret (it is rendered, JSON-dumped, and
    optionally persisted). The probe, however, needs the *actual* credential to
    determine reachability for a cred-ful host — without it asyncssh would fail
    authentication and a perfectly reachable host would be misclassified into
    ``failed_probe``. The ``${VAR}`` env reference is resolved here into a copy
    that is built, probed, and discarded; it is never stored in the plan or
    written to ``targets.yaml`` (the save path re-derives ``${VAR}`` from the
    env name, never from ``entry.password``).
    """

    if not isinstance(entry, SSHEntry):
        return entry
    if candidate.password_env is None and candidate.passphrase_env is None:
        return entry
    return entry.model_copy(
        update={
            "password": (
                os.environ.get(candidate.password_env) if candidate.password_env else None
            ),
            "passphrase": (
                os.environ.get(candidate.passphrase_env) if candidate.passphrase_env else None
            ),
        }
    )


def _redact_validation_error(exc: ValidationError) -> str:
    """Distil a ``ValidationError`` into a redacted, field-only summary.

    Promotion failure is bucketed as ``invalid_candidate`` and rendered to the
    operator, so the summary must never carry a host / credential value — only
    the offending field location(s) + error type. Pydantic's ``loc`` is the
    field path; ``type`` is the machine-readable rule name (e.g.
    ``string_pattern_mismatch``). We deliberately drop ``msg`` (which can echo
    the bad input value) and ``input``.
    """

    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(piece) for piece in err.get("loc", ()))
        kind = str(err.get("type", "invalid"))
        parts.append(f"{loc}:{kind}" if loc else kind)
    return "; ".join(parts) or "validation_error"


async def build_import_plan(
    ref: str,
    *,
    source: str | None,
    settings: Settings,
    existing_names: set[str],
    concurrency: int,
    registry: InventorySourceRegistry | None = None,
) -> ImportPlan:
    """Run the read-only pipeline and return the four-bucket ``ImportPlan``.

    Steps (all read-only — no ``targets.yaml`` write):

    1. **Resolve + parse** the inventory ``ref`` via the source registry
       (``--source`` explicit wins; else content sniff). Parse errors
       (``ConfigError``) propagate to the caller (CLI maps to exit 2).
    2. **Promote** each ``CandidateTarget`` to a ``LocalEntry`` / ``SSHEntry``;
       a ``ValidationError`` buckets that one candidate as
       ``invalid_candidate`` (the batch continues).
    3. **Probe** every promoted entry concurrently (semaphore-bounded), with
       credential ``${VAR}`` env refs resolved into a transient probe entry so a
       cred-ful host authenticates (the plan entries stay credential-free).
    4. **Classify** by probe outcome + name collision:
       - probe OK + name free → ``to_add`` (with credential env refs)
       - name already in ``existing_names`` → ``skipped``
       - probe failed → ``failed_probe``

    The name-collision check happens *after* probing so ``skipped`` reflects an
    already-managed target regardless of its current reachability (idempotent
    re-runs land here). ``existing_names`` is passed in so this function never
    re-reads the config file.
    """

    if registry is None:
        registry = default_source_registry()

    inventory_source = registry.resolve(ref, source=source)
    candidates = inventory_source.parse(ref)

    promoted: list[tuple[CandidateTarget, LocalEntry | SSHEntry]] = []
    invalid: list[InvalidCandidate] = []
    for candidate in candidates:
        try:
            entry = promote_candidate(candidate)
        except ValidationError as exc:
            invalid.append(
                InvalidCandidate(candidate=candidate, error_summary=_redact_validation_error(exc))
            )
            continue
        except ValueError:
            # promote_candidate's own guards (e.g. ssh missing host); the
            # message carries no host/credential value, but we keep a fixed
            # redacted summary to honour the no-value-leak discipline.
            invalid.append(InvalidCandidate(candidate=candidate, error_summary="promotion_error"))
            continue
        promoted.append((candidate, entry))

    probe = TargetProbe(settings, concurrency=concurrency)
    # Probe with credential env refs resolved (transient); the plan entries in
    # ``promoted`` stay credential-free so the ImportPlan never carries a secret.
    probe_entries = [_resolve_probe_entry(candidate, entry) for candidate, entry in promoted]
    results = await probe.probe_many(probe_entries)

    to_add: list[PendingAdd] = []
    skipped: list[str] = []
    failed: list[FailedProbe] = []
    for (candidate, entry), result in zip(promoted, results, strict=True):
        if entry.name in existing_names:
            skipped.append(entry.name)
            continue
        if result.reachable:
            to_add.append(
                PendingAdd(
                    entry=entry,
                    password_env=candidate.password_env,
                    passphrase_env=candidate.passphrase_env,
                )
            )
        else:
            failed.append(
                FailedProbe(
                    entry=entry,
                    result=result,
                    password_env=candidate.password_env,
                    passphrase_env=candidate.passphrase_env,
                )
            )

    return ImportPlan(
        to_add=to_add,
        skipped=skipped,
        failed_probe=failed,
        invalid_candidate=invalid,
    )


def assemble_save_entries(plan: ImportPlan, *, include_unreachable: bool) -> list[SaveEntry]:
    """Project a plan into the ``save_targets_config`` entry list.

    - ``to_add`` candidates (probe OK) keep ``enabled=True`` and carry their
      credential env references.
    - When ``include_unreachable`` is set, ``failed_probe`` candidates are also
      registered but with ``enabled=False`` (registered-but-not-activated, so
      later inspections don't report ``requires_unmet`` noise). Their credential
      env refs ARE threaded — a disabled entry is not connected now, but the
      operator may re-enable it once the host is back, and dropping the
      ``${VAR}`` placeholder would silently lose its auth.

    The returned list is the exact shape ``save_targets_config`` consumes.
    """

    entries: list[SaveEntry] = [
        (item.entry, item.password_env, item.passphrase_env) for item in plan.to_add
    ]
    if include_unreachable:
        for failed in plan.failed_probe:
            disabled = failed.entry.model_copy(update={"enabled": False})
            entries.append((disabled, failed.password_env, failed.passphrase_env))
    return entries
