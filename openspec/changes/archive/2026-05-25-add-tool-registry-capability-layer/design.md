## 上下文

M0 已落地项目脚手架（CLI / config / logging / 异常基类），M1 三个 proposal（`add-execution-target-abstraction` / `add-inspector-plugin-system` / `add-report-data-model`）尚未开起 —— 本 proposal 与 M1 三件套并列在 M2 之前，目的是把"Agent ↔ 能力"的 host-agnostic 契约定死。

约束：
- 必须按 CLAUDE.md §4.10 / ARCHITECTURE.md §3 的双层 capability 模型实现（Layer 1 spec + Layer 2 surface adapter）
- 必须遵守 ADR-008（`LLMBackend` 是 `AgentLoop` 私有依赖，不进 `ToolContext`）
- 必须保留 §4.10 的 6 条硬规则与反模式清单
- Inspector / ExecutionTarget / Notifier 三类业务插件**不**进 `ToolSpec`（L4 不归 L3 管）

利益相关者：M2 Agent loop 实施者（最终消费者）；M1 三件套 spec 作者（需要按本 proposal 定下的 `ToolContext` 字段反向约束他们的 registry 接口）；M7 MCP adapter 实施者（需要按本 proposal 定下的 `sensitive_output` / `surfaces` 语义投影 ToolSpec）。

## 目标 / 非目标

**目标：**

- 把"Agent 可调用能力"的 spec 抽象（`ToolSpec`）落到 Pydantic 模型 + 单测覆盖
- 把"依赖注入容器"（`ToolContext`）的字段最小集定死，杜绝服务定位器化
- 把 `@tool` 装饰器与 `register_default_tools` 显式装配的工程纪律写进 spec scenario
- 把 Layer 2 agent surface adapter 实现为可投影 Anthropic `tool_use` schema 的可测试组件
- 通过 3 个首批 ToolSpec（`run_inspector` / `list_inspectors` / `list_targets`）证明双层模型可走通完整路径
- 把 `list_targets` 输出 schema 按 M7 MCP 投影安全标准前置定型，避免后期 breaking change
- 把 CLAUDE.md §4.10 / ARCHITECTURE.md §3 硬规则 3 措辞改成与新设计一致

**非目标：**

- MCP surface adapter（推到 M7）
- CLI surface adapter（暂未决定时机）
- 写操作 / destructive ToolSpec / 真实 ApprovalService（推到 M9）
- `read_finding_detail` ToolSpec 与报告持久化（推到 M3）
- LLMBackend Protocol 与实现（独立 proposal `add-llm-backend-protocol`）
- Agent loop 本体、token 预算、max_turns 控制（独立 proposal `add-agent-loop-skeleton`）
- ToolSpec 的版本兼容协商（`version` 字段保留但 M2 不实现兼容处理）
- `report_store` / `run_history` 等未来 ToolContext 字段（M3 再加）

## 决策

### D-1 `ToolContext` 字段最小集（M2 范围）

**选择：** `target_registry` + `inspector_registry` + `config` + `logger` + `approval_service` + `cancel`，外加 `approval_service` 提供 `NoopApprovalService` 真实 stub（不是 `None`）。

**替代方案：**

- (A) 预先添加 `report_store` / `run_history` 字段（M3 会用）→ **否决**。理由：`ToolContext` 字段一旦定下来是 ABI，提前加未被消费的字段等于把 DI 容器退化成 service bag，破坏 §4.10 反模式"handler 必须从 ctx 拿依赖"的工程意图。
- (B) `approval_service: ApprovalService | None = None` 允许缺省 → **否决**。理由：`None` 检查会污染每个写类 handler，未来 M9 destructive ToolSpec 实施时还得回头补；M2 用 `NoopApprovalService` 保 ABI 稳定，handler 代码一致，dispatch 时由 policy gate 拒绝带 `requires_approval=True` 的工具。

**理由：** 字段集严格对齐 M2 首批 3 个 ToolSpec 的真实需求 —— `run_inspector` 需要 `target_registry` + `inspector_registry` + `logger`；`list_inspectors` / `list_targets` 只读 registry + `logger`。`config` 用于 ToolSpec.timeout 默认值的 override；`cancel` 用于 Agent loop 超时取消 / 用户 Ctrl-C 传播。

