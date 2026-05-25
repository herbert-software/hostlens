"""Unit tests for ``hostlens.targets.ssh.SSHTarget``.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/ssh-execution-target/spec.md``.

These tests mock ``asyncssh.connect`` / ``conn.run`` / SFTP to exercise
the state-machine and exception-mapping logic without standing up a real
sshd. The complementary end-to-end coverage (real sshd container, no
mocks) lives in ``test_ssh_integration.py`` per CLAUDE.md §6 (testing
rule "测试用真实 fixture") — the unit tests here are explicitly the
"narrow" part of the testing pyramid.

Covers tasks 5.1-5.9:

- 5.1 lazy connection + reuse
- 5.2 idle timeout
- 5.3 reconnect path (success / exhaustion / first-connect skip)
- 5.4 exec channel + parallel exec sharing
- 5.5 first-connect exception kind mapping
- 5.6 read_file SFTP-only branches
- 5.7 env via ``conn.run(env=...)`` only
- 5.8 three-layer credential scrub
- 5.9 ``aclose`` + destructor warning surface
"""

from __future__ import annotations

import asyncio
import socket
import warnings
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from hostlens.core.exceptions import TargetError
from hostlens.targets.ssh import SSHTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeEntry:
    """Lightweight stand-in for the Group D ``TargetEntry``.

    SSHTarget only reads attributes off the entry — duck-typing via a
    plain dataclass is sufficient and keeps these tests independent of
    the Group D config module (which is still skeleton-only).
    """

    name: str = "ssh-host"
    host: str = "remote.example"
    user: str = "alice"
    port: int = 22
    key_path: str | None = None
    password: str | None = None
    passphrase: str | None = None
    connect_timeout: int | None = None
    enabled: bool = True


def _make_run_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_status: int | None = 0,
    exit_signal: str | None = None,
) -> MagicMock:
    """Build an asyncssh-compatible ``SSHCompletedProcess`` stand-in."""

    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.exit_status = exit_status
    result.exit_signal = exit_signal
    return result


def _make_fake_conn(
    *,
    run_result: MagicMock | None = None,
    run_side_effect: Any = None,
) -> MagicMock:
    """Build an asyncssh ``SSHClientConnection`` stand-in.

    ``run`` is configured as an ``AsyncMock`` returning ``run_result``
    (or honoring ``run_side_effect`` when provided so tests can raise
    ``ConnectionLost`` on the first attempt).
    """

    conn = MagicMock()
    if run_side_effect is not None:
        conn.run = AsyncMock(side_effect=run_side_effect)
    else:
        conn.run = AsyncMock(
            return_value=run_result or _make_run_result(stdout="ok"),
        )
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    return conn


def _attach_entry(target: SSHTarget, entry: FakeEntry) -> None:
    """Inject a fake entry the way ``TargetRegistry.register`` will.

    Unit tests bypass the registry (which is Group D) — this helper is
    the documented single point where ``_entry`` is set so spec
    enforcement points match.
    """

    target._entry = entry  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Task 5.1 — construction + lazy connect + control connection reuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["Prod-Web", "1web", "prod web", "", "a" * 65, "-leading", "UPPER"],
)
def test_init_rejects_invalid_name(bad_name: str) -> None:
    with pytest.raises(TargetError) as exc_info:
        SSHTarget(bad_name)
    assert exc_info.value.kind == "invalid_target_name"
    assert exc_info.value.target == bad_name


def test_init_baseline_attrs() -> None:
    target = SSHTarget("ssh-host")
    assert target.name == "ssh-host"
    assert target.type == "ssh"
    cap_values = {c.value for c in target.capabilities}
    assert cap_values == {"ssh", "shell", "file_read"}
    assert target._conn is None
    assert target._entry is None


