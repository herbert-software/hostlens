## 1. 模块布局与基础类型

- [x] 1.1 创建 `src/hostlens/tools/__init__.py`（导出 `ToolSpec` / `ToolContext` / `ToolRegistry` / `register_default_tools` / `tool` 五个名字）；验收：`python -c "from hostlens.tools import ToolSpec, ToolContext, ToolRegistry, register_default_tools, tool"` exit 0
- [x] 1.2 创建 `src/hostlens/tools/base.py`：定义 `ToolHandler` Protocol（async callable，签名 `(args: BaseModel, ctx: ToolContext) -> Awaitable[BaseModel]`）；定义 `ApprovalService` Protocol（最小方法集：`async def request_approval(self, action: str, reason: str) -> bool`）；验收：mypy --strict 通过
- [x] 1.3 在 `src/hostlens/core/exceptions.py` 新增 `ToolError(HostlensError)` 与 `ToolPolicyViolation(ToolError)`；后者 `__init__(self, *, tool_name: str, surface: Literal["agent","mcp","cli"], violated_field: Literal["surfaces","side_effects","requires_approval","sensitive_output","permissions","target_constraints"], reason: Literal["not_exposed_to_surface","side_effects_not_permitted","approval_flow_not_supported_in_m2","sensitive_output_not_declared","missing_required_permission","target_constraint_violated"])`；运行时校验 surface / violated_field / reason 取值在 Literal 集合内（不在则 raise `ValueError`）；把 4 字段挂在实例上；`__str__` 返回 `f"ToolPolicyViolation(tool={tool_name}, surface={surface}, field={violated_field}, reason={reason})"`；验收：单测 `tests/core/test_exceptions.py` 覆盖 (a) `isinstance(ToolPolicyViolation(...), HostlensError)` (b) 结构化字段可访问 (c) `__str__` 含 4 字段 (d) 试图传入自由文本 reason 触发 `ValueError` (e) `str(err)` 在所有合法 reason 取值下都不含 `/Users/` / `/home/` / IPv4 / `Bearer ` 等敏感子串
- [x] 1.4 **更新 M0 `tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes`**：把断言"恰好 4 个"改为"恰好 6 个"，新增 `ToolError` 与 `ToolPolicyViolation` 到 `sorted(public_names)` 比较列表；同步更新 `src/hostlens/core/exceptions.py` 的 `__all__`（如已存在）从 4 元素扩展到 6 元素；验收：`pytest tests/core/test_exceptions.py -v` exit 0，且测试名称重命名为 `test_module_exports_exactly_six_exception_classes_after_m2`

## 2. ToolContext 与 NoopApprovalService

- [x] 2.1 在 `src/hostlens/tools/base.py` 定义 `@dataclass(frozen=True) class ToolContext`，字段恰好 6 个：`target_registry: TargetRegistry` / `inspector_registry: InspectorRegistry` / `config: Settings` / `logger: structlog.BoundLogger` / `approval_service: ApprovalService` / `cancel: asyncio.Event`；用 stub Protocol 占位 `TargetRegistry` / `InspectorRegistry`（M1 落地前的占位）；验收：mypy --strict 通过，`dataclasses.fields(ToolContext)` 返回 6 个 field
- [x] 2.2 实现 `class NoopApprovalService(ApprovalService)`：`request_approval` 永远 raise `ToolPolicyViolation(tool_name="<noop>", surface="agent", violated_field="requires_approval", reason="approval_flow_not_supported_in_m2")`（注意 `tool_name` 仍需匹配 `^[a-z][a-z0-9_]*$` 正则，可用 `noop_approval_service` 等占位 name；reason 必须是 Literal 枚举码）；验收：单测覆盖 raise 行为 + 4 字段值都在受约束取值域内
- [x] 2.3 写 `tests/tools/test_context.py` 覆盖 spec §需求:ToolContext 的 3 个场景（字段集严格 / 不可变 / approval_service 不允许 None）；验收：3 个测试 pass

## 3. ToolSpec Pydantic 模型

- [x] 3.1 在 `src/hostlens/tools/base.py` 定义 `class ToolSpec(BaseModel)`，字段按 spec §需求:ToolSpec 数据模型必须包含完整 policy 元数据 列表；`model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)`（`type[BaseModel]` 字段需要 `arbitrary_types_allowed`）；为 `input_schema` / `output_schema` 添加 `field_validator` 强制必须是 `BaseModel` 子类
- [x] 3.2 写 `tests/tools/test_spec.py` 覆盖 spec §需求:ToolSpec 的 5 个场景（字段完整性 / extra 字段拒绝 / 不可变 / input_schema 类型校验 / sensitive_output 默认 None）；验收：5 个测试 pass
- [x] 3.3 验收：`mypy --strict src/hostlens/tools/base.py` exit 0

