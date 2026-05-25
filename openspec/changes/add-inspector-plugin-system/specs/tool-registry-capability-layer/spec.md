## 修改需求

### 需求:`ToolContext` 必须包含 M2 字段最小集且禁止持有 LLMBackend

`hostlens.tools.base.ToolContext` 必须是 dataclass（`@dataclass(frozen=True)`），M2 字段集**恰好**为：

- `target_registry: TargetRegistry`（M1 `add-execution-target-abstraction` 已落地后 import 自 `hostlens.targets.registry.TargetRegistry`；**禁止**保留 stub Protocol fallback）
- `inspector_registry: InspectorRegistry`（**本变更（add-inspector-plugin-system）落地后必须 import 自 `hostlens.inspectors.registry.InspectorRegistry`；禁止保留 stub Protocol fallback** —— 本变更合并前，仓库 main 上仍是 `hostlens.tools.base.InspectorRegistry` stub）
- `config: Settings`（M0 已落地）
- `logger: structlog.stdlib.BoundLogger`（与 M0 已落地的 `hostlens.tools.base` 实际 import 一致——`structlog.stdlib` 子模块的 BoundLogger，**不**用顶层 `structlog.BoundLogger` 别名以避免 mypy import 解析歧义）
- `approval_service: ApprovalService`（M2 必须传 `NoopApprovalService` 真实实例，**禁止** `None`）
- `cancel: asyncio.Event`

**禁止**字段：`llm_backend` / `anthropic_client` / `messages_create` 等任何 LLM 调用入口（ADR-008：Backend 是 AgentLoop 私有依赖）。

#### 场景:ToolContext 字段集严格

- **当** 检查 `dataclasses.fields(ToolContext)` 的 name 集合
- **那么** 必须**恰好**返回 `{"target_registry", "inspector_registry", "config", "logger", "approval_service", "cancel"}`（不多不少）

#### 场景:ToolContext 实例不可变

- **当** 已实例化的 `ctx` 试图赋值 `ctx.logger = other_logger`
- **那么** 必须 raise `dataclasses.FrozenInstanceError`

#### 场景:approval_service 不允许 None

- **当** 试图实例化 `ToolContext(..., approval_service=None)`
- **那么** 必须在类型检查阶段（mypy --strict）报错（`ApprovalService` 不是 `Optional`）；运行时调用 `ctx.approval_service.request_approval(...)` 应使用 `NoopApprovalService` 的真实实现

#### 场景:target_registry 是真实 TargetRegistry 类型

- **当** 检查 `typing.get_type_hints(ToolContext)["target_registry"]`（**必须用 `get_type_hints` 而不是 `__annotations__`** —— 仓库广泛使用 `from __future__ import annotations`，`__annotations__` 在该模式下返回 string 而非 type 对象，会让断言失效）
- **那么** 必须解析为 `hostlens.targets.registry.TargetRegistry` 真实类型（**不**是 stub Protocol 或 `typing.Any`）
- **且** `hostlens.tools.base` 模块的 import 段必须含 `from hostlens.targets.registry import TargetRegistry`，**禁止**保留 stub Protocol 类定义或 `if TYPE_CHECKING: ...` 的 Protocol fallback
- **且** 旧 stub Protocol 上的 `list_summaries()` 方法签名必须从 `hostlens.tools.base` 中**完全删除**（**禁止**保留为 backward compat 别名 —— 真实 `TargetRegistry.list()` 取代它）

#### 场景:inspector_registry 是真实 InspectorRegistry 类型

- **当** 检查 `typing.get_type_hints(ToolContext)["inspector_registry"]`（同上必须用 `get_type_hints`，不用 `__annotations__`）
- **那么** 必须解析为 `hostlens.inspectors.registry.InspectorRegistry` 真实 class 类型（**不**是 stub Protocol 或 `typing.Any`）
- **且** `hostlens.tools.base` 模块的 import 段必须含 `from hostlens.inspectors.registry import InspectorRegistry`，**禁止**保留 stub Protocol 类定义或 `if TYPE_CHECKING: ...` 的 Protocol fallback
- **且** 旧 stub Protocol 上的 `list_summaries()` 方法签名必须从 `hostlens.tools.base` 中**完全删除**（真实 `InspectorRegistry.list_summaries()` 取代它；签名一致但归属模块切换）

### 需求:M2 首批 ToolSpec 必须含 `run_inspector` / `list_inspectors` / `list_targets`

`hostlens.tools.default_tools` 模块必须导出 3 个 ToolSpec，policy 元数据严格按以下取值：

| ToolSpec | surfaces | side_effects | sensitive_output | requires_approval | timeout |
|---|---|---|---|---|---|
| `run_inspector` | `{"agent"}` | `"read"` | `True` | `False` | 30.0 |
| `list_inspectors` | `{"agent"}` | `"none"` | `False` | `False` | 5.0 |
| `list_targets` | `{"agent"}` | `"none"` | `True` | `False` | 5.0 |

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
  4. `result = await runner.run(manifest, target, parameters=args.parameters, cancel=ctx.cancel)` —— **注意**：`allow_privileged` 在 M2 agent surface 强制 `False`（Agent 不能 opt-in privilege；只有 CLI / human approval 才能）
  5. 投影 `InspectorResult → RunInspectorOutput`：`target_name` ← `result.target_name`；`inspector_name` ← `result.name`；`findings` ← `[FindingSummary(severity=f.severity, message=f.message, evidence=_str_only(f.evidence)) for f in result.findings]`
  6. **重要**：`result.status != "ok"` 时仍然返回 `RunInspectorOutput`，**不**抛异常；`findings` 为空数组即可（M2 Planner Agent 通过 finding 数量为 0 + log 中的 status 字段判断是否补查；不污染 tool_use 的"成功"/"失败"两态）
  7. **修订**：`RunInspectorOutput` schema 已锁定，**不**新增 status 字段；status / error / missing 信息通过 structlog 记录但不进 tool_use 返回值；M3 `add-report-data-model` 才扩展 RunInspectorOutput 暴露 status

#### 场景:run_inspector ToolSpec 元数据

- **当** 装配后 `spec = registry.get("run_inspector")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "read"` / `spec.sensitive_output is True` / `spec.requires_approval is False` / `spec.timeout == 30.0`

#### 场景:list_inspectors ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_inspectors")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is False` / `spec.timeout == 5.0`

#### 场景:list_targets ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_targets")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is True` / `spec.timeout == 5.0`

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

## 移除需求

(本提案无移除需求 —— stub `InspectorRegistry` Protocol 不是 spec 级需求；它是实现细节，通过修订 `ToolContext` 字段类型自动失效)