async def test_exec_reuses_single_connection_across_calls() -> None:
    """Three sequential ``exec`` calls must trigger ``asyncssh.connect`` once."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    fake_conn = _make_fake_conn(run_result=_make_run_result(stdout="ok"))

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=fake_conn),
    ) as mock_connect:
        await target.exec("echo a", timeout=5)
        await target.exec("echo b", timeout=5)
        await target.exec("echo c", timeout=5)

    assert mock_connect.call_count == 1
    # asyncssh.run was called 3 times for user execs + 2 times for the
    # one-shot capability probe (``command -v systemctl`` / ``docker``)
    # that the first successful exec triggers.
    user_calls = [
        c for c in fake_conn.run.await_args_list if not c.args[0].startswith("command -v ")
    ]
    probe_calls = [c for c in fake_conn.run.await_args_list if c.args[0].startswith("command -v ")]
    assert len(user_calls) == 3
    assert len(probe_calls) == 2


# ---------------------------------------------------------------------------
# Task 5.2 — idle timeout closes + re-opens
# ---------------------------------------------------------------------------


async def test_idle_timeout_triggers_reconnect() -> None:
    """If ``time.monotonic`` jumps past ``idle_timeout`` the conn is re-opened."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn_a = _make_fake_conn(run_result=_make_run_result(stdout="a"))
    conn_b = _make_fake_conn(run_result=_make_run_result(stdout="b"))

    fake_time = {"now": 1000.0}

    def _monotonic() -> float:
        return fake_time["now"]

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect", new=AsyncMock(side_effect=[conn_a, conn_b])
        ) as mock_connect,
        patch("hostlens.targets.ssh.time.monotonic", side_effect=_monotonic),
    ):
        await target.exec("echo a", timeout=5)
        # Jump past the 300 s default idle window.
        fake_time["now"] = 1000.0 + 400.0
        await target.exec("echo b", timeout=5)

    assert mock_connect.call_count == 2
    conn_a.close.assert_called_once()


async def test_within_idle_window_keeps_connection() -> None:
    """A second exec inside the idle window must not reconnect."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn_a = _make_fake_conn(run_result=_make_run_result(stdout="a"))

    fake_time = {"now": 1000.0}

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect", new=AsyncMock(return_value=conn_a)
        ) as mock_connect,
        patch("hostlens.targets.ssh.time.monotonic", side_effect=lambda: fake_time["now"]),
    ):
        await target.exec("echo a", timeout=5)
        fake_time["now"] = 1000.0 + 60.0  # well within 300s
        await target.exec("echo b", timeout=5)

    assert mock_connect.call_count == 1


# ---------------------------------------------------------------------------
# Task 5.3 — reconnect path
# ---------------------------------------------------------------------------


async def test_reconnect_succeeds_on_second_attempt() -> None:
    """ConnectionLost mid-exec triggers reconnect; second connect succeeds."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    # conn_a: initial connection that the first exec runs on (lost mid-exec)
    conn_a = _make_fake_conn(
        run_side_effect=[asyncssh.ConnectionLost("server dropped")],
    )
    # conn_b: the reconnect target
    conn_b = _make_fake_conn(run_result=_make_run_result(stdout="recovered"))

    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=[conn_a, conn_b]),
        ) as mock_connect,
        patch("hostlens.targets.ssh.asyncio.sleep", new=_record_sleep),
    ):
        result = await target.exec("echo hi", timeout=5)

    assert result.exit_code == 0
    assert result.stdout == "recovered"
    # 1 successful user exec on conn_b's channel; the post-exec
    # one-shot capability probe also runs on conn_b (``command -v
    # systemctl`` / ``docker``), so total ``conn_b.run`` awaits = 3.
    user_calls = [c for c in conn_b.run.await_args_list if not c.args[0].startswith("command -v ")]
    assert len(user_calls) == 1
    # 2 connect calls: original + reconnect-attempt #1
    assert mock_connect.call_count == 2
    # Exactly one backoff sleep (1s) preceded the successful retry.
    assert sleeps == [1.0]


