"""``LocalTarget`` — POSIX-only local subprocess execution target.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`LocalTarget` 必须基于 ``asyncio.create_subprocess_shell`` 实现且超时杀整个进程组(POSIX-only).

The Windows guard fires at module import time so callers get a clear
``ImportError`` instead of a cryptic runtime failure when the
implementation reaches for ``os.killpg`` / ``os.getpgid`` /
``start_new_session=True`` (all POSIX-only). The guard MUST precede the
POSIX-only imports below — putting it after them would make the guard
unreachable (Windows would already have crashed during ``import os``'s
attribute lookups when we call ``os.killpg``).
"""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover - guard exercised on Windows only
    raise ImportError(
        "LocalTarget requires POSIX host (Linux/macOS); Windows support is not in M1 scope"
    )

import asyncio
import contextlib
import os
import re
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

# ``aiofiles`` ships no type stubs and no PEP 561 marker; suppress the
# ``import-untyped`` complaint locally instead of polluting global mypy
# config. Each call site that produces ``Any`` re-narrows via ``cast``.
import aiofiles  # type: ignore[import-untyped]

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability, ExecResult

if TYPE_CHECKING:
    from hostlens.targets.config import TargetEntry

__all__ = ["LocalTarget"]

# Mirror of the ``ExecutionTarget.name`` regex from the spec; enforced in
# ``__init__`` as the per-implementation defence-in-depth layer (the other
# two enforcement points are ``TargetsConfig`` loader and
# ``TargetRegistry.register``).
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")

# 10 MiB cap for ``read_file`` (spec §需求:`ExecutionTarget` Protocol).
_READ_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024


def _normalize_returncode(returncode: int | None) -> int | None:
    """Map ``asyncio`` subprocess returncodes to POSIX ``128 + signum``.

    ``asyncio.subprocess.Process.returncode`` exposes ``-signum`` when a
    child was killed by a signal (mirroring ``subprocess.Popen``), but
    the spec — and the rest of the Hostlens stack downstream — expect
    the POSIX shell-style ``128 + signum`` encoding so signal exits live
    in the same value domain as normal non-zero exits (spec §场景:
    signal-killed 命令返回 128+signum). The mapping is unambiguous
    because real Linux/macOS processes never exit with a negative
    ``waitpid`` status — anything ``< 0`` is the asyncio-internal
    signal marker.
    """

    if returncode is None or returncode >= 0:
        return returncode
    return 128 + (-returncode)


