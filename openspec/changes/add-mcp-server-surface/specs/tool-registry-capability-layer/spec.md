## 修改需求

### 需求:M2 首批 ToolSpec 必须含 `run_inspector` / `list_inspectors` / `list_targets`

`hostlens.tools.default_tools` 模块必须导出 3 个 ToolSpec，policy 元数据严格按以下取值（M7 `add-mcp-server-surface` 把三者显式 opt-in `"mcp"` surface，向远程 LLM 暴露只读巡检三件套）：

| ToolSpec | surfaces | side_effects | sensitive_output | requires_approval | timeout |
|---|---|---|---|---|---|
| `run_inspector` | `{"agent", "mcp"}` | `"read"` | `True` | `False` | 30.0 |
| `list_inspectors` | `{"agent", "mcp"}` | `"none"` | `False` | `False` | 5.0 |
| `list_targets` | `{"agent", "mcp"}` | `"none"` | `True` | `False` | 5.0 |

**mcp surface 暴露的安全前提（M7）**：`list_inspectors` 无敏感数据（`sensitive_output=False`）；`list_targets` 经 `TargetSummary` 既有脱敏（本 spec §需求:`TargetSummary` 输出 schema 必须脱敏，标注 M7-safe，凭据/IP/identity 整条 skip）；`run_inspector` 为 `side_effects="read"` 且 non-CLI surface 强制 `allow_privileged=False`（Agent / MCP 均不能 opt-in 提权），命令面被 Inspector YAML manifest 封死。Diagnostician 内部编排工具（`correlate_findings` / `request_more_inspection`）**不**进 mcp surface。

**handler 实现契约（M1 `add-execution-target-abstraction` 已落地 + 本变更 `add-inspector-plugin-system` 进一步落地后）**：

- `list_targets_handler`：M1 `add-execution-target-abstraction` 已迁移至 `ctx.target_registry.list()` + `ctx.target_registry.get_entry(name)`；handler 内做 `ExecutionTarget → TargetSummary` 投影（应用本 spec §需求:`TargetSummary` 输出 schema 必须脱敏 的 scrub + 字段名 allowlist + capability allowlist 过滤）；**本变更不再次修改该 handler**
- `list_inspectors_handler`：M2 stub 阶段调用 `ctx.inspector_registry.list_summaries()` 拿到 stub Protocol 返回的 `list[Any]`；**本变更落地后**：
  1. `ctx.inspector_registry` 类型切换到真实 `hostlens.inspectors.registry.InspectorRegistry`（见上方 `ToolContext` 需求）
  2. `list_summaries()` 方法在真实 `InspectorRegistry` 上返回 `list[InspectorSummary]`（schema 已锁定为 `hostlens.tools.schemas.list_inspectors.InspectorSummary`）
  3. handler 实现保持 "`raw_summaries = ctx.inspector_registry.list_summaries()` → 按 `tag` / `target_kind` 参数过滤 → 返回 `ListInspectorsOutput(inspectors=...)`" 不变；但内部数据来源从 stub 切到真实 manifest（`hello.echo` / `system.uptime` 等）
  4. **禁止**保留 stub fallback —— 测试 fixture 必须用 `build_registry_from_search_paths(...)` 装配真实 `InspectorRegistry`
- `run_inspector_handler`：M2 stub 阶段返回 placeholder `RunInspectorOutput(findings=[])`；**本变更落地后**：
  1. 从 `ctx.target_registry.get(args.target_name)` 拿 `ExecutionTarget`；未找到 raise `ToolError("target_not_found: <detail>")`（M1.3 范围 `ToolError` 不带结构化 `kind` 字段——message 前缀 `target_not_found:` 是 stable 契约；测试断言 `"target_not_found" in str(exc)`）
  2. 从 `ctx.inspector_registry.get(args.inspector_name)` 拿 `InspectorManifest`；未找到 raise `ToolError("inspector_not_found: <detail>")`（同上 message-prefix 风格）
  3. 构造 `InspectorRunner(target_registry=ctx.target_registry, settings=ctx.config, logger=ctx.logger)`
  4. `result = await runner.run(manifest, target, parameters=args.parameters, cancel=ctx.cancel)` —— **注意**：`allow_privileged` 在 **non-CLI surface（agent 与 mcp 均）** 强制 `False`（Agent / MCP 远程 LLM 都不能 opt-in privilege；只有 CLI / human approval 才能）。handler 内 surface-无关地硬写 `allow_privileged=False`，M7 把 run_inspector 加入 mcp surface 后此降级对 mcp dispatch 路径同样成立
  5. 投影 `InspectorResult → RunInspectorOutput`：`target_name` ← `result.target_name`；`inspector_name` ← `result.name`；`findings` ← `[FindingSummary(severity=f.severity, message=f.message, evidence=_str_only(f.evidence)) for f in result.findings]`
  6. **重要**：`result.status != "ok"` 时仍然返回 `RunInspectorOutput`，**不**抛异常；`findings` 为空数组即可（M2 Planner Agent 通过 finding 数量为 0 + log 中的 status 字段判断是否补查；不污染 tool_use 的"成功"/"失败"两态）
  7. **修订**：`RunInspectorOutput` schema 已锁定，**不**新增 status 字段；status / error / missing 信息通过 structlog 记录但不进 tool_use 返回值；M3 `add-report-data-model` 才扩展 RunInspectorOutput 暴露 status

