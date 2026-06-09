"""``DockerTarget`` — docker-py-backed read-only execution target.

Spec: ``openspec/changes/add-docker-target/specs/docker-execution-target/spec.md``.

Key invariants enforced by this module (each maps to a spec scenario; do
not relax without amending the spec):

- **docker-py is optional-dep** (D5): the module-level ``import docker``
  is wrapped in ``try/except ImportError`` so an environment without the
  ``[docker]`` extra can still ``import hostlens.targets.docker`` (the
  registry imports this module unconditionally to reference
  ``DockerTarget`` in its branch). Actual construction-time use raises
  ``TargetError(kind="docker_sdk_unavailable")`` with an install hint.
- **Async-first via ``asyncio.to_thread``** (D1): docker-py is a
  synchronous blocking SDK, so every blocking call (``docker.from_env`` /
  ``containers.get`` / ``exec_run`` / ``get_archive``) is wrapped in
  ``asyncio.to_thread`` — never invoked directly on the event loop.
- **One docker client per target instance**: the client is built lazily
  on first use and reused; ``exec`` / ``read_file`` never rebuild it.
- **Two ordered entry guards**: ``exec`` / ``read_file`` first check
  ``_entry is None`` (→ ``docker_no_entry``, without touching
  ``.enabled``), then ``_entry.enabled is False`` (→ ``target_disabled``)
  — both *before* any docker call (no client constructed, no daemon dial).
- **No env smuggling** (D3): ``env`` is passed only through
  ``exec_run(environment=...)``; the command is strictly
  ``["/bin/sh", "-c", cmd]`` and is never mutated with ``export``.
  ``exec_run(environment=...)`` reaches the container process environment
  directly and is not subject to sshd ``AcceptEnv`` filtering.
- **read_file is get_archive-only** (D4): no ``exec_run("cat")``
  fallback. The ``get_archive`` chunk generator is consumed lazily through
  a streaming tar reader (never buffered whole), so the size cap (10 MiB)
  uses an unconditional running-byte backstop; ``not_a_file`` is decided
  before ``file_too_large``.
- **Timeout via outer ``asyncio.wait_for``** (D2): docker-py ``exec_run``
  has no per-exec timeout. On timeout we return
  ``ExecResult(timed_out=True, exit_code=None)`` and leave the residual
  in-container process to the docker daemon to reap.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import tarfile
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

try:
    # docker-py ships no PEP 561 stubs; ``types-docker`` is not a project
    # dependency. Silence the stub complaint locally (the optional-dep is
    # exercised structurally via the ``Any``-typed client below).
    import docker  # type: ignore[import-untyped]
except ImportError:  # docker-py is an optional-dep (``hostlens[docker]``).
    docker = None

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability, ExecResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    class TargetEntry(Protocol):
        # Structural shape injected by ``TargetRegistry.register`` — the
        # concrete ``DockerEntry`` (``hostlens.targets.config``)
        # structurally satisfies this. Kept TYPE_CHECKING-only so the
        # module has no runtime import dependency on config.
        name: str
        container: str
        docker_host: str | None
        enabled: bool


__all__ = ["DockerTarget"]


class _ByteReader(Protocol):
    """Minimal sequential byte-source the tar reader consumes (no seek/tell).

    ``tarfile.open(mode="r|*")`` only ever calls ``read`` forward, so the
    adapter need not implement seek/tell — this Protocol pins exactly that
    surface for ``mypy --strict``.
    """

    def read(self, size: int = -1, /) -> bytes: ...


class _ChunkStreamReader:
    """Wrap docker-py's ``get_archive`` chunk generator as a ``read``-able.

    ``get_archive`` yields the tar frame as a sequence of byte chunks. We
    feed those into ``tarfile`` *lazily* (one chunk pulled per shortfall)
    so the full stream is never materialised in memory — the per-member
    running-byte backstop in ``_read_capped`` then enforces the 10 MiB cap
    on the file content while the tar is read on demand. tarfile in
    ``r|*`` mode reads strictly forward, so no seek/tell is needed.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()
        self._exhausted = False

    def close(self) -> None:
        """Close the underlying chunk generator, releasing the HTTP response.

        ``get_archive``'s chunk generator is backed by a ``requests``
        streaming response. When an early exit (``not_a_file`` /
        ``file_too_large``) stops pulling chunks, the response body still
        holds the unread file bytes; dropping the generator without
        closing it leaves that connection in docker-py's per-client pool
        with unread data, so the *next* docker API call blocks until the
        60s read timeout. Calling ``.close()`` triggers ``GeneratorExit``
        in ``iter_content``, whose ``finally`` closes the response —
        urllib3 then discards (not reuses) the partially-read connection.

        ``Iterator`` does not guarantee a ``close`` (only generators do),
        so we guard with ``getattr``.
        """

        closer = getattr(self._chunks, "close", None)
        if callable(closer):
            closer()
        self._exhausted = True
        self._buf.clear()

    def read(self, size: int = -1, /) -> bytes:
        if size < 0:
            # tarfile ``r|*`` never reads with a negative size, but honour
            # the file-object contract: drain the rest of the stream.
            for chunk in self._chunks:
                self._buf.extend(chunk)
            self._exhausted = True
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < size and not self._exhausted:
            nxt = next(self._chunks, None)
            if nxt is None:
                self._exhausted = True
                break
            self._buf.extend(nxt)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out


