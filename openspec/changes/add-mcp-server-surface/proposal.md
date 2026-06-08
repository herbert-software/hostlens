## 为什么

Hostlens 的项目定位是**双交付形态：CLI + MCP Server**，但当前只兑现了 CLI 一半 —— `src/hostlens/mcp_server/` 至今是空壳（只有 `__init__.py`）。M2 设计 Tool Registry 时已经把这件事预埋好了：`ToolSpec` 带 `surfaces`（agent/mcp/cli policy gate）和独立的 `mcp_description` 文案，所有内置工具的 `mcp_description` 早已写好；§4.10「上来不实现三个 surface adapter —— M2 只做 Layer 1 + Agent adapter，MCP adapter 到 M7」明确把 MCP surface adapter 推迟到了本期。

现在 M5 已把「意图→采集→诊断→推送」整条链路打通，Tool Registry 双层模型稳定，正是补上第二交付形态的时机。MCP Server 让 Hostlens 的只读巡检能力作为标准 MCP 工具被任意远程 LLM（Claude Desktop / 其他 MCP host）调用，这是简历上「同一套 host-agnostic capability 同时暴露给本地 Agent loop 与远程 LLM」的关键证明点。

## 变更内容

- **新增 `mcp_server/tools_adapter.py`**：镜像 `agent/tools_adapter.py` 的 `ToolsAdapter`，把 `surfaces ∋ "mcp"` 的 `ToolSpec` 投影成官方 `mcp` SDK 的 tool definition（`name` / `description=mcp_description` / `inputSchema`），并在投影与 dispatch 两道关口强制执行 §4.10 规则 5/6 的策略校验。
- **新增 `mcp_server/server.py`**：基于官方 `mcp` SDK 的 server 入口，**stdio transport 优先**；注册 `list_tools` / `call_tool` handler，桥接到 `McpToolsAdapter`。
- **新增 `hostlens mcp` CLI 子命令**：启动 MCP server（`hostlens mcp serve`，stdio）；复用 doctor 的环境前置检查。
- **修改默认工具集的 mcp surface 显式 opt-in**：当前所有内置工具都只声明 `surfaces={"agent"}`，无一进入 mcp surface。本期对**只读、非敏感**工具（`list_inspectors`）显式加入 `"mcp"`；对 `sensitive_output=True` 的工具（`run_inspector` / `list_targets` / `request_more_inspection`）做一次显式安全决定（§4.10 规则 1：多注册一个 surface = 一次显式安全决定）—— 详见 design.md「mcp 暴露工具集决策」。
- **填充 `pyproject.toml` 的 `mcp` optional-dependency 组**：当前 `mcp = []` 为空，本期填入官方 `mcp` SDK pin。

**对外契约影响（MCP tool schema 新增 surface）**：

- **新增 MCP tool schema 投影**：`surfaces ∋ "mcp"` 的工具首次对外暴露为 MCP tool definition。投影规则（`name` / `description` 取 `mcp_description` / `inputSchema` 由 Pydantic `model_json_schema()` 生成）是新的对外契约。
- **`ToolSpec` Layer 1 schema 不变**：只新增 surface adapter，不动 `base.py` 的 `ToolSpec` 定义（§4.10 规则 2：ToolSpec 不存 host 专有 JSON Schema，一律 adapter 投影时生成）。
- **默认工具 `surfaces` 集合变化**：部分内置工具的 `surfaces` 从 `{"agent"}` 变为 `{"agent","mcp"}`，属注册元数据变化，需更新 `tool-registry-capability-layer` 对应场景。
- **CLI 命令新增**：`hostlens mcp serve` 子命令。

## 功能 (Capabilities)

