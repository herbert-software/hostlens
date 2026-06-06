"""Unit tests for shared recording-lane helpers in ``_compose_record``."""

from __future__ import annotations

import pytest

from inspectors._compose_record import wait_until


class TestWaitUntil:
    def test_predicate_immediately_true_returns_without_waiting(self) -> None:
        calls = 0

        def predicate() -> bool:
            nonlocal calls
            calls += 1
            return True

        wait_until(predicate, timeout=1.0, interval_s=0.2)
        assert calls == 1

    def test_timeout_raises_instead_of_silently_passing(self) -> None:
        with pytest.raises(RuntimeError, match="wait_until timed out"):
            wait_until(lambda: False, timeout=0.05, interval_s=0.02)
