"""Inspector fixture recorder — a development-time tool (NOT an Agent capability).

Design: ``add-inspector-authoring-contract/design.md`` §D-5.
Spec: ``add-inspector-authoring-contract/specs/inspector-fixture-recorder/spec.md``.

The recorder takes an ``InspectorManifest`` plus a real ``ExecutionTarget``,
drives the **actual** ``InspectorRunner`` against it once, and captures every
command the runner sends — the complete preflight probe sequence (``command -v``
binary probes / ``[ -r ... ]`` file probes) **and** the main ``collect.command``
— together with each command's stdout / stderr / exit code. It writes the result
as a ``ReplayTarget``-compatible JSON fixture so the inspector can later replay
offline with zero real host.

Why drive the real runner instead of re-rendering commands here:

  * **Zero drift by construction.** The single source of truth for what
    commands an inspector emits is ``InspectorRunner`` itself (preflight order,
    ``shlex.quote`` probe wrapping, Jinja2 ``| sh`` rendering, ``sampling_window``
    injection). Re-implementing any of that in the recorder would let the
    fixture drift from what the runner actually sends — exactly the failure mode
    this tool exists to kill (design §D-5). We wrap the target in a transparent
    recording proxy and let the runner do the rendering, so the recorded command
    strings are byte-identical to the live ones and ``ReplayTarget`` matching is
    guaranteed to hit.

  * **Frozen clock.** The runner accepts an injectable ``clock``. The recorder
    injects a fixed UTC clock so any ``sampling_window`` double-sampling delta
    (``window_start`` / ``window_end``) renders to a stable string — otherwise
    the recorded command would embed the recording wall-clock and never match on
    replay.

Secret handling: secrets reach commands only via ``env=secrets_env`` injection;
they are never spliced into the command string, and ``ReplayTarget`` neither
matches on nor stores ``env``. The real leak surface is therefore command
*output* — once a command echoes an injected secret into stdout / stderr, the
plaintext would land in the fixture. The recorder redacts every injected secret
value (plus well-known token / password / webhook shapes) from recorded
stdout / stderr before writing.

This module is a dev-tool / CLI (``python -m hostlens.inspectors.recorder``).
It is deliberately **NOT** a ``ToolSpec``, **NOT** registered into any
``ToolRegistry`` and **NOT** placed on ``ToolContext`` (CLAUDE.md §4.10): it is
not an Agent-invokable capability, it is a fixture generator for inspector
authors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import InspectorManifest
from hostlens.targets.base import Capability, ExecResult, ExecutionTarget
from hostlens.targets.local import LocalTarget
from hostlens.targets.registry import TargetRegistry

__all__ = [
    "RecordedFixture",
    "record_fixture",
]

# A fixed instant used to freeze the runner clock during recording so any
# ``sampling_window`` rendered command is byte-stable across recordings. The
# value is arbitrary but pinned: 2024-01-01T00:00:00Z.
_FROZEN_CLOCK = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

# Placeholder substituted in place of any redacted secret / token / password /
# webhook fragment found in recorded stdout / stderr.
_REDACTED = "***REDACTED***"

# Heuristic patterns for secrets that surface in command output even when the
# author did not route them through ``secrets_env`` (e.g. a token printed by a
# diagnostic command, a webhook URL embedded in a config dump). These are a
# defense-in-depth net on top of exact injected-secret-value redaction; they
# intentionally err toward over-redaction in a fixture (a fixture never needs a
# real credential). Each pattern keeps a non-secret prefix group and replaces
# only the trailing credential.
_SECRET_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Telegram-style bot tokens: 8-10 digit id ':' 35-char base64-ish secret.
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    # Lark / Feishu / generic webhook URLs carrying an opaque token segment.
    re.compile(
        r"(https://[\w.-]+/(?:open-apis/bot/v2/hook|services|webhook)/)[A-Za-z0-9_/-]{8,}",
    ),
    # ``password=`` / ``passwd=`` / ``token=`` / ``secret=`` key/value pairs.
    re.compile(
        r"\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
)


class RecordedFixture:
    """In-memory ``ReplayTarget``-compatible fixture produced by the recorder.

    The shape mirrors ``hostlens.targets.replay._Fixture`` exactly so the JSON
    produced by :meth:`to_json` loads back through ``ReplayTarget`` without an
    intermediate schema. Construction is pure data; no IO.
    """

    def __init__(
        self,
        *,
        impersonate: Literal["local", "ssh", "docker"],
        capabilities: list[str],
        commands: list[dict[str, Any]],
        files: dict[str, str],
    ) -> None:
        self.impersonate = impersonate
        self.capabilities = capabilities
        self.commands = commands
        self.files = files

    def to_dict(self) -> dict[str, Any]:
        return {
            "impersonate": self.impersonate,
            "capabilities": self.capabilities,
            "commands": self.commands,
            "files": self.files,
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialise to a deterministic JSON string (trailing newline added)."""

        return json.dumps(self.to_dict(), indent=indent, sort_keys=False) + "\n"