_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")

# 10 MiB cap for ``read_file`` (mirrors LocalTarget / SSHTarget; boundary
# is strict ``>`` — exactly 10 MiB is allowed through).
_READ_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024

_PIP_INSTALL_HINT: Final[str] = 'pip install "hostlens[docker]"'

# Chunk size for the running-byte backstop while streaming a tar member.
_READ_CHUNK_BYTES: Final[int] = 64 * 1024


class DockerTarget:
    """Read-only execution target backed by a per-instance docker-py client.

    Construction is pure: no IO and no docker call happens in
    ``__init__`` (only the name-regex check and class-attribute
    assignment). The docker client is built lazily on the first
    ``exec`` / ``read_file`` and reused thereafter.

    Instances are built by ``build_registry_from_config`` and then
    ``TargetRegistry.register`` injects ``self._entry`` with the source
    ``DockerEntry`` so we can read the container reference and the
    optional ``docker_host``. Constructed standalone (no ``_entry``) the
    target raises ``TargetError(kind="docker_no_entry")`` when ``exec`` /
    ``read_file`` is called.
    """

    type: Literal["docker"] = "docker"

    def __init__(self, name: str) -> None:
        if _NAME_PATTERN.fullmatch(name) is None:
            raise TargetError(kind="invalid_target_name", target=name)
        self.name: str = name
        # Initial capability set; SYSTEMD / DOCKER_CLI are probed lazily
        # on the first successful exec. Stored as a fresh instance-level
        # set so two DockerTarget instances never share mutations.
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}
        self._probed_caps: set[Capability] | None = None

        # docker client, built lazily on first use and reused.
        self._client: Any = None
        # Serialises the lazy client build so concurrent ``exec`` /
        # ``read_file`` calls dial the daemon at most once. Materialised
        # on first ``_get_lock`` call from a coroutine so it binds to the
        # running event loop (mirrors SSHTarget's lazy-lock rationale).
        self._lock: asyncio.Lock | None = None

        # Injected by ``TargetRegistry.register`` after name validation.
        self._entry: TargetEntry | None = None

    # ------------------------------------------------------------------
    # Entry guards
    # ------------------------------------------------------------------

    def _require_entry(self) -> TargetEntry:
        """Enforce the two ordered entry guards before any docker call.

        Order is fixed (spec §需求:`DockerTarget` 两道入口防线的顺序): the
        ``_entry is None`` check must come first so we never evaluate
        ``None.enabled`` (which would raise a bare ``AttributeError``).
        """

        entry = self._entry
        if entry is None:
            raise TargetError(kind="docker_no_entry", target=self.name)
        if entry.enabled is False:
            raise TargetError(kind="target_disabled", target=self.name)
        return entry

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        """Return the client-build lock, materialising on first access.

        Lazy creation guarantees the ``asyncio.Lock`` binds to the
        currently-running event loop. Pure sync code with no ``await``
        between the None check and assignment, so coroutine scheduling
        makes initialisation atomic under concurrent callers.
        """

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _build_client(self) -> Any:
        """Construct the docker client (blocking — run via ``to_thread``).

        ``docker is None`` (extra not installed) → ``docker_sdk_unavailable``
        with the pip install hint. Any ``docker.errors.DockerException``
        (daemon unreachable / socket permission) → ``docker_unavailable``.
        """

        if docker is None:
            raise TargetError(
                kind="docker_sdk_unavailable",
                target=self.name,
                hint=_PIP_INSTALL_HINT,
            )
        entry = self._entry
        docker_host = entry.docker_host if entry is not None else None
        try:
            if docker_host is not None:
                return docker.DockerClient(base_url=docker_host)
            return docker.from_env()
        except docker.errors.DockerException as exc:
            raise TargetError(
                kind="docker_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc

    async def _ensure_client(self) -> Any:
        """Return the cached docker client, building it lazily under the lock."""

        async with self._get_lock():
            if self._client is None:
                self._client = await asyncio.to_thread(self._build_client)
            return self._client

    async def _resolve_container(self, client: Any) -> Any:
        """Look up the target container and assert it is running.

        ``NotFound`` → ``container_not_found``; any other
        ``DockerException`` → ``docker_unavailable``; a container whose
        ``status != "running"`` → ``container_not_running`` (carrying the
        observed status).
        """

        entry = self._require_entry()
        ref = entry.container
        try:
            container = await asyncio.to_thread(client.containers.get, ref)
        except docker.errors.NotFound as exc:
            raise TargetError(
                kind="container_not_found",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        except docker.errors.DockerException as exc:
            raise TargetError(
                kind="docker_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        status = container.status
        if status != "running":
            raise TargetError(
                kind="container_not_running",
                target=self.name,
                status=status,
            )
        return container

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` inside the target container via ``exec_run``.

        - Entry guards (``docker_no_entry`` / ``target_disabled``) run
          before any docker call.
        - ``cmd`` is run as ``["/bin/sh", "-c", cmd]`` for shell semantics
          (pipes / redirects / ``$VAR``); ``env`` is injected only via
          ``exec_run(environment=...)`` — never spliced into the command.
        - Timeout is enforced by an outer ``asyncio.wait_for``; on expiry
          we return ``ExecResult(timed_out=True, exit_code=None)``.
        - A non-zero exit / signal-kill is a normal ``ExecResult`` and
          does NOT raise; only transport-level failures raise.
        """

        self._require_entry()
        client = await self._ensure_client()
        container = await self._resolve_container(client)

        t0 = time.monotonic()
        try:
            exit_code, output = await asyncio.wait_for(
                asyncio.to_thread(
                    container.exec_run,
                    ["/bin/sh", "-c", cmd],
                    environment=env,
                    demux=True,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return ExecResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - t0,
                timed_out=True,
            )
        except docker.errors.NotFound as exc:
            # Container removed between ``_resolve_container`` and this
            # ``exec_run`` — a 404 is ``NotFound`` (an ``APIError`` subclass),
            # so it must be caught before the generic ``APIError`` arm to
            # avoid mislabelling a vanished container as ``exec_failed``.
            raise TargetError(
                kind="container_not_found",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        except docker.errors.APIError as exc:
            # OCI runtime exec failure (``/bin/sh`` missing on distroless,
            # etc.) — the daemon and container are healthy, only the
            # command could not start. Classify as ``exec_failed``, never
            # ``docker_unavailable`` (avoids misleading diagnosis).
            raise TargetError(
                kind="exec_failed",
                target=self.name,
                message=_scrub(exc),
            ) from exc
        except docker.errors.DockerException as exc:
            # Daemon went away mid-exec / connection dropped — a base
            # ``DockerException`` that is not an ``APIError``. Surface as a
            # structured transport error rather than letting the raw
            # docker-py exception escape (mirrors ``_resolve_container`` /
            # ``read_file``; NotFound & APIError are subclasses handled by
            # the arms above first).
            raise TargetError(
                kind="docker_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc

        duration = time.monotonic() - t0
        stdout_bytes, stderr_bytes = output if output is not None else (None, None)
        result = ExecResult(
            # ``ExitCode`` may be ``None`` in some stream modes; surface
            # ``None`` rather than a ``0`` / ``-1`` magic value (ExecResult
            # permits ``exit_code is None and not timed_out``).
            exit_code=exit_code,
            stdout=_decode(stdout_bytes),
            stderr=_decode(stderr_bytes),
            duration_seconds=duration,
            timed_out=False,
        )
        await self._probe_capabilities(container)
        return result

    async def _probe_capabilities(self, container: Any) -> None:
        """Detect ``SYSTEMD`` / ``DOCKER_CLI`` once on first successful exec.

        Uses POSIX ``command -v`` (not ``which`` — distroless / busybox
        compatible). Probe failures (``exec_run`` raising) leave
        ``self._probed_caps`` set to whatever subset was detected so we do
        not re-probe, and never affect the triggering ``exec``'s result.
        """

        if self._probed_caps is not None:
            return
        probed: set[Capability] = set()
        for binary, cap in (
            ("systemctl", Capability.SYSTEMD),
            ("docker", Capability.DOCKER_CLI),
        ):
            try:
                exit_code, _ = await asyncio.to_thread(
                    container.exec_run,
                    ["/bin/sh", "-c", f"command -v {binary}"],
                    demux=True,
                )
            except Exception:
                # Probe is a best-effort side path; settle for what was
                # already detected and stop probing.
                break
            if exit_code == 0:
                probed.add(cap)
        self._probed_caps = probed
        self.capabilities |= probed

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    def _validate_and_normalize_path(self, path: str) -> str:
        """Reject paths get_archive must not see; fold ``..`` for absolute paths.

        - NUL / newline → ``invalid_path`` (no docker request issued).
        - Relative path (not ``/``-absolute) → ``invalid_path``: the
          container cwd basis for relative get_archive is undefined.
        - Absolute path with ``..`` → folded with ``posixpath.normpath``
          (``PurePosixPath`` intentionally does NOT fold ``..``); the
          result is still a container-internal absolute path.
        """

        if "\x00" in path:
            raise TargetError(kind="invalid_path", target=self.name, path=path, reason="nul_byte")
        if "\n" in path:
            raise TargetError(kind="invalid_path", target=self.name, path=path, reason="newline")
        if not PurePosixPath(path).is_absolute():
            raise TargetError(
                kind="invalid_path", target=self.name, path=path, reason="relative_path"
            )
        return posixpath.normpath(path)

    async def read_file(self, path: str) -> bytes:
        """Read up to 10 MiB from ``path`` via ``get_archive`` (tar stream).

        Failure modes (each a distinct ``TargetError`` kind, except missing
        files which surface the stdlib ``FileNotFoundError`` to align with
        LocalTarget / SSHTarget):

        - NUL / newline / relative path → ``invalid_path`` (pre-request).
        - File not found → ``FileNotFoundError``.
        - Path resolves to a directory / symlink / non-regular entry, or
          the archive contains more than one regular file → ``not_a_file``
          (decided before ``file_too_large``).
        - Regular file > 10 MiB → ``file_too_large``.
        """

        self._require_entry()
        normalized = self._validate_and_normalize_path(path)

        client = await self._ensure_client()
        container = await self._resolve_container(client)

        try:
            stream, _stat = await asyncio.to_thread(container.get_archive, normalized)
        except docker.errors.NotFound as exc:
            raise FileNotFoundError(path) from exc
        except docker.errors.DockerException as exc:
            # Daemon went away / permission denied / other API error during
            # the archive fetch — surface as a structured transport error
            # rather than letting the raw docker-py exception escape
            # (NotFound is a DockerException subclass, so it is handled by
            # the arm above first).
            raise TargetError(
                kind="docker_unavailable",
                target=self.name,
                message=_scrub(exc),
            ) from exc

        # docker-py returns a generator of byte chunks. We wrap it in a
        # lazy ``read``-able adapter and hand that to a streaming
        # (``r|*``) tar reader so the whole archive is never buffered in
        # memory; the per-member backstop in ``_read_capped`` enforces the
        # 10 MiB cap on the file content as the tar is consumed on demand.
        reader = _ChunkStreamReader(iter(stream))
        return await asyncio.to_thread(self._extract_single_file, reader, path)

    def _extract_single_file(self, reader: _ChunkStreamReader, path: str) -> bytes:
        """Single forward pass over the tar: enforce single-regular-file + size.

        ``not_a_file`` is decided before ``file_too_large`` (spec
        §需求:read_file 固定顺序): the first non-regular-file entry, or a
        second regular-file entry, raises ``not_a_file`` immediately. The
        size cap uses an unconditional running-byte backstop while reading
        the member (``> 10 MiB`` aborts), with the tar stat ``size`` as an
        optional early-exit optimisation — not the sole defence.

        Runs inside ``asyncio.to_thread``. The ``reader.close()`` lives in
        a ``finally`` here (not at the ``read_file`` call site) so that the
        ``get_archive`` generator is closed *on this same worker thread* on
        every exit path — normal return and any raise
        (``not_a_file`` / ``file_too_large`` / other). Closing it from the
        thread keeps the ``GeneratorExit``→docker-py response-close on the
        thread that owns the socket, instead of crossing threads. Without
        this, an early exit drops the generator with unread file bytes
        still buffered, poisoning the per-client connection pool and
        stalling the next docker call for 60s.
        """

        try:
            # typeshed's ``_Fileobj`` Protocol requires write/tell/seek/close,
            # but the ``r|*`` streaming reader only ever calls ``read``
            # forward (verified by the integration suite). Our
            # ``_ChunkStreamReader`` exposes exactly that surface, so the
            # fileobj is type-narrowed here.
            with tarfile.open(fileobj=reader, mode="r|*") as tar:  # type: ignore[call-overload]
                data: bytes | None = None
                for member in tar:
                    if not member.isreg():
                        raise TargetError(kind="not_a_file", target=self.name, path=path)
                    if data is not None:
                        # Already saw a regular file; a second one means the
                        # path was a directory (multi-entry archive).
                        raise TargetError(kind="not_a_file", target=self.name, path=path)
                    if member.size > _READ_FILE_MAX_BYTES:
                        raise TargetError(
                            kind="file_too_large",
                            target=self.name,
                            path=path,
                            size=member.size,
                        )
                    extracted = tar.extractfile(member)
                    data = b"" if extracted is None else _read_capped(extracted, self.name, path)
                if data is None:
                    # No regular file in the archive at all — treat as
                    # not-a-file (directory-only / empty archive).
                    raise TargetError(kind="not_a_file", target=self.name, path=path)
                return data
        finally:
            reader.close()


def _read_capped(reader: Any, target_name: str, path: str) -> bytes:
    """Stream ``reader`` accumulating bytes; abort if total exceeds 10 MiB.

    Unconditional backstop (spec §需求:read_file 固定顺序): we never read
    the whole stream into memory before checking size — we accumulate
    chunk by chunk and raise ``file_too_large`` the moment the running
    total exceeds the cap.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _READ_FILE_MAX_BYTES:
            raise TargetError(
                kind="file_too_large",
                target=target_name,
                path=path,
                size=total,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(value: bytes | None) -> str:
    """Decode an ``exec_run`` demux stream (``None`` → empty string)."""

    if value is None:
        return ""
    return value.decode("utf-8", errors="replace")


def _scrub(exc: BaseException) -> str:
    """Extract a docker exception message and scrub incidental secrets.

    docker exceptions expose ``explanation`` (API errors) or fall back to
    ``str(exc)``. We stringify first, then run the shared
    ``scrub_exception_message`` so any incidentally-embedded home path /
    IP / credential is redacted. The default local socket path
    ``unix:///var/run/docker.sock`` is a public non-secret path and is
    deliberately NOT scrubbed (the shared scrubber does not target it,
    and this spec does not require it).
    """

    # Imported lazily to avoid dragging the agent tooling module into
    # DockerTarget import time (mirrors SSHTarget's lazy import).
    from hostlens.agent.tools_adapter import scrub_exception_message

    explanation = getattr(exc, "explanation", None)
    text = explanation if isinstance(explanation, str) and explanation else str(exc)
    return scrub_exception_message(text)