class LocalTarget:
    """Runs shell-evaluated commands on the local POSIX host.

    Construction is pure (no subprocess IO) so ``LocalTarget("name")`` is
    safe to use in unit tests and at import time. Capability probing
    happens lazily on the **first** ``exec`` call and is cached on the
    instance (``_probed_caps``); subsequent calls reuse the cache and do
    not re-run ``which`` probes.

    The implementation deliberately uses ``create_subprocess_shell`` (not
    ``create_subprocess_exec``) so Inspector commands containing pipes /
    redirects / variable expansions are parsed by ``sh`` — the security
    boundary for shell-injection is the *manifest renderer* (next
    proposal), not this class (see spec §决策 2).
    """

    type: Literal["local"] = "local"

    def __init__(self, name: str) -> None:
        if _NAME_PATTERN.fullmatch(name) is None:
            raise TargetError(kind="invalid_target_name", target=name)
        self.name: str = name
        # Initial capability set (probed-augmented on first ``exec``).
        # We store this as a fresh instance-level ``set`` so two
        # ``LocalTarget`` instances never share capability state — a
        # class-level mutable default would have allowed cross-instance
        # mutation through ``self.capabilities.add(...)``.
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}
        self._probed_caps: set[Capability] | None = None
        # ``TargetEntry`` is injected by ``TargetRegistry.register`` after
        # name validation succeeds (see execution-target spec §需求:`TargetRegistry`
        # 必须按 name 索引...). With no entry attached the target is treated as
        # enabled — unit tests that construct LocalTarget directly without a
        # registry rely on this fallback.
        self._entry: TargetEntry | None = None

    def _check_enabled(self) -> None:
        """Raise ``TargetError(kind="target_disabled")`` if entry is disabled.

        ``self._entry is None`` (standalone construction, no registry)
        is treated as enabled so unit tests that bypass registry
        injection still work.
        """

        if self._entry is not None and self._entry.enabled is False:
            raise TargetError(kind="target_disabled", target=self.name)

    async def _probe_capabilities(self) -> None:
        """Run ``which`` probes once and cache the result.

        The probe order is deterministic so test mocks counting
        ``create_subprocess_shell`` invocations can rely on exactly
        ``len(_PROBES)`` calls happening before the first user command.
        """

        if self._probed_caps is not None:
            return
        probed: set[Capability] = set()
        probes: tuple[tuple[str, Capability], ...] = (
            ("which docker", Capability.DOCKER_CLI),
            ("which systemctl", Capability.SYSTEMD),
        )
        for cmd, cap in probes:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                probed.add(cap)
        self._probed_caps = probed
        # Merge into the public set so consumers observe the probed
        # extras without losing the static baseline.
        self.capabilities |= probed

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` under ``sh -c`` and return an :class:`ExecResult`.

        Implementation notes:

        - ``start_new_session=True`` puts the spawned shell into a new
          process group so a timeout can ``killpg`` the whole tree
          (``sh → sleep`` etc.) — calling ``proc.kill`` alone would only
          terminate the top-level shell and leak the children to PID 1.
        - The caller-supplied ``env`` is merged onto ``os.environ.copy()``
          (not used to *replace* the environment) so PATH / locale / DNS
          resolver vars are preserved.
        - stdout / stderr are decoded with ``errors="replace"`` so non-UTF-8
          byte sequences surface as the Unicode replacement character
          instead of raising.
        """

        self._check_enabled()
        await self._probe_capabilities()

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            # Kill the whole process group; the shell's children
            # (``sleep``, ``find``, etc.) live inside the same session
            # because of ``start_new_session=True``. ``ProcessLookupError``
            # means the process tree died on its own between the
            # ``TimeoutError`` and the ``killpg`` call — benign.
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
            # Drain whatever the subprocess wrote before being killed so
            # callers still see partial output. ``communicate`` is
            # idempotent here and also reaps the zombie.
            stdout_bytes, stderr_bytes = await proc.communicate()
            duration = time.monotonic() - t0
            return ExecResult(
                exit_code=None,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                duration_seconds=duration,
                timed_out=True,
            )

        duration = time.monotonic() - t0
        return ExecResult(
            exit_code=_normalize_returncode(proc.returncode),
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_seconds=duration,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        """Read up to 10 MiB from ``path`` asynchronously.

        - Path containing ``\\x00`` raises ``TargetError(kind="invalid_path")``
          — the underlying ``open`` would raise ``ValueError`` with a
          message that varies across Python versions, so we normalise it
          to a structured error.
        - Missing file propagates the standard ``FileNotFoundError`` (not
          wrapped) — callers can rely on the stdlib exception class.
        - Files larger than 10 MiB raise ``TargetError(kind="file_too_large")``
          *before* any bytes are read so the cap is also a memory guard.
        """

        self._check_enabled()
        if "\x00" in path:
            raise TargetError(kind="invalid_path", target=self.name, path=path)
        p = Path(path)
        # ``stat`` raises ``FileNotFoundError`` directly — propagate per
        # the spec contract (callers treat "missing file" with the stdlib
        # exception class, not a ``TargetError`` variant).
        size = p.stat().st_size
        if size > _READ_FILE_MAX_BYTES:
            raise TargetError(
                kind="file_too_large",
                target=self.name,
                path=path,
                size=size,
            )
        async with aiofiles.open(p, "rb") as f:
            # ``aiofiles`` is untyped — narrow the dynamic ``Any`` back
            # to ``bytes`` since we opened in binary mode above.
            return cast(bytes, await f.read())
