<!-- 本 delta 仅 MODIFIED 既有 inspect-cli-command spec 中与 `--intent` 相关的 3 个需求块；其余需求块（选项/位置参数清单、--inspector 单 Inspector 管线、--parameters 解析、schema_version、集成测试等）不在本提案范围、保持不变。 -->

## 修改需求

### 需求:`hostlens inspect --intent` 必须装配并运行 PlannerAgent，实时进度走 stderr、报告走 stdout

`--intent` 路径必须用 `create_backend(settings)` + 注册了默认工具的 `ToolRegistry` + 产出含 target/inspector registry 的 `ToolContext` 的 context_factory 装配 `PlannerAgent`，并以一个 CLI 端 observer（实现 `LoopObserver` Protocol）调用 `PlannerAgent.run(intent, observer=...)`。Planner 返回后，编排层必须给 findings 盖稳定 id（见 diagnostician-agent 能力），再装配并运行 `DiagnosticianAgent`（复用同一 backend + 受限诊断师注册表 + 固定 target；backend 仍只注入 loop），同样以 CLI 端 observer 透传实时进度。`create_backend` 必须**只调用一次**，Diagnostician 复用 Planner 的同一 backend 实例 —— 不存在二次配置失败点（backend 未配置只在 Planner 装配前发生一次）。盖章与诊断使用的 `InspectorRegistry` 必须与 Planner 跑时同一实例。backend 禁止进入任一 context_factory 产出的 `ToolContext`（ADR-008）。

实时进度（Agent 逐轮的工具调用与每轮返回的 assistant 文本，**非** token 级流式）必须渲染到 **stderr**；Planner 与 Diagnostician 两段进度都必须走 stderr。最终报告必须输出到 **stdout**（或 `--output` 指定文件）。二者必须分离，使脚本消费 stdout 时不被进度输出污染。`--intent` 字符串只能作为模型的 user message，禁止进入任何 shell/命令渲染路径。CLI 边界必须把任何未预期异常（含从 loop 透传上来的非可重试 backend 错误，如 `CassetteMiss`；含盖章时 inspector 已卸载的 fail-loud）包成一行 `internal: <kind>: <msg>` → exit 2，不泄露 Python traceback。

#### 场景:实时进度与报告分流

- **当** `--intent` 运行且 Planner 与 Diagnostician 各调用了若干工具
- **那么** stderr 必须出现两段逐轮/逐工具的实时进度，stdout 必须只含最终报告内容

#### 场景:backend 未配置报配置错误

- **当** `--intent` 运行但 backend 未配置（如缺 `ANTHROPIC_API_KEY`，`create_backend` 抛 `ConfigError`）
- **那么** 必须 exit 3 并在 stderr 给出一行配置错误提示（指向 `hostlens doctor`），不泄露 traceback

### 需求:`hostlens inspect --intent` 必须输出 narrative + findings 摘要 + 根因假设 + 遥测，支持 md/json

`--intent` 路径必须按 `--format` 渲染 `DiagnosticianResult`：md 模式输出诊断 narrative（markdown；**降级路径下 narrative 可能为空字符串，渲染必须容忍空 narrative —— 不报错、不渲染空标题**）+ `## Findings` 摘要（severity / message / tags，来自 `DiagnosticianResult.findings` 顶层 canonical 集合）+ **`## 根因假设` 章节**（每条含 description / confidence / 关联的 finding 证据 / suggested_actions；hypotheses 为空时显示 `_暂无根因假设_` 占位）+ 一行 loop 遥测（turns / terminal_status / token usage）；json 模式输出 `DiagnosticianResult` 的 JSON 序列化（含 narrative / findings(顶层带 id，权威) / hypotheses / status / planner_result(其内嵌 findings 为未盖章原件、非权威) / diagnostician_loop(可能为 null)）。**禁止**组装 `reporting.models.Report`（本提案 Scope-Core，不产忠实 Report）。findings 为空时 md 模式只输出 narrative + 根因假设占位 + 遥测，不报错。

#### 场景:md 模式输出综述、findings 摘要与根因假设

- **当** `--intent --format md` 且诊断师产出了若干根因假设
- **那么** stdout 必须含诊断 narrative、findings 摘要、`## 根因假设` 章节（每条含证据与建议动作），并附 terminal_status / token usage 遥测行

#### 场景:无根因假设时显示占位

- **当** `--intent --format md` 但诊断师未产出任何根因假设
- **那么** stdout 的 `## 根因假设` 章节必须显示 `_暂无根因假设_` 占位，其余内容正常输出，不报错

