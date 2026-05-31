"""Packaged replay assets, one subdirectory per scenario.

Each ``<key>/`` holds ``fixture.json`` (ReplayTarget canned command output)
and ``cassette.jsonl`` (PlaybackBackend recorded LLM responses). This is a
package so ``importlib.resources.files("hostlens.demo.scenarios")`` can locate
the assets in both source-tree and installed-wheel layouts (design D2).
"""

from __future__ import annotations
