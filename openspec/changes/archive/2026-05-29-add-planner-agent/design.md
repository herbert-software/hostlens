## 上下文

M2.4 之前的两块下游已稳定：

- **`AgentLoop`**（`agent/loop.py`，archived `add-agent-loop-skeleton`）：构造签名 `AgentLoop(backend, tool_adapter, settings, *, system=None)`；`run(intent) -> LoopResult`。它是 intent-agnostic 的通用 tool 分发器，不构造系统提示词、不组装报告。`LoopResult` 含 `final_text` / `tool_invocations: list[ToolInvocation]` / `turns` / `terminal_status` / `usage_totals` / `stop_reason`。`ToolInvocation` 满足「`output` 与 `error` 恰好一个非空」。loop 已在调 backend 前按 `backend.capabilities.prompt_caching` 决定是否注入 `cache_control`（`_inject_cache_control`），并在构造期校验 `settings.agent is not None`。
- **`ToolsAdapter`**（`agent/tools_adapter.py`）：`list_for_agent()` 把 `surfaces ∋ "agent"` 的 ToolSpec 投成稳定有序的 Anthropic schema dicts；`dispatch(name, args, ctx)` 做 policy gate + handler 调用 + error 脱敏。`register_default_tools(registry)` 注册 `run_inspector` / `list_inspectors` / `list_targets`。`run_inspector` 的输出 `RunInspectorOutput`（`target_name` / `inspector_name` / `findings: list[FindingSummary]`），其中 `FindingSummary` 是 `reporting.models.Finding` 的类型别名 —— **工具输出里的 findings 即完整 `Finding`，无损**。

缺的是把「自然语言 intent」装配成一次配置好系统提示词 + 工具集 + backend 的 run，并把 `LoopResult` 收敛成可消费产物的中间层。本设计定义这个 `PlannerAgent`。

约束：CLAUDE.md §4.1（手写 loop 可读）/ §4.2（Planner 是调度者，不做关联）/ §4.8（prompt caching）/ §4.10（能力只经 registry）/ §7 反模式（backend 不进 ToolContext、提示词不内联）；ADR-008（backend 私有）/ ADR-005（重试单一收口在 loop）。

## 目标 / 非目标

**目标：**
- 提供 `PlannerAgent`：装配系统提示词 + 工具集 + backend，运行 `AgentLoop`，把 `LoopResult` 收敛成 `PlannerResult`。
- 系统提示词外置于 `agent/prompts/planner.md`，跨 run 稳定（可被 loop 缓存）。
- 从 `LoopResult.tool_invocations` 无损收集 `run_inspector` 的 `Finding`，透传 loop 遥测与 terminal_status。
- 单测（`FakeBackend`）+ 端到端回放（`PlaybackBackend` + cassette）可在无 SSH / 无付费 API 下跑通。

**非目标：**
- 不组装结构化 `reporting.models.Report` 对象（M3）。
- 不做 Diagnostician / 关联 / 根因（M3）；不做 CLI `--intent` 与流式输出（M2.7）；不做 cache 命中率断言（M2.5）；不做 MCP 投影（M7）。
- 不改 `AgentLoop` / `ToolsAdapter` / `ToolRegistry` / `Finding` 的任何已发布行为。

## 决策

### D-1：Planner 接收 `ToolRegistry` + `LLMBackend` + `Settings`，内部构造 `ToolsAdapter` 与 `AgentLoop`

**选择**：`PlannerAgent(backend, registry, settings, context_factory, *, prompt_path=None)`。Planner 内部用 `registry` + `context_factory` 构造 `ToolsAdapter`，再用 `backend` + adapter + `settings` + 装配好的 system 构造 `AgentLoop`。

**理由**：
- Planner 需要 `registry`（不只是 adapter）来渲染系统提示词里的「工具概览」—— 它要遍历工具的 name/description 写进提示词。虽然 `adapter.list_for_agent()` 也能拿到 name/description，但直接持 registry 让提示词渲染与 schema 投影解耦（提示词用人类可读 description，schema 投影是另一回事）。
- `context_factory: Callable[[], ToolContext]` 由调用方提供（CLI / 测试），与 `ToolsAdapter` 现有契约一致（adapter 每次 dispatch 调一次 factory，保证 per-turn `cancel` 不跨轮共享）。Planner 不自己造 `ToolContext`（它不持有 target/inspector registry 等依赖）。
- backend 经 Planner 传给 `AgentLoop.__init__`，**绝不**进 `context_factory` 产出的 `ToolContext`（ADR-008，CLAUDE.md §7）。

**替代方案**：
- (A) Planner 直接接收已构造好的 `ToolsAdapter` 和 `AgentLoop` —— 否决：那样 Planner 退化成「调一下 run 再收敛」的薄包装，系统提示词装配（M2.4 的核心价值）就无处安放，且调用方要自己拼 system，重复劳动。
- (B) Planner 接收 `ToolsAdapter` 但自己不持 registry，提示词概览从 `adapter.list_for_agent()` 反推 —— 否决：`list_for_agent()` 返回的是 JSON-schema 投影（含 input_schema），从里面抠人类可读概览很别扭；持 registry 更直接。

