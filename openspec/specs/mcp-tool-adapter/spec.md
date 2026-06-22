# mcp-tool-adapter 规范

## 目的

定义 Hostlens 双层 Capability Registry 的 MCP surface adapter（Layer 2，M7）：`hostlens.mcp_server.tools_adapter.McpToolsAdapter` 把 `surfaces ∋ "mcp"` 的 ToolSpec 投影成官方 `mcp` SDK 的 tool definition（`description` 取 `mcp_description`、`inputSchema` 由 Pydantic 投影生成），在投影与 dispatch 两侧对称强制 fail-closed `sensitive_output` 门（None 即拒绝暴露给远程 LLM），dispatch 复刻 agent adapter 的九步 policy gate 并复用 `scrub_exception_message` 脱敏 handler 异常。本规范不覆盖 MCP server 入口与 CLI（由各自 spec 描述）。
## 需求
### 需求:`McpToolsAdapter` 必须把 `surfaces ∋ "mcp"` 的 ToolSpec 投影成 mcp SDK tool definition

`hostlens.mcp_server.tools_adapter.McpToolsAdapter` 必须提供 `list_for_mcp()`，遍历 `registry.list_for("mcp")`，把每个 ToolSpec 投影成官方 `mcp` SDK 的 tool definition（字段 `name` ← `spec.name`；`description` ← `spec.mcp_description`；`inputSchema` ← `spec.input_schema.model_json_schema()`）。`description` 必须取 `mcp_description`，**禁止**取 `agent_description`（§4.10 三套 surface 文案分离）。`inputSchema` 必须由 Pydantic 投影时生成，**禁止**从 ToolSpec 读取任何 host-specific JSON Schema（§4.10 规则 2）。

构造接受 `ToolRegistry` 与 `Callable[[], ToolContext]` 工厂；工厂在**每次 dispatch 调用**时调用，保证 per-call `cancel: asyncio.Event` 不跨调用共享。

#### 场景:投影只输出 mcp surface 工具且 description 取 mcp_description

- **当** registry 含 specs：A(surfaces={"agent"}, mcp_description="A-mcp") / B(surfaces={"agent","mcp"}, mcp_description="B-mcp", agent_description="B-agent") / C(surfaces={"mcp"}, mcp_description="C-mcp")
- **那么** `list_for_mcp()` 返回的工具列表必须只含 B 与 C（按 name 字典序），**不含** A
- **且** B 对应条目的 `description` 必须等于 `"B-mcp"`，**不**等于 `"B-agent"`
- **且** 每个条目的 `inputSchema` 必须等于对应 `spec.input_schema.model_json_schema()` 的输出

#### 场景:空 registry 或无 mcp surface 工具时返回空列表

- **当** registry 为空，或所有 spec 的 surfaces 都不含 "mcp"
- **那么** `list_for_mcp()` 返回空列表，**不**抛异常

### 需求:`McpToolsAdapter` 投影时必须 fail-closed 拒绝 `sensitive_output is None` 的工具

`list_for_mcp()` 对每个待投影的 spec 必须断言 `spec.sensitive_output is not None`。若某个 `surfaces ∋ "mcp"` 的 spec 的 `sensitive_output is None`（未显式声明），`list_for_mcp()` 必须 raise `ToolPolicyViolation(tool_name=spec.name, surface="mcp", violated_field="sensitive_output", reason="sensitive_output_not_declared")`（在投影阶段尽早暴露），**禁止**静默跳过该 spec（§4.10 规则 6：MCP 暴露的工具必须显式声明 sensitive_output，缺省禁止暴露）。构造必须传齐四个 keyword-only 必填字段。

#### 场景:mcp surface 工具缺失 sensitive_output 声明则投影 raise

- **当** registry 含 spec X(name="x", surfaces={"mcp"}, sensitive_output=None)
- **那么** 调用 `list_for_mcp()` 必须 raise `ToolPolicyViolation`，`tool_name == "x"`、`surface == "mcp"`、`violated_field == "sensitive_output"`、`reason == "sensitive_output_not_declared"`
- **且** 异常**不**被静默吞掉，**不**返回一个跳过了 X 的部分列表

