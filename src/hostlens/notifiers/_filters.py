"""Shared Jinja filters for the notifier report templates.

Spec: ``openspec/specs/notifier-telegram/spec.md`` and
``openspec/specs/notifier-lark/spec.md`` (§需求:结构化布局 — 抬头 / 覆盖 /
发现优先 / 四元组去重 / 排序 / 多 target 分节 / 健康态 / fleet 主机归因).

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
  ``distinct(non-None target_name) ≤ 1`` **且非 fleet**. A fleet report
  (``fleet=True``) with ≥1 non-None target always sections by host (even a
  single distinct host), so a single-host fleet finding keeps its attribution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hostlens.core.timefmt import to_host_local

if TYPE_CHECKING:
    from datetime import datetime

    from hostlens.inspectors.result import InspectorResult
    from hostlens.reporting.models import Finding, InspectorRun

__all__ = [
    "conf_label",
    "coverage_line",
    "dedup_findings",
    "failed_checks",
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

# Bucket-level 中文 labels keyed on the **status** of a failed inspector run.
# Every failed status (the closed three-value ``_FAILED_STATUSES`` set) always
# maps here, so a reason label can always be derived without reading the
# free-text ``error``. ``target_unreachable`` is the only status whose ``error``
# is a structured ``TargetError.kind`` enum string (the other two carry a free
# sentence), so only it is refined further via ``_FAIL_KIND_LABELS``.
_FAIL_STATUS_LABELS: dict[str, str] = {
    "timeout": "执行超时",
    "target_unreachable": "不可达",
    "exception": "采集异常",
}

# Kind-level refinement used **only** when ``status == "target_unreachable"``
# (where ``error == TargetError.kind``, an enum string). An unknown kind
# (``ssh_no_entry`` / ``target_disabled`` / docker·k8s kinds…) falls back to the
# bucket-level 「不可达」 rather than rendering the raw English kind.
_FAIL_KIND_LABELS: dict[str, str] = {
    "ssh_connect_timeout": "连接超时",
    "ssh_auth_failed": "认证失败",
    "ssh_connect_failed": "连接失败",
    "ssh_connection_lost": "连接中断",
}


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


def _fail_label(status: str, error: str | None) -> str:
    """Reason label for one failed inspector result — keyed on ``status`` first.

    ``status`` is a closed five-value enum so it always maps to a bucket-level
    中文 label. Only ``target_unreachable`` carries a structured
    ``TargetError.kind`` in ``error`` (the other failed statuses carry a free
    sentence — ``"collect.command exceeded N seconds"`` / ``"parse_failed: …"``);
    so ``error`` refines the label **only** for ``target_unreachable``, with an
    unknown kind falling back to the bucket label「不可达」. The free-text
    ``error`` of ``timeout`` / ``exception`` is never used as a key — keying on
    it would render raw English and split a host's exceptions into N groups.
    """

    bucket = _FAIL_STATUS_LABELS[status]
    if status == "target_unreachable":
        return _FAIL_KIND_LABELS.get(error or "", bucket)
    return bucket


def failed_checks(
    inspector_results: list[InspectorResult],
) -> list[tuple[str, str, list[str]]]:
    """Group failed inspector results into ``(target_name, label, [name…])``.

    Filters ``inspector_results`` to ``status in _FAILED_STATUSES`` (reusing the
    closed ``coverage_line`` frozenset, **not** a ``status not in {ok,
    requires_unmet}`` negative predicate) and groups by ``(target_name, label)``
    preserving first-seen order, returning one tuple per group with its inspector
    name list. The label is derived by ``_fail_label`` (status-first), so a
    host's multiple ``exception`` results (whose free-text ``error`` differs)
    collapse into one group rather than fanning out.
    """

    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for ir in inspector_results:
        if ir.status not in _FAILED_STATUSES:
            continue
        key = (ir.target_name, _fail_label(ir.status, ir.error))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ir.name)
    return [(target, label, groups[(target, label)]) for target, label in order]


def fmt_time(value: datetime) -> str:
    """Format a ``datetime`` as host-local ``YYYY-MM-DD HH:MM`` (minute resolution).

    The report timestamp is stored UTC; render it in the host's local
    timezone so the daily push matches the operator's wall clock.
    """

    return to_host_local(value).strftime("%Y-%m-%d %H:%M")


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


def group_by_target(
    findings: list[Finding], *, fleet: bool = False
) -> list[tuple[str | None, list[Finding]]]:
    """Group findings into host sections by ``target_name``.

    Returns a list of ``(target_name, findings)`` sections. The grouping
    degrades to a **single** ``(None, findings)`` section (no host sectioning)
    when ``distinct(non-None target_name) ≤ 1`` **且非 fleet** (``fleet`` is
    ``False``) — i.e. an agent single-host report whose findings all carry
    ``None``, share one non-None target, or mix ``None`` with one single
    non-None value (the header already names the machine, so per-host
    sectioning is redundant).

    A **fleet** report (``fleet=True``) with ≥1 non-None target always takes
    the named-section path — *even when only one distinct host appears* — so a
    single-host fleet finding keeps its attribution (the spec's core fix). An
    **all-None fleet** report (``distinct == 0`` ∧ ``fleet``) falls through the
    named path with an empty ``named`` map and a non-empty ``none_group``,
    returning a single ``(None, findings)`` section; the template's "节数 > 1
    才渲未标注主机头" guard then renders it as a headerless flat list.

    Otherwise (``distinct ≥ 2``, or fleet with ``distinct ≥ 1``) real host
    sections are produced. When sectioning, sections are ordered by first
    appearance and each section preserves the incoming finding order. Findings
    with a ``None`` ``target_name`` (partial fleet stamping) collect into their
    own section keyed by ``None``, appended after the named sections in
    first-seen order.
    """

    distinct_named = {f.target_name for f in findings if f.target_name is not None}
    if len(distinct_named) <= 1 and not fleet:
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
