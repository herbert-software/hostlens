# tool-registry-capability-layer 规范

## 目的

定义 Hostlens 双层 Capability Registry 的 host-agnostic 层（Layer 1）：`ToolSpec` 数据模型、`ToolRegistry` 索引与查询接口、`ToolContext` 依赖注入容器、`@tool` 纯 spec factory 装饰器、`register_default_tools` 显式装配函数、`ToolError` / `ToolPolicyViolation` 异常层级，以及 M2 首批 3 个 ToolSpec（`run_inspector` / `list_inspectors` / `list_targets`）的元数据约束与 `TargetSummary` 输出脱敏策略。本规范不包含任何 surface adapter（agent / mcp / cli 投影由 surface-specific spec 描述）。

## 需求

### 需求:`ToolSpec` 数据模型必须包含完整 policy 元数据

`hostlens.tools.base.ToolSpec` 必须是 Pydantic v2 模型，包含以下字段：

- 标识：
  - `name: str`：必须匹配正则 `^[a-z][a-z0-9_]*$`（M2 锁定 **snake_case** 唯一约定；以小写字母开头；全局在同一 ToolRegistry 实例内唯一）
  - `version: str`：M2 阶段视为 **opaque string**（不强制 SemVer / 不做兼容协商；只用于人类可读的版本标识）；字段必须非空（`min_length=1`）
- Schema：`input_schema: type[BaseModel]` / `output_schema: type[BaseModel]`（**必须是 Pydantic 类型本身，不是 dict / JSON Schema**）
- Handler：`handler: Callable[[BaseModel, ToolContext], Awaitable[BaseModel]]`（必须是 async 函数）
- 三 surface 文案：`agent_description: str` / `mcp_description: str` / `cli_help: str | None`
- Policy 元数据：`surfaces: set[Literal["agent", "mcp", "cli"]]` / `side_effects: Literal["none", "read", "write", "destructive"]` / `requires_approval: bool = False` / `permissions: set[str] = set()` / `sensitive_output: bool | None = None`（默认 `None` 不是 `False`）/ `target_constraints: set[str] | None = None` / `timeout: float | None = None`（秒）/ `tags: set[str] = set()`

`model_config = ConfigDict(frozen=True, extra="forbid")` 必须设置，保证 ToolSpec 实例不可变且字段集严格。

#### 场景:ToolSpec 字段完整性

- **当** 实例化一个最小 ToolSpec：`ToolSpec(name="x", version="1.0.0", input_schema=XIn, output_schema=XOut, handler=h, agent_description="...", mcp_description="...", cli_help=None, surfaces={"agent"}, side_effects="read")`
- **那么** 实例必须成功创建，且 `sensitive_output is None`、`requires_approval is False`、`permissions == set()`、`target_constraints is None`、`timeout is None`、`tags == set()`（默认值生效）

#### 场景:ToolSpec extra 字段被拒绝

- **当** 试图实例化 `ToolSpec(..., unknown_field="x")`
- **那么** 必须 raise `pydantic.ValidationError`，错误消息含 `extra fields not permitted`

#### 场景:ToolSpec 不可变

- **当** 已实例化的 `spec` 试图赋值 `spec.name = "y"`
- **那么** 必须 raise `pydantic.ValidationError`（frozen=True 生效）

#### 场景:input_schema 必须是 BaseModel 子类

- **当** 实例化 `ToolSpec(..., input_schema=dict)` 或 `ToolSpec(..., input_schema="MyInput")`
- **那么** 必须 raise `pydantic.ValidationError`，错误消息指明 `input_schema must be subclass of pydantic.BaseModel`

#### 场景:sensitive_output 默认 None 不是 False

- **当** 实例化 ToolSpec 而不传 `sensitive_output`
- **那么** `spec.sensitive_output is None` 必须为 `True`（**禁止**默认值为 `False`；用于让 M7 MCP adapter 区分"未声明"与"显式声明无敏感"）

#### 场景:name 必须匹配 snake_case 正则

