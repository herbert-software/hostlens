"""Tests for ``hostlens.targets.local.LocalTarget``.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`LocalTarget` 必须基于 ``asyncio.create_subprocess_shell`` 实现且超时杀整个进程组(POSIX-only).

Covers tasks 3.1 (basic exec + name regex + Windows guard), 3.2 (timeout +
process-group reap), 3.3 (lazy capability probing), and 3.4 (``read_file``).

We deliberately use real subprocesses for the happy-path tests — the
spec explicitly mandates "测试用真实 fixture" (CLAUDE.md §6) and the
``LocalTarget`` value lives in actually exercising
``create_subprocess_shell`` + ``killpg`` semantics, which a mock would
trivialise away. The only mocked path is the lazy-probe counting test
where the call count IS the assertion.
"""

from __future__ import annotations

import asyncio
import getpass
import importlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability
from hostlens.targets.local import LocalTarget

# ---------------------------------------------------------------------------
# name regex + basic exec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["Prod-Web", "1web", "prod web", "", "a" * 65, "-leading-dash", "UPPER"],
)
def test_init_rejects_invalid_name(bad_name: str) -> None:
    """Spec §需求:`ExecutionTarget` Protocol name regex enforcement point #2.

    Constructor is the per-implementation defence-in-depth — even if a
    caller bypasses the loader they must not be able to smuggle an
    illegal name (uppercase / leading digit / whitespace / >64 chars).
    """

    with pytest.raises(TargetError) as exc_info:
        LocalTarget(bad_name)
    assert exc_info.value.kind == "invalid_target_name"
    assert exc_info.value.target == bad_name


@pytest.mark.parametrize("good_name", ["a", "prod-web", "host_01", "x" * 64])
def test_init_accepts_valid_name(good_name: str) -> None:
    """Valid names pass the regex and end up on ``target.name`` unchanged."""

    target = LocalTarget(good_name)
    assert target.name == good_name
    assert target.type == "local"
    # Baseline capabilities are populated synchronously in __init__ so
    # callers that inspect them before the first ``exec`` still see the
    # static minimum.
    assert Capability.SHELL in target.capabilities
    assert Capability.FILE_READ in target.capabilities


async def test_exec_runs_simple_command() -> None:
    """Happy path: ``echo hello`` returns ``exit_code=0`` and the text."""

    target = LocalTarget("t1")
    result = await target.exec("echo hello", timeout=5)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello" in result.stdout
    assert result.duration_seconds >= 0


async def test_exec_parses_shell_pipe() -> None:
    """Spec §场景:LocalTarget exec 走 shell 解析.

    ``echo a | wc -c`` must be parsed by ``sh`` — if the pipe were
    treated as a literal arg, ``echo`` would output ``a | wc -c`` and
    ``wc`` would never run.
    """

    target = LocalTarget("t1")
    result = await target.exec("echo a | wc -c", timeout=5)
    assert result.exit_code == 0
    # ``echo a`` outputs ``a\n`` (2 bytes); BSD ``wc`` pads the count.
    assert "2" in result.stdout


async def test_exec_merges_env_with_os_environ() -> None:
    """Spec §场景:LocalTarget env 合并而非替换.

    The caller-supplied ``env`` must extend ``os.environ`` (preserving
    ``PATH``), not replace it — otherwise even ``echo`` would fail on a
    minimal env because the shell can't resolve binaries.
    """

    target = LocalTarget("t1")
    result = await target.exec(
        "echo $PATH:$MY_VAR",
        timeout=5,
        env={"MY_VAR": "extra-value"},
    )
    assert result.exit_code == 0
    assert ":extra-value" in result.stdout
    # PATH must still resolve since merge preserved it.
    assert "/" in result.stdout  # any real PATH segment contains "/"


async def test_exec_non_zero_exit_code() -> None:
    """``exit 42`` must surface as ``exit_code=42`` (no auto-coercion)."""

    target = LocalTarget("t1")
    result = await target.exec("exit 42", timeout=5)
    assert result.exit_code == 42
    assert result.timed_out is False


async def test_exec_signal_killed_returns_128_plus_signum() -> None:
    """Spec §场景:signal-killed 命令返回 128+signum.

    POSIX shells encode ``128 + signum`` for signal-killed children;
    SIGSEGV (signum=11) → 139. Crucially, this must NOT be confused
    with the timeout case (``exit_code=None``).
    """

    target = LocalTarget("t1")
    # ``$$`` is the shell's own pid; killing it with SEGV reproduces the
    # 128+11 contract on Linux/macOS.
    result = await target.exec("sh -c 'kill -SEGV $$'", timeout=5)
    assert result.exit_code == 128 + signal.SIGSEGV
    assert result.timed_out is False


