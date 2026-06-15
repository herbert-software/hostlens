## 为什么

巡检报告里的时间戳渲染成 **UTC**，不是运行 hostlens 的宿主机本地时间。真机 ts.mac-mini（Asia/Shanghai / CST +0800）实测：报告抬头/覆盖行显示 `08:55`，而宿主机实际时钟是 `16:55`——差 8 小时。运维看每日推送（Telegram / 飞书）的「巡检时间」对不上本地时钟，易误判巡检新鲜度。

根因纯在**渲染层**：报告的 `started_at` / `finished_at` / `meta.timestamp` 由 `datetime.now(UTC)` 产生（**存储用 UTC 是对的**——可比较、可跨区、NTP 回拨容忍），但人类可读渲染**没做时区转换**直接 `strftime` / `isoformat`，于是打印的是 UTC 钟点：

- `notifiers/_filters.py:fmt_time` → `value.strftime("%Y-%m-%d %H:%M")`（Telegram `report.md.j2` + 飞书 `report.card.j2` 的报告抬头时间都经它）
- `reporting/render_markdown.py:_fmt_dt` → `dt.isoformat()`（`reports show` 的 `started_at`/`finished_at`）
- `cli/reports.py` → `row.timestamp.isoformat()`（`reports list` 的行时间）

## 变更内容

**纯渲染层改动，存储不变**：三处人类可读时间渲染在格式化前把 UTC-aware datetime 转成**宿主机系统本地时区**（`value.astimezone()`，无参 = 系统本地 TZ）。

1. `fmt_time`（Telegram + 飞书报告时间，用户每日主视图）→ 渲染本地时区。
2. `render_markdown._fmt_dt`（`reports show` 的 started_at / finished_at）→ 渲染本地时区。
3. `cli/reports.py` 的 `reports list` 行时间 → 渲染本地时区。

**naive datetime 归一**：渲染函数收到 naive datetime 时**先按 UTC 解释**（`value.replace(tzinfo=UTC)`）再 `astimezone()`——报告契约本是 UTC-aware，但渲染器作为公共入口对 naive 输入按 UTC 归一，避免 `astimezone()` 对 naive 默认「当本地」造成错误偏移。

## 非目标（Non-Goals）

- **不改存储**：`started_at` / `finished_at` / `meta.timestamp` 仍以 UTC-aware 持久化（baseline 排序、diff、跨区比较都依赖 UTC，见 report-persistence 的总序 `(timestamp DESC, rowid DESC)`）。只改渲染。
- **不引入可配置 display 时区**：用宿主机系统 TZ（`astimezone()` 无参），不新增 `report.timezone` 配置面、不复用 `schedule.timezone`（那是 cron 触发时区，非显示时区）。跨区统一显示是后续提案。
- **不改 structlog 日志时间戳**：daemon / inspector 的 JSON 日志时间戳仍 UTC（`...Z`）——日志要跨机器/跨服务关联对账，UTC 是对的，只有**人类可读报告**改本地。
- **不取被巡检服务器的时间**：报告时间是「这次巡检在宿主机上跑的时刻」（单一时点），非 6 台被巡检机各自的本地时间。
- **不改飞书 HMAC 签名的秒级 epoch timestamp**（那是协议字段、与显示无关）。

## 影响

- **契约**：`notifier-telegram` / `notifier-lark` 各新增一条「报告时间用宿主机本地时区渲染」需求（既有「覆盖行含时间」不变、只追加时区语义）。
- **代码**：`notifiers/_filters.py`（fmt_time）、`reporting/render_markdown.py`（_fmt_dt）、`cli/reports.py`（reports list 行）。
- **测试确定性（关键）**：本地时区渲染**依赖测试运行环境的 TZ**（CI=UTC vs 本地=CST 会渲不同串）。所有断言渲染时间串的测试**必须 pin TZ**（设 `TZ` env + `time.tzset()`，或注入固定 tz）才能跨 CI / 本地稳定；既有用 naive datetime 的渲染测试需改用 aware-UTC + 按 pinned TZ 重算期望串。
- **向后兼容**：渲染输出的钟点变了（UTC→本地），但这正是修复目标；存储与所有下游（diff / baseline / persisted JSON）零变化。