### D-2：系统提示词 = 静态模板（外置 .md）+ 确定性渲染的工具概览，跨 run 稳定

**选择**：`agent/prompts/planner.md` 存 Planner 角色 + 调度纪律（静态正文，含一个占位标记如 `{tool_overview}`）。Planner 加载模板后，把 registry 里 `surfaces ∋ "agent"` 的工具按 **name 升序**渲染成「name: description」清单，替换占位标记，得到最终 system 文本。该文本必须包装成**单元素 text block 列表** `[{"type": "text", "text": <rendered>}]` 作为 `AgentLoop(system=...)` 传入，**不得传裸 `str`**。

> **关键约束（对照 `loop.py` `_inject_cache_control` 实证）**：loop 只对 **`list` 形态**的 system 注入 `cache_control`（`if not isinstance(system, list) or not system: return system`）。若 Planner 传裸 `str`，cache_control 永远不会注入 —— prompt caching 会**静默失效且无报错**，直接打脸 D-2 的稳定缓存主张。故 Planner 端必须传 list[text block]；这是 M2.4 自己的契约责任，**不需要也不改** `_inject_cache_control`（保持 M2.2 已归档行为不变）。

**理由**：
- **外置**：CLAUDE.md §7 反模式明确禁止内联提示词；.md 文件便于面试官阅读、便于迭代。
- **确定性渲染**：prompt caching（CLAUDE.md §4.8）要求系统块跨 run 字节稳定。工具按 name 升序（与 `ToolRegistry.list_for` 已有的排序一致）+ 固定模板 → 同一组工具下 system text block 内容恒定，配合上面的 list[text block] 形态，loop 的 `_inject_cache_control` 才能稳定命中。
- **占位替换用 `str.replace`**：不引入 Jinja2 到 Agent 层（Jinja2 是 notifier/report 模板的依赖，Agent 层保持最小依赖；单占位符无需模板引擎）。
- **加载用 `importlib.resources`**：包内资源定位，pip 安装后仍可读；模板缺失时构造期 raise `ConfigError(kind="planner_prompt_missing")`（fail-loud，不静默空提示词）。

**替代方案**：
- (A) 提示词内联在 `planner.py` 字符串常量 —— 否决（违反 §7）。
- (B) 工具概览不进系统提示词，靠 Anthropic `tools` 参数自带的 description —— 部分否决：`tools` 参数确实带 description，但系统提示词里给一份「可用工具总览 + 调度纪律（先发现后执行、只读、不臆造工具）」能显著提升小意图下的规划质量，且这是 Planner 的角色定义所在。两者并存：tools 参数给 schema，system 给纪律 + 概览。
- (C) 渲染用 Jinja2 —— 否决：Agent 层不该为单占位符引入模板引擎依赖。

### D-3：`PlannerResult` = narrative + findings + loop_result，不组装 Report

**选择**：新增 frozen Pydantic `PlannerResult`：
- `narrative: str` —— `loop_result.final_text` **逐字透传，Planner 不截断不补救**（与 D-4 透传原则一致）。**注意降级语义（对照 `loop.py` `_finalize` / `run()` 实证）**：`_finalize` 的 `final_text` 默认 `""`，`loop_result.final_text` 非空**当且仅当** loop 捕获了助手文本，仅两条路径：
  - `stop_reason == "end_turn"`（正常完成，terminal_status=`ok` 或 `empty_response`）；
  - `stop_reason == "max_tokens"`（模型生成中途撞 max_tokens，loop 把已生成的**部分文本** `_join_text(response)` 传入 `_finalize`，terminal_status=`degraded_token_budget`）。
  其余所有降级/失败退出路径 —— **预检 token 预算守卫**（轮首 `usage >= budget`，与上面的 max_tokens 不同源，但 terminal_status 同为 `degraded_token_budget`）、`degraded_max_turns`、`failed_api_unavailable` / `degraded_rate_limited` / `degraded_no_planner`（重试耗尽）、`refusal` —— 均不传 `final_text`，`loop_result.final_text == ""`，`narrative` 为空。
  **关键推论**：`degraded_token_budget` 这一个 terminal_status 可经两条路径到达，narrative 既可能非空（max_tokens 部分输出）也可能为空（预检守卫）—— 故**不能**用 terminal_status 反推 narrative 是否为空；spec 断言 narrative=="" 时必须用 `degraded_max_turns` 这种确定无文本的路径触发。Planner 不读 `tool_invocations` 反推助手文本（越界 loop 内部状态）；降级时的有用信号在 `findings`（已收集部分）与 `terminal_status`。
- `findings: list[Finding]` —— 遍历 `loop_result.tool_invocations`，对 `tool_name == "run_inspector"` 且 `output is not None`（成功）的 invocation，用 `RunInspectorOutput.model_validate(inv.output)` 还原后 extend 其 `.findings`。保留出现顺序，不去重不排序。
- `loop_result: LoopResult` —— 原样持有，供 CLI/可观测读 `terminal_status` / `usage_totals` / `turns` / `stop_reason`。
- `intent: str` —— 回填本次意图（便于下游与日志）。

