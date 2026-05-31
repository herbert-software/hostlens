"""Asset bridge — reader / writer access to packaged scenario assets (design D2).

Two access modes that must NOT collapse into one constant (design D2):

- **reader** (``reader_path``): used at demo runtime and by ``tests/incidents``
  reads. Locates the resource via ``importlib.resources.files(...)`` and
  materializes it to a real path with ``as_file()`` so it survives a zip-safe
  wheel where the resource is not a filesystem file. The returned context
  manager MUST stay open for as long as the path is used (the temp file is
  removed on exit) — callers hold it in an ``ExitStack`` spanning the whole run.

- **writer** (``source_tree_path``): used by re-record paths that must write the
  *committed* asset in the source tree. ``as_file()`` may hand back a read-only
  temp copy, so a writer must never use a reader path — it resolves the real
  source-tree location via the package ``origin``.

``asset_exists`` does the pre-flight existence check with the ``Traversable``
``is_file()`` API (zip-safe, does not materialize); it deliberately avoids
``os.path.exists`` / ``Path.exists`` which misjudge zip resources as absent.
"""

from __future__ import annotations

import contextlib
import importlib.util
from collections.abc import Iterator
from importlib.resources import as_file, files
from pathlib import Path
from typing import Literal

__all__ = [
    "AssetKind",
    "asset_exists",
    "basename_for",
    "reader_path",
    "source_tree_path",
]

AssetKind = Literal["fixture", "cassette"]

_SCENARIOS_PACKAGE = "hostlens.demo.scenarios"
_DEMO_PACKAGE = "hostlens.demo"

_BASENAMES: dict[AssetKind, str] = {
    "fixture": "fixture.json",
    "cassette": "cassette.jsonl",
}


def basename_for(kind: AssetKind) -> str:
    """Return the on-disk basename for an asset ``kind`` (design D2 schema)."""

    return _BASENAMES[kind]


@contextlib.contextmanager
def reader_path(key: str, kind: AssetKind) -> Iterator[Path]:
    """Yield a real filesystem path to the ``kind`` asset for ``key`` (reader).

    Zip-safe: locates the resource via ``importlib.resources`` and falls back
    to a temp copy through ``as_file()`` when the resource is not a real file.
    The yielded path is valid only inside the ``with`` block — hold it in an
    ``ExitStack`` for the whole consuming run (design D2 lifecycle).
    """

    resource = files(_SCENARIOS_PACKAGE).joinpath(key, basename_for(kind))
    with as_file(resource) as path:
        yield path


def source_tree_path(key: str, kind: AssetKind) -> Path:
    """Return the writable source-tree path to the ``kind`` asset for ``key``.

    For re-record writers only (and dev-time assertions). Resolves the real
    location under ``src/hostlens/demo/scenarios/`` via the package spec origin
    — never an ``as_file()`` temp copy (which may be read-only).
    """

    spec = importlib.util.find_spec(_DEMO_PACKAGE)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"cannot locate package {_DEMO_PACKAGE!r} origin")
    package_dir = Path(spec.origin).parent
    return package_dir / "scenarios" / key / basename_for(kind)


def asset_exists(key: str, kind: AssetKind) -> bool:
    """Return whether the ``kind`` asset for ``key`` is present (pre-flight).

    Uses the ``Traversable.is_file()`` API (zip-safe, no materialization) per
    design D2 — never ``os.path.exists`` / ``Path.exists``.
    """

    return files(_SCENARIOS_PACKAGE).joinpath(key, basename_for(kind)).is_file()
