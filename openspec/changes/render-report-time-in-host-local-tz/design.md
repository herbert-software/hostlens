## 决策 1:渲染层 `astimezone()` 转本地,存储不动

报告时间戳的正确分层是 **UTC 存储 + 本地显示**。源时钟 `datetime.now(UTC)`（`orchestration/deterministic.py:293` 等）产生 UTC-aware datetime,持久化、baseline 排序（report-persistence 的 `(timestamp DESC, rowid DESC)`）、diff 都依赖 UTC 的单调可比性,**不能动**。bug 只在渲染层「忘了转时区」。

修复:三处人类可读渲染在 `strftime` / `isoformat` 前调 `value.astimezone()`(无参 → 系统本地 TZ)。`astimezone()` 对 **aware** datetime 是纯时区换算(同一时刻、换钟面),不改语义。

## 决策 2:naive datetime 按 UTC 归一(渲染器入口防御)

`datetime.astimezone()` 对 **naive** datetime 的行为是「假设它已是系统本地时间」——若一个 naive **UTC** 值（如某些既有测试 fixture 的 `datetime(2026,5,26,12,0,0)`）走这条路,会被当本地、不偏移,语义错。

报告契约本是 UTC-aware,但 `fmt_time` / `_fmt_dt` 是**公共渲染入口**,对 naive 输入按 **UTC 归一**最稳:

```python
def _to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)   # 报告时间语义恒为 UTC
    return value.astimezone()                # 无参 = 系统本地 TZ
```

这不是「给不可能分支兜底」——naive datetime 是渲染入口真实可能收到的输入域(测试、未来调用方),按契约语义(UTC)归一是正确处理、非防御性 fallback。

## 决策 3:测试必须 pin TZ(否则 CI/本地渲不同串)

`astimezone()` 无参依赖**进程的系统本地 TZ**。CI runner 多为 UTC、开发机可能 CST——同一 UTC 输入会渲出不同钟点串。故**任何断言渲染时间串的测试必须显式 pin TZ**:

- 用 `monkeypatch.setenv("TZ", "Asia/Shanghai")` + `time.tzset()`(POSIX 生效),或注入固定 `ZoneInfo` 做转换断言。
- 既有用 naive datetime 的渲染测试(`test_render_markdown_meta.py` / `test_render_json.py`)改用 **aware-UTC** 输入 + 按 pinned TZ 重算期望串(如 UTC `12:00` + `Asia/Shanghai` → `20:00`)。
- 加一条**时区转换正确性**测试:固定 UTC 输入 + pinned 非 UTC TZ,断言渲染串确实是转换后的本地钟点(不是 UTC 原值)——锚住「真的转了」,防回归成 `strftime` 直出 UTC。

不 pin TZ 的渲染断言是 flaky 源(本地绿 CI 红或反之),review 须把这条当硬约束。

## 决策 4:范围只到「人类可读报告」,日志与协议字段不动

- **structlog 日志**(daemon / inspector 的 JSON 行)时间戳保持 UTC(`...Z`):日志跨机器/跨服务关联对账,UTC 是对的,本地化反而破坏可对账性。
- **飞书 HMAC 签名**的秒级 epoch timestamp 是协议字段(notifier-lark 既有需求),与显示无关、不动。
- **JSON 报告**(`render_json` / persisted `report_json`)的 datetime 字段:本提案**只改 markdown / 通知卡片等给人看的渲染**;持久化 JSON 的 datetime 保持 isoformat-UTC(机器消费 + diff/baseline 依赖)。`reports show --format json` 输出的是机器格式,保持 UTC(若未来要本地化 json 显示另议)。— 实现时确认 `render_json` 不经 `_fmt_dt` 的本地化路径(它是 JSON 序列化、非人类渲染)。

## 测试策略

- `fmt_time`:pinned TZ 下,UTC-aware 输入 → 本地钟点串;naive 输入按 UTC 归一 → 同本地串;一条「转换真的发生」锚(UTC≠本地 TZ 下输出≠UTC 原钟点)。
- `render_markdown._fmt_dt`(started_at/finished_at):pinned TZ,aware-UTC → 本地;更新既有 `test_render_markdown_meta` 期望串。
- `cli/reports.py` reports list 行:pinned TZ 下行时间为本地。
- `render_json` 回归:JSON 输出的 datetime **仍 UTC**(不被本地化误伤)。
- 全量 `pytest` + `mypy --strict` + `ruff` 绿(`UTC` 从 `datetime` import,注意 py3.11+ `datetime.UTC`)。