#### 场景:降级致 narrative 为空时渲染容忍

- **当** `--intent --format md` 但诊断（或 Planner）降级使 `DiagnosticianResult.narrative` 为空字符串
- **那么** md 渲染必须不报错、不输出空的 narrative 标题，仍输出 findings 摘要 + 根因假设占位 + 遥测行

#### 场景:json 模式输出可解析的 DiagnosticianResult

- **当** `--intent --format json`
- **那么** stdout 必须是 `DiagnosticianResult` 的合法 JSON（含 narrative / findings / hypotheses / status / planner_result / diagnostician_loop），可被 `DiagnosticianResult.model_validate_json` 往返解析；下游必须以顶层 `findings` 为权威

### 需求:`hostlens inspect --intent` 退出码沿用 4 值语义并由 DiagnosticianResult 映射

`--intent` 路径必须按 `DiagnosticianResult` 映射退出码（与 `--inspector` 路径同一 4 值语义，优先级 3>2>1>0）：`status=ok` 且无 critical finding → `0`；`status=ok` 且 ≥1 `severity=="critical"` finding → `1`；`status` ∈ 降级集合（`degraded_max_turns` / `degraded_token_budget` / `degraded_no_planner` / `degraded_rate_limited` / `empty_response`，无论该值来自 Planner 降级还是 reconcile）→ `2`；参数互斥违规 / backend 配置错误 / `--output` 写失败 / `--format` 非法 → `3`。（注：`failed_api_unavailable` **不在** `status` 降级集合内 —— `DiagnosticianResult.status` 类型是 `ReportStatus`，故意不含该值；它只经下面的 no-result 特例处理。）**Planner `terminal_status=failed_api_unavailable` 的特例**：不产 `DiagnosticianResult`，CLI 必须走 no-result 降级路径 —— stderr 给出一行降级原因、exit `2`、stdout 为空（无 findings 可输出，禁止伪造空报告骨架）。Planner 或 Diagnostician 降级时 CLI 禁止重试（重试单一收口在 loop），有 `DiagnosticianResult` 时仍输出已收集的 findings、（可能为空的）hypotheses 与（可能为空的）narrative。

**消费约定**：脚本消费方判定成功**必须看退出码（0/1）**，**禁止**用「stdout 是否为空」判断 —— no-result 路径 stdout 空 + exit 2，而健康巡检也可能 findings 空但有 narrative/占位（stdout 非空）+ exit 0，二者 stdout 空/非空与成败不构成对应。

**实现约束（不破坏 demo）**：`--intent` 路径的退出码映射与渲染必须由**新增的** DiagnosticianResult 版函数（`_compute_diag_exit_code` / `render_diagnostician_result`）承担；既有 `_compute_intent_exit_code(PlannerResult)` / `render_planner_result` 被 `cli/demo.py` 的 `demo run` 共享，**禁止**改其签名（`--inspector` 路径与 `demo run` 行为不变）。

#### 场景:健康巡检退出 0

- **当** `--intent` 运行结果 `status=ok` 且无 critical finding
- **那么** 必须 exit 0

#### 场景:critical finding 退出 1

- **当** `status=ok` 且收集到至少一条 `severity=="critical"` 的 finding
- **那么** 必须 exit 1

#### 场景:诊断师空响应 empty_response 退出 2

- **当** `DiagnosticianResult.status=empty_response`（诊断师空响应，区别于 end_turn 带文本无假设的 `ok`）
- **那么** 必须 exit 2，stdout 仍输出 findings + 根因假设占位 +（可能为空的）narrative

#### 场景:reconcile 产生的 degraded_no_planner 退出 2

- **当** `DiagnosticianResult.status=degraded_no_planner`（来自 Planner=ok + 诊断师调工具前 API 不可达的 reconcile）
- **那么** 必须 exit 2，stdout 仍输出 Planner 已收集的 findings + 根因假设占位

#### 场景:降级退出 2 且仍输出部分结果

- **当** `status` 为 `degraded_max_turns` / `degraded_token_budget` 等且存在 `DiagnosticianResult`
- **那么** 必须 exit 2，stderr 标注降级原因，stdout 仍输出已收集的 findings、（可能为空的）hypotheses 与（可能为空的）narrative，CLI 未重试

#### 场景:Planner API 不可达无结果退出 2

- **当** Planner `terminal_status=failed_api_unavailable`，不产 `DiagnosticianResult`
- **那么** 必须 exit 2，stderr 给出一行降级原因，stdout 为空（不伪造空报告骨架），CLI 未重试
