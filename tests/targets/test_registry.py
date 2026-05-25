"""Tests for ``hostlens.targets.registry`` — TargetRegistry + factory.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`TargetRegistry` 必须按 name 索引且同时持有 target 实例与配置元数据.

Group D tasks covered here:

- 4.1   ``TargetRegistry`` API (register / get / get_entry / names /
        list / list_entries; name mismatch + illegal name + duplicate
        raise).
- 4.1b  Disabled-target behaviour — ``exec`` / ``read_file`` on a
        disabled target raise ``target_disabled`` and never touch the
        underlying transport (subprocess / asyncssh).
- 4.4   ``build_registry_from_config`` round trip from
        ``TargetsConfig``.
- 4.5   Secret scrubbing — ``repr(SSHEntry)`` does not leak
        ``password`` / ``passphrase``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hostlens.core.config import Settings
from hostlens.core.exceptions import TargetError
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import (
    LocalEntry,
    SSHEntry,
    load_targets_config,
)
from hostlens.targets.local import LocalTarget
from hostlens.targets.registry import (
    TargetRegistry,
    build_registry_from_config,
)
from hostlens.targets.ssh import SSHTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_local_entry(name: str = "my-local", *, enabled: bool = True) -> LocalEntry:
    return LocalEntry(name=name, type="local", enabled=enabled)


def _make_ssh_entry(name: str = "my-ssh", *, enabled: bool = True) -> SSHEntry:
    return SSHEntry(
        name=name,
        type="ssh",
        host="10.0.0.5",
        user="alice",
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Task 4.1 — basic registry API
# ---------------------------------------------------------------------------


def test_register_then_get_round_trip() -> None:
    """Happy path: register two targets, read them back by name."""

    registry = TargetRegistry()
    local = LocalTarget("my-local")
    ssh = SSHTarget("my-ssh")
    registry.register(local, _make_local_entry("my-local"))
    registry.register(ssh, _make_ssh_entry("my-ssh"))

    assert registry.get("my-local") is local
    assert registry.get("my-ssh") is ssh
    assert registry.names() == {"my-local", "my-ssh"}


def test_get_missing_raises_keyerror() -> None:
    """Spec §场景:get 未找到 raise KeyError — **not** TargetError.

    Lookup misses are not Hostlens business errors; callers are
    expected to either ``try/except KeyError`` or guard with ``name in
    registry.names()``.
    """

    registry = TargetRegistry()
    with pytest.raises(KeyError):
        registry.get("nope")
    with pytest.raises(KeyError):
        registry.get_entry("nope")


def test_list_returns_targets_in_lexicographic_order() -> None:
    """Spec §场景:list 按 name 字典序.

    Determinism matters for snapshot tests + the Tool Registry
    projection of ``TargetSummary`` — non-deterministic order would
    make ``list_targets`` ToolSpec output unstable.
    """

    registry = TargetRegistry()
    registry.register(LocalTarget("zeta"), _make_local_entry("zeta"))
    registry.register(LocalTarget("alpha"), _make_local_entry("alpha"))
    registry.register(LocalTarget("beta"), _make_local_entry("beta"))

    assert [t.name for t in registry.list()] == ["alpha", "beta", "zeta"]
    assert [e.name for e in registry.list_entries()] == ["alpha", "beta", "zeta"]


def test_register_duplicate_name_raises() -> None:
    """Spec §场景:register 冲突 raise."""

    registry = TargetRegistry()
    registry.register(LocalTarget("prod-web"), _make_local_entry("prod-web"))
    with pytest.raises(TargetError) as exc:
        registry.register(LocalTarget("prod-web"), _make_local_entry("prod-web"))
    assert exc.value.kind == "duplicate_target"
    assert exc.value.target == "prod-web"


def test_register_rejects_target_name_entry_name_mismatch() -> None:
    """Spec §场景:register 拒绝 target.name 与 entry.name 不一致.

    Binding metadata to the wrong target instance would mislabel
    ``list_targets`` output and could even attach the wrong credentials
    on the SSH side. The check must happen *before* anything is added
    to either index so the registry stays clean on rejection.
    """

    registry = TargetRegistry()
    target = LocalTarget("a-good")
    entry = LocalEntry(name="another-name", type="local")
    with pytest.raises(TargetError) as exc:
        registry.register(target, entry)
    assert exc.value.kind == "target_entry_name_mismatch"
    assert exc.value.target == "a-good"
    assert exc.value.extra["entry_name"] == "another-name"
    # Registry state unchanged — no partial registration.
    assert registry.names() == set()


def test_register_rejects_illegal_name_via_mock_target() -> None:
    """Spec §场景:register 拒绝非法 name target — third regex defence layer.

    Constructing ``LocalTarget(name="Prod-Web")`` would normally raise
    on its own (the per-implementation guard fires first); to test the
    registry layer in isolation we hand it a duck-typed ``ExecutionTarget``
    whose ``name`` deliberately bypasses the constructor regex. The
    matching ``TargetEntry`` would normally also reject the bad name —
    we forge a ``LocalEntry`` via ``model_construct`` to bypass
    Pydantic validation, mirroring the "test bypasses every other
    layer" hypothetical the spec calls out.
    """

    class _Mock:
        name = "Prod-Web"  # illegal: uppercase letters
        type = "local"

        def __init__(self) -> None:
            # Per-instance set so RUF012 (mutable class attr) is happy
            # and so two instances never share capability state.
            self.capabilities: set[object] = set()

    bad_entry = LocalEntry.model_construct(  # type: ignore[call-arg]
        name="Prod-Web", type="local"
    )
    registry = TargetRegistry()
    with pytest.raises(TargetError) as exc:
        registry.register(_Mock(), bad_entry)  # type: ignore[arg-type]
    assert exc.value.kind == "invalid_target_name"


def test_register_injects_entry_onto_target_instance() -> None:
    """``register`` MUST set ``target._entry = entry`` so the runtime
    code can read ``enabled`` / ``connect_timeout`` / credentials off
    the target. See spec §需求:``TargetRegistry`` final paragraph."""

    registry = TargetRegistry()
    target = LocalTarget("my-local")
    assert target._entry is None
    entry = _make_local_entry("my-local")
    registry.register(target, entry)
    assert target._entry is entry


def test_register_protocol_satisfied() -> None:
    """``LocalTarget`` / ``SSHTarget`` must structurally satisfy
    ``ExecutionTarget`` — registry callers rely on it."""

    assert isinstance(LocalTarget("x"), ExecutionTarget)
    assert isinstance(SSHTarget("y"), ExecutionTarget)


# ---------------------------------------------------------------------------
# Task 4.1b — disabled-target behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_target_disabled_via_registry_blocks_exec() -> None:
    """Spec §需求:``TargetsConfig`` enabled 行为约定 — ``exec`` on a
    disabled LocalTarget raises ``target_disabled`` and does NOT spawn
    a subprocess.
    """

    registry = TargetRegistry()
    target = LocalTarget("paused")
    registry.register(target, _make_local_entry("paused", enabled=False))

    with (
        patch("hostlens.targets.local.asyncio.create_subprocess_shell") as mock_subprocess,
        pytest.raises(TargetError) as exc,
    ):
        await target.exec("echo nope", timeout=5)
    assert exc.value.kind == "target_disabled"
    assert mock_subprocess.call_count == 0


@pytest.mark.asyncio
async def test_local_target_disabled_via_registry_blocks_read_file(
    tmp_path: Path,
) -> None:
    """Same guarantee for ``read_file`` — disabled targets must not
    touch the filesystem."""

    sample = tmp_path / "f.txt"
    sample.write_text("payload")

    registry = TargetRegistry()
    target = LocalTarget("paused")
    registry.register(target, _make_local_entry("paused", enabled=False))

    with pytest.raises(TargetError) as exc:
        await target.read_file(str(sample))
    assert exc.value.kind == "target_disabled"


@pytest.mark.asyncio
async def test_local_target_no_entry_treated_as_enabled(tmp_path: Path) -> None:
    """``_entry is None`` (standalone constructor) is the documented
    test-friendly escape hatch — enabled check must NOT block."""

    target = LocalTarget("solo")
    assert target._entry is None
    result = await target.exec("echo ok", timeout=5)
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_local_target_enabled_true_unaffected(tmp_path: Path) -> None:
    """``enabled=True`` is the default and must not interfere with exec."""

    registry = TargetRegistry()
    target = LocalTarget("alive")
    registry.register(target, _make_local_entry("alive", enabled=True))

    result = await target.exec("echo ok", timeout=5)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_ssh_target_disabled_via_registry_blocks_exec() -> None:
    """Mirror of the LocalTarget assertion — disabled SSHTarget must
    NOT invoke ``asyncssh.connect``."""

    registry = TargetRegistry()
    target = SSHTarget("paused-ssh")
    registry.register(target, _make_ssh_entry("paused-ssh", enabled=False))

    with (
        patch("hostlens.targets.ssh.asyncssh.connect") as mock_connect,
        pytest.raises(TargetError) as exc,
    ):
        await target.exec("uptime", timeout=5)
    assert exc.value.kind == "target_disabled"
    assert mock_connect.call_count == 0


@pytest.mark.asyncio
async def test_ssh_target_disabled_via_registry_blocks_read_file() -> None:
    registry = TargetRegistry()
    target = SSHTarget("paused-ssh")
    registry.register(target, _make_ssh_entry("paused-ssh", enabled=False))

    with (
        patch("hostlens.targets.ssh.asyncssh.connect") as mock_connect,
        pytest.raises(TargetError) as exc,
    ):
        await target.read_file("/etc/hostname")
    assert exc.value.kind == "target_disabled"
    assert mock_connect.call_count == 0


# ---------------------------------------------------------------------------
# Task 4.4 — build_registry_from_config
# ---------------------------------------------------------------------------


def test_build_registry_from_config_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: yaml → ``TargetsConfig`` → ``TargetRegistry``.

    Asserts both names() and that the SSHTarget instance came back
    with its entry injected (so downstream connect kwargs can be
    built without further registry lookups).
    """

    monkeypatch.setenv("BUILD_REG_TEST_PWD", "supersecret")
    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [
                    {"name": "my-local", "type": "local"},
                    {
                        "name": "my-ssh",
                        "type": "ssh",
                        "host": "10.0.0.5",
                        "user": "alice",
                        "password": "${BUILD_REG_TEST_PWD}",
                    },
                ],
            }
        )
    )
    config = load_targets_config(cfg_path)
    settings = Settings()
    registry = build_registry_from_config(config, settings)

    assert registry.names() == {"my-local", "my-ssh"}
    ssh = registry.get("my-ssh")
    assert isinstance(ssh, SSHTarget)
    assert ssh._entry is not None
    assert ssh._entry.host == "10.0.0.5"
    assert ssh._entry.password == "supersecret"