class _RecordingProxy:
    """Transparent ``ExecutionTarget`` proxy that records every ``exec`` call.

    Forwards ``exec`` / ``read_file`` to the wrapped real target (so
    secret-dependent commands actually run and produce real output) while
    appending each rendered command string + ``ExecResult`` to ``records`` in
    invocation order. ``env`` is forwarded to the real target but never recorded
    — ``ReplayTarget`` does not match on env, and env is the one place secrets
    legitimately live.

    The proxy mirrors the wrapped target's ``name`` / ``type`` / ``capabilities``
    so the runner's preflight (``target.type in manifest.targets``,
    capability check) behaves identically to a direct run.
    """

    def __init__(self, inner: ExecutionTarget) -> None:
        self._inner = inner
        self.name: str = inner.name
        self.type: Literal["local", "ssh", "docker", "k8s"] = inner.type
        # Mirror the wrapped target's capabilities as a plain settable
        # attribute (the Protocol declares ``capabilities`` settable; a
        # read-only property would not satisfy it structurally). The real
        # target probes lazily on first ``exec``, so we read the (possibly
        # augmented) set again after the run when projecting the fixture.
        self.capabilities: set[Capability] = inner.capabilities
        self.records: list[tuple[str, ExecResult]] = []
        self.files: dict[str, str] = {}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        result = await self._inner.exec(cmd, timeout=timeout, env=env)
        self.records.append((cmd, result))
        return result

    async def read_file(self, path: str) -> bytes:
        data = await self._inner.read_file(path)
        self.files[path] = data.decode("utf-8", errors="replace")
        return data


def _redact(
    text: str, secret_values: Sequence[str], scrubbers: Sequence[tuple[re.Pattern[str], str]]
) -> str:
    """Strip injected secret values + well-known credential shapes from output.

    Order: (1) replace every literal injected secret value (longest first so a
    secret that is a prefix of another does not leave a tail behind), (2) apply
    the heuristic credential patterns, (3) apply caller-supplied determinism
    scrubbers (timestamps, ``now()``-derived columns, etc.).
    """

    redacted = text
    for value in sorted((v for v in secret_values if v), key=len, reverse=True):
        redacted = redacted.replace(value, _REDACTED)
    for pattern in _SECRET_OUTPUT_PATTERNS:
        redacted = pattern.sub(_redacted_match, redacted)
    for pattern, replacement in scrubbers:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redacted_match(match: re.Match[str]) -> str:
    """Replace only the credential tail of a matched pattern, keeping any
    leading non-secret group (e.g. the webhook host prefix, the ``token=`` key).
    """

    if match.lastindex:
        # Keep group 1 (the non-secret prefix / key) and redact the rest.
        prefix = match.group(1)
        return f"{prefix}{_REDACTED}"
    return _REDACTED


