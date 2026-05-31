"""Pure path assertion for the conftest record-mode write target (task 2.7b).

CI gate that needs no real API key: proves the ``llm_cassette`` record branch
resolves ``incident_<key>`` names to the SOURCE TREE
(``src/hostlens/demo/scenarios/<key>/cassette.jsonl`` via ``source_tree_path``)
rather than an ``as_file`` read-only temp copy. Writing a re-recording to a temp
copy would silently drop it and could resurrect the pre-migration dual-asset
layout — this assertion guards that the migration's writer #2 lands in the
committed tree.
"""

from __future__ import annotations

import conftest


def test_incident_record_path_is_source_tree() -> None:
    path = conftest._cassette_record_path("incident_cpu_saturation")
    parts = path.parts
    # Must land in the committed package tree, not a tmp materialization.
    assert "src" in parts
    assert path.parent.name == "cpu_saturation"
    assert "scenarios" in parts
    assert path.name == "cassette.jsonl"
    assert "demo" in parts


def test_non_incident_record_path_stays_flat() -> None:
    path = conftest._cassette_record_path("planner_health_check")
    assert path.parent.name == "cassettes"
    assert path.name == "planner_health_check.jsonl"
