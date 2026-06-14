"""Tests for ``CandidateTarget`` + ``normalize_target_name`` (task 1.1).

Spec: ``inventory-source/spec.md`` §需求:`CandidateTarget` 必须是未验证候选.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.models import CandidateTarget, normalize_target_name

# ---------------------------------------------------------------------------
# CandidateTarget — no plaintext secret field
# ---------------------------------------------------------------------------


def test_candidate_has_no_plaintext_secret_fields() -> None:
    """Model carries credential *references* only — no plaintext fields."""

    fields = set(CandidateTarget.model_fields)
    assert "password" not in fields
    assert "passphrase" not in fields
    assert {"password_env", "passphrase_env", "key_path"} <= fields


def test_candidate_rejects_unknown_field() -> None:
    """``extra="forbid"`` — a plaintext ``password`` cannot be smuggled in."""

    with pytest.raises(ValidationError):
        CandidateTarget(name="x", type="ssh", host="1.2.3.4", password="hunter2")  # type: ignore[call-arg]


def test_candidate_ssh_only_carries_references() -> None:
    cand = CandidateTarget(
        name="web1",
        type="ssh",
        host="10.0.0.1",
        user="root",
        port=22,
        password_env="WEB1_PW",
        key_path="/home/x/.ssh/id_ed25519",
    )
    assert cand.password_env == "WEB1_PW"
    assert cand.key_path == "/home/x/.ssh/id_ed25519"
    assert not hasattr(cand, "password")


def test_candidate_type_rejects_docker_k8s() -> None:
    """First-version import only produces local/ssh."""

    with pytest.raises(ValidationError):
        CandidateTarget(name="x", type="docker")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_target_name — four fixtures + edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Web-Prod", "web-prod"),  # uppercase
        ("Web-Prod.example", "web-prod-example"),  # uppercase + dot
        ("9lives", "lives"),  # leading digit stripped
        ("a" * 80, "a" * 64),  # over-length truncated to 64
    ],
)
def test_normalize_four_fixtures(raw: str, expected: str) -> None:
    assert normalize_target_name(raw) == expected


def test_normalize_already_legal_passthrough() -> None:
    assert normalize_target_name("bandwagon") == "bandwagon"


def test_normalize_collapses_runs_of_illegal_chars() -> None:
    assert normalize_target_name("a...b   c") == "a-b-c"


def test_normalize_pure_symbols_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        normalize_target_name("***")
    assert excinfo.value.kind == "invalid_target_name"
    assert excinfo.value.extra["raw_identifier"] == "***"


def test_normalize_leading_digits_only_after_strip_rejected() -> None:
    """``123`` → strip leading non-letters → empty → invalid_target_name."""

    with pytest.raises(ConfigError) as excinfo:
        normalize_target_name("123")
    assert excinfo.value.kind == "invalid_target_name"


# ---------------------------------------------------------------------------
# plaintext value never reaches the model (defense-in-depth)
# ---------------------------------------------------------------------------


def test_plaintext_value_not_in_model_dump() -> None:
    """A valid candidate's dump never contains a plaintext secret key."""

    cand = CandidateTarget(name="web1", type="ssh", host="10.0.0.1", password_env="X")
    dumped = cand.model_dump()
    assert "password" not in dumped
    assert "passphrase" not in dumped