- **当** 试图实例化 `ToolSpec(name="run-inspector", ...)`（kebab-case，含 `-`）
- **那么** 必须 raise `pydantic.ValidationError`，错误消息指明 name 必须匹配 `^[a-z][a-z0-9_]*$`

#### 场景:name 不允许大写或数字开头

- **当** 试图实例化 `ToolSpec(name="RunInspector", ...)` 或 `ToolSpec(name="1_tool", ...)`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:version 不能为空 string

- **当** 试图实例化 `ToolSpec(version="", ...)`
- **那么** 必须 raise `pydantic.ValidationError`（min_length=1）

#### 场景:version 接受任意非空 opaque string

- **当** 实例化 `ToolSpec(version="latest", ...)` 或 `ToolSpec(version="1", ...)` 或 `ToolSpec(version="0.1.0-alpha+build", ...)`
- **那么** 必须**成功**实例化（M2 不强制 SemVer；version 是 opaque）

### 需求:`@tool` 装饰器必须是纯 spec factory，禁止 mutate global state

`hostlens.tools.decorators.tool` 装饰器必须只完成一件事：把 handler 函数包装成 `ToolSpec` 实例并返回。**禁止**装饰器在装饰时 mutate 任何 module-level / global / class-level registry。装饰后的对象**必须**是 `ToolSpec` 实例，**不**是 callable。

#### 场景:@tool 返回 ToolSpec 实例

- **当** 用 `@tool(name="x", ...)` 装饰 async function `handler`
- **那么** 装饰后的名字必须指向 `ToolSpec` 实例（`isinstance(result, ToolSpec) is True`），且 `result.handler is handler`（原函数保留）

#### 场景:@tool 装饰不触发任何 import side effect

- **当** import 一个含 `@tool` 装饰 ToolSpec 的模块
- **那么** import 完成后，进程内**不存在**任何已被 mutate 的 module-level `_default_registry` / `_global_registry` / `_tools_list` 等模块变量；ToolRegistry 实例数量 = 0（在 `register_default_tools` 被调用前）

#### 场景:试图直接调用装饰后的名字 raise

- **当** 装饰后的 `run_inspector`（已是 ToolSpec 实例）被 `await run_inspector(args, ctx)` 调用
- **那么** 必须 raise `TypeError`（ToolSpec 实例不可调用），错误消息提示 "Use `registry.dispatch()` or `spec.handler()` instead"

### 需求:`register_default_tools` 显式装配函数必须存在且非幂等

`hostlens.tools.default_tools.register_default_tools(registry: ToolRegistry) -> None` 必须将 M2 首批 3 个 ToolSpec（`run_inspector` / `list_inspectors` / `list_targets`）注册到给定 registry。**禁止**幂等（重复调用同一 registry 必须 raise）。

#### 场景:成功装配 3 个 ToolSpec

- **当** `registry = ToolRegistry(); register_default_tools(registry)`
- **那么** `sorted(registry.names()) == ["list_inspectors", "list_targets", "run_inspector"]`

#### 场景:重复装配 raise

- **当** 已调用 `register_default_tools(registry)` 后再次调用 `register_default_tools(registry)`
- **那么** 必须 raise `ToolError`，错误消息含被重复注册的 ToolSpec name

#### 场景:多 registry 实例隔离

- **当** `r1 = ToolRegistry(); r2 = ToolRegistry(); register_default_tools(r1)`
- **那么** `r1.names() == 3 个名字`，且 `r2.names() == set()`（r2 不受 r1 装配影响）

### 需求:`ToolRegistry` 必须按 name 索引并支持 surface 过滤查询

`hostlens.tools.registry.ToolRegistry` 必须提供以下 API：