### D-2 M2 首批 ToolSpec 数量与边界

**选择：** 3 个 —— `run_inspector` / `list_inspectors` / `list_targets`，**不**含 `read_finding_detail`。

**替代方案：**

- (A) 4 个（含 `read_finding_detail`）→ **否决**。理由：要么冗余（`run_inspector` 输出已含完整 finding），要么提前耦合到 M3 报告持久化（需要 finding identity model + report store），范围溢出。
- (B) 2 个（去掉 `list_targets`，只留 `run_inspector` + `list_inspectors`）→ **否决**。理由：Planner 不能选 target 等于把"选哪台机器"硬编码进 system prompt，违反 §4.10"prompt 里不写死能力"。

**理由：** 3 个工具构成最小可证明的能力面 —— "发现 inspector / 发现 target / 跑 inspector" 是 Planner 的最小决策三件套。

### D-3 `@tool` 是纯 spec factory + 显式 `register_default_tools` 装配

**选择：** `@tool(...)` 装饰器只包装 handler 返回 `ToolSpec` 实例，**不** mutate 任何 module-level / global registry；装配走 `register_default_tools(registry: ToolRegistry) -> None` 显式函数。

**替代方案：**

- (A) `@tool` 装饰时自动注册到 module-level `_DEFAULT_REGISTRY` → **否决**。理由：
  1. import 顺序敏感，测试隔离不可能（"clean registry" fixture 写不出来）
  2. 与 §4.10 反模式"handler 不得 `from hostlens.targets.registry import TARGET_REGISTRY`"在哲学上矛盾（前者批判 global lookup，后者却用 global registration）
  3. 多 registry 实例不可行（M2 测试需要 unit / integration 分别拿干净 registry）
- (B) 完全去掉 `@tool` 装饰器，纯手写 `ToolSpec(name=..., handler=...)` → **可行但代价高**。装饰器在 metadata 与 handler 之间提供视觉绑定，对"简历可读性"有正向贡献；纯手写会让 ToolSpec 注册散在多处。

**理由：** 装饰器 + 显式装配是 FastAPI Router / Flask Blueprint 的成熟模式 —— 装饰器声明能力，显式组合到 application factory。这两步分离让测试隔离 / 多 registry / 文档生成都可解。

### D-4 Layer 1 + Agent adapter 同 proposal（不拆分）

**选择：** 本 proposal 同时包含 Layer 1（`ToolSpec` / `ToolRegistry` / `ToolContext`）与 Layer 2 agent surface adapter（`ToolsAdapter`）。

**替代方案：**

- (A) 只 propose Layer 1，把 agent adapter 拆到 `add-agent-tool-adapter` → **否决**。理由：Layer 1 单独没有 exercised path —— 定了 `ToolSpec` 但没有 adapter 投影出 Anthropic tool_use schema，等于 spec 没实证。
- (B) 把 MCP adapter 也包进来 → **否决**。理由：M2 不需要 MCP，M7 才用；MCP adapter 还要校验 `sensitive_output` 与 `requires_approval`，作用域大；提前实现会拖慢 M2 demo。

**理由：** ARCHITECTURE.md §3 line 308-313 已明确"M2 实现 Layer 1 + Agent surface adapter；M7 实现 MCP adapter"—— 与本 proposal 范围一致。

### D-5 policy gate 校验失败用自定义异常 `ToolPolicyViolation`

**选择：** 定义 `ToolError(HostlensError)` + `ToolPolicyViolation(ToolError)` 继承链；`ToolPolicyViolation` 携带四个**全部受约束取值域**的结构化字段：

- `tool_name: str`（受 ToolSpec 的 `^[a-z][a-z0-9_]*$` 正则间接约束）
- `surface: Literal["agent", "mcp", "cli"]`
- `violated_field: Literal["surfaces", "side_effects", "requires_approval", "sensitive_output", "permissions", "target_constraints"]`
- `reason: Literal[...]`（6 个 M2 合法 reason 码：`not_exposed_to_surface` / `side_effects_not_permitted` / `approval_flow_not_supported_in_m2` / `sensitive_output_not_declared` / `missing_required_permission` / `target_constraint_violated`）

