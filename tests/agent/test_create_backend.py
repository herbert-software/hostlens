"""Tests for ``hostlens.agent.backend.create_backend`` factory.

Covers spec §需求:`create_backend` 工厂 acceptance scenarios:

(a) ``settings.backend is None`` → ``ConfigError``.
(b) ``anthropic_api`` → ``AnthropicAPIBackend`` instance.
(c) **Key invariant**: SecretStr unwrapping. The factory MUST call
    ``.get_secret_value()`` so the live SDK receives the raw key, not the
    redacted ``"**********"`` placeholder.
(d) ``anthropic_api`` + ``api_key is None`` → ``ConfigError`` (defensive
    check; schema validator normally catches this earlier).
(e) ``playback`` → ``PlaybackBackend`` instance.
(f) ``bedrock`` → ``NotImplementedError("...M10.5...")``.
(g) Daemon-mode gate: when ``is_daemon_mode`` returns True and the backend's
    ``ensure_safe_for_daemon`` raises ``BackendDaemonUnsafe``, the exception
    propagates (factory does not catch it).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import SecretStr

from hostlens.agent.backend import create_backend
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.core.config import AgentSettings, BackendSettings, Settings
from hostlens.core.exceptions import BackendDaemonUnsafe, ConfigError


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear ``HOSTLENS_*`` env and chdir off the repo so a dev ``.env`` /
    exported ``HOSTLENS_*`` don't leak a configured backend/agent into the
    tests asserting the unconfigured path (``backend is None`` → ConfigError,
    ``settings.agent is None`` → fallback). ``Settings()`` reads ``.env`` from
    cwd, so the chdir is what actually blocks the file read."""

    for key in list(os.environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


_FAKE_KEY = (
    "sk-" + "ant-" + "validkey1234"
)  # pragma: allowlist secret — fake fixture, not a real key
_FAKE_RAW_SECRET = (
    "sk-" + "ant-" + "realvalue123"
)  # pragma: allowlist secret — fake fixture, not a real key


# (a) ConfigError when backend is None
def test_backend_none_raises_config_error() -> None:
    settings = Settings()
    with pytest.raises(ConfigError) as excinfo:
        create_backend(settings)
    assert "backend.type required" in str(excinfo.value)


# (b) anthropic_api → AnthropicAPIBackend
def test_anthropic_api_dispatch_returns_anthropic_api_backend() -> None:
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        )
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend.name == "anthropic_api"


# (c) SecretStr unwrapping — the critical security invariant
def test_anthropic_api_secret_unwrapped_to_raw_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §需求:`create_backend` 工厂 §场景:anthropic_api 分派.

    The Anthropic SDK MUST receive the raw key string via the ``api_key``
    kwarg, not the SecretStr placeholder or any masked form. We spy on
    ``anthropic.AsyncAnthropic.__init__`` to assert the exact kwarg shape.
    """

    captured_kwargs: dict[str, Any] = {}
    original_init = anthropic.AsyncAnthropic.__init__

    def _spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)
        # Still call the real init so the backend object is usable.
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _spy_init)

    raw_secret = _FAKE_RAW_SECRET
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(raw_secret),
        )
    )
    create_backend(settings)

    # The spy must have observed the exact raw value — not a SecretStr
    # object, not "**********", not "<redacted>", not str(SecretStr(...)).
    assert "api_key" in captured_kwargs, captured_kwargs
    assert captured_kwargs["api_key"] == raw_secret
    assert captured_kwargs["api_key"] != "**********"
    assert captured_kwargs["api_key"] != "<redacted>"
    assert not isinstance(captured_kwargs["api_key"], SecretStr)
    assert captured_kwargs["api_key"] != str(SecretStr(raw_secret))


def test_anthropic_api_uses_agent_health_check_model_when_present() -> None:
    """The factory wires ``agent.health_check_model`` into the backend.

    Counter-test: when ``settings.agent`` is None the backend falls back
    to its own ``health_check_model`` default ("claude-haiku-4-5").
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
        agent=AgentSettings(health_check_model="claude-custom-haiku"),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    # Access internal attribute set by AnthropicAPIBackend.__init__.
    assert backend._health_check_model == "claude-custom-haiku"


