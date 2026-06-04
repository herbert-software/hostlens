"""``only_if`` routing — report-level severity derivation + DSL gate/eval.

Spec: ``openspec/changes/add-notifier-channels/specs/notify-routing/spec.md``
(§需求:`only_if` 路由必须复用硬化 DSL 求值器并对 severity 做有序比较 /
§需求:`only_if` 运行期求值异常必须归类为通道失败且隔离). Design D-3.

Two layers, mirroring the spec's load-time vs run-time split:

- **Load time** (``validate_only_if``): every manifest ``only_if`` is run
  through ``inspectors.dsl.validate_ast`` so a malformed / forbidden /
  empty-string expression fails loud *before* the scheduler ever fires —
  never silently swallowed at run time.
- **Run time** (``should_send``): the expression is evaluated against a
  context that maps the report's aggregate severity to an ordered rank
  (``info=0 < warning=1 < critical=2``) so ``severity >= warning`` is a
  numeric comparison rather than a string-lexicographic one, and exposes
  the union of all finding ``tags`` so ``'x' in tags`` works. **Any**
  runtime evaluation exception is caught and turned into a
  ``NotifyResult(status="failed")`` — it must never bubble out of the
  notify dispatch and must never change the already-decided ``RunStatus``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hostlens.inspectors import dsl
from hostlens.notifiers.base import NotifyResult, redact_secret_text

if TYPE_CHECKING:
    from hostlens.reporting.models import Report, Severity

__all__ = [
    "aggregate_severity",
    "collect_tags",
    "evaluate_only_if",
    "should_send",
    "validate_only_if",
]


# Ordered severity rank. The DSL context binds both the report's resolved
# rank (under ``severity``) and the three name→rank bindings so an
# expression like ``severity >= warning`` compares two ints rather than a
# str against a str (which would be lexicographic, not the severity ladder).
_SEV_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

# Inverse of ``_SEV_RANK`` for projecting the aggregate rank back to a
# ``Severity`` literal in ``aggregate_severity``.
_RANK_SEV: dict[int, Severity] = {0: "info", 1: "warning", 2: "critical"}

# Hard ceiling for a single ``only_if`` evaluation, handed to
# ``dsl.evaluate``; aligned with the inspector finding DSL default.
_ONLY_IF_TIMEOUT_SECONDS: float = 1.0


def aggregate_severity(report: Report) -> Severity:
    """Return the report-level severity = max over all finding severities.

    Comparison is by **rank** (``info=0 < warning=1 < critical=2``), not by
    string ordering — lexicographically ``"critical" < "info" < "warning"``,
    which would mis-rank the ladder. A report with no findings derives
    ``"info"`` (nothing wrong to report).

    Derived here (not on the frozen ``Report`` model) per design D-3: the
    Report model is frozen and §4.4 places routing in the Notifier domain.
    """

    highest_rank = 0
    for finding in report.findings:
        rank = _SEV_RANK[finding.severity]
        if rank > highest_rank:
            highest_rank = rank
    return _RANK_SEV[highest_rank]


def collect_tags(report: Report) -> list[str]:
    """Return the sorted union of every finding's ``tags`` across the report.

    Sorted so the ``tags`` binding handed to the DSL is deterministic
    (stable membership tests / reproducible logs). Duplicates across
    findings collapse to a single entry.
    """

    seen: set[str] = set()
    for finding in report.findings:
        seen.update(finding.tags)
    return sorted(seen)


def validate_only_if(only_if: str) -> None:
    """Load-time gate for one ``only_if`` expression.

    Runs ``dsl.validate_ast`` so a forbidden construct (lambda /
    comprehension / ``__import__`` / dunder attribute / import) **and** an
    unparsable empty string ``""`` fail loud at manifest load — never left
    to silently skip at run time. ``validate_ast`` is a syntax/AST gate: it
    does **not** resolve whether names exist, so a typo like ``severty``
    passes here and only fails at run time (caught by ``evaluate_only_if``).

    ``None`` (field omitted) means "always send" and is **not** passed here
    — callers skip the gate for ``None``. The empty string ``""`` is a
    distinct, illegal value (to always-send, omit the field) and raises via
    ``validate_ast``'s parse failure.

    Raises:
        simpleeval.FeatureNotAvailable: AST gate hit or empty-string parse
          failure (the exact type ``validate_ast`` raises).
    """

    dsl.validate_ast(only_if)


async def evaluate_only_if(only_if: str, report: Report) -> bool:
    """Evaluate ``only_if`` against the report's routing context.

    Builds the context ``{"severity": <rank>, "info": 0, "warning": 1,
    "critical": 2, "tags": [...]}`` and runs the expression through the
    hardened ``dsl.evaluate`` (static AST gate + 1s timeout), normalising
    the result with ``bool(...)``.

    This is the **raising** form: it propagates any evaluation exception so
    the caller (``should_send``) can decide whether to treat it as a routing
    skip vs a channel failure. Most callers want ``should_send`` instead.
    """

    severity = aggregate_severity(report)
    context: dict[str, object] = {
        "severity": _SEV_RANK[severity],
        **_SEV_RANK,
        "tags": collect_tags(report),
    }
    result = await dsl.evaluate(only_if, context, timeout_seconds=_ONLY_IF_TIMEOUT_SECONDS)
    return bool(result)


async def should_send(
    channel: str,
    only_if: str | None,
    report: Report,
) -> NotifyResult | None:
    """Decide routing for ``channel`` given its ``only_if`` and the report.

    Returns:
        - ``None`` when the channel should proceed to render/send — either
          ``only_if`` is ``None`` (always send) or it evaluated truthy.
        - ``NotifyResult(status="skipped")`` when ``only_if`` evaluated
          falsy (a normal routing skip, not an error).
        - ``NotifyResult(status="failed", error=...)`` when ``only_if``
          evaluation raised **any** exception at run time.

    The failure branch catches ``Exception`` wholesale — **not** a curated
    subset — per spec §需求:`only_if` 运行期求值异常必须归类为通道失败且隔离:
    type mismatch (``TypeError`` from ``severity >= 'warning'``), undefined
    name (``simpleeval.NameNotDefined`` from a typo), timeout
    (``TimeoutError``), and every other ``simpleeval`` runtime class
    (``InvalidExpression`` / ``FeatureNotAvailable`` / ``NumberTooHigh`` /
    ``FunctionNotDefined`` …) all collapse to one ``failed`` result. The
    error text is run through ``redact_secret_text`` before it lands on the
    ``NotifyResult`` (which persists into ``runs.db``). The exception is
    swallowed here so a single channel's bad expression neither bubbles out
    of the notify dispatch nor disturbs the other channels' routing.
    """

    if only_if is None:
        return None

    try:
        passed = await evaluate_only_if(only_if, report)
    except Exception as exc:  # spec mandates wholesale catch (failure isolation)
        return NotifyResult(
            channel=channel,
            status="failed",
            error=redact_secret_text(f"only_if evaluation failed: {exc!r}"),
        )

    if passed:
        return None
    return NotifyResult(channel=channel, status="skipped")
