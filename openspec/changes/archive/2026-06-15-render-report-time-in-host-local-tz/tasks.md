## 1. 共享渲染 helper:UTC→本地

- [ ] 1.1 `notifiers/_filters.py`:`fmt_time` 渲染前转本地——naive 先 `replace(tzinfo=UTC)`、再 `value.astimezone()`,然后 `strftime("%Y-%m-%d %H:%M")`。`UTC` 从 `datetime` import(py3.11+ `from datetime import UTC`)
- [ ] 1.2 测试 `tests/notifiers/`:pin TZ(`monkeypatch.setenv("TZ","Asia/Shanghai")` + `time.tzset()`)下,UTC-aware `08:55Z` → `16:55`;naive `08:55`(按 UTC 归一)→ `16:55`;**转换锚**:非 UTC TZ 下输出钟点 ≠ UTC 原钟点(防回归成直出 UTC)

## 2. reports show / list 渲染

- [ ] 2.1 `reporting/render_markdown.py`:`_fmt_dt`(started_at/finished_at)同样 naive→UTC 归一 + `astimezone()` 后 `isoformat()`(本地 offset)
- [ ] 2.2 `cli/reports.py`:`reports list` 行的 `row.timestamp.isoformat()` 同样转本地
- [ ] 2.3 测试:pin TZ 下 `reports show` 的 started_at/finished_at、`reports list` 行时间为本地;更新既有 `tests/reporting/test_render_markdown_meta.py` 的 naive fixture → aware-UTC + 按 pinned TZ 重算期望串

## 3. 不被误伤的回归锚

- [ ] 3.1 `render_json` 回归:`reports show --format json` / persisted `report_json` 的 datetime **仍 UTC**(机器格式 + diff/baseline 依赖),确认未经本地化路径
- [ ] 3.2 structlog 日志时间戳不在本提案范围(保持 UTC `...Z`),不动

## 4. 收尾

- [ ] 4.1 全量 `pytest tests/`(非子集,pin TZ 的测试跨 CI/本地稳定)+ `mypy --strict` + `ruff` 绿;**特别确认**未 pin TZ 的渲染断言不残留(否则 CI flaky)
- [ ] 4.2 `openspec-cn validate render-report-time-in-host-local-tz --strict` 绿;temp 副本实测 `archive` 不报错
- [ ] 4.3 真机:ts.mac-mini `git pull` 后 `schedule trigger` 验证 TG/飞书报告时间显示 CST(`16:xx`)而非 UTC(`08:xx`)
