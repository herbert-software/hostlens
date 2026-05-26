## 新增需求

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

`hostlens.reporting.models.Finding` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下四个字段（M1 最小集；M3 add-only 扩展 `id` / `inspector_run_id` / `seen_at` / `fingerprint` 等）：

- `severity: Severity`
- `message: str`（min_length=1）
- `evidence: list[Evidence] = []`
- `tags: list[str] = []`（M1 finding DSL 不生产 tags；本字段用于 M5 Notifier `only_if` 路由表达式 —— 对齐 CLAUDE.md §4.4 「`only_if` 表达式（基于报告 severity / finding tags）决定是否发送」契约；每个 tag 字符串约束 `^[a-z][a-z0-9_-]*$`，loader / DSL 扩展提案在赋值时强制）

`hostlens.reporting.models.Finding` 是 **唯一 SOT**；以下 import path 必须是 type alias re-export，**禁止**保留独立定义：

- `hostlens.inspectors.result.Finding` = `from hostlens.reporting.models import Finding as Finding`
- `hostlens.tools.schemas.run_inspector.FindingSummary` = `FindingSummary = Finding`

#### 场景:Finding 字段集严格四字段

- **当** 试图 `Finding(severity="info", message="x", evidence=[], tags=[], extra="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:Finding 默认 evidence 与 tags 为空 list

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.evidence == []` 且 `finding.tags == []` 必须均为 True

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

### 需求:`Report` Pydantic 模型必须严格 conform M1 字段集与 schema_version 锁定

`hostlens.reporting.models.Report` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段：

- `report_id: UUID`（必填；构造方推荐用 `uuid4()` 但模型不强制——`Report.from_inspector_results()` 工厂方法自动生成）
- `schema_version: Literal["1.0"]`（本提案锁定恰好 `"1.0"`；M3 add-only 扩展为 `Literal["1.0", "1.1"]` 等）
- `intent: str | None = None`（M1 留 None，M2 Planner Agent 填自然语言意图）
- `target_name: str`（min_length=1）
- `inspector_results: list[InspectorResult]`（min_length=1；引用 `hostlens.inspectors.result.InspectorResult`）
- `findings: list[Finding] = []`（聚合自所有 `inspector_results[].findings` 的扁平视图；构造时机械 flatten，**不** 做去重 / 排序）
- `started_at: datetime`
- `finished_at: datetime`
- `metadata: dict[str, str] = {}`

**模型级 `model_validator(mode="after")` 必须** 强制：

- `finished_at >= started_at` —— 违反 → raise `pydantic.ValidationError("finished_at must be >= started_at")`
- 当 `inspector_results` 为空 list → raise（`min_length=1` 字段约束）

#### 场景:Report 字段集严格

- **当** 试图 `Report(report_id=..., schema_version="1.0", target_name="x", inspector_results=[ir], started_at=..., finished_at=..., extra_field="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:schema_version 锁定 1.0

- **当** 试图 `Report(..., schema_version="2.0", ...)`
- **那么** 必须 raise `pydantic.ValidationError`（Literal["1.0"] 拒绝非 "1.0" 值）

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

`Report.from_inspector_results(target_name, inspector_results, *, intent=None, started_at, finished_at, metadata=None) -> Report` 必须：

- 自动生成 `report_id = uuid4()`
- 自动锁定 `schema_version = "1.0"`
- 自动 flatten findings：`findings = [f for ir in inspector_results for f in ir.findings]`（按 inspector_results 顺序 + 每个 InspectorResult 内部 findings 顺序；**不** 做去重 / 排序 / 过滤）
- `metadata` 缺省 = `{}`
- 当 `inspector_results` 为空 list → raise `ValueError("from_inspector_results requires at least one InspectorResult")`（在 Pydantic validate 之前拦截，给更清晰的错误信息）

#### 场景:自动 flatten findings

- **当** 调用 `Report.from_inspector_results("t", [ir_a, ir_b], started_at=t0, finished_at=t1)` 且 `ir_a.findings = [f1, f2]`、`ir_b.findings = [f3]`
- **那么** `report.findings == [f1, f2, f3]` 必须为 True

#### 场景:flatten 不去重

- **当** `ir_a.findings = [Finding(severity="info", message="hi")]`、`ir_b.findings = [Finding(severity="info", message="hi")]`（两个 Finding 实例内容相同）
- **那么** `len(report.findings) == 2` 必须为 True

#### 场景:flatten 不排序

- **当** `ir_a.findings = [Finding(severity="info", message="a"), Finding(severity="critical", message="b")]`
- **那么** `report.findings[0].severity == "info"` 且 `report.findings[1].severity == "critical"`（保持 manifest 顺序）

#### 场景:自动生成唯一 report_id

- **当** 连续调用两次 `Report.from_inspector_results(...)`（参数完全相同）
- **那么** 两次返回的 `report.report_id` 必须不同（uuid4 概率上唯一）

#### 场景:schema_version 锁定 1.0

- **当** 调用 `Report.from_inspector_results(...)`
- **那么** `report.schema_version == "1.0"` 必须为 True

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
5. **`## Inspector Results` 章节**：附录每个 `InspectorResult`，渲染 `name` / `version` / `status` / `target_name` / `duration_seconds` / `error`（None 时省略）；**`output` JSON** 渲染为 ` ```json ... ``` ` 围栏代码块；status != "ok" 时显式提示 `**Status:** {status}`（如 `**Status:** timeout`）

**控制字符转义**：渲染器**必须**对 evidence 的 `stdout` / `stderr` / `excerpt` / `command` 字段以及 InspectorResult.error 字段做控制字符 escape——保留 `\n` 和 `\t`，其他 `\x00-\x1f` 与 `\x7f` 转为 `\xXX` 字面量字符串（如 ANSI escape `\x1b[31m` 渲染为 `\\x1b[31m`）；**禁止** 直接写入原始字节到 markdown 输出。

**Env var 不展开**：渲染器**禁止** 把 `evidence.command` 中的 `$VAR` / `${VAR}` 替换为 `os.environ.get("VAR")`；模板字符串原样输出。

**单文件 ≤ 200 行**（含 imports 与 docstring）；**禁止**引入 Jinja2 依赖。

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

对齐 docs/OPERABILITY.md §7.2「任何写入 SQLite / 日志 / Notifier payload 的字符串都过 `core/redact.py`」硬约束 —— 本提案把 CLI stdout / `--output` 文件 / `render_*.render()` 返回值都视为等价 sink（用户可见、可被复制到 Notifier 或日志），统一在**渲染边界**强制脱敏：

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
