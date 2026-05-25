"""Tests for ``hostlens.targets.base.ExecResult`` Pydantic model.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`ExecResult` 必须把 `timed_out` 与 `exit_code` 字段分离, 超时时 `exit_code=None`.

Six scenarios, one per spec scenario:
1. Timed-out result: ``timed_out=True`` together with ``exit_code=None``.
2. Non-zero exit returns a real wait status (no auto-coercion).
3. Signal-killed processes carry the POSIX ``128 + signum`` exit code
   (e.g. SIGSEGV → 139) — must NOT be conflated with the timeout case.
4. Model-layer validator rejects the invariant violation
   ``timed_out=True, exit_code=0``.
5. UTF-8 fault tolerance: bytes that fail UTF-8 decode are surfaced as
   ``\\ufffd`` (replacement character), never raise.
6. Frozen model: assigning to fields after construction raises.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.targets.base import ExecResult


def test_timed_out_result_has_none_exit_code() -> None:
    """Spec §场景:超时时 timed_out=True 且 exit_code=None.

    Callers branching on ``timed_out`` must see ``exit_code=None``;
    the magic ``-1`` marker is explicitly forbidden by the spec.
    """

    result = ExecResult(
        exit_code=None,
        stdout="",
        stderr="",
        duration_seconds=1.25,
        timed_out=True,
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_seconds == pytest.approx(1.25)


def test_non_zero_exit_code_preserved_as_is() -> None:
    """Spec §场景:正常返回非零 exit_code.

    A real ``exit 42`` must surface as ``exit_code=42`` — no auto-coercion,
    no special handling of "non-zero" inside the model.
    """

    result = ExecResult(
        exit_code=42,
        stdout="",
        stderr="bye\n",
        duration_seconds=0.01,
        timed_out=False,
    )

    assert result.timed_out is False
    assert result.exit_code == 42


def test_signal_killed_returns_128_plus_signum() -> None:
    """Spec §场景:signal-killed 命令返回 128+signum.

    POSIX wait-status convention is preserved as-is: SIGSEGV=11 → 139.
    Treating these as ``-1`` (the rejected timeout sentinel) would
    collide with this case — that's precisely why the spec mandates
    ``int | None`` and a separate ``timed_out`` flag.
    """

    result = ExecResult(
        exit_code=139,  # 128 + SIGSEGV(11)
        stdout="",
        stderr="",
        duration_seconds=0.05,
        timed_out=False,
    )

    assert result.exit_code == 139
    assert result.timed_out is False


def test_model_validator_blocks_timed_out_with_concrete_exit_code() -> None:
    """Spec §场景:模型层强制 timed_out 蕴含 exit_code=None.

    The invariant ``timed_out is True ⇒ exit_code is None`` is enforced
    at the model layer; the reverse implication is intentionally NOT
    enforced (``exit_code=None and not timed_out`` is a legal
    "remote dropped without wait status" outcome).
    """

    with pytest.raises(ValidationError):
        ExecResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=1.0,
            timed_out=True,
        )


def test_non_utf8_bytes_surface_as_replacement_character() -> None:
    """Spec §场景:stdout/stderr 非 UTF-8 字节不 raise.

    Callers receive ``stdout: str`` so subprocess decoders must use
    ``errors="replace"`` — the replacement character ``\\ufffd`` is the
    documented contract for the "could not decode" case.
    """

    decoded = b"\xff\xfe".decode("utf-8", errors="replace")
    assert "�" in decoded

    result = ExecResult(
        exit_code=0,
        stdout=decoded,
        stderr="",
        duration_seconds=0.001,
        timed_out=False,
    )

    assert "�" in result.stdout


def test_exec_result_is_frozen() -> None:
    """Spec §场景:ExecResult 实例不可变.

    ``model_config = ConfigDict(frozen=True)`` makes the dataclass
    behave immutably; attempts to mutate a constructed result must
    raise ``ValidationError`` (Pydantic v2's frozen-model error class).
    """

    result = ExecResult(
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        timed_out=False,
    )

    with pytest.raises(ValidationError):
        result.exit_code = 1  # type: ignore[misc]
