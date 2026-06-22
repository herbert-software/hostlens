## 修改需求

### 需求:`ToolsAdapter.dispatch` 必须执行 policy gate

`async def ToolsAdapter.dispatch(self, name: str, args_json: dict, ctx: ToolContext | None = None) -> dict` 必须在调用 handler 前执行下列 policy gate 校验，**任一失败必须 raise `ToolPolicyViolation`**：

1. **surface gate**：被请求的 ToolSpec 必须 `surfaces ∋ "agent"`，否则 `reason="not_exposed_to_surface"`
2. **side_effects gate**：如果 `spec.side_effects ∈ {"write", "destructive"}`，必须 raise `reason="side_effects_not_permitted"`（Agent 表面**永久只读**——写类 ToolSpec 在 agent surface 永久被拒，不是临时墙；M9 受控修复不以 agent-surface ToolSpec 形式存在，写路径自成 Remediation 子系统）
3. **approval gate**：如果 `spec.requires_approval is True`，必须 raise `reason="approval_flow_not_supported"`（agent surface **永久**无 approval flow；真审批属 Remediation 子系统的独立 `ApprovalGate`，不经 `ToolContext`）
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

#### 场景:side_effects ∈ {write, destructive} 永久 raise

- **当** 某 ToolSpec `side_effects="write"` 且 `surfaces={"agent"}` 且 `requires_approval=False`；调用 `await adapter.dispatch("...", {}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`violated_field=="side_effects"` / `reason=="side_effects_not_permitted"`
- **且** 同样情况下 `side_effects="destructive"` 也必须 raise 同样的 reason

#### 场景:requires_approval=True 永久 raise

- **当** 某 ToolSpec `requires_approval=True` 且 `surfaces={"agent"}` 且 `side_effects ∈ {"none", "read"}`；调用 `await adapter.dispatch("...", {}, ctx)`
- **那么** 必须 raise `ToolPolicyViolation`，`violated_field=="requires_approval"` / `reason=="approval_flow_not_supported"`

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
