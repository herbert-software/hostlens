## 1. 系统提示词模板

- [x] 1.1 新建 `src/hostlens/agent/prompts/__init__.py`（空 package，供 `importlib.resources` 定位）+ `src/hostlens/agent/prompts/planner.md`：写 Planner 角色 + 调度纪律（先 `list_targets` / `list_inspectors` 发现，再 `run_inspector`；只读巡检；不臆造工具；最后自然语言综述），含一个工具概览占位标记（如 `{tool_overview}`）。验收：文件存在，`importlib.resources.files("hostlens.agent.prompts").joinpath("planner.md").read_text()` 能读到内容。
- [x] 1.2 在 `pyproject.toml` 确认 `planner.md` 被打进 wheel（`tool.setuptools.package-data` 或等价；若用 `include-package-data` 配合 MANIFEST 则核对）。验收：`pip install -e .` 后 `python -c "from importlib.resources import files; print(files('hostlens.agent.prompts').joinpath('planner.md').read_text()[:20])"` 成功。

## 2. PlannerResult 数据模型

- [x] 2.1 在 `src/hostlens/agent/planner.py` 定义 frozen Pydantic `PlannerResult`：`narrative: str` / `findings: list[Finding]`（import `hostlens.reporting.models.Finding`）/ `loop_result: LoopResult` / `intent: str`。验收：mypy --strict 通过；构造与 `model_dump()` 往返正常。

## 3. PlannerAgent 装配与运行

- [x] 3.1 实现 `PlannerAgent.__init__(backend, registry, settings, context_factory, *, prompt_path=None)`：加载提示词模板（`importlib.resources`，缺失 raise `ConfigError(kind="planner_prompt_missing")`）；按 registry 中 `surfaces ∋ "agent"` 工具 name 升序渲染 `{tool_overview}`（`str.replace`，不引 Jinja2）；把渲染后的文本包装成单元素 text block 列表 `[{"type": "text", "text": <rendered>}]`（**禁止裸 str**，否则 loop 的 `_inject_cache_control` 跳过缓存）；内部构造 `ToolsAdapter(registry, context_factory)` 与 `AgentLoop(backend, adapter, settings, system=<list[text block]>)`。验收：模板缺失时构造抛 `ConfigError`；传入 `AgentLoop` 的 `system` 是 list[text block]（单测断言类型与形状）；同一 registry 重复构造产出字节一致的 system 文本。
- [x] 3.2 确认 backend 只到达 `AgentLoop`、不进 `context_factory` 产出的 `ToolContext`（ADR-008）。验收：单测构造一个记录传参的 fake `context_factory`，断言其产出的 `ToolContext` 无 backend 字段/引用。
- [x] 3.3 实现 `PlannerAgent.run(intent) -> PlannerResult`：调 `AgentLoop.run(intent)`；收集 findings —— 遍历 `loop_result.tool_invocations`，对 `tool_name == run_inspector.name` 且 `output is not None` 的项用 `RunInspectorOutput.model_validate(inv.output)` 还原并 extend `.findings`（保留顺序，不去重排序）；`narrative = loop_result.final_text`；透传 `terminal_status`（不二次判定）。`run_inspector` 名称与 `RunInspectorOutput` 从 `hostlens.tools.default_tools` / schemas import，不硬编码字面量（design D-5）。验收：见 §4。

## 4. 测试（FakeBackend 单测 + PlaybackBackend 回放）

- [x] 4.1 `tests/agent/test_planner.py`：用 `FakeBackend` 构造一段「tool_use(run_inspector) → tool_use(run_inspector) → end_turn」的响应序列，断言 `PlannerResult.findings` 按顺序无损合并两次调用的 findings、`narrative` 为最终文本、`terminal_status == "ok"`。验收：`pytest tests/agent/test_planner.py -m "not live" -q` 绿。
- [x] 4.2 失败工具跳过：`FakeBackend` 序列里让一次 `run_inspector` 返回 error envelope（dispatch 产出 `is_error`），断言该 invocation 的 findings 不进 `PlannerResult.findings`，但仍在 `loop_result.tool_invocations` 中。
- [x] 4.3 无 inspector 调用：`FakeBackend` 直接 `end_turn`，断言 `findings == []`、`terminal_status == "ok"`、不抛异常。
- [x] 4.4 降级透传（max_turns，narrative 空）：构造让 loop 触发 `degraded_max_turns`（如 `max_turns=1` + 持续 tool_use），断言 `terminal_status == "degraded_max_turns"`、`findings` 含已收集部分、`narrative == ""`（该路径 loop 不填 final_text）、Planner 未重试未抛错。
- [x] 4.4b max_tokens 降级（narrative 非空透传）：构造 `FakeBackend` 返回 `stop_reason == "max_tokens"` 且 content 含文本块，断言 `terminal_status == "degraded_token_budget"`、`narrative` 逐字等于该响应文本（**非空**）—— 验证 Planner 不因降级 terminal_status 把 narrative 置空（防止把 loop `max_tokens` 路径的部分输出误判为空）。
- [x] 4.5 系统提示词稳定性 + 形态：断言对同一 registry 两次构造 `PlannerAgent` 渲染出的 system 文本字节一致；且传给 `AgentLoop` 的 `system` 是 list[text block]（非裸 str）—— prompt caching 前提；命中率断言留 M2.5。
- [x] 4.6 端到端回放：手写一份 cassette，用 `PlaybackBackend` 跑 `PlannerAgent.run("检查这台机器的健康状况")`，断言 `findings` 非空、`narrative` 非空、`terminal_status == "ok"`、重复跑结果稳定（决定性回放）。验收：`pytest tests/agent/test_planner.py -k playback -q` 绿，CI 默认 replay 不消耗 API 额度。

## 5. Anthropic API 降级行为（经 AgentLoop 验证，Planner 透传）

- [x] 5.1 验证 backend 持续不可用时 Planner 不重试、透传 loop 的 `failed_api_unavailable` / `degraded_no_planner`：用一个总是 raise `BackendUnavailable` 的 fake backend（或 cassette miss 模拟），断言 `PlannerResult.loop_result.terminal_status` 为 API 宕机对应值、`narrative` 为空、Planner 未额外重试（断言 backend 调用次数 = loop 重试上限，不被 Planner 放大）。
- [x] 5.2 验证 429 with retry-after 的 honor 仍由 `AgentLoop` 负责、Planner 不干预：构造限流响应，断言 Planner 不吞不重试、最终 terminal_status 反映 loop 的限流降级（`degraded_rate_limited`）。

## 6. 质量门

- [x] 6.1 `mypy --strict src/hostlens/agent/planner.py` 通过，无 `Any`（除非带注释说明）。
- [x] 6.2 `ruff check` + `ruff format --check` 通过；`planner.py` 注释只写 WHY（CLAUDE.md §6），关键装配/收敛步骤可读，面试官能看懂。
- [x] 6.3 全量 `pytest -m "not live"` 绿，无回归。
