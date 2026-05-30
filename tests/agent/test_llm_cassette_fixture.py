"""Tests for the ``llm_cassette`` fixture + ``HOSTLENS_LLM_MODE`` three-state.

Covers (group E / tasks 5.3 + 5.4):

- spec §需求:`HOSTLENS_LLM_MODE` 必须只在测试 fixture 内分派 backend —— 缺省=replay
  / 非法值 fail-fast / 生产 `create_backend` 不感知 mode.
- spec §需求:`llm_cassette(name)` fixture 必须按显式名映射 cassette —— replay 返回
  `PlaybackBackend` / 文件缺失报错含路径 / record 缺 key fail / live 不写盘.
- spec §需求:record 模式必须由 fixture 强制在装配层拒绝真实 target —— fixture 强制
  守门无法绕过 (record + 含 SSH 的 registry → 取 backend 即 raise) / record 缺
  `target_registry` 即 fail.

These exercise the fixture at the assembly layer. record-mode tests use
``monkeypatch`` to set env and never issue a real API request — they assert
the fixture-layer behavior (missing key fails, missing registry fails, SSH
registry raises) before any inner call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _CASSETTES_DIR, _resolve_llm_mode

from hostlens.agent.backend import create_backend
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.core.config import BackendSettings, Settings
from hostlens.targets.config import LocalEntry, SSHEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config

# A name guaranteed to have a committed cassette in the repo.
_EXISTING_CASSETTE_NAME = "list_inspectors_demo"


def _ssh_registry() -> object:
    return build_registry_from_config(
        TargetsConfig(
            version="1",
            targets=[SSHEntry(name="prod", type="ssh", host="h.internal", user="u")],
        ),
        Settings(),
    )


def _synthetic_registry() -> object:
    return build_registry_from_config(
        TargetsConfig(
            version="1",
            targets=[LocalEntry(name="syn", type="local", tags=["cassette-synthetic"])],
        ),
        Settings(),
    )


# --- §需求:HOSTLENS_LLM_MODE ------------------------------------------------


def test_default_mode_is_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTLENS_LLM_MODE", raising=False)
    assert _resolve_llm_mode() == "replay"


def test_empty_mode_is_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_LLM_MODE", "")
    assert _resolve_llm_mode() == "replay"


def test_illegal_mode_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_LLM_MODE", "bogus")
    with pytest.raises(ValueError) as exc_info:
        _resolve_llm_mode()
    message = str(exc_info.value)
    assert "replay" in message
    assert "record" in message
    assert "live" in message


def test_production_factory_ignores_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """§场景:生产工厂不感知 mode —— create_backend dispatches only on
    settings.backend.type, even with HOSTLENS_LLM_MODE=record set."""

    monkeypatch.setenv("HOSTLENS_LLM_MODE", "record")
    settings = Settings(
        backend=BackendSettings(type="anthropic_api", api_key="sk-test-12345678"),
    )
    backend = create_backend(settings)
    # Despite mode=record, the factory returns the type-driven backend, NOT a
    # RecordingBackend.
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend.name == "anthropic_api"


# --- §需求:llm_cassette(name) 按显式名映射 ---------------------------------


def test_replay_returns_playback_backend(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
) -> None:
    monkeypatch.delenv("HOSTLENS_LLM_MODE", raising=False)
    backend = llm_cassette(_EXISTING_CASSETTE_NAME)
    assert isinstance(backend, PlaybackBackend)


def test_replay_missing_cassette_reports_path(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
) -> None:
    monkeypatch.delenv("HOSTLENS_LLM_MODE", raising=False)
    with pytest.raises(FileNotFoundError) as exc_info:
        llm_cassette("definitely_missing_cassette")
    message = str(exc_info.value)
    expected = str(_CASSETTES_DIR / "definitely_missing_cassette.jsonl")
    assert expected in message


def test_record_missing_api_key_fails(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
) -> None:
    monkeypatch.setenv("HOSTLENS_LLM_MODE", "record")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pytest.fail.Exception) as exc_info:
        llm_cassette("foo", target_registry=_synthetic_registry())
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_live_returns_anthropic_backend_no_write(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOSTLENS_LLM_MODE", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live-12345678")
    before = set(_CASSETTES_DIR.iterdir())
    backend = llm_cassette("never_written")
    assert isinstance(backend, AnthropicAPIBackend)
    # live path writes no cassette file.
    after = set(_CASSETTES_DIR.iterdir())
    assert before == after
    assert not (_CASSETTES_DIR / "never_written.jsonl").exists()


# --- §需求:record 模式必须由 fixture 强制守门 ------------------------------


def test_record_with_ssh_registry_raises_on_acquire(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
) -> None:
    """§场景:fixture 强制守门, 无法绕过 —— record + SSH registry raises when
    obtaining the backend (guard runs before RecordingBackend is returned),
    without HOSTLENS_ALLOW_REAL_TARGET_RECORD set."""

    monkeypatch.setenv("HOSTLENS_LLM_MODE", "record")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-12345678")
    monkeypatch.delenv("HOSTLENS_ALLOW_REAL_TARGET_RECORD", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        llm_cassette("ssh_scenario", target_registry=_ssh_registry())
    assert "HOSTLENS_ALLOW_REAL_TARGET_RECORD=1" in str(exc_info.value)


def test_record_missing_target_registry_fails(
    monkeypatch: pytest.MonkeyPatch,
    llm_cassette,
) -> None:
    """§场景:record 模式缺 target_registry 即 fail —— must not return an
    un-guarded RecordingBackend."""

    monkeypatch.setenv("HOSTLENS_LLM_MODE", "record")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-12345678")
    with pytest.raises(pytest.fail.Exception) as exc_info:
        llm_cassette("no_registry")
    assert "target_registry" in str(exc_info.value)
