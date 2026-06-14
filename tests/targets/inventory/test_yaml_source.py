"""Tests for the ``yaml`` inventory source (task 1.4).

Spec: ``inventory-source/spec.md`` §需求:`yaml` source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.sources.yaml import YamlSource


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


def test_can_handle_yml_mapping(tmp_path: Path) -> None:
    ref = _write(tmp_path / "inv.yml", "group:\n  h1:\n    type: local\n")
    assert YamlSource().can_handle(str(ref)) is True


def test_can_handle_rejects_non_mapping(tmp_path: Path) -> None:
    ref = _write(tmp_path / "inv.yaml", "- a\n- b\n")
    assert YamlSource().can_handle(str(ref)) is False


def test_can_handle_rejects_no_extension(tmp_path: Path) -> None:
    ref = _write(tmp_path / "hosts", "group:\n  h1:\n    type: local\n")
    assert YamlSource().can_handle(str(ref)) is False


# ---------------------------------------------------------------------------
# standard schema + defaults filtered by type
# ---------------------------------------------------------------------------


def test_defaults_filtered_by_type(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "defaults:\n  user: root\n"
        "hosts_proxy:\n  web1:\n    type: ssh\n    host: 10.0.0.1\n"
        "hosts_local:\n  l1:\n    type: local\n",
    )
    candidates = {c.name: c for c in YamlSource().parse(str(ref))}
    assert candidates["web1"].user == "root"  # default applied to ssh
    # ``l1`` skips ``user`` (local has no such field) — must not raise.
    assert candidates["l1"].type == "local"
    assert candidates["l1"].user is None


def test_local_entry_only_type(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "hosts_local:\n  demo-localhost:\n    type: local\n",
    )
    candidates = YamlSource().parse(str(ref))
    assert len(candidates) == 1
    assert candidates[0].name == "demo-localhost"
    assert candidates[0].type == "local"
    assert candidates[0].host is None


def test_ssh_full_fields(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  web:\n    type: ssh\n    host: 1.2.3.4\n    user: admin\n"
        "    port: 2200\n    password_env: WEB_PW\n    key_path: /k/id\n",
    )
    cand = YamlSource().parse(str(ref))[0]
    assert cand.host == "1.2.3.4"
    assert cand.user == "admin"
    assert cand.port == 2200
    assert cand.password_env == "WEB_PW"
    assert cand.key_path == "/k/id"


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_ssh_missing_host_points_to_ssh_config(tmp_path: Path) -> None:
    ref = _write(tmp_path / "inv.yml", "g:\n  web:\n    type: ssh\n    user: root\n")
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "missing_required_field"
    assert "ssh_config" in str(excinfo.value).lower()


def test_type_docker_rejected(tmp_path: Path) -> None:
    ref = _write(tmp_path / "inv.yml", "g:\n  c:\n    type: docker\n    host: 1.2.3.4\n")
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_target_type"


def test_invalid_password_env_value_rejected(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        'g:\n  web:\n    type: ssh\n    host: 1.2.3.4\n    password_env: "lower case"\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_env_var_name"


def test_plaintext_password_fail_closed(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  web:\n    type: ssh\n    host: 1.2.3.4\n    password: hunter2\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "plaintext_secret_forbidden"
    assert excinfo.value.extra["field"] == "password"
    # The plaintext value must never reach the exception surface.
    assert "hunter2" not in str(excinfo.value)


def test_plaintext_passphrase_in_defaults_fail_closed(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "defaults:\n  passphrase: leaky\ng:\n  web:\n    type: ssh\n    host: 1.2.3.4\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "plaintext_secret_forbidden"
    assert "leaky" not in str(excinfo.value)


def test_unsupported_field_rejected(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  web:\n    type: ssh\n    host: 1.2.3.4\n    tailscale_ipv4: 100.1.1.1\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_entry_field"


def test_empty_inventory_returns_empty(tmp_path: Path) -> None:
    ref = _write(tmp_path / "inv.yml", "")
    assert YamlSource().parse(str(ref)) == []


def test_normalized_name_collision_rejected(tmp_path: Path) -> None:
    # ``Web.Prod`` and ``Web-Prod`` both normalize to ``web-prod`` → collision.
    ref = _write(
        tmp_path / "inv.yml",
        'hosts:\n  "Web.Prod": {type: ssh, host: 1.1.1.1}\n'
        '  "Web-Prod": {type: ssh, host: 2.2.2.2}\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "ambiguous_target_name"


def test_invalid_port_raises_config_error(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    port: notaport\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_entry"


def test_port_out_of_range_rejected(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    port: 70000\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_entry"


def test_port_zero_rejected(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    port: 0\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_entry"


def test_port_bool_rejected(tmp_path: Path) -> None:
    """``port: true`` must not silently coerce to 1 (bool is an int subclass)."""
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    port: true\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_entry"


def test_binary_file_raises_config_error_not_traceback(tmp_path: Path) -> None:
    """A non-UTF-8 file → ConfigError (exit 2), never an uncaught traceback."""
    ref = tmp_path / "inv.yaml"
    ref.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "yaml_read_error"


def test_can_handle_binary_yaml_returns_false(tmp_path: Path) -> None:
    ref = tmp_path / "inv.yaml"
    ref.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    assert YamlSource().can_handle(str(ref)) is False


def test_key_path_tilde_expanded(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    key_path: ~/.ssh/id_x\n",
    )
    candidates = YamlSource().parse(str(ref))
    assert candidates[0].key_path is not None
    assert "~" not in candidates[0].key_path


def test_key_path_placeholder_rejected(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "inv.yml",
        "g:\n  h:\n    type: ssh\n    host: 1.1.1.1\n    key_path: ${KEY}/id\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        YamlSource().parse(str(ref))
    assert excinfo.value.kind == "key_path_placeholder_forbidden"
