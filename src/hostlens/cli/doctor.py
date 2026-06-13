"""`hostlens doctor` implementation.

M0 checkers report on local environment health:
- `check_python_version()`: interpreter is >= 3.11 (project floor).
- `check_anthropic_key()` : `ANTHROPIC_API_KEY` env var is present.
- `check_config_dir()`    : `~/.config/hostlens/` exists and is readable.

M1 (`add-execution-target-abstraction`) adds:
- `_check_targets()`      : per-target connectivity probe + credential
                            source detection + capability readout. See
                            spec §需求:`hostlens doctor` 必须新增 targets 健康检查.

`run_doctor(json_output)` builds a `DoctorReport`, prints it (human Rich
table or strict JSON), and returns the process exit code.

================================================================
SECURITY REVIEW CHECKLIST — `check_anthropic_key()`
================================================================
Do NOT regress these invariants without an explicit spec update:

- [ ] Function body MUST NOT read `os.environ["ANTHROPIC_API_KEY"]`
      or `os.environ.get("ANTHROPIC_API_KEY")`. Use membership test
      (`"ANTHROPIC_API_KEY" in os.environ`) only — existence-style
      checks have no need for the value.
- [ ] Returned `CheckResult.detail` MUST be the literal `None`. No
      conditional assignment (no length, hash, prefix, suffix, mask,
      or any other value-derived string).
- [ ] No `print()`, no `logger.info()`, no exception messages that
      could capture the env value (even indirectly via f-strings).
- [ ] Any future "validate the key actually works" probe MUST live in
      a separate checker with its own spec entry; do not extend this
      function with side effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

# ``PyYAML`` ships no PEP 561 marker — see CLI target.py for the same
# rationale. We only need raw yaml parsing here (no schema validation)
# to detect `${VAR}` placeholders that the loader would otherwise have
# expanded into real secret values by the time it returns.
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from hostlens.agent.backend import (
    BackendDiagnostics,
    api_key_fingerprint,
    create_backend,
)
from hostlens.cli._doctor_schema import (
    BackendHealthRow,
    CheckResult,
    DoctorReport,
    InspectorLoadErrorRow,
    InspectorMissingSecretRow,
    InspectorsHealth,
    TargetConnectivity,
    TargetCredentialSource,
    TargetHealth,
)
from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import ConfigError, InspectorError, TargetError
from hostlens.core.logging import configure_logging
from hostlens.core.redact import redact_text
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.remediation.audit import default_audit_path
from hostlens.scheduler.loader import load_schedules
from hostlens.scheduler.schema import ScheduleManifest
from hostlens.scheduler.store import RunStore
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import TargetEntry, TargetsConfig, load_targets_config
from hostlens.targets.registry import build_registry_from_config

__all__ = [
    "check_anthropic_key",
    "check_config_dir",
    "check_mcp",
    "check_python_version",
    "check_remediation",
    "run_doctor",
]


_CONFIG_DIR_DEFAULT = Path("~/.config/hostlens")


# Same regex used by the targets/config loader; doctor needs to detect
# ``${VAR}`` placeholders by reading the raw yaml (the loader expands
# them before returning), so the constant is duplicated here rather
# than imported — keeping doctor decoupled from the loader's private
# implementation details.
_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def check_python_version() -> CheckResult:
    """Report on the running interpreter version vs the >=3.11 floor."""

    info = sys.version_info
    detail = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) < (3, 11):
        return CheckResult(status="error", detail=detail)
    return CheckResult(status="ok", detail=detail)


def check_anthropic_key() -> CheckResult:
    """Report existence (not value) of `ANTHROPIC_API_KEY`.

    SECURITY: This function intentionally performs ONLY a membership
    test against `os.environ`. It must never read, mask, hash, or
    surface any portion of the key value. See module-level checklist.
    """

    if "ANTHROPIC_API_KEY" in os.environ:
        return CheckResult(status="present", detail=None)
    return CheckResult(status="missing", detail=None)


def check_mcp() -> CheckResult:
    """Report whether the official ``mcp`` SDK is importable."""

    if importlib.util.find_spec("mcp") is not None:
        return CheckResult(status="ok", detail=None)
    return CheckResult(status="missing", detail=None)


def check_config_dir() -> CheckResult:
    """Report on `~/.config/hostlens/` existence and readability."""

    path = _CONFIG_DIR_DEFAULT.expanduser()
    path_str = str(path)
    if not path.exists():
        return CheckResult(status="missing", detail=None, path=path_str)
    if not path.is_dir():
        return CheckResult(
            status="error",
            detail="path exists but is not a directory",
            path=path_str,
        )
    if not os.access(path, os.R_OK):
        return CheckResult(status="unreadable", detail=None, path=path_str)
    return CheckResult(status="ok", detail=None, path=path_str)


# ---------------------------------------------------------------------------
# M1: per-target health checks
# ---------------------------------------------------------------------------


def _detect_credential_source(
    entry_type: str,
    raw_entry: dict[str, Any] | None,
) -> TargetCredentialSource:
    """Classify how the target's credentials are sourced.

    Reads from the **raw** yaml mapping (pre-expansion) so ``${VAR}``
    placeholders are still visible — by the time the loader returns,
    those have been replaced with real secret values and doctor would
    misclassify them as ``inline_plaintext``.

    Classification rules (spec §需求:`hostlens doctor`):

    - LocalTarget: always ``none`` (no SSH credentials by definition).
    - SSH with ``password`` or ``passphrase`` matching ``${VAR}`` →
      ``env_var``.
    - SSH with a literal ``password`` or ``passphrase`` string →
      ``inline_plaintext`` (doctor warns, but does NOT exit 1).
    - SSH with ``key_path`` set and no password / passphrase →
      ``key_only``.
    - Otherwise → ``none``.
    """

    if entry_type != "ssh":
        return "none"
    if raw_entry is None:
        return "none"
    password = raw_entry.get("password")
    passphrase = raw_entry.get("passphrase")
    # Either credential being a ${VAR} placeholder makes the whole
    # entry env-sourced from doctor's perspective. Mixed literal +
    # placeholder is unusual; we report ``inline_plaintext`` to surface
    # the literal so the operator can fix it.
    has_inline_password = (
        isinstance(password, str) and _PLACEHOLDER_PATTERN.fullmatch(password) is None
    )
    has_inline_passphrase = (
        isinstance(passphrase, str) and _PLACEHOLDER_PATTERN.fullmatch(passphrase) is None
    )
    has_env_password = (
        isinstance(password, str) and _PLACEHOLDER_PATTERN.fullmatch(password) is not None
    )
    has_env_passphrase = (
        isinstance(passphrase, str) and _PLACEHOLDER_PATTERN.fullmatch(passphrase) is not None
    )
    if has_inline_password or has_inline_passphrase:
        return "inline_plaintext"
    if has_env_password or has_env_passphrase:
        return "env_var"
    if raw_entry.get("key_path") is not None:
        return "key_only"
    return "none"


def _read_raw_entries(path: Path) -> dict[str, dict[str, Any]]:
    """Parse ``targets.yaml`` without env-var expansion.

    Returns a mapping of target name → raw entry dict. We swallow any
    parse error here because the strict ``load_targets_config`` call in
    ``_check_targets`` will surface schema problems through its own
    ``ConfigError`` path — doctor's credential-source classifier just
    needs the raw text for ``${VAR}`` detection.
    """

    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    targets = raw.get("targets")
    if not isinstance(targets, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in targets:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item
    return out


async def _probe_target(target: ExecutionTarget) -> tuple[TargetConnectivity, str | None]:
    """Run a single lightweight ``echo`` probe.

    Returns ``("ok", None)`` on a successful probe, ``("failed",
    error_kind)`` on TargetError, and ``("failed", "non_zero_exit")``
    / ``("failed", "timed_out")`` on protocol-level failures. We
    deliberately keep the timeout small (5 s) so doctor stays
    responsive even with a few unreachable SSH targets.
    """

    try:
        result = await target.exec("echo hostlens-doctor-probe", timeout=5)
    except TargetError as exc:
        return "failed", exc.kind
    except Exception as exc:  # pragma: no cover — defensive
        return "failed", type(exc).__name__
    if result.timed_out:
        return "failed", "timed_out"
    if result.exit_code is None or result.exit_code != 0:
        return "failed", "non_zero_exit"
    return "ok", None


def _check_targets(settings: Settings) -> list[TargetHealth]:
    """Per-target health probe for ``hostlens doctor``.

    Behaviour per spec §需求:`hostlens doctor` 必须新增 targets 健康检查:

    - Disabled targets emit ``connectivity: skipped`` and skip the
      actual probe (we never want doctor to dial an explicitly disabled
      host).
    - Enabled targets get a 5-second ``echo`` probe. Failure carries
      the ``TargetError.kind`` so callers can branch on the error
      class without parsing free text.
    - ``credential_source`` reads the raw yaml so ``${VAR}``
      placeholders are still detectable (the loader expanded them by
      the time it returned).

    Returns an empty list when no targets are configured; the caller
    decides whether to render a "run `hostlens target add`" hint.
    """

    # Short-circuit when the config file does not exist. ``load_targets_config``
    # would log a structlog INFO ("config file not found, returning empty
    # TargetsConfig") which the PrintLogger routes to **stdout** — that
    # would corrupt our strict-JSON output stream. Skipping the call
    # entirely on the absent-file path keeps the JSON contract intact
    # for the snapshot tests that pin the base doctor output schema.
    if not settings.targets_config_path.exists():
        return []

    try:
        config = load_targets_config(settings.targets_config_path)
        registry = build_registry_from_config(config, settings)
    except (ConfigError, TargetError, ValidationError, yaml.YAMLError) as exc:
        # Spec §需求:`hostlens doctor` 必须新增 targets 健康检查 +
        # CLAUDE.md "no defensive fallback": a malformed targets.yaml
        # must NOT silently appear as "zero targets configured" — that
        # hides real misconfiguration from operators. Surface as a
        # synthetic failure row so the existing `connectivity ==
        # "failed"` rule in render_targets_for_human / exit-code logic
        # drives doctor to exit 1.
        return [
            TargetHealth(
                name="<config>",
                type="local",
                enabled=True,
                connectivity="failed",
                credential_source="none",
                capabilities=[],
                error_kind=getattr(exc, "kind", type(exc).__name__),
            )
        ]

    raw_entries = _read_raw_entries(settings.targets_config_path)

    rows: list[TargetHealth] = []
    enabled_entries: list[tuple[TargetEntry, ExecutionTarget, TargetCredentialSource]] = []
    for entry in registry.list_entries():
        target = registry.get(entry.name)
        credential_source = _detect_credential_source(
            entry.type,
            raw_entries.get(entry.name),
        )
        if entry.enabled is False:
            rows.append(
                TargetHealth(
                    name=entry.name,
                    type=entry.type,
                    enabled=False,
                    connectivity="skipped",
                    credential_source=credential_source,
                    capabilities=sorted(c.value for c in target.capabilities),
                    error_kind=None,
                )
            )
            continue
        if entry.type == "replay":
            # ReplayTarget is an offline fixture replayer — it has no live
            # endpoint. The generic ``echo`` probe would miss the fixture and
            # raise ``ReplayMiss``, falsely reporting the target as failed.
            # Report it healthy without a probe (capabilities come straight
            # from the fixture, no lazy probing needed).
            rows.append(
                TargetHealth(
                    name=entry.name,
                    type=entry.type,
                    enabled=entry.enabled,
                    connectivity="ok",
                    credential_source=credential_source,
                    capabilities=sorted(c.value for c in target.capabilities),
                    error_kind=None,
                )
            )
            continue
        enabled_entries.append((entry, target, credential_source))

    if enabled_entries:
        targets_only = [t for (_, t, _) in enabled_entries]
        probe_results = asyncio.run(_probe_enabled_targets(targets_only))
        for (entry, target, credential_source), (connectivity, error_kind) in zip(
            enabled_entries, probe_results, strict=True
        ):
            # Re-read capabilities AFTER the probe so the lazy ``which``
            # probe inside LocalTarget / SSHTarget has populated SYSTEMD
            # / DOCKER_CLI extras.
            rows.append(
                TargetHealth(
                    name=entry.name,
                    type=entry.type,
                    enabled=entry.enabled,
                    connectivity=connectivity,
                    credential_source=credential_source,
                    capabilities=sorted(c.value for c in target.capabilities),
                    error_kind=error_kind,
                )
            )
    return rows


async def _probe_enabled_targets(
    targets: list[ExecutionTarget],
) -> list[tuple[TargetConnectivity, str | None]]:
    """Probe a batch of enabled targets on one event loop.

    ``aclose`` runs in a ``finally`` so a probe raising still releases the
    underlying SSH control connection — ``SSHTarget.__del__`` would
    otherwise surface a ResourceWarning. Doctor is best-effort so close
    failures are suppressed (they must not pollute the strict-JSON output).
    """

    try:
        return await asyncio.gather(*[_probe_target(t) for t in targets])
    finally:
        for t in targets:
            aclose = getattr(t, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()


# ---------------------------------------------------------------------------
# Inspector registry health
# ---------------------------------------------------------------------------


def _count_builtin_manifests() -> int:
    """Count YAML manifests under the hardcoded builtin directory.

    Used by the duplicate_inspector failure path in ``_check_inspectors``
    where the builder raises **after** builtins are already registered —
    so the doctor row should still reflect the (partially) loaded count
    instead of reporting 0.
    """
    import hostlens.inspectors as _inspectors_pkg

    builtin_dir = Path(_inspectors_pkg.__file__).parent / "builtin"
    if not builtin_dir.is_dir():
        return 0
    return sum(1 for _ in builtin_dir.rglob("*.yaml"))


def _check_inspectors(settings: Settings) -> InspectorsHealth:
    """Inspector registry health probe for ``hostlens doctor``.

    Behaviour per spec §需求:`hostlens doctor` 必须新增 `inspectors` section:

    - Build the registry via ``build_registry_from_search_paths``. That
      function already collects per-file user-path errors into
      ``result.errors`` so doctor does **not** need its own try/except
      around individual manifest loads.
    - ``duplicate_inspector`` (and any other non-collectable kind) is
      raised by the builder; we catch ``InspectorError`` here and surface
      it as a synthetic ``InspectorLoadErrorRow`` with ``path=<duplicate>``
      so the JSON shape stays uniform and ``status="fail"`` flips the
      overall exit code via ``_is_ready``.
    - For every successfully-loaded manifest, walk ``manifest.secrets``
      and record each env-var name that is NOT in ``os.environ``. The
      value is **never** read — only existence is checked, matching the
      same security posture as ``check_anthropic_key``.
    - ``status`` follows the spec mapping: errors → ``fail``;
      missing_secrets only → ``warn``; both empty → ``ok``.
    """

    errors: list[InspectorLoadErrorRow] = []
    missing_secrets: list[InspectorMissingSecretRow] = []
    loaded = 0

    try:
        result = build_registry_from_search_paths(
            settings.inspectors_search_paths,
            settings=settings,
        )
    except InspectorError as exc:
        # Fatal error from the builder (typically ``duplicate_inspector``).
        # Convert it into a single failure row so the JSON contract stays
        # uniform and ``_is_ready`` can flip the exit code via
        # ``inspectors.status == "fail"``.
        synthetic_path: Path | None = exc.path or exc.new_path or exc.existing_path
        errors.append(
            InspectorLoadErrorRow(
                path=str(synthetic_path) if synthetic_path is not None else "<registry>",
                kind=exc.kind,
                detail=str(exc),
            )
        )
        # Builder raises in two cases (see registry.build_registry_from_search_paths):
        #   - duplicate_inspector on the user-path leg → builtins all registered
        #     before the duplicate fires; report disk count so JSON doesn't
        #     under-report what's actually available.
        #   - any other fatal kind (e.g. builtin manifest broken) → builder
        #     aborted while scanning builtins; the registry state is incomplete
        #     and nothing is usable. Report 0 — the JSON must not show a
        #     "healthy loaded count" when registry build failed entirely.
        loaded_count = _count_builtin_manifests() if exc.kind == "duplicate_inspector" else 0
        return InspectorsHealth(
            status="fail",
            loaded=loaded_count,
            errors=errors,
            missing_secrets=[],
        )

    for err in result.errors:
        errors.append(
            InspectorLoadErrorRow(
                path=str(err.path),
                kind=err.kind,
                detail=err.detail,
            )
        )

    manifests = result.registry.list()
    loaded = len(manifests)
    for manifest in manifests:
        for secret_name in manifest.secrets:
            if secret_name not in os.environ:
                missing_secrets.append(
                    InspectorMissingSecretRow(
                        inspector=manifest.name,
                        secret=secret_name,
                    )
                )

    if errors:
        status: Literal["ok", "warn", "fail"] = "fail"
    elif missing_secrets:
        status = "warn"
    else:
        status = "ok"

    return InspectorsHealth(
        status=status,
        loaded=loaded,
        errors=errors,
        missing_secrets=missing_secrets,
    )


# Readiness semantics per spec cli-foundation (M0) + execution-target (M1):
# - `python_version`: must be `ok` (interpreter floor is hard).
# - `anthropic_key` : `present` or `missing` both pass (spec: "缺失
#   ANTHROPIC_API_KEY 不阻塞"); only `error` fails.
# - `config_dir`    : `ok` or `missing` both pass (M0 only probes; a
#   non-existent dir is fine because `hostlens` writes nothing there
#   yet). `unreadable` / `error` fail (spec explicitly requires exit 1
#   for the unreadable case).
# - `targets`       : every enabled target's connectivity must not be
#   `failed`; `inline_plaintext` credential_source emits a warning but
#   does NOT block (spec §场景:doctor 检测明文密码 warn).
# - `inspectors`    : ``status == "fail"`` flips exit 1; ``warn`` /
#   ``ok`` both pass (spec §场景:secret 缺失 status=warn doctor exit 0).


def _is_ready(
    checks: dict[str, CheckResult],
    targets: list[TargetHealth],
    inspectors: InspectorsHealth,
) -> bool:
    py = checks["python_version"].status
    cfg = checks["config_dir"].status
    key = checks["anthropic_key"].status
    if not (py == "ok" and cfg in {"ok", "missing"} and key in {"present", "missing"}):
        return False
    # A malformed schedule manifest (loader fail-loud → status "error") is a
    # real misconfiguration that must not silently pass — mirror the
    # targets / inspectors fail-loud posture and flip exit 1.
    if checks["schedules"].status == "error":
        return False
    # ``channels`` is only present when ``--check-channels`` was passed; a
    # failed probe (invalid token / missing env var / malformed file) is a
    # real misconfiguration that flips exit 1, mirroring targets / schedules.
    channels = checks.get("channels")
    if channels is not None and channels.status == "error":
        return False
    # Any target failing connectivity flips the whole doctor to exit 1
    # (spec §场景:某 target 连通失败 doctor exit 1).
    if any(row.connectivity == "failed" for row in targets):
        return False
    # Inspector load failures flip exit 1 too — silent skip is forbidden
    # so attackers can't plant a same-named manifest in the user path and
    # have the operator miss the failure.
    return inspectors.status != "fail"


# ---------------------------------------------------------------------------
# M2 (`add-llm-backend-protocol`): backend health probe
# ---------------------------------------------------------------------------


_BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS = 10.0
"""Fallback health-check timeout used when ``settings.agent is None``.