async def test_reconnect_exhausts_full_backoff_schedule() -> None:
    """All three reconnect attempts fail → raise ``ssh_connection_lost``."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    # initial conn: lost mid-exec
    initial = _make_fake_conn(
        run_side_effect=[asyncssh.ConnectionLost("dropped")],
    )

    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(
                side_effect=[
                    initial,
                    asyncssh.ConnectionLost("retry-1"),
                    asyncssh.ConnectionLost("retry-2"),
                    asyncssh.ConnectionLost("retry-3"),
                ],
            ),
        ) as mock_connect,
        patch("hostlens.targets.ssh.asyncio.sleep", new=_record_sleep),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo hi", timeout=5)

    assert exc_info.value.kind == "ssh_connection_lost"
    assert exc_info.value.target == "ssh-host"
    # 1 initial + 3 reconnect attempts
    assert mock_connect.call_count == 4
    # OPERABILITY §2.2 schedule strictly: 1 → 4 → 16
    assert sleeps == [1.0, 4.0, 16.0]


async def test_reconnect_retry_failure_raises_target_error() -> None:
    """Second ``_run_on_channel`` dropping must surface as ``TargetError``.

    Race window: reconnect succeeds but the brand-new connection drops
    before the retried exec completes. The raw asyncssh exception must
    be wrapped so callers get the documented ``ssh_connect_failed``
    contract instead of a vendor exception type.
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn_a = _make_fake_conn(
        run_side_effect=[asyncssh.ConnectionLost("initial drop")],
    )
    conn_b = _make_fake_conn(
        run_side_effect=[asyncssh.ConnectionLost("retry drop")],
    )

    async def _instant_sleep(_delay: float) -> None:
        return None

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=[conn_a, conn_b]),
        ) as mock_connect,
        patch("hostlens.targets.ssh.asyncio.sleep", new=_instant_sleep),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo hi", timeout=5)

    assert exc_info.value.kind == "ssh_connect_failed"
    assert exc_info.value.target == "ssh-host"
    # 1 initial connect + 1 reconnect = 2 dials; we never re-enter
    # ``_reconnect`` once the retry-on-new-conn fails.
    assert mock_connect.call_count == 2


async def test_first_connect_failure_does_not_enter_reconnect_loop() -> None:
    """First-connect OSError → ssh_connect_timeout; no backoff sleeps."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=OSError("network unreachable")),
        ) as mock_connect,
        patch("hostlens.targets.ssh.asyncio.sleep", new=_record_sleep),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo hi", timeout=5)

    assert exc_info.value.kind == "ssh_connect_timeout"
    assert exc_info.value.target == "ssh-host"
    # No reconnect loop → exactly 1 connect attempt and 0 sleeps.
    assert mock_connect.call_count == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# Task 5.4 — exec + parallel channels share the connection
# ---------------------------------------------------------------------------


async def test_parallel_exec_shares_single_connection() -> None:
    """``asyncio.gather`` of three exec calls must dial out exactly once.

    Deterministically tests serialisation by making ``asyncssh.connect``
    yield via ``await asyncio.sleep(0)`` before returning the mock
    connection. Without that yield, ``AsyncMock(return_value=...)``
    completes synchronously inside the first task's lock-held section
    on the same event-loop step, so the other two tasks never observe
    contention — both py3.11 and py3.12 then pass, but for slightly
    different reasons (py3.12's lazy lock binding hides a subtle
    py3.11 scheduling difference). Forcing the yield makes the test
    a real assertion about lock-based serialisation.
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn = _make_fake_conn(run_result=_make_run_result(stdout="ok"))

    async def _slow_connect(*args: Any, **kwargs: Any) -> MagicMock:
        # The yield gives sibling tasks a chance to queue at the lock
        # before the first task finishes connecting; the subsequent
        # tasks then observe ``self._conn`` already set and skip the
        # asyncssh.connect call entirely.
        await asyncio.sleep(0)
        return conn

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        side_effect=_slow_connect,
    ) as mock_connect:
        await asyncio.gather(
            target.exec("echo a", timeout=5),
            target.exec("echo b", timeout=5),
            target.exec("echo c", timeout=5),
        )

    assert mock_connect.call_count == 1
    # Three parallel ``exec`` calls plus one probe pair (``command -v
    # systemctl`` + ``command -v docker``) fired by the first exec to
    # complete. The probe is one-shot so 5 total ``conn.run`` awaits.
    user_calls = [c for c in conn.run.await_args_list if not c.args[0].startswith("command -v ")]
    assert len(user_calls) == 3


