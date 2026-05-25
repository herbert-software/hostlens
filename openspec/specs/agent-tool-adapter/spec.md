# agent-tool-adapter 规范

## 目的

定义 Hostlens 双层 Capability Registry 的 Agent surface adapter（Layer 2）：`hostlens.agent.tools_adapter.ToolsAdapter` 把 `surfaces ∋ "agent"` 的 ToolSpec 投成 Anthropic Messages API `tool_use` schema，在 dispatch 前强制 policy gate 校验，包装 handler 异常为 tool_error dict，并保证投影结构稳定性以匹配 Anthropic 服务端的 prompt cache。本规范不覆盖 M7 MCP adapter 与 CLI adapter（由各自 spec 描述）。

## 需求

### 需求:`ToolsAdapter` 必须把 ToolSpec 投成 Anthropic `tool_use` schema

`hostlens.agent.tools_adapter.ToolsAdapter(registry: ToolRegistry, context_factory: Callable[[], ToolContext])` 必须提供 `list_for_agent() -> list[dict[str, Any]]` 方法，把 `registry.list_for("agent")` 返回的每个 ToolSpec 投成符合 Anthropic Messages API `tool_use` 协议的 dict：

```python
{
    "name": spec.name,
    "description": spec.agent_description,
    "input_schema": spec.input_schema.model_json_schema(),
}
```

输出列表必须按 ToolSpec `name` 字典序排序（保证 Anthropic prompt caching 命中：列表顺序变 → cache miss）。

#### 场景:投影输出符合 Anthropic schema

- **当** registry 含 3 个首批 ToolSpec，调用 `adapter.list_for_agent()`
- **那么** 返回值必须是 length=3 的 list，每个元素是 dict 含 `name` / `description` / `input_schema` 三个 key（且**只**含这三个）

#### 场景:投影列表按 name 字典序排序

- **当** registry 注册顺序为 `run_inspector` → `list_inspectors` → `list_targets`
- **那么** `adapter.list_for_agent()` 返回的 list 必须按 name 字典序：`[{"name": "list_inspectors", ...}, {"name": "list_targets", ...}, {"name": "run_inspector", ...}]`

#### 场景:input_schema 是 Anthropic 兼容 JSON Schema

- **当** 取投影后某项的 `input_schema` 字段
- **那么** 必须是符合 JSON Schema Draft 2020-12 的 dict（Anthropic Messages API 要求），含 `type: "object"` / `properties` / `required` 等标准字段

#### 场景:adapter 不会读取 `surfaces ∌ "agent"` 的 ToolSpec

- **当** registry 含 spec `A(surfaces={"agent"})` 和 spec `B(surfaces={"mcp"})`
- **那么** `adapter.list_for_agent()` 返回只含 A 的投影，不含 B

### 需求:`ToolsAdapter.dispatch` 必须执行 policy gate

`async def ToolsAdapter.dispatch(self, name: str, args_json: dict, ctx: ToolContext | None = None) -> dict` 必须在调用 handler 前执行下列 policy gate 校验，**任一失败必须 raise `ToolPolicyViolation`**：

1. **surface gate**：被请求的 ToolSpec 必须 `surfaces ∋ "agent"`，否则 `reason="not_exposed_to_surface"`
2. **side_effects gate**：M2 阶段如果 `spec.side_effects ∈ {"write", "destructive"}`，必须 raise `reason="side_effects_not_permitted"`（M2 不支持写操作 ToolSpec；写操作 ToolSpec 推到 M9 同时 enable approval flow）
3. **approval gate**：M2 阶段如果 `spec.requires_approval is True`，必须 raise `reason="approval_flow_not_supported_in_m2"`
4. **input schema validation**：`args_json` 必须能被 `spec.input_schema.model_validate(args_json)` 解析；失败时 raise `TypeError`（**不是** `ToolPolicyViolation` —— 类型错误不是 policy 拒绝）

成功通过 gate 后：
5. 实例化 `args = spec.input_schema.model_validate(args_json)`
6. 如 `spec.timeout is not None`，用 `asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout)` 调用；否则直接 `await spec.handler(args, ctx)`
7. 校验 `isinstance(result, spec.output_schema)` 通过
8. 返回 `result.model_dump()`

`ctx` 为 `None` 时必须调用 `self._context_factory()` 拿到一个新 ToolContext。

#### 场景:surface mismatch raise ToolPolicyViolation

