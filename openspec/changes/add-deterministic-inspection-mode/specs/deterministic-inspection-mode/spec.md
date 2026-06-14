## 新增需求

### 需求:deterministic 模式必须固定 inspector 集逐 target 直跑、不走 Planner、不漫游

`mode=deterministic` 的 job body **必须**对 `manifest.targets` 的**每个** target，跑解析出的固定 inspector 集（经 `InspectorRunner`，复用 `run_inspector` 的 target / inspector 解析 + capability 门），收集 `InspectorResult`。**禁止**实例化 Planner、**禁止**让 LLM 选 inspector 或 target;采集阶段**禁止**注入 `LLMBackend`（守 §4.2「Inspector 只采集不调 LLM」+ ADR-008）。每个 inspector 对**不满足 capability** 的 target **必须**记为 `skipped`（**非** `failed`，不计入报告 severity）。单 inspector 失败**必须**隔离进结果、**禁止**崩整批。`targets × inspectors` 的并发**必须**信号量限流。

#### 场景:固定集逐 target 跑、覆盖确定不漫游
- **当** `mode=deterministic`、`targets=[A, B, C]`、解析出的 inspector 集 = `{cpu, disk}`
- **那么** **必须**恰好对 A / B / C 各跑 `cpu` + `disk`（共 6 次）；**禁止**跑集外 inspector、**禁止**跑 `targets` 之外的 target

#### 场景:不适用项记 skipped 不污染 severity
- **当** 某 inspector 要求的 capability 在某 target 不满足
- **那么** 该项**必须**记为 `skipped`、**禁止**计入报告 severity 聚合，整批其余照常

### 需求:deterministic 模式的 inspector 集由内置健康默认集或 `manifest.inspectors` 权威决定

`mode=deterministic` 时:manifest **无** `inspectors:` → **必须**用内置默认健康集;manifest **有** `inspectors:` → **必须**用它作**权威集**（不再是 soft hint、不再叠加默认集）。内置默认健康集**必须**覆盖核心健康域（cpu / 内存 / 磁盘容量 / inode / 系统负载 / systemd 服务 / 近期错误日志 / 网络连通性），其成员**必须**全部存在于 inspector registry（加测试钉死,防 curated 集漂移）。

#### 场景:无 inspectors 用默认健康集
- **当** `mode=deterministic` 且 manifest 无 `inspectors:`
- **那么** **必须**跑内置默认健康集，按各 target capability 过滤

#### 场景:显式 inspectors 变权威集（不再 soft hint）
- **当** `mode=deterministic` 且 manifest `inspectors: [disk]`
- **那么** **必须**只跑 `disk`，**禁止**当 soft hint 忽略、**禁止**补默认集

### 需求:deterministic 模式 LLM 只对采集结果写根因叙述、不得追加巡检

采集完成后**必须**经一次 Diagnostician 仅产根因叙述:**只注册 `correlate_findings`（关联 / 根因假设）、禁止注册 `request_more_inspection`**（结构上让 LLM 无法再跑 inspector 或选 target）。LLM 输入为**已采集的固定结果**，输出为根因假设 / 处置建议并入 Report。`LLMBackend` 注入 `AgentLoop`（**非** `ToolContext`，守 ADR-008）。

#### 场景:narrate-only 不漫游
- **当** deterministic 采集完成、过 Diagnostician
- **那么** LLM **禁止**持有任何能再跑 inspector / 选 target 的工具;**只**产根因叙述并入 Report

### 需求:deterministic 多 target 必须聚合成一份报告、severity 全队聚合供路由

多 target 采集结果**必须**组装成**一份** Report:findings 跨全部 target、各自保留 target 上下文;`report_target_name` 为 fleet 标签。notify `only_if` 路由**必须**在**全队聚合 severity** 上判定（机制复用既有 `aggregate_severity`）。

#### 场景:一份全队报告 + 聚合路由
- **当** `targets=[A, B]` 各产 findings，某通道 `only_if: severity >= warning`
- **那么** **必须**组装一份含 A 与 B findings 的 Report;当 A∪B 聚合 severity ≥ warning 才发该通道
