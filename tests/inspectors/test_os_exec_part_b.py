"""Collector-execution tests (part B) — STRONG attestation for 10 wave-1 inspectors.

Companion to `test_os_collector_execution.py`: same `_Case` + parametrized-runner
structure, same `_shim_exec` harness. Each case drives the **real**
`InspectorRunner` against a `ShimShellTarget` that runs the rendered
`collect.command` through a real `/bin/sh` with only the *data-source* commands
shimmed (serving author-controlled RAW input) while the *text tools* (awk / jq /
sort / wc / grep / …) stay REAL. The collector's awk/jq derivation therefore
executes against known raw input and we assert the independently-reasoned
expected output + findings — a wrong awk field index, jq path, decode, or JSON
escape produces a mismatch the snapshot suite (canned final-JSON stdout) cannot
see.

Determinism notes:
  * journalctl/systemctl/ps/pgrep cases use the harness *name-only* fallback key
    (e.g. `"journalctl"`) so the clock-varying `--since/--until` args are ignored.
  * `linux.cron.last_runs` and `linux.systemd.timer_status` embed `date +%s` in
    the collector to derive an age scalar; that makes the derived age
    clock-dependent. `cron.last_runs` SHIMS `date` to a fixed epoch so the age is
    reproducible. `timer_status` instead uses a never-fired timer
    (`last_trigger_usec == 0`), whose jq branch yields a constant `-1` age, so
    the real `date +%s` does not leak into the asserted output.
  * `linux.system.reboot_required` probes the flag with the `[ -e ]` shell
    builtin (NOT a shimmable command), so the harness cannot inject "flag
    present". On the test host `/var/run/reboot-required` is absent, so only the
    "absent → reboot_required=false, no finding" branch is exercised here. See
    the case comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry

from ._shim_exec import ShimShellTarget, build_shim_env

_BUILTIN = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"


@dataclass(frozen=True)
class _Case:
    inspector: str  # manifest path relative to builtin/, e.g. "linux/process_zombies.yaml"
    commands: list[str]  # data-source command names to shim (text tools stay real)
    responses: dict[str, tuple[str, int]]  # "<cmd> <args>" -> (stdout, exit_code)
    expected_output: dict[str, Any]
    expected_findings: list[tuple[str, str]]  # (severity, message), [] = none expected
    params: dict[str, Any] = field(default_factory=dict)
    case_id: str = ""


_CASES: tuple[_Case, ...] = (
    # ---- linux.process.zombies: two Z-state procs, comm with backslash+quote -- #
    # `ps axo stat=,pid=,comm=` (name-only key). awk filters STAT `^Z`, counts,
    # and JSON-escapes the comm ($3). comm `ba\d"proc` carries BOTH a backslash
    # and a quote so the gsub escape pair is fully executed — and its
    # backslash-FIRST ordering is load-bearing: swap the two gsubs and this input
    # emits invalid JSON (parse error → status!=ok → test fails). This also
    # anchors-by-equivalence the byte-identical escape in
    # system_reboot_required's present branch.
    _Case(
        inspector="linux/process_zombies.yaml",
        case_id="process_zombies_two",
        commands=["ps"],
        responses={"ps": ('Z 1234 defunct1\nS 1 init\nZ 5678 ba\\d"proc\n', 0)},
        expected_output={
            "zombie_count": 2,
            "results": [
                {"pid": 1234, "comm": "defunct1"},
                {"pid": 5678, "comm": 'ba\\d"proc'},
            ],
        },
        expected_findings=[
            ("warning", "2 zombie process(es) present (defunct, not reaped)"),
            ("info", "zombie process pid=1234 (defunct1)"),
            ("info", 'zombie process pid=5678 (ba\\d"proc)'),
        ],
    ),
    # ---- linux.process.total: 95 procs / pid_max 100 = 95.0% -> critical ----- #
    # `ps -e --no-headers | wc -l` (name-only `ps`, real wc) + exact-key
    # `cat /proc/sys/kernel/pid_max`. awk derives used_pct = total/pid_max*100.
    _Case(
        inspector="linux/process_total.yaml",
        case_id="process_total_95pct",
        commands=["ps", "cat"],
        responses={
            "ps": ("x\n" * 95, 0),
            "cat /proc/sys/kernel/pid_max": ("100\n", 0),
        },
        expected_output={"total": 95, "pid_max": 100, "used_pct": "95.0"},
        expected_findings=[
            ("critical", "Process table near pid_max: 95/100 (95.0% used)"),
        ],
    ),
    # ---- linux.process.critical_alive: pgrep rc=1 (absent) -> critical ------- #
    # The per-name loop runs `pgrep -x -- "$proc"`; rc=1 means "no match"
    # (process absent — the very signal). The name-only `pgrep` shim returns
    # ("", 1) so the single requested name resolves to alive=false.
    _Case(
        inspector="linux/process_critical_alive.yaml",
        case_id="process_critical_alive_absent",
        commands=["pgrep"],
        responses={"pgrep": ("", 1)},
        params={"names": ["nginx"]},
        expected_output={"results": [{"name": "nginx", "alive": False}]},
        expected_findings=[
            ("critical", "Critical process nginx is not alive"),
        ],
    ),
    # ---- linux.systemd.timer_status: never-fired timer -> warning ----------- #
    # `systemctl list-timers --all -o json` (name-only). A timer with
    # next_elapse_realtime==0 and last_trigger_usec==0 yields a constant
    # last_trigger_age_sec==-1 (jq else-branch), keeping output clock-independent
    # despite the collector's `date +%s`.
    _Case(
        inspector="linux/systemd_timer_status.yaml",
        case_id="systemd_timer_never_fired",
        commands=["systemctl"],
        responses={
            "systemctl": (
                '[{"unit":"stuck.timer","next_elapse_realtime":0,"last_trigger_usec":0}]',
                0,
            )
        },
        expected_output={
            "results": [
                {
                    "unit": "stuck.timer",
                    "next_elapse_usec": 0,
                    "last_trigger_usec": 0,
                    "last_trigger_age_sec": -1,
                }
            ]
        },
        expected_findings=[
            (
                "warning",
                "systemd timer stuck.timer has never fired and has no next elapse scheduled",
            ),
        ],
    ),
    # ---- linux.systemd.masked: two masked unit files -> warning ------------- #
    # `systemctl list-unit-files --state=masked ...` (name-only). awk extracts
    # $1 (unit name) per line into the {"masked":[...]} object.
    _Case(
        inspector="linux/systemd_masked.yaml",
        case_id="systemd_masked_two",
        commands=["systemctl"],
        responses={"systemctl": ("foo.service masked\nbar.timer masked\n", 0)},
        expected_output={
            "masked": [{"unit": "foo.service"}, {"unit": "bar.timer"}],
        },
        expected_findings=[
            ("warning", "One or more systemd unit files are masked (see masked for details)"),
        ],
    ),
    # ---- linux.cron.last_runs: stale cron job (age 10000s) -> warning -------- #
    # journalctl `-o json` (name-only). `date` is SHIMMED to a fixed epoch
    # (1700000000) so the awk-derived age is reproducible: newest CMD timestamp
    # 1699990000000000us -> 1699990000s -> age = 1700000000 - 1699990000 = 10000.
    # The older duplicate line (same command) is superseded by last-wins; the
    # non-CMD session line is filtered by the jq `select(... CMD ...)`.
    _Case(
        inspector="linux/cron_last_runs.yaml",
        case_id="cron_last_runs_stale",
        commands=["journalctl", "date"],
        responses={
            "journalctl": (
                '{"__REALTIME_TIMESTAMP":"1699900000000000","MESSAGE":"(root) CMD (/bin/backup.sh)"}\n'
                '{"__REALTIME_TIMESTAMP":"1699990000000000","MESSAGE":"(root) CMD (/bin/backup.sh)"}\n'
                '{"__REALTIME_TIMESTAMP":"1699999000000000","MESSAGE":"pam_unix(cron:session): session opened"}\n',
                0,
            ),
            "date": ("1700000000\n", 0),
        },
        expected_output={
            "results": [{"command": "/bin/backup.sh", "last_run_age_sec": 10000}],
        },
        expected_findings=[
            (
                "warning",
                "cron job /bin/backup.sh last ran 10000s ago (exceeds staleness threshold)",
            ),
        ],
    ),
    # ---- linux.cron.failures: 2 failure markers in window -> warning -------- #
    # journalctl (name-only). real grep -iE matches `exit status [1-9]` and
    # `(CRON) error`; the benign CMD line is not matched. wc -l = 2; parse.kv
    # strips the BSD wc padding so failure_count == "2". window_seconds is the
    # manifest sampling_window duration (3600).
    _Case(
        inspector="linux/cron_failures.yaml",
        case_id="cron_failures_two",
        commands=["journalctl"],
        responses={
            "journalctl": (
                "Jun 05 01:00:00 host CRON[123]: (root) CMD (/bin/backup) exit status 1\n"
                "Jun 05 02:00:00 host CRON[124]: (CRON) error (grandchild process failed)\n"
                "Jun 05 03:00:00 host CRON[125]: (root) CMD (/bin/ok)\n",
                0,
            )
        },
        expected_output={"failure_count": "2", "window_seconds": "3600"},
        expected_findings=[
            ("warning", "2 cron failure log entries in the last 3600s"),
        ],
    ),
    # ---- linux.system.reboot_required: flag absent -> no finding ------------ #
    # DESIGN NOTE: the collector probes the flag with `[ -e "$flag" ]`, a shell
    # builtin the data-source shim CANNOT intercept, so "flag present" cannot be
    # injected in an execution test. `/var/run/reboot-required` is absent on the
    # test host, so this exercises the "absent -> reboot_required=false, no
    # finding" branch end-to-end (the `cat` reads in the present-branch are never
    # reached). See module docstring + tracked as a design-issue in the report.
    _Case(
        inspector="linux/system_reboot_required.yaml",
        case_id="reboot_required_absent",
        commands=[],
        responses={},
        expected_output={"reboot_required": False, "reason": "", "pkgs": ""},
        expected_findings=[],
    ),
    # ---- linux.kernel.messages: 5 error lines in window -> warning ---------- #
    # journalctl -k -p err (name-only). real wc -l = 5; parse.kv strips padding
    # so error_count == "5" (>=5 and <20 -> elevated/warning). window_seconds is
    # the manifest sampling_window duration (300).
    _Case(
        inspector="linux/kernel_messages.yaml",
        case_id="kernel_messages_five",
        commands=["journalctl"],
        responses={
            "journalctl": (
                "kernel: I/O error, dev sda, sector 1\n"
                "kernel: I/O error, dev sda, sector 2\n"
                "kernel: EXT4-fs error on dm-0\n"
                "kernel: mce: hardware error reported\n"
                "kernel: nvme nvme0: controller reset\n",
                0,
            )
        },
        expected_output={"error_count": "5", "window_seconds": "300"},
        expected_findings=[
            ("warning", "Elevated kernel error rate: 5 messages in the last 300s"),
        ],
    ),
    # ---- log.exception_burst: ValueError x12 over threshold -> warning ------ #
    # `cat <log_path>` (exact key). real awk tallies per exception-class token,
    # sort orders deterministically (KeyError < ValueError in both byte and
    # locale collation), the second awk wraps to {"results":[...]}. ValueError
    # count 12 >= burst_threshold(10) fires; KeyError count 1 does not.
    _Case(
        inspector="log/exception_burst.yaml",
        case_id="exception_burst_valueerror",
        commands=["cat"],
        responses={
            "cat /var/log/app.log": (
                ("ERROR request failed ValueError raised in handler\n" * 12)
                + "ERROR lookup missed KeyError happened\n",
                0,
            )
        },
        params={"log_path": "/var/log/app.log"},
        expected_output={
            "results": [
                {"signature": "KeyError", "count": 1},
                {"signature": "ValueError", "count": 12},
            ]
        },
        expected_findings=[
            ("warning", "Exception burst: ValueError occurred 12 times"),
        ],
    ),
)


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("collector-exec-test-part-b")  # type: ignore[no-any-return]


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
async def test_collector_executes_against_raw_input(case: _Case, tmp_path: Path) -> None:
    bin_dir, data_dir = build_shim_env(tmp_path, commands=case.commands, responses=case.responses)
    manifest = load_manifest(_BUILTIN / case.inspector)
    target = ShimShellTarget(case.case_id.replace("_", "-"), bin_dir=bin_dir, data_dir=data_dir)
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())

    result = await runner.run(manifest, target, case.params)

    # The real awk/jq derivation ran against the raw fixture — assert it derived
    # the expected output and findings (not a canned bypass).
    assert result.status == "ok", f"{case.case_id}: status={result.status} error={result.error}"
    assert result.output == case.expected_output
    assert [(f.severity, f.message) for f in result.findings] == case.expected_findings
