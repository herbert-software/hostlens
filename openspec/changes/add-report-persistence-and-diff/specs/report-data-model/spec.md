## 修改需求

### 需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT

`hostlens.reporting.models.Finding` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段——四个 M1 核心字段（不变）+ 三个 M3 add-only 身份字段（全部带默认值，旧构造方与旧 JSON 零改动可加载）：

核心字段（M1，不变）：

- `severity: Severity`
- `message: str`（min_length=1）
- `evidence: list[Evidence] = []`
- `tags: list[Tag] = []`（M1 finding DSL 不生产 tags；用于 M5 Notifier `only_if` 路由；每个 tag 约束 `^[a-z][a-z0-9_-]*$`）

M3 add-only 身份字段（用于 diff 指纹与根因假设引用；见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹）：

- `id: str | None = None`（确定性内容指纹；`from_inspector_results` 自动计算；直接构造或 legacy JSON 缺省为 None）
- `inspector_name: str | None = None`（产出该 finding 的 Inspector name；工厂从所属 `InspectorResult.name` 填充）
- `inspector_version: str | None = None`（Inspector version；工厂从 `InspectorResult.version` 填充；diff 版本对齐用）

`extra="forbid"` 仍生效。`hostlens.reporting.models.Finding` 是 **唯一 SOT**；以下 import path 必须是 type alias re-export，**禁止**独立定义：

- `hostlens.inspectors.result.Finding` = `from hostlens.reporting.models import Finding as Finding`
- `hostlens.tools.schemas.run_inspector.FindingSummary` = `FindingSummary = Finding`

#### 场景:Finding 字段集严格（核心四字段 + M3 身份字段，拒绝未声明字段）

- **当** 试图 `Finding(severity="info", message="x", evidence=[], tags=[], extra="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:Finding 默认 evidence 与 tags 为空 list

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.evidence == []` 且 `finding.tags == []` 必须均为 True

#### 场景:Finding 仅核心字段时身份字段默认 None

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.id is None` 且 `finding.inspector_name is None` 且 `finding.inspector_version is None` 必须均为 True

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

#### 场景:legacy 缺身份字段的 dict 可加载

- **当** 执行 `Finding.model_validate({"severity": "info", "message": "x"})`（旧 schema 1.0 产出的 finding，无 id/inspector_name/inspector_version）
- **那么** 必须成功且三个身份字段为 None（add-only 向后兼容）

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

## 新增需求

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
- 透传 `Report.meta`（`meta is None` 时保持 None）并对其内字符串字段（`target_name` / `intent` / `target_id` / `schedule_name`）过 `redact_text`；透传 `Report.hypotheses` 并对 `description` / `suggested_actions` 过 `redact_text`
- `meta.token_usage` / `inspectors_used` / `status` 等数值与枚举字段不脱敏，原样透传
- **不变量**：脱敏拷贝**必须**保留 `meta`（除非源就是 None）——否则 `render_json` 输出缺 meta，`ReportStore` 落盘后 round-trip 取回的报告 meta 丢失（store 的索引列另从内存 `report.meta` 投影，不依赖脱敏 JSON，见 report-persistence spec）

#### 场景:脱敏拷贝保留 Finding 身份字段

- **当** 对含 `Finding(id="abc", inspector_name="insp", inspector_version="1.0", ...)` 的 report 调 `redact_report_for_render`
- **那么** 返回报告对应 finding 的 `id == "abc"`、`inspector_name == "insp"`、`inspector_version == "1.0"`（未丢失）

#### 场景:脱敏拷贝保留并脱敏 meta

- **当** `report.meta is not None` 且 `report.meta.intent` 含敏感字符串 `"token=sk-ABCDEFGHIJKLMNOPQRSTUVWX1234"`
- **那么** `redact_report_for_render(report).meta is not None` 且其 `intent` 不含完整 `sk-ABCDEFGHIJKLMNOPQRSTUVWX1234` 字面量

#### 场景:legacy 无 meta 报告脱敏不崩

- **当** 对 `report.meta is None` 的 legacy 报告调 `redact_report_for_render`
- **那么** 必须成功且返回报告 `meta is None`
