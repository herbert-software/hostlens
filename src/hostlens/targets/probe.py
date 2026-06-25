"""``TargetProbe`` — promote candidates, probe reachability, emit ``ProbeResult``.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`CandidateTarget` 必须先提升为 `TargetEntry`... / §需求:`TargetProbe`
必须复用 ExecutionTarget、先 exec 判可达、产可序列化脱敏 `ProbeResult`.

Three things live here:

1. ``promote_candidate`` — map a (loose) ``CandidateTarget`` onto a validated
   ``LocalEntry`` / ``SSHEntry``. Validation truth lives in those entry
   schemas; promotion failure is a ``ValidationError`` caught by the caller
   and bucketed as ``invalid_candidate`` (never crashing the batch). The
   promoted ``SSHEntry.password``/``passphrase`` are **always None** —
   credentials travel only as ``*_env`` references, never inlined.
2. ``ProbeResult`` — a serialisable, redacted scalar record: ``reachable`` /
   ``capabilities`` / ``fingerprint`` / ``error_kind`` (closed enum). It
   carries no host address, no ``user@host``, no traceback, no free-text
   exception message.
3. ``TargetProbe`` — orchestrates one read-only ``exec`` to decide
   reachability, then projects the target's lazy-probed ``capabilities`` and
   parses a small OS/runtime fingerprint. ``probe_many`` fans out with a
   semaphore bound and isolates per-host failure into ``ProbeResult`` rather
   than letting one bad host abort the batch.
"""

from __future__ import annotations

import asyncio
import getpass
from typing import TYPE_CHECKING, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from hostlens.core.exceptions import TargetError
from hostlens.targets.inventory.models import contains_unsafe_display_chars
from hostlens.targets.registry import build_one_target
from hostlens.targets.ssh import _DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS

if TYPE_CHECKING:
    from hostlens.core.config import Settings
    from hostlens.targets.base import ExecResult, ExecutionTarget
    from hostlens.targets.config import LocalEntry, SSHEntry
    from hostlens.targets.inventory.models import CandidateTarget

__all__ = [
    "ProbeError",
    "ProbeResult",
    "TargetProbe",
    "promote_candidate",
]


# Closed value domain for ``ProbeResult.error_kind`` (mirrors the
# ``_INSPECTOR_ERROR_KINDS`` discipline in core.exceptions). ``reachable``
# results carry ``None``; everything else picks exactly one of these four.
ProbeErrorKind = Literal["unreachable", "auth_failed", "timeout", "exec_failed"]
_PROBE_ERROR_KINDS: Final[frozenset[str]] = frozenset(get_args(ProbeErrorKind))

# Full map from ``TargetError.kind`` (the set a probe ``exec`` can surface,
# see ``targets/ssh.py``) to the four-value closed ``ProbeErrorKind``. Any
# kind not listed here (including a future unlisted ``TargetError.kind``)
# falls back to ``exec_failed`` so the closed set never breaks.
_TARGET_ERROR_KIND_MAP: Final[dict[str, ProbeErrorKind]] = {
    "ssh_connect_timeout": "timeout",
    "ssh_auth_failed": "auth_failed",
    "ssh_connect_failed": "unreachable",
    "ssh_connection_lost": "unreachable",
}
_TARGET_ERROR_KIND_FALLBACK: Final[ProbeErrorKind] = "exec_failed"

# Fingerprint key allowlist. ``hostname`` is intentionally absent — even
# though the probe command runs ``hostname`` (to confirm reachability), its
# output is internal-topology intel and never lands in the fingerprint.
_FINGERPRINT_KEYS: Final[frozenset[str]] = frozenset({"os", "kernel", "arch", "runtime"})

# Per-value truncation cap. ``/etc/os-release`` is an attacker-controllable
# remote file, so its parsed values are display-only labels: truncate to 64
# chars and strip control chars / newlines before they reach a plan render.
_FINGERPRINT_VALUE_MAX_LEN: Final[int] = 64

# Read-only probe command — a **fixed literal** with no inventory-derived
# interpolation (host / user / tag never appear). Connection parameters are
# carried only through the ``ExecutionTarget`` parameterised interface, never
# spliced into this string. ``cat /etc/os-release`` (the initiator parses the
# ``KEY=value`` lines) is used instead of ``. /etc/os-release`` — ``source``
# would *execute* the untrusted remote file in a shell.
_PROBE_COMMAND: Final[str] = (
    "hostname; uname -srm; cat /etc/os-release; command -v docker podman kubectl"
)

