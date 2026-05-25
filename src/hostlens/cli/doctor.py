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
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# ``PyYAML`` ships no PEP 561 marker — see CLI target.py for the same
# rationale. We only need raw yaml parsing here (no schema validation)
# to detect `${VAR}` placeholders that the loader would otherwise have
# expanded into real secret values by the time it returns.
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from hostlens.cli._doctor_schema import (
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
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import TargetEntry, load_targets_config
from hostlens.targets.registry import build_registry_from_config

__all__ = [
    "check_anthropic_key",
    "check_config_dir",
    "check_python_version",
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
    # Any target failing connectivity flips the whole doctor to exit 1
    # (spec §场景:某 target 连通失败 doctor exit 1).
    if any(row.connectivity == "failed" for row in targets):
        return False
    # Inspector load failures flip exit 1 too — silent skip is forbidden
    # so attackers can't plant a same-named manifest in the user path and
    # have the operator miss the failure.
    return inspectors.status != "fail"


def _build_report(settings: Settings) -> DoctorReport:
    checks: dict[str, CheckResult] = {
        "python_version": check_python_version(),
        "anthropic_key": check_anthropic_key(),
        "config_dir": check_config_dir(),
    }
    targets = _check_targets(settings)
    inspectors = _check_inspectors(settings)
    return DoctorReport(
        version="0.1.0",
        timestamp=datetime.now(UTC),
        checks=checks,
        ready=_is_ready(checks, targets, inspectors),
        targets=targets,
        inspectors=inspectors,
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


def run_doctor(json_output: bool) -> int:
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

    report = _build_report(settings)
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
