## 新增需求

### 需求:`ScheduleManifest` 必须是 Pydantic v2 强类型 manifest

`hostlens.scheduler.schema.ScheduleManifest` 必须继承 `pydantic.BaseModel`，字段集为：

- `name: str`（job 唯一标识，必须为合法 APScheduler job_id：非空、不含路径分隔符）
- `schedule: ScheduleSpec`（cron / interval 判别结构，见下）
- `targets: list[str]`（非空；每个是 `TargetRegistry` 中已注册的 target id；**M4 加载器校验恰 1 个成员**——多 target fan-out 为非目标，见下）
- `intent: str`（非空；Planner Agent 据此规划）
- `inspectors: list[str] | None = None`（可选 hint；Planner 优先考虑但可按需补查）
- `report: ReportConfig = ReportConfig()`（缺省整体可省略）。`ReportConfig` 字段精确定义、与既有 `inspect`/`reports` CLI 的格式字面量**对齐**：`format: Literal["md", "json"] = "md"`（**禁止** `markdown`/`html` 等其它字面量——既有 `--format` 是 `Literal["md","json"]`，见 inspect-cli-command spec / `reports.py:144`）；`diff_with_last: bool = False`（**M4 仅解析为类型化字段、不消费**——与 `notify` 同属占位）。**M4 的 regression diff 仍是 persisted reports 上的 post-hoc 操作**（既有 `hostlens reports diff` / `compute_diff`），**不**在报告组装期自动 diff、**不**把 diff 嵌进巡检报告 section（`Report.from_inspector_results` 不填 `baseline_ref`；自动 diff / 嵌入报告是 M5 非目标）。`diff_with_last` 为 M5 的 auto-diff 预留语义、M4 不据它改变任何行为。`ReportConfig` 也 `extra="forbid"`。
- `notify: list[NotifyConfig] = []`（**占位**：解析为类型化字段，M4 不消费、不验其中 secret）

`ScheduleManifest` 必须 `model_config = ConfigDict(extra="forbid")`：manifest 出现未知顶层字段必须 `ValidationError`（拼写错误 fail-loud，不静默吞）。

#### 场景:合法 manifest 解析为 ScheduleManifest

- **当** 用含 `name` / `schedule.interval` / `targets` / `intent` 的合法 dict 构造 `ScheduleManifest`
- **那么** 必须成功解析；`manifest.name` / `manifest.targets` / `manifest.intent` 取到原值；`manifest.notify == []`

#### 场景:未知顶层字段拒绝

- **当** manifest dict 含未声明字段（如 `scheduel: ...` 拼写错误）
- **那么** 必须 raise `pydantic.ValidationError`（`extra="forbid"`），错误指出该未知字段

#### 场景:report.format 限 md/json，非法字面量拒绝

- **当** manifest `report: {format: markdown}`（非 `md`/`json`）
- **那么** 必须 raise `ValidationError`（`format` 是 `Literal["md","json"]`），错误指出非法字面量

#### 场景:diff_with_last 仅解析不消费

- **当** manifest `report: {diff_with_last: true}`，执行加载与一次调度触发
- **那么** manifest 必须正常解析（`report.diff_with_last == True`）；调度产出的 Report **不**因此在组装期自动 diff、**不**嵌入 diff section（M4 不消费该字段，与 notify 占位同理）；regression diff 仍只能事后用 `hostlens reports diff` 取得

### 需求:`schedule` 必须是 cron / interval 二选一并携带 timezone

`ScheduleManifest.schedule`（`ScheduleSpec`）必须恰好提供 `cron: str`（**标准 5 段 crontab 表达式**：minute hour day-of-month month day-of-week；秒级 cron 非目标，因 APScheduler `CronTrigger.from_crontab()` 仅支持 5 段——秒级精度用 interval 表达）**或** `interval: IntervalSpec`（`weeks?` / `days?` / `hours?` / `minutes?` / `seconds?` 至少一个为正）之一，**禁止同时提供、禁止都不提供**（`model_validator` 强制恰一个，违反即 `ValidationError`）。`schedule` 必须含 `timezone: str`，其值必须是 `zoneinfo` 可解析的时区名（如 `Asia/Shanghai`），非法时区 `ValidationError`。

#### 场景:cron 与 interval 同时提供被拒

- **当** `schedule` 同时含 `cron` 与 `interval`
- **那么** 必须 raise `ValidationError`（恰一个约束），错误说明二者互斥

#### 场景:cron 与 interval 都不提供被拒

