## 新增需求

### 需求:`ToolsAdapter.dispatch` 的 output-schema 校验失败必须 raise `ToolError`（区别于 input-schema 的 `TypeError`）

`ToolsAdapter.dispatch` 步骤 7 的 output-schema 校验（`isinstance(result, spec.output_schema)`）失败时，必须 raise `hostlens.core.exceptions.ToolError`，**禁止** raise `TypeError`，**禁止**包装成 `is_error` envelope 返回。

理由：input-schema 校验失败（步骤 4）已 spec-locked 为 `TypeError`，语义是「模型给的 args 不合法」——可由 Agent loop 回灌让模型自纠的**可恢复**错误。output-schema 失败语义完全不同：handler 返回了非 `output_schema` 类型 = handler / adapter 的**代码 bug**，必须 fail-loud。二者若共用 `TypeError`，Agent loop 无法区分，会把代码 bug 误当可恢复的模型错误回灌（浪费 token + 产生误导 trace）。`ToolError` 与 input 端的 `TypeError` 类型不同，使 Agent loop 能用类型而非脆弱的 message 匹配区分二者。

#### 场景:handler 返回非 output_schema 类型 raise ToolError

- **当** 某 ToolSpec 的 `output_schema=TypedOutput`，但其 handler 返回了 `EmptyOutput` 实例；调用 `await adapter.dispatch(name, args_json, ctx)`
- **那么** 必须 raise `ToolError`（不是 `TypeError`、不是 `ToolPolicyViolation`、不包装成 `is_error` envelope dict）；异常 message 指明期望的 output_schema 名与实际返回类型名

#### 场景:input-schema 失败仍 raise TypeError（与 output 端区分）

- **当** `args_json` 不符合 `spec.input_schema`（步骤 4 / 5 校验失败）
- **那么** 仍必须 raise `TypeError`（保持既有契约不变）——与 output-schema 失败的 `ToolError` 区分开
