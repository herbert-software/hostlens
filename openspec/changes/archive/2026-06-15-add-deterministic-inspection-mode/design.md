## 上下文

调度 job body（`scheduler/runner.py`）当前**只调** `run_diagnosis_pipeline`（agent）:Planner（LLM）经 `run_inspector(target_name=自选)` + `list_targets` 自主选 inspector **和 target**，受 `max_turns` / `token_budget` 封顶。实测覆盖非确定 + 常 `partial`。`manifest.inspectors` 仅 soft hint（`runner.py:405` 不消费）。

可复用的现成件:`InspectorRunner.run(manifest, target, parameters, ...)`（确定性跑一个 inspector）、`run_inspector_handler` 的 target+inspector 解析逻辑、`InspectorResult` 收集、`from_inspector_results`（结果 → Report）、Diagnostician 的 `correlate_findings`（只关联不巡检）、Report 持久化 + Notifier 派发 + 调度留痕。**deterministic 模式 = 用这些件拼一条不走 Planner 的路**。

## 目标 / 非目标

**目标:** 见 proposal —— 固定集逐 target 直跑、多 target、内置健康默认集、LLM narrate-only、多 target 报告。

**非目标:** 不改 agent 模式;不动 Notifier / 触发 / 留痕 / 优雅停机;不新增 inspector;不让 deterministic 成默认;不为 agent 放开多 target。

## 决策

1. **`mode: Literal["agent","deterministic"] = "agent"`**（manifest 字段，默认 agent → 向后兼容)。runner job body 按 mode 分叉:`agent` → 现有 `run_diagnosis_pipeline`(零改动);`deterministic` → 新 `run_deterministic_inspection`。

2. **多 target 仅 deterministic 放开**。loader 的 M4 单 target 校验改为:`mode=="agent"` → 仍恰好 1 个;`mode=="deterministic"` → ≥1 个。理由:agent 的 Planner 本就漫游、多 target 无意义;deterministic 才需要「逐台跑」。

3. **内置健康默认集 = 命名常量** `DEFAULT_HEALTH_INSPECTORS`(一组现有 inspector name)。解析规则:`mode=="deterministic"` 且 manifest **无** `inspectors:` → 用默认集;**有** `inspectors:` → 用它(权威,不再 soft hint)。每个 inspector 对每个 target 跑前过 **capability 门**(`requires_unmet`):不适用的**视为跳过**(`InspectorStatus` 仍是 `requires_unmet`、**不新增** `skipped` 枚举值,仅在 deterministic severity / 降级派生处当跳过——非 failed,不污染报告 severity)。`DEFAULT_HEALTH_INSPECTORS` 成员必须都在 registry —— 加测试钉死(防 curated 集漂移)。

4. **确定性采集路径** `run_deterministic_inspection(manifest, ...)`:
   - 对 `manifest.targets` 每个 target × 解析出的 inspector 集,经 `InspectorRunner.run` 跑(复用 `run_inspector_handler` 的 target/inspector 解析 + capability 门),收集 `InspectorResult`。
   - **信号量限流**(复用 probe_many 同款),`targets × inspectors` 并发有界;单项失败隔离进结果(不崩整批,对齐 inspector 错误边界)。
   - **无 AgentLoop、无 LLMBackend 注入采集阶段**(守 §4.2「Inspector 不调 LLM」+ ADR-008)。

5. **LLM narrate-only（B-2 修复）**:采集完 → fleet 组装路径组装 Report → 过一次 Diagnostician。诊断师**必须**经 `diagnostician-agent` 的 **narrate-only 装配路径**装配 —— **只注册 `correlate_findings`(复用既有 `_build_correlate_findings_spec` 工厂),禁止注册 `request_more_inspection` / `list_inspectors` / `list_targets`**,结构上让 LLM 拿不到再巡检 / 选 target 的工具。**B-2 修复点**:原设计只说「禁止注册 `request_more_inspection`」,但 `register_diagnostician_tools` 是**无条件**装三件(`correlate_findings` + `request_more_inspection` + `list_inspectors`),没有只装一件的入口;故必须**新增** narrate-only 装配路径(新函数或新参数)落到 `diagnostician-agent` spec,而非在 deterministic 路径里临时拆。LLM 输入 = 已采集的固定结果,输出 = 根因假设 + 处置建议。Backend 注入 `AgentLoop.__init__`(非 ToolContext,守 ADR-008)。

6. **多 target 报告（B-3 修复）**:采集结果按 `InspectorResult.target_name` 标记;经 `report-data-model` 的**多 target（fleet）组装路径**组装成**一份** Report,findings 跨全部 target、**每条盖来源 `Finding.target_name`**;`Report.target_name`(**真实字段**,非占位 `report_target_name`)取**确定性 fleet 标签**(有序 target 名 join,满足 `min_length=1`);`meta.target_id` 取**确定性 fleet id**(有序 target_id + `schedule_name` 派生,避免不同 fleet 撞 store key)。notify 路由的 `aggregate_severity` 对全部 findings 聚合 —— `only_if` 在全队聚合 severity 上判定(机制不变)。**B-3 修复点**:原设计的 `report_target_name` 是悬空措辞——`Report` 没有这个字段,只有 `target_name: str (min_length=1)` 单值;fleet 标注落到真实 `Report.target_name` + `meta.target_id`,并给 `Finding` 加 add-only `target_name` 来源字段(见决策 7),由 `report-data-model` MODIFY 落地,删除原「归入 scheduler-engine MODIFY 小让步」的悬空让步。