async def test_exec_signal_killed_returns_128_plus_signum() -> None:
    """asyncssh reports ``exit_signal`` for killed processes → 128+signum."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    # SIGKILL → 9 → exit_code 137
    result_mock = _make_run_result(exit_status=None, exit_signal="KILL")
    conn = _make_fake_conn(run_result=result_mock)

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        result = await target.exec("kill -9 $$", timeout=5)

    assert result.exit_code == 137
    assert result.timed_out is False


async def test_exec_timeout_returns_timed_out() -> None:
    """``asyncio.wait_for`` timeout surfaces as ExecResult(timed_out=True)."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    async def _slow_run(*_a: Any, **_kw: Any) -> Any:
        await asyncio.sleep(10)
        return _make_run_result()

    conn = MagicMock()
    conn.run = AsyncMock(side_effect=_slow_run)
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        # timeout is an int per Protocol; use 0 so wait_for fires
        # immediately (without burning a real second of test time).
        result = await target.exec("sleep 60", timeout=0)

    assert result.timed_out is True
    assert result.exit_code is None


# ---------------------------------------------------------------------------
# Task 5.5 — first-connect exception classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_kind",
    [
        (TimeoutError("dial timeout"), "ssh_connect_timeout"),
        (OSError("network unreachable"), "ssh_connect_timeout"),
        (socket.gaierror("dns lookup failed"), "ssh_connect_timeout"),
        (ConnectionRefusedError("port closed"), "ssh_connect_timeout"),
    ],
)
async def test_first_connect_network_errors_map_to_timeout_kind(
    exc: Exception,
    expected_kind: str,
) -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=exc),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    assert exc_info.value.kind == expected_kind
    assert exc_info.value.target == "ssh-host"


async def test_first_connect_permission_denied_maps_to_auth_failed() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=asyncssh.PermissionDenied("auth failed")),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    assert exc_info.value.kind == "ssh_auth_failed"
    # original IS NOT preserved on auth failures — the message has been
    # scrubbed, and exposing ``original`` would let callers print the
    # raw asyncssh error and undo the redaction.
    assert exc_info.value.original is None


async def test_first_connect_host_key_maps_to_auth_failed() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(
                side_effect=asyncssh.HostKeyNotVerifiable(
                    "host key mismatch",
                    "RSA",
                ),
            ),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    assert exc_info.value.kind == "ssh_auth_failed"


async def test_first_connect_other_asyncssh_error_maps_to_connect_failed() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    class WeirdAsyncsshError(asyncssh.Error):
        pass

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=WeirdAsyncsshError(255, "protocol error")),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    assert exc_info.value.kind == "ssh_connect_failed"
    assert exc_info.value.original is not None


# ---------------------------------------------------------------------------
# Task 5.6 — read_file SFTP branches
# ---------------------------------------------------------------------------


def _attach_sftp(conn: MagicMock, sftp: MagicMock) -> None:
    """Wire ``conn.start_sftp_client`` to act as an async-context-manager.

    asyncssh returns an awaitable that itself implements ``__aenter__`` /
    ``__aexit__``. The cleanest way to mimic that with ``MagicMock`` is
    to give ``start_sftp_client`` a regular return value that has the
    async-CM dunders.
    """

    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=sftp)
    async_cm.__aexit__ = AsyncMock(return_value=None)
    conn.start_sftp_client = MagicMock(return_value=async_cm)


