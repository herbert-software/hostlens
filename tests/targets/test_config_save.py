"""Tests for ``save_targets_config`` + ``_atomic_write_yaml`` (write path).

Spec: ``openspec/changes/add-cli-target-import/specs/execution-target/spec.md``
§需求:`save_targets_config` 必须原子、幂等、保全 `${VAR}`、文件权限 0600;
序列化 helper 下沉至 config 层.

Covers the atomic ``0o600`` write primitive, the idempotent upsert,
``${VAR}`` preservation, the structured failure mapping, and a
field-for-field crosscheck that ``save_targets_config`` writes the same
dict shape as ``hostlens target add`` (both go through ``_entry_to_dict``).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from hostlens.core.exceptions import ConfigError
from hostlens.targets.config import (
    LocalEntry,
    SSHEntry,
    _atomic_write_yaml,
    _entry_to_dict,
    save_targets_config,
)

# ---------------------------------------------------------------------------
# ``_atomic_write_yaml`` — permissions + atomicity
# ---------------------------------------------------------------------------


def test_atomic_write_sets_file_0600_and_parent_0700(tmp_path: Path) -> None:
    """Spec §场景:文件 0600 (显式 fchmod) + 既有父目录被收紧为 0700.

    The parent dir is pre-created ``0o755`` to prove the write narrows an
    existing loose dir, not just a freshly-created one.
    """

    parent = tmp_path / "hostlens"
    parent.mkdir(mode=0o755)
    # ``mkdir(mode=...)`` is umask-masked; force the loose mode explicitly.
    os.chmod(parent, 0o755)
    cfg_path = parent / "targets.yaml"

    _atomic_write_yaml(cfg_path, {"version": "1", "targets": []})

    file_mode = stat.S_IMODE(cfg_path.stat().st_mode)
    parent_mode = stat.S_IMODE(parent.stat().st_mode)
    assert file_mode == 0o600
    assert parent_mode == 0o700


def test_atomic_write_creates_parent_dir_0700_when_absent(tmp_path: Path) -> None:
    """Absent parent dir is created ``0o700`` (secret-dir guarantee)."""

    cfg_path = tmp_path / "fresh" / "targets.yaml"
    assert not cfg_path.parent.exists()

    _atomic_write_yaml(cfg_path, {"version": "1", "targets": []})

    assert stat.S_IMODE(cfg_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


def test_atomic_write_no_temp_file_left_behind(tmp_path: Path) -> None:
    """A successful write leaves no ``.targets-*.tmp`` sibling."""

    cfg_path = tmp_path / "targets.yaml"
    _atomic_write_yaml(cfg_path, {"version": "1", "targets": []})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "targets.yaml"]
    assert leftovers == []


def test_atomic_write_byte_output_matches_safe_dump(tmp_path: Path) -> None:
    """The on-disk bytes equal ``yaml.safe_dump(raw, sort_keys=False)``.

    Proves the atomic primitive did not reorder / reformat keys relative to
    the prior ``write_text(yaml.safe_dump(...))`` the CLI used.
    """

    raw = {
        "version": "1",
        "targets": [
            {"name": "b", "type": "local"},
            {"name": "a", "type": "ssh", "host": "h", "user": "u"},
        ],
    }
    cfg_path = tmp_path / "targets.yaml"
    _atomic_write_yaml(cfg_path, raw)
    assert cfg_path.read_text() == yaml.safe_dump(raw, sort_keys=False)


def test_atomic_write_parent_is_a_file_raises_config_error(tmp_path: Path) -> None:
    """Spec §需求 失败映射 — unwritable parent → structured ConfigError, not bare OSError.

    The parent path is a regular file, so ``os.makedirs`` / ``mkstemp``
    cannot create a directory entry under it; the resulting ``OSError`` must
    be wrapped in a structured ``ConfigError`` rather than escaping raw.
    """

    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("i am a file, not a directory")
    cfg_path = not_a_dir / "targets.yaml"

    with pytest.raises(ConfigError) as exc:
        _atomic_write_yaml(cfg_path, {"version": "1", "targets": []})
    assert exc.value.kind == "targets_config_write_failed"


# ---------------------------------------------------------------------------
# ``save_targets_config`` — upsert / idempotency / ${VAR} / pre-validation
# ---------------------------------------------------------------------------


def test_save_appends_local_entry_and_sets_0600(tmp_path: Path) -> None:
    cfg_path = tmp_path / "targets.yaml"
    save_targets_config(cfg_path, [(LocalEntry(name="demo", type="local"), None, None)])

    payload = yaml.safe_load(cfg_path.read_text())
    assert payload == {"version": "1", "targets": [{"name": "demo", "type": "local"}]}
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


def test_save_is_idempotent_on_rerun(tmp_path: Path) -> None:
    """Spec §场景:重跑幂等不重复 — re-running with the same name does not duplicate."""

    cfg_path = tmp_path / "targets.yaml"
    entries: list[tuple[LocalEntry | SSHEntry, str | None, str | None]] = [
        (LocalEntry(name="demo", type="local"), None, None)
    ]
    save_targets_config(cfg_path, entries)
    save_targets_config(cfg_path, entries)

    payload = yaml.safe_load(cfg_path.read_text())
    assert [e["name"] for e in payload["targets"]] == ["demo"]


def test_save_skips_name_already_in_file(tmp_path: Path) -> None:
    """Existing on-disk name is skipped (not overwritten) — upsert default skip."""

    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "demo", "type": "local"}]},
            sort_keys=False,
        )
    )
    save_targets_config(
        cfg_path,
        [
            (LocalEntry(name="demo", type="local"), None, None),
            (LocalEntry(name="fresh", type="local"), None, None),
        ],
    )
    payload = yaml.safe_load(cfg_path.read_text())
    assert [e["name"] for e in payload["targets"]] == ["demo", "fresh"]


def test_save_preserves_existing_placeholder_not_flattened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:既有 `${VAR}` 占位写回保持 — must not flatten into plaintext.

    The env var is *set* so a naive expand-then-write would surface the
    secret; the raw round-trip must keep ``${SOME_ENV}`` verbatim.
    """

    monkeypatch.setenv("SOME_ENV", "SECRET_VALUE_NOT_REAL")  # pragma: allowlist secret
    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [
                    {
                        "name": "existing",
                        "type": "ssh",
                        "host": "h",
                        "user": "u",
                        "password": "${SOME_ENV}",
                    }
                ],
            },
            sort_keys=False,
        )
    )
    save_targets_config(cfg_path, [(LocalEntry(name="new", type="local"), None, None)])

    text = cfg_path.read_text()
    assert "${SOME_ENV}" in text
    assert "SECRET_VALUE_NOT_REAL" not in text
    payload = yaml.safe_load(text)
    assert payload["targets"][0]["password"] == "${SOME_ENV}"


