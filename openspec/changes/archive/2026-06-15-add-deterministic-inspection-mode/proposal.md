## 为什么

调度巡检当前**只有 agent 一条路**:manifest 给 `intent`，Planner（LLM）自主选 inspector **和 target** 跑。在 tizi 6 台实测发现这对**无人值守的每日巡检不可靠**:

- Planner 有 `run_inspector(target_name=LLM 自选)` + `list_targets`(返回全部 target)→ **target 覆盖非确定**:一次漫游到 aliyun-bj ×17、另一次只跑 bandwagon ×2。
- 受 token / turn 预算封顶（`max_turns=20`、`token_budget_input=100K`），巡检多台 degrade 成 `partial` → **常常覆盖不全**。
- `manifest.inspectors` 是 **soft hint 不被消费**（`runner.py:405`），无法强制确定性覆盖。

结论:LLM 自主选 inspector/target 是项目核心展示点、适合**交互式探查**，但**定时盯一组固定机器**需要的是「每台都把固定检查项跑完」的确定性，agent 模式给不了。

## 变更内容

新增**确定性巡检模式**(deterministic)，与现有 agent 模式并存:

- **manifest `mode: agent | deterministic`**（默认 `agent`，**向后兼容**）。
- **deterministic 模式**:对**每个 target**，**直接经 `InspectorRunner` 跑一个固定 inspector 集**（不走 Planner、不漫游、不让 LLM 选 target）；按 target capability 自动跳过不适用项（复用 `requires_unmet` 门控）。
- **多 target**:deterministic 模式**放开 M4 单 target 约束** —— 一份 manifest `targets: [全部 6 台]`，逐台跑固定集 → 一份全队报告。agent 模式仍单 target。
- **内置「健康巡检」默认集**:提供一个 curated 默认健康集（cpu / 内存 / 磁盘 / inode / 负载 / systemd / 日志 / 网络等，取自现有 registry）；manifest 不写 `inspectors:` 即用默认；**写了则 `inspectors:` 在 deterministic 模式下变权威集**（不再是 soft hint）。
- **LLM 只写根因叙述（narrate-only）**:把**确定性采集到的结果**喂 Diagnostician，**仅**生成根因假设 / 处置建议 —— **不选 inspector、不选 target、不漫游、不追加巡检**。覆盖确定 + 保留 LLM 洞察，token 有界。
- **多 target 报告**:按 target 分节聚合各检查项发现，聚合 severity 供 notify `only_if` 路由;复用既有 Report 持久化 + Notifier 派发。

**非目标**:

- 不改 agent 模式既有行为（单 target、Planner 自主选）—— 默认仍是 agent，向后兼容。
- 不让 deterministic 成为默认。
- 不新增 inspector —— 默认健康集是对**现有** inspector 的 curated 选择。
- 不为 agent 模式放开多 target（多 target 仅 deterministic）。
- 不动 Notifier 协议 / 通道配置 / 调度触发与留痕机制。
- **fleet（多 target）Report 不支持 per-target regression diff** —— fleet Report 是 notify 导向、持单一 fleet `target_id`，无法按内含 target 分别取 baseline;per-target regression diff 仍只在 agent 模式的单 target report 上做。
- **不改 `compute_finding_id`** —— `Finding.target_name` 是 add-only 标注字段，**不**纳入 finding id 指纹（保单 target finding id 跨 run 稳定、per-target diff 同 id 锚点不变）。

## 功能 (Capabilities)

### 新增功能

- `deterministic-inspection-mode`: 调度的确定性巡检模式 —— 固定 inspector 集逐 target 直跑（不走 Planner、按 capability 过滤）、多 target、内置健康默认集（manifest 可 override 为权威集）、LLM 仅对采集结果写根因叙述、多 target 报告聚合。

### 修改功能

- `schedule-manifest`: 新增 `mode: agent | deterministic`(默认 agent);deterministic 模式放开 `targets` 多成员（agent 仍单 target）;`inspectors` 在 deterministic 模式语义从 soft hint 变权威集。
- `scheduler-engine`: job body 按 `mode` 路由 —— `agent` 走现有 `run_diagnosis_pipeline`;`deterministic` 走新的「逐 target 跑固定集 → narrate-only → 多 target 报告」路径。
- `report-data-model`: `Finding` 加 add-only 来源字段 `target_name: str | None = None`(默认 None,旧构造 / 旧 JSON 零改动;**不**纳入 `compute_finding_id`);新增多 target（fleet）Report 组装路径(跨多 target 的 inspector_results 组装一份 Report、`Report.target_name` 为确定性 fleet 标签、`meta.target_id` 为确定性 fleet id、每条 finding 盖来源 target_name);明确 fleet Report 不支持 per-target regression diff。
- `diagnostician-agent`: 新增 narrate-only 装配路径(只注册 `correlate_findings`,禁注册 `request_more_inspection` / `list_inspectors` / `list_targets`),供 deterministic 路径让 LLM 结构上拿不到再巡检 / 选 target 的工具;既有全装配(三件)不变。

## 影响

- **代码**:`scheduler/{schema,loader,runner}.py`（mode 字段 + 多 target 校验 + 路由 + 确定性采集路径）;默认健康集定义（常量 / 命名集）;`reporting/models.py`（`Finding.target_name` add-only 字段 + 多 target fleet Report 组装路径,`compute_finding_id` 不变）;`tools/diagnostician_tools.py`（narrate-only 装配路径,只注册 `correlate_findings`,复用 `_build_correlate_findings_spec`);deterministic 组装把 `requires_unmet` 排除出 severity / 降级触发集。
- **行为**:新增 opt-in 模式;agent 模式与既有调度留痕 / 触发 / 优雅停机不变。
- **文档**:schedule manifest 文档增 `mode` / 多 target / 默认健康集说明 + Demo Path。
- **测试**:确定性路径（固定集逐 target 跑、不漫游、capability 过滤）、多 target 校验、narrate-only（diagnostician 不追加巡检）、默认集 vs 显式 override、多 target 报告聚合 + notify 路由;VCR cassette 回放 narrate-only LLM。
