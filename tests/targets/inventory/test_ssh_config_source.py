"""Tests for the ``ssh_config`` inventory source (task 1.3).

Spec: ``inventory-source/spec.md`` §需求:`ssh_config` source.

The ``Include`` security scenarios rebind ``HOME`` so ``~/.ssh`` points at a
tmp tree; the source's ``realpath(~/.ssh)`` boundary is exercised against
real symlinks (no mocking of os.path).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.sources.ssh_config import SshConfigSource


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


def test_can_handle_basename_config(tmp_path: Path) -> None:
    ref = _write(tmp_path / "config", "Host x\n")
    assert SshConfigSource().can_handle(str(ref)) is True


def test_can_handle_hosts_basename(tmp_path: Path) -> None:
    ref = _write(tmp_path / "hosts", "Host x\n  HostName 1.2.3.4\n")
    assert SshConfigSource().can_handle(str(ref)) is True


def test_can_handle_content_directive(tmp_path: Path) -> None:
    ref = _write(tmp_path / "random.txt", "# comment\nHost gw\n")
    assert SshConfigSource().can_handle(str(ref)) is True


def test_can_handle_rejects_plain_yaml(tmp_path: Path) -> None:
    ref = _write(tmp_path / "x.txt", "key: value\nother: 1\n")
    assert SshConfigSource().can_handle(str(ref)) is False


# ---------------------------------------------------------------------------
# multi-alias canonical name + HostName
# ---------------------------------------------------------------------------


def test_multi_alias_canonical_name_and_hostname(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "hosts",
        "Host bwg bandwagon\n  HostName 100.76.213.134\n  User root\n  Port 2222\n",
    )
    candidates = SshConfigSource().parse(str(ref))
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.name == "bandwagon"
    assert cand.host == "100.76.213.134"
    assert cand.user == "root"
    assert cand.port == 2222
    assert cand.type == "ssh"


def test_ipv6_hostname_preserved_addressfamily_dropped(tmp_path: Path) -> None:
    """IPv6-only telegrambot form: keep IPv6 literal, no DNS, drop AddressFamily."""

    ref = _write(
        tmp_path / "config",
        "Host telegrambot\n  HostName fd7a:115c::6874\n  AddressFamily inet6\n",
    )
    candidates = SshConfigSource().parse(str(ref))
    assert candidates[0].host == "fd7a:115c::6874"
    # AddressFamily is not a CandidateTarget field — it cannot land anywhere.
    assert "address_family" not in candidates[0].model_dump()


def test_hostname_missing_takes_host_token(tmp_path: Path) -> None:
    ref = _write(tmp_path / "config", "Host gw\n  User admin\n")
    candidates = SshConfigSource().parse(str(ref))
    assert candidates[0].host == "gw"
    assert candidates[0].name == "gw"


# ---------------------------------------------------------------------------
# IdentityFile: ~ expansion vs ${VAR} fail-closed
# ---------------------------------------------------------------------------


def test_identity_file_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ref = _write(tmp_path / "config", "Host gw\n  HostName 1.2.3.4\n  IdentityFile ~/.ssh/id\n")
    candidates = SshConfigSource().parse(str(ref))
    assert candidates[0].key_path == str(tmp_path / ".ssh" / "id")
    assert "~" not in candidates[0].key_path


def test_identity_file_var_placeholder_rejected(tmp_path: Path) -> None:
    ref = _write(tmp_path / "config", "Host gw\n  HostName 1.2.3.4\n  IdentityFile ${KEY_DIR}/k\n")
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "key_path_placeholder_forbidden"


# ---------------------------------------------------------------------------
# Match / wildcard Host skipped
# ---------------------------------------------------------------------------


def test_wildcard_host_skipped(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "config",
        "Host *\n  User root\n\nHost gw\n  HostName 1.2.3.4\n",
    )
    candidates = SshConfigSource().parse(str(ref))
    assert len(candidates) == 1
    assert candidates[0].name == "gw"


def test_match_block_skipped(tmp_path: Path) -> None:
    ref = _write(
        tmp_path / "config",
        "Match host gw\n  User root\n\nHost real\n  HostName 1.2.3.4\n",
    )
    candidates = SshConfigSource().parse(str(ref))
    assert [c.name for c in candidates] == ["real"]


# ---------------------------------------------------------------------------
# Include boundary security
# ---------------------------------------------------------------------------


def _make_ssh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return ssh


def test_include_in_tree_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    _write(ssh / "hosts", "Host inc\n  HostName 9.9.9.9\n")
    ref = _write(ssh / "config", "Include ~/.ssh/hosts\n\nHost top\n  HostName 1.1.1.1\n")
    candidates = SshConfigSource().parse(str(ref))
    names = {c.name for c in candidates}
    assert names == {"inc", "top"}


def test_pre_host_directives_apply_as_global_defaults(tmp_path: Path) -> None:
    """Directives before the first Host are OpenSSH globals (implicit Host *).

    A host-specific directive still wins over the global default.
    """
    ref = _write(
        tmp_path / "config",
        "User globaluser\n"
        "Port 2222\n"
        "\n"
        "Host alpha\n"
        "  HostName 1.1.1.1\n"
        "\n"
        "Host beta\n"
        "  HostName 2.2.2.2\n"
        "  User betauser\n",
    )
    candidates = {c.name: c for c in SshConfigSource().parse(str(ref))}
    assert candidates["alpha"].user == "globaluser"
    assert candidates["alpha"].port == 2222
    # beta's explicit User overrides the global default; Port is inherited.
    assert candidates["beta"].user == "betauser"
    assert candidates["beta"].port == 2222


def test_include_absolute_outside_tree_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    ref = _write(ssh / "config", "Include /etc/shadow\n")
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "include_path_escape"
    # Exception text must not echo the escaping target's basename — only a
    # fixed message + the path kind (no content / path leak in the audit trail).
    assert "shadow" not in str(excinfo.value).lower()


def test_include_symlink_escape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``~/.ssh/evil -> outside`` path string is in-tree but realpath escapes."""

    ssh = _make_ssh_home(tmp_path, monkeypatch)
    outside = tmp_path / "secret_outside"
    _write(outside, "Host leaked\n  HostName 6.6.6.6\n")
    evil = ssh / "evil"
    os.symlink(str(outside), str(evil))
    ref = _write(ssh / "config", "Include ~/.ssh/evil\n")
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "include_path_escape"