#### 场景:mcp surface 工具显式声明 sensitive_output 则正常投影

- **当** registry 含 spec Y(surfaces={"mcp"}, sensitive_output=True) 与 Z(surfaces={"mcp"}, sensitive_output=False)
- **那么** `list_for_mcp()` 正常返回含 Y 与 Z 的列表，不抛异常

### 需求:`McpToolsAdapter.dispatch` 必须执行 mcp surface 策略门并复用脱敏语义

`McpToolsAdapter.dispatch(name, args_json, ctx=None)` 必须按序执行（所有 `ToolPolicyViolation` 构造必须传齐四个 keyword-only 必填字段 `tool_name` / `surface` / `violated_field` / `reason`，缺一即 `TypeError`；`reason` 必须取 `hostlens.core.exceptions.ToolPolicyReason` 的合法成员）：

(1) `registry.get(name)`，`KeyError` 传播不包装；
(2) surface 门 —— `"mcp" not in spec.surfaces` 则 raise `ToolPolicyViolation(tool_name=name, surface="mcp", violated_field="surfaces", reason="not_exposed_to_surface")`；
(3) **sensitive_output 门（fail-closed 的 dispatch 侧对称门）** —— `spec.sensitive_output is None` 则 raise `ToolPolicyViolation(tool_name=name, surface="mcp", violated_field="sensitive_output", reason="sensitive_output_not_declared")`。此门与 `list_for_mcp()` 投影侧的 fail-closed 检查**两条路径对称**，专门挡住「远程 LLM 不先调 list_tools、直接 call_tool 一个 `surfaces ∋ "mcp"` 且 `sensitive_output is None` 的工具」的绕过路径（§4.10 规则 6 缺省禁止暴露，必须在 dispatch 路径同样成立）；
(4) side_effects 门 —— `spec.side_effects ∈ {"write","destructive"}` 则 raise `ToolPolicyViolation(tool_name=name, surface="mcp", violated_field="side_effects", reason="side_effects_not_permitted")`（MCP 表面**永久只读**，写类 ToolSpec 永久被拒）；
(5) approval 门 —— `spec.requires_approval is True` 则 raise `ToolPolicyViolation(tool_name=name, surface="mcp", violated_field="requires_approval", reason="approval_flow_not_supported")`（MCP 表面**永久**无 approval flow；真审批属 Remediation 子系统）；
(6) 输入 schema 校验 dict→typed model，失败 raise `TypeError`；
(7) 解析 ctx（默认走 context 工厂）并调 handler，`spec.timeout` 非空时包 `asyncio.wait_for`；
(8) 输出 schema sanity 检查（`isinstance`，不符 raise `ToolError`）；
(9) 返回 `result.model_dump()`。

handler 抛出的异常（除 `ToolPolicyViolation` / `KeyError` / `asyncio.CancelledError` 外）必须包成结构化错误包络，且包络内所有字符串值必须经 `hostlens.agent.tools_adapter.scrub_exception_message` 脱敏。`ToolPolicyViolation` / `KeyError` / `asyncio.CancelledError` 必须原样传播，**禁止**误包成错误包络。

**`spec.timeout` 触发的 `asyncio.TimeoutError`（在 Python 3.11 即 builtin `TimeoutError`，**不是** `CancelledError` 子类）必须落在错误包络捕获范围内**：步骤 7 的 `asyncio.wait_for(...)` 必须置于错误包络 `try` 块**之内**（镜像 agent adapter），使超时被 dispatch 自身捕获、脱敏、以 `error_kind="TimeoutError"` 的 `is_error` dict 返回，**禁止**裸 `TimeoutError` 逃逸 dispatch。`TimeoutError` **不在**传播豁免集（`ToolPolicyViolation` / `KeyError` / `CancelledError`）内。