def test_anthropic_api_falls_back_to_default_health_check_model_when_no_agent() -> None:
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._health_check_model == "claude-haiku-4-5"


def test_anthropic_api_passes_disable_thinking_true() -> None:
    """Spec §需求:`create_backend` 必须透传 `disable_thinking`.

    The factory reads ``backend.disable_thinking`` and threads it into the
    constructed ``AnthropicAPIBackend`` so injection is observable downstream.
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
            disable_thinking=True,
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._disable_thinking is True


def test_anthropic_api_disable_thinking_defaults_false() -> None:
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._disable_thinking is False


def test_anthropic_api_strips_auth_headers_case_insensitive() -> None:
    """Spec §需求:`AnthropicAPIBackend` 必须支持 `extra_headers` 透传 §场景:认证 header 丢弃大小写不敏感.

    Authentication is sourced solely from ``api_key`` (D-4). ``create_backend``
    MUST drop ``x-api-key`` / ``authorization`` keys (case-insensitive)
    before threading ``extra_headers`` into the backend so a statistics header
    can never override the SecretStr-backed auth path. Only the non-auth
    ``HTTP-Referer`` survives.
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
            extra_headers={
                "X-Api-Key": "attacker",  # pragma: allowlist secret — adversarial fixture
                "AUTHORIZATION": "Bearer x",  # pragma: allowlist secret — adversarial fixture
                "HTTP-Referer": "https://x",
            },
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._extra_headers == {"HTTP-Referer": "https://x"}


def test_anthropic_api_extra_headers_all_auth_collapses_to_none() -> None:
    """When every key is an auth header, the stripped dict collapses to None.

    Passing an empty dict as ``default_headers`` would break the backend's
    "None → don't pass ``default_headers``" default-shape guarantee, so the
    wiring layer normalizes a fully-stripped dict back to None.
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
            extra_headers={
                "x-api-key": "attacker",  # pragma: allowlist secret — adversarial fixture
                "Authorization": "Bearer x",  # pragma: allowlist secret — adversarial fixture
            },
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._extra_headers is None


def test_anthropic_api_extra_headers_default_none() -> None:
    """No ``extra_headers`` configured → backend receives None (default shape)."""

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend._extra_headers is None


def test_anthropic_api_prompt_caching_false_end_to_end() -> None:
    """Spec §需求:`AnthropicAPIBackend` 必须支持 `prompt_caching` capability 实例注入.

    A config-level ``prompt_caching=False`` must flow through ``create_backend``
    into the constructed backend's ``capabilities.prompt_caching``, so the
    Agent loop skips ``cache_control`` injection for non-Claude endpoints.
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
            prompt_caching=False,
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend.capabilities.prompt_caching is False


@pytest.mark.parametrize("configured", [None, True])
def test_anthropic_api_prompt_caching_none_or_true_maps_to_true(
    configured: bool | None,
) -> None:
    """Both unset (None) and explicit True map to backend ``prompt_caching=True``."""

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
            prompt_caching=configured,
        ),
    )
    backend = create_backend(settings)
    assert isinstance(backend, AnthropicAPIBackend)
    assert backend.capabilities.prompt_caching is True


