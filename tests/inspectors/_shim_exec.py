"""Shim-shell execution harness — a STRONG attestation anchor for collectors.

The snapshot tests (`test_os_*.py`) replay `ReplayTarget` fixtures whose stdout
is the collector's *final* JSON: they exercise `parse → findings` but **never
execute** the collector's embedded shell / awk / jq. That left collector-logic
correctness (awk field indices, jq paths, decode tables, JSON escaping,
fail-loud guards) with only weak (read-the-code) attestation — exactly the class
of bug that slipped past the snapshot suite during review.

This harness closes that gap: it drives the **real** ``InspectorRunner`` against
a ``ShimShellTarget`` that runs each rendered ``collect.command`` through a real
``/bin/sh`` — with the *data-source* commands (``cat`` for /proc//sys, ``ss``,
``journalctl``, ``smartctl``, ``chronyc``, ``systemctl``, ``dig``, ``findmnt``,
``ls``, ``pgrep``, ``ps``, ``date`` …) shimmed to serve **raw** fixtures, while
the *text-processing* tools (``awk``, ``jq``, ``sort``, ``head``, ``cut``,
``grep``, ``wc``, ``tr``, ``sed``) stay REAL. The awk/jq derivation therefore
executes against author-controlled raw input, and the test asserts the
independently-reasoned expected output + findings. A wrong awk field index or a
mis-ordered decode table produces a mismatch — the snapshot suite could not.

Mechanics:

  * One shim script (``_shim_cmd.sh``) is copied into ``bin/`` under each
    shimmed command name. PATH is ``bin/`` first, then the real PATH, so only
    the shimmed names are intercepted; everything else resolves to the real
    binary.
  * The shim keys on ``"<basename> <args…>"``, sanitises it to a filename, and
    emits ``data/<safe>.out`` with exit code ``data/<safe>.rc`` (default 0). A
    missing fixture is LOUD: stderr + exit 127, so an unmodelled command surfaces
    rather than silently returning empty (which fail-loud collectors would treat
    as a dead data source).
  * The shim reads its fixture via ``$SHIM_REALCAT`` (an absolute path to the
    real ``cat``) because ``cat`` itself is shimmed — using a bare ``cat`` inside
    the shim would recurse into the shim.

Pure POSIX sh + tr; runs on macOS and Linux with no /proc, no real tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
import time
from pathlib import Path

from hostlens.targets.base import Capability, ExecResult

__all__ = ["ShimShellTarget", "build_shim_env", "shim_key"]

_SHIM_SCRIPT = """#!/bin/sh
# Generic data-source shim — serves a canned raw fixture keyed by argv.
base="$(basename "$0")"
key="$base"
for a in "$@"; do key="$key $a"; done
serve() {
  "${SHIM_REALCAT:-/bin/cat}" "$1.out"
  if [ -f "$1.rc" ]; then exit "$("${SHIM_REALCAT:-/bin/cat}" "$1.rc")"; fi
  exit 0
}
# Exact "<cmd> <args>" match first (keeps `cat <path>` precise across paths).
safe=$(printf '%s' "$key" | tr -c 'A-Za-z0-9' '_')
# Sequential responses: $safe.<n>.out picked by per-key call count, for a
# collector that invokes the SAME command twice (e.g. disk_io reads
# /proc/diskstats before and after a sleep). Past the last defined index the
# highest one is reused.
if [ -f "$SHIM_DATA/$safe.1.out" ]; then
  cnt="$SHIM_DATA/$safe.count"
  n=0; [ -f "$cnt" ] && n=$("${SHIM_REALCAT:-/bin/cat}" "$cnt")
  n=$((n+1)); printf '%s' "$n" > "$cnt"
  [ -f "$SHIM_DATA/$safe.$n.out" ] && serve "$SHIM_DATA/$safe.$n"
  i="$n"; while [ "$i" -ge 1 ]; do
    [ -f "$SHIM_DATA/$safe.$i.out" ] && serve "$SHIM_DATA/$safe.$i"; i=$((i-1))
  done
