# report-data-model 规范

## 目的

Hostlens Report 数据模型 SOT —— `Severity` / `Evidence` / `Finding` / `Report` Pydantic 模型 + `render_markdown` / `render_json` 双渲染器（渲染边界强制脱敏 via `core/redact`）+ schema_version 锁定与扩展契约。本 capability 由 add-report-data-model 提案于 M1.6 引入；M2 Planner Agent / M3 Diagnostician / M5 Notifier 三个后续提案均消费这一份契约。
## 需求
### 需求:`Severity` Literal 必须严格三值

`hostlens.reporting.models.Severity` 必须是 `Literal["info", "warning", "critical"]`，**恰好** 三个值。与 archived `inspector-plugin-system` spec §需求:`FindingRule` 四字段 DSL 字段集与静态校验 中的 finding DSL severity 三值严格对齐。**禁止** 引入额外值（如 `debug` / `error` / `fatal`）——M3 add-report-data-model 提案若需要扩展也必须保持向后兼容（add-only）。

#### 场景:仅接受三个 Literal 值

- **当** 在 `Finding(severity="info", ...)` / `Finding(severity="warning", ...)` / `Finding(severity="critical", ...)` 三种构造
- **那么** 必须成功

#### 场景:拒绝未列出值

- **当** 试图 `Finding(severity="debug", ...)` 或 `Finding(severity="fatal", ...)`
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 severity 字段名与允许值列表

### 需求:`Evidence` Pydantic 模型必须按 kind ↔ 字段子集映射强制约束

`hostlens.reporting.models.Evidence` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段：

- `kind: Literal["command_output", "file_excerpt", "metric", "structured"]`（必填）
- `command: str | None = None`
- `stdout: str | None = None`
- `stderr: str | None = None`
- `exit_code: int | None = None`
- `path: str | None = None`
- `excerpt: str | None = None`
- `metric_name: str | None = None`
- `metric_value: float | str | None = None`
- `data: dict[str, Any] | None = None`
- `truncated: bool = False`

**模型级 `model_validator(mode="after")` 必须** 强制以下 kind ↔ 字段子集映射（违反 → raise `pydantic.ValidationError`）：

| kind | 必填字段（非 None） | 禁止字段（必须为 None） |
|---|---|---|
| `command_output` | `command`, `stdout` | `path`, `excerpt`, `metric_name`, `metric_value`, `data` |
| `file_excerpt` | `path`, `excerpt` | `command`, `stdout`, `stderr`, `exit_code`, `metric_name`, `metric_value`, `data` |
| `metric` | `metric_name`, `metric_value` | `command`, `stdout`, `stderr`, `exit_code`, `path`, `excerpt`, `data` |
| `structured` | `data` | `command`, `stdout`, `stderr`, `exit_code`, `path`, `excerpt`, `metric_name`, `metric_value` |

`truncated` 字段所有 kind 均可选（默认 False）。M3 add-only 扩展新 kind 时，本需求必须 MODIFIED 同步扩展映射表。

#### 场景:command_output kind 必须含 command 与 stdout

- **当** 构造 `Evidence(kind="command_output", command="echo hi", stdout="hi\n")`
- **那么** 必须成功

#### 场景:command_output kind 缺 command 被拒绝

- **当** 试图 `Evidence(kind="command_output", stdout="hi\n")`（缺 command）
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 `kind="command_output" requires command`

#### 场景:command_output kind 含 path 被拒绝

- **当** 试图 `Evidence(kind="command_output", command="echo hi", stdout="hi\n", path="/etc/hosts")`
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 `kind="command_output" forbids path`

#### 场景:file_excerpt kind 必须含 path 与 excerpt

- **当** 构造 `Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="127.0.0.1 localhost\n")`
- **那么** 必须成功

#### 场景:metric kind 接受 float 和 str 类型 value

- **当** 分别构造 `Evidence(kind="metric", metric_name="load_1min", metric_value=0.42)` 与 `Evidence(kind="metric", metric_name="load_1min", metric_value="unavailable")`
- **那么** 两者必须均成功

#### 场景:structured kind 必须含 data

- **当** 构造 `Evidence(kind="structured", data={"k": "v"})`
- **那么** 必须成功

#### 场景:structured kind 缺 data 被拒绝

- **当** 试图 `Evidence(kind="structured")`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:truncated 字段所有 kind 均可用

- **当** 分别构造 `Evidence(kind="command_output", command="x", stdout="...", truncated=True)` 与 `Evidence(kind="file_excerpt", path="/etc/hosts", excerpt="...", truncated=True)`
- **那么** 两者必须均成功

#### 场景:extra 字段被拒绝