**为何 `reason` 必须是 enum 不是自由字符串：** 安全考虑。如果 `reason: str` 允许任意文本，调用方可能把 `reason=f"target {target.host} not allowed"` 这样的拼接传入，让异常 `__str__` 直接 echo IP / 路径 / 凭据。改为 Literal 枚举后，所有 4 字段都来自受约束的取值域，`ToolPolicyViolation.__str__` / `__repr__` 输出**不可能**包含用户数据或敏感子串。

**替代方案：**

- (A) 用 stdlib `PermissionError` → **否决**。理由：OS 层语义干扰（文件 / 进程权限），用在"业务层 policy 拒绝"会让 reader 误判故障原因。
- (B) 用 `ValueError` / `RuntimeError` → **否决**。理由：与 M0 落地的 `HostlensError` 体系一致性破坏，且无法携带结构化字段供 M7 MCP adapter 的诊断使用。
- (C) `reason: str` 自由文本 → **否决**。安全 blocker：自由文本是 prompt injection / log injection / leak surface 的入口。enum 强制让调用方在编译期决定 reason。

**理由：** 与 M0 已落地的异常基类层次保持一致（`HostlensError → ToolError → ToolPolicyViolation`），结构化字段对 M7 MCP adapter 的失败诊断至关重要（"为什么这个工具不能投影到 MCP" 需要不解析字符串就能拿到原因）。

**M0 兼容性说明：** M0 `core-services` spec §需求:异常基类层次明确 §场景:M0 子类列表完整且最小 限定"M0 子类恰好 4 个"是**M0 阶段范围约束**。本 proposal 是 M2 范围，新增 `ToolError` + `ToolPolicyViolation` 后公共导出 = 6 个。**M0 异常完整性测试 `tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes` 必须在本变更同 PR 内更新为断言恰好 6 个**（参见 tasks.md §1.3），同时 `src/hostlens/core/exceptions.py` 的 `__all__` 同步更新。M0 spec 的"恰好 4 个"语义在 M2 后演进为"恰好 6 个"，由 M2 spec 显式声明此 invariant 变更。

### D-6 `list_targets` 安全输出 schema（M2 + M7-safe）

**选择：** `TargetSummary` 字段集 = `name: str` / `kind: Literal["local", "ssh", "docker", "k8s"]` / `display_name: str | None` / `description: str | None` / `capabilities: list[str]` / `tags: list[str]` / `enabled: bool`。

**`kind` 值与 ARCHITECTURE.md §5 一致：** ExecutionTarget Protocol 已锁定 `type: Literal["local", "ssh", "docker", "k8s"]`（不是 `"kubernetes"`）；TargetSummary.kind 必须保持同一枚举，避免 K8s 命名漂移。

**禁止字段名（15 个）：** `password` / `token` / `private_key` / `ssh_key_path` / `connection_string` / `dsn` / `url` / `host` / `hostname` / `ip_address` / `port` / `username` / `env` / `secret_ref` / `raw_config`。

**字符串字段值脱敏（针对 `name` / `display_name` / `description` / `capabilities[*]` / `tags[*]`）：** 不光禁止字段名，还要防止敏感子串从 `name="prod-10.0.0.5"` 这类字段值里"穿过"。`list_targets_handler` 在构造每个 `TargetSummary` 前必须调 `scrub_inventory_string` 函数（adapter 层无关，在 schemas/list_targets.py 内）：

- 字段值含 `/Users/.+` / `/home/.+` / `\.ssh(/|$)` / IPv4/v6 / `*_KEY=...` / `Bearer ...` / `sk-...{20+}` 任一模式 → **整 target skip**
- 字段值含独立 `user` / `username` token（按 `\b` 词边界判断，避免误伤 "user-service" 复合词）→ 字段值替换为 `"***"`，target 仍保留

