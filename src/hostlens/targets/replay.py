"""``ReplayTarget`` — execution-layer replay target (offline / deterministic).

Spec: ``openspec/changes/add-incident-pack/specs/replay-execution-target/spec.md``
Design: ``add-incident-pack/design.md`` §D1 双回放层.

``ReplayTarget`` implements the full ``ExecutionTarget`` Protocol but, instead
of running real subprocesses / reading real files, it returns pre-recorded
results from a JSON fixture. It is the execution-layer mirror of the LLM-layer
``PlaybackBackend`` (M2.6), letting Inspectors run the complete
``target → collect → parse → findings`` path against canned incident data with
zero SSH, zero real host, and zero API quota.

Two concepts that look alike but are independent (see design D1):

- The fixture's top-level ``impersonate`` field drives the **runtime** ``.type``
  property (``"local"`` / ``"ssh"`` / ``"docker"``, default ``"local"``). The
  runner's preflight checks ``target.type in manifest.targets`` (a
  ``Literal["local", "ssh", "docker"]``); impersonating an existing type makes
  that check transparent so the ``ExecutionTarget.type`` Literal and
  ``InspectorManifest.targets`` Literal never need a new ``"replay"`` member.
- The config-layer discriminator value ``type: replay`` (a ``TargetsConfig``
  union member) is what selects ``ReplayTarget`` during registry assembly. It
  never touches the runtime ``.type`` above.

Loud-failure contract: a fixture miss raises :class:`ReplayMiss` (which inherits
``HostlensError``, NOT ``TargetError`` — so the runner's ``except TargetError``
cannot swallow it as ``target_unreachable``). But because the upstream
``ToolsAdapter.dispatch`` has a blanket ``except Exception`` that absorbs tool
handler errors into ``is_error`` tool_results, pipeline-level drift detection
relies on ``self.misses`` (every miss is appended to it *even when* the call
also raises) rather than on the exception bubbling up. ``ReplayTarget`` NEVER
falls back to a real shell or filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hostlens.core.exceptions import ConfigError, ReplayMiss, TargetError
from hostlens.targets.base import Capability, ExecResult

__all__ = ["ReplayTarget"]


# Mirror of the ``ExecutionTarget.name`` regex (same as LocalTarget /
# SSHTarget) — enforced in ``__init__`` as the per-implementation
# defence-in-depth layer.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")


def _command_key(cmd: str) -> str:
    """Match key: SHA256 of the command with each line right-stripped.

    Only trailing per-line whitespace is normalised (terminal newline /
    trailing spaces that recording tools add); everything else matches
    exactly. The same normalisation is applied to fixture commands at load
    time and to the live ``cmd`` at lookup time so the two agree.
    """

    normalized = "\n".join(line.rstrip() for line in cmd.split("\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class _RecordedCommand(BaseModel):
    """One pre-recorded command result inside a fixture's ``commands[]``."""

    model_config = ConfigDict(extra="forbid")

    cmd: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    duration_seconds: float = 0.0


class _Fixture(BaseModel):
    """Top-level schema of a ReplayTarget JSON fixture (see design D1)."""

    model_config = ConfigDict(extra="forbid")

    impersonate: Literal["local", "ssh", "docker"] = "local"
    capabilities: list[str] = Field(default_factory=list)
    commands: list[_RecordedCommand] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)


