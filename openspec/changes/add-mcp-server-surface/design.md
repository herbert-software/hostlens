## 上下文

M2 落地 Tool Registry 时已建立双层 Capability 模型：Layer 1 `ToolSpec`（host-agnostic，带 `surfaces` policy gate 与三套独立描述文案 `agent_description`/`mcp_description`/`cli_help`）+ Layer 2 surface adapter。M2 只交付了 agent adapter（`agent/tools_adapter.py`），并在 §4.10 明确「MCP adapter 到 M7」。

当前状态：

- `src/hostlens/mcp_server/` 只有空 `__init__.py`。
- `agent/tools_adapter.py` 的 `ToolsAdapter` 是成熟的镜像模板：`list_for_agent()` 投影 + `dispatch()` 八步策略门 + `scrub_exception_message()` 脱敏。
- 所有内置工具（`list_inspectors`/`list_targets`/`run_inspector`/`correlate_findings`/`request_more_inspection`）的 `mcp_description` 已写好，但 `surfaces` 一律只含 `"agent"`，无一进 mcp surface。
- `ToolRegistry.list_for("mcp")` 已支持 surface 过滤（M2 即就位），当前返回空列表。
- `pyproject.toml` 有空的 `mcp = []` optional-dependency 组。
- `TargetSummary` 输出 schema 在 tool-registry spec 中已标注「M2 + **M7-safe**」—— 脱敏设计预留了 M7 经 MCP 暴露 `list_targets` 的场景。

约束：官方 `mcp` SDK（已在技术栈锁定）；async-first；MCP server 不直接调 LLM（§4.2 红线下游：它是被远程 LLM 调用的工具提供方）；不动 Layer 1 `ToolSpec`。

## 目标 / 非目标

**目标：**

- 新增 `mcp_server/tools_adapter.py`：`McpToolsAdapter`，镜像 agent adapter，投影 `surfaces ∋ "mcp"` 的 ToolSpec 成官方 mcp SDK tool definition，并强制 §4.10 规则 5/6 的 fail-closed 策略校验。
- 新增 `mcp_server/server.py`：基于官方 `mcp` SDK 的 stdio server，桥接 adapter。
- 新增 `hostlens mcp serve` CLI 子命令。
- 对只读巡检三件套（`list_inspectors`/`list_targets`/`run_inspector`）显式 opt-in `"mcp"` surface。
- 复用 agent adapter 的脱敏与异常语义，不重复造轮子。

**非目标：**

- HTTP transport + 远程鉴权（stdio-only 起步）。
- MCP resources / prompts primitive（本期只做 tools）。
- 写工具 / Remediation（M9 门控）。
- 把 Diagnostician 内部编排工具（`correlate_findings`/`request_more_inspection`）暴露给远程 LLM —— 它们是 agent loop 私有的循环控制工具，单独调用语义不完整。
- 改 `ToolSpec` Layer 1 schema。

## 决策

### 决策 1：`McpToolsAdapter` 镜像 agent adapter，而非抽出共享基类

**选择**：新写一个 `McpToolsAdapter`（独立 class，复用 `scrub_exception_message` 自由函数），结构与 `ToolsAdapter` 平行，但策略门按 mcp surface 调整。

**理由**：

- 两个 adapter 的**投影输出格式不同**：agent 投影成 Anthropic `{name, description, input_schema}`；mcp 投影成 mcp SDK 的 `types.Tool(name=..., description=..., inputSchema=...)`。description 取的字段也不同（`agent_description` vs `mcp_description`）。
- 两个 adapter 的**策略门取值不同**：agent surface 门检查 `"agent" in surfaces`；mcp 门检查 `"mcp" in surfaces` 且额外强制 `sensitive_output is not None`（§4.10 规则 6 —— MCP 暴露的工具必须显式声明 sensitive_output，缺省禁止暴露）。
- 过早抽共享基类会把两套 surface 的策略差异塞进条件分支，违反 §4.10「软分类一定失控」的警告。`scrub_exception_message` 这个**纯函数**值得复用（已在 agent adapter 模块导出），但 adapter class 不强行共享。

**替代方案**：抽 `BaseSurfaceAdapter` 模板方法 —— 拒绝，理由如上；surface 策略是 policy gate 不是可参数化的 hint。