详细规则与场景见 specs/tool-registry-capability-layer/spec.md §需求:TargetSummary 输出 schema 必须脱敏。

**替代方案：**

- (A) 输出 full target config（含 host / port / username）→ **否决**。理由：违反 §4.10 "MCP 投影必须 policy gate"；即使 M2 只过 agent surface，schema 一旦释出就成为契约，M7 MCP 投影时只能 BREAKING 改。
- (B) 输出仅 `name` 一个字段 → **可行但 underpowered**。Planner 没法按 capability 筛 target（"找一台能跑 docker 命令的"），需要 `kind` + `capabilities` 元数据。

**理由：** Codex 立场 + §4.10 反模式"M2 上来就实现三个 surface adapter" 的反向推论 ——schema 必须在 M2 阶段就按 M7 MCP 投影安全设计，否则 M7 是 breaking。`capabilities` 字段值受 `Capability` 枚举（M1 落地）约束为 allowlist，防止泄露未来 capability 名称。

### D-7 `sensitive_output` 默认 `None`，强制显式声明

**选择：** `ToolSpec.sensitive_output: bool | None = None`，三种取值语义：
- `True`：MCP 投影时需要 policy gate（M7 由 MCP adapter 强制校验）
- `False`：MCP 投影无 sensitive 限制（项目元数据可公开）
- `None`：缺省 = 禁止 MCP 投影（M7 MCP adapter 见 `None` 直接拒绝）

**首批 3 个 ToolSpec 的取值：**

- `run_inspector.sensitive_output=True`（Inspector 输出可能含 process list / open ports / network connections）
- `list_inspectors.sensitive_output=False`（Inspector name + description + 标签是项目元数据）
- `list_targets.sensitive_output=True`（即使 `TargetSummary` 过滤后 target name + kind + tags 仍透露环境结构）

**替代方案：**

- (A) 默认 `False`（无敏感字段）→ **否决**。理由：默认 `False` 让"忘记声明"与"显式声明无敏感"无法区分，破坏 §4.10 "policy gate" 语义。
- (B) 默认 `True`（视为敏感）→ **可行但过严**。需要每个 ToolSpec 作者显式判断敏感性，但默认拒绝会让 M7 MCP 投影"按需 opt-in"，与"必须显式声明"语义反而模糊。

**理由：** Codex 立场 + §4.10 注释"刻意用 `None` 而不是 `False`"。

### D-8 ToolSpec 不持久化 host-specific JSON Schema（由 adapter 投影时生成）

**选择：** `ToolSpec.input_schema: type[BaseModel]` + `output_schema: type[BaseModel]`（持久化 Pydantic 类型）；Anthropic `tool_use` JSON Schema / MCP `inputSchema` 都由 surface adapter 在投影时从 Pydantic 模型 `.model_json_schema()` 生成，**不**进 ToolSpec 字段。

**替代方案：**

- (A) `ToolSpec` 多存一个 `anthropic_schema: dict` 字段 → **否决**。理由：违反 §4.10 硬规则 2 "ToolSpec 不存 host 专有 JSON Schema"；surface 一旦增多（MCP / CLI），字段爆炸；且 Pydantic 是 SOT，多份 schema 必漂移。

**理由：** Pydantic v2 的 `model_json_schema()` 输出的 JSON Schema 与 Anthropic / MCP 的 inputSchema 格式直接兼容（Anthropic 要求 Draft 2020-12 兼容，MCP 同）。投影时间复杂度 O(num_tools)，M2 仅 3 个 ToolSpec，性能无忧。

### D-9 doc 修订与 proposal 实施同 commit

**选择：** CLAUDE.md §4.10 / ARCHITECTURE.md §3 硬规则 3 的措辞修订**进**本 proposal 的 tasks.md（不单独走 docs-only PR）。

**替代方案：**

- (A) 单独走 `chore/clarify-tool-registration-pattern` docs-only PR 先合 → **否决（用户判断点 1 确认）**。理由：spec-driven 一致性优先，proposal review 时一并讨论"措辞 + 实现"才是完整契约；分两步会让 review 上下文割裂。