- **当** 试图 `Evidence(kind="command_output", command="x", stdout="y", weird_field="z")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:未列出 kind 被拒绝

- **当** 试图 `Evidence(kind="trace", ...)`
- **那么** 必须 raise `pydantic.ValidationError`

### 需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT

`hostlens.reporting.models.Finding` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段——四个 M1 核心字段（不变）+ 三个 M3 add-only 身份字段（全部带默认值）+ 一个本提案新增 add-only 来源字段（带默认值，旧构造方与旧 JSON 零改动可加载）：

核心字段（M1，不变）：

- `severity: Severity`
- `message: str`（min_length=1）
- `evidence: list[Evidence] = []`
- `tags: list[Tag] = []`（M1 finding DSL 不生产 tags；用于 M5 Notifier `only_if` 路由；每个 tag 约束 `^[a-z][a-z0-9_-]*$`）

M3 add-only 身份字段（用于 diff 指纹与根因假设引用；见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹）：

- `id: str | None = None`（确定性内容指纹；`from_inspector_results` 自动计算；直接构造或 legacy JSON 缺省为 None）
- `inspector_name: str | None = None`（产出该 finding 的 Inspector name；工厂从所属 `InspectorResult.name` 填充）
- `inspector_version: str | None = None`（Inspector version；工厂从 `InspectorResult.version` 填充；diff 版本对齐用）

本提案 add-only 来源字段（用于多 target / fleet Report 标注每条 finding 的来源 target；见 §需求:多 target Report 必须由确定性 fleet 组装路径产出）：

- `target_name: str | None = None`（产出该 finding 的来源 target 名；默认 `None` → 旧构造方 / 旧 JSON 零改动可加载；多 target 组装路径给每条 flatten 出的 finding 盖来源 `InspectorResult.target_name`；单 target 路径可留 `None` 或盖单值。**禁止**纳入 `compute_finding_id` 指纹——保单 target finding id 跨 run 稳定，见下「不纳入指纹」约束）

`extra="forbid"` 仍生效。`hostlens.reporting.models.Finding` 是 **唯一 SOT**；以下 import path 必须是 type alias re-export，**禁止**独立定义：

- `hostlens.inspectors.result.Finding` = `from hostlens.reporting.models import Finding as Finding`
- `hostlens.tools.schemas.run_inspector.FindingSummary` = `FindingSummary = Finding`

**`target_name` 不纳入 `compute_finding_id`**：指纹仍恒为 `sha256(f"{inspector_name}\x00{inspector_version}\x00{message}")[:16]`（见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹，本提案**不改**该指纹定义）；`target_name` 是 add-only 标注字段，**禁止**进入指纹，否则同一检查项跨 target 会得到不同 id、破坏 per-target regression diff 的同 id 锚点。

#### 场景:Finding 字段集严格（核心四字段 + M3 身份字段 + 来源字段，拒绝未声明字段）

- **当** 试图 `Finding(severity="info", message="x", evidence=[], tags=[], extra="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:Finding 默认 evidence 与 tags 为空 list

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.evidence == []` 且 `finding.tags == []` 必须均为 True

#### 场景:Finding 仅核心字段时身份字段与来源字段默认 None

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.id is None` 且 `finding.inspector_name is None` 且 `finding.inspector_version is None` 且 `finding.target_name is None` 必须均为 True

#### 场景:单 target 路径构造 finding 不带来源 target_name

- **当** 在单 target 路径构造 `Finding(severity="info", message="ok")`（不传 `target_name`）
- **那么** `finding.target_name is None` 必须为 True（向后兼容,单 target 不强制盖来源标注）

#### 场景:Finding 接受显式来源 target_name

- **当** 构造 `Finding(severity="warning", message="cpu high", target_name="aliyun-bj")`
- **那么** 必须成功且 `finding.target_name == "aliyun-bj"`

#### 场景:Finding 接受显式身份字段

- **当** 构造 `Finding(severity="warning", message="cpu high", id="abc123", inspector_name="linux.cpu.top_processes", inspector_version="1.0.0")`
- **那么** 必须成功且三个身份字段按传入值保存

#### 场景:Finding 接受 Evidence 实例列表

- **当** 构造 `Finding(severity="critical", message="db down", evidence=[Evidence(kind="command_output", command="ping db", stdout="", stderr="timeout", exit_code=1)])`
- **那么** 必须成功且 `finding.evidence[0].kind == "command_output"`

#### 场景:Finding 接受 tags 列表

- **当** 构造 `Finding(severity="warning", message="cpu high", tags=["cpu", "perf"])`
- **那么** 必须成功且 `finding.tags == ["cpu", "perf"]`

#### 场景:Finding 拒绝 dict 形式 evidence

- **当** 试图 `Finding(severity="info", message="x", evidence={"key": "value"})`（dict 而非 list）
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝 list 中混入非 Evidence 元素

- **当** 试图 `Finding(severity="info", message="x", evidence=["not an evidence"])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝非字符串 tags

- **当** 试图 `Finding(severity="info", message="x", tags=[123, None])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 是 frozen 不可变

- **当** 构造 `f = Finding(severity="info", message="x")` 后试图 `f.severity = "critical"`
- **那么** 必须 raise `pydantic.ValidationError` 或 `TypeError`（Pydantic v2 frozen 行为）

#### 场景:Finding type alias 路径必须可 import

- **当** 执行 `from hostlens.inspectors.result import Finding as F1; from hostlens.tools.schemas.run_inspector import FindingSummary as F2; from hostlens.reporting.models import Finding as F3`
- **那么** `F1 is F3` 与 `F2 is F3` 必须均为 True（type alias，不是子类）

#### 场景:legacy 缺身份字段与来源字段的 dict 可加载

- **当** 执行 `Finding.model_validate({"severity": "info", "message": "x"})`（旧 schema 产出的 finding，无 id/inspector_name/inspector_version/target_name）
- **那么** 必须成功且四个 add-only 字段均为 None（add-only 向后兼容）

#### 场景:target_name 不改变 finding id

- **当** 两次以同 `inspector_name`/`inspector_version`/`message` 但不同 `target_name`（一个 `"a"` 一个 `"b"`）经 `compute_finding_id` 计算 id（指纹 helper 入参不含 target_name）
- **那么** 两次 `id` 必须**相同**（`target_name` 不参与指纹）

### 需求:`Report` Pydantic 模型必须严格 conform M1 字段集与 schema_version 锁定

`hostlens.reporting.models.Report` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段——M1 扁平字段（不变，保留）+ M3 add-only 容器字段：

M1 扁平字段（不变）：

- `report_id: UUID`（必填；`Report.from_inspector_results()` 工厂自动生成）
- `intent: str | None = None`（M1 留 None，M2 Planner Agent 填自然语言意图）
- `target_name: str`（min_length=1）
- `inspector_results: list[InspectorResult]`（min_length=1；引用 `hostlens.inspectors.result.InspectorResult`）
- `findings: list[Finding] = []`（聚合自所有 `inspector_results[].findings` 的扁平视图；机械 flatten，**不** 去重 / 排序）
- `started_at: datetime`
- `finished_at: datetime`
- `metadata: dict[str, str] = {}`

schema 版本（MODIFIED：M1 锁 `"1.0"`；本提案 add-only 放宽）：

- `schema_version: Literal["1.0", "1.1"]`（填了 `meta` 即写 `"1.1"`，否则 `"1.0"`；旧 `"1.0"` 报告仍可加载）

M3 add-only 容器字段：

- `meta: ReportMeta | None = None`（运行元信息容器；**所有经 `from_inspector_results` 产出的新报告必定非 None**；`None` 仅出现在加载 legacy schema 1.0 JSON 时。约定：`meta` 为前进方向的权威源，扁平字段保留仅为 M1/M2 既有 consumer 兼容，二者冗余但必须语义一致）
- `hypotheses: list[RootCauseHypothesis] = []`（根因假设容器；**本提案不产生内容**，恒为 `[]`，由后续 `add-diagnostician-agent` 填充）

**模型级 `model_validator(mode="after")` 必须** 强制：

- `finished_at >= started_at` —— 违反 → raise `pydantic.ValidationError("finished_at must be >= started_at")`
- 当 `inspector_results` 为空 list → raise（`min_length=1` 字段约束）

#### 场景:Report 字段集严格

- **当** 试图 `Report(report_id=..., schema_version="1.0", target_name="x", inspector_results=[ir], started_at=..., finished_at=..., extra_field="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:schema_version 接受 1.0 与 1.1

- **当** 分别构造 `Report(..., schema_version="1.0", ...)` 与 `Report(..., schema_version="1.1", meta=<ReportMeta>, ...)`
- **那么** 两者必须均成功

#### 场景:schema_version 拒绝非法值

- **当** 试图 `Report(..., schema_version="2.0", ...)`
- **那么** 必须 raise `pydantic.ValidationError`（Literal["1.0","1.1"] 拒绝其它值）

#### 场景:meta 默认 None 且 hypotheses 默认空 list

- **当** 构造 `Report(...)`（不传 meta / hypotheses）
- **那么** `report.meta is None` 且 `report.hypotheses == []` 必须均为 True

#### 场景:legacy schema 1.0 JSON 无 meta 可加载

- **当** 执行 `Report.model_validate(<旧 1.0 报告 dict，无 meta/hypotheses 键>)`
- **那么** 必须成功，`report.meta is None`、`report.hypotheses == []`（add-only 向后兼容）

#### 场景:inspector_results 不能为空

- **当** 试图 `Report(..., inspector_results=[], ...)`
- **那么** 必须 raise `pydantic.ValidationError`（min_length=1）

#### 场景:finished_at 不能早于 started_at

- **当** 试图 `Report(..., started_at=datetime(2026, 5, 26, 12, 0, 0), finished_at=datetime(2026, 5, 26, 11, 0, 0), ...)`
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 `finished_at must be >= started_at`

#### 场景:finished_at 等于 started_at 允许

- **当** 构造 `Report(..., started_at=t, finished_at=t, ...)`（同一时间戳，理论场景如 cached result）
- **那么** 必须成功

#### 场景:intent 默认 None

- **当** 构造 `Report(...)`（不传 intent）
- **那么** `report.intent is None` 必须为 True

#### 场景:metadata 默认空 dict

- **当** 构造 `Report(...)`（不传 metadata）
- **那么** `report.metadata == {}` 必须为 True

#### 场景:Report 是 frozen

- **当** 构造 `r = Report(...)` 后试图 `r.target_name = "other"`
- **那么** 必须 raise `pydantic.ValidationError` 或 `TypeError`

### 需求:`Report.from_inspector_results` 工厂方法必须自动 flatten findings 与生成 report_id

`Report.from_inspector_results(target_name, inspector_results, *, intent=None, started_at, finished_at, metadata=None, target_id=None, target_type="local", token_usage=None, status=None, schedule_name=None) -> Report` 必须：

- 自动生成 `report_id = uuid4()`
- 自动 flatten findings：`findings = [f for ir in inspector_results for f in ir.findings]`（按 inspector_results 顺序 + 每个内部 findings 顺序；**不** 去重 / 排序 / 过滤），并对每个 flatten 出的 finding **填充身份字段**——`inspector_name`/`inspector_version` 取自所属 `InspectorResult.name`/`.version`，`id` 按 §需求:`Finding.id` 计算（因 Finding frozen，用 `model_copy(update=...)` 产生带身份字段的新实例）。**M3 起这是对 M1「flatten」行为的蓄意 add-only 变更**：`report.findings[i]` 是带身份字段的 `model_copy` 副本，**不再** `is` 原 finding、也不再裸 `==` 无身份字段的原对象；但**顺序 / 数量 / severity / message / evidence / tags 内容保持**（由下「flatten 不去重 / 不排序」两场景守门）
- 组装 `meta: ReportMeta`（`run_id = str(report_id)`；`target_id` 缺省 = `target_name`；`target_type` 缺省 `"local"`；`timestamp = started_at`；`duration_seconds = (finished_at - started_at).total_seconds()`；`inspectors_used` 从 `inspector_results` 机械投影为 `list[InspectorRun]`；`status` 缺省派生（`timeout`/`target_unreachable` **严格对齐 ARCHITECTURE §9 Failure Semantics**，`exception`/`requires_unmet` 按相同保守原则外推）：全 `ok` → `ok`；非 ok 结果**仅为 `timeout`** 且**至少一个 `ok`** → `ok`（§9「Inspector 超时 → ok，除非全部超时」单/部分超时不降级）；任一 `target_unreachable`（§9）/`exception`/`requires_unmet`（保守外推）或**全部** `timeout` → `partial`，调用方可显式覆盖；`token_usage` 缺省 `TokenUsage()`（全零）；`intent`/`schedule_name` 透传；`baseline_ref`/`diff_skipped_reason` 恒 None）
- 锁定 `schema_version = "1.1"`（因必定填充 meta）
- `metadata` 缺省 = `{}`
- 当 `inspector_results` 为空 list → raise `ValueError("from_inspector_results requires at least one InspectorResult")`（在 Pydantic validate 之前拦截，给更清晰的错误信息）

#### 场景:自动 flatten findings 并填充身份字段

- **当** 调用 `Report.from_inspector_results("t", [ir_a, ir_b], started_at=t0, finished_at=t1)` 且 `ir_a`（name=`insp.a` version=`1.0`）`.findings = [f1, f2]`、`ir_b`（name=`insp.b` version=`2.0`）`.findings = [f3]`
- **那么** `report.findings == [f1*, f2*, f3*]`（顺序保持），且 `report.findings[0].inspector_name == "insp.a"`、`report.findings[0].inspector_version == "1.0"`、`report.findings[2].inspector_name == "insp.b"`，每个 finding 的 `id` 非 None

#### 场景:flatten 不去重

- **当** `ir_a.findings = [Finding(severity="info", message="hi")]`、`ir_b.findings = [Finding(severity="info", message="hi")]`（两个 Finding 实例内容相同，但来自不同 inspector）
- **那么** `len(report.findings) == 2` 必须为 True

#### 场景:flatten 不排序

- **当** `ir_a.findings = [Finding(severity="info", message="a"), Finding(severity="critical", message="b")]`
- **那么** `report.findings[0].severity == "info"` 且 `report.findings[1].severity == "critical"`（保持 manifest 顺序）

#### 场景:自动生成唯一 report_id

- **当** 连续调用两次 `Report.from_inspector_results(...)`（参数完全相同）
- **那么** 两次返回的 `report.report_id` 必须不同（uuid4 概率上唯一）

#### 场景:工厂写 schema_version 1.1 且 meta 非 None

- **当** 调用 `Report.from_inspector_results(...)`
- **那么** `report.schema_version == "1.1"` 且 `report.meta is not None`（M3 工厂必填 meta，故锁 1.1，取代 M1 的「锁 1.0」）

#### 场景:meta.run_id 等于 report_id 字符串

- **当** 调用 `Report.from_inspector_results(...)`
- **那么** `report.meta.run_id == str(report.report_id)` 必须为 True

#### 场景:全 ok 派生 ok

- **当** `inspector_results` 全部 `status == "ok"` 调用工厂
- **那么** `report.meta.status == "ok"`

#### 场景:部分 timeout 但有 ok 仍 ok（§9）

- **当** `inspector_results` 是 `ok` 与 `timeout` 混合（至少一个 `ok`，非 ok 部分**仅** timeout）
- **那么** `report.meta.status == "ok"`（§9：单/部分 Inspector 超时不降级，「ok 除非全部超时」）

#### 场景:全 timeout 派生 partial

- **当** `inspector_results` 全部 `status == "timeout"`
- **那么** `report.meta.status == "partial"`（§9「除非全部超时」）

#### 场景:target_unreachable/exception/requires_unmet 派生 partial

- **当** `inspector_results` 含任一 `target_unreachable` / `exception` / `requires_unmet`
- **那么** `report.meta.status == "partial"`

#### 场景:meta.inspectors_used 机械投影

- **当** `inspector_results = [ir]` 且 `ir.name="x"`、`ir.version="1.2"`、`ir.status="ok"`、`ir.duration_seconds=0.5`、`ir.findings=[f1,f2]`
- **那么** `report.meta.inspectors_used == [InspectorRun(name="x", version="1.2", status="ok", duration_seconds=0.5, finding_count=2)]`

#### 场景:调用方可覆盖 status

- **当** 调用 `Report.from_inspector_results(..., status="degraded_token_budget")`
- **那么** `report.meta.status == "degraded_token_budget"`（覆盖默认派生）

#### 场景:token_usage 缺省全零

- **当** 调用工厂不传 `token_usage`
- **那么** `report.meta.token_usage == TokenUsage()`（四字段全 0）

#### 场景:空 inspector_results 列表 raise ValueError

- **当** 调用 `Report.from_inspector_results("t", [], started_at=t0, finished_at=t1)`
- **那么** 必须 raise `ValueError`，错误信息含 `from_inspector_results requires at least one InspectorResult`（**不**是 ValidationError——工厂方法在 Pydantic 之前拦截）

### 需求:`Report` 必须提供 `total_evidence_bytes` 字节计数访问器

`hostlens.reporting.models.Report` 必须暴露 `total_evidence_bytes(self) -> int` 实例方法，返回所有 `inspector_results[].findings[].evidence[]` 中**字符串型字段**的 UTF-8 字节总数：

- 计入：`Evidence.command` / `stdout` / `stderr` / `excerpt` / `path` / `metric_name`，以及 `metric_value`（仅当其为 `str` 时）
- 不计入：`exit_code`（int） / `truncated`（bool） / `metric_value`（float 时） / `data`（dict——M1 范围递归字节计数延后；docs 已记录此 known accepted risk）

调用方（CLI / Notifier）使用本方法决定是否触发"large report" 警告（CLI 当前阈值 8 MiB）；模型层只提供数字，不持有阈值常量。

#### 场景:空 Report 返回 0

- **当** 构造 `Report` 含一个无 findings 的 InspectorResult
- **那么** `report.total_evidence_bytes() == 0` 必须为 True

#### 场景:多字段累加

- **当** 构造 `Evidence(kind="command_output", command="echo hi", stdout="hi\n", stderr="err", exit_code=0)`（command 7 字节 + stdout 3 字节 + stderr 3 字节 = 13）
- **那么** 含该 evidence 的 Report 的 `total_evidence_bytes()` 必须 ≥ 13；`exit_code` 不计入

#### 场景:UTF-8 多字节字符按字节计算

- **当** `Evidence.command = "中"`（UTF-8 编码 3 字节，单 codepoint）
- **那么** 该 evidence 贡献 3 字节到 `total_evidence_bytes()`

#### 场景:float metric_value 不计入

- **当** `Evidence(kind="metric", metric_name="load_1min", metric_value=0.42)`
- **那么** 仅 `metric_name`（9 字节）计入；`metric_value` 因是 float 跳过

### 需求:`render_markdown.render` 必须输出固定 GFM 结构且对控制字符做转义

`hostlens.reporting.render_markdown.render(report: Report) -> str` 必须输出符合以下结构的 GitHub-Flavored Markdown 字符串：

1. **标题行**：`# Hostlens Inspection Report`
2. **Meta 表**：包含字段 `report_id` / `schema_version` / `target_name` / `intent`（None 时显示 `—`）/ `started_at` / `finished_at` / `duration_seconds` 的 2 列表格（Field | Value）
3. **`## Summary` 章节**：按 severity 分组的 finding 数量统计（如 `- critical: 2` / `- warning: 1` / `- info: 0`）；当无 finding 时输出 `_No findings._`
4. **`## Findings` 章节**：按 severity 倒序（critical → warning → info）分组；每个 finding 渲染为：
   - 标题 `### [{SEVERITY}] {message}` （severity 大写）
   - 当 `finding.evidence` 非空，渲染 `<details><summary>Evidence ({n} items)</summary>` 折叠块 + 每个 evidence 的 sub-table（按 kind 渲染对应字段；transparent 渲染 truncated 标记）
   - 当 finding 无 evidence，**不** 渲染 details 块（避免空 details 视觉噪音）
5. **`## 根因假设` 章节**（M3 add-only，位于 `## Findings` 之后、`## Inspector Results` 之前）：当 `report.hypotheses == []`（M3 本提案下恒成立）→ 输出 `_暂无根因假设_`；当非空（后续 Diagnostician 提案）→ 每个 hypothesis 渲染 `description` + `confidence` + 关联 finding ids（`supporting_findings`）+ `suggested_actions`。本章节为 add-only，**不改** 上面 1-4 与下面 6 的既有渲染行为。
6. **`## Inspector Results` 章节**：附录每个 `InspectorResult`，渲染 `name` / `version` / `status` / `target_name` / `duration_seconds` / `error`（None 时省略）；**`output` JSON** 渲染为 ` ```json ... ``` ` 围栏代码块；status != "ok" 时显式提示 `**Status:** {status}`（如 `**Status:** timeout`）

**控制字符转义**：渲染器**必须**对 evidence 的 `stdout` / `stderr` / `excerpt` / `command` 字段以及 InspectorResult.error 字段做控制字符 escape——保留 `\n` 和 `\t`，其他 `\x00-\x1f` 与 `\x7f` 转为 `\xXX` 字面量字符串（如 ANSI escape `\x1b[31m` 渲染为 `\\x1b[31m`）；**禁止** 直接写入原始字节到 markdown 输出。

**Env var 不展开**：渲染器**禁止** 把 `evidence.command` 中的 `$VAR` / `${VAR}` 替换为 `os.environ.get("VAR")`；模板字符串原样输出。

**单文件 ≤ 220 行**（含 imports 与 docstring；根因章节为本提案新增，行预算由 M1 的 200 放宽到 220）；**禁止**引入 Jinja2 依赖。

#### 场景:无 finding 时 Summary 章节显示 No findings

- **当** `report.findings == []`
- **那么** 渲染输出的 `## Summary` 章节下必须含字符串 `_No findings._`

#### 场景:按 severity 倒序排列 Findings

- **当** `report.findings = [Finding(severity="info", message="i"), Finding(severity="critical", message="c"), Finding(severity="warning", message="w")]`
- **那么** 渲染输出中 `### [CRITICAL] c` 必须出现在 `### [WARNING] w` 之前，后者必须出现在 `### [INFO] i` 之前

#### 场景:evidence 为空时不渲染 details 折叠块

- **当** `finding.evidence == []`
- **那么** 渲染输出中**不** 含 `<details>` 标签

#### 场景:evidence 非空时渲染 details 折叠块且数量正确

- **当** `finding.evidence` 含 2 个 Evidence
- **那么** 渲染输出含 `<details>` 块 + `<summary>Evidence (2 items)</summary>` + 2 个 evidence 的内容

#### 场景:控制字符被转义

- **当** `evidence.stdout = "ok\n\x1b[31mred\x1b[0m\n"`（含 ANSI escape）
- **那么** 渲染输出中**不** 含原始 `\x1b` 字节；**必须** 含字面量字符串 `\x1b[31mred\x1b[0m`（其中 `\n` 保留为换行）

#### 场景:env var 不被展开

- **当** `evidence.command = "psql -h $PGHOST -U $PGUSER"` 且环境中 `PGHOST=db.prod` 已设置
- **那么** 渲染输出含字面量 `psql -h $PGHOST -U $PGUSER`；**不** 含 `db.prod`

#### 场景:intent 为 None 时渲染为破折号

- **当** `report.intent is None`
- **那么** Meta 表中 `intent` 行的 Value 列必须显示**恰好** `—`（U+2014 EM DASH，单一 Unicode 码点）；**禁止** 用 ASCII hyphen `-` 或 `--` 字符串替代

#### 场景:InspectorResult.status != "ok" 时显式提示

- **当** `inspector_result.status == "timeout"` 且 `error == "collect.command exceeded 60 seconds"`
- **那么** 渲染输出在 `## Inspector Results` 章节对应条目下必须含 `**Status:** timeout` 与 `**Error:** collect.command exceeded 60 seconds`

#### 场景:无假设时根因章节显示占位

- **当** `report.hypotheses == []`
- **那么** 渲染输出必须含 `## 根因假设` 标题与 `_暂无根因假设_`

#### 场景:根因章节位于 Findings 之后 Inspector Results 之前

- **当** 渲染任意 report
- **那么** 输出中 `## 根因假设` 标题必须出现在 `## Findings` 之后、`## Inspector Results` 之前

#### 场景:渲染单次延迟 < 50ms（M1 范围）

- **当** 对一个 `Report` 含 1 个 InspectorResult + 0 findings 调用 `render(report)`
- **那么** 单次调用耗时 < 50ms（性能场景，M1 范围合理保证）

### 需求:`render_json.render` 必须先脱敏再走 Pydantic model_dump_json

`hostlens.reporting.render_json.render(report: Report) -> str` 必须：

1. **先** 调用 `hostlens.reporting._redact.redact_report_for_render(report) -> Report`（深拷贝 + 递归把所有字符串字段过 `hostlens.core.redact.redact_text` —— 覆盖范围严格对齐下一需求 §需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过 `core/redact.py` 中的字段路径表）
2. **再** 返回 `redacted_report.model_dump_json(indent=2, exclude_none=False)`

**禁止** 自定义序列化逻辑（如手动构造 dict 再 `json.dumps`）——序列化 SOT 是 Pydantic 模型字段集，脱敏 SOT 是 `redact_text`。**禁止** 跳过脱敏步骤直接 `report.model_dump_json(...)`（违反 OPERABILITY.md §7.2 硬约束）。

**包装目的**：让 M2 / M3 / M5 调用方统一从 `hostlens.reporting.render_json` 入口走；脱敏边界单点维护。

#### 场景:输出有效 JSON

- **当** 调用 `render(report)`
- **那么** `json.loads(result)` 必须成功且返回 dict

#### 场景:Pydantic round-trip 兼容（无敏感内容路径）

- **当** 调用 `data = json.loads(render(report))` 然后 `Report.model_validate(data)`（report 不含 `redact_text` 默认规则会命中的敏感字符串）
- **那么** 必须成功（除 `started_at` / `finished_at` 字符串 → datetime 由 Pydantic v2 自动 parse；`report_id` 字符串 → UUID 由 Pydantic v2 自动 parse）

#### 场景:含敏感内容时 round-trip 不要求 byte-equal

- **当** `report` 含敏感字符串（如 `evidence.stderr = "...sk-XXXX..."`）
- **那么** `Report.model_validate(json.loads(render(report)))` 必须仍**成功**（脱敏后字符串仍是合法 str），但 round-trip 后的 Report 中该字段值已是脱敏形式（脱敏不可逆是预期；保证 schema 结构兼容即可，**不**保证字节级 round-trip）

#### 场景:exclude_none=False（保留 None 字段）

- **当** `report.intent is None` 时调用 `render(report)`
- **那么** JSON 输出中必须含 `"intent": null` 字段（不可省略，让 schema 字段集对 schema 消费者可见）

#### 场景:indent=2 缩进

- **当** 调用 `render(report)`
- **那么** 输出字符串包含至少一处 `\n  "`（2 空格缩进证据）

### 需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过 `core/redact.py`

`render_markdown.render` 与 `render_json.render` **必须** 在写入输出之前对 `Report` 中所有字符串字段调用 `hostlens.core.redact.redact_text`，对齐 docs/OPERABILITY.md §7.2「任何写入 SQLite / 日志 / Notifier payload 的字符串都过 `core/redact.py`」硬约束 —— 本提案把 CLI stdout / `--output` 文件 / `render_*.render()` 返回值都视为等价 sink（用户可见、可被复制到 Notifier 或日志），统一在**渲染边界**强制脱敏：

- `hostlens.reporting.render_markdown.render(report)` 与 `hostlens.reporting.render_json.render(report)` **必须** 在写入输出之前，把 `Report` 中**所有字符串字段**通过 `hostlens.core.redact.redact_text(s: str) -> str` 过一遍。覆盖范围：
  - `Report.target_name` / `Report.intent`（None 不脱敏）/ `Report.metadata` 所有 value
  - `InspectorResult.name` / `InspectorResult.version` / `InspectorResult.target_name` / `InspectorResult.error` / `InspectorResult.output` 中所有字符串值（递归过 dict 与 list） / `InspectorResult.missing` 中每个字符串
  - `Finding.message` / `Finding.tags` 中每个字符串
  - `Evidence.command` / `Evidence.stdout` / `Evidence.stderr` / `Evidence.excerpt` / `Evidence.path` / `Evidence.metric_name` / `Evidence.metric_value`（仅当类型为 str 时）/ `Evidence.data` 中所有字符串值（递归）
- **不**脱敏：`report_id`（UUID） / `schema_version`（Literal） / `started_at` / `finished_at`（datetime） / 数值字段（`exit_code` / `duration_seconds` / `metric_value` 当为 float 时）
- **Report 内存对象本身不被修改**：脱敏是渲染前的拷贝转换（用 `model_dump` + 递归过滤再传给 `model_dump_json` 或 markdown 拼接）；runner / Agent loop 持有的 `Report` 仍保留 raw 内容（供 reasoning）
- `redact_text` 由 `hostlens.core.redact` 提供（M0 已落地 / 若未落地由本提案补建 stub 调用 OPERABILITY §7.2 默认正则）；脱敏规则继承 OPERABILITY §7.2（password/secret/token/api-key/bearer / JWT eyJ... / sk-xxxx）+ 保留前 4 后 4 字符

**已知接受约束**：

- stdout 与 `--output` 文件的脱敏一致（不区分 sink），用户若需 raw 内容须显式 opt-in `--no-redact`（M3+ 提案再设计；本提案 M1 范围**不**暴露该选项）
- 脱敏只对**字符串字段**生效；二进制 / dict / list 中嵌套的字符串递归过 redact_text
- 脱敏在渲染边界做（不在 `Report` 构造时做），保证 `Report` 模型对内部消费者透明

#### 场景:render_markdown 脱敏 evidence.stderr 中的 API key

- **当** `evidence.stderr = "ERROR: invalid api_key=sk-abcdefghijklmnopqrstuvwxyz1234567890"` 且 redact_text 默认规则启用
- **那么** `render_markdown.render(report)` 输出中**不** 含 `sk-abcdefghijklmnopqrstuvwxyz1234567890` 子串；含脱敏后形式（如 `sk-abcd...7890` 或 OPERABILITY §7.2 规则输出）

#### 场景:render_json 脱敏 evidence.stdout 中的 JWT

- **当** `evidence.stdout = "Authorization: Bearer eyJhbGciOiJIUzI1NiIs...XYZ"`
- **那么** `render_json.render(report)` 输出中**不** 含完整 JWT 字面量；含脱敏形式（保留前 4 后 4）

#### 场景:脱敏不破坏 Report 内存对象

- **当** 构造 `report = Report(...)` 含敏感字符串 → 调用 `render_markdown.render(report)` → 再访问 `report.findings[0].message`
- **那么** `report.findings[0].message` 仍是脱敏前的 raw 字符串（脱敏只发生在渲染拷贝路径，不修改源对象）

#### 场景:脱敏覆盖 Evidence.data 嵌套结构

- **当** `evidence = Evidence(kind="structured", data={"creds": {"password": "p@ssw0rd!"}, "level": "info"})`
- **那么** `render_json.render(report)` 输出中**不** 含 `p@ssw0rd!` 字面量；`level` 字段值 `info` 不受影响

#### 场景:数值字段不被脱敏

- **当** `evidence = Evidence(kind="metric", metric_name="load_1min", metric_value=0.42)`
- **那么** `render_json.render(report)` 输出中含 `"metric_value": 0.42`（float 字面量，不进 redact_text）

#### 场景:render_markdown 与 render_json 脱敏行为一致

- **当** 对同一 `report` 含敏感字符串 `"secret=sk-AAAA"` 分别调用 `render_markdown.render(report)` 与 `render_json.render(report)`
- **那么** 两份输出中均**不** 含 `sk-AAAA` 完整字面量；含相同的脱敏后形式（保证两个渲染器走同一 redact_text 路径，不漂移）

### 需求:`hostlens.reporting` 包导入零副作用

`hostlens.reporting.__init__` 模块**禁止** 在 import 时触发任何 IO / 全局状态修改 / registry 装配等副作用工作。仅允许：

- `from .models import Severity, Evidence, Finding, Report`
- `from .render_markdown import render as render_markdown`
- `from .render_json import render as render_json`
- 定义 `__all__`

理由：与 `hostlens.inspectors.__init__` 既定的"`__init__` 不做副作用工作"原则一致（见现有 `hostlens/inspectors/__init__.py` 注释）；让 `import hostlens.reporting` 保持廉价可预测。

#### 场景:import 触发 zero file IO

- **当** 在 sys.modules 中清除 `hostlens.reporting` 后 `import hostlens.reporting`
- **那么** 该 import 触发的文件读取数 == 0（除 Python 自身的 .py / .pyc 加载之外）

#### 场景:公开 API 完整

- **当** 执行 `from hostlens.reporting import Severity, Evidence, Finding, Report, render_markdown, render_json`
- **那么** 全部 import 必须成功

### 需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹

`Finding.id` 由 `Report.from_inspector_results` 计算，必须是**确定性**（同输入同输出）且**与 severity 无关**的内容指纹：`id = sha256(f"{inspector_name}\x00{inspector_version}\x00{message}".encode("utf-8")).hexdigest()[:16]`。指纹 helper **必须**要求 `inspector_name` 与 `inspector_version` 为非 None 字符串（工厂总是先填充再算 id；**禁止**用 None 参与指纹，否则 `"None\x00None\x00..."` 会让不同 inspector 的 finding 静默碰撞）。

- **禁止**使用 uuid / 随机数（跨 run 无法匹配、snapshot 不稳定）。
- **禁止**把 `severity` 纳入指纹——这样同一 finding 跨 run 变 severity 时 `id` 不变 → diff 才能识别 `changed_severity`（同 id、异 severity）；severity 进指纹则表现为「一个 resolved + 一个 added」假象。
- `id` 同时作 `RootCauseHypothesis.supporting_findings` 引用的 intra-report 锚点。
- **已接受约束**：同一 run 内同 `(inspector_name, inspector_version, message)` 但异 severity 的两个 finding 会得到相同 `id`（病态场景，记 design Open Question）。

#### 场景:相同内容产出相同 id

- **当** 两次以 `inspector_name="insp.x"`、`inspector_version="1.0"`、`message="disk 95%"` 计算 id（severity 一个 `warning` 一个 `critical`）
- **那么** 两次 `id` 必须**相同**（severity 不参与指纹）

#### 场景:不同 message 产出不同 id

- **当** 同 inspector 同 version 但 `message` 分别为 `"disk 95%"` 与 `"disk 96%"`
- **那么** 两个 `id` 必须不同

#### 场景:不同 inspector_version 产出不同 id

- **当** 同 inspector name 同 message 但 `inspector_version` 分别为 `"1.0"` 与 `"1.1"`
- **那么** 两个 `id` 必须不同（版本进指纹，支撑 diff 版本对齐）

#### 场景:None 参数被拒绝

- **当** 以 `inspector_name=None` 或 `inspector_version=None` 调指纹 helper
- **那么** 必须 raise（**禁止**产出 `"None\x00..."` 指纹）

### 需求:`ReportStatus` 必须是八值闭集 enum 且本提案只产出三值

`hostlens.reporting.models.ReportStatus` 必须是 `str, Enum`，含**恰好**八个值，对齐 docs/ARCHITECTURE.md §9 Failure Semantics 表的 `Report.meta.status` 列：`ok` / `partial` / `degraded_no_planner` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `empty_response` / `stored_as_orphan`。**禁止**包含 `failed_api_unavailable`——该场景无 Report 产出，归 M4 `RunStatus`（§7 边界）。

**本提案产出范围（防「定义即可测」误解）**：区分**自动派生**与 **override 透传**两条路径——
- **自动派生**只产出三值：`ok` / `partial`（由 `from_inspector_results` 按 §9 派生）、`stored_as_orphan`（由 `ReportStore` orphan 降级回写）。
- 其余五值（`degraded_no_planner` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `empty_response`）**本提案不由任何代码路径自动产出**；但工厂的 `status` **override 入口接受任一 `ReportStatus`**（透传，供 `add-diagnostician-agent` 传入）——「§场景:调用方可覆盖 status」正是验证该 override **透传机制**（以 `degraded_token_budget` 为样例值），**不**代表本提案实现了「何时该取该 degraded 值」的判定逻辑（那留给 `add-diagnostician-agent` 消费 `LoopResult.terminal_status`）。

本需求只验收「八值可构造 + `failed_api_unavailable` 被拒」。

#### 场景:接受八个合法值

- **当** 对八个值各构造 `ReportStatus(v)`
- **那么** 必须全部成功

#### 场景:拒绝 failed_api_unavailable

- **当** 试图 `ReportStatus("failed_api_unavailable")`
- **那么** 必须 raise `ValueError`（该值属 RunStatus，不在 ReportStatus）

### 需求:`RootCauseHypothesis` / `InspectorRun` / `BaselineRef` / `TokenUsage` / `ReportMeta` 模型字段集

以下 Pydantic v2 模型（`extra="forbid"`）必须按字段集定义，对齐 §10 Report Schema：

- `RootCauseHypothesis`：`description: str`、`confidence: Literal["low","medium","high"]`、`supporting_findings: list[str] = []`（finding id 引用）、`suggested_actions: list[str] = []`
- `InspectorRun`：`name: str`、`version: str`、`status: Literal["ok","timeout","target_unreachable","requires_unmet","exception"]`、`duration_seconds: float`、`finding_count: int`
- `BaselineRef`：`run_id: str`、`timestamp: datetime`、`status: ReportStatus`、`inspector_versions: dict[str, str] = {}`（name→version）、`report_schema_version: str`
- `TokenUsage`：`input_tokens: int = 0`、`output_tokens: int = 0`、`cache_creation_input_tokens: int = 0`、`cache_read_input_tokens: int = 0`（全字段默认 0；字段名/类型与 `LoopUsage` 对齐，便于 `TokenUsage(**loop_result.usage_totals.model_dump())` 投影——见 design 决策 4）
- `ReportMeta`：`run_id: str`、`report_schema_version: str = "1.1"`、`timestamp: datetime`（tz-aware 优先）、`target_id: str`、`target_name: str`、`target_type: str`（放宽为 str，docstring 列 canonical `local/ssh/docker/k8s/replay`；不用 Literal 以兼容 demo ReplayTarget 与未来类型）、`intent: str | None = None`、`schedule_name: str | None = None`、`status: ReportStatus`、`inspectors_used: list[InspectorRun] = []`、`token_usage: TokenUsage = TokenUsage()`、`duration_seconds: float`、`baseline_ref: BaselineRef | None = None`、`diff_skipped_reason: str | None = None`

#### 场景:TokenUsage 默认全零

- **当** 构造 `TokenUsage()`
- **那么** 四个字段必须均为 0

#### 场景:InspectorRun 可从 InspectorResult 机械投影

- **当** 由 `InspectorResult(name="x", version="1.0", status="ok", target_name="t", duration_seconds=0.3, findings=[f1])` 投影
- **那么** 得到的 `InspectorRun` 必须是 `InspectorRun(name="x", version="1.0", status="ok", duration_seconds=0.3, finding_count=1)`

#### 场景:RootCauseHypothesis 拒绝非法 confidence

- **当** 试图 `RootCauseHypothesis(description="x", confidence="maybe")`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:ReportMeta target_type 接受非 Literal 值

- **当** 构造 `ReportMeta(..., target_type="replay", ...)`
- **那么** 必须成功（target_type 是 str，不约束为 §10 的四值 Literal）

### 需求:渲染/落盘边界必须脱敏 `meta`/`hypotheses` 字符串并透传 Finding 身份字段

`redact_report_for_render` 在产生脱敏拷贝时，**必须**：

- 透传 `Finding.id` / `Finding.inspector_name` / `Finding.inspector_version`（**不**脱敏——id 是 hash、inspector name/version 非敏感；但必须在 `_redact_finding` 重构调用里带上，否则脱敏拷贝丢字段）
- **透传 `Finding.target_name`（本提案 add-only 来源字段）**：在 `_redact_finding` 重构 `Finding(...)` 调用里**必须**带上 `target_name`，与既有 `meta` / `report` 的 `target_name` 同样过 `redact_text`（`None` 透传 `None`），否则脱敏拷贝把它丢成 `None`。**理由（硬约束，非可选）**：所有 notifier 渲染入口（`telegram.py` / `lark.py` 的 `render`）**先** `redact_report_for_render`、**再**喂模板（见 notifier-telegram / notifier-lark spec），故提案 C 的多 target 分节与 `(target_name, inspector_name, message, severity)` 四元组去重消费的是**脱敏拷贝**的 finding；若 `_redact_finding` 不透传 `target_name`，渲染时 `target_name` 全 `None` → C 的退化判据 `distinct(non-None target_name) ≤ 1` 命中 → 多 target 分节**永不触发**、跨主机同 finding 因 `target_name` 全 `None` 被四元组误并 → fleet 报告**静默丢失主机维度**。`redact_text` 对正常 target 名是 no-op 但确定且幂等，不破坏去重 / 分节的同值比较。**既有回归门**：`tests/reporting/test_redact_m3_fields.py` 的 add-only 字段透传守门测试**必须**同步加 `target_name` 存活断言。
- 透传 `Report.meta`（`meta is None` 时保持 None）并对其内字符串字段（`target_name` / `intent` / `target_id` / `schedule_name`）过 `redact_text`；透传 `Report.hypotheses` 并对 `description` / `suggested_actions` 过 `redact_text`
- `meta.token_usage` / `inspectors_used` / `status` 等数值与枚举字段不脱敏，原样透传
- **不变量**：脱敏拷贝**必须**保留 `meta`（除非源就是 None）——否则 `render_json` 输出缺 meta，`ReportStore` 落盘后 round-trip 取回的报告 meta 丢失（store 的索引列另从内存 `report.meta` 投影，不依赖脱敏 JSON，见 report-persistence spec）

#### 场景:脱敏拷贝保留 Finding 身份字段

- **当** 对含 `Finding(id="abc", inspector_name="insp", inspector_version="1.0", ...)` 的 report 调 `redact_report_for_render`
- **那么** 返回报告对应 finding 的 `id == "abc"`、`inspector_name == "insp"`、`inspector_version == "1.0"`（未丢失）

#### 场景:脱敏拷贝保留 Finding 来源 target_name（fleet 分节 / 去重前提）

- **当** 对含 `Finding(message="cpu high", target_name="bandwagon", ...)` 的 fleet report 调 `redact_report_for_render`
- **那么** 返回报告对应 finding 的 `target_name == "bandwagon"`（**未丢成 `None`**）——使下游提案 C 渲染层的多 target 分节与四元组去重在脱敏拷贝上仍有真实来源值

#### 场景:脱敏拷贝保留并脱敏 meta

- **当** `report.meta is not None` 且 `report.meta.intent` 含敏感字符串 `"token=sk-ABCDEFGHIJKLMNOPQRSTUVWX1234"`
- **那么** `redact_report_for_render(report).meta is not None` 且其 `intent` 不含完整 `sk-ABCDEFGHIJKLMNOPQRSTUVWX1234` 字面量

#### 场景:legacy 无 meta 报告脱敏不崩

- **当** 对 `report.meta is None` 的 legacy 报告调 `redact_report_for_render`
- **那么** 必须成功且返回报告 `meta is None`

### 需求:多 target Report 必须由确定性 fleet 组装路径产出

除既有单 target `Report.from_inspector_results`（target 单值）外,**必须**提供一条多 target（fleet）Report 组装路径,接受**跨多个 target** 的 `InspectorResult` 列表并组装成**一份** Report,供确定性巡检模式（见 `deterministic-inspection-mode` 能力）的逐 target 采集结果聚合使用。该路径**必须**:

- 接受多 target 的 `inspector_results`（每个 `InspectorResult` 携带自己的 `target_name`）。
- 把 `Report.target_name` 设为**确定性 fleet 标签**——由参与的 target 名派生，派生前**必须对 target 名集合先排序取规范序**（**不依赖调用方传入的 target 顺序**），再按确定性规则 join，满足 `Report.target_name` 的 `min_length=1` 约束;同一组 target（无论传入顺序）**必须**派生同一标签（确定性、可复现）。
- 把 `meta.target_id` 设为**确定性 fleet id**——由 target_id 集合（**先排序取规范序**）+ `schedule_name` 派生,使**不同 fleet**（不同 target 集合或不同 schedule）得到**不同** `target_id`,避免在 `ReportStore` 中撞 store key（per-target store key 复用既有 target_id-keyed 语义）；确定性**不得**依赖调用方传入 target 的顺序（未来扇出若重排 target，fleet id 不得漂移、否则 store key churn 误判 baseline miss）。该派生**必须**落在**与裸 `target_name` 不相交的命名空间**——fleet id **必须**带一个不可能等于任何裸 target_name 的限定（如强制 `fleet:` 前缀）。理由:per-target report 的 `target_id` 缺省 == `target_name`(见 report-persistence);**单成员** deterministic fleet（`targets:[x]`,schedule-manifest 允许）若朴素派生出 `target_id == "x"`,会与该机 agent 模式 per-target report **撞 store key**,使 `compute_diff` 的「`target_id` 不等才 raise」防线**失效**、fleet report 被误与 per-target report 互 diff——正是本能力「fleet 无 per-target diff」非目标要禁止的污染。前缀限定把这条软约束变硬。
- flatten findings 时给**每条** finding 盖**来源** `target_name`（取自该 finding 所属 `InspectorResult.target_name`）,使一份 fleet Report 内可按来源 target 区分 findings。
- flatten findings 时**必须**与既有 `from_inspector_results` **一致地填充 M3 身份字段** `id` / `inspector_name` / `inspector_version`（取自该 finding 所属 `InspectorResult`,`id` 经 `compute_finding_id` 计算）——**仅** `target_name` 取来源 target,身份字段**不得留 None**。**下游依赖**:提案 C 的多 target 渲染按 `(target_name, inspector_name, message, severity)` 四元组去重,fleet finding 的 `inspector_name` 若为 None 会让不同 inspector 的同 message/severity finding 被误并;故 fleet 路径填身份字段是 C 去重正确性的硬前提。最省力实现 = fleet 路径**复用** `from_inspector_results` 的 per-finding 加工逻辑(身份字段 + 来源 target 一起盖)。
- 组装 `meta.inspectors_used` 时**必须**为**每个**参与的 `(target, inspector)` 留一条 `InspectorRun`、其 `status` **逐项保真**（含 `requires_unmet` / `timeout` / `target_unreachable` / `exception`，不折叠不删除）。**下游依赖**:提案 C 的覆盖行 `{ok}/{total} 项检查 · {skipped} 项跳过` 从 `meta.inspectors_used[].status == requires_unmet` 计 `{skipped}`;deterministic 模式的「`requires_unmet` 不降级」override（见 `deterministic-inspection-mode` / `scheduler-engine`）**仅**作用于 `meta.status` 报告级派生,**禁止**借此从 `inspectors_used` 删除或改写任何逐项记录,否则 C 的覆盖行 `{skipped}` 静默归零、`{total}` 缩水而无人报错。

既有单 target `from_inspector_results` 行为**不变**（target 单值、不强制盖 finding 来源 target_name）。

#### 场景:多 target 组装产出一份 Report

- **当** 以 `targets=[a, b]` 的混合 `InspectorResult`（`a` 与 `b` 各自的结果各带其 `target_name`）经 fleet 组装路径组装
- **那么** **必须**产出**一份** `Report`,其 `inspector_results` 含 a 与 b 的全部结果,`findings` 是跨 a/b 的扁平视图

#### 场景:fleet Report 的 findings 带来源 target_name

- **当** fleet 组装路径 flatten `targets=[a, b]` 的 findings
- **那么** 来自 `a` 的 `InspectorResult` 的每条 finding `target_name == "a"`,来自 `b` 的每条 finding `target_name == "b"`

#### 场景:fleet Report 的 findings 身份字段非 None（C 去重前提）
- **当** fleet 组装路径 flatten 一条来自某 inspector 的 finding
- **那么** 该 finding 的 `inspector_name` / `inspector_version` / `id` **必须**非 None（与既有 `from_inspector_results` 一致填充）,**仅** `target_name` 取来源 target;**禁止**只盖 `target_name` 而留身份字段 None

#### 场景:fleet 的 inspectors_used 逐项保真 requires_unmet（C 覆盖行前提）
- **当** fleet 组装一组结果含一个 `status == requires_unmet` 的 inspector,且 deterministic 模式对 `meta.status` 应用「`requires_unmet` 不降级」override
- **那么** `meta.inspectors_used` **必须**仍含该 inspector 的 `InspectorRun` 且其 `status == "requires_unmet"`（逐项记录不被 override 删除 / 改写）;`meta.status` 不因它降级 partial，但 `inspectors_used` 保真——使提案 C 覆盖行的 `{skipped}` 计数能从中数出 ≥ 1

#### 场景:fleet target_id 由有序 target 集合与 schedule 确定性派生

- **当** 对同一组 `targets`（同序）+ 同一 `schedule_name` 两次组装 fleet Report
- **那么** 两次的 `meta.target_id` **必须相同**;而对**不同** target 集合或不同 `schedule_name` 组装时 `meta.target_id` **必须不同**（避免不同 fleet 撞 store key）

#### 场景:单成员 fleet 的 target_id 不撞该成员的 per-target target_id
- **当** 以 `targets=[x]` 组装 deterministic fleet Report,且该机另有 agent 模式 per-target report（`meta.target_id == "x"`）
- **那么** fleet Report 的 `meta.target_id` **必须 ≠ `"x"`**（带 `fleet:` 类限定前缀）,使二者在 `ReportStore` 不撞 key、`compute_diff` 不会跨 fleet/per-target 误取基线

#### 场景:fleet target_name 标签确定性

- **当** 对同一组 `targets`（同序）两次组装 fleet Report
- **那么** 两次的 `Report.target_name` **必须相同**且满足 `min_length=1`

### 需求:fleet（多 target）Report 的 per-target regression diff 是非目标

多 target（fleet）Report 是 **notify 导向**的聚合产物;**per-target regression diff 仍只在 per-target（agent 模式）report 上做**。fleet Report 持有**单一** `meta.target_id`（fleet id),**无法**为其内含的每个 target 取 per-target baseline,故**禁止**期望对 fleet Report 做 per-target regression diff。`report-regression-diff` 的 target_id-keyed baseline 语义对 fleet Report **不适用**:fleet Report 的 baseline（若做）只能是「同 fleet id 的上一份 fleet Report」整体比对,**不**拆分到每个 target。本提案**不**为 fleet Report 实现任何 diff;regression diff 的既有 per-target 契约不变。**反向依赖提示**:提案 C 的 finding-id message-churn 免责（inspector-authoring-contract）依赖**本条「不为 fleet Report 实现任何 diff」**——若未来给 fleet Report 加**任何** diff（per-target **或** fleet-level「同 `meta.target_id`(fleet id) 整体比对」;`compute_finding_id` 恒 hash `message`、与 diff 粒度无关,故两种粒度都会让 message 改写产生一次性 `resolved`/`added` churn），须同步评估并撤销 C 的 churn 免责叙述。

#### 场景:fleet Report 不期望 per-target baseline

- **当** 一份 fleet（多 target）Report 落盘后
- **那么** **禁止**对其执行 per-target regression diff（按各内含 target 分别取 baseline）;per-target diff 仅适用于 agent 模式的单 target report
