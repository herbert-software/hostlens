"""End-to-end ``DockerTarget`` tests against a real ``alpine`` container.

Spec: ``openspec/changes/add-docker-target/specs/docker-execution-target/spec.md``
§需求:DockerTarget 集成测试必须用真实 docker 容器, 无 daemon 时 skip.

The spec explicitly forbids mocking docker-py in this file — the value of
these tests over the unit tests in ``test_docker_unit.py`` is that they
exercise the real docker daemon API (``exec_run`` demux, ``get_archive``
tar streams, real container lifecycle states). A grep-based assertion at
the bottom of this module enforces the no-mock rule (spec §场景:不允许
mock docker-py).

Container topology:

- ``alpine:latest`` image (busybox userland: ``/bin/sh``, POSIX
  ``command -v``, ``truncate``, ``dd`` all present).
- One long-lived ``sleep 3600`` container brought up once per session and
  reused across tests; per-test isolation is achieved with unique file
  paths written into the container before each read.
- A second, deliberately-stopped container backs the
  ``container_not_running`` scenario.

Skip behaviour: if the docker daemon is unreachable the whole module is
skipped (``pytest.skip("docker daemon unavailable")``) so developers /
CI without docker still run the rest of the suite. Every test is marked
``@pytest.mark.docker_integration`` (registered in ``pyproject.toml``).
"""

from __future__ import annotations

import io
import tokenize
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability
from hostlens.targets.docker import DockerTarget

pytestmark = pytest.mark.docker_integration

_TEN_MIB = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Skip-gate + session container fixtures (real docker, never mocked)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client() -> Iterator[Any]:
    """Yield a real docker client or skip the whole module if unreachable."""

    try:
        import docker
    except ImportError:  # pragma: no cover - [docker] extra missing
        pytest.skip("docker SDK unavailable")

    try:
        client = docker.from_env()
        client.ping()
    except Exception:  # docker.errors.DockerException + transport errors
        pytest.skip("docker daemon unavailable")

    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def running_container(docker_client: Any) -> Iterator[str]:
    """Start one ``alpine`` container (``sleep 3600``), reused per session."""

    name = f"hostlens-docker-it-{uuid.uuid4().hex[:8]}"
    container = docker_client.containers.run(
        "alpine:latest",
        ["sleep", "3600"],
        name=name,
        detach=True,
        auto_remove=False,
    )
    try:
        yield name
    finally:
        container.remove(force=True)


