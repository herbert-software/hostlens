## 重命名需求

- FROM: `### 需求:job 执行必须复用诊断 pipeline 并按结果映射 RunStatus`
- TO: `### 需求:job 执行必须按 mode 路由（agent 复用诊断 pipeline / deterministic 走确定性采集）并按结果映射 RunStatus`

## 修改需求

### 需求:job 执行必须按 mode 路由（agent 复用诊断 pipeline / deterministic 走确定性采集）并按结果映射 RunStatus

job 执行体**必须**按 `manifest.mode` 路由，两条路径都产出 `Report | None` 并按**同一套** RunStatus 映射落 Run:

**`mode == "agent"`（不变）**:调用交付层无关的编排函数 `run_diagnosis_pipeline`（Planner→Diagnostician→`Report | None`），注入既有 `planner_result_sink` 捕获 `terminal_status` 判别 `None` 原因（后端不可用 vs 空采集）。

**`mode == "deterministic"`（新增）**:调用 `run_deterministic_inspection`（见 `deterministic-inspection-mode` 能力):逐 target 跑固定 inspector 集（不走 Planner、不注入 LLMBackend 到采集阶段）→ 组装多 target `Report` → narrate-only Diagnostician 写根因。返回 `Report`（采集到 ≥1 个 inspector 结果）或 `None`（全部 target × inspector 均无结果可组装）。

**共享映射规则**（两 mode 一致）:

- 返回 `Report` 且 `meta.status == ok` → `ReportStore.save` 后落 `Run(status=ok, report_id=<saved>, report_hash)`
- 返回 `Report` 且 `meta.status` 为降级类——既有显式枚举**逐字保留不削**：`partial` / `degraded_no_planner` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `empty_response` / `stored_as_orphan`——→ `Run(status=partial, report_id=<saved>, report_hash)`;**token/turns 预算耗尽（仅 agent 可触发）产 `degraded_token_budget`/`degraded_max_turns` 的 Report 仍映射 `partial`、禁止映射无-Report 的 `budget_exhausted`**
- agent 返回 `None` 且 sink `terminal_status == "failed_api_unavailable"` → `Run(status=failed_api_unavailable, report_id=None)`
- agent 返回 `None` 且非上述（空采集）→ `Run(status=failed, error="pipeline produced no inspector results", report_id=None)`
- **deterministic 返回 `None`（全 inspector 无结果）→ `Run(status=failed, error="deterministic inspection produced no inspector results", report_id=None)`**;deterministic **不经** LLM 采集，故**不产** `failed_api_unavailable`（narrate 阶段后端不可用按 `degraded` Report 处理、不丢已采集结果）

Report 持久化、orphan 边界、`report_storage` 字段语义不变（复用 `ReportStore`）。

#### 场景:agent 模式行为不变
- **当** 一个 `mode: agent`（或省略 mode）manifest 触发、pipeline 正常产 ok Report 入库
- **那么** 行为与变更前**完全一致**:`run_diagnosis_pipeline` + sink + 落 `Run(status=ok, report_id=<saved>, report_storage="db")`

#### 场景:deterministic 模式逐 target 跑固定集产多 target Report
- **当** 一个 `mode: deterministic`、`targets: [a, b]` 的 manifest 触发
- **那么** **必须**走 `run_deterministic_inspection`（不实例化 Planner、采集阶段不注入 LLMBackend），逐 target 跑固定集、组装一份含 a/b 的 Report、narrate-only 写根因，并按共享规则落 `Run`（ok/partial）

#### 场景:deterministic 全无结果落 failed
- **当** deterministic 模式下全部 target × inspector 均无可组装结果
- **那么** **必须**落 `Run(status=failed, error="deterministic inspection produced no inspector results", report_id=None)`,**禁止**误记为 `failed_api_unavailable`
