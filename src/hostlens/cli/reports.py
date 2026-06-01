"""``hostlens reports`` Typer subcommand group — persisted-report browsing.

Spec: ``openspec/changes/add-report-persistence-and-diff/specs/report-persistence/spec.md``
and ``.../specs/report-regression-diff/spec.md``.

Three read-only commands over the SQLite ``ReportStore`` (no remote-state
write, so no ``--yes`` / approval gate — the store is local):

- ``list <target> [--json]`` — run index for ``meta.target_id`` (M3:
  ``target_id == target_name``, so the user passes the target name).
- ``show <run_id> [--format md|json]`` — render one persisted report.
- ``diff <a> <b>`` (explicit two-run) **or** ``diff --target <t>
  [--baseline last_success] [--force]`` (auto-baseline) — regression diff.

Exit code contract (aligned with the project-wide CLI semantics, priority
``3 > 2 > 1 > 0``):

- ``0`` success — including "empty history", "no comparable baseline",
  and a diff that *does* contain regressions (regression is expressed in
  the output, not the exit code).
- ``2`` orphan-degraded persistence is surfaced by ``inspect --persist``,
  not here; ``reports`` is purely read-only.
- ``3`` not-found — unknown ``run_id`` for ``show`` / ``diff``. Also the
  Typer-usage errors rewritten by the click-UsageError wrapper in
  ``cli/__init__.py``.

stdout / stderr separation: rendered report / run index → stdout; error
hints → stderr; **no** Python traceback ever reaches the user.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import NoReturn

import click
import typer
from pydantic import ValidationError

import hostlens.inspectors.result  # noqa: F401  (triggers Report.model_rebuild)
from hostlens.reporting import render_json, render_markdown
from hostlens.reporting.diff import RegressionDiff, compute_diff
from hostlens.reporting.models import Report
from hostlens.reporting.store import ReportStore, RunIndexRow

__all__ = ["reports_app"]


reports_app = typer.Typer(
    name="reports",
    help="Browse and diff persisted inspection reports.",
    no_args_is_help=True,
    add_completion=False,
)


@reports_app.callback()
def _root() -> None:
    """Force Typer into multi-command mode so ``list`` / ``show`` / ``diff``
    stay addressable (same guard used by ``hostlens inspectors`` / ``demo``).
    """


def _store() -> ReportStore:
    """Construct a ``ReportStore`` at the default db path.

    The default path resolves ``$XDG_DATA_HOME/hostlens/reports.db``
    (``~/.local/share/hostlens/reports.db`` otherwise), so tests point the
    store at a temporary directory by setting ``XDG_DATA_HOME`` — the same
    knob ``inspect --persist`` reads.
    """

    return ReportStore()


# --------------------------------------------------------------------------- #
# `hostlens reports list`
# --------------------------------------------------------------------------- #


@reports_app.command("list")
def list_cmd(
    target: str = typer.Argument(
        ...,
        help="Target name (matches meta.target_id; M3: target_id == target_name).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the run index as a JSON array to stdout instead of a table.",
    ),
) -> None:
    """List persisted runs for ``target`` (newest first).

    Empty history is **not** an error: a hint is printed and the command
    exits 0. ``--json`` emits an array whose elements carry exactly the
    ``RunIndexRow`` field set (``run_id`` / ``timestamp`` / ``status`` /
    ``finding_count``).
    """

    try:
        rows = asyncio.run(_store().list_runs(target))
    except (ValueError, sqlite3.Error) as exc:
        _store_unavailable(exc)

    if json_output:
        payload = [row.model_dump(mode="json") for row in rows]
        typer.echo(_dumps(payload))
        return

    if not rows:
        typer.echo(
            f"无历史 run: {target} —— 先运行 "
            "'hostlens inspect <target> --inspector <name> --persist' 落盘"
        )
        return

    for row in rows:
        typer.echo(_format_row(row))


def _format_row(row: RunIndexRow) -> str:
    return f"{row.run_id}\t{row.timestamp.isoformat()}\t{row.status}\tfindings={row.finding_count}"


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# `hostlens reports show`
# --------------------------------------------------------------------------- #


@reports_app.command("show")
def show_cmd(
    run_id: str = typer.Argument(..., help="Run id (from `hostlens reports list`)."),
    fmt: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: 'md' or 'json'.",
        click_type=click.Choice(["md", "json"]),
    ),
) -> None:
    """Render the persisted report for ``run_id``.

    Unknown ``run_id`` → single stderr line ``run not found: <run_id>``
    with a ``reports list`` hint, exit 3, no report body on stdout, no
    Python traceback.
    """

    try:
        report = asyncio.run(_store().get_run(run_id))
    except ValidationError as exc:
        _invalid_report(run_id, exc)
    except sqlite3.Error as exc:
        _store_unavailable(exc)
    if report is None:
        _run_not_found(run_id)

    rendered = render_markdown(report) if fmt == "md" else render_json(report)
    typer.echo(rendered)


def _run_not_found(run_id: str) -> NoReturn:
    typer.echo(
        f"run not found: {run_id}; run 'hostlens reports list <target>' to see persisted runs",
        err=True,
    )
    raise typer.Exit(code=3)


def _invalid_report(run_id: str, exc: Exception) -> NoReturn:
    # A stored `report_json` blob that is damaged / manually edited / written by
    # an incompatible schema makes `get_run`'s `Report.model_validate_json`
    # raise `ValidationError`. Surface it as a single stderr line + exit 3 (the
    # report is present but unusable), never a raw Python traceback. A pydantic
    # `ValidationError`'s `str()` is multi-line, so only the type name is
    # embedded — the full validation detail is not actionable for the user.
    typer.echo(f"stored report is invalid or corrupt: {run_id} ({type(exc).__name__})", err=True)
    raise typer.Exit(code=3)


def _store_unavailable(exc: Exception) -> NoReturn:
    # A corrupt index row (`ReportStatus(...)` / `datetime.fromisoformat(...)` →
    # `ValueError`) or a damaged / unreadable db (connect / PRAGMA WAL →
    # `sqlite3.Error`). Single stderr line + exit 3, never a raw traceback.
    typer.echo(f"reports: store unavailable or corrupt: {type(exc).__name__}", err=True)
    raise typer.Exit(code=3)


async def _load_report(store: ReportStore, run_id: str) -> Report:
    """Load a persisted report, mapping a corrupt/invalid blob
    (`ValidationError`) and a missing run to single stderr lines + exit 3 —
    never a traceback. Shared by `reports show` / `diff` (explicit + auto).
    """
    try:
        report = await store.get_run(run_id)
    except ValidationError as exc:
        _invalid_report(run_id, exc)
    if report is None:
        _run_not_found(run_id)
    return report


def _compute_diff_or_exit(baseline: Report, current: Report, *, force: bool) -> RegressionDiff:
    """Run `compute_diff`, mapping its cross-target `ValueError` (rule 1 —
    reachable in auto mode too via a corrupt index/blob `target_id` mismatch)
    to a single stderr line + exit 3 instead of a traceback.
    """
    try:
        return compute_diff(baseline, current, force=force)
    except ValueError as exc:
        typer.echo(f"hostlens reports diff: {exc}", err=True)
        raise typer.Exit(code=3) from exc


# --------------------------------------------------------------------------- #
# `hostlens reports diff`
# --------------------------------------------------------------------------- #


@reports_app.command("diff")
def diff_cmd(
    run_id_a: str | None = typer.Argument(None, help="Baseline run id (explicit two-run mode)."),
    run_id_b: str | None = typer.Argument(None, help="Current run id (explicit two-run mode)."),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Auto-baseline mode: diff the latest run for this target against "
        "its most recent ok baseline.",
    ),
    baseline: str = typer.Option(
        "last_success",
        "--baseline",
        help="Auto-baseline selector (only 'last_success' is supported in M3).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Diff even when the baseline status is not 'ok'.",
    ),
) -> None:
    """Compare two persisted reports (explicit or auto-baseline mode).

    Two mutually exclusive modes:

    - ``diff <a> <b>`` — ``a`` is baseline, ``b`` is current.
    - ``diff --target <t>`` — current = the target's total-order-latest run;
      baseline = its most-recent ``ok`` run (excluding current itself).

    Exit codes: unknown run → 3; no comparable baseline (first run / all
    non-ok / auto mode with only the current run) → printed text + exit 0;
    a diff containing regressions still exits 0 (the regression shows in
    the output, not the exit code).
    """

    explicit = run_id_a is not None and run_id_b is not None
    if target is not None:
        if run_id_a is not None or run_id_b is not None:
            _usage_error("--target is mutually exclusive with explicit run ids")
        if baseline != "last_success":
            _usage_error(
                f"unsupported --baseline value: {baseline!r} (M3 supports only 'last_success')"
            )
        try:
            asyncio.run(_diff_auto(target, force=force))
        except (sqlite3.Error, ValueError, ValidationError) as exc:
            _store_unavailable(exc)
        return

    if not explicit:
        _usage_error("provide two run ids (`reports diff <a> <b>`) or `--target <t>`")

    assert run_id_a is not None and run_id_b is not None
    try:
        asyncio.run(_diff_explicit(run_id_a, run_id_b, force=force))
    except (sqlite3.Error, ValueError, ValidationError) as exc:
        _store_unavailable(exc)


def _usage_error(message: str) -> NoReturn:
    typer.echo(f"hostlens reports diff: {message}", err=True)
    raise typer.Exit(code=3)


async def _diff_explicit(run_id_a: str, run_id_b: str, *, force: bool) -> None:
    store = _store()
    baseline_report = await _load_report(store, run_id_a)
    current_report = await _load_report(store, run_id_b)
    _render_diff(_compute_diff_or_exit(baseline_report, current_report, force=force))


async def _diff_auto(target: str, *, force: bool) -> None:
    store = _store()
    latest = await store.list_runs(target, limit=1)
    if not latest:
        typer.echo(f"无可比基线: {target} 无任何历史 run")
        return

    # The index row was just listed; a missing/corrupt report here is a store
    # inconsistency — `_load_report` surfaces both as exit 3, not a traceback.
    current = await _load_report(store, latest[0].run_id)

    baseline_ref = await store.latest_ok_baseline(target, before_run_id=latest[0].run_id)
    if baseline_ref is None:
        typer.echo(f"无可比基线: {target} 在当前 run 之前没有 ok 基线")
        return

    baseline_report = await _load_report(store, baseline_ref.run_id)
    _render_diff(_compute_diff_or_exit(baseline_report, current, force=force))


def _render_diff(diff: RegressionDiff) -> None:
    """Render a ``RegressionDiff`` to stdout as concise readable text."""

    if diff.diff_skipped_reason is not None:
        typer.echo(f"diff 跳过: {diff.diff_skipped_reason}")
        return

    if diff.inspector_upgraded:
        typer.echo(f"inspector 版本变更: {', '.join(diff.inspector_upgraded)}")

    typer.echo(f"added ({len(diff.added)}):")
    for fp in diff.added:
        typer.echo(f"  + {fp.severity}: {fp.message}")

    typer.echo(f"resolved ({len(diff.resolved)}):")
    for fp in diff.resolved:
        typer.echo(f"  - {fp.severity}: {fp.message}")

    typer.echo(f"changed_severity ({len(diff.changed_severity)}):")
    for sc in diff.changed_severity:
        typer.echo(f"  ~ {sc.from_severity} -> {sc.to_severity}: {sc.message}")
