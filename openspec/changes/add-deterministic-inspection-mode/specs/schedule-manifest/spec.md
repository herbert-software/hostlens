## 修改需求

### 需求:`ScheduleManifest` 必须是 Pydantic v2 强类型 manifest

`hostlens.scheduler.schema.ScheduleManifest` 必须是 `extra="forbid"` 的 Pydantic v2 模型，字段含 `name` / `schedule`（cron / interval 二选一 + timezone）/ `targets: list[str]`（非空，每个是 `TargetRegistry` 已注册 id）/ `intent: str`（非空）/ `inspectors: list[str] | None` / `report: ReportConfig` / `notify: list[NotifyConfig]`，并新增:

- **`mode: Literal["agent", "deterministic"] = "agent"`**（默认 `agent` → **向后兼容**:无 `mode` 字段的既有 manifest 解析为 `agent`、行为零变）。`mode` 决定 job 体路由与 `targets` / `inspectors` 的语义（见下）。
- **`targets` 基数按 `mode`**:`agent` 模式恰好 1 个成员;`deterministic` 模式 ≥1 个成员（见「manifest target 基数按 mode 决定」需求）。
- **`inspectors` 语义按 `mode`**:`agent` 模式 `inspectors` 仍是**不被消费的 soft hint**（Planner 自主选）;`deterministic` 模式 `inspectors`（若提供）是**权威集**、不提供则用内置默认健康集（见 `deterministic-inspection-mode` 能力）。
- `report` / `notify` 的既有语义不变。

#### 场景:无 mode 字段的既有 manifest 默认 agent
- **当** 用一个不含 `mode` 的合法 dict 构造 `ScheduleManifest`
- **那么** 必须成功解析、`manifest.mode == "agent"`，行为与变更前一致

#### 场景:deterministic 模式可声明多 target
- **当** `mode: deterministic` 且 `targets: [a, b, c]`（均已注册）
- **那么** 必须成功解析（不因多 target 被拒）

## 重命名需求

- FROM: `### 需求:M4 每个 manifest 必须恰好一个 target（多 target fan-out 为非目标）`
- TO: `### 需求:manifest 的 target 基数必须按 mode 决定（agent 单 target，deterministic 多 target）`

## 修改需求

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