# (d) anthropic_api + api_key is None → ConfigError
def test_anthropic_api_without_api_key_raises_config_error() -> None:
    """Defensive guard for programmatic construction that bypassed schema
    validation.

    ``BackendSettings(...)`` already rejects ``api_key=None`` for
    ``type=anthropic_api``, so reaching this branch requires somehow
    mutating ``settings.backend.api_key`` to ``None`` after construction.
    We do that via ``model_copy`` with ``update``.
    """

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    # Bypass the validator by mutating the backend field after construction.
    # ``object.__setattr__`` bypasses Pydantic v2's frozen-style assignment
    # validation.
    object.__setattr__(settings.backend, "api_key", None)

    with pytest.raises(ConfigError) as excinfo:
        create_backend(settings)
    assert "api_key required" in str(excinfo.value)


# (e) playback → PlaybackBackend instance
def test_playback_dispatch_returns_playback_backend(tmp_path: Path) -> None:
    cassette = tmp_path / "demo.jsonl"
    # Empty cassette is a valid empty-record file (no records → every call
    # is a CassetteMiss, but constructor itself succeeds).
    cassette.write_text("")
    settings = Settings(
        backend=BackendSettings(type="playback", cassette_path=cassette),
    )
    backend = create_backend(settings)
    assert isinstance(backend, PlaybackBackend)
    assert backend.name == "playback"


# (f) bedrock → NotImplementedError
@pytest.mark.parametrize("placeholder_type", ["bedrock", "vertex", "claude_subscription"])
def test_placeholder_backend_types_raise_not_implemented(placeholder_type: str) -> None:
    """Spec §需求:`create_backend` 工厂 §场景:不支持类型 raise NotImplementedError.

    M10.5 / 1.0 placeholders — schema accepts them so config files can ship
    ahead of the backend impl; ``create_backend`` is the layer that defers
    to ``NotImplementedError`` with a helpful migration hint.
    """

    if placeholder_type == "bedrock":
        settings = Settings(
            backend=BackendSettings(type="bedrock", aws_region="us-east-1"),
        )
    elif placeholder_type == "vertex":
        settings = Settings(backend=BackendSettings(type="vertex"))
    else:
        settings = Settings(
            backend=BackendSettings(
                type="claude_subscription",
                oauth_token=SecretStr("oauth-x"),
            )
        )

    with pytest.raises(NotImplementedError) as excinfo:
        create_backend(settings)
    msg = str(excinfo.value)
    assert placeholder_type in msg
    assert "M10.5" in msg


# (g) Daemon-mode gate propagates BackendDaemonUnsafe
def test_daemon_mode_gate_propagates_backend_daemon_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``is_daemon_mode`` returns True and ``ensure_safe_for_daemon``
    raises ``BackendDaemonUnsafe``, the factory MUST let it propagate.

    The factory does NOT catch the exception; the caller (M5 Scheduler boot
    path) is responsible for deciding whether to fall back / abort.
    """

    def _raise_daemon_unsafe(self: Any) -> None:
        raise BackendDaemonUnsafe(
            backend_name="anthropic_api",
            reason="subscription_in_daemon",
        )

    monkeypatch.setattr(
        "hostlens.agent.backend.is_daemon_mode",
        lambda settings: True,
    )
    monkeypatch.setattr(
        AnthropicAPIBackend,
        "ensure_safe_for_daemon",
        _raise_daemon_unsafe,
    )

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    with pytest.raises(BackendDaemonUnsafe):
        create_backend(settings)


def test_daemon_mode_false_skips_ensure_safe_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-test to (g): when ``is_daemon_mode`` returns False the
    factory MUST NOT call ``ensure_safe_for_daemon``.

    ``is_daemon_mode`` defaults to False (M2 scope), so this is the
    normal-path behavior — any backend's ``ensure_safe_for_daemon`` raising
    must NOT fire during routine ``create_backend`` calls.
    """

    call_count = {"value": 0}

    def _track_ensure_safe(self: Any) -> None:
        call_count["value"] += 1

    monkeypatch.setattr(
        AnthropicAPIBackend,
        "ensure_safe_for_daemon",
        _track_ensure_safe,
    )

    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(_FAKE_KEY),
        ),
    )
    create_backend(settings)
    assert call_count["value"] == 0
