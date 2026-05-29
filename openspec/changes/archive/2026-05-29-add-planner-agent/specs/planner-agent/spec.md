## 新增需求

### 需求:Planner Agent 装配系统提示词

`PlannerAgent` 必须从外置模板文件 `agent/prompts/planner.md` 加载系统提示词，禁止在 Python 代码中内联提示词正文。模板加载必须使用 `importlib.resources` 以保证 pip 安装后仍可读。模板缺失或不可读时，`PlannerAgent` 构造必须 fail-loud（抛出 `ConfigError`，kind=`planner_prompt_missing`），禁止静默退化为空提示词。

`PlannerAgent` 必须把 `ToolRegistry` 中 `surfaces ∋ "agent"` 的工具按 name 升序渲染为「工具名: 描述」概览，替换模板占位标记，得到最终系统提示词文本。该文本必须以单元素 text block 列表（`[{"type": "text", "text": <rendered>}]`）形态传给 `AgentLoop`，禁止传裸字符串 —— `AgentLoop._inject_cache_control` 只对 list 形态的 system 注入 `cache_control`，传裸字符串会使 prompt caching 静默失效。同一组工具下渲染结果必须跨多次 run 字节稳定（prompt caching 前提）。

#### 场景:模板正常加载并渲染工具概览
- **当** 用一个已注册默认工具（`run_inspector` / `list_inspectors` / `list_targets`）的 `ToolRegistry` 构造 `PlannerAgent`
- **那么** 装配出的系统提示词必须包含模板正文与三个工具按 name 升序排列的概览，且对同一 registry 重复构造产出字节一致的提示词

#### 场景:系统提示词以 text block 列表形态传入
- **当** `PlannerAgent` 把装配好的系统提示词交给 `AgentLoop`
- **那么** 传入的 `system` 必须是单元素 text block 列表 `[{"type": "text", "text": <rendered>}]`，禁止为裸字符串（保证 `AgentLoop._inject_cache_control` 能注入 `cache_control`）

#### 场景:模板文件缺失时构造期 fail-loud
- **当** 指定的提示词模板路径不存在或不可读
- **那么** `PlannerAgent` 构造必须抛出 `ConfigError`（kind=`planner_prompt_missing`），禁止用空提示词继续

### 需求:Planner Agent 经 ToolRegistry 装配并运行 AgentLoop

`PlannerAgent` 必须仅通过 `ToolsAdapter`（由其内部用注入的 `ToolRegistry` 与 `context_factory` 构造）暴露能力给 `AgentLoop`，禁止直接 import Inspector registry、禁止绕过 registry 直调 handler、禁止暴露任意命令执行工具。

`LLMBackend` 必须经 `PlannerAgent` 传入 `AgentLoop.__init__`，禁止放入 `context_factory` 产出的 `ToolContext`（ADR-008）。

`PlannerAgent` 必须把装配好的系统提示词以 text block 列表形态作为 `AgentLoop(system=...)` 传入，并通过 `AgentLoop.run(intent)` 驱动多轮 tool-use 循环。Token 预算、最大轮数、重试均由 `AgentLoop` 按既有逻辑强制，`PlannerAgent` 禁止放宽这些上限、禁止自行重试。

#### 场景:意图驱动 Agent 自选并调用 inspector
- **当** 以自然语言意图（如「检查这台机器的健康状况」）调用 `PlannerAgent.run(intent)`，且 backend 回放中 Agent 选择调用 `run_inspector`
- **那么** Planner 必须把 intent 作为 user message、装配好的提示词作为 system、registry 中 agent-surface 工具作为 tools，交给 `AgentLoop` 运行，并返回收敛后的结果

#### 场景:backend 不进 ToolContext
- **当** `PlannerAgent` 构造内部依赖
- **那么** `LLMBackend` 必须只到达 `AgentLoop`，`context_factory` 产出的 `ToolContext` 中禁止出现 backend 引用

### 需求:Planner Agent 收敛 LoopResult 为 PlannerResult

`PlannerAgent.run(intent)` 必须返回一个不可变 `PlannerResult`，包含：`narrative`（取自 `LoopResult.final_text`）、`findings`（结构化 `Finding` 列表）、`loop_result`（原始 `LoopResult`）、`intent`（本次意图）。

`findings` 必须通过遍历 `LoopResult.tool_invocations`、对 `tool_name` 等于 `run_inspector` 工具名且 `output` 非空（成功）的 invocation 用 `RunInspectorOutput` 还原后收集其 findings 得到，保留出现顺序，禁止去重或排序。`output` 为空（即 `error` 非空）的 invocation 必须被跳过、不计入 `findings`，但必须仍保留在 `loop_result.tool_invocations` 中。

`PlannerAgent` 必须原样透传 `LoopResult.terminal_status`，禁止二次判定成功/失败、禁止在 `ok` 之外重新解释状态语义。空 `findings` 配合 `ok` 状态必须被视为合法结论而非错误。

`narrative` 必须逐字等于 `LoopResult.final_text`，`PlannerAgent` 禁止截断、补救或反推该文本。`narrative` 是否为空由 loop 决定而非由 terminal_status 唯一推定：loop 仅在 `stop_reason == "end_turn"` 与 `stop_reason == "max_tokens"`（后者 terminal_status=`degraded_token_budget`，携带模型部分输出）两条路径填入文本；`degraded_max_turns`、token 预算预检守卫、重试耗尽、`refusal` 路径下 `final_text` 为空。

#### 场景:无损收集成功的 run_inspector findings
- **当** `LoopResult.tool_invocations` 含两次成功的 `run_inspector` 调用，各返回若干 `Finding`
- **那么** `PlannerResult.findings` 必须按出现顺序包含两次调用的全部 findings，且字段无损

#### 场景:跳过失败的工具调用
- **当** 某次 `run_inspector` 调用返回 error envelope（`output` 为空、`error` 非空）
- **那么** 该次调用的 findings 必须不出现在 `PlannerResult.findings` 中，但该 invocation 必须仍保留在 `PlannerResult.loop_result.tool_invocations` 中

#### 场景:Agent 未调用任何 inspector 仍正常返回
- **当** Agent 在未调用任何 `run_inspector` 的情况下以 `end_turn` 结束（terminal_status=`ok`）
- **那么** `PlannerResult.findings` 必须为空列表、`narrative` 为 LLM 文本、`terminal_status` 保持 `ok`，且不抛出异常

#### 场景:降级状态透传（max_turns，无助手文本）
- **当** `AgentLoop` 因达到最大轮数返回 `degraded_max_turns`
- **那么** `PlannerResult.loop_result.terminal_status` 必须原样为 `degraded_max_turns`，`findings` 含已收集的部分结果，`narrative` 必为空字符串（该路径 loop 不填 `final_text`），`PlannerAgent` 禁止重试或抛错、禁止反推补救 narrative

#### 场景:max_tokens 降级携带部分输出
- **当** `AgentLoop` 因 `stop_reason == "max_tokens"` 返回 `degraded_token_budget`，且 `LoopResult.final_text` 含模型部分输出
- **那么** `PlannerResult.narrative` 必须逐字等于该 `LoopResult.final_text`（可能非空），`PlannerAgent` 禁止因 terminal_status 是降级值而把 narrative 置空