### 决策 2：fail-closed —— `sensitive_output is None` 在 `list_tools` 投影阶段就拒绝

**选择**：`McpToolsAdapter.list_for_mcp()` 遍历 `registry.list_for("mcp")` 时，对每个 spec 断言 `spec.sensitive_output is not None`；若为 `None`（未显式声明），**raise**（server 启动首次投影即暴露 bug），不静默 skip。

**理由**：§4.10「`sensitive_output` 默认 `None` 而不是 `False`：`False` 会让"忘记声明"和"显式声明无敏感输出"无法区分」。MCP 把工具暴露给**远程** LLM，比 agent surface 风险更高，必须 fail-closed。fail-closed 在**三处对称生效**：`list_for_mcp()` 投影时 raise、`build_server` 构造时 eager 调一次 `list_for_mcp()` 自检（使「server 起不来好过运行时裸奔」真正成立 —— `list_for_mcp()` 本身只在 list_tools 调用时跑，故必须由 build_server 主动 eager 触发）、以及 `dispatch` 步骤 3 的对称门（挡住 call_tool 绕过 list_tools 直调 `sensitive_output is None` 工具的路径）。三者缺一，fail-closed 都有缺口。

**替代方案**：投影时静默跳过 `sensitive_output is None` 的 spec —— 拒绝，静默跳过会让"我注册了工具但 MCP 列表里没有"变成难查的幽灵 bug，违反「不静默放行」。

### 决策 3：dispatch 策略门复用 agent adapter 的八步，但 surface 门改 mcp + 放行 read

**选择**：`McpToolsAdapter.dispatch()` 复刻 agent adapter 的步骤，差异：

- 步骤 2 surface 门：`"mcp" not in spec.surfaces` → raise `ToolPolicyViolation(surface="mcp", violated_field="surfaces")`。
- 步骤 3 side_effects 门：拒绝 `write`/`destructive`（与 agent 一致，本期只读）。
- 步骤 4 approval 门：拒绝 `requires_approval=True`（与 agent 一致，本期无审批流）。
- 步骤 5–8：与 agent adapter 完全一致（输入 schema 校验 / timeout / 输出 schema sanity / 错误包络脱敏 / `CancelledError`+`ToolPolicyViolation`+`KeyError` 不误包）。
- 返回值：dispatch 返回 `result.model_dump()`；由 server 层包成 mcp SDK 的 `list[types.ContentBlock]`（成功）或 `isError=true`（策略门/异常包络）。

**理由**：策略门逻辑是 surface 无关的安全语义，agent adapter 已实测稳定；mcp 只需替换 surface 字面量。错误脱敏对远程 LLM context 同等重要。

### 决策 4：只读巡检三件套显式 opt-in mcp surface

**选择**：`register_default_tools` 装配时，把以下三个工具的 `surfaces` 从 `{"agent"}` 改为 `{"agent", "mcp"}`：

| ToolSpec | 暴露 mcp 理由 | 安全前提 |
|---|---|---|
| `list_inspectors` | 远程 LLM 需先知道有哪些 inspector | `sensitive_output=False`，无敏感数据 |
| `list_targets` | 远程 LLM 需知道可巡检的 target | `TargetSummary` 已 M7-safe 脱敏（凭据/IP/identity 整条 skip，spec §需求:TargetSummary 输出 schema 必须脱敏） |
| `run_inspector` | MCP 的核心价值 = 远程 LLM 驱动只读巡检 | `side_effects="read"`；non-CLI surface 强制 `allow_privileged=False`（Agent/MCP 不能 opt-in 提权） |

保持 agent-only（不进 mcp）：`correlate_findings` / `request_more_inspection` —— Diagnostician loop 内部编排工具，远程单独调用语义不完整。

**理由**：用户已确认「只读巡检三件套」；三者均 read-only 且各有既成的脱敏/降权保护。`sensitive_output=True` 的两个（`list_targets`/`run_inspector`）是有意识的暴露决定，安全前提已分别记录。

**替代方案**：仅暴露 `list_inspectors`（极小）—— 拒绝，MCP server 价值过薄，无法展示「远程 LLM 驱动巡检」的核心故事。

### 决策 5：`mcp` 为 optional-dependency，缺失时 `hostlens mcp serve` 优雅退出