def test_include_when_ssh_dir_is_symlink_not_misrejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate Include when ``~/.ssh`` itself is a symlink must NOT be rejected."""

    home = tmp_path / "home"
    home.mkdir(parents=True)
    real_ssh = tmp_path / "dotfiles" / "ssh"
    real_ssh.mkdir(parents=True)
    os.symlink(str(real_ssh), str(home / ".ssh"))
    monkeypatch.setenv("HOME", str(home))

    _write(real_ssh / "hosts", "Host inc\n  HostName 9.9.9.9\n")
    ref = _write(real_ssh / "config", "Include ~/.ssh/hosts\n")
    candidates = SshConfigSource().parse(str(ref))
    assert {c.name for c in candidates} == {"inc"}


def test_nested_include_one_level_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    _write(ssh / "deep", "Host deep\n  HostName 2.2.2.2\n")
    _write(ssh / "hosts", "Include ~/.ssh/deep\n\nHost mid\n  HostName 3.3.3.3\n")
    ref = _write(ssh / "config", "Include ~/.ssh/hosts\n")
    candidates = SshConfigSource().parse(str(ref))
    # ``deep`` is reached via a nested Include and is dropped (one level only).
    assert {c.name for c in candidates} == {"mid"}


# ---------------------------------------------------------------------------
# malformed input → structured ConfigError (not a raw crash)
# ---------------------------------------------------------------------------


def test_empty_host_line_raises_config_error(tmp_path: Path) -> None:
    ref = _write(tmp_path / "config", "Host\n  HostName 1.2.3.4\n")
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_ssh_config"


def test_non_numeric_port_raises_config_error(tmp_path: Path) -> None:
    ref = _write(tmp_path / "config", "Host gw\n  HostName 1.2.3.4\n  Port notaport\n")
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "invalid_ssh_config"


def test_normalized_name_collision_rejected(tmp_path: Path) -> None:
    # ``Web.Prod`` and ``Web-Prod`` both normalize to ``web-prod`` → collision.
    ref = _write(
        tmp_path / "config",
        "Host Web.Prod\n  HostName 1.1.1.1\nHost Web-Prod\n  HostName 2.2.2.2\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        SshConfigSource().parse(str(ref))
    assert excinfo.value.kind == "ambiguous_target_name"


def test_include_relative_anchored_to_ssh_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative Include anchors to ~/.ssh (OpenSSH rule), not the CWD."""
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    (ssh / "config.d").mkdir()
    _write(ssh / "config.d" / "hosts", "Host rel\n  HostName 5.5.5.5\n")
    ref = _write(ssh / "config", "Include config.d/hosts\n")
    candidates = SshConfigSource().parse(str(ref))
    assert {c.name for c in candidates} == {"rel"}


def test_include_glob_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Include ~/.ssh/config.d/*`` expands the glob to every matching file."""
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    (ssh / "config.d").mkdir()
    _write(ssh / "config.d" / "a.conf", "Host ga\n  HostName 1.1.1.1\n")
    _write(ssh / "config.d" / "b.conf", "Host gb\n  HostName 2.2.2.2\n")
    ref = _write(ssh / "config", "Include ~/.ssh/config.d/*\n")
    candidates = SshConfigSource().parse(str(ref))
    assert {c.name for c in candidates} == {"ga", "gb"}


def test_include_home_outside_ssh_dir_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented tizi pattern: ~/.ssh/config includes ~/tizi/hosts (in home)."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "tizi").mkdir()
    _write(home / "tizi" / "hosts", "Host tz\n  HostName 7.7.7.7\n")
    monkeypatch.setenv("HOME", str(home))
    ref = _write(home / ".ssh" / "config", "Include ~/tizi/hosts\n")
    candidates = SshConfigSource().parse(str(ref))
    assert {c.name for c in candidates} == {"tz"}


def test_host_star_provides_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Host *`` directives are defaults applied to every host; explicit wins."""
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    ref = _write(
        ssh / "config",
        "Host *\n  User globaluser\n  Port 2200\n"
        "Host foo\n  HostName 1.1.1.1\n"
        "Host bar\n  HostName 2.2.2.2\n  User specific\n",
    )
    by_name = {c.name: c for c in SshConfigSource().parse(str(ref))}
    assert by_name["foo"].user == "globaluser"  # default applied
    assert by_name["foo"].port == 2200
    assert by_name["bar"].user == "specific"  # host-specific overrides default
    assert by_name["bar"].port == 2200  # default still fills the gap


def test_host_star_defaults_apply_to_included_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Host *`` defaults reach hosts pulled in via ``Include``."""
    ssh = _make_ssh_home(tmp_path, monkeypatch)
    _write(ssh / "extra", "Host inc\n  HostName 9.9.9.9\n")
    ref = _write(ssh / "config", "Host *\n  User globaluser\nInclude ~/.ssh/extra\n")
    by_name = {c.name: c for c in SshConfigSource().parse(str(ref))}
    assert by_name["inc"].user == "globaluser"