async def test_exec_decodes_non_utf8_bytes_with_replacement() -> None:
    """Non-UTF-8 bytes surface as ``\\ufffd`` instead of raising.

    ``printf '\\xff'`` is not portable — bash interprets the escape,
    but POSIX ``dash`` (the default ``/bin/sh`` on Debian / Ubuntu /
    GitHub Actions runners) emits the literal five-character string.
    Drive the raw 0xff byte through Python's stdout buffer instead so
    the exec sees an invalid-UTF-8 byte regardless of the shell.
    """

    target = LocalTarget("t1")
    result = await target.exec(
        "python3 -c \"import sys; sys.stdout.buffer.write(b'\\xff')\"",
        timeout=5,
    )
    assert result.exit_code == 0
    assert "�" in result.stdout


def test_windows_import_guard_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:Windows 宿主 import 时 raise ImportError.

    We can't run a real Windows host, but we can poison ``sys.platform``
    and reload the module to exercise the guard branch. After the test
    we reload it again with the real platform so subsequent tests still
    see a functional ``LocalTarget``.

    NOTE: this only covers the *guard branch*; it does NOT prove real
    Windows behaviour is correct (``os.killpg`` is still importable on
    POSIX). Real Windows support is design non-goal #8.
    """

    import hostlens.targets.local as local_mod

    monkeypatch.setattr(sys, "platform", "win32")
    try:
        with pytest.raises(ImportError, match="LocalTarget requires POSIX host"):
            importlib.reload(local_mod)
    finally:
        # Restore the real platform and reload so the module's
        # POSIX-only symbols (``os.killpg`` etc.) are bound again.
        monkeypatch.undo()
        importlib.reload(local_mod)


# ---------------------------------------------------------------------------
# timeout + process-group reap
# ---------------------------------------------------------------------------


async def test_exec_timeout_kills_process_group_and_leaves_no_zombie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:LocalTarget 超时回收整个进程组无 zombie.

    Two-layer assertion per spec:

    1. The shell PID must not exist anymore (``psutil.pid_exists``).
    2. No ``sleep 60`` process owned by the current user can remain
       anywhere in the global process table (covers the orphan case
       where ``start_new_session=True`` would have reparented sleeps to
       PID 1 if ``killpg`` had missed them).
    """

    target = LocalTarget("t1")

    captured: dict[str, int] = {}
    orig_create = asyncio.create_subprocess_shell

    async def hooked(cmd: str | bytes, *args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await orig_create(cmd, *args, **kwargs)
        # Capture only the user command's PID — probe commands are
        # short-lived and the last hook wins, which is the user cmd.
        if isinstance(cmd, str) and cmd.startswith("sleep "):
            captured["pid"] = proc.pid
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", hooked)

    result = await target.exec("sleep 60", timeout=1)
    assert result.timed_out is True
    assert result.exit_code is None
    # Sanity: we didn't actually sleep 60s.
    assert result.duration_seconds < 5.0

    parent_pid = captured["pid"]

    # Layer 1: the shell PID is gone.
    # Give the kernel a tick to finish reaping. ``proc.wait()`` already
    # returned by this point, but psutil may still see the entry briefly.
    for _ in range(20):
        if not psutil.pid_exists(parent_pid):
            break
        await asyncio.sleep(0.05)
    assert psutil.pid_exists(parent_pid) is False, (
        f"shell PID {parent_pid} still alive after timeout reap"
    )

    # Layer 2: no orphaned ``sleep 60`` owned by us anywhere.
    me = getpass.getuser()
    leaked: list[int] = []
    for p in psutil.process_iter(["cmdline", "username", "pid"]):
        try:
            info = p.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if info.get("username") != me:
            continue
        cmdline = info.get("cmdline") or []
        if not cmdline:
            continue
        # Match either ``sleep 60`` (direct) or any cmdline mentioning it.
        if "sleep" in cmdline[0] and any("60" in arg for arg in cmdline):
            leaked.append(info["pid"])
    assert leaked == [], f"orphaned sleep 60 processes survived killpg: {leaked}"


# ---------------------------------------------------------------------------
# lazy capability probing
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process`` covering only
    the surface area used by ``_probe_capabilities`` and ``exec``.
    """

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 0

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _make_probe_mock(
    *,
    docker_returncode: int,
    systemctl_returncode: int,
    counter: list[int],
) -> Callable[..., Awaitable[_FakeProc]]:
    """Return a stand-in for ``asyncio.create_subprocess_shell`` that
    counts calls and routes probe vs. user-command invocations.
    """

    async def fake(cmd: str | bytes, *args: Any, **kwargs: Any) -> _FakeProc:
        counter[0] += 1
        cmd_str = cmd.decode() if isinstance(cmd, bytes) else cmd
        if cmd_str == "which docker":
            return _FakeProc(docker_returncode)
        if cmd_str == "which systemctl":
            return _FakeProc(systemctl_returncode)
        # User command — return zero with a stub output.
        return _FakeProc(0, stdout=b"ok\n", stderr=b"")

    return fake


async def test_capability_probe_detects_docker_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``which docker`` succeeding and ``which systemctl`` failing,
    only ``DOCKER_CLI`` is added to ``capabilities``.
    """

    target = LocalTarget("probe-target")
    counter = [0]
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _make_probe_mock(
            docker_returncode=0,
            systemctl_returncode=1,
            counter=counter,
        ),
    )

    await target.exec("echo ignored", timeout=5)

    assert Capability.DOCKER_CLI in target.capabilities
    assert Capability.SYSTEMD not in target.capabilities
    # Baseline stays intact.
    assert Capability.SHELL in target.capabilities
    assert Capability.FILE_READ in target.capabilities