#### 场景:run_inspector ToolSpec 元数据

- **当** 装配后 `spec = registry.get("run_inspector")`
- **那么** `spec.surfaces == {"agent", "mcp"}` / `spec.side_effects == "read"` / `spec.sensitive_output is True` / `spec.requires_approval is False` / `spec.timeout == 30.0`

#### 场景:list_inspectors ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_inspectors")`
- **那么** `spec.surfaces == {"agent", "mcp"}` / `spec.side_effects == "none"` / `spec.sensitive_output is False` / `spec.timeout == 5.0`

#### 场景:list_targets ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_targets")`
- **那么** `spec.surfaces == {"agent", "mcp"}` / `spec.side_effects == "none"` / `spec.sensitive_output is True` / `spec.timeout == 5.0`

#### 场景:list_targets handler 投影真实 TargetRegistry 数据且应用脱敏 + allowlist

- **当** 构造真实 `TargetRegistry` 实例含 2 个 target：(a) `LocalTarget("safe-local")` + `TargetEntry(name="safe-local", display_name="Local Dev", tags=["dev"], enabled=True)`；(b) `SSHTarget("prod-ssh")` + `TargetEntry(name="prod-ssh", display_name="login as admin@10.0.0.5", tags=["prod"], enabled=True, password="literal-pwd-do-not-leak-xyz123")`
- **当** 实例化 `ctx = ToolContext(target_registry=registry, ...)`，`await registry_tool.dispatch("list_targets", ListTargetsInput(), ctx)`（**注意**：`ToolRegistry.dispatch` 要求 `BaseModel` 实例，不接受 bare dict；走 `ToolsAdapter.dispatch` 才接 dict 并 model_validate）
- **那么** 返回的 `ListTargetsOutput.targets` 必须含 `safe-local`（带 `display_name="Local Dev"` / `tags=["dev"]` / `capabilities` 来自 `LocalTarget.capabilities` 与 `CAPABILITY_ALLOWLIST` 的交集，按字典序）
- **且** `prod-ssh` 必须被**整条 skip**（display_name 含 IPv4 + 凭据特征触发 scrub_inventory_string 规则）；structlog warning 记录 skip 原因码
- **且** `ListTargetsOutput.model_dump_json()` 必须**不**含 `"literal-pwd-do-not-leak-xyz123"` / `"10.0.0.5"` / `"admin"` 任意子串

#### 场景:TargetSummary metadata 字段必须来自 TargetEntry 而不是 ExecutionTarget Protocol

- **当** 测试代码定义一个 fake `ExecutionTarget` 实现（普通 class，非 Pydantic，方便动态属性）：
  ```python
  class _FakeTargetWithExtraAttr:
      name = "t1"
      type = "local"
      capabilities: set[Capability] = {Capability.SHELL}
      display_name = "FROM_TARGET_INSTANCE"  # 故意在 target 实例上加一个 Protocol 未声明的属性
      async def exec(self, cmd, *, timeout, env=None): ...
      async def read_file(self, path): ...
  ```
- **当** 注册 `_FakeTargetWithExtraAttr()` 实例 + `TargetEntry(name="t1", type="local", display_name="FROM_ENTRY", enabled=True)` 到 registry，调用 `list_targets` handler 处理 `t1`
- **那么** 返回的 `TargetSummary.display_name` 必须等于 `"FROM_ENTRY"`（来自 `TargetEntry`），**不**等于 `"FROM_TARGET_INSTANCE"`
- **理由**：`ExecutionTarget` Protocol 上**不**暴露 `display_name` / `description` / `tags` / `enabled` 字段；handler 必须通过 `TargetRegistry.get_entry(name)` 拿这些 metadata，避免误用 target 实例上偶然存在的同名属性；用 fake target 而非 `LocalTarget` 是因为后者可能是 Pydantic / dataclass 不允许任意 setattr