async def test_read_file_returns_bytes() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    conn = _make_fake_conn()
    sftp = MagicMock()
    sftp.stat = AsyncMock(return_value=MagicMock(size=5))

    sftp_file = MagicMock()
    sftp_file.__aenter__ = AsyncMock(return_value=sftp_file)
    sftp_file.__aexit__ = AsyncMock(return_value=None)
    sftp_file.read = AsyncMock(return_value=b"hello")
    sftp.open = MagicMock(return_value=sftp_file)
    _attach_sftp(conn, sftp)

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        data = await target.read_file("/tmp/hello.txt")

    assert data == b"hello"
    # Stat was called BEFORE open — that's the spec's size-check ordering.
    sftp.stat.assert_awaited_once_with("/tmp/hello.txt")


async def test_read_file_over_10mb_raises_before_download() -> None:
    """Size check must happen at ``stat`` so we don't pull 11 MiB just to reject."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    conn = _make_fake_conn()
    sftp = MagicMock()
    sftp.stat = AsyncMock(return_value=MagicMock(size=11 * 1024 * 1024))
    sftp.open = MagicMock()  # must NOT be awaited / called
    _attach_sftp(conn, sftp)

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(return_value=conn),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.read_file("/tmp/big.bin")

    assert exc_info.value.kind == "file_too_large"
    assert exc_info.value.extra.get("path") == "/tmp/big.bin"
    assert exc_info.value.extra.get("size") == 11 * 1024 * 1024
    sftp.open.assert_not_called()


async def test_read_file_at_exact_10mb_boundary_succeeds() -> None:
    """Files of exactly 10 MiB must read — the cap is "larger than", not
    "at or above". Mirrors the LocalTarget boundary contract.
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    payload = b"\x00" * (10 * 1024 * 1024)
    conn = _make_fake_conn()
    sftp = MagicMock()
    sftp.stat = AsyncMock(return_value=MagicMock(size=10 * 1024 * 1024))

    sftp_file = MagicMock()
    sftp_file.__aenter__ = AsyncMock(return_value=sftp_file)
    sftp_file.__aexit__ = AsyncMock(return_value=None)
    sftp_file.read = AsyncMock(return_value=payload)
    sftp.open = MagicMock(return_value=sftp_file)
    _attach_sftp(conn, sftp)

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        data = await target.read_file("/tmp/exactly_10mb.bin")

    assert len(data) == 10 * 1024 * 1024


async def test_read_file_nul_byte_path_rejected_before_connect() -> None:
    """NUL byte must short-circuit before any SFTP / connect call."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(),
        ) as mock_connect,
        pytest.raises(TargetError) as exc_info,
    ):
        await target.read_file("/tmp/x\x00.txt")

    assert exc_info.value.kind == "invalid_path"
    mock_connect.assert_not_called()


async def test_read_file_sftp_unavailable_raises() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    conn = _make_fake_conn()
    conn.start_sftp_client = MagicMock(
        side_effect=asyncssh.Error(1, "sftp subsystem disabled"),
    )

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(return_value=conn),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.read_file("/tmp/x")

    assert exc_info.value.kind == "sftp_unavailable"


async def test_read_file_missing_file_raises_filenotfound() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    conn = _make_fake_conn()
    sftp = MagicMock()
    sftp.stat = AsyncMock(
        side_effect=asyncssh.SFTPNoSuchFile("no such file"),
    )
    sftp.open = MagicMock()
    _attach_sftp(conn, sftp)

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(return_value=conn),
        ),
        pytest.raises(FileNotFoundError),
    ):
        await target.read_file("/nonexistent")


# ---------------------------------------------------------------------------
# Task 5.7 — env injection is asyncssh-only
# ---------------------------------------------------------------------------


async def test_env_is_passed_through_run_kwarg_not_command_string() -> None:
    """The exact command string must reach ``conn.run`` unmutated.

    Secrets only go through ``env=`` — never spliced into the command
    string (which would leak via ``ps auxw`` / shell history).
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn = _make_fake_conn(run_result=_make_run_result(stdout="ok"))

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        await target.exec(
            "ps auxw",
            timeout=5,
            env={"SECRET_TOKEN": "literal-secret-do-not-leak"},
        )

    # Inspect the FIRST call args — cmd must be the EXACT string.
    # NB: ``conn.run`` may also be called by the post-exec capability
    # probe (``command -v systemctl`` / ``command -v docker``), so we
    # explicitly grab call #0 instead of ``await_args`` (which would
    # return the last call).
    assert conn.run.await_args_list, "conn.run was never called"
    call_args = conn.run.await_args_list[0]
    # First positional is cmd
    assert call_args.args[0] == "ps auxw"
    # No "export" / token splicing in the cmd string.
    assert "SECRET_TOKEN" not in call_args.args[0]
    assert "literal-secret-do-not-leak" not in call_args.args[0]
    # env passed as keyword
    assert call_args.kwargs["env"] == {
        "SECRET_TOKEN": "literal-secret-do-not-leak",
    }