class ReplayTarget:
    """Returns pre-recorded ``ExecResult`` / file bytes from a JSON fixture.

    Construction loads + validates the fixture eagerly (so a malformed fixture
    fails fast at registry-build time, not mid-run). All lookups are pure
    in-memory dict reads — no IO, no subprocess. Read-only: there is no write
    path, hence no EUID==0 guard.
    """

    def __init__(self, name: str, *, fixture: str | Path) -> None:
        if _NAME_PATTERN.fullmatch(name) is None:
            raise TargetError(kind="invalid_target_name", target=name)
        self.name: str = name

        path = Path(fixture)
        try:
            raw_text = path.read_text()
        except OSError as exc:
            raise ConfigError(
                "failed to read ReplayTarget fixture",
                kind="replay_fixture_unreadable",
                original=exc,
                target=name,
                fixture=str(path),
            ) from exc
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "failed to parse ReplayTarget fixture",
                kind="replay_fixture_parse_error",
                original=exc,
                target=name,
                fixture=str(path),
            ) from exc
        try:
            data = _Fixture.model_validate(parsed)
        except ValidationError as exc:
            raise ConfigError(
                "invalid ReplayTarget fixture schema",
                kind="replay_fixture_invalid",
                original=exc,
                target=name,
                fixture=str(path),
            ) from exc

        # Runtime ``.type`` impersonates an existing target type so runner
        # preflight (``target.type in manifest.targets``) is transparent.
        self.type: Literal["local", "ssh", "docker"] = data.impersonate

        # Project the fixture's capability strings onto the Capability enum.
        # An unknown string is a fixture authoring error — fail fast.
        caps: set[Capability] = set()
        for value in data.capabilities:
            try:
                caps.add(Capability(value))
            except ValueError as exc:
                raise ConfigError(
                    "unknown capability in ReplayTarget fixture",
                    kind="replay_fixture_unknown_capability",
                    original=exc,
                    target=name,
                    fixture=str(path),
                    capability=value,
                ) from exc
        self.capabilities: set[Capability] = caps

        # Build the command index one entry at a time so a duplicate match key
        # (identical ``cmd`` strings, or commands differing only by trailing
        # per-line whitespace) is a hard fixture-authoring error rather than a
        # silent last-writer-wins overwrite. Silent overwrite would let
        # ReplayTarget return the wrong recorded result and defeat the
        # loud-failure contract this whole target exists to provide.
        self._commands: dict[str, _RecordedCommand] = {}
        for entry in data.commands:
            key = _command_key(entry.cmd)
            if key in self._commands:
                raise ConfigError(
                    "duplicate command in ReplayTarget fixture",
                    kind="replay_fixture_duplicate_command",
                    target=name,
                    fixture=str(path),
                    command=entry.cmd,
                )
            self._commands[key] = entry
        self._files: dict[str, str] = dict(data.files)

        # Every exec/read_file miss is appended here (even when the call also
        # raises ReplayMiss). Snapshot tests assert ``target.misses == []``
        # after a pipeline run — this is the primary strict-consumption drift
        # guard, independent of any exception bubbling. Entries are
        # ``{"kind": "exec"|"read_file", "cmd": <missed command / path>}``.
        self.misses: list[dict[str, str]] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Return the pre-recorded ``ExecResult`` for ``cmd``.

        ``timeout`` and ``env`` are accepted to satisfy the Protocol but do
        **not** participate in matching (env carries secrets, never part of
        the match key; the 8 incident scenarios use no secrets). A miss is
        recorded to ``self.misses`` and then raises :class:`ReplayMiss` —
        it never falls back to a real subprocess.
        """

        del timeout, env  # accepted for Protocol parity; not part of the key
        recorded = self._commands.get(_command_key(cmd))
        if recorded is None:
            self.misses.append({"kind": "exec", "cmd": cmd})
            raise ReplayMiss(kind="exec", cmd=cmd)
        return ExecResult(
            exit_code=recorded.exit_code,
            stdout=recorded.stdout,
            stderr=recorded.stderr,
            duration_seconds=recorded.duration_seconds,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        """Return the pre-recorded bytes for ``path`` from the fixture ``files``.

        A miss is recorded to ``self.misses`` and then raises
        :class:`ReplayMiss` — it never touches the real filesystem.
        """

        content = self._files.get(path)
        if content is None:
            self.misses.append({"kind": "read_file", "cmd": path})
            raise ReplayMiss(kind="read_file", cmd=path)
        return content.encode("utf-8")