- `register(spec: ToolSpec) -> None`：name 冲突时 raise `ToolError`
- `get(name: str) -> ToolSpec`：未找到 raise `KeyError`
- `names() -> set[str]`：返回所有已注册 ToolSpec 的 name 集合
- `list_for(surface: Literal["agent", "mcp", "cli"]) -> list[ToolSpec]`：返回 `surfaces ∋ surface` 的 ToolSpec 列表，**按 name 字典序**排序（保证测试可复现）
- `async def dispatch(self, name: str, args: BaseModel, ctx: ToolContext) -> BaseModel`：**异步**方法。查找 spec → 校验 `isinstance(args, spec.input_schema)` → `await spec.handler(args, ctx)`（如 `spec.timeout is not None` 用 `asyncio.wait_for` 包裹）→ 校验 `isinstance(result, spec.output_schema)` → 返回 model 实例
- **注意**：`ToolRegistry.dispatch` 接收 `BaseModel` 实例；`ToolsAdapter.dispatch`（见 agent-tool-adapter spec）接收 `dict` 并先做 `model_validate` —— 这是有意的层次分工：registry 层假定调用方已是受信代码，adapter 层做 untrusted dict → typed model 的边界校验

#### 场景:register name 冲突 raise

- **当** registry 已含 `run_inspector` spec，再次 `registry.register(another_run_inspector_spec)`
- **那么** 必须 raise `ToolError`，错误消息含原 spec 与新 spec 的 `__module__` 来源便于 debug

#### 场景:list_for(surface) 过滤

- **当** registry 含 specs: A(surfaces={"agent"}) / B(surfaces={"agent","mcp"}) / C(surfaces={"mcp"})
- **那么** `registry.list_for("agent")` 必须返回 `[A, B]`（按 name 排序）；`registry.list_for("mcp")` 必须返回 `[B, C]`；`registry.list_for("cli")` 必须返回 `[]`

#### 场景:dispatch args type 错误 raise

- **当** spec.input_schema = `RunInspectorInput`，但 `await registry.dispatch("run_inspector", "not_a_pydantic_model", ctx)`
- **那么** 必须 raise `TypeError`（M2 用 stdlib `TypeError`，不是 ToolPolicyViolation —— 因为是类型错误不是 policy 拒绝）

#### 场景:dispatch 必须是 async 方法

- **当** 检查 `inspect.iscoroutinefunction(ToolRegistry.dispatch)`
- **那么** 必须返回 `True`（dispatch 是 `async def`，调用方必须 `await`）

#### 场景:dispatch 走 timeout 路径

- **当** spec.timeout=0.5 但 handler 内部 `await asyncio.sleep(5)`；调用 `await registry.dispatch("slow", args, ctx)`
- **那么** 必须 raise `asyncio.TimeoutError`（由 `asyncio.wait_for` 抛出；registry 层**不**包装为 tool_error —— 那是 ToolsAdapter 层的职责）

### 需求:`ToolContext` 必须包含 M2 字段最小集且禁止持有 LLMBackend

`hostlens.tools.base.ToolContext` 必须是 dataclass（`@dataclass(frozen=True)`），M2 字段集**恰好**为：

- `target_registry: TargetRegistry`（M1 落地前可用 stub Protocol）
- `inspector_registry: InspectorRegistry`（M1 落地前可用 stub Protocol）
- `config: Settings`（M0 已落地）
- `logger: structlog.BoundLogger`
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

### 需求:`ToolError` 与 `ToolPolicyViolation` 异常层级

`hostlens.core.exceptions` 必须新增以下子类，继承自 M0 已落地的 `HostlensError`：

- `ToolError(HostlensError)`：所有 Tool Registry / ToolSpec 相关错误的基类
- `ToolPolicyViolation(ToolError)`：policy gate 校验失败专用，**必须**携带结构化字段：
  - `tool_name: str`（ToolSpec.name；已被 ToolSpec 字段正则约束为 `^[a-z][a-z0-9_]*$`，不可能含路径/secret）
  - `surface: Literal["agent", "mcp", "cli"]`（surface 枚举之一）
  - `violated_field: Literal["surfaces", "side_effects", "requires_approval", "sensitive_output", "permissions", "target_constraints"]`（policy 字段名枚举）
  - `reason: Literal[
        "not_exposed_to_surface",
        "side_effects_not_permitted",
        "approval_flow_not_supported_in_m2",
        "sensitive_output_not_declared",
        "missing_required_permission",
        "target_constraint_violated",
    ]`（M2 阶段合法 reason 码集合；**禁止**接受自由文本字符串以防上层调用方把路径/凭据/用户输入塞进 reason 字段）

