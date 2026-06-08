## 1. 依赖与脚手架

- [x] 1.1 `pyproject.toml`：把 `mcp = []` optional-dep 组填成 `mcp = ["mcp>=<选定下界>"]`（查官方 mcp SDK 最新稳定版定下界）；验收：`pip install -e ".[mcp]"`（需 `dangerouslyDisableSandbox`，PyPI 被墙）后 `python -c "import mcp; print(mcp.__version__)"` 成功
- [x] 1.2 `src/hostlens/mcp_server/__init__.py`：从空壳补上模块 docstring 与 `__all__`（导出 `McpToolsAdapter` / `build_server` / `run_stdio`）；验收：`python -c "import hostlens.mcp_server"` 无 ImportError（mcp SDK 已装时）

## 2. McpToolsAdapter（mcp-tool-adapter spec）

- [x] 2.1 `mcp_server/tools_adapter.py`：实现 `McpToolsAdapter.list_for_mcp()` —— 遍历 `registry.list_for("mcp")`，投影 `{name, description=mcp_description, inputSchema=input_schema.model_json_schema()}`；验收：单测断言只输出 mcp surface 工具、description 取 mcp_description、agent-only 工具不出现（spec 场景「投影只输出 mcp surface 工具且 description 取 mcp_description」）
- [x] 2.2 `list_for_mcp()` fail-closed：对每个待投影 spec 断言 `sensitive_output is not None`，否则 raise `ToolPolicyViolation(surface="mcp", violated_field="sensitive_output")`；验收：单测注入 `sensitive_output=None` 的 mcp spec → `list_for_mcp()` raise（不静默 skip，spec 场景「mcp surface 工具缺失 sensitive_output 声明则投影 raise」）
- [x] 2.3 `McpToolsAdapter.dispatch()`：复刻 agent adapter（九步），surface 门改 `"mcp" not in surfaces`、**新增 sensitive_output 对称门**（步骤 3：`spec.sensitive_output is None → raise`）、复用 `hostlens.agent.tools_adapter.scrub_exception_message`；所有 `ToolPolicyViolation` 构造传齐四必填字段（`tool_name`/`surface`/`violated_field`/`reason`，reason 取 `ToolPolicyReason` 合法成员）；验收：单测覆盖 surface / sensitive_output(绕过 list_tools 直 dispatch) / side_effects / approval 各门 raise 且断言 `tool_name`+`reason`、handler 不调用；输入非法→TypeError、handler 返回错类型→ToolError 各一场景（spec 全部策略门 + TypeError/ToolError 场景）
- [x] 2.4 dispatch 异常包络脱敏 + 不误包：handler 抛含 `user=admin@10.0.0.5 sk-...` 的异常 → 包络字符串脱敏不含原始子串；`CancelledError` / `ToolPolicyViolation` / `KeyError` 原样传播；验收：**密钥不进 MCP 错误包络**脱敏测试 + CancelledError/ToolPolicyViolation 传播测试（spec 场景「handler 异常包成脱敏错误包络」「ToolPolicyViolation 与 CancelledError 不被误包」）
- [x] 2.5 跨 adapter 一致性测试：同一组 fixture spec 跨 `ToolsAdapter`(agent) 与 `McpToolsAdapter`(mcp) 断言策略门拒绝行为一致（防双写漂移，design 风险表）

## 3. MCP Server（mcp-server spec）

- [x] 3.1 `mcp_server/server.py`：`build_server(registry, context_factory) -> Server`，**构造时 eager 调一次 `list_for_mcp()` 自检**（混入 sensitive_output is None 的 mcp spec → build_server raise、server 不创建），注册 list_tools handler 委托 `list_for_mcp()`；验收：list_tools 返回含三件套、**额外注册一个 agent-only 测试 spec 后断言其不出现**（不用 vacuous 的 correlate/request 断言）+ build_server 对 sensitive_output is None 的 mcp 工具 eager raise（spec 场景「list_tools 只返回 mcp surface 工具」「build_server eager 失败」）
- [x] 3.2 call_tool handler：委托 `dispatch`，成功 dict 包成 `types.TextContent`(JSON)；**统一捕获 `ToolPolicyViolation`/`KeyError`/`TypeError`/`ToolError` 全部包成 `isError=true`（脱敏），仅 `asyncio.CancelledError` 放行传播**，禁止任何裸异常逃逸 transport；验收：call_tool 成功路径返回可反序列化结果 + 策略门/未注册名(KeyError)/输入非法(TypeError) 三条路径均返回 isError 不逃逸不执行 handler（spec 四个 call_tool 场景）
- [x] 3.3 `run_stdio(server)`：用官方 SDK `mcp.server.stdio.stdio_server()`，stdin EOF / SIGTERM 优雅退出（不残留挂起任务、不抛未捕获异常）；验收：集成测试用 SDK in-memory/stdio client 跑 list_tools+call_tool 往返，关闭 stdin 后 `run_stdio` 正常返回（spec 场景「stdin EOF 触发优雅退出」）
- [x] 3.4 「server 不持有 LLMBackend」守卫测试：断言 `build_server` / `McpToolsAdapter` 构造签名与运行路径均不接收/调用 `LLMBackend`（ADR-008，spec 场景「server 不持有也不调用 LLMBackend」）
- [x] 3.5 call_tool 异常脱敏端到端：某工具 handler 抛含敏感子串异常 → call_tool 返回错误文本不含原始凭据/IP/identity（spec 场景「call_tool handler 异常经脱敏后返回」）