### 新增功能
- `mcp-tool-adapter`: MCP surface adapter —— 把 `surfaces ∋ "mcp"` 的 ToolSpec 投影为官方 mcp SDK 的 tool definition；投影时强制 `sensitive_output` 显式声明（None 即拒绝暴露）、dispatch 前强制 side_effects/requires_approval 策略门，错误信息复用 `scrub_exception_message` 脱敏。
- `mcp-server`: MCP Server 入口 —— 基于官方 mcp SDK 的 stdio server，注册 list_tools / call_tool，桥接 McpToolsAdapter；优雅停机；不直接调 LLM（它是被远程 LLM 调用的工具提供方）。
- `mcp-cli-command`: `hostlens mcp` Typer 子命令 —— `hostlens mcp serve`（stdio 启动），含环境前置检查与非交互行为约定。

### 修改功能
- `tool-registry-capability-layer`: 默认工具集对 mcp surface 的显式 opt-in —— 只读非敏感工具加入 `"mcp"`，敏感输出工具按 design 决策处理；`register_default_tools` 的工具 `surfaces` 元数据相应变化。

## 影响

- **新增代码**：`src/hostlens/mcp_server/{tools_adapter.py, server.py}`、`src/hostlens/cli/` 下 `mcp` 子命令模块。
- **修改代码**：`src/hostlens/tools/default_tools.py`（部分工具 `surfaces` 加 `"mcp"`）、`src/hostlens/cli/__init__.py`（注册 mcp 子命令）、`pyproject.toml`（`mcp` optional-dep 组填入 SDK pin）、`doctor`（可选：mcp 依赖可用性检查）。
- **依赖**：新增运行时可选依赖 `mcp` SDK（仅在 `pip install hostlens[mcp]` 时拉取，核心 CLI 不强依赖）。
- **测试**：新增 `tests/mcp_server/` —— adapter 投影/策略门单测、server list_tools/call_tool 集成测试（用 in-memory transport 或 FakeBackend-free 路径，无需真 LLM）。
- **不影响**：Agent loop / agent adapter / Inspector / Notifier / Scheduler 主流程零改动。

## 非目标（Non-Goals）

- **不实现 HTTP transport 的复杂远程鉴权**：本期 **stdio-only** 起步（Claude Desktop 等本地 host 场景）。HTTP transport + OAuth/token 鉴权标记为后续 follow-up，不在本期范围（避免在没有真实远程部署需求时过度设计攻击面）。
- **不实现 Remediation 写工具**：写操作工具属 M9，门控未解锁（需先验证只读诊断准确性）。MCP server 本期只暴露 `side_effects ∈ {none, read}` 的只读工具。
- **不把 Notifier / Inspector / Target 塞进 Tool Registry / MCP**：§4.10 规则 4 —— Notifier 是 Scheduler/Reporter 触发的输出通道，不是 Agent capability；Inspector/Target 是业务插件。MCP 只暴露已注册的 ToolSpec。
- **不改 `ToolSpec` Layer 1 schema**：只加 mcp surface adapter，Layer 1 已在 M2 就位。
- **不实现 MCP resources / prompts**：本期只做 MCP **tools** 一个 primitive；resources/prompts 留待有明确需求时再提案。
- **MCP server 不直接调 LLM**：它是工具提供方，被远程 LLM 调用；因此 prompt caching / backend capabilities 在本期不适用。

## Failure Modes

1. **`mcp` SDK 未安装就执行 `hostlens mcp serve`**：核心 CLI 不强依赖 mcp SDK（optional-dep）。`mcp serve` 入口必须捕获 `ImportError`，以**退出码 1 + 清晰提示**（`pip install hostlens[mcp]`）退出，不得抛裸 traceback；doctor `--json` 应反映 mcp 依赖不可用。
2. **远程 LLM 调用未声明 `sensitive_output` 的工具**：fail-closed 三处对称挡住 —— ① `list_for_mcp()` 投影拒绝 `sensitive_output is None` 的 spec（不出现在 MCP tool 列表）；② `build_server` 构造时 eager 调一次 `list_for_mcp()` 自检，混入此类 spec 则 **server 起不来**（不进入运行态）；③ `dispatch` 步骤 3 的对称门挡住「绕过 list_tools 直接 call_tool」路径。三处均 **raise 不静默跳过**。
3. **远程 LLM 调用 write/destructive 或 requires_approval 工具**：dispatch 前策略门 raise `ToolPolicyViolation`，包装成 MCP 错误结果返回（`isError=true`），不执行 handler。本期默认工具集无此类工具，属防御性边界。
4. **handler 抛异常泄漏 secret 进远程 LLM context**：所有 handler 异常经 `scrub_exception_message` 脱敏后才包进 MCP 错误结果；`CancelledError` / `ToolPolicyViolation` 按 agent adapter 既有语义传播，不误包。
5. **stdio transport 在 daemon/无 TTY 环境下被误用为长驻服务**：本期 stdio server 设计为前台进程（由 MCP host 拉起管理生命周期）；优雅响应 SIGTERM/EOF（stdin 关闭即退出），不残留僵尸进程。