`__init__` 必须用 keyword-only args：`def __init__(self, *, tool_name, surface, violated_field, reason)`；运行时如收到不在 `reason` Literal 集合内的值，必须 raise `ValueError`（M2 实现时可用 `assert reason in get_args(REASON_LITERAL)`）。

**M0 兼容性：** M0 `core-services` spec §需求:异常基类层次明确 §场景:M0 子类列表完整且最小 限定"恰好 4 个"是 **M0 阶段范围约束**。本 spec 在 M2 范围内声明扩展：M2 落地后 `hostlens.core.exceptions` 公共导出从 4 增加到 6（新增 `ToolError` / `ToolPolicyViolation`）。**M0 子类完整性测试**（`tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes`）必须在本变更同 PR 内被更新为断言"恰好 6 个"，否则 M0 测试与 M2 实现会冲突。

#### 场景:ToolPolicyViolation 结构化字段

- **当** raise `ToolPolicyViolation(tool_name="run_x", surface="mcp", violated_field="sensitive_output", reason="sensitive_output_not_declared")`
- **那么** 异常实例必须含 4 个属性可被 `err.tool_name` / `err.surface` / `err.violated_field` / `err.reason` 访问；`str(err)` 必须含全部 4 字段值

#### 场景:ToolPolicyViolation reason 必须是合法枚举码

- **当** 试图 raise `ToolPolicyViolation(tool_name="x", surface="agent", violated_field="surfaces", reason="custom free text with /Users/alice/secrets")`
- **那么** 必须 raise `ValueError`（在 `ToolPolicyViolation.__init__` 中阻断非法 reason；防止自由文本注入泄露敏感数据）

#### 场景:ToolError 可被 HostlensError 通用 catch

- **当** `try: raise ToolPolicyViolation(tool_name="x", surface="agent", violated_field="surfaces", reason="not_exposed_to_surface") except HostlensError as e: ...`
- **那么** catch 必须成功（继承链正确）

#### 场景:ToolPolicyViolation 不泄露用户数据

- **当** 检查 `ToolPolicyViolation` 的 `__str__` 与 `__repr__` 输出
- **那么** 输出必须**仅**含 4 字段值（tool_name 受 `^[a-z][a-z0-9_]*$` 约束 / surface 枚举 / violated_field 枚举 / reason 枚举），**所有 4 字段都来自受约束的取值域**；**禁止**echo 用户传入的 input_schema 实例值或 handler 内部状态或调用栈细节

#### 场景:M0 异常测试必须同 PR 更新

- **当** 本变更 PR 落地后检查 `tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes`（或其等价后继）
- **那么** 测试必须断言公共导出 = 6 个类（`HostlensError` / `ConfigError` / `TargetError` / `InspectorError` / `ToolError` / `ToolPolicyViolation`），且 `__all__` 在 `hostlens/core/exceptions.py` 中同步更新

### 需求:M2 首批 ToolSpec 必须含 `run_inspector` / `list_inspectors` / `list_targets`

`hostlens.tools.default_tools` 模块必须导出 3 个 ToolSpec，policy 元数据严格按以下取值：

| ToolSpec | surfaces | side_effects | sensitive_output | requires_approval | timeout |
|---|---|---|---|---|---|
| `run_inspector` | `{"agent"}` | `"read"` | `True` | `False` | 30.0 |
| `list_inspectors` | `{"agent"}` | `"none"` | `False` | `False` | 5.0 |
| `list_targets` | `{"agent"}` | `"none"` | `True` | `False` | 5.0 |

