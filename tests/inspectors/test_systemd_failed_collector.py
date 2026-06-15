"""Collector + severity tests for `linux.systemd.failed_units` (D-7 convention).

`ground-diagnostician-failure-analysis` group B: the systemd inspector now carries
a per-unit time anchor (`Type` + `InactiveEnterTimestampMonotonic`) plus the system
`uptime_seconds`, and calibrates severity per-unit instead of one blanket
`critical`. A long-running box's boot-time `oneshot` residue (cloud-init et al.)
is downgraded to `warning`; a fresh-reboot oneshot, a late failure, or a resident
service failure stays `critical`.

Two tiers of test live here, both per the os-shell fixture convention
([[project_d7_os_shell_fixture_convention]]):

  * **Collector JSON regression** — runs the manifest's REAL final `awk` stage
    against an author-controlled `uptime\\nunit\\ttype\\tmono` stream (swapping
    only the upstream `systemctl`/`/proc/uptime` producers for the stream), and
    asserts the awk emits valid JSON: `results`/`type`/`inactive_monotonic_us`,
    BARE numbers (uptime + monotonic are JSON numbers, not strings), and correct
    JSON-string escaping of unit names that legally carry `\\` or `"`. The empty
    failed-set still prints a valid top-level OBJECT.
  * **Severity calibration** — runs the REAL `InspectorRunner` against a
    `_CaptureTarget` that answers the `command -v` binary probes and returns the
    canned final-pipeline JSON for the authored host state, recording the rendered
    collect command for a byte-for-byte command-string lock (the collector shell
    is not offline-validated; its correctness is pinned by the locked command +
    the real-host Demo Path, not by these fixtures).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import structlog
import yaml

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "../src/hostlens/inspectors/builtin/linux/systemd_failed_units.yaml"
).resolve()

_PROBE_PREFIX = "command -v "

# The exact collect command the renderer produces (no Jinja2 substitution — the
# systemd collect block is a static shell snippet). Locking it byte-for-byte means
# any drift in the collector pipeline (e.g. a swap to gawk `systime()`, or losing
# the `while IFS= read -r unit` per-unit loop) fails loudly rather than silently
# changing what we feed the parser. This is a HARD literal (not re-read from the
# manifest) so a hand-edit to the collect pipeline genuinely flips this test red;
# Jinja2's `from_string` strips the single trailing newline the YAML `|` block
# scalar carries, so the literal carries none either. The trailing `'` is
# concatenated because a raw triple-quoted string cannot end on a quote char.
_EXPECTED_COLLECT = (
    r"""read -r uptime_seconds _ < /proc/uptime || { echo "read /proc/uptime failed" >&2; exit 1; }
{
  printf '%s\n' "$uptime_seconds"
  systemctl list-units --type=service --state=failed --no-legend --plain 2>/dev/null \
    | awk '{ if ($1 != "") print $1 }' \
    | while IFS= read -r unit; do
        type=$(systemctl show "$unit" -p Type --value 2>/dev/null)
        mono=$(systemctl show "$unit" -p InactiveEnterTimestampMonotonic --value 2>/dev/null)
        case "$mono" in (*[!0-9]*|"") mono=0 ;; esac
        printf '%s\t%s\t%s\n' "$unit" "$type" "$mono"
      done
} | awk -F '\t' '
    BEGIN { n = 0 }
    NR == 1 { uptime = $0; next }
    {
      unit = $1
      gsub(/\\/, "\\\\", unit)
      gsub(/"/, "\\\"", unit)
      units[n] = unit
      types[n] = $2
      monos[n] = ($3 == "" ? 0 : $3)
      n++
    }
    END {
      printf "{\"uptime_seconds\":%s,\"results\":[", (uptime == "" ? 0 : uptime)
      for (i = 0; i < n; i++) {
        printf "%s{\"unit\":\"%s\",\"type\":\"%s\",\"inactive_monotonic_us\":%d}", \
          (i ? "," : ""), units[i], types[i], monos[i]
      }
      printf "]}"
    }"""
    + "'"
)


# --------------------------------------------------------------------------- #
# Tier 1: collector JSON regression — run the manifest's REAL final awk stage.
# --------------------------------------------------------------------------- #


def _final_awk_stage() -> str:
    """The manifest collect command's terminal ``| awk -F '\\t' '...'`` stage,
    isolated so a test can feed its own ``uptime\\nrecords`` stream in via stdin."""
    command = yaml.safe_load(_MANIFEST.read_text())["collect"]["command"]
    idx = command.rfind("| awk -F")
    assert idx != -1, "collect command shape changed: expected a trailing `| awk -F` stage"
    return "awk -F" + command[idx + len("| awk -F") :]


def _run_awk(stream: str) -> dict[str, object]:
    """Feed the author-controlled ``uptime\\nunit\\ttype\\tmono`` stream into the
    manifest's final awk stage — the same stage the shell loop feeds in prod."""
    out = subprocess.run(
        ["bash", "-c", _final_awk_stage()],
        input=stream,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    parsed = json.loads(out)  # raises if the awk emitted invalid JSON
    assert isinstance(parsed, dict)
    return parsed


def test_collector_emits_results_with_bare_numbers() -> None:
    parsed = _run_awk("3110400\nnginx.service\tnotify\t500000000\nmysql.service\tforking\t0\n")
    # `uptime_seconds` is a JSON NUMBER (not "3110400") and `inactive_monotonic_us`
    # is a JSON INT — quoting either would fail jsonschema number/int validation.
    assert parsed["uptime_seconds"] == 3110400
    assert isinstance(parsed["uptime_seconds"], int)
    assert parsed["results"] == [
        {"unit": "nginx.service", "type": "notify", "inactive_monotonic_us": 500000000},
        {"unit": "mysql.service", "type": "forking", "inactive_monotonic_us": 0},
    ]
    assert all(isinstance(r["inactive_monotonic_us"], int) for r in parsed["results"])


def test_collector_uptime_is_a_float_when_proc_uptime_has_fraction() -> None:
    # `/proc/uptime`'s first field carries a fractional part (e.g. `3110400.55`);
    # it must round-trip as a JSON number, not a string.
    parsed = _run_awk("3110400.55\n")
    assert parsed["uptime_seconds"] == 3110400.55
    assert isinstance(parsed["uptime_seconds"], float)
    assert parsed["results"] == []


def test_collector_escapes_backslash_in_unit_name() -> None:
    # A C-escaped instance/path unit name carries a literal backslash; raw
    # emission would be `"foo\\x2dbar.service"` — an invalid JSON escape.
    parsed = _run_awk("3110400\nfoo\\x2dbar.service\toneshot\t120000000\n")
    assert parsed["results"] == [
        {"unit": "foo\\x2dbar.service", "type": "oneshot", "inactive_monotonic_us": 120000000},
    ]


def test_collector_escapes_double_quote_in_unit_name() -> None:
    parsed = _run_awk('3110400\nweird"name.service\tsimple\t0\n')
    assert parsed["results"] == [
        {"unit": 'weird"name.service', "type": "simple", "inactive_monotonic_us": 0},
    ]


def test_collector_empty_case_is_valid_object() -> None:
    # No failed units → only the uptime line reaches the final awk. It must still
    # emit a valid top-level OBJECT (parse_json rejects a top-level array).
    parsed = _run_awk("3110400\n")
    assert parsed == {"uptime_seconds": 3110400, "results": []}


# --------------------------------------------------------------------------- #
# Tier 2: severity calibration — run the REAL InspectorRunner via _CaptureTarget.
# --------------------------------------------------------------------------- #


class _CaptureTarget:
    """Offline target: answers `command -v <bin>` probes, returns canned JSON
    stdout for the main collect command, and records every rendered command.

    `command -v <bin>` probes succeed with a synthetic path (so the
    `[systemctl, awk]` requires_binaries preflight passes); everything else is the
    inspector's main pipeline and returns `main_stdout` — the final JSON the
    pipeline would print on a host in the authored failed-unit state.
    """

    type = "local"

    def __init__(self, name: str, *, main_stdout: str) -> None:
        self.name = name
        self.capabilities: set[Capability] = {Capability.SHELL}
        self._main_stdout = main_stdout
        self.commands: list[str] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        self.commands.append(cmd)
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        return ExecResult(
            exit_code=0,
            stdout=self._main_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


def _logger() -> Any:
    return structlog.get_logger("test-systemd-failed")


def _pipeline_json(*, uptime_seconds: float, results: list[dict[str, object]]) -> str:
    """The final JSON the collect pipeline prints for the authored host state."""
    return json.dumps({"uptime_seconds": uptime_seconds, "results": results})


async def _run(main_stdout: str) -> tuple[_CaptureTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST)
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())
    target = _CaptureTarget("systemd-host", main_stdout=main_stdout)
    result = await runner.run(manifest, target)
    return target, result


def _assert_collect_command_locked(target: _CaptureTarget) -> None:
    """The rendered main collect command must match the manifest byte-for-byte
    (command-string lock — probes are filtered out)."""
    main_cmds = [c for c in target.commands if not c.startswith(_PROBE_PREFIX)]
    assert main_cmds == [_EXPECTED_COLLECT], main_cmds


# 病 1 anchor: a long-running box's boot-time oneshot residue → warning ----------


async def test_longrunning_boot_window_oneshot_is_warning() -> None:
    # up 36 days (>> min_uptime_seconds=3600), cloud-final.service (oneshot) failed
    # 90s after boot (< boot_window_seconds=180 → 90e6 us <= 180e6) and nothing
    # else: this is provisioning residue, not a fleet-wide outage → exactly one
    # `warning`, so the report's aggregate severity is NOT pulled to `critical`.
    target, result = await _run(
        _pipeline_json(
            uptime_seconds=3110400,
            results=[
                {
                    "unit": "cloud-final.service",
                    "type": "oneshot",
                    "inactive_monotonic_us": 90000000,
                },
            ],
        )
    )

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("warning", "systemd 开机一次性失败（历史残留）：cloud-final.service（Type=oneshot）"),  # noqa: RUF001
    ]


