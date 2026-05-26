## 修改需求

### 需求:`InspectorResult` Pydantic 模型字段集

`hostlens.inspectors.result.InspectorResult` 必须是 Pydantic v2 模型（`extra="forbid"`, `frozen=True`），含**恰好**以下字段：

- `name: str`：inspector name
- `version: str`：inspector version（manifest.version）
- `status: Literal["ok", "timeout", "target_unreachable", "requires_unmet", "exception"]`
- `target_name: str`
- `duration_seconds: float`
- `output: dict[str, Any] = {}`：parser 返回的结构化数据；`status != "ok"` 时可能为空 dict
- `findings: list[Finding] = []`：DSL 求值产生的 finding 列表；按 manifest.findings 顺序输出
- `error: str | None = None`：仅 `status ∈ {timeout, target_unreachable, exception}` 时必须含错误简述（非空非空白字符串）；`status == "ok"` 必须为 None；`status == "requires_unmet"` 允许 None（原因由 `missing` 字段承载，避免冗余）
- `missing: list[str] = []`：仅 `status == "requires_unmet"` 时有意义；其他 status 必须为空

**Finding 的 SOT（变更点）**：`hostlens.inspectors.result.Finding` 不再是独立 Pydantic 定义，而是 `hostlens.reporting.models.Finding` 的 **type alias re-export**：

```python
# src/hostlens/inspectors/result.py
from hostlens.reporting.models import Finding as Finding
```

`hostlens.reporting.models.Finding` 是项目级 Finding SOT，其字段集与字段约束由 `report-data-model` capability 的 §需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT 规范；本 spec **不重复定义** Finding 字段集，仅说明：

- `InspectorResult.findings: list[Finding]` 中的 Finding 即 `hostlens.reporting.models.Finding`
- 与 archived `inspector-plugin-system` 旧版本相比，**`Finding.evidence` 字段类型从 `dict[str, str]` 变为 `list[Evidence]`**（BREAKING）；M1 阶段 finding DSL 在 collect 之后求值产生的 finding 默认 `evidence=[]`（DSL 当前**不**构造 evidence；evidence 构造能力留给 M6+ hook.py 或 M3 finding DSL 扩展提案）

**对 `hostlens.tools.schemas.run_inspector.FindingSummary` 的连带影响**：

```python
# src/hostlens/tools/schemas/run_inspector.py
from hostlens.reporting.models import Finding
FindingSummary = Finding  # type alias，零行为变更
```

M2 已落地的 `register_default_tools` 与 `RunInspectorOutput.findings: list[FindingSummary]` schema 声明**零修改**（跟进字段集变更通过 type alias 自动传导）；但 `default_tools.py` 中 `_run_inspector_handler` 的投影逻辑**必须**修改（约第 148-159 行）—— 当前实现用 `evidence={k: str(v) for k, v in finding.evidence.items()}` dict comprehension，BREAKING 后 `evidence` 是 `list[Evidence]` 不再有 `.items()`，必须改为 `findings=list(result.findings)` 直接复用 InspectorResult.findings（因 `FindingSummary = Finding` 同类型）。该修改属于本提案 add-report-data-model 范围内，**不**修改 archived `inspector-plugin-system` spec 的 ToolRegistry handler 行为契约（output schema 字段集不变）。

#### 场景:status=ok 时 error 必须 None

- **当** 实例化 `InspectorResult(name="x", version="1.0.0", status="ok", target_name="t", duration_seconds=1.0, error="some error")`
- **那么** 必须 raise `pydantic.ValidationError`（model_validator 强制 ok ⇒ error is None）

#### 场景:status=requires_unmet 时 missing 必须非空

- **当** 实例化 `InspectorResult(status="requires_unmet", missing=[], ...)`
- **那么** 必须 raise `pydantic.ValidationError`（model_validator 强制 requires_unmet ⇒ missing 非空）

#### 场景:status=ok 时 missing 必须为空

- **当** 实例化 `InspectorResult(status="ok", missing=["x"], ...)`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:findings 顺序与 manifest 顺序一致

- **当** manifest.findings 索引 0/1/2 各触发 1 个 finding
- **那么** `InspectorResult.findings` 按 0,1,2 顺序排列

#### 场景:findings 中 Finding.evidence 默认为空 list

- **当** finding DSL 求值产生一个 Finding（M1 DSL 不构造 evidence）
- **那么** 该 `finding.evidence == []` 必须为 True（list 而非 dict——BREAKING 后的新字段类型）

#### 场景:Finding 是 type alias 而非独立定义

- **当** 执行 `from hostlens.inspectors.result import Finding as F_inspectors; from hostlens.reporting.models import Finding as F_reporting`
- **那么** `F_inspectors is F_reporting` 必须为 True（type alias 等价于同一类对象）

#### 场景:tools/schemas/run_inspector.FindingSummary 也是 type alias

- **当** 执行 `from hostlens.tools.schemas.run_inspector import FindingSummary; from hostlens.reporting.models import Finding`
- **那么** `FindingSummary is Finding` 必须为 True

