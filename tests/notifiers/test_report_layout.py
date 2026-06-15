"""Structured-layout rendering tests for the Telegram + Lark notifiers.

Spec: ``openspec/changes/improve-report-rendering-and-i18n/specs/notifier-telegram/spec.md``
and ``.../notifier-lark/spec.md`` (§需求:结构化布局 — 抬头非 intent / 覆盖行 /
根因置顶 / 四元组去重 / 同 message 不同 severity 不去重 / severity 排序 / 带来源 /
健康态 / 多 target 分节 / 单主机退化 / 去重x分节组合).

Both channels render through the real ``render()`` entry point (which redacts
the report before templating), so the multi-target cases exercise the
proposal-B ``_redact_finding`` ``target_name`` pass-through — feeding a raw
(unredacted) report would mask a dropped ``target_name`` and turn a real
regression green.
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.lark import LarkNotifier
from hostlens.notifiers.telegram import TelegramNotifier
from hostlens.reporting.models import (
    Finding,
    Report,
    RootCauseHypothesis,
    Severity,
)

_START = datetime(2026, 1, 1, 3, 30)
_END = datetime(2026, 1, 1, 3, 31)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _ir(
    name: str,
    *,
    target: str = "web-1",
    status: str = "ok",
    findings: list[Finding] | None = None,
    error: str | None = None,
) -> InspectorResult:
    return InspectorResult(
        name=name,
        version="1.0.0",
        status=status,  # type: ignore[arg-type]
        target_name=target,
        duration_seconds=0.1,
        output={},
        findings=findings or [],
        error=error,
        missing=["svc"] if status == "requires_unmet" else [],
    )


def _single_report(
    findings: list[Finding],
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
    extra_results: list[InspectorResult] | None = None,
    intent: str = "对所有主机做夜间巡检并报告异常",
) -> Report:
    results = [_ir("linux.systemd", findings=findings)]
    if extra_results:
        results.extend(extra_results)
    report = Report.from_inspector_results(
        "web-1",
        results,
        started_at=_START,
        finished_at=_END,
        intent=intent,
    )
    if hypotheses:
        report = report.model_copy(update={"hypotheses": hypotheses})
    return report


def _fleet_report(
    results: list[InspectorResult],
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
) -> Report:
    report = Report.from_fleet_results(
        results,
        schedule_name="nightly",
        started_at=_START,
        finished_at=_END,
        intent="对 fleet 做夜间巡检",
    )
    if hypotheses:
        report = report.model_copy(update={"hypotheses": hypotheses})
    return report


def _tg() -> TelegramNotifier:
    return TelegramNotifier(
        instance_name="ops-tg",
        config={"bot_token": "x", "chat_id": "1"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def _lark() -> LarkNotifier:
    return LarkNotifier(
        instance_name="ops-lk",
        config={"webhook_url": "https://open.feishu.cn/hook/x"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def _tg_body(report: Report, severity: Severity) -> str:
    return _tg().render(report, severity=severity).body


def _lark_card(report: Report, severity: Severity) -> dict[str, object]:
    body = _lark().render(report, severity=severity).body
    card = json.loads(body)
    assert isinstance(card, dict)
    return card


def _lark_contents(card: dict[str, object]) -> list[str]:
    """Flatten every ``lark_md`` / ``plain_text`` content string from the card."""

    out: list[str] = []
    inner = card["card"]
    assert isinstance(inner, dict)
    header = inner["header"]
    assert isinstance(header, dict)
    title = header["title"]
    assert isinstance(title, dict)
    out.append(str(title["content"]))
    elements = inner["elements"]
    assert isinstance(elements, list)
    for el in elements:
        assert isinstance(el, dict)
        text = el.get("text")
        if isinstance(text, dict) and "content" in text:
            out.append(str(text["content"]))
    return out


# --------------------------------------------------------------------------- #
# 抬头不是 intent + 覆盖行
# --------------------------------------------------------------------------- #


def test_telegram_header_is_not_intent_and_has_coverage() -> None:
    report = _single_report(
        [Finding(severity="critical", message="磁盘使用率 95%")],
        intent="对所有主机做夜间巡检并报告异常这是一段很长的意图描述",
    )
    body = _tg_body(report, "critical")
    first_line = body.splitlines()[0]
    assert "Hostlens 巡检" in first_line
    assert "严重" in first_line
    assert "🔴" in first_line
    # The integral intent sentence must NOT be the title.
    assert "对所有主机做夜间巡检" not in first_line
    # Coverage line present (N/M 项检查).
    assert "项检查" in body


def test_lark_header_is_not_intent_and_isomorphic() -> None:
    report = _single_report(
        [Finding(severity="critical", message="磁盘使用率 95%")],
        hypotheses=[
            RootCauseHypothesis(
                description="磁盘写满", confidence="high", suggested_actions=["清理"]
            )
        ],
        intent="对所有主机做夜间巡检并报告异常这是一段很长的意图描述",
    )
    card = _lark_card(report, "critical")
    header = card["card"]["header"]  # type: ignore[index]
    title_content = header["title"]["content"]  # type: ignore[index]
    assert "Hostlens 巡检" in title_content
    assert "严重" in title_content
    assert "对所有主机做夜间巡检" not in title_content
    contents = _lark_contents(card)
    joined = "\n".join(contents)
    assert "项检查" in joined  # coverage
    assert "**根因分析**" in contents  # root cause section
    assert "**发现**" in contents


# --------------------------------------------------------------------------- #
# 覆盖行计入失败状态
# --------------------------------------------------------------------------- #


def _coverage_results() -> list[InspectorResult]:
    """5 ok / 1 requires_unmet / 2 failed (timeout + target_unreachable) = 8."""

    results = [_ir(f"linux.ok{i}") for i in range(5)]
    results.append(_ir("linux.skip", status="requires_unmet"))
    results.append(_ir("linux.t1", status="timeout", error="timed out"))
    results.append(_ir("linux.t2", status="target_unreachable", error="unreachable"))
    return results


def test_telegram_coverage_counts_failures() -> None:
    report = Report.from_inspector_results(
        "web-1", _coverage_results(), started_at=_START, finished_at=_END
    )
    body = _tg_body(report, "info")
    # ok=5, total=8, skipped=1, failed=2 → 5/8 项检查 · 1 项跳过 · 2 项失败
    assert "5/8 项检查" in body
    assert "1 项跳过" in body
    assert "2 项失败" in body
    # Invariant ok + skipped + failed == total (5 + 1 + 2 == 8).


def test_lark_coverage_counts_failures() -> None:
    report = Report.from_inspector_results(
        "web-1", _coverage_results(), started_at=_START, finished_at=_END
    )
    contents = _lark_contents(_lark_card(report, "info"))
    coverage = next(c for c in contents if "项检查" in c)
    assert "5/8 项检查" in coverage
    assert "1 项跳过" in coverage
    assert "2 项失败" in coverage


def test_coverage_omits_failed_clause_when_zero() -> None:
    # 3 ok + 1 requires_unmet, no timeout / unreachable / exception.
    results = [_ir(f"linux.ok{i}") for i in range(3)]
    results.append(_ir("linux.skip", status="requires_unmet"))
    report = Report.from_inspector_results("web-1", results, started_at=_START, finished_at=_END)
    body = _tg_body(report, "info")
    assert "项失败" not in body
    assert "3/4 项检查 · 1 项跳过" in body
    contents = _lark_contents(_lark_card(report, "info"))
    coverage = next(c for c in contents if "项检查" in c)
    assert "项失败" not in coverage


# --------------------------------------------------------------------------- #
# 根因分析置顶
# --------------------------------------------------------------------------- #


def test_telegram_root_cause_before_findings() -> None:
    report = _single_report(
        [Finding(severity="warning", message="磁盘使用率 80%")],
        hypotheses=[
            RootCauseHypothesis(
                description="日志暴涨占满磁盘",
                confidence="high",
                suggested_actions=["清理 /var/log", "扩容磁盘"],
            )
        ],
    )
    body = _tg_body(report, "warning")
    assert "*根因分析*" in body
    assert body.index("根因分析") < body.index("*发现*")
    # confidence rendered as 中文 label
    assert "置信度 高" in body
    # suggested_actions listed with ↳
    assert "↳ 清理 /var/log" in body
    assert "↳ 扩容磁盘" in body


def test_lark_root_cause_before_findings() -> None:
    report = _single_report(
        [Finding(severity="warning", message="磁盘使用率 80%")],
        hypotheses=[
            RootCauseHypothesis(
                description="日志暴涨占满磁盘",
                confidence="medium",
                suggested_actions=["清理 /var/log"],
            )
        ],
    )
    contents = _lark_contents(_lark_card(report, "warning"))
    assert "**根因分析**" in contents
    assert contents.index("**根因分析**") < contents.index("**发现**")
    assert any("置信度 中" in c for c in contents)
    assert any(c.startswith("↳ 清理 /var/log") for c in contents)


# --------------------------------------------------------------------------- #
# 四元组去重 + 排序 + 来源
# --------------------------------------------------------------------------- #


def test_telegram_dedup_sort_and_source() -> None:
    dup = Finding(severity="warning", message="磁盘使用率 80%")
    low = Finding(severity="info", message="负载正常")
    report = _single_report([dup, dup, low])
    body = _tg_body(report, "warning")
    # The two identical (four-tuple equal) findings collapse to one line.
    assert body.count("磁盘使用率 80%") == 1
    # critical/warning ranks above info → warning line before info line.
    assert body.index("磁盘使用率 80%") < body.index("负载正常")
    # Each finding carries its source inspector name.
    assert "linux\\.systemd" in body


def test_lark_dedup_sort_and_source() -> None:
    dup = Finding(severity="warning", message="磁盘使用率 80%")
    low = Finding(severity="info", message="负载正常")
    report = _single_report([dup, dup, low])
    contents = _lark_contents(_lark_card(report, "warning"))
    disk = [c for c in contents if "磁盘使用率 80%" in c]
    assert len(disk) == 1
    assert "(linux.systemd)" in disk[0]
    disk_idx = next(i for i, c in enumerate(contents) if "磁盘使用率 80%" in c)
    load_idx = next(i for i, c in enumerate(contents) if "负载正常" in c)
    assert disk_idx < load_idx


# --------------------------------------------------------------------------- #
# 同 message 不同 severity 不去重
# --------------------------------------------------------------------------- #


def test_telegram_same_message_diff_severity_not_merged() -> None:
    report = _single_report(
        [
            Finding(severity="critical", message="服务 nginx 异常"),
            Finding(severity="warning", message="服务 nginx 异常"),
        ]
    )
    body = _tg_body(report, "critical")
    assert body.count("服务 nginx 异常") == 2
    # critical (🔴) ranks above warning (⚠️) in the 发现 section.
    crit = body.index("🔴 服务 nginx 异常")
    warn = body.index("⚠️ 服务 nginx 异常")
    assert crit < warn


def test_lark_same_message_diff_severity_not_merged() -> None:
    report = _single_report(
        [
            Finding(severity="critical", message="服务 nginx 异常"),
            Finding(severity="warning", message="服务 nginx 异常"),
        ]
    )
    contents = _lark_contents(_lark_card(report, "critical"))
    nginx = [c for c in contents if "服务 nginx 异常" in c]
    assert len(nginx) == 2


# --------------------------------------------------------------------------- #
# 健康态
# --------------------------------------------------------------------------- #


def test_telegram_health_no_empty_findings_section() -> None:
    report = _single_report([])
    body = _tg_body(report, "info")
    assert "✅ 未发现异常" in body
    assert "*发现*" not in body
    # Coverage line still present.
    assert "项检查" in body


def test_lark_health_no_empty_findings_section() -> None:
    report = _single_report([])
    contents = _lark_contents(_lark_card(report, "info"))
    assert "✅ 未发现异常" in contents
    assert "**发现**" not in contents


# --------------------------------------------------------------------------- #
# 多 target 分节
# --------------------------------------------------------------------------- #


def test_telegram_multi_target_sections() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ]
    )
    body = _tg_body(report, "critical")
    # Each section header carries the host's own max severity (spec「每节主机名 +
    # 该主机 severity」): hostA's finding is critical, hostB's is warning.
    assert "*hostA · 严重*" in body
    assert "*hostB · 警告*" in body
    # hostA section holds its finding; hostB section holds its own.
    assert body.index("hostA") < body.index("磁盘满")
    assert body.index("hostB") < body.index("CPU 高")


def test_lark_multi_target_sections() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message="磁盘满")],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ]
    )
    joined = "\n".join(_lark_contents(_lark_card(report, "critical")))
    # Per-host section header carries the host's own max severity.
    assert "**hostA · 严重**" in joined
    assert "**hostB · 警告**" in joined


def test_telegram_none_section_gets_header_when_sectioned() -> None:
    # A sectioned fleet report where one finding is unstamped (target_name None,
    # e.g. partial fleet stamping) renders an explicit "(未标注主机)" header for
    # the None group rather than silently folding it under the previous host.
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
                Finding(severity="info", message="未知来源项", target_name=None),
            ]
        }
    )
    body = _tg_body(report, "critical")
    # Header uses fullwidth parens (U+FF08/FF09) — not MarkdownV2-reserved (ASCII
    # parens would need escaping); assert the CJK label, not the paren form.
    assert "未标注主机" in body
    # The unstamped finding renders under its own header, after the named hosts.
    assert body.index("未标注主机") < body.index("未知来源项")
    assert body.index("hostA") < body.index("未标注主机")


def test_lark_none_section_gets_header_when_sectioned() -> None:
    # Lark mirror of the above — its None section uses a leading-comma `,{...}`
    # card element, more fragile than Telegram's newline, so assert the header
    # renders AND the card is still valid JSON (no dangling/double comma).
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
                Finding(severity="info", message="未知来源项", target_name=None),
            ]
        }
    )
    contents = _lark_contents(_lark_card(report, "critical"))  # _lark_card asserts valid JSON
    assert any("未标注主机" in c for c in contents)
    assert any("未知来源项" in c for c in contents)


def test_none_section_stays_last_even_when_unstamped_finding_is_first() -> None:
    # Regression: a None-target finding appearing FIRST in the flattened list
    # must NOT push the unlabeled section ahead of the named hosts —
    # group_by_target holds the None group aside and appends it last, so the
    # documented order ("None section after named hosts") holds for any input order.
    base = _fleet_report(
        [_ir("linux.disk", target="hostA", findings=[Finding(severity="info", message="seed")])]
    )
    report = base.model_copy(
        update={
            "findings": [
                Finding(severity="info", message="未知来源项", target_name=None),  # FIRST
                Finding(severity="critical", message="磁盘满", target_name="hostA"),
                Finding(severity="warning", message="CPU 高", target_name="hostB"),
            ]
        }
    )
    body = _tg_body(report, "critical")
    assert body.index("hostA") < body.index("未标注主机")
    assert body.index("hostB") < body.index("未标注主机")


# --------------------------------------------------------------------------- #
# 去重 x 分节组合
# --------------------------------------------------------------------------- #


def test_telegram_dedup_x_section_cross_host_not_merged() -> None:
    # hostA & hostB: same inspector/message/severity but different target →
    # NOT merged (target_name in the dedup key). hostA also has an intra-host
    # exact duplicate that MUST merge.
    same = Finding(severity="critical", message="磁盘满")
    report = _fleet_report(
        [
            _ir("linux.disk", target="hostA", findings=[same, same]),
            _ir("linux.disk", target="hostB", findings=[same]),
        ]
    )
    body = _tg_body(report, "critical")
    # Cross-host: two sections, each one finding → message appears twice total.
    assert body.count("磁盘满") == 2
    # Both hosts' findings are critical → each section header shows 严重.
    assert "*hostA · 严重*" in body
    assert "*hostB · 严重*" in body


def test_lark_dedup_x_section_cross_host_not_merged() -> None:
    same = Finding(severity="critical", message="磁盘满")
    report = _fleet_report(
        [
            _ir("linux.disk", target="hostA", findings=[same, same]),
            _ir("linux.disk", target="hostB", findings=[same]),
        ]
    )
    contents = _lark_contents(_lark_card(report, "critical"))
    disk = [c for c in contents if "磁盘满" in c]
    assert len(disk) == 2
    joined = "\n".join(contents)
    assert "**hostA · 严重**" in joined
    assert "**hostB · 严重**" in joined


# --------------------------------------------------------------------------- #
# 单主机退化(distinct non-None ≤ 1)
# --------------------------------------------------------------------------- #


def test_telegram_single_host_no_sections() -> None:
    # Single-target report: target_name on findings is None (per-target path).
    report = _single_report([Finding(severity="warning", message="磁盘 80%")])
    assert all(f.target_name is None for f in report.findings)
    body = _tg_body(report, "warning")
    assert "*发现*" in body
    # No per-host bold section header appears below the 发现 heading — the only
    # bold markers are the 抬头 and ``*发现*`` itself.
    findings_block = body.split("*发现*", 1)[1]
    assert "*" not in findings_block


def test_telegram_single_host_degrade_when_one_named_target() -> None:
    # A fleet of one target → distinct non-None == 1 → no sectioning.
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="only-host",
                findings=[Finding(severity="warning", message="磁盘 80%")],
            )
        ]
    )
    body = _tg_body(report, "warning")
    assert "*only-host*" not in body
    assert "磁盘 80%" in body


def test_lark_single_host_no_sections() -> None:
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="only-host",
                findings=[Finding(severity="warning", message="磁盘 80%")],
            )
        ]
    )
    contents = _lark_contents(_lark_card(report, "warning"))
    assert "**only-host**" not in contents
    assert any("磁盘 80%" in c for c in contents)


# --------------------------------------------------------------------------- #
# MarkdownV2 转义不回归
# --------------------------------------------------------------------------- #


def test_telegram_markdownv2_escape_not_regressed() -> None:
    report = _single_report([Finding(severity="warning", message="disk 95.2% used (warn!)")])
    body = _tg_body(report, "warning")
    assert r"95\.2%" in body
    assert r"\(warn\!\)" in body


def test_lark_renders_valid_json_for_every_scenario() -> None:
    # Smoke: a rich report (findings + hypotheses + multi-target) must serialize
    # to valid JSON through render() (json.loads is inside _lark_card).
    report = _fleet_report(
        [
            _ir(
                "linux.disk",
                target="hostA",
                findings=[Finding(severity="critical", message='磁盘满 "quoted" \\ slash')],
            ),
            _ir(
                "linux.cpu",
                target="hostB",
                findings=[Finding(severity="warning", message="CPU 高")],
            ),
        ],
        hypotheses=[
            RootCauseHypothesis(
                description='根因含 "引号" 与 \\ 反斜杠',
                confidence="high",
                suggested_actions=['执行 "command"'],
            )
        ],
    )
    card = _lark_card(report, "critical")  # raises if invalid JSON
    assert card["msg_type"] == "interactive"