@pytest.fixture(scope="session")
def stopped_container(docker_client: Any) -> Iterator[str]:
    """Start then stop an ``alpine`` container for the not-running scenario."""

    name = f"hostlens-docker-it-stopped-{uuid.uuid4().hex[:8]}"
    container = docker_client.containers.run(
        "alpine:latest",
        ["sleep", "3600"],
        name=name,
        detach=True,
        auto_remove=False,
    )
    container.stop(timeout=1)
    try:
        yield name
    finally:
        container.remove(force=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEntry:
    """Structural stand-in for ``DockerEntry`` (the shape ``register`` injects)."""

    def __init__(
        self,
        *,
        container: str,
        docker_host: str | None = None,
        enabled: bool = True,
        name: str = "docker-it",
    ) -> None:
        self.name = name
        self.container = container
        self.docker_host = docker_host
        self.enabled = enabled


def _build_target(*, container: str, name: str = "docker-it") -> DockerTarget:
    target = DockerTarget(name)
    target._entry = _FakeEntry(container=container, name=name)  # type: ignore[assignment]
    return target


def _unique_path(suffix: str = "") -> str:
    return f"/tmp/hostlens-{uuid.uuid4().hex}{suffix}"


async def _write_in_container(
    target: DockerTarget, path: str, *, content: str | None = None, size: int | None = None
) -> None:
    """Materialise a file inside the container via a real ``exec``.

    Exactly one of ``content`` / ``size`` must be given. ``content`` writes
    the literal bytes; ``size`` truncates a sparse file to an exact length
    (fast, no real allocation).
    """

    if content is not None:
        # ``printf %s`` avoids the trailing newline ``echo`` would add.
        result = await target.exec(f"printf %s '{content}' > {path}", timeout=10)
    else:
        result = await target.exec(f"truncate -s {size} {path}", timeout=10)
    assert result.exit_code == 0, f"setup write failed: {result.stderr}"


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


async def test_exec_echo_returns_stdout(running_container: str) -> None:
    """Spec §场景:集成测试通过真实容器跑 echo."""

    target = _build_target(container=running_container)
    result = await target.exec("echo hostlens-probe", timeout=10)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hostlens-probe" in result.stdout


async def test_exec_non_zero_exit_returns_result_not_raise(running_container: str) -> None:
    """Spec §场景:exec 非零退出返回 ExecResult 不 raise."""

    target = _build_target(container=running_container)
    result = await target.exec("exit 3", timeout=10)
    assert result.exit_code == 3
    assert result.timed_out is False


async def test_exec_timeout_returns_timed_out_with_none_exit(running_container: str) -> None:
    """Spec §场景:exec 超时返回 timed_out 且 exit_code 为 None.

    Asserts ONLY the return-value invariant (``timed_out is True`` +
    ``exit_code is None``); per spec §需求 排除项 ② we deliberately do NOT
    claim anything about background-thread / in-container process release.
    """

    target = _build_target(container=running_container)
    result = await target.exec("sleep 60", timeout=2)
    assert result.timed_out is True
    assert result.exit_code is None


async def test_exec_env_injected_via_environment_and_secret_absent_from_cmd(
    running_container: str,
) -> None:
    """Spec §场景:exec 经 environment 注入且不在 cmd string 泄露 + secret 不出现.

    ``$MY_VAR`` must expand to the value injected via ``environment=``
    (proving env reaches the process); a separately-named ``SECRET_TOKEN``
    must NOT appear anywhere in stdout when the command does not reference
    it (proving env is not spliced into the command string).
    """

    target = _build_target(container=running_container)
    result = await target.exec(
        "echo val=$MY_VAR",
        timeout=10,
        env={"MY_VAR": "x", "SECRET_TOKEN": "do-not-leak-abc"},
    )
    assert result.exit_code == 0
    assert "val=x" in result.stdout
    assert "do-not-leak-abc" not in result.stdout
    assert "SECRET_TOKEN" not in result.stdout


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


async def test_read_file_small(running_container: str) -> None:
    """Spec §场景:read_file 读小文件 — round-trips raw bytes via get_archive tar."""

    target = _build_target(container=running_container)
    path = _unique_path("-hello.txt")
    await _write_in_container(target, path, content="hello")
    data = await target.read_file(path)
    assert data == b"hello"


async def test_read_file_exact_10mb_succeeds(running_container: str) -> None:
    """Spec §场景:read_file 恰好 10MB 放行 — boundary is strict ``>``."""

    target = _build_target(container=running_container)
    path = _unique_path("-exact.bin")
    await _write_in_container(target, path, size=_TEN_MIB)
    data = await target.read_file(path)
    assert len(data) == _TEN_MIB


async def test_read_file_over_10mb_raises(running_container: str) -> None:
    """Spec §场景:read_file 超过 10MB raise — file_too_large, no bytes returned."""

    target = _build_target(container=running_container)
    path = _unique_path("-big.bin")
    await _write_in_container(target, path, size=_TEN_MIB + 1)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file(path)
    assert exc_info.value.kind == "file_too_large"
    assert exc_info.value.extra.get("path") == path


async def test_read_file_directory_raises_not_a_file(running_container: str) -> None:
    """Spec §场景:read_file 路径指向目录 raise not_a_file."""

    target = _build_target(container=running_container)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("/etc")
    assert exc_info.value.kind == "not_a_file"


async def test_read_file_symlink_raises_not_a_file(running_container: str) -> None:
    """Spec §场景:read_file 路径指向符号链接 raise not_a_file (no follow)."""

    target = _build_target(container=running_container)
    link = _unique_path("-link")
    target_file = _unique_path("-target.txt")
    await _write_in_container(target, target_file, content="real")
    mk = await target.exec(f"ln -s {target_file} {link}", timeout=10)
    assert mk.exit_code == 0
    with pytest.raises(TargetError) as exc_info:
        await target.read_file(link)
    assert exc_info.value.kind == "not_a_file"


async def test_read_file_directory_with_oversized_file_prefers_not_a_file(
    running_container: str,
) -> None:
    """Spec §场景:read_file 多条目超大归档优先报 not_a_file (非 file_too_large).

    A directory containing a >10 MiB file yields a multi-entry tar whose
    first entry is the directory metadata; the file-type check must fire
    before the size check.
    """

    target = _build_target(container=running_container)
    dirpath = _unique_path("-dir")
    mk = await target.exec(f"mkdir -p {dirpath}", timeout=10)
    assert mk.exit_code == 0
    await _write_in_container(target, f"{dirpath}/big.bin", size=_TEN_MIB + 1)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file(dirpath)
    assert exc_info.value.kind == "not_a_file"


async def test_read_file_relative_path_raises_invalid_path(running_container: str) -> None:
    """Spec §场景:read_file 相对路径 raise invalid_path — no docker request."""

    target = _build_target(container=running_container)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("tmp/x")
    assert exc_info.value.kind == "invalid_path"


async def test_read_file_newline_path_raises_invalid_path(running_container: str) -> None:
    """Spec §场景:read_file 路径含换行 raise invalid_path — no docker request."""

    target = _build_target(container=running_container)
    with pytest.raises(TargetError) as exc_info:
        await target.read_file("/tmp/x\n.txt")
    assert exc_info.value.kind == "invalid_path"


async def test_read_file_absolute_with_dotdot_is_normalized(running_container: str) -> None:
    """Spec §场景:read_file 绝对路径含 `..` 规范化后读取.

    ``/a/../b/c.txt`` must be folded by ``posixpath.normpath`` to
    ``/b/c.txt`` before reaching ``get_archive`` (``PurePosixPath`` would
    NOT fold ``..``). We place a real file at the folded location and read
    via the un-folded path.
    """

    target = _build_target(container=running_container)
    token = uuid.uuid4().hex
    real_dir = f"/tmp/hostlens-{token}-b"
    mk = await target.exec(f"mkdir -p {real_dir}", timeout=10)
    assert mk.exit_code == 0
    await _write_in_container(target, f"{real_dir}/c.txt", content="folded")
    unfolded = f"/tmp/hostlens-{token}-a/../hostlens-{token}-b/c.txt"
    data = await target.read_file(unfolded)
    assert data == b"folded"


async def test_read_file_missing_raises_file_not_found(running_container: str) -> None:
    """Spec §场景:read_file 不存在 raise FileNotFoundError (stdlib, not TargetError)."""

    target = _build_target(container=running_container)
    with pytest.raises(FileNotFoundError):
        await target.read_file(_unique_path("-nope"))


# ---------------------------------------------------------------------------
# container lifecycle failures
# ---------------------------------------------------------------------------


async def test_container_not_found_raises(running_container: str) -> None:
    """Spec §场景:容器不存在 raise container_not_found."""

    target = _build_target(container=f"hostlens-absent-{uuid.uuid4().hex}")
    with pytest.raises(TargetError) as exc_info:
        await target.exec("echo hi", timeout=10)
    assert exc_info.value.kind == "container_not_found"
    assert exc_info.value.target == "docker-it"


async def test_container_not_running_raises(stopped_container: str) -> None:
    """Spec §场景:容器存在但已停止 raise container_not_running (含 status)."""

    target = _build_target(container=stopped_container)
    with pytest.raises(TargetError) as exc_info:
        await target.exec("echo hi", timeout=10)
    assert exc_info.value.kind == "container_not_running"
    assert exc_info.value.extra.get("status") != "running"


# ---------------------------------------------------------------------------
# capabilities lazy probe
# ---------------------------------------------------------------------------


async def test_capabilities_probed_only_after_first_exec(running_container: str) -> None:
    """Spec §场景:DockerTarget capabilities 首次 exec 后才探测.

    Before the first ``exec`` the set is exactly ``{SHELL, FILE_READ}``;
    after a successful ``exec`` it reflects the probe. ``alpine`` has
    neither ``systemctl`` nor ``docker``, so the probe adds nothing — the
    point is the probe ran (``_probed_caps`` populated) without regressing
    the baseline.
    """

    target = _build_target(container=running_container)
    assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}
    assert target._probed_caps is None

    result = await target.exec("echo hi", timeout=10)
    assert result.exit_code == 0

    assert target._probed_caps is not None
    assert Capability.SHELL in target.capabilities
    assert Capability.FILE_READ in target.capabilities
    # alpine ships neither systemctl nor a docker client.
    assert Capability.SYSTEMD not in target.capabilities
    assert Capability.DOCKER_CLI not in target.capabilities


