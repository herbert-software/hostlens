"""Tests for the M1 builtin Inspector manifests (Group 6 — Tasks 9.1 + 9.2).

The end-to-end ``runner.run(...)`` exercise lives in Group 7 / 8b once
the runner + ToolRegistry dispatch are wired; here we pin the static
contract:

  * Each builtin yaml passes ``load_manifest`` cleanly (so the loader's
    Jinja2 AST walker / parameter-schema walker / ReDoS detector are all
    happy with the M1 fixtures we ship).
  * ``build_registry_from_search_paths([], settings=Settings())``
    surfaces both manifests with ``errors == []`` — the integration
    point Group 7 / 8b will build on.
"""

from __future__ import annotations

from pathlib import Path

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.registry import build_registry_from_search_paths


def _builtin_root() -> Path:
    """Return the directory holding the shipped builtin manifests.

    Computed exactly the way the registry's resolver does so the test
    can never drift from the production lookup path.
    """

    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


# --------------------------------------------------------------------------- #
# Task 9.1 — hello.echo
# --------------------------------------------------------------------------- #


class TestHelloEcho:
    def test_loader_accepts_echo_manifest(self) -> None:
        manifest = load_manifest(_builtin_root() / "hello" / "echo.yaml")

        assert manifest.name == "hello.echo"
        assert manifest.version == "1.0.0"
        assert manifest.targets == ["local", "ssh"]
        assert "demo" in manifest.tags
        assert manifest.privilege == "none"
        assert manifest.collect.command == "echo hello"
        assert manifest.collect.timeout_seconds == 5
        assert manifest.parse.format == "raw"
        assert manifest.parse.raw_extract_regex is None
        # One aggregate-mode finding referencing the top-level `raw`
        # output field — design Decision 8's minimal `Finding` model
        # mandates the message be solvable at runtime via
        # ``template.format(**output)``.
        assert len(manifest.findings) == 1
        finding = manifest.findings[0]
        assert finding.severity == "info"
        assert finding.for_each is None
        assert "{raw}" in finding.message

    def test_registry_contains_hello_echo(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        manifest = result.registry.get("hello.echo")
        assert manifest.name == "hello.echo"
        assert result.errors == []


# --------------------------------------------------------------------------- #
# Task 9.2 — system.uptime
# --------------------------------------------------------------------------- #


class TestSystemUptime:
    def test_loader_accepts_uptime_manifest(self) -> None:
        manifest = load_manifest(_builtin_root() / "system" / "uptime.yaml")

        assert manifest.name == "system.uptime"
        assert manifest.version == "1.0.0"
        assert manifest.targets == ["local", "ssh"]
        assert "linux" in manifest.tags
        assert manifest.requires_capabilities == ["shell"]
        assert manifest.requires_binaries == ["uptime"]
        assert manifest.collect.command == "uptime"
        assert manifest.parse.format == "raw"
        assert manifest.parse.columns == ["load1", "load5", "load15"]
        # All three named groups must round-trip via the regex; columns
        # length must match (the ParseSpec model_validator enforces this
        # but pinning the literal column set here documents the contract
        # the runner will rely on once Group 7 wires `_parse_raw`).
        assert manifest.parse.raw_extract_regex is not None
        assert "(?P<load1>" in manifest.parse.raw_extract_regex
        assert "(?P<load5>" in manifest.parse.raw_extract_regex
        assert "(?P<load15>" in manifest.parse.raw_extract_regex
        # Two aggregate-mode findings staircase warning -> critical.
        assert len(manifest.findings) == 2
        severities = {r.severity for r in manifest.findings}
        assert severities == {"warning", "critical"}

    def test_registry_contains_system_uptime(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        manifest = result.registry.get("system.uptime")
        assert manifest.name == "system.uptime"
        assert result.errors == []