## 4. @tool 装饰器（纯 spec factory）

- [x] 4.1 在 `src/hostlens/tools/decorators.py` 实现 `def tool(**metadata) -> Callable[[ToolHandler], ToolSpec]`：返回的 decorator 把传入 handler 与 metadata 组合成 `ToolSpec(handler=handler, **metadata)` 返回；**禁止**触碰任何 module-level / global 状态；docstring 在源码顶部明示"装饰后的名字指向 ToolSpec 实例，不再可直接 await"
- [x] 4.2 写 `tests/tools/test_decorator.py` 覆盖 spec §需求:@tool 的 3 个场景（返回 ToolSpec 实例 / import 不触发 side effect / 直接调用装饰后名字 raise TypeError）；验收：3 个测试 pass，且其中 "import 不触发 side effect" 测试用 `importlib.reload(...)` + module dict snapshot 严格验证
- [x] 4.3 验收：`mypy --strict src/hostlens/tools/decorators.py` exit 0

## 5. ToolRegistry

- [x] 5.1 在 `src/hostlens/tools/registry.py` 实现 `class ToolRegistry`：内部 `_specs: dict[str, ToolSpec]`；`register(spec)` 检查 name 冲突 raise `ToolError`；`get(name)` 未找到 raise `KeyError`；`names()` 返回 `set(self._specs.keys())`；`list_for(surface)` 过滤 `surfaces ∋ surface` 并**按 name 字典序**返回 list
- [x] 5.2 实现 `async def ToolRegistry.dispatch(self, name, args, ctx)`：**必须是 async 方法**；步骤按 spec §需求:ToolRegistry §dispatch 描述；注意 `dispatch` 的 args 必须是 BaseModel 实例（不是 dict）—— dict 形式的 dispatch 由 ToolsAdapter 处理；如 `spec.timeout is not None`，用 `asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout)`；超时由 `asyncio.TimeoutError` 抛出（registry 层不包装，由上层 adapter 决定如何处理）
- [x] 5.3 写 `tests/tools/test_registry.py` 覆盖 spec §需求:ToolRegistry 的 5 个场景（name 冲突 raise / list_for 过滤 + 排序 / dispatch args type 错误 raise TypeError / `inspect.iscoroutinefunction(dispatch)` is True / timeout 路径 raise asyncio.TimeoutError）；验收：5 个测试 pass
- [x] 5.4 验收：`mypy --strict src/hostlens/tools/registry.py` exit 0

## 6. 首批 3 个 ToolSpec 的 input/output schemas

- [x] 6.1 在 `src/hostlens/tools/schemas/run_inspector.py` 定义 `RunInspectorInput(target_name: str, inspector_name: str, parameters: dict[str, str] = {})` 与 `RunInspectorOutput(target_name: str, inspector_name: str, findings: list[FindingSummary])`；`FindingSummary` 是临时 minimal schema（M3 报告体系会重新定义；M2 用 `severity: Literal["info", "warning", "critical"]` + `message: str` + `evidence: dict[str, str] = {}` 三字段占位）
- [x] 6.2 在 `src/hostlens/tools/schemas/list_inspectors.py` 定义 `ListInspectorsInput(tag: str | None = None, target_kind: str | None = None)` 与 `ListInspectorsOutput(inspectors: list[InspectorSummary])`；`InspectorSummary(name: str, version: str, description: str, tags: list[str], compatible_target_kinds: list[str])`
- [x] 6.3 在 `src/hostlens/tools/schemas/list_targets.py` 定义 `ListTargetsInput(include_disabled: bool = False)` 与 `ListTargetsOutput(targets: list[TargetSummary])`；`TargetSummary` 字段**恰好**按 spec §需求:TargetSummary 输出 schema 必须脱敏 列出的 7 个字段（不多不少）；`kind` 必须是 `Literal["local", "ssh", "docker", "k8s"]`（**禁止** `"kubernetes"`）
- [x] 6.4 在 `src/hostlens/tools/schemas/list_targets.py` 同文件实现 `def scrub_inventory_string(value: str, *, field_kind: str) -> str | None`：按 spec §需求:TargetSummary 输出 schema 必须脱敏 §字段值脱敏约束 描述的 4 类正则模式工作；命中"整 target skip"模式返回 `None`（caller 据此决定是否 skip 整 target）；命中"username 独立 token"模式返回 `"***"`；正常字符串返回原值
- [x] 6.5 写 `tests/tools/test_schemas.py` 覆盖 (a) `TargetSummary.model_fields` 字段集恰好 7 个 (b) `TargetSummary.kind` 拒绝 `"kubernetes"` 而接受 `"k8s"` (c) 试图实例化 `TargetSummary(ssh_key_path="...")` raise `pydantic.ValidationError`（extra forbid）(d) `ListTargetsOutput.model_dump_json` 不含 15+ 禁止子串测试；验收：所有测试 pass
- [x] 6.6 写 `tests/tools/test_scrub_inventory_string.py`：(a) 路径子串触发 skip：`/Users/alice/secrets` / `/home/bob/.ssh/id_rsa` / `.aws/credentials` 都返回 `None` (b) IPv4 触发 skip：`prod-10.0.0.5` 返回 `None` (c) 凭据特征触发 skip：`API_KEY=sk-abc123` / `Bearer xyz123` 都返回 `None` (d) "user" 独立 token 局部替换：`"Owned by user alice, contact via slack"` 返回值经 scrub 后**必须**为 `"Owned by user ***, contact via slack"`（**保留**前缀 "Owned by user" 与后缀 ", contact via slack"，**仅**替换紧跟的标识符 token "alice" 为 "***"）；输出**不含** "alice" 子串(e) 运维 tag 复合词不误伤：`"user-service"` / `"auth-microservice"` 返回原值