# ---------------------------------------------------------------------------
# guard: no docker-py mocks anywhere in this file
# ---------------------------------------------------------------------------


def test_no_docker_mocks_present() -> None:
    """Spec §场景:不允许 mock docker-py.

    Hard guard so a refactor cannot quietly start mocking the docker SDK
    here (which would defeat the value of integration tests). We tokenise
    the source and flag any ``patch`` / ``mocker.patch`` /
    ``monkeypatch.setattr`` / ``patch.object`` callable whose argument
    list contains a string mentioning ``docker`` — covering
    ``patch("docker...")``, ``patch("hostlens.targets.docker.docker")``,
    ``monkeypatch.setattr(docker, ...)`` and ``patch.object(docker, ...)``
    alike. Scanning tokens (not raw substrings) keeps the guard from
    tripping on its own docstring.
    """

    source = Path(__file__).read_text()
    mock_callables = {"patch", "setattr", "object"}
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))

    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string not in mock_callables:
            continue
        # Accept both ``patch(...)`` and ``patch.object(...)`` /
        # ``monkeypatch.setattr(...)`` — the matched NAME must be directly
        # followed by ``(`` (a call), so ``patch.object`` is caught at the
        # ``object`` token and ``setattr`` at its own token.
        if i + 1 >= len(tokens) or tokens[i + 1].string != "(":
            continue
        # Walk the matched argument list (balanced parens) and inspect
        # every token for a "docker"-bearing reference.
        depth = 0
        j = i + 1
        while j < len(tokens):
            t = tokens[j]
            if t.type == tokenize.OP and t.string == "(":
                depth += 1
            elif t.type == tokenize.OP and t.string == ")":
                depth -= 1
                if depth == 0:
                    break
            else:
                text = t.string.strip("\"'")
                if t.type in (tokenize.STRING, tokenize.NAME) and "docker" in text:
                    raise AssertionError(
                        f"docker mock detected at line {tok.start[0]}: "
                        f"{tok.string}(... {t.string} ...); integration tests "
                        "must use the real docker daemon "
                        "(spec §场景:不允许 mock docker-py)."
                    )
            j += 1
