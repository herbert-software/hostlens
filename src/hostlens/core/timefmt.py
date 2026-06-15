"""Host-local timezone conversion for human-readable report timestamps.

Report timestamps are stored UTC-aware everywhere (comparable across
regions, robust to NTP step-backs, the basis for baseline ordering and
diff). Only the human-readable rendering layer converts to the host's
system-local timezone — see the ``render-report-time-in-host-local-tz``
capability. Machine-facing serialisations (persisted ``report_json``,
``--format json``) and structlog log timestamps stay UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime


def to_host_local(value: datetime) -> datetime:
    """Return ``value`` in the host's system-local timezone.

    A naive ``value`` is interpreted as UTC first — report timestamps are
    UTC by contract, and ``datetime.astimezone()`` would otherwise assume a
    naive value is already local and skip the conversion. An aware ``value``
    is converted as-is (same instant, different wall clock).
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone()
