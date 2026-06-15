"""Command-string lock for the `linux.systemd.failed_units` collector awk.

Per the os-shell fixture convention ([[project_d7_os_shell_fixture_convention]])
the collector shell is not offline-unit-tested through `_CaptureTarget` (which
returns canned final stdout). This test instead runs the **real manifest awk**
against author-controlled `systemctl --plain` output, swapping only the leading
`systemctl ...` producer for a `printf`, and asserts the awk emits valid JSON.

The regression it locks: systemd unit names may legally carry backslashes
(C-escaped `\\x2d` in path/instance-derived names) or — adversarially — a double
quote; emitting them raw into the JSON string literal would crash `parse_json`.
The flagship is the allowlist's sole member and the pattern later domain PRs
copy, so its JSON-string escaping must be correct here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "../src/hostlens/inspectors/builtin/linux/systemd_failed_units.yaml"
).resolve()


def _awk_pipeline() -> str:
    """The manifest's collect command with the `systemctl` producer removed,
    leaving `| awk '...'` so a test can pipe its own `--plain` sample in."""
    command = yaml.safe_load(_MANIFEST.read_text())["collect"]["command"]
    _, sep, awk = command.partition("| awk")
    assert sep, "collect command shape changed: expected a `| awk` stage"
    return "awk" + awk


def _run(sample: str) -> dict[str, object]:
    # Feed the `--plain` sample via stdin (preserving real newlines) into the
    # manifest's `awk` stage — the same stage `systemctl ... |` feeds in prod.
    out = subprocess.run(
        ["bash", "-c", _awk_pipeline()],
        input=sample,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    parsed = json.loads(out)  # raises if the awk emitted invalid JSON
    assert isinstance(parsed, dict)
    return parsed


def test_collector_emits_valid_json_for_plain_names() -> None:
    parsed = _run("nginx.service loaded active failed\nmysql.service loaded active failed\n")
    assert [u["unit"] for u in parsed["failed"]] == ["nginx.service", "mysql.service"]  # type: ignore[index,union-attr]
    assert parsed["failed_names"] == "nginx.service, mysql.service"


def test_collector_escapes_backslash_in_unit_name() -> None:
    # A C-escaped instance/path unit name carries a literal backslash; raw
    # emission would be `"foo\x2dbar.service"` — an invalid JSON escape.
    parsed = _run("foo\\x2dbar.service loaded active failed\n")
    assert parsed["failed"] == [{"unit": "foo\\x2dbar.service"}]
    assert parsed["failed_names"] == "foo\\x2dbar.service"


def test_collector_escapes_double_quote_in_unit_name() -> None:
    parsed = _run('weird"name.service loaded active failed\n')
    assert parsed["failed"] == [{"unit": 'weird"name.service'}]
    assert parsed["failed_names"] == 'weird"name.service'


def test_collector_empty_case_is_valid_object() -> None:
    parsed = _run("")
    assert parsed == {"failed": [], "failed_names": ""}
