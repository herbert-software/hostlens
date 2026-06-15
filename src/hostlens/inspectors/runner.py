"""InspectorRunner — orchestrates one Inspector run on one target.

The runner is the M1 sole entry point that translates an
``InspectorManifest`` + an ``ExecutionTarget`` into a deterministic
``InspectorResult`` (one of the five closed-set ``InspectorStatus`` values).

Design contract — per-call-site exception scoping (design.md Decision 7):

  * Every business call site has a **precise** ``except`` list. The runner
    never wraps the orchestrator body in a bare-``Exception`` catch or in a
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
import contextlib
import json
import os
import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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

__all__ = ["InspectorRunner", "coerce_and_validate_parameters"]


# --------------------------------------------------------------------------- #
# Precise exception sets — kept as module-level tuples so the precise-except
# contract is grep-friendly: `grep "except (" runner.py` enumerates every
# capture point and the bare-``Exception`` grep gate must remain empty.
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._target_registry = target_registry
        self._settings = settings
        self._logger = logger
        # Injectable clock — defaults to real UTC. Tests / replay inject a
        # frozen clock so that `sampling_window` rendered commands are
        # byte-stable across runs (ReplayTarget matches the exact rendered
        # string; a drifting `now` would never hit). Existing callers that
        # do not pass `clock` keep the previous behaviour unchanged.
        self._clock = clock

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

        if manifest is None:
            raise ValueError("manifest must not be None")
        if target is None:
            raise ValueError("target must not be None")

        # Cooperative cancellation channel (ToolContext.cancel) — checked
        # at every phase boundary. Raising ``asyncio.CancelledError`` lets
        # the ToolRegistry dispatch layer propagate the signal naturally;
        # adding a new ``InspectorStatus`` value would require a spec
        # update, while CancelledError is the standard async cancellation
        # contract that callers already handle.
        _check_cancel(cancel)

        start = time.monotonic()
        self._logger.info(
            "inspector_started",
            inspector_name=manifest.name,
            inspector_version=manifest.version,
            target_name=target.name,
        )

        # ---- Steps 1-6: preflight ------------------------------------ #
        status, missing, preflight_error = await self._preflight(
            manifest, target, allow_privileged=allow_privileged
        )
        _check_cancel(cancel)
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
        if status == "target_unreachable":
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
                    error=preflight_error,
                    missing=[],
                ),
            )

        # ---- Step 7: validate parameters + render command + collect secrets env ---- #
        #
        # Parameter validation is done at the ``run()`` boundary rather than inside
        # ``_render_command`` so that ``_render_command`` stays a pure Jinja2 helper
        # (no result-shape coupling) and the established "narrow ``except`` per call
        # site" pattern stays uniform. The manifest loader already validates that
        # ``manifest.parameters`` is itself a well-formed JSON Schema (see
        # ``InspectorManifest._validate_jsonschema_well_formed``), but the runner
        # must still validate the caller-supplied *values* against that schema:
        # without this gate, an attacker (or a buggy dispatcher) could pass a
        # string like ``"5432; rm -rf /"`` for a parameter declared as
        # ``{type: integer}``; because numeric types are assumed safe by the
        # loader's sh-filter gate, the rendered template would smuggle the
        # injection payload into the shell. ``SchemaError`` is also caught as a
        # defense-in-depth path for callers that bypass Pydantic via
        # ``model_construct`` (same shape as the ``output_schema`` defense at
        # step 9-10).
        #
        # Pipeline order (when manifest.parameters is non-None):
        #
        #   1. ``_apply_schema_defaults`` — inject any ``default`` declared on a
        #      top-level property the caller omitted. Defaults come from the
        #      manifest author (already type-correct per the schema), so they
        #      bypass step 2.
        #   2. ``_coerce_parameters`` — narrow string-typed caller values to
        #      ``integer`` / ``number`` / ``boolean`` where the schema asks for
        #      them. ``RunInspectorInput.parameters`` is locked to
        #      ``dict[str, str]`` for M2, but typed manifests must still work.
        #      Coercion is intentionally permissive (a failed cast leaves the
        #      value as a string so the next step rejects it cleanly).
        #   3. ``jsonschema.validate`` — final gate. Any value that survived
        #      coercion in the wrong type still fails here, so the
        #      ``"5432; rm -rf /"`` payload still cannot reach ``target.exec``.
        #
        # Both helpers are one-level walks over ``properties``; nested-object
        # defaults / coercion are out of M1 scope.
        _check_cancel(cancel)
        effective_parameters: dict[str, Any] = dict(parameters or {})
        if manifest.parameters is not None:
            try:
                effective_parameters = coerce_and_validate_parameters(parameters, manifest)
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
                        error=f"parameter_validation_failed: {exc.message}",
                        missing=[],
                    ),
                )
            except jsonschema.exceptions.SchemaError as exc:
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
                        error=f"parameter_schema_invalid: {exc.message}",
                        missing=[],
                    ),
                )

        # ---- sampling_window injection (when declared) ---------------- #
        #
        # Computed once here so the same window variables flow into BOTH the
        # Jinja2 command-render context and the Finding DSL eval context.
        # When `sampling_window` is omitted the dict is empty and neither
        # context changes — byte-identical to the pre-delta behaviour.
        window_context = self._build_window_context(manifest)

        try:
            cmd, secrets_env = await self._render_command(
                manifest, effective_parameters, window_context
            )
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
        _check_cancel(cancel)
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
                    # Per archived inspector-plugin-system spec §需求:
                    # `InspectorResult` Pydantic 模型字段集 — `status != "ok"`
                    # must carry a brief error description. The same text is
                    # surfaced by `render_markdown` / `render_json` so users
                    # see the timeout root cause in the rendered Report.
                    error=(f"collect.command exceeded {manifest.collect.timeout_seconds} seconds"),
                    missing=[],
                ),
                stdout_length=len(exec_result.stdout),
                stderr_length=len(exec_result.stderr),
            )

        # ---- Steps 9-10: parse + jsonschema validate ----------------- #
        _check_cancel(cancel)
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
        except jsonschema.exceptions.SchemaError as exc:
            # Defense in depth: ``InspectorManifest._validate_jsonschema_well_formed``
            # rejects malformed ``output_schema`` at load time, but a caller using
            # ``InspectorManifest.model_construct(...)`` bypasses every Pydantic
            # validator. The runner must still collapse this to ``status="exception"``
            # rather than let ``SchemaError`` escape ``run()`` as an unhandled error
            # (per spec §需求:`InspectorRunner.run` 必须永远返回 `InspectorResult` 不抛业务异常).
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
                    error=f"output_schema_invalid: {exc.message}",
                    missing=[],
                ),
                stdout_length=len(exec_result.stdout),
                stderr_length=len(exec_result.stderr),
            )

        # ---- Step 11: evaluate findings ------------------------------ #
        _check_cancel(cancel)
        findings = await self._evaluate_findings(
            manifest.findings, output, effective_parameters, window_context
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
    ) -> tuple[
        Literal["ok", "requires_unmet", "target_unreachable"],
        list[str],
        str | None,
    ]:
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

        The third tuple element is the ``TargetError.kind`` when the
        probe call site raises (e.g. ``ssh_connection_lost``); ``None``
        otherwise. Per the per-call-site exception contract,
        ``TargetError`` from ``target.exec`` during a probe maps to the
        ``target_unreachable`` status — the runner's ``run`` translates
        that tuple into the corresponding ``InspectorResult`` without
        letting the exception escape.
        """

        # Step 1: target type compatibility ---------------------------- #
        if target.type not in manifest.targets:
            return "requires_unmet", ["target_type"], None

        # Step 2: capabilities ---------------------------------------- #
        required_caps = set(manifest.requires_capabilities)
        present_caps = {cap.value for cap in target.capabilities}
        missing_caps = required_caps - present_caps
        if missing_caps:
            return "requires_unmet", sorted(missing_caps), None

        # Step 3: privilege opt-in ------------------------------------ #
        if manifest.privilege != "none" and not allow_privileged:
            return "requires_unmet", ["privilege_opt_in"], None

        # Step 4: env secrets ----------------------------------------- #
        missing_secrets = [f"env:{name}" for name in manifest.secrets if name not in os.environ]
        if missing_secrets:
            return "requires_unmet", missing_secrets, None

        # Step 5: binary probes (parallel) ---------------------------- #
        if manifest.requires_binaries:
            try:
                probe_results = await asyncio.gather(
                    *(
                        target.exec(
                            f"command -v {shlex.quote(binary)}",
                            timeout=10,
                        )
                        for binary in manifest.requires_binaries
                    )
                )
            except TargetError as exc:
                return "target_unreachable", [], exc.kind
            missing_bins = [
                f"bin:{binary}"
                for binary, exec_result in zip(
                    manifest.requires_binaries, probe_results, strict=True
                )
                if exec_result.exit_code != 0
            ]
            if missing_bins:
                return "requires_unmet", missing_bins, None

        # Step 6: file readability probes (parallel) ------------------ #
        if manifest.requires_files:
            try:
                file_probes = await asyncio.gather(
                    *(
                        target.exec(
                            f"[ -r {shlex.quote(path)} ]",
                            timeout=5,
                        )
                        for path in manifest.requires_files
                    )
                )
            except TargetError as exc:
                return "target_unreachable", [], exc.kind
            missing_files = [
                f"file:{path}"
                for path, exec_result in zip(manifest.requires_files, file_probes, strict=True)
                if exec_result.exit_code != 0
            ]
            if missing_files:
                return "requires_unmet", missing_files, None

        return "ok", [], None

    def _build_window_context(self, manifest: InspectorManifest) -> dict[str, Any]:
        """Return the `sampling_window` variables, or an empty dict.

        When `collect.sampling_window` is declared, computes
        ``window_end = clock()`` and ``window_start = window_end -
        duration_seconds`` and returns the three reserved injection
        variables. ``window_start`` / ``window_end`` are formatted as
        ``YYYY-MM-DD HH:MM:SS`` UTC strings (journalctl ``--since/--until``
        friendly, NOT the ``T``/offset-bearing ISO form). When the field is
        omitted the dict is empty so neither downstream context is touched.
        """

        window = manifest.collect.sampling_window
        if window is None:
            return {}
        # The spec promises UTC window strings, but the injected ``clock()``
        # may return an aware non-UTC datetime or a naive one. Normalize so
        # the formatted strings are always the same UTC wall-clock regardless
        # of the clock's tzinfo — otherwise an injected non-UTC clock would
        # produce wrong windows and break replay fixture stability.
        raw_end = self._clock()
        if raw_end.tzinfo is None:
            # Naive datetimes are interpreted as UTC (the documented contract
            # for the default clock); attach UTC rather than guess local time.
            window_end = raw_end.replace(tzinfo=UTC)
        else:
            window_end = raw_end.astimezone(UTC)
        window_start = window_end - timedelta(seconds=window.duration_seconds)
        fmt = "%Y-%m-%d %H:%M:%S"
        return {
            "window_start": window_start.strftime(fmt),
            "window_end": window_end.strftime(fmt),
            "window_seconds": window.duration_seconds,
        }

    async def _render_command(
        self,
        manifest: InspectorManifest,
        parameters: dict[str, Any] | None,
        window_context: dict[str, Any] | None = None,
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
        rendered = template.render(**(parameters or {}), **(window_context or {}))

        secrets_env: dict[str, str] = {name: os.environ[name] for name in manifest.secrets}
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
        window_context: dict[str, Any] | None = None,
    ) -> list[Finding]:
        """Evaluate each FindingRule in manifest order.

        Per-rule failures (DSL evaluation errors or ``format_message``
        KeyError/AttributeError) skip the offending rule with a structured
        warning log, but the overall inspector result remains ``ok``. The
        ``except`` lists are precise — every other exception propagates as
        a runner bug.
        """

        context: dict[str, Any] = {**output, **(parameters or {}), **(window_context or {})}
        out: list[Finding] = []

        for index, rule in enumerate(findings):
            if rule.for_each is not None:
                await self._evaluate_iterative_rule(rule, index, context, out)
            else:
                await self._evaluate_aggregate_rule(rule, index, context, out)

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
            # M1 finding DSL does not yet produce structured evidence —
            # `Finding.evidence: list[Evidence]` stays empty until the M3
            # finding-DSL evidence extension lands. The `for_each` bound
            # variable is already interpolated into `message` via
            # `format_message`, so no information is lost.
            out.append(
                Finding(
                    severity=rule.severity,
                    message=message,
                    evidence=[],
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
        # M1 finding DSL does not yet produce structured evidence — see
        # the iterative-rule branch above for context. `evidence=[]` is
        # the stable M1 surface; M3 will populate `list[Evidence]`.
        out.append(
            Finding(
                severity=rule.severity,
                message=message,
                evidence=[],
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


def _check_cancel(cancel: asyncio.Event | None) -> None:
    """Raise ``asyncio.CancelledError`` when the cooperative cancel event
    is set.

    Called at every phase boundary inside ``InspectorRunner.run``. Using
    the standard async cancellation contract (rather than a new
    ``InspectorStatus`` value) keeps the spec stable — the
    ``ToolRegistry`` dispatch layer already propagates
    ``CancelledError`` naturally.
    """

    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError()


def coerce_and_validate_parameters(
    params: dict[str, Any] | None, manifest: InspectorManifest
) -> dict[str, Any]:
    """Apply schema defaults, coerce caller values, then ``jsonschema.validate``.

    Shared by ``InspectorRunner.run`` (runtime gate) and the schedule loader
    (load-time gate) so both surfaces accept the *same* parameter set. A
    raw-validate-only loader would reject configs the runner accepts whenever a
    field is both ``required`` and carries a ``default`` (defaults are injected
    before validation here, not by raw ``jsonschema.validate``).

    Callers gate this on ``manifest.parameters is not None``; the body assumes a
    non-None schema. Exceptions propagate to the caller unchanged — the contract
    is ``(jsonschema.ValidationError, jsonschema.exceptions.SchemaError)``: a
    malformed inspector schema raises ``SchemaError``, a non-conforming value
    raises ``ValidationError``. The runner wraps both into ``status="exception"``
    results; the loader wraps both into ``ConfigError``.
    """

    params = dict(params or {})
    schema = manifest.parameters
    assert schema is not None  # caller gates on `manifest.parameters is not None`
    params = _apply_schema_defaults(params, schema)
    params = _coerce_parameters(params, schema)
    jsonschema.validate(params, schema)
    return params


def _apply_schema_defaults(params: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Inject top-level ``default`` values from ``schema.properties`` into ``params``.

    Standard ``jsonschema.validate`` only validates — it never fills defaults. Without
    this helper a manifest like ``properties: {expected_status: {default: 200}}`` would
    pass validation when the caller omits ``expected_status``, but the variable would
    be missing from the Jinja2 template namespace and the DSL ``when:`` evaluation
    context, breaking the manifest contract.

    Returns a new dict; never mutates the input. Caller-supplied keys always win over
    defaults (no override). Non-dict / unstructured schemas are tolerated (returns a
    copy of ``params`` unchanged) — this is one-level only; nested-object defaults
    are out of M1 scope.
    """

    merged = dict(params)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return merged
    for prop_name, prop_schema in properties.items():
        if prop_name in merged:
            continue
        if not isinstance(prop_schema, dict):
            continue
        if "default" not in prop_schema:
            continue
        merged[prop_name] = prop_schema["default"]
    return merged


def _coerce_parameters(params: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Narrow string-typed caller values to ``integer`` / ``number`` / ``boolean``.

    ``RunInspectorInput.parameters`` is locked to ``dict[str, str]`` at the
    ToolRegistry boundary (M2-locked schema), but typed manifests legitimately
    declare integer / float / bool parameters. Without coercion the post-defaults
    ``jsonschema.validate`` step would reject every caller value coming through the
    Agent.

    Security invariant: coercion MUST stay permissive — any failed cast leaves the
    value as-is, so ``jsonschema.validate`` still rejects it as the wrong type. For
    example ``int("5432; rm -rf /")`` raises ``ValueError``; the helper leaves the
    string in place; validation rejects the integer field; the runner surfaces
    ``parameter_validation_failed`` and never reaches ``target.exec``.

    Returns a new dict; never mutates the input. One-level walk over ``properties``.
    String values whose manifest-declared type is ``array`` / ``object`` are
    JSON-decoded (the Agent surface ``RunInspectorInput.parameters`` is locked to
    ``dict[str, str]``, so a structured parameter can only arrive as a JSON-encoded
    string); a non-JSON string or a decoded value that does not match the declared
    container type is left untouched for ``jsonschema.validate`` to reject.
    """

    coerced = dict(params)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return coerced
    for prop_name, prop_schema in properties.items():
        if prop_name not in coerced:
            continue
        if not isinstance(prop_schema, dict):
            continue
        declared_type = prop_schema.get("type")
        value = coerced[prop_name]
        if not isinstance(value, str):
            continue
        if declared_type == "integer":
            # Leave as-is on failed cast; jsonschema.validate rejects below.
            with contextlib.suppress(ValueError):
                coerced[prop_name] = int(value)
        elif declared_type == "number":
            with contextlib.suppress(ValueError):
                coerced[prop_name] = float(value)
        elif declared_type == "boolean":
            if value in ("true", "1"):
                coerced[prop_name] = True
            elif value in ("false", "0"):
                coerced[prop_name] = False
            # else: leave as-is; jsonschema rejects.
        elif declared_type in ("array", "object"):
            # The Agent passes structured parameters as a JSON-encoded string
            # (``parameters`` is ``dict[str, str]``). Decode it so a manifest
            # declaring e.g. ``endpoints: {type: array}`` is reachable from the
            # Agent surface. Adopt the decoded value ONLY when it matches the
            # declared container type; a non-JSON string or a type mismatch is
            # left untouched so ``jsonschema.validate`` below rejects it — the
            # same permissive-coerce-then-validate invariant as the scalar
            # branches. The per-item ``pattern`` + the ``| sh`` shellquote
            # filter remain the injection defense; this branch never reaches
            # ``target.exec`` with an unvalidated value.
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                decoded = json.loads(value)
                if (declared_type == "array" and isinstance(decoded, list)) or (
                    declared_type == "object" and isinstance(decoded, dict)
                ):
                    coerced[prop_name] = decoded
        # string / null: no coercion.
    return coerced


def _sh_filter(value: object) -> str:
    """Jinja2 ``sh`` filter — wraps a value in ``shlex.quote``.

    ``None`` / empty list raise ``jinja2.TemplateRuntimeError`` (a subclass
    of ``jinja2.TemplateError``) rather than rendering as an empty quoted
    string. Silent empty rendering would let a missing parameter slip past
    the manifest contract.

    The exception type is deliberately a Jinja2 template-runtime error —
    not ``ValueError`` — so the runner's ``except (jinja2.UndefinedError,
    jinja2.TemplateError)`` block at the ``_render_command`` call site
    catches it and surfaces a ``status="exception"`` ``InspectorResult``
    with ``error="render_failed: ..."``. A bare ``ValueError`` would
    escape that block and propagate out of ``run()`` as a runner bug.
    """

    if value is None:
        raise jinja2.TemplateRuntimeError("sh filter received None — parameter must be bound")
    if isinstance(value, list) and not value:
        raise jinja2.TemplateRuntimeError(
            "sh filter received empty list — use map('sh') | join(...) only on non-empty arrays"
        )
    return shlex.quote(str(value))