**理由**：
- `run_inspector` 输出的 findings 是无损 `Finding`，但**没有 InspectorResult 级字段**（status / 计时 / result-level evidence）。强行组装完整 `Report`（`Report.from_inspector_results` 需要 `list[InspectorResult]`）会要么伪造 InspectorResult、要么丢字段 —— 都污染架构。M2.4 是「调度者」，产出「LLM 综述 + 收集到的结构化 finding」已满足退出条件（出 markdown 报告 = narrative）。完整 `Report` 组装 + 关联是 M3。
- 收集时**跳过 error invocation**（`output is None`）：inspector skip/超时/校验失败不计入 findings，但仍留在 `loop_result.tool_invocations` 供调试。
- frozen：与 `LoopResult` / `Finding` 一致，结果对象不可变。

**替代方案**：
- (A) 现在就组装 `Report` —— 否决（上述有损/伪造问题；越界 M3）。
- (B) 只返回 `LoopResult`，findings 让调用方自己抠 —— 否决：每个调用方（M2.7 CLI、M3）都要重复写「过滤 run_inspector + model_validate + extend」的收集逻辑，且 `tool_name` 字符串与 `RunInspectorOutput` schema 的耦合应收敛在 Planner 一处。
- (C) 扩展 `run_inspector` 让它回传完整 InspectorResult —— 否决：那会撑大 LLM 上下文（违背工具输出刻意精简的设计），且改的是 M2.3 已归档契约，超出 M2.4 范围。

### D-4：terminal_status 透传，Planner 不重试、不二次判定成功

**选择**：Planner 不解释 `terminal_status`，原样放进 `PlannerResult.loop_result`。空 findings + `ok` 是合法结论（意图无需巡检）；degraded/failed 状态下 findings 为已收集的部分结果。

**理由**：ADR-005 重试单一收口在 loop；CLAUDE.md §4.2 Planner 只调度。把 terminal_status 的语义判定（退出码、是否告警）留给 M2.7 CLI / M4 Scheduler，避免在两层重复判定逻辑。

### D-5：`run_inspector` 名称与 schema 的耦合用模块常量收敛

**选择**：在 `planner.py` 顶部从 `hostlens.tools.default_tools` import `run_inspector`（ToolSpec）取 `run_inspector.name` 作为收集判定的 key，并 import `RunInspectorOutput` 做 `model_validate`。不硬编码字面量 `"run_inspector"`。

**理由**：避免字符串字面量与 ToolSpec 漂移；若 M3 改名，引用处编译期可见。这也明确了 Planner 对「哪个工具产 finding」的依赖点，便于未来扩展（如多个产 finding 的工具时改成查 ToolSpec 的某个 tag）。

## 风险 / 权衡

- **[提示词里工具概览与 `tools` 参数 description 重复]** → 接受。system 里是「纪律 + 总览」（人类语气、稳定有序），`tools` 参数是 schema（机器用）。重复的是 description 文本本身，但两处用途不同；token 成本由 prompt caching 摊薄（system 块缓存）。
- **[空 findings 难以区分「Agent 没调工具」与「调了但全 skip」]** → 缓解：两种情况都是合法降级，`loop_result.tool_invocations` 保留全部 invocation（含 error），调用方/调试可据此区分；`terminal_status` 也提供信号。
- **[`RunInspectorOutput.model_validate` 在 output dict 结构漂移时会 raise]** → 缓解：output 来自本进程内 `ToolsAdapter.dispatch` 的 `result.model_dump()`，schema 自洽，正常路径不会漂移；若 raise 说明是代码 bug，应 fail-loud（不吞）。这与 §6「错误处理只在边界做」一致 —— Planner 信任本进程工具输出。
- **[提示词模板字节变化导致 cache 失效]** → 接受并文档化：迭代 `planner.md` 会让旧 cassette/缓存失效，属预期；M2.5 会加 cache 命中测试时一并固定。
- **[Planner 不组装 Report，下游 M2.7 需自己渲染 narrative]** → 接受：M2.7 本就要做 Rich/markdown 渲染；narrative 已是 markdown 文本，CLI 直接输出即可，findings 可选做结构化补充。

## Migration Plan

纯新增，无迁移、无破坏性变更。新增 `agent/planner.py` + `agent/prompts/planner.md`，新增公共符号 `PlannerAgent` / `PlannerResult`。回滚 = 删除新增文件与测试，无下游已依赖（M2.7 尚未实现）。

## Open Questions

- **提示词 `planner.md` 是否需要 few-shot 示例块？** 倾向 M2.4 先不加（保持精简、降低 token），M2.8 incident pack 落地后若规划质量不足再补 few-shot（届时也要进 cache 静态块）。本提案不预留 few-shot 占位。
- **`PlannerAgent` 是否暴露 `cancel: asyncio.Event` 以支持 CLI Ctrl-C 取消？** 倾向留给 M2.7：取消信号经 `context_factory` 产出的 `ToolContext.cancel` 已可达工具层，loop 层的取消接入是 M2.7 CLI 集成时的事；本提案不在 Planner 引入取消 API。