#### 场景:旧 dict 形式 evidence 不再接受

- **当** 试图 `Finding(severity="info", message="x", evidence={"key": "value"})`（dict 而非 list）
- **那么** 必须 raise `pydantic.ValidationError`（BREAKING：M1 旧版 dict 形式不再兼容）

### 需求:内置 Inspector `hello.echo` 与 `system.uptime` 必须满足验收契约

`src/hostlens/inspectors/builtin/hello/echo.yaml` 必须：

- `name: hello.echo`
- `version: 1.0.0`
- `description`: 简短说明用于验证 inspector 管线
- `tags: [demo, hello]`
- `targets: [local, ssh]`
- `requires_capabilities: []`
- `requires_binaries: [echo]`
- `privilege: none`
- 无 `parameters` / 无 `secrets`
- `collect.command: "echo hello"`、`collect.timeout_seconds: 5`
- `parse.format: raw`、无 `raw_extract_regex`
- `output_schema`: `{type: object, properties: {raw: {type: string}}, required: [raw]}`
- `findings`: 1 个聚合 rule `{when: "len(raw) > 0", severity: info, message: "hello received: {raw}"}`（由于 simpleeval 上下文中无 `target_name` 变量，message 直接引用 output 字段 raw）

`src/hostlens/inspectors/builtin/system/uptime.yaml` 必须：

- `name: system.uptime`
- `version: 1.0.0`
- `description`: 提取负载平均值
- `tags: [system, linux, performance]`
- `targets: [local, ssh]`
- `requires_capabilities: [shell]`
- `requires_binaries: [uptime]`
- `privilege: none`
- 无 `parameters` / 无 `secrets`
- `collect.command: "uptime"`、`collect.timeout_seconds: 5`
- `parse.format: raw`、`raw_extract_regex: "load average:\\s+(?P<load1>[\\d.]+),\\s+(?P<load5>[\\d.]+),\\s+(?P<load15>[\\d.]+)"`、`columns: [load1, load5, load15]`
- `output_schema`: `{type: object, properties: {load1: {type: [string, "null"]}, load5: ..., load15: ...}}`
- `findings`: 2 个聚合 rule（M1 范围；load 阈值是固定字面量，未来 M6 时再 parameterize）：
  - `{when: "load1 and float(load1) > 4.0", severity: warning, message: "1-min load average is {load1}"}`
  - `{when: "load1 and float(load1) > 8.0", severity: critical, message: "1-min load average critically high: {load1}"}`

（`float` / `int` 由 §需求:Finding DSL 引擎... 中的 functions 集合显式注册；本需求块**不需要**额外修订该函数集——上方已涵盖。）

**本提案 MODIFIED 的变更点**：仅 `hello.echo demo path 跑通` 场景的 finding 字段类型——`evidence` 从 archived 的 `{...}` dict 字面量改为 `[]` 空 list（兑现 BREAKING `evidence: list[Evidence]`）；新增 `tags=[]`（兑现 Finding 新字段）。其他字段集、manifest yaml、`hello.echo 加载成功` / `system.uptime 在 linux/macos 上跑通` / `simpleeval 必须支持 float/int 内置` 三个场景**保持不变**。

#### 场景:hello.echo 加载成功

- **当** `result = build_registry_from_search_paths([], settings=Settings())` 装配
- **那么** `result.registry.get("hello.echo")` 必须返回完整 `InspectorManifest`；`result.errors == []`

#### 场景:hello.echo demo path 跑通

- **当** 用 `LocalTarget("local-host")` + `runner.run(manifest_hello, target, ...)` 完整跑通
- **那么** 返回 `InspectorResult.status == "ok"`、`findings == [Finding(severity="info", message="hello received: hello\n", evidence=[], tags=[])]`（**BREAKING 后**：evidence 是空 list 而非 dict 字面量；tags 是空 list 新增字段默认值）

#### 场景:system.uptime 在 linux/macos 上跑通

- **当** 在 macOS 或 Linux 上用 `LocalTarget` 跑 `system.uptime`
- **那么** 返回 `InspectorResult.status == "ok"`、`output.load1` 是数值字符串（非 None）

#### 场景:simpleeval 必须支持 float/int 内置

- **当** `evaluate("float('4.5') > 4.0", {})`
- **那么** 返回 `True`（float/int 已注册到 functions）

#### 场景:hello.echo 不再接受 dict evidence 旧形式

- **当** 试图把 archived spec 中 `evidence={"key": "value"}` dict 形式的 finding 构造写进 hello.echo 测试
- **那么** Pydantic 校验失败 + 测试 fail（BREAKING 兜底；保证旧测试不会偶然通过）

#### 场景:hello.echo manifest yaml 保持不变

- **当** 加载 `inspectors/builtin/hello/echo.yaml`
- **那么** manifest 顶层字段集与上方列出的 14 项契约一致；本 MODIFIED 仅影响 finding 构造时 Pydantic 字段类型，**不**改 builtin manifest yaml
