"""Tests for ``promote_candidate`` / ``ProbeResult`` / ``TargetProbe``.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
§需求:`CandidateTarget` 必须先提升... / §需求:`TargetProbe` 必须复用
ExecutionTarget、先 exec 判可达、产可序列化脱敏 `ProbeResult`.

The reachability tests probe a real ``local`` target (non-root, no docker /
no SSH — CI-safe). The error-mapping / redaction tests drive a fake target
that raises the various ``TargetError.kind`` values so the closed-set
``error_kind`` mapping is exercised without a network.
"""

from __future__ import annotations

import asyncio

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.config import LocalEntry, SSHEntry
from hostlens.targets.inventory.models import CandidateTarget
from hostlens.targets.probe import (
    ProbeError,
    ProbeResult,
    TargetProbe,
    promote_candidate,
)

# ---------------------------------------------------------------------------
# promote_candidate
# ---------------------------------------------------------------------------


def test_promote_local_candidate_yields_local_entry() -> None:
    candidate = CandidateTarget(name="demo", type="local")
    entry = promote_candidate(candidate)
    assert isinstance(entry, LocalEntry)
    assert entry.name == "demo"
    assert entry.type == "local"


def test_promote_ssh_candidate_yields_ssh_entry_with_no_inline_password() -> None:
    """Spec: promoted ``SSHEntry.password`` is **always None** (env-ref only)."""

    candidate = CandidateTarget(
        name="prod",
        type="ssh",
        host="10.0.0.5",
        user="alice",
        port=2222,
        password_env="MY_PWD",
        passphrase_env="MY_PASS",
        key_path="/tmp/id",
    )
    entry = promote_candidate(candidate)
    assert isinstance(entry, SSHEntry)
    assert entry.host == "10.0.0.5"
    assert entry.user == "alice"
    assert entry.port == 2222
    assert entry.key_path == "/tmp/id"
    # Credentials never inline into the entry — only env refs (threaded
    # separately to save_targets_config) carry them.
    assert entry.password is None
    assert entry.passphrase is None


async def test_promoted_ssh_entry_exec_does_not_raise_ssh_no_entry() -> None:
    """Spec §场景:合法候选提升后经 registry 构造注入 `_entry`、可 exec.

    A bare ``SSHTarget(name=...)`` (``_entry=None``) raises ``ssh_no_entry``
    on first exec. After promotion + ``build_one_target`` the target is
    registered with its entry, so exec dials out (and fails with a *connect*
    error against the unroutable host) rather than ``ssh_no_entry``.
    """

    from hostlens.targets.registry import build_one_target

    candidate = CandidateTarget(
        name="unroutable",
        type="ssh",
        # RFC5737 TEST-NET-1, guaranteed unroutable — connect fails fast.
        host="192.0.2.1",
        user="nobody",
    )
    entry = promote_candidate(candidate)
    target = build_one_target(entry, Settings())
    try:
        with pytest.raises(TargetError) as exc:
            await target.exec("true", timeout=2)
        assert exc.value.kind != "ssh_no_entry"
    finally:
        await target.aclose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ProbeResult — closed enum + fingerprint allowlist + round-trip
# ---------------------------------------------------------------------------


def test_probe_result_rejects_unknown_error_kind() -> None:
    """The closed ``Literal`` set rejects a non-member error_kind at construction."""

    with pytest.raises(ValueError, match="error_kind"):
        ProbeResult(reachable=False, error_kind="bogus")  # type: ignore[arg-type]


def test_probe_result_rejects_disallowed_fingerprint_key() -> None:
    """``hostname`` (and any non-allowlist key) is rejected."""

    with pytest.raises(ValueError, match="fingerprint keys must be a subset"):
        ProbeResult(reachable=True, fingerprint={"hostname": "secret-box"})


def test_probe_result_accepts_allowlist_fingerprint_keys() -> None:
    result = ProbeResult(
        reachable=True,
        fingerprint={"os": "Debian", "kernel": "Linux 6", "arch": "x86_64", "runtime": "podman"},
    )
    assert set(result.fingerprint) == {"os", "kernel", "arch", "runtime"}


