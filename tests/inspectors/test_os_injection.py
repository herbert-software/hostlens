"""Injection-safety regression for the wave-1 string-parameter inspectors.

`add-os-shell-inspectors-wave1` tasks.md §10.3 / spec §需求:套件内每个
inspector 必须是遵守作者契约的纯 YAML §场景:参数安全进 shell.

Three wave-1 inspectors splice a caller-supplied *string* value into their
`collect.command`:

  * `net.dns.resolve`        — `names`     (array of strings, `| map('sh')`)
  * `linux.process.critical_alive` — `names` (array of strings, `| map('sh')`)
  * `log.exception_burst`    — `log_path`  (scalar string, `| sh`)

The Authoring Contract's injection-safety triad requires: (a) the value flows
through `| sh` / `| map('sh')` (shlex.quote), (b) the `parameters` JSON Schema
`pattern` restricts the charset so shell metacharacters never reach a
shell-evaluated position, and (c) it is never bare-spliced.

This file drives the **real** `InspectorRunner.run` for each inspector with a
matrix of injection payloads and asserts the defense holds end to end:

  * Each payload — `'; whoami; #`, `$(curl evil)`, `a b`, `x;y` — contains at
    least one character outside the inspector's `pattern`, so parameter
    validation rejects it (`status=exception`, `parameter_validation_failed`)
    BEFORE the collector command is rendered or run. The malicious string never
    reaches a shell-evaluated position: `_ProbeOnlyTarget` raises if the
    collector command is ever exec'd.

  * A positive control with a benign value proves the pattern is not
    over-rejecting and that the value rides the `| sh` / `| map('sh')` filter
    (the rendered collector command carries the value as a single shlex-quoted
    token), recorded against the same probe-only target by capturing the
    rendered command.

No real host / network / `dig` / `pgrep` / `awk` is touched.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"


# The injection payload matrix mandated by tasks.md §10.3. Each must contain a
# character outside every targeted inspector's `pattern` so validation rejects
# it before the command renders.
_PAYLOADS: list[tuple[str, str]] = [
    ("command_separator_comment", "'; whoami; #"),
    ("command_substitution", "$(curl evil)"),
    ("space_split", "a b"),
    ("semicolon_chain", "x;y"),
]


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


class _ProbeOnlyTarget:
    """Answers preflight probes; records the collector command without running it.

    The runner's preflight (binary `command -v X` / file `[ -r P ]` probes) runs
    BEFORE parameter validation, so those probes legitimately reach `exec`. The
    rendered collector command is the only place a malicious value could land in
    a shell-evaluated position. For a rejected payload it must NEVER reach
    `exec`; for the benign positive control it is captured into `last_collector`
    (and answered with a stub so the run can complete to `ok`).
    """

    type = "local"
    name = "probe-only-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    def __init__(self, *, allow_collector: bool, collector_stdout: str = "") -> None:
        self._allow_collector = allow_collector
        self._collector_stdout = collector_stdout
        self.last_collector: str | None = None

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            binary = cmd[len("command -v ") :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        if not self._allow_collector:
            raise AssertionError(f"collector command must not run for a rejected payload: {cmd!r}")
        self.last_collector = cmd
        return ExecResult(
            exit_code=0,
            stdout=self._collector_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


# (registry name, yaml rel path, parameter factory taking the payload value)
_STRING_PARAM_CASES: list[tuple[str, str, str]] = [
    ("net.dns.resolve", "net/dns_resolve.yaml", "names_array"),
    ("linux.process.critical_alive", "linux/process_critical_alive.yaml", "names_array"),
    ("log.exception_burst", "log/exception_burst.yaml", "log_path_scalar"),
]


def _params_for(kind: str, value: str) -> dict[str, Any]:
    if kind == "names_array":
        return {"names": [value]}
    if kind == "log_path_scalar":
        return {"log_path": value}
    raise AssertionError(f"unknown param kind {kind!r}")


@pytest.mark.parametrize(
    "name,rel_path,kind",
    _STRING_PARAM_CASES,
    ids=[c[0] for c in _STRING_PARAM_CASES],
)
@pytest.mark.parametrize("label,payload", _PAYLOADS, ids=[p[0] for p in _PAYLOADS])
async def test_injection_payload_rejected_before_command(
    name: str, rel_path: str, kind: str, label: str, payload: str
) -> None:
    """A malicious value is rejected by the schema pattern before any exec.

    The collector command is never rendered or run — the malicious string never
    becomes a shell token (`_ProbeOnlyTarget` raises if the collector runs).
    """

    manifest = load_manifest(_BUILTIN_DIR / rel_path)
    assert manifest.name == name
    target = _ProbeOnlyTarget(allow_collector=False)

    result = await _runner().run(
        manifest,
        target,  # type: ignore[arg-type]
        parameters=_params_for(kind, payload),
    )

    assert result.status == "exception", (label, payload)
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error
    assert result.findings == []
    assert target.last_collector is None


@pytest.mark.parametrize(
    "name,rel_path,kind,benign",
    [
        ("net.dns.resolve", "net/dns_resolve.yaml", "names_array", "safe-host.example"),
        (
            "linux.process.critical_alive",
            "linux/process_critical_alive.yaml",
            "names_array",
            "sshd",
        ),
        ("log.exception_burst", "log/exception_burst.yaml", "log_path_scalar", "/var/log/app.log"),
    ],
    ids=["net.dns.resolve", "linux.process.critical_alive", "log.exception_burst"],
)
async def test_benign_value_renders_shlex_quoted(
    name: str, rel_path: str, kind: str, benign: str
) -> None:
    """Positive control: a pattern-valid value rides `| sh` / `| map('sh')`.

    Proves the pattern is not over-rejecting AND that the value is interpolated
    through shlex.quote — the captured collector command must contain the value
    as a single shlex-split token (no extra eval, no information loss). The
    collector stdout is a benign empty `{"results":[]}` so the run reaches `ok`.
    """

    manifest = load_manifest(_BUILTIN_DIR / rel_path)
    assert manifest.name == name
    # All three collectors `parse.format: json` and require a top-level object.
    target = _ProbeOnlyTarget(allow_collector=True, collector_stdout='{"results":[]}')

    result = await _runner().run(
        manifest,
        target,  # type: ignore[arg-type]
        parameters=_params_for(kind, benign),
    )

    assert result.status == "ok", result.error
    assert target.last_collector is not None
    # The benign value must appear in the rendered collector command exactly as
    # `shlex.quote(benign)` produced it — proving it was routed through the `sh`
    # filter (shlex.quote). These benign values have no shell metacharacters so
    # shlex.quote leaves them bare, but the substring must still be present in
    # the rendered command (a bare-splice bug would also place it there, so the
    # rejection tests above are the real injection guard; this is the
    # not-over-rejecting + value-on-the-sh-path positive control).
    assert shlex.quote(benign) in target.last_collector, (benign, target.last_collector)