# Default per-host exec timeout for a probe (seconds). Kept modest so one slow
# host does not stall the batch; the slow host lands in ``failed_probe`` via
# the ``timed_out`` path.
_DEFAULT_PROBE_TIMEOUT: Final[int] = 12

# Default semaphore bound for ``probe_many`` — caps simultaneous handshakes so
# importing dozens of hosts does not trigger a connection storm.
_DEFAULT_CONCURRENCY: Final[int] = 10

# Hard upper bound on the semaphore — ``--concurrency`` is operator-supplied, so
# clamp it (the cap above is the *intent*; without a ceiling ``--concurrency
# 999999`` would build a 999999-permit semaphore and fan out that many
# simultaneous handshakes — a self-inflicted connection storm / FD exhaustion).
_MAX_CONCURRENCY: Final[int] = 100

# Bytes/chars considered control characters to strip from fingerprint values.
_CONTROL_CHARS: Final[frozenset[str]] = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


class ProbeError(Exception):
    """Internal carrier for a probe failure with a pre-mapped closed kind.

    Raised inside ``TargetProbe.probe`` when reachability cannot be
    established, then turned into a ``ProbeResult(reachable=False, ...)`` by
    the same method. It never escapes the probe module — ``probe`` always
    returns a ``ProbeResult``, never raises. ``kind`` is constrained to the
    closed ``ProbeErrorKind`` set at construction time so a typo cannot leak.
    """

    def __init__(self, kind: ProbeErrorKind) -> None:
        if kind not in _PROBE_ERROR_KINDS:
            allowed = tuple(sorted(_PROBE_ERROR_KINDS))
            raise ValueError(f"ProbeError.kind must be one of {allowed}, got {kind!r}")
        super().__init__(kind)
        self.kind: ProbeErrorKind = kind