**理由：** 用户 explore 阶段判断点 1 明确选定合并方案；OpenSpec 工作流允许 design.md 显式列出 doc 修订作为实施步骤。

### D-10 `@tool` 装饰器的元数据传递语义

**选择：** `@tool` 接收所有 ToolSpec 字段（除 `handler` 由装饰器内部填）作为 keyword args；返回值是 `ToolSpec` 实例（**不是**装饰后的 callable）。

```python
@tool(
    name="run_inspector",
    version="1.0.0",
    input_schema=RunInspectorInput,
    output_schema=RunInspectorOutput,
    agent_description="Run one inspector against one target.",
    mcp_description="Run one read-only inspector against one target.",
    cli_help=None,
    surfaces={"agent"},
    side_effects="read",
    sensitive_output=True,
    timeout=30.0,
)
async def run_inspector(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
    ...

# After decoration, `run_inspector` is now a ToolSpec instance, not a coroutine function.
```

**影响：** 装饰后的"函数名"指向 `ToolSpec` 实例，**不能再被直接 await**。这是有意设计 —— 防止 import side effect 调用未注册的 handler。

**调用入口的工程纪律：**

- **唯一推荐入口：** `await registry.dispatch(name, args, ctx)`（registry 层）或 `await adapter.dispatch(name, args_json, ctx)`（adapter 层，多做 dict → model 边界校验）
- **`tool_spec.handler(args, ctx)` 是 escape hatch**，仅用于单元测试场景（直接测试 handler 而绕过 registry policy gate）；**禁止**在 production code 中直接调用 `tool_spec.handler`
- spec scenario 不强制 `tool_spec.handler` 私有化（Python 没有真正的 private 字段；用下划线前缀也只是约定），但 mypy 配合自定义 lint rule（未来）可以在 production 代码中标记 `tool_spec.handler` 直调为 error；M2 阶段先靠 CLAUDE.md §4.10 工程纪律 + code review 把关

### D-11 `register_default_tools` 的命名与多套装配

**选择：** M2 阶段提供单一 `register_default_tools(registry: ToolRegistry) -> None`；未来如果有多套（如"只读集"vs"含写操作集"vs"测试桩集"），按 `register_<scope>_tools` 命名（如 `register_read_only_tools` / `register_remediation_tools`）。

**M2 内部实现：**

```python
def register_default_tools(registry: ToolRegistry) -> None:
    """Register the M2 default tool batch.

    Non-idempotent: calling twice on the same registry raises ToolError
    (no silent re-register; force callers to use a fresh ToolRegistry instance
    if they need a clean state, e.g. in tests).
    """
    registry.register(run_inspector)
    registry.register(list_inspectors)
    registry.register(list_targets)
```

**理由：** "default" 是 M2 唯一装配集，命名直白；未来扩展用 scope 后缀避免"default 集变迁"的认知负担。

## 风险 / 权衡

