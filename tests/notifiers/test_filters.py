"""Tests for shared notifier Jinja filters (``hostlens.notifiers._filters``).

``fmt_time`` renders the report timestamp in the host's local timezone, so
the ``shanghai_tz`` fixture (conftest) pins ``TZ`` for deterministic
assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hostlens.notifiers._filters import fmt_time


def test_fmt_time_renders_host_local(shanghai_tz: None) -> None:
    # 08:55 UTC → 16:55 Asia/Shanghai (the real ts.mac-mini symptom).
    assert fmt_time(datetime(2026, 6, 15, 8, 55, 0, tzinfo=UTC)) == "2026-06-15 16:55"


def test_fmt_time_naive_treated_as_utc(shanghai_tz: None) -> None:
    assert fmt_time(datetime(2026, 6, 15, 8, 55, 0)) == "2026-06-15 16:55"


def test_fmt_time_does_not_leak_utc_wall_clock(shanghai_tz: None) -> None:
    # Regression anchor: the UTC hour (08) must not appear in the local render.
    assert "08:55" not in fmt_time(datetime(2026, 6, 15, 8, 55, 0, tzinfo=UTC))
