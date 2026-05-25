"""Tests for ``hostlens.inspectors.registry``.

The registry has two surfaces:

  * ``InspectorRegistry`` — the in-memory store. We pin the API contract
    (sorting, projection completeness, error vocabulary) so a future
    refactor that subtly changes return types is caught immediately.
  * ``build_registry_from_search_paths`` — the assembly factory. The
    interesting test cases here exercise the error-tier contract:
    builtins fail fatal; user paths collect per-file errors but
    propagate ``duplicate_inspector`` because silently overriding a
    builtin is a documented security risk.

The builtin fixtures are the real ``hello.echo`` / ``system.uptime``
manifests shipped under ``src/hostlens/inspectors/builtin/`` —
``test_builtin_inspectors.py`` pins their end-to-end content; here we
only assert "registry assembly with empty user paths surfaces both
names with no errors" which is a contract test, not a content test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.registry import (
    InspectorRegistry,
    RegistryBuildResult,
    RegistryLoadError,
    build_registry_from_search_paths,
)
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.tools.schemas.list_inspectors import InspectorSummary

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_manifest(
    name: str,
    *,
    version: str = "1.0.0",
    description: str = "test inspector",
    tags: list[str] | None = None,
    targets: list[str] | None = None,
) -> InspectorManifest:
    """Build a minimal valid ``InspectorManifest`` for registry tests.

    The output_schema is intentionally a tiny ``type: object`` so the
    manifest survives the loader-free construction path. We bypass the
    loader entirely because these tests target the registry, not yaml.
    """

    return InspectorManifest(
        name=name,
        version=version,
        description=description,
        tags=tags if tags is not None else [],
        targets=targets if targets is not None else ["local"],  # type: ignore[arg-type]
        collect=CollectSpec(command="echo hello"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
    )


_MINIMAL_YAML = """\
name: {name}
version: 1.0.0
description: minimal user manifest
targets:
  - local
collect:
  command: 'echo ok'
parse:
  format: raw
output_schema:
  type: object
