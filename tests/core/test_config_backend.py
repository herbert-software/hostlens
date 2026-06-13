"""Tests for ``BackendSettings`` / ``AgentSettings`` Pydantic schema.

Covers spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace
acceptance scenarios (a-g):

(a) Settings without ``backend`` / ``agent`` fields loads cleanly.
(b) ``anthropic_api`` requires ``api_key``.
(c) ``playback`` requires ``cassette_path``.
(d) ``agent.max_turns`` enforces the 1-100 range.
(e) ``api_key`` SecretStr redacts under ``model_dump_json()``.
(f) ``bedrock`` / ``vertex`` / ``claude_subscription`` placeholders load
    without raising (NotImplementedError fires later in ``create_backend``).
(g) Sensitive value never leaks into ``ConfigError`` even when an *unrelated*
    field fails validation.

All tests construct ``BackendSettings`` / ``AgentSettings`` directly OR drive
the ``load_settings()`` env-loading path with ``HOSTLENS_*`` env vars
(pydantic-settings nested delimiter ``__``). The redaction test (g) routes
through ``load_settings()`` so it exercises the real ``_format_validation_error``
secret-scrubber that production callers depend on.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from hostlens.core.config import (
    AgentSettings,
    BackendSettings,
    Settings,
    load_settings,
)
from hostlens.core.exceptions import ConfigError


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear all ``HOSTLENS_*`` env vars and chdir into ``tmp_path``.

    Without isolation the developer's ``HOSTLENS_BACKEND__*`` env (or a
    stray ``.env`` in the repo root) would leak into every test below.
    """

    for key in list(os.environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


# (a) Settings without backend / agent fields loads cleanly
def test_settings_without_backend_or_agent_loads_clean() -> None:
    settings = load_settings()
    assert settings.backend is None
    assert settings.agent is None


# (b) anthropic_api requires api_key
def test_anthropic_api_requires_api_key_via_direct_construction() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BackendSettings(type="anthropic_api", api_key=None)
    assert "api_key required for type=anthropic_api" in str(excinfo.value)


def test_anthropic_api_requires_api_key_via_load_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    # No HOSTLENS_BACKEND__API_KEY → api_key stays None → validator fires.

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "api_key required" in str(excinfo.value)


# (c) playback requires cassette_path
def test_playback_requires_cassette_path_via_direct_construction() -> None:
    with pytest.raises(ValidationError) as excinfo:
        BackendSettings(type="playback", cassette_path=None)
    assert "cassette_path required for type=playback" in str(excinfo.value)


def test_playback_requires_cassette_path_via_load_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "playback")
    # No HOSTLENS_BACKEND__CASSETTE_PATH → cassette_path stays None.

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "cassette_path required" in str(excinfo.value)


# (d) agent.max_turns range
def test_agent_max_turns_above_range_rejected() -> None:
    """Spec §场景:agent.max_turns 范围校验.

    The spec acceptance text mentions ``max_turns must be in range 1-100``
    as a readability hint; Pydantic v2's actual message is ``Input should
    be less than or equal to 100``. We assert on the **token set** (field
    name + bound value) so this test stays tied to spec intent without
    coupling to Pydantic's wording across versions.
    """

    with pytest.raises(ValidationError) as excinfo:
        AgentSettings(max_turns=200)
    msg = str(excinfo.value)
    assert "max_turns" in msg
    assert "100" in msg


def test_agent_max_turns_below_range_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AgentSettings(max_turns=0)
    msg = str(excinfo.value)
    assert "max_turns" in msg
    assert "1" in msg


def test_agent_max_turns_via_load_settings_surfaces_field_and_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_AGENT__MAX_TURNS", "200")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "max_turns" in msg
    assert "100" in msg


# (e) api_key SecretStr redacts under model_dump_json
def test_api_key_secretstr_redacts_in_model_dump_json() -> None:
    """Spec §场景:SecretStr model_dump 脱敏.

    ``model_dump_json()`` MUST surface ``"**********"`` rather than the
    raw secret. Any caller serializing Settings to logs / doctor JSON
    / Notifier payloads relies on this guarantee.
    """

    fake_key = "sk-" + "ant-" + "real"  # pragma: allowlist secret — fake fixture
    settings = Settings(
        backend=BackendSettings(
            type="anthropic_api",
            api_key=SecretStr(fake_key),
        )
    )
    dumped = settings.model_dump_json()
    assert fake_key not in dumped
    assert "**********" in dumped