async def test_capability_probe_runs_once_and_init_does_no_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §需求:`LocalTarget`... lazy probe contract.

    Construction MUST NOT spawn any subprocess. The first ``exec`` runs
    exactly three subprocess invocations (2 probes + 1 user command);
    the second ``exec`` reuses the cache and only invokes the user
    command (1 more call → 4 total).
    """

    counter = [0]
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _make_probe_mock(
            docker_returncode=0,
            systemctl_returncode=0,
            counter=counter,
        ),
    )

    # Construction triggers no subprocess.
    target = LocalTarget("probe-target")
    assert counter[0] == 0

    # First ``exec``: 2 probes + 1 user command.
    await target.exec("echo first", timeout=5)
    assert counter[0] == 3

    # Second ``exec``: only the user command (probe cache hit).
    await target.exec("echo second", timeout=5)
    assert counter[0] == 4


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


async def test_read_file_returns_bytes(tmp_path: Path) -> None:
    """Happy path: a small file roundtrips as raw bytes."""

    target = LocalTarget("t1")
    f = tmp_path / "small.txt"
    payload = b"hello\x00world"  # NUL inside content is fine; only the path is restricted.
    f.write_bytes(payload)

    data = await target.read_file(str(f))
    assert data == payload


async def test_read_file_missing_raises_file_not_found_error(tmp_path: Path) -> None:
    """Spec contract: missing file propagates the stdlib exception
    (NOT a ``TargetError`` variant) so callers can use the standard
    pattern ``except FileNotFoundError``.
    """

    target = LocalTarget("t1")
    missing = tmp_path / "no-such-file.txt"
    with pytest.raises(FileNotFoundError):
        await target.read_file(str(missing))


async def test_read_file_over_10mb_raises_target_error(tmp_path: Path) -> None:
    """Spec §场景:read_file 文件超过 10MB raise.

    Using ``os.truncate`` makes the test fast on any FS — we never
    actually allocate 10 MiB of bytes, just sparse-file the size up.
    """

    target = LocalTarget("t1")
    big = tmp_path / "huge.bin"
    big.write_bytes(b"")
    os.truncate(big, 10 * 1024 * 1024 + 1)  # 10 MiB + 1 byte

    with pytest.raises(TargetError) as exc_info:
        await target.read_file(str(big))
    err = exc_info.value
    assert err.kind == "file_too_large"
    assert err.target == "t1"
    assert err.extra["path"] == str(big)
    assert err.extra["size"] == 10 * 1024 * 1024 + 1


async def test_read_file_at_exact_10mb_boundary_succeeds(tmp_path: Path) -> None:
    """Files of exactly 10 MiB must read successfully — the cap is "larger
    than" not "at or above". A sparse 10 MiB file reads as 10 MiB of zero
    bytes, which exercises the boundary without allocating real data.
    """

    target = LocalTarget("t1")
    on_boundary = tmp_path / "exactly_10mb.bin"
    on_boundary.write_bytes(b"")
    os.truncate(on_boundary, 10 * 1024 * 1024)

    data = await target.read_file(str(on_boundary))
    assert len(data) == 10 * 1024 * 1024


async def test_read_file_rejects_nul_byte_in_path() -> None:
    """NUL in the path raises ``TargetError(kind="invalid_path")`` so the
    error surface is structured (the stdlib would raise ``ValueError``
    with a wording that varies across CPython versions).
    """

    target = LocalTarget("t1")
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("/tmp/with\x00nul")
    assert exc_info.value.kind == "invalid_path"
    assert exc_info.value.target == "t1"