# fresh-reboot anchor: same boot-window oneshot but low uptime → critical --------


async def test_fresh_reboot_boot_window_oneshot_is_critical() -> None:
    # uptime 120s < min_uptime_seconds (3600): on a freshly-rebooted box a
    # boot-window oneshot failure cannot be ruled "historical" — it may be the
    # current fault → stays `critical` (not downgraded to warning).
    target, result = await _run(
        _pipeline_json(
            uptime_seconds=120,
            results=[
                {
                    "unit": "cloud-final.service",
                    "type": "oneshot",
                    "inactive_monotonic_us": 90000000,
                },
            ],
        )
    )

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "systemd 失败服务：cloud-final.service（Type=oneshot）"),  # noqa: RUF001
    ]


# resident service + late oneshot → critical (no false downgrade) ----------------


async def test_resident_service_and_late_oneshot_are_critical() -> None:
    # On a long-running box (uptime >> min):
    #   * zerotier-one.service is Type=notify (resident) → not the oneshot rule →
    #     critical (a failed resident network service is a real problem),
    #   * backup.service is oneshot but failed at 1e9 us == 1000s after boot, well
    #     OUTSIDE the 180s boot window → not historical residue → critical.
    target, result = await _run(
        _pipeline_json(
            uptime_seconds=3110400,
            results=[
                {
                    "unit": "zerotier-one.service",
                    "type": "notify",
                    "inactive_monotonic_us": 50000000000,
                },
                {"unit": "backup.service", "type": "oneshot", "inactive_monotonic_us": 1000000000},
            ],
        )
    )

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "systemd 失败服务：zerotier-one.service（Type=notify）"),  # noqa: RUF001
        ("critical", "systemd 失败服务：backup.service（Type=oneshot）"),  # noqa: RUF001
    ]


# degenerate monotonic 0 → critical (never-inactive guard) ------------------------


async def test_oneshot_with_zero_monotonic_is_critical() -> None:
    # systemd reports `InactiveEnterTimestampMonotonic=0` for a unit that has
    # never gone inactive. The `inactive_monotonic_us > 0` guard excludes it from
    # the downgrade → critical (conservative: never a false negative on missing
    # anchor), even on a long-running box.
    target, result = await _run(
        _pipeline_json(
            uptime_seconds=3110400,
            results=[
                {"unit": "weird.service", "type": "oneshot", "inactive_monotonic_us": 0},
            ],
        )
    )

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        ("critical", "systemd 失败服务：weird.service（Type=oneshot）"),  # noqa: RUF001
    ]


# no failed units → zero findings -------------------------------------------------


async def test_no_failed_units_yields_no_finding() -> None:
    target, result = await _run(_pipeline_json(uptime_seconds=3110400, results=[]))

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert result.output == {"uptime_seconds": 3110400, "results": []}
    assert result.findings == []