def test_connect_kwargs_expands_tilde_in_key_path() -> None:
    """``~`` in ``key_path`` must be expanded before reaching asyncssh.

    asyncssh.connect does not shell-evaluate ``client_keys`` entries;
    passing a raw ``~/.ssh/id_rsa`` silently fails to load the key and
    falls through to other auth methods. Hostlens expands ``~`` via
    ``os.path.expanduser`` so the documented home-relative form works.
    """

    import os

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry(key_path="~/.ssh/id_test_no_real_key"))

    kwargs = target._connect_kwargs()

    assert kwargs["client_keys"] == [os.path.expanduser("~/.ssh/id_test_no_real_key")]
    assert "~" not in kwargs["client_keys"][0]


# ---------------------------------------------------------------------------
# Task 5.8 — three-layer credential scrub
# ---------------------------------------------------------------------------


async def test_auth_failure_scrubs_known_secret() -> None:
    """Layer 1: known ``entry.password`` exact-replaced in the message.

    A plain alphanumeric password that no other layer would match — this
    test isolates Layer 1's responsibility: the configured secret MUST
    disappear regardless of pattern coverage.
    """

    secret = "literal-pwd-do-not-leak-12345"
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry(password=secret))

    err = asyncssh.PermissionDenied(
        f"auth failed for admin@10.0.0.5 with password {secret}",
    )
    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=err),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    scrubbed = str(exc_info.value)
    assert secret not in scrubbed
    # Layer 2 catches IPv4 + user@host
    assert "10.0.0.5" not in scrubbed
    assert "admin@" not in scrubbed


