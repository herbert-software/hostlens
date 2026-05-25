"""CI gate: builtin inspectors path must NOT be settings-configurable.

Per `openspec/changes/add-inspector-plugin-system/tasks.md` §12.2 and
proposal.md §"build_registry_from_search_paths": the builtin inspector
directory is hardcoded inside `build_registry_from_search_paths` and is
deliberately **not** exposed via `Settings`. Exposing it would let a user
or attacker override or shadow trusted builtin manifests, defeating the
`duplicate_inspector` fatal-raise that protects the registry.

This test asserts the invariant at the source-tree level so any future
patch adding `builtin_inspectors_path` (field or constant) under
`src/hostlens/` fails CI immediately.
"""

from __future__ import annotations

from pathlib import Path

FORBIDDEN_TOKENS: tuple[str, ...] = (
    "builtin_inspectors_path",
    "BUILTIN_INSPECTORS",
)


def test_builtin_inspectors_path_not_exposed_in_src() -> None:
    src_root = Path(__file__).resolve().parents[2] / "src" / "hostlens"
    assert src_root.is_dir(), f"expected src tree at {src_root}"

    offenders: list[tuple[Path, int, str]] = []
    for py_path in src_root.rglob("*.py"):
        try:
            text = py_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in FORBIDDEN_TOKENS:
                if token in line:
                    offenders.append((py_path, lineno, line.rstrip()))

    assert not offenders, (
        "builtin inspectors path must remain hardcoded in "
        "build_registry_from_search_paths and MUST NOT appear as a "
        "settings field, constant, or attribute under src/hostlens/. "
        f"Found {len(offenders)} offending reference(s): {offenders}"
    )