## 7. 首批 3 个 ToolSpec 的 handler stubs + 装配

- [x] 7.1 在 `src/hostlens/tools/default_tools.py` 实现 3 个 handler stub（M1 落地前用 stub registry 返回固定数据）：`run_inspector_handler` 从 `ctx.inspector_registry` 取 stub inspector + 从 `ctx.target_registry` 取 stub target，返回 1 个 stub finding；`list_inspectors_handler` 从 `ctx.inspector_registry.list_summaries()` 取数据；`list_targets_handler` 从 `ctx.target_registry.list_summaries()` 取数据并**强制**映射到 `TargetSummary`（**禁止**直接 `model_dump` 原始 target config）
- [x] 7.2 在 `src/hostlens/tools/default_tools.py` 用 `@tool` 装饰 3 个 handler 得到 3 个 ToolSpec 实例（policy 元数据严格按 spec §需求:M2 首批 ToolSpec 必须含 ... 表格取值）
- [x] 7.3 在 `src/hostlens/tools/default_tools.py` 实现 `def register_default_tools(registry: ToolRegistry) -> None`：依次 `registry.register(run_inspector)` / `registry.register(list_inspectors)` / `registry.register(list_targets)`；如 ToolRegistry.register 已 raise duplicate，则非幂等行为天然成立
- [x] 7.4 写 `tests/tools/test_default_tools.py` 覆盖 spec §需求:register_default_tools 的 3 个场景（成功装配 / 重复装配 raise / 多 registry 隔离）；验收：3 个测试 pass
- [x] 7.5 写 `tests/tools/test_default_tools_metadata.py` 覆盖 spec §需求:M2 首批 ToolSpec 必须含 ... 的 3 个场景（每个 ToolSpec 的 policy 元数据精确匹配）；验收：3 个测试 pass

## 8. list_targets 脱敏专项测试

- [x] 8.1 写 `tests/tools/test_list_targets_redaction.py`：构造 stub TargetRegistry 含一个 target，其原始配置含 `ssh_key_path="/Users/alice/.ssh/id_rsa"` / `host="10.0.0.5"` / `username="admin"` / `password="secret123"` / `connection_string="postgres://user:pass@db:5432/x"`；调用 `list_targets_handler`；断言：
  - 返回 `TargetSummary` 字段集恰好 7 个
  - `ListTargetsOutput.model_dump_json()` 字符串**不含** `/Users/` / `/home/` / `.ssh` / `id_rsa` / `10.0.0.5` / `admin` / `secret123` / `postgres://` / `user:pass` 任意子串
