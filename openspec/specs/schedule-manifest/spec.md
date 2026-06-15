# schedule-manifest 规范

## 目的

定义调度任务 manifest 契约——`ScheduleManifest` 为 Pydantic v2 强类型 manifest、`schedule` 为 cron / interval 二选一并携带 timezone、加载器扫描 `schedules/*.yaml` 并在加载时 fail-loud 校验、M4 每 manifest 恰好一个 target(多 target fan-out 为非目标)、`notify` 配置在 M5 被消费用于路由发送。
## 需求
### 需求:`ScheduleManifest` 必须是 Pydantic v2 强类型 manifest

`hostlens.scheduler.schema.ScheduleManifest` 必须继承 `pydantic.BaseModel`，字段集为：

- `name: str`（job 唯一标识，必须为合法 APScheduler job_id：非空、不含路径分隔符）
- `schedule: ScheduleSpec`（cron / interval 判别结构，见下）
- `targets: list[str]`（非空；每个是 `TargetRegistry` 中已注册的 target id；**M4 加载器校验恰 1 个成员**——多 target fan-out 为非目标，见下）
- `intent: str`（非空；Planner Agent 据此规划）
- `inspectors: list[str] | None = None`（可选 hint）。**M4 解析并保留、但 scheduler job 体不消费它**——M4 的 job 体只把 `intent` 传给 `run_diagnosis_pipeline`，Planner 按 Agent-loop 设计（CLAUDE.md §4.2）**自主**从 Inspector registry 选 inspector；把 hint 注入 Planner 上下文/提示词留后续里程碑（与 `format`/`diff_with_last`/`notify` 同属「parse 早、消费在 M4 之外」）。设置该 hint 在 M4 不改变巡检行为。
- `report: ReportConfig = ReportConfig()`（缺省整体可省略）。`ReportConfig` 字段精确定义、与既有 `inspect`/`reports` CLI 的格式字面量**对齐**：`format: Literal["md", "json"] = "md"`（**禁止** `markdown`/`html` 等其它字面量——既有 `--format` 是 `Literal["md","json"]`，见 inspect-cli-command spec / `reports.py:144`）。`format` 在 M4 **被解析并保留、但 scheduler job 体不消费它**——job 体持久化的是 format-agnostic 的**结构化 Report**，`format` 在**渲染时点**应用（`hostlens reports show --format` / M5 notify 渲染），与 `diff_with_last` 同属「parse 早、用在 M4 之外」。`diff_with_last: bool = False`（**M4 仅解析为类型化字段、不消费**——与 `notify` 同属占位）。**M4 的 regression diff 仍是 persisted reports 上的 post-hoc 操作**（既有 `hostlens reports diff` / `compute_diff`），**不**在报告组装期自动 diff、**不**把 diff 嵌进巡检报告 section（`Report.from_inspector_results` 不填 `baseline_ref`；自动 diff / 嵌入报告是 M5 非目标）。`diff_with_last` 为 M5 的 auto-diff 预留语义、M4 不据它改变任何行为。`ReportConfig` 也 `extra="forbid"`。
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

### 需求:`notify` 配置在 M5 被消费用于路由发送

`ScheduleManifest.notify` 必须解析为类型化结构（每条含 `channel: str` 与可选 `only_if: str`），且 M5 的加载与调度路径**必须**消费它。**校验分两个时机，互不耦合**：

- **manifest 加载期**（`schedule list` / `run` / `daemon` / `trigger` 共同的纯加载路径，**不依赖 `notifiers.yaml`**）：每条 `only_if`（若提供）必须经 `hostlens.inspectors.dsl.validate_ast` 校验语法/AST，非法表达式 fail-loud；空串 `only_if` 同样 fail-loud。此阶段**不**校验 channel 是否存在（`schedule list` 不应被迫读 `notifiers.yaml`）。
- **调度装配期**（实际要派发 notify 的路径：`daemon` / `run` / `trigger` 注入 `channel_registry` + 加载 `notifiers.yaml` 时）：每个 `notify.channel` 必须能解析到一个已注册通道实例，否则 fail-loud（指出未知 channel，拼写错的 channel 名不得静默忽略）。runner 因此需注入通道配置依赖（`channel_registry` / 已加载通道集），与 `TargetRegistry` 注入同列。

