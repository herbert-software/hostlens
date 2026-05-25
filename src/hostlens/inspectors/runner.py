"""InspectorRunner — orchestrates one Inspector run on one target.

The runner is the M1 sole entry point that translates an
``InspectorManifest`` + an ``ExecutionTarget`` into a deterministic
``InspectorResult`` (one of the five closed-set ``InspectorStatus`` values).

Design contract — per-call-site exception scoping (design.md Decision 7):

  * Every business call site has a **precise** ``except`` list. The runner
    never wraps the orchestrator body in ``except Exception`` or in a
    blanket ``except (AttributeError, KeyError, TypeError)`` — those would
    swallow runner-internal bugs and silently coerce them into
    ``status="exception"``. The only allowed ``except KeyError`` /
    ``except AttributeError`` lives inside ``_evaluate_findings`` around
    the ``format_message`` call (per spec, manifest authors hitting a
    missing variable should skip the single finding, not crash the run).

  * ``ValueError`` is raised by ``run`` itself only for caller programming
    errors (``manifest is None`` / ``target is None``). Every other failure
    becomes an ``InspectorResult`` status.

Log redaction (spec §需求: runner 日志脱敏): the runner emits exactly two
structlog events — ``inspector_started`` and ``inspector_finished`` — and
the field set is a closed set (name / version / target / status / duration
/ findings_count / stdout_length / stderr_length). ``parameters`` /
``output`` / ``secrets_env`` are **never** logged because any of them may
contain user secrets that have not been declared as such.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from typing import Any, Literal

import jinja2
import jsonschema
import simpleeval
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError, TargetError
from hostlens.inspectors import dsl
from hostlens.inspectors.parsers import parse_json, parse_kv, parse_raw, parse_table
from hostlens.inspectors.result import Finding, InspectorResult
from hostlens.inspectors.schema import (
    FindingRule,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.registry import TargetRegistry

__all__ = ["InspectorRunner"]


# --------------------------------------------------------------------------- #
# Precise exception sets — kept as module-level tuples so the precise-except
# contract is grep-friendly: `grep "except (" runner.py` enumerates every
# capture point and `grep "except Exception" runner.py` must remain empty.
# --------------------------------------------------------------------------- #

# DSL ``evaluate`` call-site — simpleeval 1.0+ surfaces every business
# failure as a subclass of ``InvalidExpression`` (``FeatureNotAvailable``,
# ``NameNotDefined``, ``NumberTooHigh``, ``IterableTooLong``,
# ``AttributeDoesNotExist``, ``FunctionNotDefined``, ``OperatorNotDefined``).
# We list ``InvalidExpression`` plus the well-known leaf classes explicitly so
# a future simpleeval refactor that flattens the hierarchy still keeps the
# documented capture set intact. Plus ``asyncio.TimeoutError`` from the soft
# fallback. Anything outside this tuple (e.g. ``AttributeError``) propagates
# as a runner bug per spec §需求:`InspectorRunner.run` 必须永远返回...
_DSL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    simpleeval.InvalidExpression,
    simpleeval.FeatureNotAvailable,
    simpleeval.NameNotDefined,
    simpleeval.NumberTooHigh,
    simpleeval.IterableTooLong,
    asyncio.TimeoutError,
)

# ``format_message`` call-site — the only place in the runner that is
# permitted to catch ``KeyError`` / ``AttributeError`` / ``IndexError``.
# Manifests legitimately reference user variables that may not be bound
# at runtime; that's a per-rule skip, not a runner failure.
_FORMAT_MESSAGE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyError,
    IndexError,
    AttributeError,
)


class InspectorRunner:
    """Orchestrator for running a single Inspector against a single target.

    Construction is pure — no IO, no subprocess. ``__init__`` only wires
    dependencies. See ``run`` for the actual execution flow.
    """

    def __init__(
        self,
        target_registry: TargetRegistry,
        *,
        settings: Settings,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._target_registry = target_registry
        self._settings = settings
        self._logger = logger

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    async def run(
        self,
        manifest: InspectorManifest,
        target: ExecutionTarget,
        parameters: dict[str, Any] | None = None,
        *,
        allow_privileged: bool = False,
        cancel: asyncio.Event | None = None,
    ) -> InspectorResult:
        """Run ``manifest`` against ``target`` and return an ``InspectorResult``.

        Caller programming errors (``manifest is None`` / ``target is
        None``) raise ``ValueError`` — they are not business failures.
        Every other failure path collapses into one of the five
        ``InspectorStatus`` values without exception propagation.
        """

        del cancel  # M1 runner does not yet check cooperative cancellation
        if manifest is None:
            raise ValueError("manifest must not be None")
        if target is None:
            raise ValueError("target must not be None")

        start = time.monotonic()
        self._logger.info(
            "inspector_started",
            inspector_name=manifest.name,
            inspector_version=manifest.version,
            target_name=target.name,
        )

        # ---- Steps 1-6: preflight ------------------------------------ #
        status, missing = await self._preflight(
            manifest, target, allow_privileged=allow_privileged
        )
        if status == "requires_unmet":
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="requires_unmet",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=None,
                    missing=missing,
                ),
            )

        # ---- Step 7: render command + collect secrets env ------------ #
        try:
            cmd, secrets_env = await self._render_command(manifest, parameters)
        except (jinja2.UndefinedError, jinja2.TemplateError) as exc:
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="exception",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=f"render_failed: {exc}",
                    missing=[],
                ),
            )

        # ---- Step 8: exec via target --------------------------------- #
        try:
            exec_result = await target.exec(
                cmd,
                timeout=manifest.collect.timeout_seconds,
                env=secrets_env or None,
            )
        except TargetError as exc:
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="target_unreachable",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=exc.kind,
                    missing=[],
                ),
            )

        if exec_result.timed_out:
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="timeout",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=None,
                    missing=[],
                ),
                stdout_length=len(exec_result.stdout),
                stderr_length=len(exec_result.stderr),
            )

        # ---- Steps 9-10: parse + jsonschema validate ----------------- #
        try:
            output = self._parse_and_validate(
                exec_result.stdout, manifest.parse, manifest.output_schema
            )
        except (json.JSONDecodeError, InspectorError) as exc:
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="exception",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=f"parse_failed: {exc}",
                    missing=[],
                ),
                stdout_length=len(exec_result.stdout),
                stderr_length=len(exec_result.stderr),
            )
        except jsonschema.ValidationError as exc:
            return self._finish(
                manifest=manifest,
                target=target,
                start=start,
                result=InspectorResult(
                    name=manifest.name,
                    version=manifest.version,
                    status="exception",
                    target_name=target.name,
                    duration_seconds=time.monotonic() - start,
                    output={},
                    findings=[],
                    error=f"output_schema_mismatch: {exc.message}",
                    missing=[],
                ),
                stdout_length=len(exec_result.stdout),
                stderr_length=len(exec_result.stderr),
            )

        # ---- Step 11: evaluate findings ------------------------------ #
        findings = await self._evaluate_findings(
            manifest.findings, output, parameters
        )

        return self._finish(
            manifest=manifest,
            target=target,
            start=start,
            result=InspectorResult(
                name=manifest.name,
                version=manifest.version,
                status="ok",
                target_name=target.name,
                duration_seconds=time.monotonic() - start,
                output=output,
                findings=findings,
                error=None,
                missing=[],
            ),
            stdout_length=len(exec_result.stdout),
            stderr_length=len(exec_result.stderr),
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _preflight(
        self,
        manifest: InspectorManifest,
        target: ExecutionTarget,
        *,
        allow_privileged: bool,
    ) -> tuple[Literal["ok", "requires_unmet"], list[str]]:
        """Run the 6-step preflight in fixed order.

        The order is contractual (spec §需求:`InspectorRunner` 求值顺序必须
        固定): cheaper / earlier failures must surface before more expensive
        probes. For example, when a manifest needs both an unsupported
        capability and a missing binary, the missing capability must be
        reported (step 2) — we never probe the binary (step 5) in that
        case.

        ``shlex.quote`` wraps every binary name / file path before string
        substitution into the probe command. The field-level regex already
        restricts the character set at manifest load time; ``shlex.quote``
        here is the documented defense-in-depth second gate (per spec
        §场景:shlex.quote 防御验证).
        """

        # Step 1: target type compatibility ---------------------------- #
        if target.type not in manifest.targets:
            return "requires_unmet", ["target_type"]

        # Step 2: capabilities ---------------------------------------- #
        required_caps = set(manifest.requires_capabilities)
        present_caps = {cap.value for cap in target.capabilities}
        missing_caps = required_caps - present_caps
        if missing_caps:
            return "requires_unmet", sorted(missing_caps)

        # Step 3: privilege opt-in ------------------------------------ #
        if manifest.privilege != "none" and not allow_privileged:
            return "requires_unmet", ["privilege_opt_in"]

        # Step 4: env secrets ----------------------------------------- #
        missing_secrets = [
            f"env:{name}"
            for name in manifest.secrets
            if name not in os.environ
        ]
        if missing_secrets:
            return "requires_unmet", missing_secrets

        # Step 5: binary probes (parallel) ---------------------------- #
        if manifest.requires_binaries:
            probe_results = await asyncio.gather(
                *(
                    target.exec(
                        f"command -v {shlex.quote(binary)}",
                        timeout=10,
                    )
                    for binary in manifest.requires_binaries
                )
            )
            missing_bins = [
                f"bin:{binary}"
                for binary, exec_result in zip(
                    manifest.requires_binaries, probe_results, strict=True
                )
                if exec_result.exit_code != 0
            ]
            if missing_bins:
                return "requires_unmet", missing_bins

        # Step 6: file readability probes (parallel) ------------------ #
        if manifest.requires_files:
            file_probes = await asyncio.gather(
                *(
                    target.exec(
                        f"[ -r {shlex.quote(path)} ]",
                        timeout=5,
                    )
                    for path in manifest.requires_files
                )
            )
            missing_files = [
                f"file:{path}"
                for path, exec_result in zip(
                    manifest.requires_files, file_probes, strict=True
                )
                if exec_result.exit_code != 0
            ]
            if missing_files:
                return "requires_unmet", missing_files

        return "ok", []

    async def _render_command(
        self,
        manifest: InspectorManifest,
        parameters: dict[str, Any] | None,
    ) -> tuple[str, dict[str, str]]:
        """Render ``manifest.collect.command`` via Jinja2 and collect secrets env.

        The ``sh`` filter is registered on a fresh environment and maps to
        ``shlex.quote(str(value))``. ``autoescape`` is disabled because
        we render shell commands, not HTML — ``html_escape`` would silently
        corrupt user payloads and provide zero shell-injection protection.

        Preflight has already verified every declared secret is in
        ``os.environ``; this method just reads them.
        """

        env = jinja2.Environment(
            autoescape=False,
            undefined=jinja2.StrictUndefined,
        )
        env.filters["sh"] = _sh_filter
        template = env.from_string(manifest.collect.command)
        rendered = template.render(**(parameters or {}))

        secrets_env: dict[str, str] = {
            name: os.environ[name] for name in manifest.secrets
        }
        return rendered, secrets_env

    def _parse_and_validate(
        self,
        stdout: str,
        parse_spec: ParseSpec,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch on ``parse_spec.format`` then jsonschema-validate.

        Parser failures (``InspectorError(parse_json_not_object)`` /
        ``json.JSONDecodeError``) and ``jsonschema.ValidationError``
        propagate to the caller's narrow ``except`` blocks.
        """

        fmt = parse_spec.format
        parsed: dict[str, Any]
        if fmt == "raw":
            parsed = parse_raw(stdout, parse_spec)
        elif fmt == "table":
            parsed = parse_table(stdout, parse_spec)
        elif fmt == "json":
            parsed = parse_json(stdout, parse_spec)
        elif fmt == "kv":
            parsed = dict(parse_kv(stdout, parse_spec))
        else:
            # Defensive — ParseSpec.Literal already constrains the value at
            # load time; reaching this branch is a runner bug, not a
            # business failure, so we let ``ValueError`` propagate.
            raise ValueError(f"unknown parse format: {fmt!r}")

        jsonschema.validate(parsed, output_schema)
        return parsed

    async def _evaluate_findings(
        self,
        findings: list[FindingRule],
        output: dict[str, Any],
        parameters: dict[str, Any] | None,
    ) -> list[Finding]:
        """Evaluate each FindingRule in manifest order.

        Per-rule failures (DSL evaluation errors or ``format_message``
        KeyError/AttributeError) skip the offending rule with a structured
        warning log, but the overall inspector result remains ``ok``. The
        ``except`` lists are precise — every other exception propagates as
        a runner bug.
        """

        context: dict[str, Any] = {**output, **(parameters or {})}
        out: list[Finding] = []

        for index, rule in enumerate(findings):
            if rule.for_each is not None:
                await self._evaluate_iterative_rule(
                    rule, index, context, out
                )
            else:
                await self._evaluate_aggregate_rule(
                    rule, index, context, out
                )

        return out

    async def _evaluate_iterative_rule(
        self,
        rule: FindingRule,
        index: int,
        context: dict[str, Any],
        out: list[Finding],
    ) -> None:
        """Evaluate a single ``for_each`` rule, appending zero or more findings."""

        # ``parse_for_each`` is run at loader time too, but we re-invoke
        # here so the runner doesn't depend on private parse state. If
        # the loader contract is violated (for_each malformed) the runner
        # surfaces the same ``InspectorError`` — caller catches it as
        # part of the findings-evaluation ladder.
        try:
            iterable_expr, var_name = dsl.parse_for_each(
                rule.for_each if rule.for_each is not None else ""
            )
        except InspectorError as exc:
            self._logger.warning(
                "inspector.finding.skipped",
                index=index,
                reason="for_each_invalid",
                error=str(exc),
            )
            return

        try:
            iterable = await dsl.evaluate(iterable_expr, context)
        except _DSL_EXCEPTIONS as exc:
            self._logger.warning(
                "inspector.finding.skipped",
                index=index,
                reason="for_each_evaluate_failed",
                error=str(exc),
            )
            return

        try:
            iterator = iter(iterable)
        except TypeError as exc:
            self._logger.warning(
                "inspector.finding.skipped",
                index=index,
                reason="for_each_not_iterable",
                error=str(exc),
            )
            return

        for item in iterator:
            iter_context = {**context, var_name: item}
            try:
                when_result = await dsl.evaluate(rule.when, iter_context)
            except _DSL_EXCEPTIONS as exc:
                self._logger.warning(
                    "inspector.finding.iteration_skipped",
                    index=index,
                    reason="when_evaluate_failed",
                    error=str(exc),
                )
                continue
            if not when_result:
                continue
            try:
                message = dsl.format_message(rule.message, iter_context)
            except _FORMAT_MESSAGE_EXCEPTIONS as exc:
                self._logger.warning(
                    "inspector.finding.iteration_skipped",
                    index=index,
                    reason="format_message_failed",
                    error=str(exc),
                )
                continue
            out.append(
                Finding(
                    severity=rule.severity,
                    message=message,
                    evidence={var_name: str(item)},
                )
            )

    async def _evaluate_aggregate_rule(
        self,
        rule: FindingRule,
        index: int,
        context: dict[str, Any],
        out: list[Finding],
    ) -> None:
        """Evaluate a single aggregate-mode rule, appending zero or one finding."""

        try:
            when_result = await dsl.evaluate(rule.when, context)
        except _DSL_EXCEPTIONS as exc:
            self._logger.warning(
                "inspector.finding.skipped",
                index=index,
                reason="when_evaluate_failed",
                error=str(exc),
            )
            return
        if not when_result:
            return
        try:
            message = dsl.format_message(rule.message, context)
        except _FORMAT_MESSAGE_EXCEPTIONS as exc:
            self._logger.warning(
                "inspector.finding.skipped",
                index=index,
                reason="format_message_failed",
                error=str(exc),
            )
            return
        out.append(
            Finding(
                severity=rule.severity,
                message=message,
                evidence={},
            )
        )

    def _finish(
        self,
        *,
        manifest: InspectorManifest,
        target: ExecutionTarget,
        start: float,
        result: InspectorResult,
        stdout_length: int = 0,
        stderr_length: int = 0,
    ) -> InspectorResult:
        """Emit the ``inspector_finished`` log event and return ``result``.

        Only the closed log-field set is recorded — ``parameters`` /
        ``output`` / ``secrets_env`` are NEVER part of this event.
        """

        del start  # duration is on the result itself
        self._logger.info(
            "inspector_finished",
            inspector_name=manifest.name,
            inspector_version=manifest.version,
            target_name=target.name,
            status=result.status,
            duration_seconds=result.duration_seconds,
            findings_count=len(result.findings),
            stdout_length=stdout_length,
            stderr_length=stderr_length,
        )
        return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sh_filter(value: object) -> str:
    """Jinja2 ``sh`` filter — wraps a value in ``shlex.quote``.

    ``None`` / empty list raise ``ValueError`` rather than rendering as
    an empty quoted string. Silent empty rendering would let a missing
    parameter slip past the manifest contract.
    """

    if value is None:
        raise ValueError("sh filter received None — parameter must be bound")
    if isinstance(value, list) and not value:
        raise ValueError(
            "sh filter received empty list — use map('sh') | join(...) only on non-empty arrays"
        )
    return shlex.quote(str(value))