fi
[ -f "$SHIM_DATA/$safe.out" ] && serve "$SHIM_DATA/$safe"
# Name-only fallback (for single-call commands with varying args, e.g.
# `journalctl --since <clock>`): register the response under just "<cmd>".
bsafe=$(printf '%s' "$base" | tr -c 'A-Za-z0-9' '_')
[ -f "$SHIM_DATA/$bsafe.out" ] && serve "$SHIM_DATA/$bsafe"
printf 'shim: no fixture for [%s]\\n' "$key" >&2
exit 127
"""


def shim_key(invocation: str) -> str:
    """Sanitise a ``"<cmd> <args>"`` invocation to the fixture filename stem.

    Must match the shell shim's ``tr -c 'A-Za-z0-9' '_'`` exactly so the Python
    side and the sh side agree on the file name.
    """

    return re.sub(r"[^A-Za-z0-9]", "_", invocation)


def build_shim_env(
    tmp_path: Path,
    *,
    commands: list[str],
    responses: dict[str, tuple[str, int] | list[tuple[str, int]]],
) -> tuple[Path, Path]:
    """Materialise the shim ``bin/`` + ``data/`` dirs for one scenario.

    ``commands`` is the set of data-source command names to intercept (e.g.
    ``["cat", "ss"]``). ``responses`` maps an exact ``"<cmd> <args>"`` invocation
    (or a bare ``"<cmd>"`` for the name-only fallback) to either a single
    ``(stdout, exit_code)`` or a **list** of them for a command the collector
    invokes more than once (served in order, e.g. disk_io's two diskstats
    reads). Returns ``(bin_dir, data_dir)``.
    """

    bin_dir = tmp_path / "bin"
    data_dir = tmp_path / "data"
    bin_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    script_path = bin_dir / "_shim_cmd.sh"
    script_path.write_text(_SHIM_SCRIPT, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    for cmd in commands:
        dest = bin_dir / cmd
        shutil.copy2(script_path, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _write(stem: str, stdout: str, rc: int) -> None:
        (data_dir / f"{stem}.out").write_text(stdout, encoding="utf-8")
        if rc != 0:
            (data_dir / f"{stem}.rc").write_text(str(rc), encoding="utf-8")

    for invocation, resp in responses.items():
        safe = shim_key(invocation)
        if isinstance(resp, list):
            for idx, (stdout, rc) in enumerate(resp, start=1):
                _write(f"{safe}.{idx}", stdout, rc)
        else:
            _write(safe, resp[0], resp[1])

    return bin_dir, data_dir


class ShimShellTarget:
    """``ExecutionTarget`` that runs commands through real sh with a shimmed PATH.

    Data-source commands resolve to the shim (canned raw fixtures); text tools
    (awk/jq/sort/…) resolve to the real binary, so the collector's derivation
    logic genuinely executes. Used only by the collector-execution strong-anchor
    test — never wired into production.
    """

    type = "local"

    # Real text tools a wave-1 collector (or the shim itself) may resolve when
    # PATH is isolated via ``omit_real`` — symlinked into a curated dir minus the
    # omitted names. Used only to force the no-jq branch of disk_smart (``command
    # -v jq`` must fail), so a host with jq still exercises the awk fallback.
    _ISOLATE_ALLOWLIST = (
        "sh",
        "awk",
        "jq",
        "sort",
        "head",
        "cut",
        "grep",
        "wc",
        "tr",
        "sed",
        "basename",
        "cat",
        "expr",
        "sleep",
        "env",
        "date",
        "pgrep",
        "ps",
        "dirname",
    )

    def __init__(
        self,
        name: str,
        *,
        bin_dir: Path,
        data_dir: Path,
        omit_real: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self._bin_dir = bin_dir
        self._data_dir = data_dir
        self._omit_real = omit_real
        # All wave-1 collectors declare `requires_capabilities: [shell]`.
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}
        self._curated: Path | None = None
        if omit_real:
            curated = data_dir.parent / "realtools"
            curated.mkdir(exist_ok=True)
            for tool in self._ISOLATE_ALLOWLIST:
                if tool in omit_real:
                    continue
                real = shutil.which(tool)
                link = curated / tool
                if real and not link.exists():
                    link.symlink_to(real)
            self._curated = curated

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        # Shim env is set LAST so a caller-supplied env (secrets) cannot clobber
        # PATH / SHIM_DATA. Real `cat` is captured before the shim shadows it.
        real_cat = shutil.which("cat") or "/bin/cat"
        # With omit_real, PATH is bin_dir + the curated real-tools dir ONLY (no
        # system PATH), so `command -v <omitted>` fails — used to force
        # disk_smart's no-jq awk fallback. Otherwise bin_dir + the full PATH.
        rest = str(self._curated) if self._curated is not None else merged.get("PATH", "")
        merged["PATH"] = f"{self._bin_dir}:{rest}"
        merged["SHIM_DATA"] = str(self._data_dir)
        merged["SHIM_REALCAT"] = real_cat

        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged,
            start_new_session=True,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:  # pragma: no cover - shim fixtures never sleep
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), 9)
            return ExecResult(
                exit_code=None,
                stdout="",
                stderr="timeout",
                duration_seconds=timeout,
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode,
            stdout=out_b.decode(errors="replace"),
            stderr=err_b.decode(errors="replace"),
            duration_seconds=time.monotonic() - t0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise NotImplementedError("ShimShellTarget does not support read_file")