## Operational Limits

- **并发预算**：MCP server 单连接串行处理 call_tool（stdio 单管道）；每个 call_tool 复用 `ToolContext` 工厂的 per-call `cancel` event，超时由 `spec.timeout` 控制（继承 agent adapter 既有 `asyncio.wait_for` 机制）。
- **内存预算**：server 无额外常驻缓存；tool 列表在启动时一次性投影（工具数量级 < 20），可缓存于内存。
- **超时设置**：单工具 dispatch 超时沿用 `ToolSpec.timeout`；无全局 server 超时（生命周期由 MCP host 控制）。stdin EOF 触发优雅退出。

## Security & Secrets

- **不引入新密钥**：MCP server 复用 `Settings` 既有配置（target 凭据走 `HOSTLENS_*` env 注入路径），stdio-only 无网络监听端口，攻击面不扩大。
- **脱敏**：复用 agent adapter 的同一 `scrub_exception_message` 函数（路径 / IPv4 / IPv6 / 凭据 / identity / email 多类正则）对所有 MCP 错误结果脱敏 —— 远程 LLM context 与本地 Agent loop context 同等敏感；不重新声明正则，行为与 agent surface 一致。
- **fail-closed 暴露**：`sensitive_output is None`（未显式声明）的工具拒绝进入 MCP 列表（§4.10 规则 6）；只读非敏感工具显式 opt-in，敏感工具暴露需 design 单独决策记录。
- **攻击面**：stdio transport 不开网络端口，仅本机进程间管道；相比 HTTP transport 显著收窄攻击面（HTTP + 鉴权列为 Non-Goal）。

## Cost / Quota Impact

- **零 LLM token 消耗**：MCP server 不直接调 Anthropic API（它是被远程 LLM 调用的工具提供方）。token 成本由调用方 LLM host 承担，不计入 Hostlens 的 Anthropic 配额。
- **API 调用频次**：MCP server 自身不产生 Anthropic API 调用；工具 handler 内部走 Inspector 采集（SSH/local exec），不调 LLM（§4.2 Inspector 不能调 LLM）。
- **prompt caching 不适用**：本期 server 不构造 Anthropic 请求，无 cache_control 注入需求。

## Demo Path

无需 SSH / 无需付费 API 的 5 分钟本地复现：

```bash
pip install -e ".[mcp]"           # 安装含 mcp SDK 的可选依赖
hostlens doctor --json            # 确认 mcp 依赖可用（checks.mcp）
# 方式 A：用官方 mcp SDK 自带的 stdio client 烟测（测试内置）
pytest tests/mcp_server/ -v       # list_tools 返回仅含 mcp-surface 工具；
                                  # call_tool list_inspectors 返回内置 inspector 列表（local target，无 SSH）
# 方式 B：手动连 Claude Desktop（可选，需配置 mcpServers）
hostlens mcp serve                # stdio 前台启动，由 host 拉起
```

核心验证点：`list_tools` 输出**不含** `sensitive_output is None` 或 agent-only 工具；`call_tool` 对只读工具（`list_inspectors`）成功返回结构化结果；对 `sensitive_output=True` 的 `list_targets` 经 mcp `call_tool` 路径后，结果 `model_dump_json()` **不含**凭据/IP 子串（验证敏感工具经 MCP 暴露时脱敏前提真实生效，与 tasks 4.2 对齐）；对策略门拦截的工具返回 `isError=true` 而非执行。