def test_build_registry_settings_idle_timeout_visible_to_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SSHTarget._idle_timeout()`` must reflect the env-driven Settings.

    The factory does not need to plumb ``Settings`` into SSHTarget
    construction directly (the target reads ``Settings()`` lazily on
    first ``exec``); this test asserts the env-var override path is
    intact end-to-end.
    """

    monkeypatch.setenv("HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS", "120")
    target = SSHTarget("solo-ssh")
    # ``_idle_timeout`` reads from a freshly-built ``Settings()`` so the
    # env override surfaces — keep this in lockstep with SSHTarget's
    # docstring contract about lazy Settings construction.
    assert target._idle_timeout() == 120


def test_build_registry_preserves_disabled_entries(tmp_path: Path) -> None:
    """Spec §需求:``TargetsConfig`` enabled — registry assembly does
    NOT filter disabled targets so doctor / list_targets can see them."""

    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [
                    {"name": "alive", "type": "local"},
                    {"name": "sleeping", "type": "local", "enabled": False},
                ],
            }
        )
    )
    config = load_targets_config(cfg_path)
    registry = build_registry_from_config(config, Settings())
    assert registry.names() == {"alive", "sleeping"}
    sleeping_entry = registry.get_entry("sleeping")
    assert sleeping_entry.enabled is False


# ---------------------------------------------------------------------------
# Task 4.5 — secret scrub
# ---------------------------------------------------------------------------


_LITERAL_SECRET = "literal-test-secret-do-not-leak"


def test_ssh_entry_repr_masks_password() -> None:
    """``repr(SSHEntry)`` MUST NOT include the literal password string.

    pytest failure messages, structlog ``repr=True`` rendering, and
    ``print(entry)`` would otherwise leak credentials to logs.
    """

    entry = SSHEntry(
        name="prod-web",
        type="ssh",
        host="10.0.0.5",
        user="alice",
        password=_LITERAL_SECRET,
    )
    rendered = repr(entry)
    assert _LITERAL_SECRET not in rendered
    assert "***" in rendered
    # The attribute itself is preserved unredacted — only the
    # representation is masked.
    assert entry.password == _LITERAL_SECRET


def test_ssh_entry_repr_masks_passphrase() -> None:
    entry = SSHEntry(
        name="prod-web",
        type="ssh",
        host="10.0.0.5",
        user="alice",
        passphrase=_LITERAL_SECRET,
    )
    rendered = repr(entry)
    assert _LITERAL_SECRET not in rendered
    assert entry.passphrase == _LITERAL_SECRET


def test_ssh_entry_str_masks_password() -> None:
    """Pydantic's default ``__str__`` delegates to ``__repr__`` for
    ``BaseModel``, but we double-check here so a future Pydantic
    change does not silently regress the scrub."""

    entry = SSHEntry(
        name="prod-web",
        type="ssh",
        host="10.0.0.5",
        user="alice",
        password=_LITERAL_SECRET,
    )
    assert _LITERAL_SECRET not in str(entry)


def test_load_to_registry_does_not_leak_secret_in_repr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end scrub check: yaml → load → registry → ``repr`` of
    any object the test could plausibly log must not contain the
    literal secret string."""

    monkeypatch.setenv("HOSTLENS_TEST_SECRET", _LITERAL_SECRET)
    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [
                    {
                        "name": "prod-web",
                        "type": "ssh",
                        "host": "10.0.0.5",
                        "user": "alice",
                        "password": "${HOSTLENS_TEST_SECRET}",
                    }
                ],
            }
        )
    )
    config = load_targets_config(cfg_path)
    registry = build_registry_from_config(config, Settings())

    entry = registry.get_entry("prod-web")
    target = registry.get("prod-web")

    for rendered in (
        repr(entry),
        str(entry),
        repr(target),
    ):
        assert _LITERAL_SECRET not in rendered, f"secret leaked through: {rendered!r}"