#### 场景:run_inspector ToolSpec 元数据

- **当** 装配后 `spec = registry.get("run_inspector")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "read"` / `spec.sensitive_output is True` / `spec.requires_approval is False` / `spec.timeout == 30.0`

#### 场景:list_inspectors ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_inspectors")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is False` / `spec.timeout == 5.0`

#### 场景:list_targets ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_targets")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is True` / `spec.timeout == 5.0`

### 需求:`TargetSummary` 输出 schema 必须脱敏（M2 + M7-safe）

`list_targets` 的 `output_schema = ListTargetsOutput`，其中 `ListTargetsOutput.targets: list[TargetSummary]`。`TargetSummary` 必须**恰好**包含以下字段（不多不少）：

- `name: str`
- `kind: Literal["local", "ssh", "docker", "k8s"]`（与 `docs/ARCHITECTURE.md` §5 ExecutionTarget Protocol 已锁定的 `type` 枚举一致；**禁止**使用 `"kubernetes"` 等异名）
- `display_name: str | None`
- `description: str | None`
- `capabilities: list[str]`
- `tags: list[str]`
- `enabled: bool`

**字段名禁止集**：以下字段名**禁止**出现在 `TargetSummary.model_fields`：

`password` / `token` / `private_key` / `ssh_key_path` / `connection_string` / `dsn` / `url` / `host` / `hostname` / `ip_address` / `port` / `username` / `env` / `secret_ref` / `raw_config`

**字段值脱敏约束（对所有 string 类型字段：`name` / `display_name` / `description` / `capabilities[*]` / `tags[*]`）**：

`list_targets_handler` 在构造 `TargetSummary` 时**必须**对所有 string 类型字段值应用 `hostlens.tools.schemas.list_targets.scrub_inventory_string` 函数；scrub 必须按以下正则模式拒绝或脱敏：

- 路径子串：`/Users/[^/\s]+` / `/home/[^/\s]+` / `\.ssh(/|$)` / `\.aws/credentials` / `\.kube/config` —— 命中则**整个 target 被 skip**（不是脱敏后保留，避免给攻击者半张信息地图）
- IPv4 / IPv6 字面量：`\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` / IPv6 简化模式 —— 命中则整个 target 被 skip
- 凭据特征：`[A-Za-z]+_(KEY|TOKEN|SECRET|PASSWORD)=[^\s]+` / `[Bb]earer\s+[\w.-]+` / `sk-[a-zA-Z0-9]{20,}` —— 命中则整个 target 被 skip
- 形如 `(?:user|username|usr)\s+\S+`（"user / username / usr" 关键词后紧跟一个标识符 token，按词边界 `\b` 判断独立成词）—— 命中则**仅替换紧跟的标识符 token 为 `"***"`**，保留前缀关键词与剩余上下文（如 `"Owned by user alice, contact slack"` → `"Owned by user ***, contact slack"`）；target **不** skip；运维复合词如 `"user-service"` 不触发（关键词后紧跟连字符或下划线不被识别为独立 token）

被 skip 的 target 必须记录 structlog warning 含被 skip 的原因码（不含敏感字段值本身）。

#### 场景:TargetSummary 字段集恰好

- **当** 检查 `TargetSummary.model_fields` 的 key 集合
- **那么** 必须**恰好**返回 `{"name", "kind", "display_name", "description", "capabilities", "tags", "enabled"}`（不多不少）

#### 场景:list_targets 保留安全规划字段

- **当** Agent 调用 `list_targets`
- **且** 已配置 target `name="prod-web"`、`kind="ssh"`、`capabilities=["shell", "file_read"]`、`tags=["web", "prod"]`、`enabled=True`
- **那么** 返回的 `TargetSummary` 必须含 `name="prod-web"` / `kind="ssh"` / `capabilities=["shell", "file_read"]` / `tags=["web", "prod"]` / `enabled=True`
- **且** 返回值可被 Planner 用于选择后续 `run_inspector` 的 target

