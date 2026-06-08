## 新增需求

### 需求:MCP Server 必须基于官方 mcp SDK 暴露 list_tools 与 call_tool 并桥接 McpToolsAdapter

`hostlens.mcp_server.server` 必须提供一个基于官方 `mcp` SDK 的 server 工厂（例如 `build_server(registry, context_factory) -> Server`），注册两个 handler：

- **list_tools handler**：返回 `McpToolsAdapter.list_for_mcp()` 投影的 tool 列表（mcp SDK `types.Tool`）。
- **call_tool handler**：接收 `(name, arguments: dict)`，委托 `McpToolsAdapter.dispatch(name, arguments, ctx)`。dispatch 返回的成功 dict 包成 mcp SDK 的成功 content（如 `types.TextContent` 承载 JSON 序列化结果）；dispatch 返回的错误包络（`is_error=True`）必须包成 mcp 的错误结果（`isError=true`）。dispatch **抛出**的异常 —— `ToolPolicyViolation`（策略门）/ `KeyError`（未注册工具名，dispatch 步骤 1 传播）/ `TypeError`（输入 schema 校验失败，步骤 6）/ `ToolError`（输出 schema 不符，步骤 8）—— **必须**全部被 call_tool handler 捕获并包成 `isError=true`（错误文本经 `scrub_exception_message` 脱敏），**禁止**让任何裸异常逃逸到 SDK transport 层；唯一例外是 `asyncio.CancelledError`，必须放行传播以让 server 优雅关闭。（注：`spec.timeout` 触发的 `asyncio.TimeoutError` 由 dispatch 内部包络成 `is_error` dict 返回、**不**作为裸异常抛出，故不在此捕获集 —— 见 mcp-tool-adapter dispatch 契约。）

**`build_server` 必须在构造时立即 eager 调用一次 `McpToolsAdapter.list_for_mcp()` 做投影自检**：若 registry 内存在 `surfaces ∋ "mcp"` 且 `sensitive_output is None` 的 spec，`list_for_mcp()` 的 fail-closed 检查会 raise，使 `build_server` **直接失败、server 对象不被创建**（「server 起不来好过运行时某工具裸奔」的生效落点 —— 配合 dispatch 侧对称门，fail-closed 在 list_tools 投影、build_server 自检、call_tool dispatch 三处一致）。

MCP Server **禁止**直接构造 Anthropic 请求或调用任何 LLMBackend —— 它是被远程 LLM 调用的工具提供方，不是 LLM 调用方（§4.2 下游红线）。

#### 场景:list_tools 只返回 mcp surface 工具

- **当** registry 经 `register_default_tools` 装配（三件套 `list_inspectors`/`list_targets`/`run_inspector` 现含 mcp surface），**并额外注册一个 agent-only 测试 spec**（`name="agent_only_probe"`, `surfaces={"agent"}`, sensitive_output 显式声明），构造 server，调用 list_tools handler
- **那么** 返回的 tool 列表必须含 `list_inspectors`/`list_targets`/`run_inspector`
- **且** 必须**不含** `agent_only_probe`（surface 过滤真实生效 —— 该 fixture 确已注册于同一 registry，断言可证伪）
- **理由**：`correlate_findings`/`request_more_inspection` 由独立的 `register_diagnostician_tools` 注册、不进 `register_default_tools` 的 registry，对它们断言「不含」是 vacuous（恒真无法 fail）；必须用一个**确已注册**的 agent-only spec 才能真正验证 surface 过滤

#### 场景:call_tool 未注册工具名（KeyError）被捕获返回 isError 不逃逸

- **当** 远程 LLM 对一个**不在** list_tools 列表里的工具名发起 call_tool（dispatch 步骤 1 `registry.get(name)` 抛 `KeyError`）
- **那么** call_tool handler 必须捕获该 `KeyError` 并返回 `isError=true` 的 mcp 错误结果（文本经脱敏），**禁止**让裸 `KeyError` 逃逸到 SDK transport 层