调度触发产出 Report 后，runner 必须按 `only_if` 路由把（已脱敏的）报告发送到对应通道，并把每通道 `NotifyResult` 写入 `Run.notify_results`。secret 仍只经 `${ENV_VAR}` 注入、不入 manifest 明文。`NotifyConfig` 必须 `model_config = ConfigDict(extra="forbid")`（M5 收紧、替换 M4 的 `extra="allow"`）：notify 子字段出现未声明 key（如拼错 `only_iff`）必须 `ValidationError` fail-loud，与 manifest 其它模型（`ScheduleManifest` / `ReportConfig`）的 fail-loud 基调一致；M5 的合法字段恰为 `channel` + 可选 `only_if`，未来新增字段须经后续 OpenSpec 提案显式扩展。**已知可接受弱化**：M4 的 `NotifyConfig` 为 `extra="allow"`，故 M4 用户若在 `schedules/*.yaml` 的 `notify` 写过额外 key，M5 收紧后这些既有 manifest 会 `ValidationError`。此弱化可接受——M4 的 `notify` 是**显式声明「解析但不消费」的占位**（无任何行为依赖它），收紧成 `extra="forbid"` 正是要让这类多余/拼写 key fail-loud（错误信息会指出未知 key，用户删除即可）；不提供静默兼容（静默吞 key 与 fail-loud 基调矛盾），不写迁移脚本（占位字段无语义可迁移）。区别于 scheduler-engine 的 F15（那是 runs.db `notify_results` 反序列化，作用对象是 store；此处作用对象是用户手写 manifest）。

#### 场景:带 notify 的 manifest 触发后产生 notify_results

- **当** manifest 含 `notify: [{channel: ops-telegram, only_if: "severity >= warning"}]`，`ops-telegram` 已在 `notifiers.yaml` 配置，执行加载与一次产出 Report 的调度触发
- **那么** manifest 必须正常加载；`only_if` 求值为真时该通道实际发送且 `Run.notify_results` 含对应 `NotifyResult(status="sent")`；求值为假时记 `NotifyResult(status="skipped")`

#### 场景:引用未配置通道在装配期 fail-loud

- **当** manifest 的 `notify` 引用 `notifiers.yaml` 中不存在的 channel 名，执行 `daemon` / `run` / `trigger`（注入 channel_registry 的装配路径）
- **那么** 装配期必须 raise（指出未知 channel），禁止静默跳过该通道

#### 场景:schedule list 不因 notify 引用而要求 notifiers.yaml

- **当** manifest 含 `notify`、`notifiers.yaml` 不存在，执行 `hostlens schedule list`
- **那么** 列表必须正常加载（仅做 `only_if` 语法校验），**不**因 channel 未配置或缺 `notifiers.yaml` 而失败

#### 场景:非法 only_if 在加载期拒绝

- **当** manifest 的 `only_if` 含被 AST 闸门拒绝的构造，或为空串
- **那么** manifest 加载期必须 raise，禁止留到运行期

### 需求:manifest 的 target 基数必须按 mode 决定（agent 单 target，deterministic 多 target）

`ScheduleManifest.targets` 是 `list[str]`。加载器**必须**按 `mode` 校验其成员数:

- **`mode == "agent"`**:**必须恰好 1 个成员**——多于 1 个时 fail-loud 拒绝加载。理由不变:agent 复用的 `run_diagnosis_pipeline` 是单 target 编排（`report_target_name` / `target_lookup_name` 单值），多 target fan-out 的 Run/Report 基数对 agent 仍是非目标。
- **`mode == "deterministic"`**:**允许 ≥1 个成员**——deterministic 模式逐 target 跑固定集、组装一份多 target 报告（见 `deterministic-inspection-mode` 能力），多 target 是其核心用途。

任一成员未在 `TargetRegistry` 注册时仍 fail-loud（不变）。

#### 场景:agent 模式多 target 仍 fail-loud
- **当** `mode: agent`（或省略 mode）的 manifest `targets` 含 2 个或更多 id（即便都已注册），执行加载
- **那么** 加载**必须** fail-loud raise，错误指出该文件 + "agent 模式仅支持单 target"语义

#### 场景:deterministic 模式多 target 正常加载
- **当** `mode: deterministic` 的 manifest `targets` 含 ≥1 个均已注册的 id，执行加载
- **那么** **必须**正常加载（不因多 target 被拒）

#### 场景:单 target manifest 在两种 mode 均正常加载
- **当** 任一 mode 的 manifest `targets` 恰好 1 个已注册成员
- **那么** **必须**正常加载