## 4. 默认工具 mcp surface opt-in（tool-registry-capability-layer MODIFIED）

- [x] 4.1 `tools/default_tools.py`：把 `run_inspector` / `list_inspectors` / `list_targets` 三个 ToolSpec 的 `surfaces` 从 `{"agent"}` 改为 `{"agent", "mcp"}`；`correlate_findings` / `request_more_inspection` 保持不变；验收：更新 tool-registry-capability-layer 既有元数据测试断言 `spec.surfaces == {"agent", "mcp"}`（spec 三个「ToolSpec 元数据」场景）
- [x] 4.2 回归既有脱敏测试：确认 `list_targets` 经 mcp dispatch 后 `ListTargetsOutput.model_dump_json()` 仍不含凭据/IP/identity 子串（TargetSummary M7-safe 脱敏在 mcp surface 同样生效）；验收：复用既有 list_targets 脱敏测试 + 经 McpToolsAdapter.dispatch 路径再跑一遍
- [x] 4.3 `run_inspector` 经 mcp surface 强制 `allow_privileged=False`：验收：non-root 用户跑通，privilege=sudo 的 inspector 经 mcp dispatch 返回空 findings（MCP 不能 opt-in 提权）

## 5. CLI 与 doctor（mcp-cli-command spec）

- [x] 5.1 `cli/` 新增 `mcp` 子命令组 + `hostlens mcp serve`：装配真实 `ToolRegistry`(register_default_tools)+`ToolContext` 工厂，调 `run_stdio`；验收：mcp SDK 已装时 `hostlens mcp serve` 启动 stdio server（spec 场景「mcp SDK 已安装时 serve 启动 stdio server」）
- [x] 5.2 缺依赖优雅退出：`mcp serve` 捕获 `ImportError` → stderr 提示 `pip install "hostlens[mcp]"` + 退出码 1，禁裸 traceback、禁退出码 0 静默成功；验收：**非交互环境无依赖退出 1** 测试（卸载/mock mcp 不可 import 后 `hostlens mcp serve` 退出码==1 且 stderr 含安装提示，spec 场景「mcp SDK 未安装时 serve 退出码 1 且提示安装」）
- [x] 5.3 `cli/__init__.py` 注册 `mcp` 子命令组到主 app；验收：`hostlens mcp --help` 列出 `serve`；`hostlens --help` 列出 `mcp`
- [x] 5.4 doctor 增 `checks.mcp`：可 import→`status="ok"`、不可 import→`status="missing"`（复用既有 `CheckResult` 枚举），**禁止**把 `checks.mcp` 加入 `_is_ready` 白名单；验收：**doctor --json schema 稳定性** —— `hostlens doctor --json` 含 `checks.mcp` 且 status∈{ok,missing}，mcp 缺失时 status==missing 但 doctor 整体 readiness 不失败（spec 场景「doctor --json 含 checks.mcp 状态且非致命」）

## 6. 文档与收尾

- [x] 6.1 README 「MCP Server（M7）」章节从「规划中」改为「已实现」，补 Claude Desktop `mcpServers` 配置片段 + Demo Path；更新里程碑到 M7
- [x] 6.2 docs/ARCHITECTURE.md §M7 标注落地状态；CLAUDE.md §9「当前阶段」追加 M7 交付摘要
- [x] 6.3 跑全量验收：`mypy --strict` 过、`pytest tests/mcp_server/ -v` 全绿、`pytest` 全量回归不破坏既有用例、`hostlens doctor --json` 含 checks.mcp；Demo Path 5 分钟本地复现（`pip install -e ".[mcp]"` → pytest mcp_server → list_tools 仅三件套 + call_tool list_inspectors 返回结构化结果）