- **当** `schedule` 既无 `cron` 也无 `interval`
- **那么** 必须 raise `ValidationError`（至少一个约束）

#### 场景:非法时区被拒

- **当** `schedule.timezone` 为 `Not/AZone`（`zoneinfo` 不可解析）
- **那么** 必须 raise `ValidationError`，错误指出时区非法

#### 场景:interval 全零/全省略被拒

- **当** `schedule.interval` 的所有字段均为 0 或全部省略（无任何正字段）
- **那么** 必须 raise `ValidationError`（「至少一个为正」约束），错误指出 interval 无有效周期

### 需求:加载器必须扫描 `schedules/*.yaml` 并在加载时 fail-loud 校验

`hostlens.scheduler.loader` 必须提供加载入口扫描 `schedules/` 目录下所有 `*.yaml`，逐个解析为 `ScheduleManifest` 并执行**加载时**语义校验：(a) 每个 `targets` 成员必须存在于注入的 `TargetRegistry`；(b) 所有 manifest 的 `name` 必须全局唯一（跨文件不重名）；(c) `intent` 必须非空白；(d) **`targets` 在 M4 必须恰好含 1 个成员**（见下「单 target」需求）。任一 manifest 非法时，加载必须 **fail-loud**——raise 一个错误，其消息含**出错文件名 + 字段 + 原因**，**禁止**静默跳过该文件、**禁止**把校验推迟到 job 触发时。

校验时机契约：`schedule list` / `schedule daemon` / `schedule trigger` 启动时即触发加载校验；非法 manifest 使这些命令在执行实际调度前就退出报错。

#### 场景:targets 引用不存在的 target

- **当** 某 manifest 的 `targets` 含一个未在 `TargetRegistry` 注册的 id，执行加载
- **那么** 加载必须 raise，错误含该文件名 + 该 target id + "未注册"语义；**禁止**静默跳过或等到触发时才失败

#### 场景:跨文件 name 重复

- **当** `schedules/` 下两个文件声明了相同的 `name`
- **那么** 加载必须 raise，错误指出重复的 name 与涉及的文件

#### 场景:合法目录全部加载成功

- **当** `schedules/` 下所有 manifest 均合法且 targets 都已注册
- **那么** 加载必须返回全部 `ScheduleManifest`（数量与文件数一致），无报错

### 需求:M4 每个 manifest 必须恰好一个 target（多 target fan-out 为非目标）

`ScheduleManifest.targets` 字段类型为 `list[str]`（为未来 fan-out 保留列表形态），但 **M4 加载器必须校验其恰好含 1 个成员**——多于 1 个时 fail-loud 拒绝加载。理由：复用的 `run_diagnosis_pipeline` 是**单 target** 编排（`report_target_name` / `target_lookup_name` 单值，`cli/_intent.py:454-455`），且 ARCHITECTURE §7「一次触发产 0 或 1 个 Report」隐含一 Run↔单 target。多 target fan-out（一次触发跑多 target → Run/Report 基数、状态聚合、report_id 生成规则）涉及未定的设计决策，**显式列为 M4 非目标**，留后续提案。`Run.targets` 在 M4 因此恒为 1 元素列表（字段保留 list 形态以便 fan-out 落地时不破 schema）。

#### 场景:多 target manifest 被拒

- **当** 某 manifest 的 `targets` 含 2 个或更多 target id（即便都已注册），执行加载
- **那么** 加载必须 fail-loud raise，错误指出该文件 + "M4 仅支持单 target，多 target fan-out 未实现"语义

#### 场景:单 target manifest 正常加载

- **当** 某 manifest 的 `targets` 恰含 1 个已注册 target id
- **那么** 必须正常加载，`manifest.targets` 为该 1 元素列表

### 需求:`notify` 配置在 M4 为惰性占位

`ScheduleManifest.notify` 必须能解析 M5 将使用的类型化结构（`channel` + `only_if` 等字段允许出现且通过校验），但 M4 的加载与调度路径**禁止**消费它：不评估 `only_if` 表达式、不解析其中的 `${ENV_VAR}` secret、不实例化任何 Notifier。其存在不得影响 manifest 加载成功与否（除字段类型本身的校验外）。

#### 场景:带 notify 的 manifest 正常加载且不触发发送

- **当** manifest 含 `notify: [{channel: x, only_if: "..."}]`，执行加载与一次调度触发
- **那么** manifest 必须正常加载；调度产出的 `Run.notify_results` 必须为空 `[]`；**禁止**有任何通知发送或 `only_if` 求值发生