#### 场景:list_targets 不泄露 ssh_key 路径

- **当** Agent 调用 `list_targets` 且某 target 配置了 `ssh_key_path="/Users/alice/.ssh/id_rsa"`、`host="10.0.0.5"`、`username="admin"`、`password="secret123"`
- **那么** 返回的 `TargetSummary` 必须**不**含 `ssh_key_path` / `host` / `username` / `password` 字段
- **且** `ListTargetsOutput.model_dump_json()` 返回的 string 中**禁止**含 `/Users/`、`/home/`、`.ssh`、`id_rsa`、`10.0.0.5`、`admin`、`secret123` 任意子串
- **且** 原始 target 配置中的 credential / secret reference / connection string 不得出现在 `ListTargetsOutput.model_dump()` 的任何位置

#### 场景:name / display_name / description 含敏感子串时整 target 被 skip

- **当** 某 target 配置 `name="prod-web"` 但 `display_name="login as admin@10.0.0.5"`（display_name 字段值含 IPv4 + 凭据特征）
- **那么** 该 target 必须从 `ListTargetsOutput.targets` 中**整条 skip**（不是仅 display_name 脱敏后保留），structlog warning 记录 skip 原因码 `"sensitive_substring_in_display_name"`（**不**含原始字段值）
- **且** `ListTargetsOutput.model_dump_json()` 中**禁止**含 `10.0.0.5` 子串

#### 场景:tags 含 IPv4 子串时整 target 被 skip

- **当** 某 target 配置 `tags=["prod", "db", "192.168.1.42"]`
- **那么** 该 target 必须从输出中整条 skip；structlog warning 含 skip 原因码 `"sensitive_substring_in_tags"`

#### 场景:description 含 username 关键词时邻接标识符被局部替换

- **当** 某 target 配置 `description="Owned by user alice, contact via slack"`（"user" 关键词后紧跟独立标识符 token "alice"；description 不含路径 / IP / 凭据）
- **那么** target **不**被 skip；`description` 字段值经过 scrub 后输出为 `"Owned by user ***, contact via slack"`（**仅替换紧跟 "user" 的标识符 token**，保留前缀关键词与剩余上下文）；输出字段值**不含** "alice" 子串；其他字段保留原值

#### 场景:运维 tag 常用词不被误伤

- **当** 某 target 配置 `tags=["user-service", "auth-microservice"]`（"user" 是复合词的一部分，非独立 token）
- **那么** target **不**被 skip；`tags` 字段值保留原样（scrub 必须按词边界 `\b` 判断"独立 token"，不误伤复合词）

#### 场景:TargetSummary capabilities 必须 allowlist

- **当** 某 target 内部配置 `capabilities = ["shell", "file_read", "internal_admin_root"]`
- **那么** `TargetSummary.capabilities` 输出必须**只**含 allowlist 内的 capability token（与 M1 `Capability` 枚举定义一致；非 allowlist 的 token 被静默剔除或在 schema 加载时 raise）

### 需求:`ToolSpec` 禁止持久化 host-specific JSON Schema

`ToolSpec` 字段集**禁止**含 `anthropic_schema` / `mcp_schema` / `openai_schema` 或任何 host-specific JSON Schema 字段。所有 host-specific schema 必须由对应 surface adapter 在投影时从 `input_schema.model_json_schema()` 动态生成。

#### 场景:ToolSpec 不存 host schema

- **当** 检查 `ToolSpec.model_fields` 的 key 集合
- **那么** 必须**禁止**含 `anthropic_schema` / `mcp_schema` / `openai_schema` / `host_schema` 或任何 surface-prefix 的 schema 字段

#### 场景:adapter 投影从 Pydantic 生成

- **当** `ToolsAdapter` 把某 ToolSpec 投成 Anthropic schema
- **那么** 输出的 JSON Schema 必须由 `spec.input_schema.model_json_schema()` 派生（可加包装但不另存）；Pydantic schema 是 SOT