#### 场景:list_inspectors handler 投影真实 InspectorRegistry 数据

- **当** 构造真实 `InspectorRegistry` 含 2 个 manifest：`hello.echo`（tags=[demo, hello]，targets=[local, ssh]）+ `system.uptime`（tags=[system, linux, performance]，targets=[local, ssh]）
- **当** 实例化 `ctx = ToolContext(inspector_registry=registry, ...)`，`await registry_tool.dispatch("list_inspectors", ListInspectorsInput(), ctx)`
- **那么** 返回的 `ListInspectorsOutput.inspectors` 必须含 2 项，按 name 字典序：`[InspectorSummary(name="hello.echo", ...), InspectorSummary(name="system.uptime", ...)]`
- **且** 每项的 `tags` 与 `compatible_target_kinds` 必须按字典序输出（与 `InspectorRegistry.list_summaries()` 投影规则一致）

#### 场景:list_inspectors handler 应用 tag 过滤

- **当** 同上 registry，`await registry_tool.dispatch("list_inspectors", ListInspectorsInput(tag="linux"), ctx)`
- **那么** 返回 `ListInspectorsOutput.inspectors` 仅含 `system.uptime`

#### 场景:list_inspectors handler 应用 target_kind 过滤

- **当** 同上 registry，`await registry_tool.dispatch("list_inspectors", ListInspectorsInput(target_kind="ssh"), ctx)`
- **那么** 返回 `ListInspectorsOutput.inspectors` 必须含**两个**（hello.echo 与 system.uptime 都 compatible_target_kinds 含 ssh）

#### 场景:run_inspector handler 通过 InspectorRunner dispatch 真实 inspector

- **当** registry 含 `hello.echo`；target_registry 含 `LocalTarget("local-host")`；`ctx = ToolContext(...)`，`await registry_tool.dispatch("run_inspector", RunInspectorInput(target_name="local-host", inspector_name="hello.echo"), ctx)`
- **那么** 返回的 `RunInspectorOutput.target_name == "local-host"`、`inspector_name == "hello.echo"`、`findings` 长度 == 1（hello.echo 的 1 个 info-level finding）

#### 场景:run_inspector handler 在 status != ok 时返回空 findings 不抛异常

- **当** 用 `hello.echo` 但 target 不可达（mock target.exec 抛 `TargetError(kind="ssh_connection_lost")`）
- **那么** dispatch 返回 `RunInspectorOutput.findings == []`，**不**抛异常；同时 structlog 记录 `inspector_status="target_unreachable"`

#### 场景:run_inspector handler target 不存在 raise ToolError

- **当** `await registry_tool.dispatch("run_inspector", RunInspectorInput(target_name="not-exist", inspector_name="hello.echo"), ctx)`
- **那么** 必须 raise `ToolError`，且 `"target_not_found" in str(exc)`（M1.3 范围 `ToolError` 不带结构化 kind 字段；message-prefix `"target_not_found: ..."` 是 stable 契约——这是调用方传错参数的情况，应该抛而非吞掉）

#### 场景:run_inspector handler inspector 不存在 raise ToolError

- **当** `await registry_tool.dispatch("run_inspector", RunInspectorInput(target_name="local-host", inspector_name="does.not.exist"), ctx)`
- **那么** 必须 raise `ToolError`，且 `"inspector_not_found" in str(exc)`（同上 message-prefix 风格）

#### 场景:run_inspector handler 在 agent surface 强制 allow_privileged=False

- **当** manifest.privilege="sudo" 的 inspector 被 agent dispatch
- **那么** 返回 `RunInspectorOutput.findings == []`（runner 内部判定 `requires_unmet`，missing=["privilege_opt_in"]，handler 投影时空 findings）；agent surface 永远不能 opt-in privilege

#### 场景:run_inspector handler 在 mcp surface 同样强制 allow_privileged=False

- **当** manifest.privilege="sudo" 的 inspector 经 **mcp dispatch**（M7 后 run_inspector 已进 mcp surface）
- **那么** 返回 `RunInspectorOutput.findings == []`（runner 内部判定 `requires_unmet`，missing=["privilege_opt_in"]）；**MCP 远程 LLM 永远不能 opt-in privilege**，与 agent surface 对称 —— 把「Agent / MCP 均不能提权」的安全前提钉成双 surface 回归点
