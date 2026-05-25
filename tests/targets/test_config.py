"""Tests for ``hostlens.targets.config`` — schema + yaml loader.

Spec: ``openspec/specs/execution-target/spec.md`` §需求:`TargetsConfig`
必须从 yaml 加载且环境变量占位展开.

Covers the Pydantic schema and ``load_targets_config`` behaviour
(env-placeholder expansion, missing-file fallback, error mapping). The
registry-side secret-scrub round-trip lives in the registry test
module since it requires registry build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.targets.config import (
    LocalEntry,
    SSHEntry,
    TargetsConfig,
    load_targets_config,
)

# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


def test_local_entry_minimal_fields() -> None:
    """A ``LocalEntry`` accepts only the documented common fields."""

    entry = LocalEntry(name="my-local", type="local")
    assert entry.name == "my-local"
    assert entry.type == "local"
    assert entry.enabled is True
    assert entry.tags == []
    assert entry.display_name is None
    assert entry.description is None


def test_ssh_entry_field_set_is_exactly_seven_specifics() -> None:
    """Spec §场景:TargetEntry SSH 字段集严格 — exactly 7 SSH-specific fields."""

    ssh_specific = set(SSHEntry.model_fields.keys()) - set(LocalEntry.model_fields.keys())
    assert ssh_specific == {
        "host",
        "user",
        "port",
        "key_path",
        "password",
        "passphrase",
        "connect_timeout",
    }


def test_ssh_entry_accepts_connect_timeout() -> None:
    """``connect_timeout`` is a per-target override (None means use default)."""

    entry = SSHEntry(
        name="prod-web",
        type="ssh",
        host="10.0.0.5",
        user="alice",
        connect_timeout=42,
    )
    assert entry.connect_timeout == 42

    entry_default = SSHEntry(
        name="prod-web",
        type="ssh",
        host="10.0.0.5",
        user="alice",
    )
    assert entry_default.connect_timeout is None


def test_ssh_entry_rejects_unknown_field() -> None:
    """``extra="forbid"`` ensures schema typos raise loudly.

    Spec scenario: passing ``agent_forwarding=True`` (a real asyncssh
    option, but **not** in the M1 schema) must raise so users do not
    silently believe a feature is wired up that is not.
    """

    with pytest.raises(ValidationError):
        SSHEntry(
            name="prod-web",
            type="ssh",
            host="x",
            user="y",
            agent_forwarding=True,  # type: ignore[call-arg]
        )


def test_targets_config_unknown_type_raises() -> None:
    """Spec §场景:unknown type raise — ``type: vm`` is not in the Literal."""

    with pytest.raises(ValidationError):
        TargetsConfig.model_validate(
            {
                "version": "1",
                "targets": [{"name": "x", "type": "vm"}],
            }
        )


@pytest.mark.parametrize(
    "bad_name",
    ["Prod-Web", "1web", "prod web", "UPPER", "", "a" * 65, "-leading-dash"],
)
def test_targets_config_name_regex_enforced(bad_name: str) -> None:
    """Spec §场景:TargetEntry name 不匹配正则 raise — loader is the first
    of three regex enforcement points (the other two are constructor +
    registry)."""

    with pytest.raises(ValidationError):
        TargetsConfig.model_validate(
            {
                "version": "1",
                "targets": [{"name": bad_name, "type": "local"}],
            }
        )


def test_targets_config_version_must_be_one() -> None:
    """``version`` is a Literal["1"] — future schema bumps will signal break."""

    with pytest.raises(ValidationError):
        TargetsConfig.model_validate({"version": "2", "targets": []})


def test_targets_config_extra_top_level_field_rejected() -> None:
    """``extra="forbid"`` at the top level catches typos like ``targest:``."""

    with pytest.raises(ValidationError):
        TargetsConfig.model_validate({"version": "1", "targets": [], "stray": True})


# ---------------------------------------------------------------------------
# ``load_targets_config``
# ---------------------------------------------------------------------------


def test_load_returns_empty_config_when_file_missing(tmp_path: Path) -> None:
    """Spec §场景:配置文件不存在返回空 TargetsConfig.

    No file → ``TargetsConfig(version="1", targets=[])``. This must
    **not** raise — doctor / CLI surface this as a "bootstrap me" hint.
    """

    missing = tmp_path / "nope.yaml"
    cfg = load_targets_config(missing)
    assert cfg.version == "1"
    assert cfg.targets == []


def test_load_handles_empty_yaml_file(tmp_path: Path) -> None:
    """Empty file is equivalent to "no targets" — must not raise."""

    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    cfg = load_targets_config(empty)
    assert cfg.targets == []


def test_load_basic_local_and_ssh(tmp_path: Path) -> None:
    """Two-target round trip exercising both discriminator branches."""

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
                    },
                ],
            }
        )
    )
    cfg = load_targets_config(cfg_path)
    assert [e.name for e in cfg.targets] == ["my-local", "my-ssh"]
    assert isinstance(cfg.targets[0], LocalEntry)
    assert isinstance(cfg.targets[1], SSHEntry)


def test_load_expands_env_placeholder_in_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:`${ENV}` 占位展开 — secret fields read from os.environ."""

    monkeypatch.setenv(
        "HOSTLENS_DEMO_PWD", "TEST_PWD_NOT_A_REAL_SECRET"
    )  # pragma: allowlist secret
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
                        "password": "${HOSTLENS_DEMO_PWD}",
                    }
                ],
            }
        )
    )
    cfg = load_targets_config(cfg_path)
    entry = cfg.targets[0]
    assert isinstance(entry, SSHEntry)
    assert entry.password == "TEST_PWD_NOT_A_REAL_SECRET"