"""


def _write_user_manifest(directory: Path, name: str, file_name: str | None = None) -> Path:
    """Write a minimal valid manifest yaml into ``directory``.

    ``file_name`` lets a caller put two manifests with the same `name`
    field into different ``*.yaml`` files for duplicate-detection tests.
    """

    file_path = directory / (file_name or f"{name.replace('.', '_')}.yaml")
    file_path.write_text(_MINIMAL_YAML.format(name=name))
    return file_path


# --------------------------------------------------------------------------- #
# InspectorRegistry API
# --------------------------------------------------------------------------- #


class TestInspectorRegistry:
    def test_register_and_get(self) -> None:
        registry = InspectorRegistry()
        manifest = _make_manifest("linux.cpu")
        registry.register(manifest)
        assert registry.get("linux.cpu") is manifest

    def test_register_with_source_path_remembered_on_duplicate(self, tmp_path: Path) -> None:
        registry = InspectorRegistry()
        first_path = tmp_path / "first.yaml"
        second_path = tmp_path / "second.yaml"
        manifest_a = _make_manifest("linux.cpu")
        manifest_b = _make_manifest("linux.cpu", description="other")
        registry.register(manifest_a, source_path=first_path)

        with pytest.raises(InspectorError) as exc:
            registry.register(manifest_b, source_path=second_path)
        assert exc.value.kind == "duplicate_inspector"
        assert exc.value.inspector == "linux.cpu"
        assert exc.value.existing_path == first_path
        assert exc.value.new_path == second_path

    def test_register_same_manifest_twice_raises(self) -> None:
        registry = InspectorRegistry()
        manifest = _make_manifest("linux.cpu")
        registry.register(manifest)
        with pytest.raises(InspectorError) as exc:
            registry.register(manifest)
        assert exc.value.kind == "duplicate_inspector"

    def test_get_missing_raises_inspector_not_found(self) -> None:
        registry = InspectorRegistry()
        with pytest.raises(InspectorError) as exc:
            registry.get("does.not.exist")
        assert exc.value.kind == "inspector_not_found"
        assert exc.value.inspector == "does.not.exist"

    def test_names_sorted_ascending(self) -> None:
        registry = InspectorRegistry()
        # Register in non-alphabetical order to prove sort happens at read
        # time, not insertion.
        registry.register(_make_manifest("b.x"))
        registry.register(_make_manifest("a.x"))
        registry.register(_make_manifest("c.x"))
        assert registry.names() == ["a.x", "b.x", "c.x"]

    def test_list_sorted_by_name(self) -> None:
        registry = InspectorRegistry()
        registry.register(_make_manifest("b.x"))
        registry.register(_make_manifest("a.x"))
        registry.register(_make_manifest("c.x"))
        ordered = registry.list()
        assert [m.name for m in ordered] == ["a.x", "b.x", "c.x"]

    def test_list_summaries_projection_complete(self) -> None:
        registry = InspectorRegistry()
        # Insert with non-sorted tag / target order to prove the projection
        # sorts both for prompt-cache stability.
        registry.register(
            _make_manifest(
                "linux.cpu",
                version="1.0.0",
                description="CPU check",
                tags=["linux", "cpu"],
                targets=["ssh", "local"],
            )
        )
        summaries = registry.list_summaries()
        assert summaries == [
            InspectorSummary(
                name="linux.cpu",
                version="1.0.0",
                description="CPU check",
                tags=["cpu", "linux"],
                compatible_target_kinds=["local", "ssh"],
            )
        ]

    def test_list_summaries_sorted_by_name(self) -> None:
        registry = InspectorRegistry()
        registry.register(_make_manifest("zeta.x"))
        registry.register(_make_manifest("alpha.x"))
        summaries = registry.list_summaries()
        assert [s.name for s in summaries] == ["alpha.x", "zeta.x"]


# --------------------------------------------------------------------------- #
# build_registry_from_search_paths
# --------------------------------------------------------------------------- #


class TestBuildRegistryFromSearchPaths:
    """Builtin path is hardcoded — every test in this group passes
    ``user_paths=[]`` plus per-test additions, so the builtin set
    (``hello.echo`` + ``system.uptime``) is always present in
    ``result.registry``."""

    def test_builtin_path_hardcoded_loads_two_inspectors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert isinstance(result, RegistryBuildResult)
        # Builtin set is the bare minimum — additional builtins added in
        # later milestones won't break this assertion (it's a `>=` check
        # via subset).
        assert "hello.echo" in result.registry.names()
        assert "system.uptime" in result.registry.names()
        assert result.errors == []

    def test_user_manifest_same_name_as_builtin_raises_duplicate(self, tmp_path: Path) -> None:
        _write_user_manifest(tmp_path, "hello.echo")
        with pytest.raises(InspectorError) as exc:
            build_registry_from_search_paths([tmp_path], settings=Settings())
        assert exc.value.kind == "duplicate_inspector"
        assert exc.value.inspector == "hello.echo"

    def test_two_user_paths_with_same_name_raise_duplicate(self, tmp_path: Path) -> None:
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_a.mkdir()
        path_b.mkdir()
        _write_user_manifest(path_a, "team.alpha")
        _write_user_manifest(path_b, "team.alpha")
        with pytest.raises(InspectorError) as exc:
            build_registry_from_search_paths([path_a, path_b], settings=Settings())
        assert exc.value.kind == "duplicate_inspector"

    def test_user_path_one_bad_yaml_two_good_collects_one_error(self, tmp_path: Path) -> None:
        # Two good manifests + one malformed yaml. Should load the two,
        # collect one parse-error entry, and NOT raise.
        _write_user_manifest(tmp_path, "team.good.one", file_name="good1.yaml")
        _write_user_manifest(tmp_path, "team.good.two", file_name="good2.yaml")
        bad_path = tmp_path / "bad.yaml"
        bad_path.write_text("name: [unclosed\n")

        result = build_registry_from_search_paths([tmp_path], settings=Settings())

        # Builtin set + the two good user manifests are registered.
        assert "team.good.one" in result.registry.names()
        assert "team.good.two" in result.registry.names()
        assert "hello.echo" in result.registry.names()
        assert "system.uptime" in result.registry.names()

        # Bad yaml collected, not raised.
        assert len(result.errors) == 1
        err = result.errors[0]
        assert isinstance(err, RegistryLoadError)
        assert err.path == bad_path
        assert err.kind == "manifest_parse_error"
        assert "manifest_parse_error" in err.detail

    def test_builtin_path_with_bad_yaml_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Stand up a fake "builtin" directory containing a broken yaml and
        # point the registry's path resolver at it. The expectation is
        # that file-level errors under the builtin tree propagate (a
        # shipped builtin yaml being broken is a maintainer bug, not a
        # collectable end-user error).
        fake_pkg_root = tmp_path / "pkg"
        fake_builtin = fake_pkg_root / "builtin"
        fake_builtin.mkdir(parents=True)
        bad_path = fake_builtin / "broken.yaml"
        bad_path.write_text("name: [unclosed\n")

        # Make the registry think this is the package directory by faking
        # the package's `__file__` (the resolver computes
        # ``Path(hostlens.inspectors.__file__).parent / 'builtin'``).
        fake_init = fake_pkg_root / "__init__.py"
        fake_init.write_text("")
        monkeypatch.setattr(
            "hostlens.inspectors.__file__",
            str(fake_init),
        )

        with pytest.raises(InspectorError) as exc:
            build_registry_from_search_paths([], settings=Settings())
        assert exc.value.kind == "manifest_parse_error"
        assert exc.value.path == bad_path