def test_save_rejects_corrupt_existing_file(tmp_path: Path) -> None:
    """Spec §场景 既有非法占位文件 → exit 2 — pre-validation raises ConfigError.

    A placeholder in a non-secret field is a misconfiguration the read path
    rejects; the write path must reject it too (not silently round-trip it).
    """

    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [{"name": "bad", "type": "ssh", "host": "${HOST_VAR}", "user": "u"}],
            },
            sort_keys=False,
        )
    )
    with pytest.raises(ConfigError) as exc:
        save_targets_config(cfg_path, [(LocalEntry(name="new", type="local"), None, None)])
    assert exc.value.kind == "env_placeholder_not_allowed_here"


def test_save_rejects_unparseable_existing_file(tmp_path: Path) -> None:
    """Corrupt yaml → ConfigError before any write."""

    cfg_path = tmp_path / "targets.yaml"
    cfg_path.write_text("version: 1\n  bad-indent: oops\n :::")
    with pytest.raises(ConfigError) as exc:
        save_targets_config(cfg_path, [(LocalEntry(name="new", type="local"), None, None)])
    assert exc.value.kind == "yaml_parse_error"


def test_save_writes_ssh_credential_env_as_placeholder(tmp_path: Path) -> None:
    """``password_env`` / ``passphrase_env`` land as ``${VAR}`` (never plaintext)."""

    cfg_path = tmp_path / "targets.yaml"
    entry = SSHEntry(name="prod", type="ssh", host="10.0.0.5", user="alice")
    save_targets_config(cfg_path, [(entry, "MY_PWD", "MY_PASS")])

    payload = yaml.safe_load(cfg_path.read_text())
    [written] = payload["targets"]
    assert written["password"] == "${MY_PWD}"
    assert written["passphrase"] == "${MY_PASS}"


# ---------------------------------------------------------------------------
# Crosscheck: save_targets_config dict shape == target add dict shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "password_env", "passphrase_env"),
    [
        # cred-less ssh
        (
            SSHEntry(name="credless", type="ssh", host="h", user="u"),
            None,
            None,
        ),
        # key_path ssh + non-default port + env-referenced creds
        (
            SSHEntry(
                name="keyed",
                type="ssh",
                host="h",
                user="u",
                port=2222,
                key_path="/tmp/id_rsa",
            ),
            "PWD_ENV",
            "PASS_ENV",
        ),
        # local
        (LocalEntry(name="loc", type="local"), None, None),
    ],
)
def test_save_dict_shape_matches_target_add(
    tmp_path: Path,
    entry: LocalEntry | SSHEntry,
    password_env: str | None,
    passphrase_env: str | None,
) -> None:
    """Spec §场景:与 target add 输出逐字段同形.

    The entry dict written by ``save_targets_config`` must equal what
    ``target add`` writes for the same logical entry — both share
    ``_entry_to_dict`` with the same env-name passthrough.
    """

    cfg_path = tmp_path / "targets.yaml"
    save_targets_config(cfg_path, [(entry, password_env, passphrase_env)])

    payload = yaml.safe_load(cfg_path.read_text())
    [written] = payload["targets"]
    expected = _entry_to_dict(entry, password_env=password_env, passphrase_env=passphrase_env)
    assert written == expected
