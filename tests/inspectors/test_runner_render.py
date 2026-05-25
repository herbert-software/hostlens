"""Tests for `InspectorRunner._render_command`.

Three contracts:

  * Jinja2 + custom `sh` filter wraps every interpolated value in
    `shlex.quote(str(value))`. Injection payloads in parameters do NOT
    survive as executable shell after rendering.
  * `secrets_env` collects every declared secret from `os.environ`
    (preflight has already guaranteed presence; this is the read-back).
  * Jinja2 errors propagate so `run()` can map them to status="exception".
  * The `sh` filter rejects `None` / empty list to avoid silent empty
    rendering.
"""

from __future__ import annotations

import shlex

import jinja2
import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.registry import TargetRegistry


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


def _make_manifest(
    *,
    command: str = "echo hello",
    secrets: list[str] | None = None,
) -> InspectorManifest:
    return InspectorManifest(
        name="test.render",
        version="1.0.0",
        description="test",
        targets=["local"],
        secrets=secrets or [],
        collect=CollectSpec(command=command),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )


async def test_simple_render_no_parameters() -> None:
    runner = _runner()
    manifest = _make_manifest(command="echo hello")
    cmd, secrets_env = await runner._render_command(manifest, None)
    assert cmd == "echo hello"
    assert secrets_env == {}


async def test_sh_filter_quotes_simple_value() -> None:
    runner = _runner()
    manifest = _make_manifest(command="ping {{ host | sh }}")
    cmd, _ = await runner._render_command(manifest, {"host": "example.com"})
    assert cmd == f"ping {shlex.quote('example.com')}"


async def test_sh_filter_neutralizes_injection_payload() -> None:
    """Payload `'; rm -rf /` must NOT survive as executable shell."""

    runner = _runner()
    manifest = _make_manifest(command="ping {{ host | sh }}")
    payload = "'; rm -rf /"
    cmd, _ = await runner._render_command(manifest, {"host": payload})
    # The rendered command must equal `ping <shlex.quote(payload)>`. After
    # shell evaluation, the payload becomes a single literal argument, not
    # a sequence of commands.
    assert cmd == f"ping {shlex.quote(payload)}"
    # The literal `'; rm -rf /` should NOT appear unquoted in the result.
    assert "; rm -rf /" not in cmd or cmd.count("'") >= 2


async def test_secrets_env_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "literal-secret-12345")
    monkeypatch.setenv("API_TOKEN", "abc123")
    runner = _runner()
    # Secrets must NOT appear in the rendered command (loader rejects that).
    manifest = _make_manifest(
        command="psql ...",  # the secret is consumed via env, not via {{ }}
        secrets=["PGPASSWORD", "API_TOKEN"],
    )
    cmd, secrets_env = await runner._render_command(manifest, None)
    assert cmd == "psql ..."
    assert secrets_env == {
        "PGPASSWORD": "literal-secret-12345",
        "API_TOKEN": "abc123",
    }


async def test_undefined_parameter_propagates() -> None:
    runner = _runner()
    manifest = _make_manifest(command="echo {{ missing_var | sh }}")
    # Jinja2's default mode treats undefined as empty string, but with our
    # current settings, calling `| sh` on an Undefined raises since the
    # filter receives a value that stringifies to empty. We expect either
    # an UndefinedError or a TemplateError from the strict autoescape=False
    # environment — depending on Jinja2 internals, let's test that some
    # Jinja2 error surfaces, since `sh` filter on `''` (str of Undefined)
    # wouldn't raise. Use a stricter case: filter chain that depends on
    # parameter being defined.
    with pytest.raises((jinja2.UndefinedError, jinja2.TemplateError)):
        await runner._render_command(manifest, {"unrelated": "x"})


async def test_sh_filter_rejects_none() -> None:
    runner = _runner()
    manifest = _make_manifest(command="ping {{ host | sh }}")
    # `sh` filter raises `jinja2.TemplateRuntimeError` (subclass of
    # `jinja2.TemplateError`) so that the runner's narrow `except` block at
    # `_render_command` catches it and surfaces `status="exception"`.
    with pytest.raises(jinja2.TemplateError, match="None"):
        await runner._render_command(manifest, {"host": None})


async def test_sh_filter_rejects_empty_list() -> None:
    runner = _runner()
    # Empty list passed directly to `| sh` (not via map) triggers the
    # explicit empty-list rejection inside the `sh` filter — silent empty
    # rendering would hide a missing parameter.
    manifest = _make_manifest(command="ping {{ endpoints | sh }}")
    with pytest.raises(jinja2.TemplateError, match="empty list"):
        await runner._render_command(manifest, {"endpoints": []})


async def test_sh_filter_with_array_via_map() -> None:
    runner = _runner()
    manifest = _make_manifest(command="ping {{ endpoints | map('sh') | join(' ') }}")
    cmd, _ = await runner._render_command(manifest, {"endpoints": ["host1", "host2"]})
    assert cmd == f"ping {shlex.quote('host1')} {shlex.quote('host2')}"


async def test_secrets_env_only_for_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manifest declares 1 secret; only that one is in secrets_env."""

    monkeypatch.setenv("DECLARED_SECRET", "in")
    monkeypatch.setenv("UNDECLARED_SECRET", "out")
    runner = _runner()
    manifest = _make_manifest(secrets=["DECLARED_SECRET"])
    _, secrets_env = await runner._render_command(manifest, None)
    assert secrets_env == {"DECLARED_SECRET": "in"}
    assert "UNDECLARED_SECRET" not in secrets_env
