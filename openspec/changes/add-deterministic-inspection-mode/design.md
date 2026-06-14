## 上下文

调度 job body（`scheduler/runner.py`）当前**只调** `run_diagnosis_pipeline`（agent）:Planner（LLM）经 `run_inspector(target_name=自选)` + `list_targets` 自主选 inspector **和 target**，受 `max_turns` / `token_budget` 封顶。实测覆盖非确定 + 常 `partial`。`manifest.inspectors` 仅 soft hint（`runner.py:405` 不消费）。

可复用的现成件:`InspectorRunner.run(manifest, target, parameters, ...)`（确定性跑一个 inspector）、`run_inspector_handler` 的 target+inspector 解析逻辑、`InspectorResult` 收集、`from_inspector_results`（结果 → Report）、Diagnostician 的 `correlate_findings`（只关联不巡检）、Report 持久化 + Notifier 派发 + 调度留痕。**deterministic 模式 = 用这些件拼一条不走 Planner 的路**。

## 目标 / 非目标

**目标:** 见 proposal —— 固定集逐 target 直跑、多 target、内置健康默认集、LLM narrate-only、多 target 报告。

**非目标:** 不改 agent 模式;不动 Notifier / 触发 / 留痕 / 优雅停机;不新增 inspector;不让 deterministic 成默认;不为 agent 放开多 target。

## 决策

1. **`mode: Literal["agent","deterministic"] = "agent"`**（manifest 字段，默认 agent → 向后兼容)。runner job body 按 mode 分叉:`agent` → 现有 `run_diagnosis_pipeline`(零改动);`deterministic` → 新 `run_deterministic_inspection`。

2. **多 target 仅 deterministic 放开**。loader 的 M4 单 target 校验改为:`mode=="agent"` → 仍恰好 1 个;`mode=="deterministic"` → ≥1 个。理由:agent 的 Planner 本就漫游、多 target 无意义;deterministic 才需要「逐台跑」。

3. **内置健康默认集 = 命名常量** `DEFAULT_HEALTH_INSPECTORS`(一组现有 inspector name)。解析规则:`mode=="deterministic"` 且 manifest **无** `inspectors:` → 用默认集;**有** `inspectors:` → 用它(权威,不再 soft hint)。每个 inspector 对每个 target 跑前过 **capability 门**(`requires_unmet`):不适用的记为 **skipped**(非 failed,不污染报告 severity)。`DEFAULT_HEALTH_INSPECTORS` 成员必须都在 registry —— 加测试钉死(防 curated 集漂移)。

4. **确定性采集路径** `run_deterministic_inspection(manifest, ...)`:
   - 对 `manifest.targets` 每个 target × 解析出的 inspector 集,经 `InspectorRunner.run` 跑(复用 `run_inspector_handler` 的 target/inspector 解析 + capability 门),收集 `InspectorResult`。
   - **信号量限流**(复用 probe_many 同款),`targets × inspectors` 并发有界;单项失败隔离进结果(不崩整批,对齐 inspector 错误边界)。
   - **无 AgentLoop、无 LLMBackend 注入采集阶段**(守 §4.2「Inspector 不调 LLM」+ ADR-008)。

5. **LLM narrate-only**:采集完 → `from_inspector_results` 组装 Report → 过一次 Diagnostician,**只注册 `correlate_findings`(关联/根因叙述),禁止注册 `request_more_inspection`**(结构上让 LLM 无法追加巡检 / 选 target)。LLM 输入 = 已采集的固定结果,输出 = 根因假设 + 处置建议。Backend 注入 `AgentLoop.__init__`(非 ToolContext,守 ADR-008)。

6. **多 target 报告**:采集结果按 `InspectorResult.target_name` 标记;组装成**一份** Report,findings 跨全部 target(各自带 target/inspector 上下文);`report_target_name` 取 fleet 标签(如 `"fleet:<count> targets"` 或 targets join)。notify 路由的 `aggregate_severity` 对全部 findings 聚合 —— `only_if` 在全队聚合 severity 上判定(机制不变)。若 Report 模型需要小让步(report_target_name 容纳标签),归入 scheduler-engine MODIFY,不动 report-data-model 的 finding 结构。

## 风险 / 权衡

- **默认集漂移**:`DEFAULT_HEALTH_INSPECTORS` 是 curated 列表,新增 inspector 不会自动进集。权衡:显式可控 > 自动全跑(全跑会很慢很吵)。测试钉「成员都存在」+ 文档说明「想进默认集要显式加」。
- **多 target 报告体量**:6 台 × N inspector 的 findings 聚合可能很大(尤其 LLM 输入)。narrate-only 仍读全部结果耗 token,但**有界**(无漫游、无 request_more_inspection)。必要时对 LLM 输入做 finding 摘要 / 上限。
- **并发 SSH 压力**:`targets × inspectors` 多路 SSH exec;信号量限流(同 probe_many),避免连接风暴。
- **capability skip 的呈现**:不适用项记 skipped 而非 failed,报告 / severity 不被「这台没装 mysql」噪声污染 —— 与既有 `requires_unmet` 语义一致。
- **mode 默认 agent 的兼容性**:现有 manifest(无 `mode`)= agent,行为零变;新字段 `extra="forbid"` 下需确保旧 manifest 仍校验通过(`mode` 有默认值即可)。
