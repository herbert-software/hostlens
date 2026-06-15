"""Tests for ``hostlens.core.timefmt.to_host_local``.

The conversion uses the process system-local timezone (``astimezone()`` with
no arg), so the ``shanghai_tz`` fixture (conftest) pins ``TZ`` for
deterministic assertions across a UTC CI runner and a local CST dev box.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hostlens.core.timefmt import to_host_local


def test_aware_utc_converts_to_local(shanghai_tz: None) -> None:
    # 12:00 UTC → 20:00 Asia/Shanghai (+08:00): same instant, local wall clock.
    out = to_host_local(datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC))
    assert out.hour == 20
    assert out.utcoffset() == timedelta(hours=8)


def test_naive_is_interpreted_as_utc_then_converted(shanghai_tz: None) -> None:
    # A naive value is the report contract's UTC, NOT local — so it must shift
    # to 20:00, not stay 12:00 (which a bare astimezone() would do).
    out = to_host_local(datetime(2026, 5, 26, 12, 0, 0))
    assert out.hour == 20
    assert out.utcoffset() == timedelta(hours=8)


def test_conversion_actually_happens(shanghai_tz: None) -> None:
    # Anchor against a regression back to raw-UTC rendering: under a non-UTC
    # TZ the local wall clock must differ from the UTC wall clock.
    src = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    assert to_host_local(src).hour != src.hour


def test_same_instant_preserved(shanghai_tz: None) -> None:
    src = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    # Conversion is timezone-only — the absolute instant is unchanged.
    assert to_host_local(src).astimezone(UTC) == src
