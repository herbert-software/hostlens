## 新增需求

### 需求:deterministic 模式必须固定 inspector 集逐 target 直跑、不走 Planner、不漫游

`mode=deterministic` 的 job body **必须**对 `manifest.targets` 的**每个** target，跑解析出的固定 inspector 集（经 `InspectorRunner`，复用 `run_inspector` 的 target / inspector 解析 + capability 门），收集 `InspectorResult`。**禁止**实例化 Planner、**禁止**让 LLM 选 inspector 或 target;采集阶段**禁止**注入 `LLMBackend`（守 §4.2「Inspector 只采集不调 LLM」+ ADR-008）。每个 inspector 对**不满足 capability** 的 target **必须**当作跳过处理（**非** `failed`，不计入报告 severity）。**术语澄清**:这里「跳过」是 **severity 处理概念**——`InspectorStatus` 是闭集 5 值（`ok` / `timeout` / `target_unreachable` / `requires_unmet` / `exception`）,本提案**不新增** `skipped` 枚举值;capability 不匹配的真实状态仍是 `requires_unmet`,只是在 deterministic 的 severity / 降级派生处**当跳过处理**（见下「`requires_unmet` 必须视为预期跳过」需求）。单 inspector 失败**必须**隔离进结果、**禁止**崩整批。`targets × inspectors` 的并发**必须**信号量限流。

#### 场景:固定集逐 target 跑、覆盖确定不漫游
- **当** `mode=deterministic`、`targets=[A, B, C]`、解析出的 inspector 集 = `{cpu, disk}`
- **那么** **必须**恰好对 A / B / C 各跑 `cpu` + `disk`（共 6 次）；**禁止**跑集外 inspector、**禁止**跑 `targets` 之外的 target

#### 场景:不适用项当跳过处理不污染 severity
- **当** 某 inspector 要求的 capability 在某 target 不满足
- **那么** 该项的 `InspectorStatus` **保持** `requires_unmet`（**不新增** `skipped` 枚举值）,但在 deterministic 的 severity / 降级派生处**当跳过处理**——**禁止**计入报告 severity 聚合,整批其余照常

### 需求:deterministic 模式的 capability 不匹配（requires_unmet）必须视为预期跳过、不降级报告

deterministic 模式逐 target 跑**固定健康集**时,固定集中某 inspector 对某 target **capability 不匹配**（如 mysql inspector 跑在没装 mysql 的台,记 `requires_unmet`）是**跨异构机跑统一健康集的正常情形**,**必须**视为**预期跳过**:

- **禁止**计入 deterministic 报告的 severity 聚合（与「skipped 不污染 severity」一致）。
- **禁止**因 `requires_unmet` 把报告 `meta.status` 降级为 `partial`。

这是 deterministic 模式对既有 `from_inspector_results` 默认派生（§需求:`Report.from_inspector_results` 工厂方法……中「任一 `requires_unmet` → partial」的保守外推）的**蓄意覆盖**:agent 模式下 `requires_unmet` 罕见且值得标注为 partial;deterministic 模式下它是固定健康集跨异构机的**预期**结果,标注 partial 会让每份 fleet 报告恒为 partial、淹没真正的降级信号。**真正的降级信号**——`timeout` / `exception` / `target_unreachable`——**仍**按既有语义计入 severity / 降级判定(全 timeout、任一 target_unreachable / exception 仍派生 partial)。deterministic 组装路径**必须**在派生 report status 时把 `requires_unmet` 排除出降级触发集（显式传入 override status,或调用支持该语义的组装路径)。**派生真值表（防「永远传 ok」吞真失败）**:把 `requires_unmet` 视同 `ok` 后套用既有 `_derive_report_status` 语义——① 全 `ok`（含被视同 ok 的 `requires_unmet`）→ `ok`；② 非 ok 仅 `timeout` 且至少一个 `ok` → `ok`；③ **全** `timeout`、或任一 `target_unreachable` / `exception` → `partial`。**禁止**实现成无条件 `ok`（那会吞掉 ③ 的真降级,违反下「真正的失败仍降级」场景）。