def test_load_missing_env_var_raises_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:env 未设置 raise ConfigError."""

    monkeypatch.delenv("UNSET_FOR_HOSTLENS_TEST", raising=False)
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
                        "password": "${UNSET_FOR_HOSTLENS_TEST}",
                    }
                ],
            }
        )
    )
    with pytest.raises(ConfigError) as exc:
        load_targets_config(cfg_path)
    assert exc.value.kind == "missing_env_var"
    assert exc.value.extra["var_name"] == "UNSET_FOR_HOSTLENS_TEST"
    assert exc.value.extra["target"] == "prod-web"


@pytest.mark.parametrize("field", ["host", "user", "key_path"])
def test_load_rejects_placeholder_in_non_secret_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Spec §场景:占位出现在非 secret 字段 raise.

    ``${ENV}`` outside ``password`` / ``passphrase`` is rejected even
    if the env var is set — the policy is "explicitly disallowed", not
    "warn if missing".
    """

    monkeypatch.setenv("SOME_VALUE", "set-but-disallowed")
    body = {
        "name": "prod-web",
        "type": "ssh",
        "host": "10.0.0.5",
        "user": "alice",
    }
    body[field] = "${SOME_VALUE}"
    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(yaml.safe_dump({"version": "1", "targets": [body]}))
    with pytest.raises(ConfigError) as exc:
        load_targets_config(cfg_path)
    assert exc.value.kind == "env_placeholder_not_allowed_here"
    assert exc.value.extra["field"] == field
    assert exc.value.extra["target"] == "prod-web"


def test_load_passphrase_placeholder_expanded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``passphrase`` is on the secret allowlist alongside ``password``."""

    monkeypatch.setenv(
        "HOSTLENS_DEMO_PASSPHRASE", "TEST_PASSPHRASE_NOT_A_REAL_SECRET"
    )  # pragma: allowlist secret
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
                        "passphrase": "${HOSTLENS_DEMO_PASSPHRASE}",
                    }
                ],
            }
        )
    )
    cfg = load_targets_config(cfg_path)
    entry = cfg.targets[0]
    assert isinstance(entry, SSHEntry)
    assert entry.passphrase == "TEST_PASSPHRASE_NOT_A_REAL_SECRET"


def test_load_yaml_parse_error_wrapped_in_config_error(tmp_path: Path) -> None:
    """Malformed yaml surfaces as a ``ConfigError`` with kind set."""

    cfg_path = tmp_path / "broken.yaml"
    cfg_path.write_text("version: 1\n  bad-indent: oops\n :::")
    with pytest.raises(ConfigError) as exc:
        load_targets_config(cfg_path)
    assert exc.value.kind == "yaml_parse_error"


def test_load_non_mapping_top_level_raises(tmp_path: Path) -> None:
    """Top-level yaml must be a mapping; lists / scalars are rejected."""

    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text(yaml.safe_dump(["not", "a", "mapping"]))
    with pytest.raises(ConfigError) as exc:
        load_targets_config(cfg_path)
    assert exc.value.kind == "invalid_top_level"


def test_load_keeps_disabled_targets(tmp_path: Path) -> None:
    """Spec §需求:``TargetsConfig`` enabled 行为约定 — loader does **not**
    filter disabled targets. Filtering happens downstream (handler).
    """

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
    cfg = load_targets_config(cfg_path)
    assert {e.name for e in cfg.targets} == {"alive", "sleeping"}
    enabled_map = {e.name: e.enabled for e in cfg.targets}
    assert enabled_map == {"alive": True, "sleeping": False}


def test_load_expand_env_false_rejects_non_secret_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write-path must surface the same misconfiguration as read-path.

    Placeholder in ``host`` (a non-secret field) is forbidden regardless
    of whether the env var is set — the write path used by ``hostlens
    target add`` / ``remove`` must not silently accept this.
    """

    monkeypatch.delenv("UNSET_FOR_HOSTLENS_TEST", raising=False)
    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [
                    {
                        "name": "prod-web",
                        "type": "ssh",
                        "host": "${UNSET_FOR_HOSTLENS_TEST}",
                        "user": "alice",
                    }
                ],
            }
        )
    )
    with pytest.raises(ConfigError) as exc:
        load_targets_config(cfg_path, expand_env=False)
    assert exc.value.kind == "env_placeholder_not_allowed_here"
    assert exc.value.extra["field"] == "host"
    assert exc.value.extra["target"] == "prod-web"


def test_load_expand_env_false_strips_secret_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``password: "${UNSET}"`` validates under ``expand_env=False`` even
    when the env var is missing — entries with unresolved secrets must
    not block write commands that touch unrelated targets.
    """

    monkeypatch.delenv("UNSET_FOR_HOSTLENS_TEST", raising=False)
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
                        "password": "${UNSET_FOR_HOSTLENS_TEST}",
                    }
                ],
            }
        )
    )
    cfg = load_targets_config(cfg_path, expand_env=False)
    entry = cfg.targets[0]
    assert isinstance(entry, SSHEntry)
    assert entry.password is None