def test_probe_result_json_round_trip() -> None:
    """Spec §场景:ProbeResult 可 JSON round-trip."""

    result = ProbeResult(
        reachable=True,
        capabilities=["shell", "ssh"],
        fingerprint={"os": "Debian 13", "arch": "aarch64"},
        error_kind=None,
    )
    restored = ProbeResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_probe_error_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match=r"ProbeError\.kind must be one of"):
        ProbeError("bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TargetProbe.probe — real local target reachability
# ---------------------------------------------------------------------------


async def test_probe_local_target_is_reachable() -> None:
    """Spec §场景:exec 返回且未超时判 reachable.

    Reachability is decided by ``exec returned and not timed_out`` — NOT by
    capabilities being non-empty (a local target has non-empty capabilities
    at construction, so that would be a tautology).
    """

    probe = TargetProbe(Settings(), timeout=10)
    result = await probe.probe(LocalEntry(name="demo-localhost", type="local"))

    assert result.reachable is True
    assert result.error_kind is None
    # capabilities come from target.capabilities (lazy-probe), projected to
    # enum values. SHELL + FILE_READ are the static baseline.
    assert Capability.SHELL.value in result.capabilities
    assert Capability.FILE_READ.value in result.capabilities
    # podman is never a Capability member; if a runtime exists it is in
    # fingerprint.runtime, not capabilities.
    assert "podman" not in result.capabilities


# ---------------------------------------------------------------------------
# Fake-target probing — timeout / error mapping / fingerprint / redaction
# ---------------------------------------------------------------------------


class _FakeTarget:
    """Minimal ExecutionTarget stand-in driving probe outcomes deterministically.

    ``exec`` either returns a caller-supplied ``ExecResult`` or raises a
    caller-supplied ``TargetError``. ``capabilities`` is fixed. No network.
    """

    type = "ssh"

    def __init__(
        self,
        name: str,
        *,
        result: ExecResult | None = None,
        error: TargetError | None = None,
        capabilities: set[Capability] | None = None,
    ) -> None:
        self.name = name
        self._result = result
        self._error = error
        self.capabilities = capabilities or {Capability.SSH, Capability.SHELL, Capability.FILE_READ}
        self.aclosed = False

    async def exec(
        self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused by probe
        raise NotImplementedError

    async def aclose(self) -> None:
        self.aclosed = True


def _patch_build_one_target(monkeypatch: pytest.MonkeyPatch, target: _FakeTarget) -> None:
    """Make ``probe`` use ``target`` instead of constructing a real one."""

    import hostlens.targets.probe as probe_mod

    monkeypatch.setattr(probe_mod, "build_one_target", lambda entry, settings: target)


async def test_probe_timed_out_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:exec 超时判 failed_probe(不误判 reachable).

    A timed-out ExecResult does NOT raise — probe must still classify it as
    unreachable + ``timeout`` rather than treating "exec did not raise" as
    reachable.
    """

    timed = ExecResult(exit_code=None, stdout="", stderr="", duration_seconds=2.0, timed_out=True)
    target = _FakeTarget("slow", result=timed)
    _patch_build_one_target(monkeypatch, target)

    probe = TargetProbe(Settings())
    result = await probe.probe(SSHEntry(name="slow", type="ssh", host="h", user="u"))

    assert result.reachable is False
    assert result.error_kind == "timeout"
    assert target.aclosed is True


async def test_probe_nonzero_exit_is_still_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit_code is still reachable (host can log in + run)."""

    res = ExecResult(exit_code=1, stdout="", stderr="boom", duration_seconds=0.1, timed_out=False)
    target = _FakeTarget("minimal", result=res)
    _patch_build_one_target(monkeypatch, target)

    probe = TargetProbe(Settings())
    result = await probe.probe(SSHEntry(name="minimal", type="ssh", host="h", user="u"))

    assert result.reachable is True
    assert result.error_kind is None


@pytest.mark.parametrize(
    ("target_kind", "expected"),
    [
        ("ssh_connect_timeout", "timeout"),
        ("ssh_auth_failed", "auth_failed"),
        ("ssh_connect_failed", "unreachable"),
        ("ssh_connection_lost", "unreachable"),
        ("ssh_no_entry", "exec_failed"),
        ("target_disabled", "exec_failed"),
        ("some_future_unlisted_kind", "exec_failed"),
    ],
)
async def test_probe_maps_target_error_kind_to_closed_set(
    monkeypatch: pytest.MonkeyPatch, target_kind: str, expected: str
) -> None:
    """Spec §需求:`TargetError.kind` 全映射表 + fallback→exec_failed.

    Every ``TargetError.kind`` (incl. an unlisted future kind) maps to one of
    the four closed ``ProbeErrorKind`` values.
    """

    err = TargetError(kind=target_kind, target="some-host", host="10.0.0.9")
    target = _FakeTarget("err", error=err)
    _patch_build_one_target(monkeypatch, target)

    probe = TargetProbe(Settings())
    result = await probe.probe(SSHEntry(name="err", type="ssh", host="h", user="u"))

    assert result.reachable is False
    assert result.error_kind == expected
    # Redaction: error_kind is a closed enum, never the host / extra fields.
    assert result.error_kind in {"unreachable", "auth_failed", "timeout", "exec_failed"}
    assert "10.0.0.9" not in str(result.model_dump())


async def test_probe_fingerprint_excludes_hostname_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:fingerprint 不含 hostname + 值截断 + 控制字符剥离.

    The probe stdout carries a hostname line and a PRETTY_NAME with an
    embedded newline + an over-long value; the fingerprint must drop the
    hostname, strip the control char, and truncate.
    """

    long_os = "X" * 200
    stdout = (
        "my-secret-internal-box\n"
        "Linux 6.1.0-rpi7-rpi-v8 aarch64\n"
        f'PRETTY_NAME="{long_os}"\n'
        "/usr/bin/podman\n"
    )
    res = ExecResult(exit_code=0, stdout=stdout, stderr="", duration_seconds=0.1, timed_out=False)
    target = _FakeTarget("box", result=res, capabilities={Capability.SSH, Capability.SHELL})
    _patch_build_one_target(monkeypatch, target)

    probe = TargetProbe(Settings())
    result = await probe.probe(SSHEntry(name="box", type="ssh", host="h", user="u"))

    assert result.reachable is True
    assert "hostname" not in result.fingerprint
    assert "my-secret-internal-box" not in result.fingerprint.values()
    assert result.fingerprint["kernel"] == "Linux 6.1.0-rpi7-rpi-v8"
    assert result.fingerprint["arch"] == "aarch64"
    assert result.fingerprint["runtime"] == "podman"
    assert len(result.fingerprint["os"]) <= 64


# ---------------------------------------------------------------------------
# TargetProbe.probe_many — concurrency bound + isolation + redaction
# ---------------------------------------------------------------------------


async def test_probe_many_isolates_failures_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:探活失败隔离且 error_kind 为闭集枚举.

    One unreachable host neither raises nor affects the others; results are
    index-aligned with the input entries.
    """

    ok = ExecResult(
        exit_code=0,
        stdout="host\nLinux 6 x86_64\n",
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
    )

    targets = {
        "good": _FakeTarget("good", result=ok),
        "bad": _FakeTarget("bad", error=TargetError(kind="ssh_connect_failed", host="10.0.0.1")),
    }

    import hostlens.targets.probe as probe_mod

    monkeypatch.setattr(probe_mod, "build_one_target", lambda entry, settings: targets[entry.name])

    probe = TargetProbe(Settings())
    entries: list[LocalEntry | SSHEntry] = [
        SSHEntry(name="good", type="ssh", host="h", user="u"),
        SSHEntry(name="bad", type="ssh", host="h", user="u"),
    ]
    results = await probe.probe_many(entries)

    assert results[0].reachable is True
    assert results[1].reachable is False
    assert results[1].error_kind == "unreachable"
    assert "10.0.0.1" not in str(results[1].model_dump())


async def test_probe_many_respects_concurrency_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:并发探测受限流约束.

    With ``concurrency=2`` and a probe that records peak in-flight count, the
    semaphore must keep the peak at or below 2 even with more candidates.
    """

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    res = ExecResult(
        exit_code=0, stdout="h\nLinux 6 x86\n", stderr="", duration_seconds=0.0, timed_out=False
    )

    class _SlowTarget(_FakeTarget):
        async def exec(
            self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
        ) -> ExecResult:
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return res

    import hostlens.targets.probe as probe_mod

    monkeypatch.setattr(
        probe_mod,
        "build_one_target",
        lambda entry, settings: _SlowTarget(entry.name, result=res),
    )

    probe = TargetProbe(Settings(), concurrency=2)
    entries: list[LocalEntry | SSHEntry] = [
        SSHEntry(name=f"h{i}", type="ssh", host="h", user="u") for i in range(6)
    ]
    results = await probe.probe_many(entries)

    assert len(results) == 6
    assert all(r.reachable for r in results)
    assert peak <= 2


def test_probe_isolates_unexpected_construction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected error (e.g. OSError) is isolated to exec_failed, not raised."""
    import hostlens.targets.probe as probe_mod

    def _boom(entry: object, settings: object) -> object:
        raise OSError("subprocess creation failed")

    monkeypatch.setattr(probe_mod, "build_one_target", _boom)
    result = asyncio.run(TargetProbe(Settings()).probe(LocalEntry(name="x", type="local")))
    assert result.reachable is False
    assert result.error_kind == "exec_failed"


def test_probe_many_isolates_one_bad_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """One host raising an unexpected error must not abort the batch."""
    import hostlens.targets.probe as probe_mod

    real = probe_mod.build_one_target

    def _selective(entry: object, settings: object) -> object:
        if getattr(entry, "name", None) == "bad":
            raise OSError("boom")
        return real(entry, settings)

    monkeypatch.setattr(probe_mod, "build_one_target", _selective)
    results = asyncio.run(
        TargetProbe(Settings()).probe_many(
            [LocalEntry(name="bad", type="local"), LocalEntry(name="good", type="local")]
        )
    )
    assert len(results) == 2
    assert results[0].reachable is False and results[0].error_kind == "exec_failed"
    assert results[1].reachable is True


def test_promote_ssh_defaults_user_to_os_user() -> None:
    """Missing ``User`` defaults to the OS username (OpenSSH), never empty."""
    import getpass

    entry = promote_candidate(CandidateTarget(name="x", type="ssh", host="1.1.1.1"))
    assert isinstance(entry, SSHEntry)
    assert entry.user == getpass.getuser()
    assert entry.user != ""


def test_promote_ssh_missing_host_raises() -> None:
    with pytest.raises(ValueError, match="host"):
        promote_candidate(CandidateTarget(name="x", type="ssh", host=None))