class ProbeResult(BaseModel):
    """A serialisable, redacted outcome of probing one target.

    Holds only redacted scalars so it round-trips through
    ``model_dump_json`` / ``model_validate_json`` and never carries an
    address, ``user@host``, traceback, or free-text exception message:

    - ``reachable``: ``True`` iff a probe ``exec`` returned an ``ExecResult``
      and did **not** time out. A non-zero ``exit_code`` is still reachable
      (the host can log in and run commands; only the fingerprint is partial).
    - ``capabilities``: the target's lazy-probed ``capabilities`` projected to
      the ``Capability`` enum values — **not** parsed from the probe command
      output. ``docker`` / ``podman`` / ``kubectl`` detection lands in
      ``fingerprint.runtime``, not here (the ``Capability`` enum has no
      PODMAN / KUBECTL member).
    - ``fingerprint``: allowlist keys ``{os, kernel, arch, runtime}`` only
      (never ``hostname``); each value truncated + control-char stripped.
    - ``error_kind``: ``None`` when reachable, else one of the closed
      ``ProbeErrorKind`` set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reachable: bool
    capabilities: list[str] = []
    fingerprint: dict[str, str] = {}
    # ``error_kind`` is the closed-set field; the ``Literal`` type IS the
    # construction-time guard (a non-member raises ``ValidationError``),
    # mirroring how ``_INSPECTOR_ERROR_KINDS`` pins the Inspector enum.
    error_kind: ProbeErrorKind | None = None

    @field_validator("fingerprint")
    @classmethod
    def _check_fingerprint_keys(cls, value: dict[str, str]) -> dict[str, str]:
        bad = set(value) - _FINGERPRINT_KEYS
        if bad:
            raise ValueError(
                f"fingerprint keys must be a subset of {sorted(_FINGERPRINT_KEYS)}, "
                f"got disallowed {sorted(bad)}"
            )
        return value


def promote_candidate(candidate: CandidateTarget) -> LocalEntry | SSHEntry:
    """Promote a ``CandidateTarget`` to a validated ``LocalEntry`` / ``SSHEntry``.

    The source layer (normalisation) and the config layer (``TargetEntry``
    Pydantic validation) are two independent contract boundaries; promotion is
    the explicit cross-boundary reconciliation, so a ``ValidationError`` here
    is a *classification* signal (the caller buckets it as
    ``invalid_candidate``), not a defensive fallback for an impossible branch.

    Credentials are carried only as ``*_env`` references on the candidate;
    the promoted ``SSHEntry.password`` / ``passphrase`` are therefore **always
    None** (env references are re-derived at save time via the separate
    ``password_env`` / ``passphrase_env`` params, never inlined as ``${VAR}``
    into the entry — that would double-write the credential reference).
    """

    from hostlens.targets.config import LocalEntry, SSHEntry

    if candidate.type == "local":
        return LocalEntry(name=candidate.name, type="local")

    if candidate.host is None:
        # Every source sets ``host`` for an ssh candidate; narrow the Optional
        # and turn the (otherwise unreachable) malformed case into a clean
        # invalid_candidate instead of an empty host string.
        raise ValueError("ssh candidate is missing a host")
    # Reject control / bidi characters in every inventory-sourced connection
    # field here (→ invalid_candidate) so a crafted inventory can neither spoof
    # the audit preview (host/user are echoed) nor persist a raw control-char
    # value to targets.yaml (host/user/key_path are all written). The messages
    # carry no field value, so nothing leaks through invalid_candidate.
    if contains_unsafe_display_chars(candidate.host):
        raise ValueError("ssh candidate host contains control or bidirectional characters")
    if candidate.user is not None and contains_unsafe_display_chars(candidate.user):
        raise ValueError("ssh candidate user contains control or bidirectional characters")
    if candidate.key_path is not None and contains_unsafe_display_chars(candidate.key_path):
        raise ValueError("ssh candidate key_path contains control or bidirectional characters")
    if candidate.user:
        user = candidate.user
    else:
        # OpenSSH defaults ``User`` to the local username when unspecified;
        # never an empty string (which would break the connection).
        try:
            user = getpass.getuser()
        except (KeyError, OSError) as exc:
            # getpass.getuser() raises when no USER/LOGNAME/LNAME/USERNAME env
            # var is set AND the UID has no passwd entry (minimal containers /
            # some CI sandboxes). Re-raise as the ValueError that the promote
            # loop already buckets into ``invalid_candidate`` so one userless
            # host is isolated instead of aborting the whole batch.
            raise ValueError("cannot determine default ssh user") from exc
    return SSHEntry(
        name=candidate.name,
        type="ssh",
        host=candidate.host,
        user=user,
        port=candidate.port if candidate.port is not None else 22,
        key_path=candidate.key_path,
        # password / passphrase stay None — credentials are env references
        # only, threaded separately to ``save_targets_config``.
    )


def _truncate_fingerprint_value(value: str) -> str:
    """Strip control chars / newlines and truncate to the per-value cap.

    ``/etc/os-release`` is remote-controlled, so any value sourced from it is
    untrusted: a ``PRETTY_NAME`` could smuggle a newline + internal hostname.
    We drop every control char (incl. newline / NUL) and cap the length so the
    value is a safe display label only.
    """

    cleaned = "".join(ch for ch in value if ch not in _CONTROL_CHARS)
    return cleaned[:_FINGERPRINT_VALUE_MAX_LEN]


def _parse_fingerprint(stdout: str) -> dict[str, str]:
    """Extract ``{os, kernel, arch, runtime}`` from the probe stdout.

    The probe runs ``hostname; uname -srm; cat /etc/os-release; command -v
    docker podman kubectl``. We deliberately ignore the ``hostname`` line and
    pull:

    - ``kernel`` / ``arch`` from the ``uname -srm`` line (``<sys> <release>
      <machine>`` → kernel = ``<sys> <release>``, arch = ``<machine>``).
    - ``os`` from the ``PRETTY_NAME=`` line of ``/etc/os-release``.
    - ``runtime`` from the ``command -v`` output — the basename of the first
      container runtime path found (docker / podman / kubectl).

    Best-effort: a minimal container without ``/etc/os-release`` simply yields
    a partial fingerprint. Every value is truncated + control-char stripped.
    """

    lines = stdout.splitlines()
    fingerprint: dict[str, str] = {}

    # ``uname -srm`` → kernel + arch. It is the first line that is not the
    # hostname and not a ``KEY=value`` / a path; parse positionally.
    for line in lines:
        stripped = line.strip()
        if not stripped or "=" in stripped or stripped.startswith("/"):
            continue
        parts = stripped.split()
        # uname -srm emits exactly 3 fields: sysname release machine.
        if len(parts) >= 3 and parts[0] in {"Linux", "Darwin", "FreeBSD", "SunOS", "AIX"}:
            fingerprint["kernel"] = _truncate_fingerprint_value(f"{parts[0]} {parts[1]}")
            fingerprint["arch"] = _truncate_fingerprint_value(parts[-1])
            break

    for line in lines:
        if line.startswith("PRETTY_NAME="):
            raw = line[len("PRETTY_NAME=") :].strip().strip('"').strip("'")
            fingerprint["os"] = _truncate_fingerprint_value(raw)
            break

    # ``command -v docker podman kubectl`` prints the resolved path(s), one per
    # found binary. Take the basename of the first as the runtime label.
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("/") and stripped.rsplit("/", 1)[-1] in {
            "docker",
            "podman",
            "kubectl",
        }:
            fingerprint["runtime"] = _truncate_fingerprint_value(stripped.rsplit("/", 1)[-1])
            break

    return fingerprint


class TargetProbe:
    """Reachability + capability + fingerprint probe over ``ExecutionTarget``.

    Reuses the existing target stack (asyncssh credential scrub, reconnect
    backoff, Tailscale compatibility) rather than dialling out itself.
    Construction takes ``Settings`` (threaded into ``build_one_target`` for
    the SSH branch) plus optional timeout / concurrency overrides.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        timeout: int = _DEFAULT_PROBE_TIMEOUT,
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._concurrency = max(1, min(concurrency, _MAX_CONCURRENCY))

    async def probe(self, entry: LocalEntry | SSHEntry) -> ProbeResult:
        """Probe one promoted entry and return a redacted ``ProbeResult``.

        Always returns a ``ProbeResult`` — transport / timeout failures are
        caught and mapped to ``error_kind`` rather than raised. ``reachable``
        is decided by ``exec returned an ExecResult and not timed_out``; a
        non-zero ``exit_code`` is still reachable.
        """

        try:
            # Onboarding probe opts into the cold-connect retry budget —
            # a freshly-imported Tailscale host may be 冷 on first dial
            # (spec 决策 1). Probe builds + ``aclose``s a new SSHTarget
            # per host, so the negative cache never crosses hosts.
            target = build_one_target(
                entry,
                self._settings,
                cold_connect_retry_budget_seconds=_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Construction failure (bad entry / settings) is isolated per host.
            return ProbeResult(reachable=False, error_kind="exec_failed")

        try:
            result = await self._exec_probe(target)
        except ProbeError as exc:
            return ProbeResult(reachable=False, error_kind=exc.kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Any non-transport failure (OSError from a local subprocess, an
            # asyncssh error not wrapped as TargetError, ...) is isolated to
            # this host so one bad entry cannot abort the whole batch.
            return ProbeResult(reachable=False, error_kind="exec_failed")
        finally:
            await _aclose_target(target)

        if result.timed_out:
            # Command-period timeout: exec did not raise but the host did not
            # finish in time → not reachable, mapped to ``timeout``.
            return ProbeResult(reachable=False, error_kind="timeout")

        capabilities = sorted(cap.value for cap in target.capabilities)
        fingerprint = _parse_fingerprint(result.stdout)
        return ProbeResult(
            reachable=True,
            capabilities=capabilities,
            fingerprint=fingerprint,
            error_kind=None,
        )

    async def _exec_probe(self, target: ExecutionTarget) -> ExecResult:
        """Run the fixed-literal probe command, mapping transport errors.

        ``TargetError`` (transport-level: auth / connect / no-entry / disabled)
        is mapped through the closed ``ProbeErrorKind`` table and re-raised as
        ``ProbeError`` (no host / message leaks). A timed-out ``ExecResult`` is
        returned as-is for ``probe`` to classify.
        """

        try:
            return await target.exec(_PROBE_COMMAND, timeout=self._timeout)
        except TargetError as exc:
            mapped = _TARGET_ERROR_KIND_MAP.get(exc.kind, _TARGET_ERROR_KIND_FALLBACK)
            raise ProbeError(mapped) from None

    async def probe_many(self, entries: list[LocalEntry | SSHEntry]) -> list[ProbeResult]:
        """Probe a batch concurrently under a semaphore bound, preserving order.

        Each host runs as an independent task; ``probe`` already isolates
        failure into a ``ProbeResult`` so one unreachable host neither raises
        nor stalls the batch. The returned list is index-aligned with
        ``entries``.
        """

        semaphore = asyncio.Semaphore(self._concurrency)

        async def _bounded(entry: LocalEntry | SSHEntry) -> ProbeResult:
            async with semaphore:
                return await self.probe(entry)

        return await asyncio.gather(*(_bounded(entry) for entry in entries))


async def _aclose_target(target: ExecutionTarget) -> None:
    """Best-effort close of a probe target's connection (SSH only has one).

    ``LocalTarget`` has no connection to release; ``SSHTarget.aclose`` tears
    down its control connection. We swallow any close-time error — the probe
    result is already decided and a failed close must not surface as a probe
    failure.
    """

    aclose = getattr(target, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        # Close is best-effort: the probe result is already decided, so a
        # cleanup failure must never surface as a probe failure.
        return