#### 场景:dispatch 非 mcp surface 工具触发 surface 策略门

- **当** spec Q 的 surfaces={"agent"}（不含 mcp），`await adapter.dispatch("q", {...}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`surface == "mcp"`、`violated_field == "surfaces"`、`reason == "not_exposed_to_surface"`
- **且** handler **不**被调用

#### 场景:dispatch write/destructive 工具触发 side_effects 策略门

- **当** spec 的 surfaces 含 "mcp" 但 side_effects="write"，`await adapter.dispatch(name, {...}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`surface == "mcp"`、`violated_field == "side_effects"`、`reason == "side_effects_not_permitted"`，handler 不被调用

#### 场景:dispatch sensitive_output is None 的 mcp 工具触发 fail-closed 门（不依赖 list_tools 先拦）

- **当** spec 的 surfaces 含 "mcp"、side_effects="read"、requires_approval=False 但 `sensitive_output is None`，**直接** `await adapter.dispatch(name, {...}, ctx)`（不先调 `list_for_mcp()`）
- **那么** 必须 raise `ToolPolicyViolation`，`surface == "mcp"`、`violated_field == "sensitive_output"`、`reason == "sensitive_output_not_declared"`，handler **不**被调用
- **理由**：fail-closed 必须在 dispatch 路径与投影路径**两侧对称**，否则远程 LLM 绕过 list_tools 直调即暴露未声明工具

#### 场景:dispatch requires_approval 工具触发 approval 策略门

- **当** spec 的 surfaces 含 "mcp"、side_effects="read"、sensitive_output=False 但 requires_approval=True
- **那么** 必须 raise `ToolPolicyViolation`，`surface == "mcp"`、`violated_field == "requires_approval"`、`reason == "approval_flow_not_supported"`，handler 不被调用

#### 场景:dispatch 输入 args_json 非法触发 TypeError

- **当** mcp surface 的合法工具被 dispatch，但 `args_json` 缺必填字段 / 类型不符，无法通过 `spec.input_schema.model_validate`
- **那么** dispatch 必须 raise `TypeError`（这是类型错误而非策略拒绝），handler **不**被调用

#### 场景:dispatch handler 返回错误类型触发 ToolError

- **当** mcp surface 工具的 handler 返回的对象**不是** `spec.output_schema` 实例（handler 契约违例）
- **那么** dispatch 必须 raise `ToolError`（与步骤 6 的输入 TypeError 区分：这是 handler/adapter code bug，须 fail-loud）

#### 场景:handler 异常包成脱敏错误包络

- **当** 一个 mcp surface 工具的 handler 抛 `RuntimeError("connect failed user=admin@10.0.0.5 sk-abcdefghijklmnopqrstuvwx")`
- **那么** dispatch 返回 dict 包络 `{"is_error": True, "error_kind": "RuntimeError", "tool_name": name, "message": ..., "cause": ...}`
- **且** 包络内 `message` 与 `cause` 字符串**不**含 `"admin"` / `"10.0.0.5"` / `"sk-abcdefghijklmnopqrstuvwx"` 任意子串（经 scrub_exception_message 脱敏）

#### 场景:ToolPolicyViolation 与 CancelledError 不被误包

- **当** handler 抛 `asyncio.CancelledError`（协作取消）
- **那么** dispatch 必须让 `CancelledError` 原样传播，**不**返回错误包络
- **当** handler 抛 `ToolPolicyViolation`
- **那么** dispatch 必须让其原样传播，**不**返回错误包络

#### 场景:成功 dispatch 返回 model_dump

- **当** mcp surface 的只读工具 dispatch 成功，handler 返回合法 output_schema 实例
- **那么** dispatch 返回 `result.model_dump()`（`isinstance(x, dict)` 为真），供 server 层包成 mcp content block
- **且** 返回 dict **不含** `is_error` 键（与错误包络 dict 显式区分 —— 二者都是 dict，server 层据 `is_error` 键判成功/失败）