- [x] 8.2 写 `tests/tools/test_list_targets_string_field_scrub.py`（spec §需求:TargetSummary 输出 schema 必须脱敏 的 4 个新增 scenario 对应测试）：(a) target `display_name="login as admin@10.0.0.5"` → 整 target 被 skip，warning 含原因码 `sensitive_substring_in_display_name`，输出 JSON 不含 `10.0.0.5` (b) target `tags=["prod", "db", "192.168.1.42"]` → 整 target skip，warning 原因码 `sensitive_substring_in_tags` (c) target `description="Owned by user alice"` → target 保留，description 字段值不含 `alice` (d) target `tags=["user-service", "auth-microservice"]` → target 保留，tags 字段值保持原样（运维复合词不误伤）；验收：所有测试 pass
- [x] 8.3 写 `tests/tools/test_list_targets_capabilities_allowlist.py`：构造 stub TargetRegistry 含一个 target，原始配置 `capabilities=["shell", "file_read", "internal_admin_root"]`；调用 handler；断言 `TargetSummary.capabilities` 只含 allowlist 内的 token（"internal_admin_root" 被剔除或加载时 raise，二选一在实施中决定并写进 docstring）

## 9. Agent surface adapter — ToolsAdapter

- [x] 9.1 在 `src/hostlens/agent/tools_adapter.py` 实现 `class ToolsAdapter`：**唯一构造器签名** `__init__(self, registry: ToolRegistry, context_factory: Callable[[], ToolContext])`（两个 args，无 overload）；不在构造时校验 registry 非空（spec §需求:ToolsAdapter 必须接受 ToolContext 工厂注入 §场景:registry 可为空）
- [x] 9.2 实现 `list_for_agent() -> list[dict[str, Any]]`：调用 `registry.list_for("agent")`（已按 name 字典序）；每个 spec 投成 `{"name", "description", "input_schema"}` 三字段 dict，**保证 key 按 insertion order**（不用 sort_keys）；`input_schema` 由 `spec.input_schema.model_json_schema()` 生成
- [x] 9.3 实现 `async def dispatch(self, name: str, args_json: dict, ctx: ToolContext | None = None) -> dict`：步骤严格按 spec §需求:ToolsAdapter.dispatch 必须执行 policy gate 列出的 8 步（含新增的 side_effects gate 与 timeout 路径）；`ctx` 为 `None` 时调用 `self._context_factory()` 拿一个；当 spec.timeout is not None，用 `asyncio.wait_for(spec.handler(args, ctx), timeout=spec.timeout)`
- [x] 9.4 实现 tool_error 包装：在 dispatch handler 调用阶段 `try/except` 捕获所有非 `ToolPolicyViolation` 与 `KeyError` 的异常（包括 `asyncio.TimeoutError`），包装成 `{"is_error": True, "error_kind": ..., "tool_name": ..., "message": ..., "cause": ...}` 返回；**字符串值脱敏走 adapter 内新增的 `scrub_exception_message` 函数**（spec §需求:handler 异常必须包装成 tool_error §场景:tool_error 不泄露敏感数据 列出的 5 类正则模式），**不**调用 `hostlens.core.logging.redact_sensitive`（后者只按 key 名脱敏 mapping，无法清洗 string 值中的子串）
- [x] 9.4b 在 `src/hostlens/agent/tools_adapter.py` 实现 `def scrub_exception_message(text: str) -> str`：按 spec §需求:handler 异常必须包装 §场景:tool_error 不泄露敏感数据 列出的 5 类正则模式（路径 / IPv4·v6 / 凭据特征 / 身份键值对 `user=admin` 等 / 邮件·user@host）逐条 `re.sub` 替换为 `"***"`；纯字符串处理，不依赖 logging 模块；验收：单测 `tests/agent/test_scrub_exception_message.py` 覆盖每类正则至少 2 个正例（路径 2 / IPv4 1 + IPv6 1 / `API_KEY=` + Bearer 2 / `user=admin` + `username=alice` 2 / `alice@example.com` + `admin@10.0.0.5` 2）+ 反例（正常字符串如 `"hello world"` / `"connection refused"` 不误伤）
- [x] 9.4c 在 `tests/agent/test_scrub_exception_message.py` 加专项 e2e 场景：输入字符串 `"connect to /Users/alice/.ssh/id_rsa failed via user=admin host=10.0.0.5 token=Bearer xyz123 contact=alice@10.0.0.5"`；scrub 输出**不含** `/Users/alice` / `admin` / `10.0.0.5` / `Bearer xyz123` / `alice@10.0.0.5` 任意子串；验收：单一测试就覆盖 5 类正则的协同生效
- [x] 9.5 写 `tests/agent/test_tools_adapter.py` 覆盖 spec §需求:ToolsAdapter ... 的 4+ 场景（投影输出符合 schema / 按 name 排序 / surface 不匹配过滤 / Anthropic 兼容 JSON Schema）
- [x] 9.6 写 `tests/agent/test_tools_adapter_policy.py` 覆盖 spec §需求:ToolsAdapter.dispatch ... 的 5 个场景（surface mismatch raise / **side_effects write/destructive raise** / requires_approval raise / args 错误 raise TypeError / 成功路径 model_dump）；**关键**：side_effects gate 测试**必须包含 write 与 destructive 两种取值各 1 个 case**
- [x] 9.6b 写 `tests/agent/test_tools_adapter_timeout.py` 覆盖 spec §需求:ToolsAdapter.dispatch §场景:handler 超时被 asyncio.wait_for 取消：构造 ToolSpec `timeout=0.5`，handler 内 `await asyncio.sleep(5)`；调用 `await adapter.dispatch(...)`；断言返回 tool_error dict `{"is_error": True, "error_kind": "TimeoutError", ...}`；总用时 < 2s（确认 asyncio.wait_for 真的取消了）
- [x] 9.7 写 `tests/agent/test_tools_adapter_error_handling.py` 覆盖 spec §需求:handler 异常必须包装 ... 的 3 个场景（通用异常包装 / ToolPolicyViolation 直接传播 / tool_error 不泄露敏感数据）；其中"不泄露敏感数据"测试构造 handler raise `ConnectionError("connect to /Users/alice/.ssh/id_rsa failed via user=admin host=10.0.0.5 token=Bearer xyz")`，断言 message 字段不含 `/Users/` / `.ssh` / `id_rsa` / `admin` / `10.0.0.5` / `Bearer xyz` 任意子串；error_kind 字段保留为 `"ConnectionError"`
- [x] 9.8 写 `tests/agent/test_tools_adapter_factory.py` 覆盖 spec §需求:ToolsAdapter 必须接受 ToolContext 工厂注入 的 2 个场景（每次 dispatch 拿新 ToolContext / registry 可为空）