The configured timeout lives in ``settings.agent.health_check_timeout_seconds``;
``_check_backend`` reads that when ``settings.agent`` is present. This constant
is the fallback for M0/M1 configs that ship no ``agent`` block. Its value MUST
match the field default (a drift test pins them equal); held as a literal float
rather than ``model_fields[...].default`` because ``FieldInfo.default: Any``
would leak ``Any`` under ``mypy --strict``."""


def _check_backend(settings: Settings) -> BackendHealthRow | None:
    """Build the optional ``BackendHealthRow`` for ``DoctorReport.backend``.

    Returns ``None`` so ``DoctorReport.backend`` is set to ``null`` in the
    rendered JSON when ``settings.backend is None`` (M0 / M1 configs).
    Otherwise:

    - ``type`` mirrors ``settings.backend.type``.
    - ``api_key_set`` is computed by membership; the fingerprint is computed
      via ``api_key_fingerprint(get_secret_value(...))`` and never leaks the
      full value (the helper returns ``"<unset>"`` / ``"<redacted>"`` /
      ``"<first4>...<last4>"``).
    - Health check fires only when the backend exposes
      ``BackendDiagnostics`` (duck-typed). Construction failures
      (``ConfigError`` / ``NotImplementedError`` / ``FileNotFoundError`` /
      ``ValueError`` — the latter two cover ``PlaybackBackend`` cassette
      load errors) leave the health fields ``None`` and surface the error
      via ``health_check_error`` (passed through ``redact_text``) so doctor
      stays exit-0 for "schema valid, backend not bootable" configs (e.g.
      ``type=bedrock`` or a missing cassette path) — those are not local
      readiness failures, they're "deferred" or "operator misconfig" markers.
    """

    if settings.backend is None:
        return None

    # api_key surface bits — always populated, never carry the raw value.
    api_key = (
        settings.backend.api_key.get_secret_value()
        if settings.backend.api_key is not None
        else None
    )
    fingerprint = api_key_fingerprint(api_key)

    row = BackendHealthRow(
        type=settings.backend.type,
        api_key_set=settings.backend.api_key is not None,
        api_key_fingerprint=fingerprint,
    )

    # Try to construct the backend — when this raises we still emit the
    # row with type / api_key surface intact. We do NOT propagate the
    # construction error to ``ready`` because (a) ``NotImplementedError``
    # for bedrock / vertex / claude_subscription means "M2 deferred", not
    # local broken; (b) ``ConfigError`` here would have already fired in
    # ``load_settings()`` so reaching this branch means the user wired up
    # something the factory still rejects (e.g. mutated post-construction).
    try:
        backend = create_backend(settings)
    except (ConfigError, NotImplementedError, FileNotFoundError, ValueError) as exc:
        # ``FileNotFoundError`` / ``ValueError`` cover ``PlaybackBackend``
        # cassette-load failures (missing file / invalid JSON) — those
        # construct-time exceptions would otherwise crash doctor instead of
        # producing a diagnostic row. We deliberately list each exception
        # class rather than ``except Exception`` so genuine programmer bugs
        # still propagate. The message is run through ``redact_text`` so an
        # exception that happens to embed a token-shaped path / value cannot
        # leak via doctor output.
        row.health_check_error = redact_text(str(exc))
        return row

    if not isinstance(backend, BackendDiagnostics):
        # Fake / Playback backends opt out of BackendDiagnostics; doctor
        # surfaces the type + api_key bits but skips the ping.
        return row

    # Run health_check with a hard timeout so a hung backend does not
    # block doctor for tens of seconds. The configured timeout is read once
    # into a local so the ``wait_for`` ceiling and the error-message f-string
    # render the SAME value — otherwise the message could claim the fallback
    # while the wait used the configured value.
    effective_timeout = (
        settings.agent.health_check_timeout_seconds
        if settings.agent is not None
        else _BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS
    )
    try:
        health = asyncio.run(
            asyncio.wait_for(
                backend.health_check(),
                timeout=effective_timeout,
            )
        )
    except TimeoutError:
        row.health_check_is_healthy = False
        row.health_check_error = f"health_check timeout after {effective_timeout}s"
        return row
    except Exception as exc:  # pragma: no cover — defensive
        # Backend health_check should never raise (the contract is to
        # return BackendHealth with is_healthy=False on failure); if it
        # does we treat it like a timeout-shaped failure.
        row.health_check_is_healthy = False
        row.health_check_error = f"health_check raised {type(exc).__name__}"
        return row

    row.health_check_is_healthy = health.is_healthy
    row.health_check_latency_ms = health.latency_ms
    # ``BackendHealth.error`` already went through ``redact_text`` at the
    # backend layer; doctor does NOT re-redact (the backend is the
    # canonical scrubber and double-redacting could destroy useful
    # operator hints).
    row.health_check_error = health.error
    return row


# ---------------------------------------------------------------------------
# M4 (`add-scheduler`): schedule health check (design D-10, add-only)
# ---------------------------------------------------------------------------


_SCHEDULES_DIR_DEFAULT = Path("schedules")
"""Directory doctor scans for ``schedules/*.yaml`` manifests.

cwd-relative, matching the proposal Demo Path (``cat > schedules/...``).
Resolved at call time (not import) so tests can ``monkeypatch.chdir``."""

_RECENT_RUNS_LIMIT = 20
"""How many recent ``Run`` rows doctor folds into the status distribution."""


def _next_fire_time_computable(manifest: ScheduleManifest) -> bool:
    """Whether the manifest's trigger yields a next fire time from ``now``.

    Built straight from the validated ``ScheduleSpec`` (no scheduler /
    runner instance) to stay decoupled from the APScheduler runner that is
    developed in parallel. The schema already validated cron field count /
    timezone, so this only confirms the trigger projects to a future fire.
    """

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = ZoneInfo(manifest.schedule.timezone)
    now = datetime.now(tz)
    spec = manifest.schedule
    if spec.cron is not None:
        trigger: CronTrigger | IntervalTrigger = CronTrigger.from_crontab(spec.cron, timezone=tz)
    else:
        # _exactly_one_trigger guarantees interval is set when cron is None.
        interval = spec.interval
        assert interval is not None
        trigger = IntervalTrigger(
            weeks=interval.weeks,
            days=interval.days,
            hours=interval.hours,
            minutes=interval.minutes,
            seconds=interval.seconds,
            timezone=tz,
        )
    return trigger.get_next_fire_time(None, now) is not None


def _check_schedules(settings: Settings) -> CheckResult:
    """Schedule subsystem health for ``hostlens doctor`` (design D-10).

    Lands under ``DoctorReport.checks["schedules"]`` (a new check_id in the
    existing ``checks`` namespace — **not** a new top-level field), keeping
    the closed cli-foundation ``--json`` schema intact (add-only policy).

    Behaviour:

    - No ``schedules/`` directory → ``status="ok"`` with a "no schedules
      configured" detail (an absent directory is a valid empty state, not
      a failure).
    - ``load_schedules`` raising ``ConfigError`` (malformed / unregistered
      target / duplicate name / blank intent) → ``status="error"`` carrying
      the loader's ``kind`` + file + reason in ``detail``. doctor itself
      MUST NOT crash on a bad manifest.
    - All manifests valid → ``status="ok"`` with a compact ``detail``:
      manifest count, how many have a computable next fire time, and the
      recent-Run status distribution from ``RunStore``.

    ``detail`` is the add-only carrier (no new ``CheckResult`` field) so the
    per-check schema stays unchanged across all checks.
    """

    schedules_dir = _SCHEDULES_DIR_DEFAULT
    if not schedules_dir.is_dir():
        return CheckResult(status="ok", detail="no schedules configured")

    # The loader validates ``targets`` membership against the registry, so
    # doctor must hand it the same registry ``_check_targets`` probes. A
    # malformed targets.yaml surfaces as a schedule load error too (the
    # loader cannot validate target membership without a registry).
    try:
        # Skip the loader call entirely when targets.yaml is absent — it
        # would log a structlog INFO to stdout and corrupt strict-JSON
        # output (same rationale as ``_check_targets``). An empty registry
        # is the correct input: any manifest then fails target-membership
        # validation and surfaces as a load error below.
        config = (
            load_targets_config(settings.targets_config_path)
            if settings.targets_config_path.exists()
            else TargetsConfig(version="1", targets=[])
        )
        registry = build_registry_from_config(config, settings)
        manifests = load_schedules(schedules_dir, registry)
    except (ConfigError, TargetError, ValidationError, yaml.YAMLError) as exc:
        kind = getattr(exc, "kind", type(exc).__name__)
        return CheckResult(status="error", detail=f"{kind}: {exc}")

    if not manifests:
        # No manifests configured: report the empty-but-valid state without
        # opening RunStore (which would otherwise create the real runs.db as
        # a side effect when no schedule subsystem is actually in use).
        return CheckResult(status="ok", detail="manifests=0")

    computable = sum(1 for m in manifests if _next_fire_time_computable(m))

    runs = asyncio.run(RunStore().list_recent(limit=_RECENT_RUNS_LIMIT))
    status_counts: dict[str, int] = {}
    for run in runs:
        key = str(run.status)
        status_counts[key] = status_counts.get(key, 0) + 1
    if status_counts:
        dist = " ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
    else:
        dist = "none"

    detail = (
        f"manifests={len(manifests)} "
        f"next_fire_time_ok={computable}/{len(manifests)} "
        f"recent_runs={len(runs)} status_counts=[{dist}]"
    )
    return CheckResult(status="ok", detail=detail)


def _check_channels(settings: Settings) -> CheckResult:
    """Notifier-channel connectivity / config probe for ``doctor --check-channels``.

    Lands under ``DoctorReport.checks["channels"]`` (a new check_id in the
    existing ``checks`` namespace — same add-only carrier as ``schedules``),
    so the closed cli-foundation ``--json`` schema is untouched (no new field,
    no version bump). Only emitted when ``--check-channels`` is passed.

    Behaviour (spec §需求:`doctor --check-channels`):

    - No ``notifiers.yaml`` → ``status="ok"`` with a "no channels configured"
      detail (an absent file is a valid empty state).
    - A malformed file / unknown type / missing env var / failed
      ``validate_config`` (``ConfigError`` from ``load_channels``) →
      ``status="error"`` carrying the loader's ``kind`` + reason. doctor MUST
      NOT crash on a bad file.
    - Per channel: Telegram gets a read-only ``getMe`` probe; Lark validates
      config completeness only (no business message sent). Any channel
      probing failed → ``status="error"`` with a compact per-channel summary
      in ``detail``; all healthy → ``status="ok"``.

    ``detail`` is the add-only carrier (no new ``CheckResult`` field) so every
    channel's pass/fail + reason stays machine-greppable without changing the
    per-check schema. Secrets never reach ``detail`` (the Telegram probe
    scrubs the token; Lark never echoes the webhook).
    """

    from hostlens.cli.notify import _probe_telegram
    from hostlens.notifiers.base import ChannelTypeRegistry, register_default_notifiers
    from hostlens.notifiers.config import load_channels

    if not settings.notifiers_config_path.exists():
        return CheckResult(status="ok", detail="no channels configured")

    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)
    try:
        channels = load_channels(settings, registry)
    except ConfigError as exc:
        kind = getattr(exc, "kind", type(exc).__name__)
        return CheckResult(status="error", detail=f"{kind}: {exc}")

    if not channels:
        return CheckResult(status="ok", detail="channels=0")

    # ``load_channels`` already env-expanded each config and ran
    # ``validate_config``; reaching here means config is structurally sound.
    # The per-type probe re-reads the now-resolved config off each adapter.
    parts: list[str] = []
    any_failed = False
    for name, notifier in sorted(channels.items()):
        ctype = notifier.name
        if ctype == "telegram":
            config = getattr(notifier, "_config", {})
            ok, reason = _probe_telegram(config)
            if ok:
                parts.append(f"{name}=ok")
            else:
                any_failed = True
                parts.append(f"{name}=failed({reason})")
        else:
            # Lark (and any future config-only type): config completeness was
            # already validated by load_channels; no business message sent.
            parts.append(f"{name}=ok(config)")

    status: Literal["ok", "error"] = "error" if any_failed else "ok"
    return CheckResult(status=status, detail=" ".join(parts))


def check_remediation() -> CheckResult:
    """Remediation readiness for ``hostlens fix`` (M9 P2) — **non-fatal**.

    Lands under ``DoctorReport.checks["remediation"]`` (a new check_id in the
    existing ``checks`` namespace — add-only, no schema bump). Probes the two
    write-path preconditions for ``hostlens fix``:

    - the ``audit.log`` directory is creatable + writable (the executor's
      ``precheck_writable`` gate), and
    - the current process is not ``EUID==0`` (``hostlens fix`` refuses root).

    Deliberately **non-fatal**: ``_is_ready`` does NOT inspect this check, so a
    not-yet-writable audit dir or a root doctor run never flips ``ready`` to
    false. doctor reports the condition (``status`` + ``detail``) so the
    operator can fix it before the first real ``hostlens fix``, but a fresh
    install with no remediation activity stays exit 0.
    """

    is_root = os.geteuid() == 0
    audit_dir = default_audit_path().parent
    dir_str = str(audit_dir)

    writable: bool
    detail_reason: str
    if audit_dir.exists():
        if not audit_dir.is_dir():
            writable = False
            detail_reason = "audit path exists but is not a directory"
        elif os.access(audit_dir, os.W_OK):
            writable = True
            detail_reason = "audit dir writable"
        else:
            writable = False
            detail_reason = "audit dir not writable"
    else:
        # Absent dir is fine — ``precheck_writable`` creates it on first run.
        # We only confirm the parent is creatable (writable parent).
        parent = audit_dir.parent
        if parent.exists() and os.access(parent, os.W_OK):
            writable = True
            detail_reason = "audit dir absent (will be created on first run)"
        else:
            writable = False
            detail_reason = "audit dir absent and parent not writable"

    root_note = "running as root (hostlens fix will refuse)" if is_root else "non-root"
    detail = f"{detail_reason}; {root_note}"
    status: Literal["ok", "error"] = "ok" if (writable and not is_root) else "error"
    return CheckResult(status=status, detail=detail, path=dir_str)


def _build_report(settings: Settings, *, check_channels: bool = False) -> DoctorReport:
    checks: dict[str, CheckResult] = {
        "python_version": check_python_version(),
        "anthropic_key": check_anthropic_key(),
        "config_dir": check_config_dir(),
        "mcp": check_mcp(),
        "schedules": _check_schedules(settings),
        "remediation": check_remediation(),
    }
    if check_channels:
        checks["channels"] = _check_channels(settings)
    targets = _check_targets(settings)
    inspectors = _check_inspectors(settings)
    backend_row = _check_backend(settings)
    return DoctorReport(
        version="0.1.0",
        timestamp=datetime.now(UTC),
        checks=checks,
        ready=_is_ready(checks, targets, inspectors),
        targets=targets,
        inspectors=inspectors,
        backend=backend_row,
    )


def _render_human(report: DoctorReport, console: Console) -> None:
    table = Table(title="hostlens doctor")
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail")
    for name, result in report.checks.items():
        detail_parts: list[str] = []
        if result.detail is not None:
            detail_parts.append(result.detail)
        if result.path is not None:
            detail_parts.append(f"path={result.path}")
        table.add_row(name, result.status, " ".join(detail_parts))
    console.print(table)

    if report.targets:
        ttable = Table(title="targets")
        ttable.add_column("name", no_wrap=True)
        ttable.add_column("type", no_wrap=True)
        ttable.add_column("enabled", no_wrap=True)
        ttable.add_column("connectivity", no_wrap=True)
        ttable.add_column("credential_source", no_wrap=True)
        ttable.add_column("capabilities")
        ttable.add_column("error_kind", no_wrap=True)
        for row in report.targets:
            ttable.add_row(
                row.name,
                row.type,
                str(row.enabled),
                row.connectivity,
                row.credential_source,
                ", ".join(row.capabilities),
                row.error_kind or "",
            )
        console.print(ttable)
    else:
        console.print(
            "no targets configured; run `hostlens target add` to start.",
        )

    # Inspector registry summary is always rendered (even on status=ok)
    # so operators see ``loaded`` count and can spot a missing builtin
    # at a glance.
    itable = Table(title="inspectors")
    itable.add_column("field", no_wrap=True)
    itable.add_column("value")
    itable.add_row("status", report.inspectors.status)
    itable.add_row("loaded", str(report.inspectors.loaded))
    itable.add_row("errors", str(len(report.inspectors.errors)))
    itable.add_row("missing_secrets", str(len(report.inspectors.missing_secrets)))
    console.print(itable)

    # Backend summary — only when configured. Lays out a 2-column table
    # so the api_key fingerprint stays visible without trailing whitespace
    # in the Rich output.
    if report.backend is not None:
        btable = Table(title="backend")
        btable.add_column("field", no_wrap=True)
        btable.add_column("value")
        btable.add_row("type", report.backend.type)
        btable.add_row("api_key_set", str(report.backend.api_key_set))
        btable.add_row("api_key_fingerprint", report.backend.api_key_fingerprint or "")
        if report.backend.health_check_is_healthy is not None:
            btable.add_row("health_check_is_healthy", str(report.backend.health_check_is_healthy))
        if report.backend.health_check_latency_ms is not None:
            btable.add_row(
                "health_check_latency_ms",
                f"{report.backend.health_check_latency_ms:.1f}",
            )
        if report.backend.health_check_error is not None:
            btable.add_row("health_check_error", report.backend.health_check_error)
        console.print(btable)

    console.print(f"ready: {report.ready}")


def _emit_remediation(report: DoctorReport, stderr: Console) -> None:
    """Print fix hints to stderr for actionable failures."""

    cfg = report.checks["config_dir"]
    if cfg.status == "unreadable":
        path = cfg.path or str(_CONFIG_DIR_DEFAULT)
        stderr.print(
            f"hint: config directory is not readable; try `chmod 755 {path}`",
        )
    elif cfg.status == "error":
        path = cfg.path or str(_CONFIG_DIR_DEFAULT)
        stderr.print(
            f"hint: {path} exists but is not a directory; remove or replace it",
        )

    py = report.checks["python_version"]
    if py.status == "error":
        stderr.print(
            "hint: hostlens requires Python >=3.11; upgrade your interpreter",
        )

    channels = report.checks.get("channels")
    if channels is not None and channels.status == "error":
        stderr.print(
            f"hint: notifier channel probe failed ({channels.detail}); check "
            "notifiers.yaml types / ${VAR} env vars / bot tokens",
        )

    mcp = report.checks.get("mcp")
    if mcp is not None and mcp.status == "missing":
        # markup=False so Rich does not parse the `[mcp]` extra as a markup tag.
        stderr.print(
            'hint: MCP SDK not installed; run pip install "hostlens[mcp]"',
            markup=False,
        )

    # M1: warn (not exit 1) for inline plaintext credentials.
    for row in report.targets:
        if row.credential_source == "inline_plaintext":
            stderr.print(
                f"warning: target {row.name!r} stores credentials inline in "
                "targets.yaml; replace with ${VAR} placeholder + env var",
            )
        if row.connectivity == "failed":
            kind = row.error_kind or "unknown"
            stderr.print(
                f"hint: target {row.name!r} connectivity failed (kind={kind})",
            )

    # Per-inspector load errors and missing secrets. ``errors`` flips
    # exit 1 via ``_is_ready`` so we also print remediation hints for
    # each failed manifest; ``missing_secrets`` stays warn-only.
    for err_row in report.inspectors.errors:
        stderr.print(
            f"hint: inspector load error: {err_row.path}: {err_row.kind}: {err_row.detail}",
        )
    for secret_row in report.inspectors.missing_secrets:
        stderr.print(
            f"warning: inspector {secret_row.inspector!r} declares secret "
            f"{secret_row.secret!r} but the env var is not set",
        )


def run_doctor(json_output: bool, *, check_channels: bool = False) -> int:
    """Run all checks, emit output, return process exit code.

    Wires core/config + core/logging into the CLI entrypoint so that
    `HOSTLENS_LOG_MODE` / `HOSTLENS_LOG_LEVEL` take effect for any
    structlog calls made during checks (and from M1+ checkers that may
    emit diagnostics). `load_settings()` raises `ConfigError` on invalid
    user config; we let that propagate so the user sees the validated
    error with sensitive-field redaction (see core/config.py).
    """

    settings = load_settings()
    configure_logging(settings.log_mode)

    report = _build_report(settings, check_channels=check_channels)
    stdout = Console(highlight=False, soft_wrap=True)
    stderr = Console(stderr=True, highlight=False, soft_wrap=True)

    if json_output:
        # Strict JSON to stdout only; nothing else may interleave.
        sys.stdout.write(report.model_dump_json(indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        _render_human(report, stdout)

    _emit_remediation(report, stderr)
    return 0 if report.ready else 1
