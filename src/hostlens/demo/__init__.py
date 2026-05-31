"""Offline demo pack — packaged replay assets + self-contained assembly.

Bundles the 8 incident-pack scenarios (``ReplayTarget`` fixture +
``PlaybackBackend`` cassette per scenario) as importable package data so a
clean ``pip install`` can run ``hostlens demo run <scenario>`` fully offline,
with zero external dependencies and no API key. This package is the single
source of truth for those assets; ``tests/incidents`` reads them back through
the bridge helpers here (design D1).
"""

from __future__ import annotations