- **当** registry 含 spec `mcp_only(surfaces={"mcp"})`；调用 `await adapter.dispatch("mcp_only", {}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，属性 `tool_name=="mcp_only"` / `surface=="agent"` / `violated_field=="surfaces"` / `reason=="not_exposed_to_surface"`

#### 场景:side_effects ∈ {write, destructive} 在 M2 raise

- **当** 某 ToolSpec `side_effects="write"` 且 `surfaces={"agent"}` 且 `requires_approval=False`；调用 `await adapter.dispatch("...", {}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`violated_field=="side_effects"` / `reason=="side_effects_not_permitted"`
- **且** 同样情况下 `side_effects="destructive"` 也必须 raise 同样的 reason

#### 场景:requires_approval=True 在 M2 raise

- **当** 某 ToolSpec `requires_approval=True` 且 `surfaces={"agent"}` 且 `side_effects ∈ {"none", "read"}`；调用 `await adapter.dispatch("...", {}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`violated_field=="requires_approval"` / `reason=="approval_flow_not_supported_in_m2"`

#### 场景:args 不符合 input_schema raise TypeError

- **当** spec.input_schema 要求 `{name: str, version: str}`，但 `args_json = {"name": 123}`
- **那么** 必须 raise `TypeError`（包装 `pydantic.ValidationError` 的内容），**不**raise `ToolPolicyViolation`

#### 场景:成功路径返回 model_dump

- **当** 通过所有 gate，handler 正常返回 `RunInspectorOutput(...)`
- **那么** `await adapter.dispatch()` 返回值必须是 `result.model_dump()` 的 dict（不是 model 实例）

#### 场景:handler 超时被 asyncio.wait_for 取消

- **当** spec.timeout=2.0 但 handler 内部 `await asyncio.sleep(10)`；调用 `await adapter.dispatch("slow_tool", {}, ctx)`
- **那么** 必须返回 tool_error dict `{"is_error": True, "error_kind": "TimeoutError", "tool_name": "slow_tool", ...}`（按 §需求:handler 异常必须包装 处理路径；`asyncio.wait_for` raise 的 `asyncio.TimeoutError` 不属于 `ToolPolicyViolation` 也不属于 `KeyError`，必须被包装）
- **且** 取消必须通过 `asyncio.wait_for` 的内置取消传播；同时 `ctx.cancel` event 不被 adapter 主动 set（cancel event 是 Agent loop / 用户 Ctrl-C 的传播通道，handler 内部可监听但 adapter 不做超时时的 set 操作）

### 需求:handler 异常必须包装成 tool_error 返回结构化字段

`ToolsAdapter.dispatch` 在 handler 执行阶段（步骤 5）发生未捕获异常时，**禁止**让异常向 Agent loop 顶层传播。必须捕获并返回 tool_error dict：

```python
{
    "is_error": True,
    "error_kind": "<exception class name>",
    "tool_name": spec.name,
    "message": str(exc),
    "cause": <exception 的简化堆栈摘要，禁止含敏感数据>,
}
```

**例外：** `ToolPolicyViolation` / `KeyError`（ToolRegistry.get 未找到）**不**在此范围 —— policy / lookup 错误是 adapter 自身的失败，必须 raise 让 Agent loop 决定如何处理（通常是给 LLM 报 tool_error 然后 retry）。

#### 场景:handler raise 通用异常被包装

- **当** spec.handler 内部 raise `ValueError("invalid arg")`；调用 `adapter.dispatch("...", args_json, ctx)`
- **那么** 必须返回 dict `{"is_error": True, "error_kind": "ValueError", "tool_name": "...", "message": "invalid arg", "cause": "..."}`；**不**raise

#### 场景:handler raise ToolPolicyViolation 直接传播

- **当** spec.handler 内部 raise `ToolPolicyViolation(...)`
- **那么** adapter 必须直接 re-raise，**不**包装为 tool_error dict

#### 场景:tool_error 不泄露敏感数据

- **当** handler raise 含敏感数据的异常（如 `ConnectionError("connect to /Users/alice/.ssh/id_rsa failed via user=admin host=10.0.0.5 token=Bearer xyz123 contact=alice@10.0.0.5")`）
- **那么** 返回的 tool_error dict 中 `message` / `cause` 字段必须经过 `hostlens.agent.tools_adapter.scrub_exception_message` 处理（这是 adapter 自带的 **字符串值脱敏函数**，**不**依赖 `hostlens.core.logging.redact_sensitive`——后者只按 key 名脱敏 mapping，无法清洗 string 值中的子串）；scrub 必须按以下正则模式 replace 为 `"***"`：
  1. **路径子串**：`/Users/[^/\s]+` / `/home/[^/\s]+` / `\.ssh/[^\s]+` / `\.aws/credentials` / `\.kube/config`
  2. **IPv4 / IPv6 字面量**：`\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` / IPv6 简化（如 `[0-9a-fA-F:]+:[0-9a-fA-F:]*` 至少含 2 个 `:` 且无空白）
  3. **凭据特征**：`[A-Za-z]+_(KEY|TOKEN|SECRET|PASSWORD)=[^\s]+` / `[Bb]earer\s+[\w.\-]+` / `sk-[a-zA-Z0-9]{20,}`
  4. **身份键值对**：`(?:user|username|usr|uid|account|login)=[^\s,;]+`（以 `=` 显式赋值的身份字段）
  5. **邮件 / user@host 模式**：`[\w.+\-]+@(?:[\w.\-]+|(?:\d{1,3}\.){3}\d{1,3})`（含 `@` 分隔的标识符，涵盖 `alice@example.com` / `admin@10.0.0.5` 两类）
- **且** `error_kind`（异常类名）必须**保留**让 Agent 知道是 ConnectionError；`tool_name` 保留（受 ToolSpec 正则约束本身安全）
- **且** 输出 string 中**禁止**出现原始 `/Users/alice/.ssh/id_rsa`、`admin`、`10.0.0.5`、`Bearer xyz123`、`alice@10.0.0.5` 任意子串

### 需求:`ToolsAdapter` 必须接受 `ToolContext` 工厂注入

`ToolsAdapter.__init__(registry: ToolRegistry, context_factory: Callable[[], ToolContext])` 必须接受 context factory（不是 context 实例本身）。每次 dispatch 调用 `ctx = context_factory()` 拿到新 ToolContext，避免跨 turn 共享 mutable state（如 `cancel: asyncio.Event` 必须每次 turn 新建）。

#### 场景:每次 dispatch 拿新 ToolContext

- **当** 配置 `context_factory = Mock()`；连续调用 `adapter.dispatch()` 2 次
- **那么** `context_factory.call_count == 2`（每次 dispatch 各调用一次工厂）

#### 场景:registry 可为空

- **当** `registry = ToolRegistry()`（空 registry）；试图 `ToolsAdapter(registry, ctx_factory)`
- **那么** 必须**允许**实例化（adapter 不强制 registry 非空 —— 测试 / 渐进装配场景需要）；`adapter.list_for_agent()` 返回 `[]`；任何 `dispatch` 调用因 `registry.get(name)` raise `KeyError` 而失败（不在 adapter 内捕获）

### 需求:Agent surface adapter 的投影结构稳定性

`ToolsAdapter.list_for_agent()` 返回的 `list[dict[str, Any]]` 必须是**结构确定性**的：同一 registry 状态下多次调用，返回的 list 长度、每个 dict 的 key 集合与顺序、对应 value 都必须一一相等（Python `==` 语义）。这保证 Agent loop 把该 list 传入 Anthropic Messages API `tools=[...]` 参数时，每个 tool 定义在跨 turn 之间结构一致，配合 `cache_control: ephemeral` 让 Anthropic 服务端的 prompt cache 在 tools section 上稳定命中（Anthropic 的 cache key 基于 prompt token 序列，不依赖 client 端 JSON byte-equal，但结构确定性是 token 序列稳定的充分条件）。

结构稳定性的具体要求：

- 顶层 list 必须按 ToolSpec `name` 字典序排序（与注册顺序解耦）
- 每个 dict 的 key 必须按固定顺序构建：`name` → `description` → `input_schema`（依赖 Python 3.7+ dict 保持 insertion order）
- `input_schema` 内部由 `spec.input_schema.model_json_schema()` 生成；Pydantic v2 在同一进程多次调用同一 model 返回的 dict key 顺序确定（properties / required / type 等 top-level key 由 Pydantic 内部稳定生成）

#### 场景:多次投影结构相等

- **当** 同一 registry 调用 `adapter.list_for_agent()` 两次，分别得到 `r1` / `r2`
- **那么** `r1 == r2` 必须为 `True`（Python dict / list 深度相等）
- **且** `list(r1[0].keys()) == ["name", "description", "input_schema"]`（key 顺序严格匹配）

#### 场景:列表顺序与注册顺序解耦

- **当** registry A 注册顺序 `[X, Y, Z]`；registry B 注册顺序 `[Z, X, Y]`（X/Y/Z 是同 3 个 ToolSpec 实例）
- **那么** `adapter_A.list_for_agent() == adapter_B.list_for_agent()` 必须为 `True`（投影顺序由 name 字典序决定，与注册顺序无关）

#### 场景:JSON 序列化保持 insertion order

- **当** 把 `adapter.list_for_agent()` 的结果传入 `json.dumps(result)`（**不**使用 `sort_keys=True`）
- **那么** 输出 JSON string 中每个 tool 对象的 key 顺序必须是 `name` → `description` → `input_schema`（不是字典序）
- **理由**：保留 insertion order 保证与 Anthropic SDK 内部序列化行为一致；`sort_keys=True` 会把 `description` 排到 `input_schema` 后，破坏 tools section 的 token 稳定性