def test_api_key_secretstr_repr_redacts() -> None:
    """``repr(SecretStr)`` outputs ``SecretStr('**********')`` — counter-
    test to (e) that the SecretStr type itself never leaks via ``repr``."""

    raw_secret = "sk-" + "ant-" + "real-secret"  # pragma: allowlist secret — fake fixture
    key = SecretStr(raw_secret)
    assert raw_secret not in repr(key)


# (f) bedrock / vertex / claude_subscription placeholders load without raising
def test_bedrock_placeholder_loads_without_raising() -> None:
    """Spec §场景:backend.type = bedrock 加载阶段不 raise.

    ``BackendSettings(type="bedrock", aws_region="us-east-1")`` must
    construct cleanly so a config file can ship ahead of the M10.5
    backend implementation. ``create_backend`` raises ``NotImplementedError``
    later — schema layer stays permissive.
    """

    b = BackendSettings(type="bedrock", aws_region="us-east-1")
    assert b.type == "bedrock"
    assert b.aws_region == "us-east-1"
    assert b.api_key is None  # No api_key required for bedrock placeholder.


def test_vertex_placeholder_loads_without_raising() -> None:
    b = BackendSettings(type="vertex")
    assert b.type == "vertex"


def test_claude_subscription_placeholder_loads_without_raising() -> None:
    b = BackendSettings(
        type="claude_subscription",
        oauth_token=SecretStr("oauth-placeholder"),
        accept_subscription_risks=False,
    )
    assert b.type == "claude_subscription"


# (g) sensitive value never leaks into ConfigError when other field fails
def test_api_key_redacted_in_config_error_on_unrelated_field_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:backend.api_key 在 ConfigError 中脱敏.

    Triggers a separate validation failure (invalid base_url) while api_key
    is set to a leakable value. The ``_format_validation_error`` redactor
    (which masks values for any field name matching ``key|token|secret|...``)
    must scrub the api_key from the rendered ``ConfigError`` even though
    the error itself is about ``base_url``.
    """

    leak_value = "sk-" + "ant-" + "leakvalue"  # pragma: allowlist secret — fake fixture
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", leak_value)
    # ``HttpUrl`` rejects unschema'd hosts; this triggers a base_url
    # validation error while api_key is fully set on the same model.
    monkeypatch.setenv("HOSTLENS_BACKEND__BASE_URL", "not-a-url")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert leak_value not in msg, f"api_key leaked into ConfigError message: {msg!r}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False)],
)
def test_disable_thinking_env_bool_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """Spec §场景:backend.disable_thinking 经 env 加载为 True / 缺省 False.

    pydantic-settings parses the nested ``HOSTLENS_BACKEND__DISABLE_THINKING``
    env var as a bool across the common truthy/falsy spellings.
    """

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    monkeypatch.setenv("HOSTLENS_BACKEND__DISABLE_THINKING", raw)
    settings = load_settings()
    assert settings.backend is not None
    assert settings.backend.disable_thinking is expected


def test_disable_thinking_defaults_false() -> None:
    """Spec §场景:backend.disable_thinking 缺省为 False."""

    b = BackendSettings(type="fake")
    assert b.disable_thinking is False


def test_disable_thinking_decoupled_from_type() -> None:
    """Spec §场景:非 anthropic_api type 设置 disable_thinking 被静默忽略.

    Any type accepts the flag at load time (no cross-field validation); only
    the ``anthropic_api`` path consumes it in ``create_backend``.
    """

    b = BackendSettings(
        type="playback",
        cassette_path=Path("/tmp/x.jsonl"),
        disable_thinking=True,
    )
    assert b.disable_thinking is True


def test_extra_headers_defaults_none() -> None:
    """Spec §场景:backend.extra_headers 缺省为 None."""

    b = BackendSettings(type="fake")
    assert b.extra_headers is None


def test_extra_headers_env_loads_as_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §场景:backend.extra_headers 经 env 加载为 dict.

    pydantic-settings parses the nested ``HOSTLENS_BACKEND__EXTRA_HEADERS``
    env var as JSON into a ``dict[str, str]``.
    """

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    monkeypatch.setenv(
        "HOSTLENS_BACKEND__EXTRA_HEADERS",
        '{"HTTP-Referer":"https://example.com","X-OpenRouter-Title":"hostlens"}',
    )
    settings = load_settings()
    assert settings.backend is not None
    assert settings.backend.extra_headers == {
        "HTTP-Referer": "https://example.com",
        "X-OpenRouter-Title": "hostlens",
    }


def test_extra_headers_decoupled_from_type() -> None:
    """Spec §场景: non-anthropic_api type may set extra_headers without error.

    Any type accepts the header map at load time (no cross-field validation);
    only the ``anthropic_api`` path consumes it in ``create_backend``.
    """

    b = BackendSettings(
        type="playback",
        cassette_path=Path("/tmp/x.jsonl"),
        extra_headers={"HTTP-Referer": "https://example.com"},
    )
    assert b.extra_headers == {"HTTP-Referer": "https://example.com"}


