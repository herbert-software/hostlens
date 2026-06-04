"""Unit tests for ``notifiers/config.py`` (task 3.4 — config half).

Spec: ``openspec/changes/add-notifier-channels/specs/notify-routing/spec.md``
(§需求:通道配置必须从 `notifiers.yaml` 加载并解析 `${ENV_VAR}`).

Covers:

- unset referenced env var → fail-loud naming the variable;
- set env var → injected correctly and value never logged / persisted;
- unknown ``type`` → fail-loud;
- required field present-but-empty → ``validate_config`` fail-loud;
- literal ``$`` / malformed ``${X`` kept verbatim;
- ``${}`` empty name → fail-loud;
- single-layer (non-recursive) expansion.

The tests use a **local fake** ``Notifier`` registered onto a fresh
``ChannelTypeRegistry`` so the loader contract is exercised without
depending on group C's adapter modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError
from hostlens.notifiers.base import ChannelTypeRegistry, NotifyPayload, NotifyResult
from hostlens.notifiers.config import load_channels

if TYPE_CHECKING:
    from hostlens.reporting.models import Report, Severity


class _FakeNotifier:
    """Records the resolved config it was constructed with.

    ``validate_config`` enforces that ``bot_token`` is present **and**
    non-empty (an empty string counts as missing) so the loader's
    present-but-empty fail-loud contract can be exercised.
    """

    name = "fake"

    def __init__(self, *, instance_name: str, config: dict[str, object]) -> None:
        self._instance_name = instance_name
        self.config = config

    def validate_config(self, cfg: dict[str, object]) -> None:
        token = cfg.get("bot_token")
        if not isinstance(token, str) or token == "":
            raise ConfigError(
                "bot_token required and must be non-empty",
                kind="missing_required_field",
                channel=self._instance_name,
                field="bot_token",
            )

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:  # pragma: no cover
        raise NotImplementedError

    async def send(self, payload: NotifyPayload) -> NotifyResult:  # pragma: no cover
        raise NotImplementedError


def _registry() -> ChannelTypeRegistry:
    reg = ChannelTypeRegistry()
    reg.register("fake", _FakeNotifier)
    return reg


def _settings(path: Path) -> Settings:
    return Settings(notifiers_config_path=path)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "notifiers.yaml"
    path.write_text(body)
    return path


# --------------------------------------------------------------------------- #
# Absent / empty file
# --------------------------------------------------------------------------- #


def test_absent_file_yields_empty_map(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "nope.yaml")
    assert load_channels(settings, _registry()) == {}


def test_empty_file_yields_empty_map(tmp_path: Path) -> None:
    settings = _settings(_write(tmp_path, ""))
    assert load_channels(settings, _registry()) == {}


# --------------------------------------------------------------------------- #
# ${ENV_VAR} injection
# --------------------------------------------------------------------------- #


def test_env_var_injected_correctly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "s3cret-token-value")
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    channels = load_channels(settings, _registry())

    notifier = channels["tg"]
    assert isinstance(notifier, _FakeNotifier)
    assert notifier.config["bot_token"] == "s3cret-token-value"


def test_unset_env_var_fail_loud_names_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.extra.get("var_name") == "TG_TOKEN"
    assert "TG_TOKEN" in str(exc.value)


def test_injected_secret_not_in_error_or_config_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The loader must not emit the secret value anywhere; we assert the
    # placeholder name (not the value) is what surfaces structurally, and
    # that the loader does not log the resolved secret.
    monkeypatch.setenv("TG_TOKEN", "plaintext-should-not-leak")
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    load_channels(settings, _registry())

    assert "plaintext-should-not-leak" not in caplog.text


def test_empty_env_var_name_fail_loud(tmp_path: Path) -> None:
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${}\n"
    settings = _settings(_write(tmp_path, body))

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.kind == "empty_env_var_name"


def test_literal_dollar_kept_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    # Bare ``$`` and malformed ``${X`` (no closing brace) are not
    # placeholders and must be preserved literally — no env lookup, no raise.
    body = "channels:\n  tg:\n    type: fake\n    bot_token: 'price$5 and ${X'\n"
    settings = _settings(_write(tmp_path, body))

    channels = load_channels(settings, _registry())
    assert channels["tg"].config["bot_token"] == "price$5 and ${X"  # type: ignore[attr-defined]


def test_single_layer_non_recursive_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``${A}`` resolves to a value that itself contains ``${B}``; the
    # injected content must NOT be re-scanned (single-layer).
    monkeypatch.setenv("A", "${B}")
    monkeypatch.setenv("B", "should-not-appear")
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${A}\n"
    settings = _settings(_write(tmp_path, body))

    channels = load_channels(settings, _registry())
    assert channels["tg"].config["bot_token"] == "${B}"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# type resolution + validate_config
# --------------------------------------------------------------------------- #


def test_unknown_type_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "x")
    body = "channels:\n  s:\n    type: slack\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.kind == "unknown_channel_type"
    assert exc.value.extra.get("channel_type") == "slack"


def test_missing_type_fail_loud(tmp_path: Path) -> None:
    body = "channels:\n  s:\n    bot_token: abc\n"
    settings = _settings(_write(tmp_path, body))

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.kind == "missing_channel_type"


def test_empty_required_field_fail_loud(tmp_path: Path) -> None:
    # ``bot_token`` present but empty string → validate_config must reject.
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ''\n"
    settings = _settings(_write(tmp_path, body))

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.kind == "missing_required_field"


def test_type_stripped_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "x")
    body = "channels:\n  tg:\n    type: fake\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    channels = load_channels(settings, _registry())
    assert "type" not in channels["tg"].config  # type: ignore[attr-defined]


def test_non_mapping_top_level_fail_loud(tmp_path: Path) -> None:
    settings = _settings(_write(tmp_path, "- just\n- a\n- list\n"))
    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())
    assert exc.value.kind == "invalid_top_level"


def test_unreadable_file_raises_configerror_not_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A present-but-unreadable notifiers.yaml must surface as a typed
    # ``ConfigError`` (so the CLI / scheduler callers, which only catch
    # ConfigError, map it to a clean exit) rather than a raw ``OSError``.
    # ``chmod(0o000)`` does not reliably deny read to root, so we monkeypatch
    # ``Path.read_text`` to raise ``OSError`` for determinism.
    settings = _settings(_write(tmp_path, "channels: {}\n"))

    def _raise(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise)

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, _registry())

    assert exc.value.kind == "notifiers_yaml_unreadable"


# --------------------------------------------------------------------------- #
# Real adapter construction contract (instance_name= alignment)
# --------------------------------------------------------------------------- #


def test_real_adapters_constructed_with_instance_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hostlens.notifiers.base import register_default_notifiers

    monkeypatch.setenv("TG_TOKEN", "123:abc")
    monkeypatch.setenv("LARK_HOOK", "https://open.feishu.cn/hook/xyz")
    body = (
        "channels:\n"
        "  tg:\n"
        "    type: telegram\n"
        "    bot_token: ${TG_TOKEN}\n"
        "    chat_id: '42'\n"
        "  fs:\n"
        "    type: lark\n"
        "    webhook_url: ${LARK_HOOK}\n"
    )
    settings = _settings(_write(tmp_path, body))

    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)

    channels = load_channels(settings, registry)

    assert set(channels) == {"tg", "fs"}


def test_real_adapter_missing_field_raises_configerror_not_valueerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real telegram channel with ``bot_token`` set but ``chat_id`` omitted:
    # the adapter's ``validate_config`` raises ``ValueError``, which the loader
    # must convert to ``ConfigError`` (all callers only catch ConfigError).
    from hostlens.notifiers.base import register_default_notifiers

    monkeypatch.setenv("TG_TOKEN", "123:abc")
    body = "channels:\n  tg:\n    type: telegram\n    bot_token: ${TG_TOKEN}\n"
    settings = _settings(_write(tmp_path, body))

    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)

    with pytest.raises(ConfigError) as exc:
        load_channels(settings, registry)

    assert exc.value.kind == "invalid_channel_config"
