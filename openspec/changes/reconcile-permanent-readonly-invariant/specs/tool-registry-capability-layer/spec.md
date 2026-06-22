## 修改需求

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
        "approval_flow_not_supported",
        "sensitive_output_not_declared",
        "missing_required_permission",
        "target_constraint_violated",
    ]`（合法 reason 码集合；**禁止**接受自由文本字符串以防上层调用方把路径/凭据/用户输入塞进 reason 字段）

`__init__` 必须用 keyword-only args：`def __init__(self, *, tool_name, surface, violated_field, reason)`；运行时如收到不在 `reason` Literal 集合内的值，必须 raise `ValueError`（实现时可用 `assert reason in get_args(REASON_LITERAL)`）。

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

`password` / `token` / `private_key` / `ssh_key_path` / `key_path` / `connection_string` / `dsn` / `url` / `host` / `hostname` / `ip_address` / `port` / `username` / `env` / `secret_ref` / `raw_config`

**字段值脱敏约束（对所有 string 类型字段：`name` / `display_name` / `description` / `capabilities[*]` / `tags[*]`）**：

`list_targets_handler` 在构造 `TargetSummary` 时**必须**对所有 string 类型字段值应用 `hostlens.tools.schemas.list_targets.scrub_inventory_string` 函数；scrub 必须按以下正则模式拒绝或脱敏：

- 路径子串：`/Users/[^/\s]+` / `/home/[^/\s]+` / `\.ssh(/|$)` / `\.aws/credentials` / `\.kube/config` —— 命中则**整个 target 被 skip**（不是脱敏后保留，避免给攻击者半张信息地图）
- IPv4 / IPv6 字面量：`\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` / IPv6 简化模式 —— 命中则整个 target 被 skip
- 凭据特征：`[A-Za-z]+_(KEY|TOKEN|SECRET|PASSWORD)=[^\s]+` / `[Bb]earer\s+[\w.-]+` / `sk-[a-zA-Z0-9]{20,}` —— 命中则整个 target 被 skip
- 形如 `(?:user|username|usr)\s+\S+`（"user / username / usr" 关键词后紧跟一个标识符 token，按词边界 `\b` 判断独立成词）—— 命中则**仅替换紧跟的标识符 token 为 `"***"`**，保留前缀关键词与剩余上下文；target **不** skip；运维复合词如 `"user-service"` 不触发

被 skip 的 target 必须记录 structlog warning 含被 skip 的原因码（不含敏感字段值本身）。

**capability allowlist（M1 落地后必须与 `Capability` Enum 严格相等）**：

`hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST` 必须定义为 `frozenset({c.value for c in Capability})`（**M1 落地后**——`Capability` Enum 由 `execution-target` spec 定义并 import）。**禁止**：

- 静态硬编码字面量（如 `frozenset({"shell", "file_read", ...})`）—— 易与 Enum 漂移
- 含 Enum 尚未定义的 placeholder 值（如 M2 stub 阶段的 `file_write` / `docker` / `k8s_exec` 是预留 placeholder；M1 落地后**必须**删除——其中 `file_write` / `k8s_exec` 经评审**不**回填（M9 经评审决定不新增写类 Capability、`add-kubernetes-target` 不引入 `k8s_exec`），`docker` 已由 M1 既有 `docker_cli` 覆盖）

#### 场景:TargetSummary 字段集恰好

- **当** 检查 `TargetSummary.model_fields` 的 key 集合
- **那么** 必须**恰好**返回 `{"name", "kind", "display_name", "description", "capabilities", "tags", "enabled"}`（不多不少）

#### 场景:list_targets 保留安全规划字段

- **当** Agent 调用 `list_targets`
- **且** 已配置 target `name="prod-web"`、`kind="ssh"`、`capabilities=["shell", "file_read"]`、`tags=["web", "prod"]`、`enabled=True`
- **那么** 返回的 `TargetSummary` 必须含 `name="prod-web"` / `kind="ssh"` / `capabilities=["shell", "file_read"]` / `tags=["web", "prod"]` / `enabled=True`
- **且** 返回值可被 Planner 用于选择后续 `run_inspector` 的 target

#### 场景:list_targets 不泄露 ssh_key 路径

- **当** Agent 调用 `list_targets` 且某 target 配置了 `key_path="/Users/alice/.ssh/id_rsa"`、`host="10.0.0.5"`、`username="admin"`、`password="secret123"`
- **那么** 返回的 `TargetSummary` 必须**不**含 `key_path` / `host` / `username` / `password` 字段
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

#### 场景:CAPABILITY_ALLOWLIST 派生自 Capability Enum

- **当** M1 落地后检查 `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST`
- **那么** 必须严格相等于 `frozenset({c.value for c in hostlens.targets.base.Capability})`（M1 阶段 = `{"shell", "file_read", "ssh", "systemd", "docker_cli"}`）
- **且** 源码层面必须看到 `CAPABILITY_ALLOWLIST = frozenset({c.value for c in Capability})` 形式的派生表达式（**禁止**硬编码字面量集合，避免与 Enum 漂移）

#### 场景:list_targets 投影过滤 allowlist 外 token

- **当** 某 target 内部 `capabilities = {Capability.SHELL, Capability.SSH}` 加上一个未来值（mock 注入的 `"internal_admin_root"` 假 token）
- **那么** `TargetSummary.capabilities` 输出**只**含 `["shell", "ssh"]`（按字典序）；`"internal_admin_root"` 被静默剔除；handler 必须产生 structlog warning 记录被剔除的 token