def test_prompt_caching_defaults_none() -> None:
    """Spec §场景:backend.prompt_caching 缺省为 None.

    ``None`` is semantically equivalent to ``True`` (real-Anthropic default
    prompt caching stays on); only ``False`` flips behaviour.
    """

    b = BackendSettings(type="fake")
    assert b.prompt_caching is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), ("1", True), ("0", False)],
)
def test_prompt_caching_env_bool_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """Spec §场景:backend.prompt_caching 经 env 加载为 False."""

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    monkeypatch.setenv("HOSTLENS_BACKEND__PROMPT_CACHING", raw)
    settings = load_settings()
    assert settings.backend is not None
    assert settings.backend.prompt_caching is expected


def test_prompt_caching_decoupled_from_type() -> None:
    """Spec §场景: non-anthropic_api type may set prompt_caching without error."""

    b = BackendSettings(
        type="playback",
        cassette_path=Path("/tmp/x.jsonl"),
        prompt_caching=False,
    )
    assert b.prompt_caching is False


def test_extra_fields_in_backend_settings_rejected() -> None:
    """Spec contract: ``model_config = ConfigDict(extra="forbid")``.

    Catches typos in user config files (``api-key`` vs ``api_key``) at
    load time rather than letting them silently fall through to a None
    default and surface as ``api_key required`` later.
    """

    fake_key = "sk-" + "ant-" + "x" * 4  # pragma: allowlist secret — fake fixture
    with pytest.raises(ValidationError):
        BackendSettings(type="anthropic_api", api_key=SecretStr(fake_key), unknown="x")  # type: ignore[call-arg]


def test_extra_fields_in_agent_settings_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentSettings(unknown_field="x")  # type: ignore[call-arg]


def test_agent_defaults_match_spec() -> None:
    """Spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace.

    Defaults shipped here are part of the M2 contract (model id ladder
    consistent with proposal.md Model Strategy).
    """

    a = AgentSettings()
    assert a.primary_model == "claude-opus-4-7"
    assert a.fallback_model is None
    assert a.health_check_model == "claude-haiku-4-5"
    assert a.health_check_timeout_seconds == 10.0
    assert a.max_turns == 20
    assert a.token_budget_input == 100_000
    assert a.token_budget_output == 30_000


# (h) agent.health_check_timeout_seconds: default / env override / range
def test_agent_health_check_timeout_default() -> None:
    """Spec §场景:agent.health_check_timeout_seconds 缺省为 10.0."""

    assert AgentSettings().health_check_timeout_seconds == 10.0


def test_agent_health_check_timeout_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:agent.health_check_timeout_seconds 经 env 加载.

    The nested env var coerces the string ``"40"`` to ``float`` ``40.0``.
    """

    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "40")
    settings = load_settings()
    assert settings.agent is not None
    assert settings.agent.health_check_timeout_seconds == 40.0


def test_agent_health_check_timeout_below_range_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:agent.health_check_timeout_seconds 范围校验 (lower bound).

    ``0`` violates ``ge=1``. We assert on the **complete constraint phrase**
    Pydantic v2 actually emits (``greater than or equal to 1``), which only
    appears for the lower-bound violation — NOT on a bare ``"1"`` substring
    (vacuous: ``_format_validation_error`` always emits the ``"1 configuration
    error:"`` header, so ``"1" in msg`` is true for every input).
    """

    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "health_check_timeout_seconds" in msg
    assert "greater than or equal to 1" in msg


def test_agent_health_check_timeout_negative_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:agent.health_check_timeout_seconds 范围校验 (negative)."""

    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "-5")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "health_check_timeout_seconds" in msg
    assert "greater than or equal to 1" in msg


def test_agent_health_check_timeout_above_range_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:agent.health_check_timeout_seconds 范围校验 (upper bound).

    ``200`` violates ``le=120``; assert on the full phrase ``less than or
    equal to 120`` which appears only for the upper-bound violation.
    """

    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "200")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "health_check_timeout_seconds" in msg
    assert "less than or equal to 120" in msg


def test_agent_health_check_timeout_non_numeric_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:agent.health_check_timeout_seconds 范围校验 (non-numeric).

    A non-numeric ``"abc"`` trips Pydantic's float parser; assert on the
    ``valid number`` phrase which appears only for the parse failure.
    """

    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "abc")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    msg = str(excinfo.value)
    assert "health_check_timeout_seconds" in msg
    assert "valid number" in msg