7. **`Finding.target_name` add-only + 多 target 组装 + fleet diff 非目标 + `compute_finding_id` 不变（F1 keystone,B-3 / B-4 修复）**。现 `Finding`(`extra=forbid`、`frozen`)**无 target 字段**,`from_inspector_results` 展平 findings 时**丢弃** `InspectorResult.target_name`;`Report.target_name: str (min_length=1)` 是**单值**。为承载多 target 而不破契约:
   - **`Finding.target_name: str | None = None`(add-only)**:默认 None → 旧构造方 / 旧 JSON 零改动可加载;`extra="forbid"` 仍生效;多 target 组装给每条 flatten 出的 finding 盖来源 `InspectorResult.target_name`,单 target 路径可留 None。
   - **`compute_finding_id` 不变(B-4)**:`target_name` **不**纳入指纹(指纹恒为 `sha256(name\x00version\x00message)[:16]`)。理由:指纹纳入 target_name 会让同一检查项跨 target 得不同 id,破坏 per-target regression diff 的同 id 锚点;且单 target finding id 跨 run 必须稳定。
   - **多 target(fleet)组装路径**(新):接受跨多 target 的 inspector_results,组一份 Report,`Report.target_name`=确定性 fleet 标签,`meta.target_id`=确定性 fleet id(有序 target_id + schedule_name 派生、**带 `fleet:` 类前缀使其与裸 target_name 不相交**,防单成员 fleet 撞 per-target store key),每条 finding 盖来源 target_name。
   - **下游消费者 cross-reference**:`Finding.target_name` 的 per-finding 来源标注语义被**提案 C `improve-report-rendering-and-i18n`** 的多 target 分节渲染 + 四元组 dedup(`(target_name, inspector_name, message, severity)`)消费;**改 fleet 组装的 `target_name` 盖值规则须同步评估 C 的退化判据**(C 把单主机退化判据定为「`distinct(non-None target_name) ≤ 1` → 无分节」,对本提案的盖值策略零耦合)。耦合双向可见,避免改 B 静默破坏 C 渲染。
   - **redaction 边界透传（BLOCKER 修复）**:notifier 渲染入口（`telegram.py` / `lark.py` 的 `render`）**先** `redact_report_for_render`、**再**喂模板,而 `_redact.py:_redact_finding` 用**显式字段列表**重构 `Finding(...)`——新加的 `target_name` **不在列表里就被默认丢成 None**。故本提案**必须**同步在 `_redact_finding` 透传 `target_name`（report-data-model 的「渲染/落盘边界脱敏并透传 Finding 字段」需求已 MODIFY 含此条 + 任务 2.5.5）。否则 C 的多 target 分节 / 四元组去重在**脱敏拷贝**上拿到全 None → 分节失效 + 跨主机误并,fleet 报告静默丢主机维度。既有 `tests/reporting/test_redact_m3_fields.py` 的 add-only 字段透传守门测试预言了这个 failure mode。
   - **fleet Report 不支持 per-target regression diff(B-4 修复,堵 target_id-keyed diff 失效)**:fleet Report 持**单一** `meta.target_id`(fleet id),无法为内含的每个 target 取 per-target baseline;故 fleet Report 是 **notify 导向**,per-target regression diff 仍只在 per-target(agent 模式)report 上做。写成 `report-data-model` 的非目标契约,防 review 误以为 fleet Report 能走 per-target diff。

8. **`requires_unmet` 不污染 deterministic severity / 不降级 partial（B-6 修复）**。deterministic 逐 target 跑**固定健康集**时,固定集中某 inspector 对某 target capability 不匹配(如 mysql inspector 跑在没装 mysql 的台,记 `requires_unmet`)是**跨异构机跑统一健康集的正常情形**,**必须**视为预期跳过:**不计入** severity 聚合、**不**把报告 `meta.status` 降级为 `partial`。这是对既有 `from_inspector_results` 默认派生(「任一 `requires_unmet` → partial」保守外推)的**蓄意覆盖**——agent 模式下 `requires_unmet` 罕见、值得标 partial;deterministic 模式下它是预期结果,标 partial 会让每份 fleet 报告恒 partial、淹没真信号。**真正的失败**——`timeout`(全 timeout)/ `exception` / `target_unreachable`——**仍**按既有语义计入 severity / 降级。deterministic 组装路径在派生 status 时把 `requires_unmet` 排除出降级触发集(显式传 override status 或调用支持该语义的组装路径)。

## 风险 / 权衡

- **默认集漂移**:`DEFAULT_HEALTH_INSPECTORS` 是 curated 列表,新增 inspector 不会自动进集。权衡:显式可控 > 自动全跑(全跑会很慢很吵)。测试钉「成员都存在」+ 文档说明「想进默认集要显式加」。
- **多 target 报告体量**:6 台 × N inspector 的 findings 聚合可能很大(尤其 LLM 输入)。narrate-only 仍读全部结果耗 token,但**有界**(无漫游、无 request_more_inspection)。必要时对 LLM 输入做 finding 摘要 / 上限。
- **并发 SSH 压力**:`targets × inspectors` 多路 SSH exec;信号量限流(同 probe_many),避免连接风暴。
- **capability skip 的呈现**:不适用项**视为跳过**(status 仍 `requires_unmet`、不新增枚举值)而非 failed,报告 / severity 不被「这台没装 mysql」噪声污染 —— 与既有 `requires_unmet` 语义一致。
- **mode 默认 agent 的兼容性**:现有 manifest(无 `mode`)= agent,行为零变;新字段 `extra="forbid"` 下需确保旧 manifest 仍校验通过(`mode` 有默认值即可)。