#### 场景:call_tool 输入参数非法（TypeError）被捕获返回 isError 不逃逸

- **当** 对合法 mcp 工具发起 call_tool 但 `arguments` 无法通过输入 schema 校验（dispatch 步骤 6 抛 `TypeError`）
- **那么** call_tool handler 必须捕获该 `TypeError` 并返回 `isError=true`，**禁止**裸 `TypeError` 逃逸 transport

#### 场景:build_server 对 sensitive_output is None 的 mcp 工具 eager 失败

- **当** registry 含一个 `surfaces ∋ "mcp"` 且 `sensitive_output is None` 的 spec，调用 `build_server(registry, context_factory)`
- **那么** `build_server` 必须 raise（eager 投影自检触发 `list_for_mcp()` 的 fail-closed），server 对象**不**被创建 —— 启动即失败，不进入运行态

#### 场景:call_tool 成功路径返回结构化结果

- **当** 经 list_inspectors 的 call_tool 调用（registry 含真实 inspector manifest），arguments={}
- **那么** call_tool handler 返回 mcp 成功结果，content 承载 `list_inspectors` 的结构化 inspector 列表（可 JSON 反序列化回 dict）
- **且** 结果**未**标记 `isError`

#### 场景:call_tool 策略门拦截返回 isError 而非执行

- **当** 对一个不在 mcp surface 的工具名发起 call_tool（或 dispatch 触发 ToolPolicyViolation）
- **那么** call_tool handler 返回 `isError=true` 的 mcp 错误结果，**不**执行该工具 handler
- **且** 裸 `ToolPolicyViolation` 异常**不**逃逸到 transport 层

#### 场景:call_tool handler 异常经脱敏后返回

- **当** 某工具 handler 抛含敏感子串的异常
- **那么** call_tool 返回的错误结果文本**不**含原始凭据/IP/identity 子串（dispatch 层 scrub_exception_message 已脱敏）

### 需求:MCP Server 必须走 stdio transport 并优雅响应连接关闭

`hostlens.mcp_server.server` 必须提供 stdio 运行入口（例如 `async def run_stdio(server) -> None`），使用官方 SDK 的 stdio transport（`mcp.server.stdio.stdio_server()`）。**EOF 是 normative 的优雅退出路径**：stdin 收到 EOF（MCP host 关闭连接）时 server 必须优雅退出（释放 transport，`run_stdio` 协程正常返回、不抛未处理异常、不残留挂起任务）。**SIGTERM 路径**：本期生命周期由 MCP host 管理（Decision 6），server **不**安装自定义 SIGTERM handler、**不**自行 daemon 化；收到 SIGTERM 时进程及时终止、不打印未处理异常 traceback（无持久化状态需清理，故无需自定义优雅停机逻辑 —— 区别于 `schedule daemon`）。本期**禁止**实现 HTTP transport 与远程鉴权（Non-Goal）。

#### 场景:stdin EOF 触发优雅退出

- **当** stdio server 运行中，stdin 被关闭（EOF）
- **那么** `run_stdio` 必须正常返回（协程结束），**不**抛未捕获异常、**不**残留挂起任务

#### 场景:SIGTERM 终止不残留 traceback

- **当** `hostlens mcp serve` 子进程运行中收到 SIGTERM
- **那么** 进程必须终止（进程退出 / 不再存活），**且** stderr **不**含字面 `Traceback (most recent call last)`（无未处理异常 traceback）
- **理由**：本期不装自定义 SIGTERM handler、依赖默认信号处置（默认即终止），无持久化状态需 flush；判据用可观测的「进程退出 + stderr 无 traceback 字面」而非不确定的时限断言

#### 场景:server 不持有也不调用 LLMBackend

- **当** 审视 `hostlens.mcp_server.server` 与 `McpToolsAdapter` 的依赖
- **那么** 二者构造签名与运行路径**均不**接收 / 持有 / 调用 `LLMBackend`（ADR-008：backend 是 AgentLoop 私有依赖，不进 ToolContext / 不进 MCP server）