## 10. 投影结构稳定性（agent surface adapter）

- [x] 10.1 写 `tests/agent/test_tools_adapter_structural_stability.py` 覆盖 spec §需求:Agent surface adapter 的投影结构稳定性 的 3 个场景：(a) 同一 registry 两次 `list_for_agent()` 返回值 `r1 == r2` 且 `list(r1[0].keys()) == ["name", "description", "input_schema"]`（key 顺序严格匹配）(b) 两个 registry 不同注册顺序但同 3 个 ToolSpec 实例 → `adapter_A.list_for_agent() == adapter_B.list_for_agent()` (c) `json.dumps(r)`（**不**用 sort_keys）输出 string 中每个 tool 对象的 key 出现顺序是 `"name"` 在 `"description"` 在 `"input_schema"` 之前；验收：3 个测试 pass
- [x] 10.2 在 `ToolsAdapter.list_for_agent` 内部确保每个 dict 按 `name → description → input_schema` insertion order 构建（Python 3.7+ dict 保持 insertion order；用 dict literal 而非 `**unpack` 散乱构建）；**禁止**调用 `sort_keys=True` 或 `dict(sorted(...))`（会破坏 insertion order）

## 11. pytest fixture（测试隔离）

- [x] 11.1 在 `tests/conftest.py` 添加 fixture `tool_registry`：`@pytest.fixture def tool_registry() -> ToolRegistry: r = ToolRegistry(); register_default_tools(r); return r`；验收：fixture 在多个测试文件中可用，每个测试拿到独立实例
- [x] 11.2 在 `tests/conftest.py` 添加 fixture `tool_context_factory`：返回一个 callable，每次调用生成新 ToolContext（含 stub `target_registry` / `inspector_registry` / `NoopApprovalService` / 干净 `asyncio.Event`）
- [x] 11.3 写 `tests/tools/test_fixture_isolation.py`：两个测试用同一 fixture 但 mutate 各自 registry，验证互不影响（spec §需求:register_default_tools §场景:多 registry 实例隔离 的程序化版本）；验收：两个测试都 pass

## 12. 集成测试（端到端 demo path）