**选择**：`pyproject.toml` 的 `[project.optional-dependencies]` 把 `mcp = []` 填成 `mcp = ["mcp>=<pin>"]`；核心 CLI 不强依赖。`cli/mcp` 子命令在 import mcp SDK 时捕获 `ImportError`，以退出码 1 + 提示 `pip install "hostlens[mcp]"` 退出，不抛裸 traceback。doctor 增加 `checks.mcp`（mcp SDK 可 import 与否）。

**理由**：MCP 是第二交付形态，不应让只用 CLID 的用户被迫装 mcp SDK。继承全局 CLAUDE.md「doctor 把环境前置检查显式化」「非交互缺前提直接退 1 不静默成功」。

### 决策 6：stdio server 生命周期由 MCP host 管理，优雅响应 EOF/SIGTERM

**选择**：`hostlens mcp serve` 是前台进程，走官方 SDK 的 stdio transport（`mcp.server.stdio.stdio_server()`）；stdin EOF（host 关闭连接）或 SIGTERM 触发优雅退出，不残留子进程。不实现自己的 daemon 化（区别于 `schedule daemon`）。

**理由**：MCP stdio 约定就是 host（Claude Desktop 等）拉起并管理子进程生命周期。自己 daemon 化会与 host 的进程管理冲突。

## 风险 / 权衡

- **[远程 LLM 通过 `run_inspector` 触发非预期命令执行]** → 缓解：`run_inspector` 只能跑已注册的 Inspector（YAML manifest 定义的固定命令），不是任意命令执行；non-CLI surface 强制 `allow_privileged=False`；Inspector 是 SOT，命令面被 manifest 封死（§4.2）。攻击面 = 已声明的只读巡检命令集，不是 shell。
- **[`sensitive_output=True` 工具输出泄漏进远程 LLM context]** → 缓解：`list_targets` 走 `TargetSummary` 既有脱敏（M7-safe，凭据/IP 整条 skip）；`run_inspector` 的 finding evidence 经 `_str_only` 投影；所有 dispatch 异常经 `scrub_exception_message`。残余风险：Inspector finding message 本身可能含主机数据 —— 这是 read-only 巡检的固有属性，已由 sensitive_output=True 标记并经用户显式决定暴露。
- **[`mcp` SDK 版本漂移破坏投影格式]** → 缓解：pin `mcp>=<lower>`；adapter 投影只用 SDK 的稳定 `types.Tool` / `call_tool` 契约；集成测试用 SDK 自带 in-memory/stdio client 回归，SDK 升级时 CI 立即暴露。
- **[adapter 双写漂移：agent 与 mcp adapter 策略门不一致]** → 缓解：dispatch 策略门差异仅 surface 字面量，其余步骤逐行对齐 agent adapter；测试用同一组 fixture spec 跨两个 adapter 断言一致的拒绝行为。
- **[stdio server 阻塞在同步 IO]** → 缓解：全程 async（官方 SDK stdio_server 是 async context manager）；handler 内 CPU 工作走既有 `asyncio.to_thread`（Inspector runner 已遵守）。

## 迁移计划

- 纯增量：新增模块 + 三个工具 `surfaces` 加 `"mcp"` + 填 optional-dep。无数据迁移、无破坏性契约变更。
- agent surface 行为零变化（三件套仍在 agent surface；只是**额外**进了 mcp surface）。
- 回滚：移除 mcp 子命令注册 + 把三个工具 `surfaces` 还原为 `{"agent"}` 即可；无持久化状态需清理。
- 部署：`pip install "hostlens[mcp]"` → `hostlens doctor --json` 确认 `checks.mcp` → 配置 MCP host（Claude Desktop `mcpServers`）指向 `hostlens mcp serve`。

## 待解决问题

- **官方 `mcp` SDK pin 下界待定**：tasks 1.1 实现时查最新稳定版定 `mcp>=<下界>` 并回填；spec 阶段不锁死具体版本号。
- **HTTP transport + 鉴权的具体形态**：本期 Non-Goal，但若后续有远程部署需求，需单独提案决定 token/OAuth 方案与监听面收窄策略。
- **MCP resources（暴露报告历史为 resource）**：是否把 `reports` 持久化历史作为 MCP resource 暴露 —— 留待有需求时提案，本期只做 tools。