#### 场景:requires_unmet 不降级 deterministic 报告
- **当** deterministic 固定健康集对某 target 含 `requires_unmet`（该台没装对应服务）,其余结果均 `ok`
- **那么** 报告 `meta.status` **必须**为 `ok`,**禁止**因 `requires_unmet` 降级为 `partial`,且该项**禁止**计入 severity 聚合

#### 场景:真正的失败仍降级
- **当** deterministic 采集中含任一 `timeout`（且非全部 ok）以外的真失败,如 `target_unreachable` 或 `exception`
- **那么** 报告**必须**按既有语义降级（`target_unreachable` / `exception` → `partial`）,**禁止**被 requires_unmet 豁免规则误吞

### 需求:deterministic 模式的 inspector 集由内置健康默认集或 `manifest.inspectors` 权威决定

`mode=deterministic` 时:manifest **无** `inspectors:` → **必须**用内置默认健康集;manifest **有** `inspectors:` → **必须**用它作**权威集**（不再是 soft hint、不再叠加默认集）。内置默认健康集**必须**覆盖核心健康域（cpu / 内存 / 磁盘容量 / inode / 系统负载 / systemd 服务 / 近期错误日志 / 网络连通性），其成员**必须**全部存在于 inspector registry（加测试钉死,防 curated 集漂移）。

#### 场景:无 inspectors 用默认健康集
- **当** `mode=deterministic` 且 manifest 无 `inspectors:`
- **那么** **必须**跑内置默认健康集，按各 target capability 过滤

#### 场景:显式 inspectors 变权威集（不再 soft hint）
- **当** `mode=deterministic` 且 manifest `inspectors: [disk]`
- **那么** **必须**只跑 `disk`，**禁止**当 soft hint 忽略、**禁止**补默认集

### 需求:deterministic 模式 LLM 只对采集结果写根因叙述、不得追加巡检

采集完成后**必须**经一次 Diagnostician 仅产根因叙述:**必须**经 `diagnostician-agent` 能力的 **narrate-only 装配路径**装配（**只注册 `correlate_findings`,禁止注册 `request_more_inspection` / `list_inspectors` / `list_targets`**,见 `diagnostician-agent` 能力 §需求:诊断师装配必须支持 narrate-only 变体），结构上让 LLM 无法再跑 inspector 或选 target。LLM 输入为**已采集的固定结果**，输出为根因假设 / 处置建议并入 Report。`LLMBackend` 注入 `AgentLoop`（**非** `ToolContext`，守 ADR-008）。

#### 场景:narrate-only 不漫游
- **当** deterministic 采集完成、过 Diagnostician
- **那么** LLM **禁止**持有任何能再跑 inspector / 选 target 的工具;**只**产根因叙述并入 Report

### 需求:deterministic 多 target 必须聚合成一份报告、severity 全队聚合供路由

多 target 采集结果**必须**经 `report-data-model` 能力的**多 target（fleet）组装路径**（见 `report-data-model` 能力 §需求:多 target Report 必须由确定性 fleet 组装路径产出）组装成**一份** Report:findings 跨全部 target、每条保留**来源** `target_name`;`Report.target_name` 为**确定性 fleet 标签**,`meta.target_id` 为**确定性 fleet id**（有序 target_id + `schedule_name` 派生,避免不同 fleet 撞 store key）。notify `only_if` 路由**必须**在**全队聚合 severity** 上判定（机制复用既有 `aggregate_severity`）。fleet Report 是 **notify 导向**,**不**支持 per-target regression diff（见 `report-data-model` 能力 §需求:fleet（多 target）Report 的 per-target regression diff 是非目标）。

#### 场景:一份全队报告 + 聚合路由
- **当** `targets=[A, B]` 各产 findings，某通道 `only_if: severity >= warning`
- **那么** **必须**经 fleet 组装路径组装一份含 A 与 B findings 的 Report（每条 finding 带来源 `target_name`、`Report.target_name` 为确定性 fleet 标签、`meta.target_id` 为确定性 fleet id);当 A∪B 聚合 severity ≥ warning 才发该通道