| 风险 | 缓解 |
|---|---|
| **R-1** `@tool` 纯 factory 模式开发体验比"装饰器自动注册"啰嗦 —— 新增 ToolSpec 需要改两处（@tool 定义 + register_default_tools） | `register_default_tools` 是项目少数集中装配点，文件位置稳定，IDE 跳转友好；新增 ToolSpec 时 lint rule（未来）可强制提示"是否需要装配"。测试和文档收益远大于一次性写代码成本。 |
| **R-2** M2 首批 ToolSpec 不含 `read_finding_detail`，Planner 没法跨 turn 引用 finding ID | M2 demo 路径不需要跨 turn 引用：`run_inspector` 一次返回完整 finding 列表，Planner 在同一 turn 内决策即可。M3 报告持久化时再加 `read_finding_detail`，届时已有 finding identity model。 |
| **R-3** `TargetSummary` schema 在 M2 阶段没有 ExecutionTarget 实体可对应（M1 未完成），可能"为没存在的东西先定 schema" | M2 实施时用 `StubTargetRegistry`（返回内存固定数据）作为占位；schema 在单测中能 round-trip Pydantic → JSON Schema → 反序列化即可证明完整性。M1 落地后由 M1 spec 显式 reference 本 schema。 |
| **R-4** `ToolPolicyViolation` 异常带结构化字段，但 structlog 不会自动序列化 exception attributes | spec scenario 强制要求 `logger.exception("tool_policy_violation", tool=err.tool_name, ...)` 显式 bind structured fields；adapter 内部 catch 时统一 bind 5 字段（tool / surface / violated_field / reason / kind），handler 内不需要重复。 |
| **R-5** `@tool` 装饰后的"函数名"指向 ToolSpec 实例而非 callable，可能让初学者困惑（"我能直接 await `run_inspector(args, ctx)` 吗？"） | docstring 在 `@tool` 装饰器源码顶部明示；spec scenario 含一个反例场景"试图直接调用装饰后名字 → mypy + 单测捕获"。 |
| **R-6** sensitive_output=True 在 M2 surfaces={"agent"} only 的情况下是"冗余标记"（agent surface 不校验 sensitive_output） | 接受冗余：M2 阶段付出的 cognitive cost ≈ 0（标志取值时随手填一次），M7 MCP adapter 落地时直接复用，避免回头改。 |
| **R-7** `register_default_tools` 在多次调用同一 registry 时 raise（不幂等） | 接受非幂等：单测可控；如果未来真有"二次装配"场景（如插件加载），引入 `register_tools(registry, tools=[...])` 通用函数；不让 default 装配做幂等。 |

## 迁移计划

**部署步骤：**

1. 实施 PR 在 feature branch `feat/add-tool-registry-capability-layer` 上完成
2. 提交内容包含：
   - 新增 `src/hostlens/tools/` 模块（`base.py` / `registry.py` / `decorators.py` / `default_tools.py`）
   - 新增 `src/hostlens/agent/tools_adapter.py`
   - 新增 `src/hostlens/core/exceptions.py` 中 `ToolError` + `ToolPolicyViolation`
   - 新增 3 个首批 ToolSpec 的 input/output Pydantic schemas
   - 单元测试 + 集成测试
   - **同一 PR** 内修订 `CLAUDE.md` §4.10 / `docs/ARCHITECTURE.md` §3 硬规则 3 措辞
3. PR squash-merge 到 main 后归档时同步 spec 到 `openspec/specs/tool-registry-capability-layer/` 与 `openspec/specs/agent-tool-adapter/`

**回滚策略：**

- 本 proposal 只引入新 Python 模块（`hostlens.tools` / `hostlens.agent.tools_adapter`）与新异常子类，**无 schema migration / 无 config breaking change / 无 CLI 命令变更**
- 回滚 = `git revert <PR commit>` 一行；revert 后 `pip install -e ".[dev]" && pytest` 应直接通过（M0 测试不依赖新模块）
- doc 修订（CLAUDE.md / ARCHITECTURE.md）在 revert 后回到旧措辞，**M2 后续 proposal 必须感知**：如果 revert 发生而 M2 已有其他 proposal 引用新措辞，需要同步 revert 那些 proposal

## 待解决问题

- **Q-1** 是否要在 `ToolSpec` 加 `deprecated: bool = False` 字段，方便 M3+ 渐进废弃旧能力？M2 内**不决**，留到第一个真要废弃的 ToolSpec 出现时再加（避免 YAGNI）。
- **Q-2** `ToolContext` 是否需要 OpenTelemetry tracing span 字段（CLAUDE.md §2 提到 OTel 是技术栈一部分）？M2 内**不决**，留 `logger` 字段即可 ——structlog 可通过 processor 链对接 OTel，handler 不需要直接拿 span。
- **Q-3** `register_default_tools` 是否需要 `extra_tools: list[ToolSpec] | None = None` 参数支持外部插件追加？M2 内**不决**，等真有插件场景再加（M9 Remediation / M10 通道扩展可能用到）。
- **Q-4** `@tool` 装饰器是否需要支持类级别装饰（如 `@tool class RunInspector:`）？M2 内**不决**，先用 async function handler 的形态，类形态等真需要"工具内部封装状态"再考虑。