async def record_fixture(
    manifest: InspectorManifest,
    target: ExecutionTarget,
    *,
    settings: Settings | None = None,
    parameters: Mapping[str, Any] | None = None,
    allow_privileged: bool = False,
    allow_failed: bool = False,
    clock: datetime = _FROZEN_CLOCK,
    scrubbers: Sequence[tuple[re.Pattern[str], str]] = (),
) -> RecordedFixture:
    """Drive ``InspectorRunner`` against ``target`` once and capture a fixture.

    Records the complete command sequence the runner emits — preflight binary /
    file probes **and** the main ``collect.command`` — with each command's
    stdout / stderr / exit code, redacts injected secrets (and well-known
    credential shapes) from the recorded output, freezes the clock so
    ``sampling_window`` commands are byte-stable, and projects the target's
    capability set onto the fixture's ``capabilities`` declaration so replay
    runs the same preflight.

    Refuses to record a fixture from a run whose ``InspectorResult.status`` is
    not ``ok`` (raising ``RuntimeError``) unless ``allow_failed=True``. Blessing
    a failed run as a committed fixture would bake a broken-backend or
    parse-error capture into the test suite as if it were a healthy baseline;
    failure-path fixtures (where a non-zero / non-JSON run is the whole point)
    are recorded deliberately with ``allow_failed=True``.

    Returns an in-memory :class:`RecordedFixture`; the caller decides where to
    persist it (the CLI entry point writes ``to_json`` to disk).
    """

    resolved_settings = settings if settings is not None else Settings()
    logger = structlog.get_logger("hostlens.inspectors.recorder")

    # Snapshot the secret values the runner will inject so we can redact any
    # that the inspector's command echoes back into stdout / stderr. We read
    # them via the runner's own render path (``os.environ``) — the same source
    # the runner uses — so the redaction set matches exactly what gets injected.
    secret_values = [os.environ[name] for name in manifest.secrets if name in os.environ]

    proxy = _RecordingProxy(target)
    runner = InspectorRunner(
        TargetRegistry(),
        settings=resolved_settings,
        logger=logger,
        clock=lambda: clock,
    )
    run_result = await runner.run(
        manifest,
        proxy,
        dict(parameters) if parameters is not None else None,
        allow_privileged=allow_privileged,
    )
    if run_result.status != "ok" and not allow_failed:
        raise RuntimeError(
            f"refusing to record fixture: inspector status={run_result.status} "
            f"error={run_result.error}"
        )

    commands: list[dict[str, Any]] = []
    for cmd, result in proxy.records:
        commands.append(
            {
                "cmd": cmd,
                "stdout": _redact(result.stdout, secret_values, scrubbers),
                "stderr": _redact(result.stderr, secret_values, scrubbers),
                "exit_code": result.exit_code,
                "duration_seconds": 0.0,
            }
        )

    if target.type not in ("local", "ssh", "docker"):
        raise ConfigError(
            f"recorder cannot impersonate target type {target.type!r}; "
            "only local/ssh/docker fixtures are supported",
            kind="recorder_unsupported_target_type",
            target=target.name,
        )
    impersonate: Literal["local", "ssh", "docker"] = target.type
    capabilities = sorted(cap.value for cap in target.capabilities)
    files = {
        path: _redact(content, secret_values, scrubbers) for path, content in proxy.files.items()
    }

    return RecordedFixture(
        impersonate=impersonate,
        capabilities=capabilities,
        commands=commands,
        files=files,
    )


# --------------------------------------------------------------------------- #
# Dev-tool CLI entry point — `python -m hostlens.inspectors.recorder`.
#
# Intentionally a standalone module-level CLI, NOT a Typer subcommand wired
# into the user-facing `hostlens` app and NOT a registered ToolSpec: the
# recorder is an inspector-author tool, not an Agent / end-user capability
# (CLAUDE.md §4.10, spec §需求: 录制器是开发期工具).
# --------------------------------------------------------------------------- #


def _build_local_target(name: str) -> ExecutionTarget:
    # ``LocalTarget.type`` is the narrower ``Literal["local"]`` and so is not
    # invariant-compatible with the Protocol's 4-value Literal; mirror the
    # established ``cast`` used by ``TargetRegistry._build_local``.
    return cast("ExecutionTarget", LocalTarget(name))


async def _amain(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hostlens.inspectors.recorder",
        description=(
            "Record a ReplayTarget-compatible fixture by driving the real "
            "InspectorRunner against a real ExecutionTarget (dev-tool)."
        ),
    )
    parser.add_argument("manifest", type=Path, help="path to the inspector manifest YAML")
    parser.add_argument("output", type=Path, help="path to write the fixture JSON")
    parser.add_argument(
        "--target-name",
        default="recorder",
        help="name for the LocalTarget used to run probes (default: recorder)",
    )
    parser.add_argument(
        "--allow-privileged",
        action="store_true",
        help="permit manifests that declare privilege != none",
    )
    parser.add_argument(
        "--allow-failed",
        action="store_true",
        help="record even when the inspector run status is not ok (failure-path fixture)",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    target = _build_local_target(args.target_name)
    fixture = await record_fixture(
        manifest,
        target,
        allow_privileged=args.allow_privileged,
        allow_failed=args.allow_failed,
    )
    args.output.write_text(fixture.to_json())
    print(f"wrote fixture: {args.output}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_amain(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