- [x] 12.1 写 `tests/integration/test_tool_registry_demo_path.py` 覆盖 proposal Demo Path 三步：(a) `register_default_tools` 后 names 恰好 3 个 (b) `ToolsAdapter.list_for_agent()` 返回 3 个合法 Anthropic schema dict (c) 调用 `dispatch("list_inspectors", {}, ctx)` 走通完整路径（stub inspector_registry 返回固定数据）；验收：测试 pass
- [x] 12.2 写 `tests/integration/test_tool_registry_anthropic_schema_compat.py`：用 `jsonschema` 库（dev 依赖）验证 `list_for_agent()` 输出的 `input_schema` 是合法 JSON Schema Draft 2020-12（Anthropic Messages API 要求）；验收：3 个 ToolSpec 投影后的 schema 都通过 jsonschema 验证

## 13. 依赖更新

- [x] 13.1 在 `pyproject.toml` 的 `[project.optional-dependencies].dev` 添加 `jsonschema>=4.21`（仅供集成测试验证 Anthropic schema 兼容性；runtime 不引入）；验收：`pip install -e ".[dev]"` 安装成功，`python -c "import jsonschema"` exit 0
- [x] 13.2 检查 runtime 依赖：本提案**不**新增 runtime 依赖（Pydantic v2 / structlog M0 已有）；验收：`pyproject.toml` `[project].dependencies` 数组无变化

## 14. 文档修订（与代码同 PR）

- [x] 14.1 修订 `CLAUDE.md` §4.10 "6 条硬规则" 第 3 条：从 "新增 Agent 可调用能力必须走 `@tool` 注册，不允许 prompt 里写死或绕过 registry 直调函数" 替换为 design.md §D-3 / Codex A.1 给出的新措辞（"必须声明为 ToolSpec：@tool 只能作为纯 spec factory ..."）
- [x] 14.2 修订 `docs/ARCHITECTURE.md` §3 "6 条硬规则" 第 3 条：与 CLAUDE.md §4.10 措辞完全一致（diff 由 design.md §D-3 / Codex A.2 给出）
- [x] 14.3 验收：`grep -n "必须声明为" CLAUDE.md docs/ARCHITECTURE.md` 在两个文件中各匹配 1 次（不用反引号 grep 模式，避免字面匹配陷阱）

## 15. CI / 静态检查

- [x] 15.1 运行 `ruff check . && ruff format --check .` exit 0
- [x] 15.2 运行 `mypy --strict src/` exit 0；**关键**：所有新增模块（`hostlens.tools.*` / `hostlens.agent.tools_adapter`）的导出符号都有完整类型注解
- [x] 15.3 运行 `pytest --cov=hostlens.tools --cov=hostlens.agent.tools_adapter --cov-report=term` exit 0；新增模块覆盖率 ≥85%（M0 阶段 README 已声明 coverage 报告但不设强制门槛；M2 引入 80% 全局门槛 —— 但本 proposal 是 M2 前置，新增模块自检 ≥85% 防止后期补测）
- [x] 15.4 运行 `pre-commit run --all-files` exit 0

## 16. Demo Path 验收（M2 退出条件）

- [x] 16.1 在干净 venv（删除现有 `.venv/` 重建）跑 proposal Demo Path 步骤 1：`pip install -e ".[dev]" && python -c "..."` exit 0 且输出含 `Registered tools: ['list_inspectors', 'list_targets', 'run_inspector']`
- [x] 16.2 跑 proposal Demo Path 步骤 2：`python -c "..."` exit 0 且输出含 3 个合法 Anthropic tool_use schema（带 `name` / `description` / `input_schema` 字段）
- [x] 16.3 跑 proposal Demo Path 步骤 3：`pytest tests/tools/ tests/agent/ tests/integration/ -v` exit 0；至少含本提案新增的全部测试（含 §1.4 / §5.3 / §6.5 / §6.6 / §8.1-8.3 / §9.5-9.8 / §10.1 / §11.3 / §12.1-12.2 所有新增测试）

## 17. Git 工作流与归档准备

- [x] 17.1 完成所有上述任务后 commit 到 feature branch `feat/add-tool-registry-capability-layer`；commit message 含 OpenSpec change name 引用
- [x] 17.2 push branch + 开 PR 到 main；PR 描述含 spec 引用（`openspec/changes/add-tool-registry-capability-layer/`）与 Demo Path
- [x] 17.3 等 CI 全绿 + review 通过后 squash merge：`\gh pr merge <num> --squash --delete-branch`
- [x] 17.4 准备归档：跑 `openspec-cn validate add-tool-registry-capability-layer`（位置参数，不是 `--change`）确认变更可归档；后续运行 `/opsx:archive` 推进到 `openspec/specs/tool-registry-capability-layer/` 与 `openspec/specs/agent-tool-adapter/`
