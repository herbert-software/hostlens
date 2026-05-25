"""``SSHTarget`` — asyncssh-backed remote execution target.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/ssh-execution-target/spec.md``.

Key invariants enforced by this module (each maps to a spec scenario; do
not relax without amending the spec):

- **One control connection per target instance** (spec §需求:SSHTarget /
  OPERABILITY §2.1). First ``exec`` lazily ``asyncssh.connect``s; later
  ``exec`` calls reuse the connection by opening new channels via
  ``conn.run``. ``asyncssh.connect`` is invoked **at most once** per
  active connection lifetime.
- **Reconnect path is restricted** to the case ``self._conn`` is already
  set AND the failure is ``asyncssh.ConnectionLost`` /
  ``asyncssh.ChannelOpenError``. Backoff schedule is exactly
  ``[1.0, 4.0, 16.0]`` (sleeps applied **before** each attempt — matches
  OPERABILITY §2.2 "1 次自动重连 (1s→4s→16s)" wording). Exhaustion
  raises ``TargetError(kind="ssh_connection_lost")``. First-connect
  failures NEVER enter this loop — they raise the appropriate
  ``ssh_connect_*`` kind immediately.
- **No env smuggling**: ``env`` is passed through ``conn.run(env=...)``;
  the command string itself is never mutated (no ``export VAR=...; cmd``
  prepending).
- **read_file is SFTP-only**: no ``cat`` fallback. Size cap (10 MiB) is
  checked at ``SFTPClient.stat`` so we never download large files just
  to reject them.
- **Three-layer credential scrub** on auth failures: known-secret exact
  replace → ``scrub_exception_message`` regex pass → bare-keyword scrub.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import re
import socket
import time
import warnings
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Literal

import asyncssh

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability, ExecResult

if TYPE_CHECKING:
    # Structural shape that ``TargetEntry`` (Group D, ``hostlens.targets.config``)
    # must satisfy for SSHTarget to consume. Declared locally as a
    # Protocol so this module compiles cleanly under mypy --strict in
    # both the pre-Group-D state (no ``TargetEntry`` in the registry)
    # and once the concrete class lands (a real ``TargetEntry`` will
    # structurally satisfy this Protocol). Keeping the Protocol
    # TYPE_CHECKING-only avoids any runtime import dependency.
    from typing import Protocol

    class TargetEntry(Protocol):
        # Structural typing shim — see module-level docstring.
        name: str
        host: str
        user: str
        port: int
        key_path: str | None
        password: str | None
        passphrase: str | None
        connect_timeout: int | None
        enabled: bool


__all__ = ["SSHTarget"]


_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")

# 10 MiB cap for ``read_file`` (mirrors LocalTarget; spec
# §需求:`ExecutionTarget` Protocol).
_READ_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024

# Reconnect backoff schedule. Sleeps are applied **before** each
# ``asyncssh.connect`` attempt so the OPERABILITY §2.2 wording "1s → 4s
# → 16s" is the time the loop waits prior to attempt N. Total budget
# 21 s of sleep plus connect latency.
_RECONNECT_BACKOFF: Final[tuple[float, ...]] = (1.0, 4.0, 16.0)

# asyncssh keepalive — 60 s matches OPERABILITY §2.2 + ssh spec
# "early-detect dead connections".
_KEEPALIVE_INTERVAL: Final[int] = 60

# Default connect_timeout when ``TargetEntry`` does not override.
_DEFAULT_CONNECT_TIMEOUT: Final[int] = 10

# Bare credential keyword scrub — covers "with password X" / "auth token
# Y" / "passphrase Z" formats that ``scrub_exception_message`` (which is
# tuned for ``key=value`` and well-known patterns) does not catch. The
# pattern is intentionally permissive ("safety-biased over-replacement"
# is documented behaviour — e.g. ``"password policy"`` will be replaced
# with ``"password ***"``; that is preferred to leaking a credential).
_BARE_CRED_KEYWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth)\s+\S+",
)

# Pre-pass user@host pattern. We scrub user@host as one unit BEFORE
# Layer 2 runs because the shared scrubber processes its patterns in a
# fixed order (IPv4 → user@host); on input "admin@10.0.0.5" Layer 2's
# IPv4 pass turns the string into "admin@***", which no longer matches
# its user@host regex and leaves "admin@" visible. Running this pass
# first guarantees the whole user@host token is collapsed to ``***``.
_USER_AT_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[\w.+\-]+@(?:[\w.\-]+|(?:\d{1,3}\.){3}\d{1,3})",
)


class SSHTarget:
    """Remote execution target backed by a per-instance asyncssh control connection.

    Construction is pure: no IO happens until the first ``exec`` /
    ``read_file`` call (or an explicit ``aclose``). The control
    connection lifecycle is managed via ``self._lock`` — only the
    connection-state machine (``self._conn is None`` decisions and the
    reconnect handshake) is serialised; ``conn.run`` channels are opened
    in parallel by asyncssh itself, which natively multiplexes.

    Instances are typically built by ``build_registry_from_config`` and
    then ``TargetRegistry.register`` injects ``self._entry`` with the
    source ``TargetEntry`` so we can read host / user / port / credentials
    and the per-target ``connect_timeout`` override. Constructed
    standalone (no ``_entry``) the target raises a clear ``TargetError``
    when ``exec`` is called, since asyncssh needs at minimum a host.
    """

    type: Literal["ssh"] = "ssh"

    def __init__(
        self,
        name: str,
        *,
        _settings: object | None = None,
        _insecure_skip_host_key_check: bool = False,
    ) -> None:
        # Initialise ``_conn`` first so ``__del__`` is safe even when
        # the name-regex check below raises and Python still calls our
        # destructor on the half-constructed object.
        self._conn: asyncssh.SSHClientConnection | None = None
        if _NAME_PATTERN.fullmatch(name) is None:
            raise TargetError(kind="invalid_target_name", target=name)
        self.name: str = name
        # Initial capability set; runtime probes (SYSTEMD / DOCKER_CLI)
        # are deferred until the first successful exec. Stored as a
        # fresh instance-level set so two SSHTarget instances never
        # share capability mutations.
        self.capabilities: set[Capability] = {
            Capability.SSH,
            Capability.SHELL,
            Capability.FILE_READ,
        }
        self._probed_caps: set[Capability] | None = None

        # Connection state machine (guarded by ``self._lock`` for the
        # decision points; the connection itself is then used outside
        # the lock so parallel channels can run). ``self._conn`` is
        # initialised at the very top of ``__init__`` so ``__del__`` is
        # safe even if name-regex validation aborts construction.
        self._last_used_at: float = 0.0
        # The lock is lazily materialised on first ``_get_lock`` call
        # from a coroutine. ``asyncio.Lock`` created in ``__init__``
        # (sync code, no running loop on Python 3.11 in some
        # pytest-asyncio configurations) can fail to bind to the test's
        # event loop, breaking serialisation. Lazy creation inside a
        # coroutine guarantees the lock binds to the currently-running
        # loop. The lazy initialiser is itself race-free because it
        # contains no ``await`` — Python's coroutine semantics ensure
        # sync code runs atomically between yield points.
        self._lock: asyncio.Lock | None = None

        # ``TargetEntry`` is injected by ``TargetRegistry.register`` after
        # name validation succeeds — see execution-target spec
        # §需求:`TargetRegistry`... With no entry, ``exec`` cannot dial
        # out (no host); the standalone form is only useful for unit
        # tests that mock asyncssh.
        self._entry: TargetEntry | None = None

        # Settings injection — task 4.4 mandates ``Settings`` flow
        # through ``build_registry_from_config`` to here; lazy
        # ``Settings()`` is only the fallback for standalone unit
        # construction. Stored as opaque so this module does not
        # depend on the Settings concrete class at import time.
        self._settings: object | None = _settings

        # Host-key verification policy. Default (False) lets asyncssh
        # use its standard ``known_hosts`` resolution (reads
        # ``~/.ssh/known_hosts``; raises ``HostKeyNotVerifiable`` for
        # unknown hosts, which our exception classifier maps to
        # ``ssh_auth_failed``). The ``_insecure_skip_host_key_check``
        # opt-in is **only** for integration test fixtures against a
        # throwaway sshd container; the underscore prefix marks it as
        # non-production API.
        self._insecure_skip_host_key_check: bool = _insecure_skip_host_key_check

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect_timeout(self) -> int:
        """Effective ``connect_timeout`` for this target.

        Per-target override comes from ``self._entry.connect_timeout``;
        if not set (or no entry attached) the spec default 10 s applies.
        Settings does NOT carry a global ``connect_timeout`` in M1 (the
        ``ssh`` sub-namespace is intentionally minimal — see
        ``Settings.ssh`` docstring).
        """

        if self._entry is not None and self._entry.connect_timeout is not None:
            return int(self._entry.connect_timeout)
        return _DEFAULT_CONNECT_TIMEOUT

    def _idle_timeout(self) -> int:
        """Effective ``idle_timeout_seconds`` for this target.

        Prefers ``self._settings.ssh.idle_timeout_seconds`` (injected
        by ``build_registry_from_config`` per task 4.4). Falls back to
        a lazy ``Settings()`` instance only when no settings was
        injected (standalone unit construction). Lazy fallback uses
        ``Settings()`` so env-var overrides
        (``HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS``) still propagate in
        that path.
        """

        if self._settings is not None:
            # mypy can't statically verify the structural shape of
            # the injected object; settings concrete class is in
            # ``hostlens.core.config`` but we keep the attribute
            # opaque to avoid an import cycle at module load.
            return int(self._settings.ssh.idle_timeout_seconds)  # type: ignore[attr-defined]

        # Standalone fallback: lazy construct so test fixtures
        # monkey-patching env vars between targets still see the
        # latest value.
        from hostlens.core.config import Settings

        return int(Settings().ssh.idle_timeout_seconds)

    def _connect_kwargs(self) -> dict[str, Any]:
        """Build the asyncssh.connect kwargs dict from ``self._entry``.

        Centralised here so the first-connect and reconnect paths share
        the exact same parameter set (including the explicit security
        toggles: agent_forwarding=False, x11_forwarding=False).
        """

        entry = self._entry
        if entry is None:
            raise TargetError(
                kind="ssh_no_entry",
                target=self.name,
            )
        kwargs: dict[str, Any] = {
            "host": entry.host,
            "username": entry.user,
            "port": entry.port,
            # Security minimums (spec §需求:SSHTarget): never forward
            # the agent or X11 — Hostlens has no use for either and
            # forwarding turns this connection into an attacker pivot.
            "agent_forwarding": False,
            "x11_forwarding": False,
            "keepalive_interval": _KEEPALIVE_INTERVAL,
            "connect_timeout": self._connect_timeout(),
        }
        if entry.key_path is not None:
            kwargs["client_keys"] = [os.path.expanduser(entry.key_path)]
        if entry.password is not None:
            kwargs["password"] = entry.password
        if entry.passphrase is not None:
            kwargs["passphrase"] = entry.passphrase
        # Host-key verification policy.
        #
        # Default: do NOT pass ``known_hosts`` at all. asyncssh then
        # uses its standard resolution (reads ``~/.ssh/known_hosts``),
        # which raises ``HostKeyNotVerifiable`` for unknown hosts —
        # exactly the failure mode our exception classifier maps to
        # ``ssh_auth_failed``. This is the safe default for production.
        #
        # Opt-in: integration test fixtures pass
        # ``_insecure_skip_host_key_check=True`` to ``SSHTarget(...)`` so
        # ephemeral docker sshd containers (no stable host key in the
        # caller's ``known_hosts``) can be exercised without false-
        # positive auth failures. The underscore prefix marks this as
        # non-production API; production code must never set it.
        if self._insecure_skip_host_key_check:
            kwargs["known_hosts"] = None
        return kwargs

    async def _open_connection(self) -> asyncssh.SSHClientConnection:
        """Single ``asyncssh.connect`` call with the spec-mandated exception mapping.

        Raises ``TargetError`` with the appropriate ``kind`` for each
        spec'd failure mode; never raises asyncssh-native exceptions
        upward. The caller is responsible for choosing whether to enter
        the reconnect loop (only valid when ``self._conn`` was already
        set before the call).
        """

        try:
            return await asyncssh.connect(**self._connect_kwargs())
        except (
            TimeoutError,
            ConnectionRefusedError,
            socket.gaierror,
            OSError,
        ) as exc:
            # Network / DNS / firewall layer — explicitly NOT in the
            # "already connected then dropped" category.
            raise TargetError(
                kind="ssh_connect_timeout",
                target=self.name,
                host=self._entry.host if self._entry is not None else None,
                original=exc,
            ) from exc
        except (
            asyncssh.PermissionDenied,
            asyncssh.HostKeyNotVerifiable,
            asyncssh.misc.KeyExchangeFailed,
        ) as exc:
            # Auth / host-key / KEX — must run through the three-layer
            # scrubber before being surfaced.
            scrubbed = self._scrub_auth_exception(exc)
            raise TargetError(
                kind="ssh_auth_failed",
                target=self.name,
                error_type=type(exc).__name__,
                message=scrubbed,
            ) from None
        except asyncssh.DisconnectError as exc:
            # asyncssh often raises a ``ProtocolError`` ("Too many
            # authentication failures") instead of ``PermissionDenied``
            # when the remote sshd hits its retry limit. Both indicate
            # an auth-layer failure to the caller; treat them the same
            # so callers don't have to special-case sshd configs.
            msg = str(exc).lower()
            if (
                "auth" in msg
                or "permission" in msg
                or "no matching" in msg  # KEX / cipher mismatch
            ):
                scrubbed = self._scrub_auth_exception(exc)
                raise TargetError(
                    kind="ssh_auth_failed",
                    target=self.name,
                    error_type=type(exc).__name__,
                    message=scrubbed,
                ) from None
            raise TargetError(
                kind="ssh_connect_failed",
                target=self.name,
                original=exc,
            ) from exc
        except asyncssh.Error as exc:
            # Catch-all for any other asyncssh-native error during the
            # initial handshake (transport errors, protocol violations).
            raise TargetError(
                kind="ssh_connect_failed",
                target=self.name,
                original=exc,
            ) from exc

    def _get_lock(self) -> asyncio.Lock:
        """Return the connection-state lock, materialising on first access.

        Lazy creation guarantees the ``asyncio.Lock`` binds to the
        currently-running event loop (see ``__init__`` docstring for
        the py3.11 binding caveat). This method is pure sync code with
        no ``await`` between the None check and the assignment, so
        Python coroutine scheduling makes the initialisation atomic
        even when several coroutines call it concurrently.
        """

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_connection(self) -> asyncssh.SSHClientConnection:
        """Return a live control connection, opening or refreshing if needed.

        Acquires ``self._lock`` for the duration of the decision so two
        concurrent ``exec`` calls don't both decide "connection is dead,
        let's reconnect" and end up dialling twice. ``conn.run`` itself
        runs outside the lock — asyncssh multiplexes channels natively
        and ``self._lock`` is purely a connection-state guard.
        """

        async with self._get_lock():
            # Idle-timeout sweep: if the previous user left the
            # connection sitting around longer than the configured
            # ``ssh.idle_timeout_seconds``, close it before reuse. Using
            # ``time.monotonic`` (not ``time.time``) avoids wall-clock
            # jumps from breaking the comparison.
            if self._conn is not None:
                idle = time.monotonic() - self._last_used_at
                if idle > self._idle_timeout():
                    await self._close_conn_locked()

            if self._conn is None:
                self._conn = await self._open_connection()
            return self._conn

    async def _close_conn_locked(self) -> None:
        """Close the current control connection.

        Must be called with ``self._lock`` held. Swallows close-time
        exceptions because asyncssh's ``close`` is best-effort; whatever
        state is left over will be discovered the next time we try to
        reuse the connection.
        """

        conn = self._conn
        self._conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                # Close is best-effort: asyncssh may already have torn
                # the transport down; we just drop the stale handle.
                conn.close()
                await conn.wait_closed()

    async def _reconnect(self) -> asyncssh.SSHClientConnection:
        """Reconnect on ``ConnectionLost`` / ``ChannelOpenError`` only.

        Pre-condition: ``self._conn`` was previously established. The
        caller is responsible for clearing ``self._conn`` before invoking
        this so the loop's ``asyncssh.connect`` retries do not get
        confused with a healthy connection. Backoff sleeps are applied
        BEFORE each attempt (spec wording "1s → 4s → 16s" is the wait
        prior to attempt N).

        Returns the freshly opened connection on success. On exhaustion
        raises ``TargetError(kind="ssh_connection_lost")``. Other asyncssh
        exception classes during the reconnect attempt are re-raised
        immediately via ``_open_connection`` (which already classifies
        them to ``ssh_connect_timeout`` / ``ssh_auth_failed`` / etc.).
        """

        for delay in _RECONNECT_BACKOFF:
            await asyncio.sleep(delay)
            try:
                conn = await self._open_connection()
            except TargetError as exc:
                # ``_open_connection`` already classified the failure
                # (ssh_connect_timeout / ssh_auth_failed /
                # ssh_connect_failed). Only the "still dropping mid-
                # session" case maps to ssh_connect_failed via the
                # asyncssh.DisconnectError branch; we keep retrying
                # only when the underlying cause was the same
                # transient-drop family. Auth / KEX / DNS / refused
                # are non-retryable per spec — surface immediately.
                if exc.kind == "ssh_connect_failed":
                    # ``_open_connection`` classifies generic
                    # ``DisconnectError`` here; treat as drop and
                    # keep retrying within this block.
                    continue
                raise
            else:
                self._conn = conn
                return conn
        raise TargetError(kind="ssh_connection_lost", target=self.name)

    # ------------------------------------------------------------------
    # exec / read_file
    # ------------------------------------------------------------------

    def _check_enabled(self) -> None:
        """Raise ``TargetError(kind="target_disabled")`` if entry is disabled.

        ``self._entry is None`` (standalone construction, no registry)
        is treated as enabled so unit tests that bypass registry
        injection still work.
        """

        if self._entry is not None and self._entry.enabled is False:
            raise TargetError(kind="target_disabled", target=self.name)

    def _validate_remote_path(self, path: str) -> None:
        """Reject paths that the spec says SFTP must not see.

        Three rejection rules per ssh-execution-target spec §需求:SSH
        read_file 必须用 SFTP 拒绝 含 NUL 字节 / 换行 / 含 ``..`` 的相对路径:

        - NUL byte → ``invalid_path`` (reason ``nul_byte``)
        - Newline → ``invalid_path`` (reason ``newline``)
        - Relative path containing a ``..`` part → ``invalid_path``
          (reason ``parent_traversal_in_relative_path``). Absolute paths
          with ``..`` (e.g. ``/a/b/../c``) are allowed because the spec
          only mentions the relative-path variant; SFTP servers normalise
          those server-side anyway.
        """

        from pathlib import PurePosixPath

        if "\x00" in path:
            raise TargetError(
                kind="invalid_path",
                target=self.name,
                path=path,
                reason="nul_byte",
            )
        if "\n" in path:
            raise TargetError(
                kind="invalid_path",
                target=self.name,
                path=path,
                reason="newline",
            )
        posix = PurePosixPath(path)
        if not posix.is_absolute() and any(part == ".." for part in posix.parts):
            raise TargetError(
                kind="invalid_path",
                target=self.name,
                path=path,
                reason="parent_traversal_in_relative_path",
            )

    async def _run_on_channel(
        self,
        cmd: str,
        *,
        conn: asyncssh.SSHClientConnection,
        timeout: int,
        env: dict[str, str] | None,
    ) -> ExecResult:
        """Open a single channel on ``conn`` and run ``cmd``.

        The caller passes ``conn`` explicitly so concurrent execs do not
        race on ``self._conn`` mutation during reconnect.

        ``asyncio.wait_for`` enforces the caller-supplied timeout. On
        timeout we surface ``ExecResult(timed_out=True, exit_code=None)``
        and rely on asyncssh's per-channel cancellation to close the
        channel; the control connection itself is unaffected and the
        next ``exec`` reuses it.
        """

        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                conn.run(cmd, env=env, check=False),
                timeout=timeout,
            )
        except TimeoutError:
            duration = time.monotonic() - t0
            return ExecResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=duration,
                timed_out=True,
            )

        duration = time.monotonic() - t0
        stdout = _to_str(result.stdout)
        stderr = _to_str(result.stderr)
        # asyncssh exposes ``exit_status`` for the remote wait status
        # and ``exit_signal`` for signal-killed processes; normalise to
        # the same 128+signum convention LocalTarget uses so callers
        # can write portable assertions.
        exit_code: int | None
        if result.exit_signal is not None:
            signum = _signal_to_int(result.exit_signal)
            exit_code = 128 + signum if signum is not None else None
        else:
            exit_code = result.exit_status
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=False,
        )

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` on the remote host via a channel on the cached connection.

        - The control connection is opened lazily on first call and
          reused for subsequent ``exec`` calls (and for parallel
          ``asyncio.gather`` calls — asyncssh multiplexes channels).
        - ``env`` is forwarded through asyncssh's ``env=`` parameter; the
          command string is never mutated (no ``export VAR=...; cmd``).
        - On ``ConnectionLost`` / ``ChannelOpenError`` from an established
          connection we attempt one reconnect block (3 backoff attempts)
          and re-run the command on the new connection.
        """

        self._check_enabled()
        conn = await self._ensure_connection()

        try:
            result = await self._run_on_channel(cmd, conn=conn, timeout=timeout, env=env)
        except (asyncssh.ConnectionLost, asyncssh.ChannelOpenError):
            # The connection dropped mid-exec. Clear it under the lock,
            # walk the reconnect ladder, and re-run the command exactly
            # once on the new connection. We deliberately do NOT recurse
            # into ``exec`` (that would risk an infinite reconnect loop
            # if the remote keeps dropping us).
            async with self._get_lock():
                await self._close_conn_locked()
                new_conn = await self._reconnect()
            try:
                result = await self._run_on_channel(cmd, conn=new_conn, timeout=timeout, env=env)
            except (asyncssh.ConnectionLost, asyncssh.ChannelOpenError) as exc:
                raise TargetError(
                    kind="ssh_connect_failed",
                    target=self.name,
                    original=exc,
                ) from exc
            # Reconnect path: bind the post-exec bookkeeping to the
            # connection the retry actually ran on so a concurrent
            # reconnect cannot swap ``self._conn`` out from under the
            # probe step.
            active_conn = new_conn
        else:
            active_conn = conn

        self._last_used_at = time.monotonic()
        # Lazy capability probe on first successful exec. Spec requires
        # SSHTarget to add SYSTEMD / DOCKER_CLI to ``capabilities`` if
        # the remote ships those binaries. We do this AFTER the user's
        # exec completes (not before) so we never delay an exec on the
        # probe path; subsequent execs see the cached set via
        # ``self._probed_caps`` and skip the probe entirely.
        await self._probe_capabilities(conn=active_conn)
        return result

    async def _probe_capabilities(
        self,
        *,
        conn: asyncssh.SSHClientConnection,
    ) -> None:
        """Detect ``SYSTEMD`` / ``DOCKER_CLI`` once on first successful exec.

        Runs ``which systemctl`` / ``which docker`` via short ``conn.run``
        invocations on the caller-supplied ``conn`` so a concurrent
        reconnect that swaps ``self._conn`` cannot redirect the probe
        onto a different connection. Results live on
        ``self._probed_caps`` so repeated ``exec`` calls do not re-probe.
        Probe failures (e.g. asyncssh error mid-probe) leave
        ``self._probed_caps`` set so we don't spin on broken remotes —
        the cached value at that point is an empty set, which is the
        safe "no extra capabilities" default.
        """

        if self._probed_caps is not None:
            return
        probed: set[Capability] = set()
        for binary, cap in (
            ("systemctl", Capability.SYSTEMD),
            ("docker", Capability.DOCKER_CLI),
        ):
            try:
                result = await conn.run(
                    f"command -v {binary}",
                    check=False,
                )
            except asyncssh.Error:
                # If the remote drops mid-probe, settle for what we
                # already detected — don't error out and bring down
                # exec because of a missing capability lookup.
                break
            exit_status = getattr(result, "exit_status", None)
            if exit_status == 0:
                probed.add(cap)
        self._probed_caps = probed
        self.capabilities |= probed

    async def read_file(self, path: str) -> bytes:
        """Read up to 10 MiB from ``path`` via SFTP (no ``cat`` fallback).

        Failure modes (all surface ``TargetError`` with a distinct
        ``kind`` except for missing files, where we propagate the stdlib
        ``FileNotFoundError`` to align with ``LocalTarget.read_file``):

        - NUL byte in path → ``invalid_path``
        - SFTP subsystem unavailable on remote → ``sftp_unavailable``
        - File size > 10 MiB (checked via ``stat`` before downloading
          any bytes) → ``file_too_large``
        - File not found → ``FileNotFoundError`` (stdlib)
        """

        self._check_enabled()
        self._validate_remote_path(path)

        conn = await self._ensure_connection()

        try:
            sftp_ctx = conn.start_sftp_client()
        except asyncssh.Error as exc:
            raise TargetError(
                kind="sftp_unavailable",
                target=self.name,
                original=exc,
            ) from exc

        try:
            async with sftp_ctx as sftp:
                try:
                    attrs = await sftp.stat(path)
                except asyncssh.SFTPNoSuchFile as exc:
                    # Match LocalTarget contract: missing files surface
                    # as the stdlib exception, not a Hostlens-specific
                    # variant.
                    raise FileNotFoundError(path) from exc
                size = int(attrs.size) if attrs.size is not None else 0
                if size > _READ_FILE_MAX_BYTES:
                    raise TargetError(
                        kind="file_too_large",
                        target=self.name,
                        path=path,
                        size=size,
                    )
                async with sftp.open(path, "rb") as f:
                    data = await f.read()
                # ``read_file`` is a connection use just like ``exec``;
                # bump ``_last_used_at`` so the idle-timeout sweep does
                # not spuriously close the control connection between
                # back-to-back ``read_file`` calls.
                self._last_used_at = time.monotonic()
                # asyncssh SFTP file.read returns str|bytes depending on
                # mode; we requested "rb" so the value is bytes. Coerce
                # for safety in case the stub returns memoryview / str.
                if isinstance(data, str):
                    return data.encode("utf-8", errors="surrogateescape")
                return bytes(data)
        except asyncssh.SFTPError as exc:
            # Generic SFTP-side failure (permission, IO) — surface as
            # sftp_unavailable so callers do not try to fall back. We
            # explicitly do NOT classify this as "file not found": only
            # SFTPNoSuchFile maps to FileNotFoundError per the spec.
            raise TargetError(
                kind="sftp_unavailable",
                target=self.name,
                original=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Credential scrub
    # ------------------------------------------------------------------

    def _scrub_auth_exception(self, exc: BaseException) -> str:
        """Three-layer scrub of an asyncssh auth-failure message.

        Layer 1 — known-secret exact replace from ``self._entry``: the
        only layer that is *guaranteed* to redact the configured
        credentials regardless of whether they happen to match any
        regex. This is the answer to "what if the password is plain
        alphanumerics that no other pattern catches?".

        Layer 2 — ``scrub_exception_message``: regex pass over paths,
        IPv4/IPv6 literals, well-known credential keywords (``Bearer``,
        ``sk-...``), ``key=value`` identity assignments, and
        ``user@host`` patterns. Catches "unknown" credentials and
        identity bits that the caller could not enumerate in advance.

        Layer 3 — bare credential keyword scrub: covers ``with password
        X`` / ``auth token Y`` formats that lack the ``=`` Layer 2 keys
        on. Safety-biased over-replacement (e.g. ``password policy`` →
        ``password ***``) is documented as expected.
        """

        # Import lazily to break a potential module-load cycle: agent
        # tooling depends on hostlens.tools, hostlens.tools depends on
        # hostlens.targets, and we do not want to drag the entire
        # adapter module into SSHTarget import time.
        from hostlens.agent.tools_adapter import scrub_exception_message

        msg = str(exc)
        if self._entry is not None:
            for secret_attr in ("password", "passphrase"):
                secret = getattr(self._entry, secret_attr, None)
                if isinstance(secret, str) and secret:
                    msg = msg.replace(secret, "***")
        # Pre-pass: collapse user@host BEFORE Layer 2's IPv4 sweep would
        # otherwise turn ``admin@10.0.0.5`` into ``admin@***`` and leave
        # the bare username visible.
        msg = _USER_AT_HOST_PATTERN.sub("***", msg)
        msg = scrub_exception_message(msg)
        msg = _BARE_CRED_KEYWORD_PATTERN.sub(r"\1 ***", msg)
        return msg

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the control connection if any.

        Idempotent — safe to call repeatedly. Tests open + close 100
        SSHTarget instances and assert no ``ResourceWarning`` is raised
        (see ``tests/targets/test_ssh.py::test_aclose_no_warning``).
        """

        async with self._get_lock():
            await self._close_conn_locked()

    async def __aenter__(self) -> SSHTarget:
        return self

    async def __aexit__(
        self,
        exc_type: builtins.type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # ``builtins.type[...]`` qualifier sidesteps mypy's class-attribute
        # ``type`` shadowing (the ``type: Literal["ssh"]`` class attr
        # above otherwise makes bare ``type[...]`` resolve to the str
        # variable).
        await self.aclose()

    def __del__(self) -> None:  # pragma: no cover - destructor timing nondeterministic
        # ``asyncio`` does not let us schedule the async ``aclose`` from
        # a destructor (no running loop guarantee), so we just emit a
        # warning to nudge callers toward explicit ``aclose`` / ``async
        # with``. The asyncssh connection has its own ``__del__`` that
        # closes the underlying transport, so the warning is the only
        # action we take here.
        if self._conn is not None:
            with contextlib.suppress(Exception):
                # Destructors must never raise — and ``warnings.warn``
                # can fail during interpreter shutdown if the warnings
                # module has already been torn down.
                warnings.warn(
                    f"SSHTarget(name={self.name!r}) garbage-collected with an open "
                    "control connection; call aclose() or use 'async with' to "
                    "release resources explicitly.",
                    ResourceWarning,
                    stacklevel=2,
                )


def _to_str(value: object) -> str:
    """Normalise asyncssh stdout/stderr (str|bytes|memoryview) to ``str``."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _signal_to_int(signal_info: object) -> int | None:
    """Map asyncssh's ``exit_signal`` to a POSIX signal number.

    asyncssh exposes ``exit_signal`` as a 4-tuple
    ``(signame, core_dumped, msg, lang)`` (per RFC 4254 §6.10), where
    ``signame`` is the bare uppercase name ("KILL" / "TERM" / ...).
    Older minor versions may pass the bare string instead, so we
    handle both shapes. Returns ``None`` if the name cannot be
    resolved — callers then leave ``exit_code`` as ``None``.
    """

    if signal_info is None:
        return None
    import signal as signal_mod

    if isinstance(signal_info, tuple) and signal_info:
        name = str(signal_info[0])
    else:
        name = str(signal_info)
    candidate = name if name.startswith("SIG") else f"SIG{name}"
    try:
        return int(getattr(signal_mod.Signals, candidate))
    except (AttributeError, ValueError):
        return None
