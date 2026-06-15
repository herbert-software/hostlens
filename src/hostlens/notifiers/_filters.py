"""Shared Jinja filters for the notifier report templates.

Spec: ``openspec/changes/improve-report-rendering-and-i18n/specs/notifier-telegram/spec.md``
and ``.../notifier-lark/spec.md`` (§需求:结构化布局 — 抬头 / 覆盖 / 根因优先 /
四元组去重 / 排序 / 多 target 分节 / 健康态).

These filters are **pure functions** registered into both the Telegram and
the Lark Jinja environments (``telegram.py`` / ``lark.py``
``_build_environment``) so the two channels render an isomorphic information
structure from one implementation. They consume the **redacted** copy of the
report (notifier ``render`` redacts before templating), so every ``Finding``
field they read is already a redacted string / ``None``.

Filter set (the seven names pinned by the change):

- ``sev_label`` — severity literal → 中文 label (``critical→严重``).
- ``conf_label`` — confidence literal → 中文 label (``high→高``).
- ``coverage`` — ``meta.inspectors_used`` → ``{ok}/{total} 项检查 · …`` 覆盖串.
- ``fmt_time`` — ``datetime`` → human ``YYYY-MM-DD HH:MM`` string.
- ``dedup`` — collapse findings whose ``(target_name, inspector_name,
  message, severity)`` four-tuple is fully equal.
- ``sort_sev`` — sort findings by severity rank descending
  (critical → warning → info), stable within a rank.
- ``group_by_target`` — group findings by ``target_name`` into host sections,
  degrading to a single ``(None, findings)`` section when
  ``distinct(non-None target_name) ≤ 1``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from hostlens.reporting.models import Finding, InspectorRun

__all__ = [
    "conf_label",
    "coverage_line",
    "dedup_findings",
    "fmt_time",
    "group_by_target",
    "section_severity",
    "sev_label",
    "sort_sev",
]

# 中文 severity labels for the three-value closed ladder. Unknown values fall
# back to the raw string so a future ladder value never renders as an empty
# cell.
_SEV_LABELS: dict[str, str] = {
    "info": "信息",
    "warning": "警告",
    "critical": "严重",
}

# 中文 confidence labels for the RootCauseHypothesis confidence enum.
_CONF_LABELS: dict[str, str] = {
    "low": "低",
    "medium": "中",
    "high": "高",
}

# Severity rank for sort/group ordering (critical highest). Mirrors
# ``notifiers/routing.py`` ``_SEV_RANK`` but kept local so the template layer
# does not import the routing module.
_SEV_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

# Inspector statuses that count as a *failure* in the coverage line — every
# non-``ok``, non-``requires_unmet`` value of the closed five-value
# ``InspectorStatus`` set. Listed explicitly (not "everything else") so a new
# status value cannot silently fold into ``failed`` or ``skipped``.
_FAILED_STATUSES: frozenset[str] = frozenset({"timeout", "target_unreachable", "exception"})


def sev_label(severity: object) -> str:
    """Map a severity literal to its 中文 label (``critical`` → ``严重``)."""

    return _SEV_LABELS.get(str(severity), str(severity))


def conf_label(confidence: object) -> str:
    """Map a confidence literal to its 中文 label (``high`` → ``高``)."""

    return _CONF_LABELS.get(str(confidence), str(confidence))


def coverage_line(inspectors_used: list[InspectorRun]) -> str:
    """Build the coverage clause from ``meta.inspectors_used``.

    Counts each run's status into one of three buckets over the closed
    five-value ``InspectorStatus`` set:

    - ``ok`` → ``ok``
    - ``requires_unmet`` → ``skipped``
    - ``timeout`` / ``target_unreachable`` / ``exception`` → ``failed``

    The invariant ``ok + skipped + failed == total`` always holds (every
    status maps to exactly one bucket). The ``· {failed} 项失败`` clause is
    rendered **only** when ``failed > 0`` so a clean run never carries a
    ``· 0 项失败`` noise tail.
    """

    total = len(inspectors_used)
    ok = 0
    skipped = 0
    failed = 0
    for run in inspectors_used:
        status = run.status
        if status == "ok":
            ok += 1
        elif status == "requires_unmet":
            skipped += 1
        elif status in _FAILED_STATUSES:
            failed += 1
        else:  # pragma: no cover - closed five-value set is exhausted above
            failed += 1

    line = f"{ok}/{total} 项检查 · {skipped} 项跳过"
    if failed > 0:
        line += f" · {failed} 项失败"
    return line


def fmt_time(value: datetime) -> str:
    """Format a ``datetime`` as ``YYYY-MM-DD HH:MM`` (minute resolution)."""

    return value.strftime("%Y-%m-%d %H:%M")


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse findings whose ``(target_name, inspector_name, message,
    severity)`` four-tuple is fully equal, preserving first-seen order.

    The key is the **full four-tuple** — two findings merge only when all
    four fields match. Two findings with the same ``message`` but a
    different ``severity`` (or a different ``target_name`` across hosts) are
    distinct and both kept; keying on ``(inspector_name, message)`` alone
    would wrongly merge them.
    """

    seen: set[tuple[str | None, str | None, str, str]] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (
            finding.target_name,
            finding.inspector_name,
            finding.message,
            finding.severity,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def sort_sev(findings: list[Finding]) -> list[Finding]:
    """Sort findings by severity rank descending (critical → warning → info).

    A stable sort, so findings of equal severity keep their incoming order
    (and thus their post-dedup first-seen order).
    """

    return sorted(findings, key=lambda f: _SEV_RANK.get(f.severity, 0), reverse=True)


def group_by_target(findings: list[Finding]) -> list[tuple[str | None, list[Finding]]]:
    """Group findings into host sections by ``target_name``.

    Returns a list of ``(target_name, findings)`` sections. The grouping
    degrades to a **single** ``(None, findings)`` section (no host sectioning)
    when ``distinct(non-None target_name) ≤ 1`` — i.e. all findings carry
    ``None``, or share one non-None target, or are a mix of ``None`` and one
    single non-None value. Only when two or more *distinct* non-None target
    names appear are real host sections produced.

    When sectioning, sections are ordered by first appearance and each
    section preserves the incoming finding order. Findings with a ``None``
    ``target_name`` (partial fleet stamping) collect into their own section
    keyed by ``None``, appended after the named sections in first-seen order.
    """

    distinct_named = {f.target_name for f in findings if f.target_name is not None}
    if len(distinct_named) <= 1:
        return [(None, list(findings))]

    # Named sections in first-seen order; the unstamped (None) group is held
    # aside and appended **last** regardless of where its findings appear in the
    # incoming list — a None finding that happens to come first must not push
    # the unlabeled section ahead of the named hosts (the documented order).
    named: dict[str, list[Finding]] = {}
    none_group: list[Finding] = []
    for finding in findings:
        if finding.target_name is None:
            none_group.append(finding)
        else:
            named.setdefault(finding.target_name, []).append(finding)
    sections: list[tuple[str | None, list[Finding]]] = list(named.items())
    if none_group:
        sections.append((None, none_group))
    return sections


def section_severity(findings: list[Finding]) -> str:
    """Return the highest-rank severity among a host section's findings
    (critical > warning > info) for the per-host section header — the spec's
    "每节主机名 + 该主机 severity" requirement. A section always carries ≥1
    finding (empty groups are never produced); an empty list falls back to
    ``info`` rather than raising.
    """

    if not findings:
        return "info"
    return max(findings, key=lambda f: _SEV_RANK.get(f.severity, 0)).severity