async def test_auth_failure_scrubs_bare_credential_keywords() -> None:
    """Layer 3: ``password X`` / ``token Y`` / ``Bearer Z`` formats."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())  # no entry.password set

    err = asyncssh.PermissionDenied(
        "auth failed with password notconfigured Bearer xyz123",
    )
    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(side_effect=err),
        ),
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    scrubbed = str(exc_info.value)
    # Bare "password X" pattern (Layer 3)
    assert "notconfigured" not in scrubbed
    # "Bearer xyz123" (Layer 2)
    assert "xyz123" not in scrubbed
    # Both keywords are still mentioned (replaced, not stripped)
    assert "password" in scrubbed.lower()


# ---------------------------------------------------------------------------
# Task 5.9 — aclose + destructor warning
# ---------------------------------------------------------------------------


async def test_aclose_closes_connection_and_is_idempotent() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())
    conn = _make_fake_conn()

    with patch(
        "hostlens.targets.ssh.asyncssh.connect",
        new=AsyncMock(return_value=conn),
    ):
        await target.exec("echo a", timeout=5)
        await target.aclose()
        await target.aclose()  # second call must be a no-op

    conn.close.assert_called_once()
    assert target._conn is None


async def test_open_close_100_targets_emits_no_resource_warning() -> None:
    """Spec §需求:析构 / aclose → "测试套不允许 ResourceWarning".

    We collect garbage BEFORE starting the warning window so any
    leftover ``MagicMock`` instances from earlier tests in the module
    that already triggered the destructor are flushed; the assertion
    targets only objects created *inside* this test.
    """

    import gc

    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        targets: list[SSHTarget] = []
        for _ in range(100):
            target = SSHTarget("ssh-host")
            _attach_entry(target, FakeEntry())
            conn = _make_fake_conn()
            with patch(
                "hostlens.targets.ssh.asyncssh.connect",
                new=AsyncMock(return_value=conn),
            ):
                await target.exec("echo a", timeout=5)
            await target.aclose()
            targets.append(target)
        # Drop strong references then collect — any SSHTarget still
        # holding a live connection at GC time would warn here.
        del targets
        gc.collect()

    resource_warnings = [
        w
        for w in caught
        if issubclass(w.category, ResourceWarning) and "SSHTarget" in str(w.message)
    ]
    assert resource_warnings == [], (
        f"SSHTarget leaked {len(resource_warnings)} ResourceWarnings: "
        f"{[str(w.message) for w in resource_warnings]}"
    )


async def test_disabled_target_exec_raises_without_connecting() -> None:
    """`enabled=False` entries refuse to exec without touching asyncssh."""

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry(enabled=False))

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(),
        ) as mock_connect,
        pytest.raises(TargetError) as exc_info,
    ):
        await target.exec("echo a", timeout=5)

    assert exc_info.value.kind == "target_disabled"
    mock_connect.assert_not_called()


async def test_disabled_target_read_file_raises_without_connecting() -> None:
    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry(enabled=False))

    with (
        patch(
            "hostlens.targets.ssh.asyncssh.connect",
            new=AsyncMock(),
        ) as mock_connect,
        pytest.raises(TargetError) as exc_info,
    ):
        await target.read_file("/tmp/x")

    assert exc_info.value.kind == "target_disabled"
    mock_connect.assert_not_called()


async def test_concurrent_exec_does_not_race_on_reconnect() -> None:
    """``_run_on_channel`` must use the caller-supplied ``conn``, not ``self._conn``.

    Race window the test pins down: when one coroutine is mid-reconnect
    and ``self._conn`` is temporarily ``None``, a sibling coroutine that
    already obtained a live ``conn`` from ``_ensure_connection`` must
    still be able to run its channel against the connection it was
    handed — not re-read the in-flight ``self._conn``.

    The direct shape of the assertion (mock ``self._conn`` to a stale
    handle, call ``_run_on_channel`` with a fresh one, assert only the
    fresh one was awaited) is more robust than a probabilistic
    asyncio-scheduling race reproduction.
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    stale_conn = _make_fake_conn(run_result=_make_run_result(stdout="stale"))
    fresh_conn = _make_fake_conn(run_result=_make_run_result(stdout="fresh"))

    # Simulate the race: ``self._conn`` was cleared / replaced after the
    # caller already captured ``conn`` from ``_ensure_connection``.
    target._conn = stale_conn  # type: ignore[assignment]

    result = await target._run_on_channel(
        "echo hi",
        conn=fresh_conn,
        timeout=5,
        env=None,
    )

    assert result.stdout == "fresh"
    fresh_conn.run.assert_awaited_once()
    stale_conn.run.assert_not_awaited()


async def test_probe_capabilities_uses_passed_conn() -> None:
    """``_probe_capabilities`` must run on the caller-supplied ``conn``.

    Race window: a sibling coroutine mid-reconnect can flip ``self._conn``
    to a stale handle between ``exec`` finishing its channel work and the
    post-exec probe firing. Pinning the probe to the connection it was
    handed prevents the probe from caching empty / wrong capability sets
    and short-circuiting subsequent execs.
    """

    target = SSHTarget("ssh-host")
    _attach_entry(target, FakeEntry())

    stale_conn = _make_fake_conn(run_result=_make_run_result(stdout="stale"))
    fresh_conn = _make_fake_conn(run_result=_make_run_result(stdout="fresh", exit_status=0))

    # Simulate the race: ``self._conn`` was swapped to a stale handle
    # after the caller already captured ``conn`` from ``_ensure_connection``.
    target._conn = stale_conn  # type: ignore[assignment]

    await target._probe_capabilities(conn=fresh_conn)

    assert fresh_conn.run.await_count >= 1
    stale_conn.run.assert_not_awaited()
