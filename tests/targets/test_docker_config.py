"""Tests for ``DockerEntry`` config layer — schema + ``docker_host`` validation.

Spec: ``openspec/changes/add-docker-target/specs/execution-target/spec.md``
§修改需求:`TargetsConfig` (docker scenarios). No docker daemon required —
this module only exercises Pydantic schema + the loader's ``docker_host``
scheme validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.targets.config import (
    DockerEntry,
    LocalEntry,
    load_targets_config,
)


def _write_config(tmp_path: Path, doc: object) -> Path:
    path = tmp_path / "targets.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


# ---------------------------------------------------------------------------
# Schema: routing + field set
# ---------------------------------------------------------------------------


def test_type_docker_routes_to_docker_entry(tmp_path: Path) -> None:
    """Spec §场景:type docker 路由到 DockerEntry."""

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [{"name": "web-ct", "type": "docker", "container": "my-app"}],
        },
    )
    config = load_targets_config(path)
    [entry] = config.targets
    assert isinstance(entry, DockerEntry)
    assert entry.type == "docker"
    assert entry.container == "my-app"
    assert entry.docker_host is None


def test_docker_specific_field_set_is_exactly_container_and_docker_host() -> None:
    """Spec §场景:TargetEntry docker 字段集严格 — exactly {container, docker_host}."""

    docker_specific = set(DockerEntry.model_fields.keys()) - set(LocalEntry.model_fields.keys())
    assert docker_specific == {"container", "docker_host"}


def test_container_missing_raises_validation_error() -> None:
    """Spec §场景:TargetEntry docker 字段集严格 — container is required."""

    with pytest.raises(ValidationError):
        DockerEntry(name="web-ct", type="docker")  # type: ignore[call-arg]


def test_container_empty_string_raises_validation_error() -> None:
    """Spec §场景:TargetEntry docker 字段集严格 — container min_length=1."""

    with pytest.raises(ValidationError):
        DockerEntry(name="web-ct", type="docker", container="")


def test_docker_entry_extra_field_forbidden() -> None:
    """Spec §场景:TargetEntry docker 字段集严格 — extra=forbid."""

    with pytest.raises(ValidationError):
        DockerEntry(
            name="web-ct",
            type="docker",
            container="my-app",
            image="alpine",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Placeholder rejection (field-name allowlist, before model_validate)
# ---------------------------------------------------------------------------


def test_placeholder_in_container_rejected(tmp_path: Path) -> None:
    """Spec §场景:占位出现在非 secret 字段 raise — ``${...}`` in container."""

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [
                {"name": "web-ct", "type": "docker", "container": "${CONTAINER_PLACEHOLDER}"}
            ],
        },
    )
    with pytest.raises(ConfigError) as exc_info:
        load_targets_config(path)
    assert exc_info.value.kind == "env_placeholder_not_allowed_here"


# ---------------------------------------------------------------------------
# docker_host validation (post model_validate loader step)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "docker_host",
    [
        "tcp://10.0.0.5:2376",
        "ssh://user@host",
        "http://10.0.0.5:2375",
        "https://10.0.0.5:2376",
        "npipe:////./pipe/docker_engine",
        "/var/run/docker.sock",
        "unix://",
        "unix:///",
        "UNIX://x",
        "Unix:///var/run/docker.sock",
        "unix://foo",
    ],
)
def test_docker_host_rejected_inputs(tmp_path: Path, docker_host: str) -> None:
    """Spec §场景:docker_host 远程 scheme / 裸路径 / 空 unix:// / 相对 socket 被拒."""

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [
                {"name": "web-ct", "type": "docker", "container": "x", "docker_host": docker_host}
            ],
        },
    )
    with pytest.raises(ConfigError) as exc_info:
        load_targets_config(path)
    assert exc_info.value.kind == "docker_host_remote_not_supported"
    assert exc_info.value.extra.get("field") == "docker_host"


def test_docker_host_valid_unix_socket_accepted_and_preserved(tmp_path: Path) -> None:
    """Spec §场景:docker_host 合法 unix:// 被接受 — value preserved, no raise."""

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [
                {
                    "name": "web-ct",
                    "type": "docker",
                    "container": "x",
                    "docker_host": "unix:///var/run/docker.sock",
                }
            ],
        },
    )
    config = load_targets_config(path)
    [entry] = config.targets
    assert isinstance(entry, DockerEntry)
    assert entry.docker_host == "unix:///var/run/docker.sock"


def test_docker_host_placeholder_hits_env_rejection_before_scheme_check(tmp_path: Path) -> None:
    """Spec §场景:docker_host 占位先于 scheme 校验命中.

    ``${...}`` rejection happens in ``_expand_placeholders`` (before
    ``model_validate``), so it must win over the ``unix://`` scheme check.
    """

    path = _write_config(
        tmp_path,
        {
            "version": "1",
            "targets": [
                {"name": "web-ct", "type": "docker", "container": "x", "docker_host": "${SOME_VAR}"}
            ],
        },
    )
    with pytest.raises(ConfigError) as exc_info:
        load_targets_config(path)
    assert exc_info.value.kind == "env_placeholder_not_allowed_here"
