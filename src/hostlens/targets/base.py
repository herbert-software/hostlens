"""ExecutionTarget Protocol, Capability enum, and ExecResult model.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`ExecutionTarget` Protocol 必须定义完整接口 / `Capability` Enum 必须含 M1
最小集且与 ToolRegistry allowlist 严格相等 / `ExecResult` 必须把 `timed_out`
与 `exit_code` 字段分离.

This module is **platform-agnostic** (no POSIX-only imports) — it is safe
to import on Windows even though concrete ``LocalTarget`` / ``SSHTarget``
implementations may refuse to load there.
"""

from __future__ import annotations

import enum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "Capability",
    "ExecResult",
    "ExecutionTarget",
]


class Capability(enum.Enum):
    """M1 minimum capability set (exactly 5 members).

    Spec contract: member NAMES are uppercase; member VALUES are
    lowercase snake_case (``Capability.SSH.value == "ssh"``).

    Future milestones extend this enum in their own proposals:
    - M8 ``add-docker-target`` / ``add-kubernetes-target`` will add
      ``K8S_EXEC`` (and possibly others).
    - M9 ``add-remediation`` will add ``FILE_WRITE`` and write-class
      capabilities.

    Adding a member here is a **breaking** change for the
    ``hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST``
    (which is derived from this enum); both must move in the same PR
    to satisfy spec §场景:capabilities 与 ``CAPABILITY_ALLOWLIST`` 严格相等.
    """

    SHELL = "shell"
    FILE_READ = "file_read"
    SSH = "ssh"
    SYSTEMD = "systemd"
    DOCKER_CLI = "docker_cli"


class ExecResult(BaseModel):
    """Outcome of a single ``ExecutionTarget.exec`` call.

    Field semantics (spec §需求:`ExecResult` 必须把 `timed_out` 与 `exit_code`
    字段分离):

    - ``exit_code``: ``None`` means *no OS-level exit code observed* —
      either hostlens proactively cancelled the command (timeout) or the
      remote connection dropped before we could read the wait status.
      A real subprocess wait status (including ``128 + signum`` for
      signal-killed processes) is preserved as-is. ``-1`` MUST NOT be
      used as a magic timeout marker — it collides with signal-killed
      exit codes on some platforms.
    - ``timed_out``: ``True`` iff hostlens cancelled the command because
      the caller-supplied ``timeout`` elapsed. Callers MUST branch on
      this field — not on ``exit_code`` — to detect timeouts.

    Invariant (enforced by the model validator):
        ``timed_out is True  ⇒  exit_code is None``

    The reverse implication is intentionally NOT enforced:
    ``exit_code is None and not timed_out`` is a legal "remote dropped
    without sending exit status" outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool

    @model_validator(mode="after")
    def _enforce_timeout_invariant(self) -> ExecResult:
        if self.timed_out and self.exit_code is not None:
            raise ValueError(
                "ExecResult invariant violated: timed_out=True requires exit_code=None "
                "(spec §需求:`ExecResult` 必须把 `timed_out` 与 `exit_code` 字段分离)"
            )
        return self


@runtime_checkable
class ExecutionTarget(Protocol):
    """Async interface for running shell-evaluated commands and reading files.

    All M1 concrete implementations (``LocalTarget`` / ``SSHTarget``) must
    satisfy this Protocol structurally. M8 will add ``DockerTarget`` /
    ``KubernetesTarget`` without changing this interface.

    Spec contract:

    - ``name``: ``^[a-z][a-z0-9_\\-]{0,63}$`` (enforced by
      ``TargetsConfig`` loader + concrete ``__init__`` + ``TargetRegistry.register``).
    - ``type``: closed set of 4 strings, fixed by docs/ARCHITECTURE.md §5.
      Concrete implementations expose ``type`` as a class-level constant
      (not a ``__init__`` parameter) — see spec §场景:type 字段值域受限.
    - ``exec(cmd, *, timeout, env)``: shell-evaluated; ``env`` injected
      via subprocess ``env=`` parameter, **never** by string-splicing
      ``export VAR=...; cmd``. Returns ``ExecResult``; raises only on
      transport-level failure (auth / connection / SFTP unavailable).
    - ``read_file(path)``: max 10 MB; raises
      ``TargetError(kind="file_too_large", ...)`` on overflow.
    - ``capabilities``: runtime probe result; freshness contract is per
      implementation (LocalTarget / SSHTarget probe lazily on first
      ``exec``).
    """

    name: str
    type: Literal["local", "ssh", "docker", "k8s"]
    capabilities: set[Capability]

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> bytes: ...
