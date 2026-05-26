## 为什么

M1 第三块也是最后一块基石。`add-execution-target-abstraction` + `add-inspector-plugin-system` 落地后，Hostlens 已经能"在 Local / SSH target 上跑 Inspector 拿到 `InspectorResult`"——但**还没有端到端的 user-facing 出口**：`InspectorResult` 只是单 Inspector 的内部数据结构，没有报告容器、没有 markdown / json 渲染、也没有 CLI 命令让人类（与 M2 Planner Agent）把"我想巡检 prod-web-01"这件事跑到底拿到一份报告。M1 退出条件中"`hostlens inspect localhost --inspector hello.echo` 输出一份合法 JSON + 同样内容的 markdown 报告"在本提案落地前不成立。

M2 `add-agent-loop-skeleton` 的 demo path 也卡在这里：Planner Agent 调度多个 Inspector 后必须把结果聚合成一份**面向人类与下游 Notifier** 的 `Report`，否则 markdown / Telegram / 飞书三套渲染都没有统一上游。M3 `Diagnostician + 报告体系` 提案还会扩展 `Report` 加入跨信号关联、根因假设、regression diff——但前提是本提案先把 `Report` / `Finding` / `Severity` / `Evidence` 的**最小可用容器**立起来，并固化字段集，让 M3 走 add-only 扩展而非破坏性重写。

CLAUDE.md §4.4 已把"Notifier 必须是抽象 + 适配器"钉死："任何加一个新通道的需求都必须只新增一个文件、不改主流程"——这暗含 `Report` 是 Notifier 的统一上游，所有 Notifier 的 `render(report)` 都消费同一份 `Report` 对象。本提案锁定的 `Report` schema 就是这个契约。

本提案的任务是把以上契约从架构文档搬进 spec 与 `src/hostlens/reporting/` + `src/hostlens/cli/`，并交付：**`Report` / `Finding` / `Severity` / `Evidence` Pydantic 模型 + markdown / json 双渲染器 + `hostlens inspect <target> --inspector <name>` CLI（M1.6 + M1.7）**。同时**消除 `Finding` 与 `FindingSummary` 两份"近似但分裂"的定义**——把 `hostlens.inspectors.result.Finding` 与 `hostlens.tools.schemas.run_inspector.FindingSummary` 统一收归到 `hostlens.reporting.models.Finding` 这一个 SOT，前两者通过 re-export 或类型别名保持 import path 兼容（M2 `register_default_tools` 与 `RunInspectorOutput.findings: list[FindingSummary]` schema 声明零修改；但 `default_tools.py` 中 `_run_inspector_handler` 的 evidence dict comprehension 必须改为 list 直接复用——详见下方"修改代码"段）。

完成后 M1 退出条件全部成立；M2 Planner Agent 端到端 demo path 直接消费本提案的 `Report` 容器；M3 / M5 提案的扩展点（finding identity 字段 / Notifier 适配器消费契约）锚定到 `report-data-model` 这个独立 capability spec 上。

## 变更内容

**新增（Report 数据模型 SOT）：**

- `hostlens.reporting.models.Severity` Literal：**恰好** `{"info", "warning", "critical"}` 三值；与 archived `inspector-plugin-system` 的 Finding DSL severity 严格对齐
- `hostlens.reporting.models.Evidence` Pydantic 模型：**结构化承载**单条证据，字段集 = `kind: Literal["command_output", "file_excerpt", "metric", "structured"]` + `command: str | None` + `stdout: str | None` + `stderr: str | None` + `exit_code: int | None` + `path: str | None` + `excerpt: str | None` + `metric_name: str | None` + `metric_value: float | str | None` + `data: dict[str, Any] | None` + `truncated: bool = False`；`model_config = ConfigDict(extra="forbid", frozen=True)`；模型级 `model_validator` 强制 `kind` ↔ 字段子集映射（如 `kind="command_output"` 必须有 `command` 且 `path` / `metric_*` / `data` 为 None；详见 spec 场景）
- `hostlens.reporting.models.Finding` Pydantic 模型：**字段集严格扩展自现有 `hostlens.inspectors.result.Finding`**——`severity: Severity` + `message: str` + `evidence: list[Evidence] = []` + `tags: list[str] = []`（**两个 BREAKING**：`evidence` 由 `dict[str, str]` 变为 `list[Evidence]`；新增 `tags` 字段以兑现 CLAUDE.md §4.4 Notifier `only_if` 路由契约——M5 Notifier 路由表达式基于 `report.findings[].severity` + `report.findings[].tags`，M1 finding DSL 不生产 tags 但 schema 必须现在锁定，避免 M5 BREAKING；详见 spec 与 design）；`model_config = ConfigDict(extra="forbid", frozen=True)`
- `hostlens.reporting.models.Report` Pydantic 模型：聚合容器，字段集 = `report_id: UUID`（loader 默认 `uuid4()`）+ `schema_version: Literal["1.0"]`（本提案锁定 1.0；M3 扩展为 `Literal["1.0", "1.1"]` 等 add-only）+ `intent: str | None = None`（M1 留 None，M2 Planner Agent 填自然语言意图）+ `target_name: str` + `inspector_results: list[InspectorResult]`（**复用** `hostlens.inspectors.result.InspectorResult`，不重定义）+ `findings: list[Finding]`（聚合自所有 `inspector_results[].findings` 的扁平视图；本提案要求 `Report` 构造时**机械** flatten，不做去重 / 排序，由 `from_inspector_results()` 工厂方法生成）+ `started_at: datetime` + `finished_at: datetime` + `metadata: dict[str, str] = {}`（M5 Notifier 可写入路由 tag，M1 留空）；`model_config = ConfigDict(extra="forbid", frozen=True)`
- `hostlens.reporting.models.Report.from_inspector_results(target_name, inspector_results, *, intent=None, started_at, finished_at, metadata=None) -> Report`：唯一推荐构造路径；自动 flatten findings、自动生成 `report_id`、自动锁定 `schema_version="1.0"`、`metadata` 缺省 None 时 Report 用 `{}`（与 spec §需求:`Report.from_inspector_results` 一致）
- **循环导入处理（重要实现约束）**：`reporting.models.Report` 的 `inspector_results: list[InspectorResult]` 字段引用 `hostlens.inspectors.result.InspectorResult`，而 `inspectors.result.Finding` 又 type-alias re-export `reporting.models.Finding`——直接导入会形成循环。**实现必须**：(a) `reporting/models.py` 顶部用 `from __future__ import annotations` + `if TYPE_CHECKING: from hostlens.inspectors.result import InspectorResult` 推迟类型解析；(b) 在 `inspectors/result.py` 模块尾部 `from hostlens.reporting.models import Report as _Report; _Report.model_rebuild(_types_namespace={"InspectorResult": InspectorResult}, force=True)` 完成 forward-ref 解析（**必须**显式传 `_types_namespace`，因为 TYPE_CHECKING 隔离了 InspectorResult，运行时 globals 不可见；**必须**传 `force=True`，否则 Pydantic v2 在 `_Report` 已部分 build 时会直接返回 None 跳过重建，导致 forward ref 静默未解析）；(c) `hostlens.reporting.__init__` **不**触发 `Report.model_rebuild()` 自动调用（保持 import 零副作用），用户首次构造 `Report` 前应先 import `hostlens.inspectors.result`（在 CLI / Agent loop 的 entrypoint 已自然满足）；详见 design.md §决策 1 与 spec §需求:`hostlens.reporting` 包导入零副作用

**新增（渲染器；纯机械逻辑，**不**用 Jinja2 模板）：**

- `hostlens.reporting.render_markdown.render(report: Report) -> str`：单文件 ≤ 200 行（含 imports + docstring；与 spec / tasks 上限一致）；输出结构 = `# Hostlens Inspection Report` 标题 + meta 表（report_id / schema_version / target_name / intent / started_at / finished_at / duration）+ `## Summary` 按 severity 分组的 finding 数量统计 + `## Findings` 按 severity 倒序（critical → warning → info）的 finding 列表（含每个 finding 的 message + evidence 折叠块）+ `## Inspector Results` 附录每个 `InspectorResult` 的 status / duration / output JSON / error；纯 Python f-string + `io.StringIO` 拼接，**禁止**引入 Jinja2 依赖
- `hostlens.reporting.render_json.render(report: Report) -> str`：**必须**先调用 `hostlens.reporting._redact.redact_report_for_render(report)` 生成脱敏后的 `Report` 副本（覆盖 spec §需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过 `core/redact.py` 中所有字段路径）→ 再 `return redacted.model_dump_json(indent=2, exclude_none=False)`；**禁止**跳过脱敏直接序列化原 Report；包装目的是让 M2 / M3 / M5 调用方都从 `hostlens.reporting.render_json` 这一个入口走，脱敏边界单点维护

**新增（CLI）：**

- `hostlens inspect <target> --inspector <name> [--output FILE] [--format md|json] [--parameters JSON] [--allow-privileged] [--timeout SECONDS]`：
  - `target` 从 TargetRegistry 取（`local` 或 `hostlens target add` 已注册的 SSH target name）；未找到 → exit 3 + stderr 显示 `target not found: <name>`
  - `inspector` 从 InspectorRegistry 取；未找到 → exit 3 + stderr 显示 `inspector not found: <name>`
  - `--parameters` 接受 JSON 字符串（如 `'{"host": "db.prod.internal"}'`）或 `@<path>` 文件引用；JSON 解析失败 → exit 3
  - `--format` 缺省 `md`；与 `--output` 文件后缀**不强校验**（即允许 `--format json --output report.md`，理由：CLI 不替用户决定文件命名）
  - `--allow-privileged` opt-in；与 archived `add-inspector-plugin-system` spec §需求:CLI 的 inspector preflight 行为对齐
  - `--timeout` 单 Inspector 超时（缺省 None = 不覆盖，使用 manifest `collect.timeout_seconds`）；**实现路径**：`InspectorRunner.run()` 当前不接受 timeout 覆盖参数，CLI 必须在 dispatch 前**重构 CollectSpec 让 Pydantic 验证生效** —— `new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})`；直接 `manifest.collect.model_copy(update=...)` 会**绕过** Pydantic v2 字段约束 validation（这是 v2 已知行为），必须经过 `CollectSpec(**...)` 构造让 `Field(ge=1, le=300)` 触发；**不**修改 `InspectorRunner.run()` 签名（避免本提案改动 inspector-plugin-system spec 的 runner 契约）；**上限**：CLI 层先校验 1 ≤ value ≤ 300（与 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 字段约束严格一致），不在范围 → exit 3；下游 CollectSpec 构造时再次 validate 兜底（防御纵深）；**不**引入新 `Settings` 字段
  - **退出码语义化**（与 `hostlens doctor` 已有风格对齐；详见 design.md §3）：
    - `0` = 所有 finding ≤ warning **且** inspector status == "ok"
    - `1` = 至少一个 critical finding（inspector 跑成功但报出严重问题，CI 友好的"巡检告警"信号）
    - `2` = inspector status != "ok"（`timeout` / `target_unreachable` / `requires_unmet` / `exception`；runner 内部失败，区别于"业务发现严重问题"）
    - `3` = 参数 / 配置错误（target / inspector 未找到、`--parameters` JSON 解析失败、`--format` 无效值）
  - 写操作合规：本命令是**只读**（Inspector 不改远端状态），允许 EUID==0 运行（与 `hostlens inspectors list/show` / `hostlens target list` 一致）；非交互无 TTY 缺 `--allow-privileged` 时遇到 `privilege != "none"` 的 Inspector 走 runner 既定路径返回 `requires_unmet` 然后 exit 2（不假装成功也不 hang）

**修改（消除 Finding 双地址）：**

- `hostlens.inspectors.result.Finding` 改为 `from hostlens.reporting.models import Finding as Finding`（type alias re-export；零行为变更；保持原 import path 不破坏 M1 既有代码）
- `hostlens.tools.schemas.run_inspector.FindingSummary` 改为 `FindingSummary = Finding`（type alias；schema JSON 通过 `model_json_schema()` 由 adapter 在投影时生成，自动跟进新字段）
- **修改 `src/hostlens/tools/default_tools.py` 的 `run_inspector` handler 投影逻辑**（**非零修改**）：当前 handler 用 `evidence={k: str(v) for k, v in finding.evidence.items()}` 把 `Finding.evidence: dict[str, str]` 转字符串字典；BREAKING 后 `Finding.evidence` 是 `list[Evidence]`，dict comprehension 会 raise `AttributeError`。必须改为：`FindingSummary(severity=f.severity, message=f.message, evidence=list(f.evidence), tags=list(f.tags))` 或直接复用 `result.findings`（因为 `FindingSummary = Finding` 即同一类型）；建议直接 `findings=list(result.findings)`，无需逐项重构造
- `hostlens.inspectors.result.InspectorResult.findings: list[Finding]` 类型不变，但 Finding 的 evidence 字段类型升级（dict → list[Evidence]）会传导到 `InspectorResult.findings[].evidence`；archived `inspector-plugin-system` spec 中"Finding 的 evidence 字段是 `dict[str, str]`"相关场景必须更新（见 spec delta）

**修改（archived `inspector-plugin-system` capability spec 的 MODIFIED Requirements）：**

- §需求:Finding 数据模型字段集（M1 与 M2 严格对齐）— Finding 的 SOT 从 `hostlens.inspectors.result.Finding` 切到 `hostlens.reporting.models.Finding`；`evidence` 字段类型从 `dict[str, str]` 改为 `list[Evidence]`；其他场景保持

## 功能 (Capabilities)

### 新增功能

- `report-data-model`: `Severity` / `Evidence` / `Finding` / `Report` Pydantic 模型（SOT 在 `hostlens.reporting.models`）、`Report.from_inspector_results()` 工厂、`render_markdown` / `render_json` 双渲染器、`schema_version` 锁定与扩展契约
- `inspect-cli-command`: `hostlens inspect <target> --inspector <name>` 命令（典型 demo path 入口）+ 4 值退出码语义 + `--output` / `--format` / `--parameters` / `--allow-privileged` / `--timeout` 选项 + 错误降级（target/inspector 未找到 → exit 3；runner 内部失败 → exit 2；critical finding → exit 1）

### 修改功能

- `inspector-plugin-system`: Finding 的 SOT 与字段集——`Finding` 从本地定义改为 re-export `hostlens.reporting.models.Finding`；`evidence` 字段类型从 `dict[str, str]` 改为 `list[Evidence]`；archived spec 中相关场景全部 MODIFIED（其他 manifest / loader / runner / DSL / 注入防御 / parse format / 内置 Inspector 等需求**保持不变**——本提案不动 Inspector loader 与 runner 逻辑）

## 影响

**新增代码**：

- `src/hostlens/reporting/models.py`（新文件；4 个 Pydantic 模型 + 1 个 Literal + `Report.from_inspector_results()` 工厂）
- `src/hostlens/reporting/render_markdown.py`（新文件；单一公开函数 `render(report) -> str`；纯 f-string）
- `src/hostlens/reporting/render_json.py`（新文件；单一公开函数 `render(report) -> str`）
- `src/hostlens/reporting/__init__.py`（新文件；显式 export `Report` / `Finding` / `Severity` / `Evidence` + 两个 `render_*.render` 函数；遵循 `hostlens.inspectors.__init__` 既定的"`__init__` 不做副作用工作"原则）
- `src/hostlens/cli/inspect.py`（新文件；Typer command；包 asyncio.run 调度 InspectorRunner → `Report.from_inspector_results()` → 渲染 → 输出）
- `src/hostlens/cli/__init__.py`：注册 `inspect` 子命令；与现有 `doctor` / `target` / `inspectors` 三组并列

**修改代码**：

- `src/hostlens/inspectors/result.py`：`Finding` 由本地定义改为 `from hostlens.reporting.models import Finding as Finding`（type alias）；保留 `__all__` 中的 `Finding` 名字；InspectorResult 内部使用 path 不变
- `src/hostlens/tools/schemas/run_inspector.py`：`FindingSummary = Finding`（type alias）；保留 `__all__`
- `src/hostlens/tools/default_tools.py`：修改 `_run_inspector_handler` 投影逻辑（≈ 第 148-159 行）——把 `evidence={k: str(v) for k, v in finding.evidence.items()}` dict comprehension 改为 `findings=list(result.findings)` 直接复用（Finding 即 FindingSummary 同类型，BREAKING 后 evidence 是 list[Evidence] 不再是 dict）

**对外契约影响**（**含 1 个 BREAKING**）：

- **BREAKING** `Finding.evidence` 字段类型：`dict[str, str]` → `list[Evidence]`
  - **影响范围**：M1 阶段（当前）仅 archived `add-inspector-plugin-system` spec 的 finding DSL 测试中有真实使用；M2 `run_inspector` ToolSpec handler 的 `FindingSummary` 由于走 type alias 自动跟进；外部用户**尚无**——M1 尚未发版
  - **迁移路径**：spec delta 文档化 + 测试同步更新；现有 `inspectors/builtin/hello/echo.yaml` 与 `inspectors/builtin/system/uptime.yaml` 的 manifest 不变（finding DSL 的 `message` 字段不受影响；`evidence` 当前 hello.echo manifest 未声明，uptime.yaml 也未声明）
  - **降级 fallback**：**不**提供"`evidence` 仍接受 `dict[str, str]` 形式"的隐式 coercion——理由：M1 未发版，本提案是"最后一次自由调整 schema"的窗口；显式 BREAKING + spec delta 比"双 schema 并行"更清晰
- 新增 `report-data-model` capability spec（独立 spec 文件 `openspec/specs/report-data-model/spec.md`）
- 新增 `inspect-cli-command` capability spec（独立 spec 文件 `openspec/specs/inspect-cli-command/spec.md`）
- 修改 `inspector-plugin-system` capability spec（MODIFIED Requirements：Finding SOT 与 evidence 字段类型；其他章节不动）
- CLI 新增 1 个命令：`hostlens inspect`；无现有命令改名 / 删除

**依赖影响**：

- 不引入新的 **runtime** Python 依赖（**不**用 Jinja2；模板系统留给 M5 Notifier）；Pydantic v2 / Typer / Rich 已在 pyproject 中；测试可新增 `[dev]` 依赖 `syrupy`（snapshot test 用，下一条说明）
- 不修改 `Settings` schema；CLI 选项已覆盖所有运行时参数
- 测试新增 `syrupy` 依赖到 `[dev]` 分组（snapshot test 用；markdown 渲染做 golden 文件对比；优先复用项目已有的快照工具，若已存在则零新增）

**Demo Path 影响**：archived `add-inspector-plugin-system` proposal 的 Demo Path 第 6 步"通过 ToolRegistry dispatch"将被本提案的 Demo Path 第 6 步"`hostlens inspect local-host --inspector hello.echo`"上位替代（更面向人类的入口）；ToolRegistry dispatch 路径仍存在但不再是 demo 主路径，移到 M2 Agent loop demo path 演示

## Non-Goals（非目标）

- ❌ **Finding identity 字段（`id` / `inspector_run_id` / `seen_at` / `fingerprint`）**：留给 M3 `add-diagnostician-and-report-extensions`（按 TODO.md M3 范围）；本提案的 `Finding` 字段集**严格四字段**（severity / message / evidence / tags），M3 走 add-only 扩展
- ❌ **Multi-inspector 并行调度**：M1 `hostlens inspect` 一次只跑**一个** `--inspector`；M2 Planner Agent 才并行；本提案的 `Report.inspector_results` 字段为 `list[InspectorResult]` 是为 M2 预留，M1 阶段该列表始终 len==1
- ❌ **Regression diff（两份 Report 对比）**：M3 范围
- ❌ **HTML / PDF 渲染**：M3+ 按需；本提案仅 md / json
- ❌ **Jinja2 模板系统**：留给 M5 Notifier（飞书卡片 / TG MarkdownV2 需要 per-channel 模板，那时引入 Jinja2 + `notifiers/templates/` 目录）
- ❌ **Notifier 适配器与发送**：M5 范围；本提案只锁定 `Report` 作为 Notifier 上游契约，不写任何 send 代码
- ❌ **Schedule 触发的报告持久化与历史 run 索引**：M4 范围；本提案的 `Report` 是内存对象，CLI 写文件只是"渲染到 stdout / 一次性文件"，无持久层
- ❌ **Report 序列化的反向加载（`Report.from_json(...)`）**：M3 regression diff 才需要；本提案只单向 Python → md/json
- ❌ **Evidence 自动从 InspectorResult 推断**：本提案 `Finding.evidence` 由 manifest finding DSL 显式声明或 inspector hook.py 显式构造（M6+ 才有 hook.py）；M1 阶段 finding DSL 未启用 evidence 构造语法，所有 finding 的 evidence 列表为空——本提案完成后 M6 / M3 提案再扩展 finding DSL 的 evidence 构造能力
- ❌ **`hostlens inspect` 的 `--intent` 自然语言意图入口**：M2 Agent loop 提案才接入；本提案 CLI 只暴露 `--inspector <name>` 显式调度

## Failure Modes

| 故障 | 行为 | 用户可见状态 |
|---|---|---|
| `--target` 未找到（TargetRegistry 查不到） | CLI 输出 `target not found: <name>; run 'hostlens target list' to see registered targets` 到 stderr；exit 3 | exit 3；stdout 无任何 partial 渲染 |
| `--inspector` 未找到 | CLI 输出 `inspector not found: <name>; run 'hostlens inspectors list' to see available inspectors` 到 stderr；exit 3 | exit 3；stdout 无 partial 渲染 |
| `--parameters` JSON 解析失败 | CLI 输出 `invalid --parameters: <error>` 到 stderr；exit 3 | exit 3 |
| `--parameters` 不符合 inspector manifest `parameters` JSON Schema | runner preflight 阶段返回 `InspectorResult(status="exception", error="parameter_validation_failed: ...")`；CLI 渲染 Report 后 exit 2 | exit 2；Report 仍渲染（含 inspector_result.error） |
| `--format` 不在 `{md, json}` | Typer 在参数解析阶段 raise；exit 3（Typer 默认行为） | exit 3 |
| `--output` 文件写入失败（权限不足 / 目录不存在） | CLI 输出 `failed to write output: <reason>` 到 stderr；exit 3 | exit 3；stdout 已被 buffer 但**不**回退到 stdout（避免双写）|
| `Report.from_inspector_results` 收到空 `inspector_results` 列表 | raise `ValueError("from_inspector_results requires at least one InspectorResult")`；CLI 捕获 → exit 3 | exit 3；理论上 CLI 路径不会触发（CLI 始终传 1 个 InspectorResult），是工厂方法的 invariant 防御 |
| `inspector_result.status == "timeout"` | Report 仍渲染（含 inspector_result 的 status / duration / error="timeout: collect.command exceeded N seconds"）；CLI exit 2 | exit 2；md/json 均输出完整 Report |
| `inspector_result.status == "target_unreachable"` | 同上；error 含连接失败原因（脱敏，不含 secret）；exit 2 | exit 2 |
| `inspector_result.status == "requires_unmet"` | 同上；inspector_result.missing 字段非空；exit 2 | exit 2 |
| `inspector_result.status == "exception"`（parse 失败 / output_schema mismatch） | 同上；error 含失败原因；exit 2 | exit 2 |
| `inspector_result.status == "ok"` 且 findings 含 ≥1 critical | exit 1；Report 正常渲染 | exit 1 |
| `inspector_result.status == "ok"` 且 findings 全 ≤ warning（含全空） | exit 0；Report 正常渲染 | exit 0 |
| markdown 渲染时 `evidence` 含恶意控制字符（如 ANSI escape） | 渲染器对 evidence 的 stdout/stderr/excerpt 字段做 control char escape（保留 `\n` / `\t`，其他 `\x00-\x1f` 与 `\x7f` 转为 `\xXX` 字面量）；**不**渲染原始字节 | md 输出可读且不被 terminal escape 序列污染 |
| json 渲染 `evidence` 含大 stdout（如 5MB 日志） | `model_dump_json` 直接序列化，**不**截断（与 InspectorResult 的 stdout 1MB 上限呼应——上游 runner 已限制）；若 CLI 检测到 `Report.total_evidence_bytes() > 8MB` 在 stderr 输出 warning 但**不**失败 | md/json 输出完整；stderr warning 提示文件可能过大 |
| 系统时间倒流导致 `finished_at < started_at` | `Report` 模型级 `model_validator` raise `ValueError("finished_at must be ≥ started_at")`；CLI 捕获 → exit 2（标记为 runner 内部失败而非用户参数错） | exit 2 |
| 写入 `--output` 目标文件已存在 | 直接覆盖（**不**做 `--force` opt-in 提示——CLI 简洁优先，复用 `>` shell 重定向语义）；**已知接受风险**记入 docs/operations/inspect.md | 静默覆盖；与 `tar -f` / `cp` 等 POSIX 工具一致 |

## Operational Limits

参考 docs/OPERABILITY.md §1：

- **单次 `hostlens inspect` 总时延上限**：`<=（manifest collect.timeout_seconds 或 --timeout 覆盖值，CLI 校验 ≤ 300s 与 archived `inspector-plugin-system` spec 中 CollectSpec.timeout_seconds = Field(ge=1, le=300) 字段约束严格一致）+ 5s CLI overhead`；超出由上游 runner 触发 timeout 路径返回 `status="timeout"`，CLI 不另设硬上限；**不引入** 新 `Settings.concurrency.*` 字段
- **已知 archived 不一致**：OPERABILITY.md §1 表中 "单个 Inspector 的最长 wall-clock = 60s"，而 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 上限 300。本提案选择对齐 manifest 上限 300（M1 已落地约束），**不**对齐 OPERABILITY 60s——理由：(a) manifest 上限是真实的 Pydantic 字段约束，OPERABILITY 是建议性预算；(b) 修 OPERABILITY 或 manifest 不在本提案范围内（属于 archived 提案的修订）；本不一致由后续 OpenSpec 提案（如 M3 / M5）统一调整 —— 本提案文档化但不修
- **`Report` 内存占用上限**：M1 单 Inspector 单 InspectorResult；stdout / stderr 各 1MB（继承 ExecutionTarget exec 层）；evidence 列表 M1 阶段始终为空（finding DSL 不构造 evidence）；预计单 Report 内存占用 < 5MB；M2 多 Inspector 并行后预算见 M2 提案
- **markdown 渲染单次延迟**：< 50ms（M1 单 Inspector + 空 evidence；纯字符串拼接）；M3 多 Inspector + 富 evidence 后预算见 M3 提案
- **`--output` 文件大小预算**：md 通常 < 100KB（M1 范围）；json 与 InspectorResult.output 大小同量级，预计 < 2MB
- **CLI 退出超时**：无；CLI 等 runner 完成或 runner 自身 timeout 触发，**不**叠加 CLI 层超时（避免 partial state）
- **报告渲染并发度**：M1 阶段 CLI 单进程单 Report；无需并发预算

## Security & Secrets

参考 docs/OPERABILITY.md §7：

- **新增密钥来源**：无；本提案不读 `os.environ` 中的 secrets（密钥仍由上游 ExecutionTarget / Inspector runner 处理）
- **Report / Evidence 内容级脱敏（硬约束，对齐 OPERABILITY.md §7.2）**：
  - **必须**：任何写入 `--output` 文件、或通过 `render_json` / `render_markdown` 输出的字符串字段（含 `Evidence.stdout` / `Evidence.stderr` / `Evidence.excerpt` / `Evidence.command` / `InspectorResult.error`），**必须**先过 `hostlens.core.redact.redact_text(s: str) -> str`（M0 已落地或本提案补建）；脱敏规则继承 OPERABILITY.md §7.2 默认正则（password/secret/token/api-key/bearer / JWT eyJ... / sk-xxxx Anthropic-OpenAI key 形式）+ 保留前 4 后 4 字符
  - **`Report` 内存对象本身**：runner / Agent loop 持有的 `InspectorResult` / `Report` 内存表示**不**强制脱敏（保持 raw 内容用于内部 reasoning）；脱敏**仅在渲染边界**（`render_markdown.render()` / `render_json.render()` / `--output` 写文件路径）应用——避免脱敏字符串被 Agent loop 误判为真实数据
  - **stdout 直出**：CLI 默认 stdout 输出**同样**经过脱敏（与 OPERABILITY §7.2 "任何写入 Notifier payload / 日志" 等价对齐；CLI stdout 本质是用户可见的 sink）；用户若需 raw 内容必须显式 opt-in（M3+ 提案再设计 `--no-redact` 选项，本提案 M1 范围**不**暴露）
  - `Evidence.command` 字段：渲染器**禁止**把 `command` 中的 env var 值替换为实际值——渲染器只输出 manifest 原始 `command` 模板字符串（如 `psql -h $PGHOST -U $PGUSER` 而非展开后的 `psql -h db.prod -U admin`），保证 env var 值不进 Report；该字符串**仍**过 redact_text（双重保险）
  - `target_name` / `inspector_name` 是 manifest / target.yaml 中已知 ASCII 字符串，**仍**过 redact_text（默认规则对纯名字无影响，零成本一致性）
- **CLI 输出脱敏**：`hostlens inspect` 在 stderr 输出错误信息时**禁止**echo `--parameters` 的 JSON 内容（可能含敏感参数如 db host:port）；只输出 error kind + 字段名 + parameter 名
- **渲染时控制字符防御**：markdown 渲染器对 evidence 文本字段做 control char escape（详见 Failure Modes 行 13）；防止 stdout/stderr 中的 ANSI escape 把 terminal 状态污染或 markdown 渲染器误解析
- **`--output` 文件权限**：CLI 写文件用 Python 默认权限（受 umask 控制），**不**设 `0o600` —— 用户可通过 `umask 077 && hostlens inspect ...` 自控；与 archived `add-execution-target-abstraction` 的 `targets.yaml` 权限自管理一致
- **攻击面**：本提案不引入新的网络 IO / 文件读取入口（仅 `--parameters @<path>` 文件读取，路径由用户提供，无符号链接 / `..` 校验需求——这是 CLI 输入语义，攻击者已经拥有 shell）；不引入新的反序列化攻击面（仅 Pydantic 强类型构造，无 `pickle` / `yaml.load`）

## Cost / Quota Impact

参考 docs/OPERABILITY.md §3：

- **LLM token 消耗**：**零**；本提案纯本地数据模型 + 渲染，不调 LLM
- **Anthropic API 调用频次**：**零**
- **下游 LLM 影响（M2 提案集成时）**：M2 `run_inspector` ToolSpec handler 投影 `InspectorResult` → `RunInspectorOutput` 时，`FindingSummary` 字段集随本提案变更（evidence: list[Evidence]）——M2 Agent loop 看到的 tool 输出 token 体量预计**增加 < 10%**（M1 阶段 evidence 列表始终为空；M3 finding DSL 扩展后才显现）；本提案确保 `Evidence` 的 `model_dump_json` 输出稳定可被 prompt cache prefix 复用（字段按 alphabetical 序，Pydantic v2 默认行为）
- **存储成本**：CLI `--output` 写本地文件，无云存储 / 远端推送（M5 Notifier 才有上传链路）

## Demo Path

> 目标：交付后任何人在干净 macOS / Linux 上 ≤5 分钟跑通"加载 inspector → 跑一次 inspect → 看 md 报告 → 看 json 报告 → 失败路径验证"。**无 SSH、无付费 API、无远端访问。**

1. **环境准备**（30s）：`git clone` → `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
2. **加载验证**（10s）：`hostlens doctor --json | jq '.inspectors'` 期望 `{"status": "ok", "loaded": 2, "errors": []}`
3. **配置 local target**（10s；复用 archived `add-execution-target-abstraction`）：`hostlens target add local-host --type local`
4. **跑一次 inspect（markdown 默认输出到 stdout）**（10s）：`hostlens inspect local-host --inspector hello.echo` 期望：
   - stdout：md 格式 Report；含 `# Hostlens Inspection Report` 标题 + meta 表 + `## Summary` 显示 `info: 1` + `## Findings` 列 hello.echo 的 info finding（message = `hello received: hello`）+ `## Inspector Results` 附录 InspectorResult（status=ok）
   - exit code: 0
5. **跑一次 inspect（json 输出到文件）**（10s）：`hostlens inspect local-host --inspector hello.echo --format json --output /tmp/hostlens-demo.json`
   - 文件存在 + `jq '.schema_version, .findings[0].severity' /tmp/hostlens-demo.json` 输出 `"1.0"\n"info"`
   - exit code: 0
6. **失败路径：inspector 不存在**（10s）：`hostlens inspect local-host --inspector nonexistent.foo; echo "exit=$?"`
   - stderr：`inspector not found: nonexistent.foo; run 'hostlens inspectors list' to see available inspectors`
   - exit code: 3
7. **失败路径：target 不存在**（10s）：`hostlens inspect ghost-host --inspector hello.echo; echo "exit=$?"`
   - stderr：`target not found: ghost-host; run 'hostlens target list' to see registered targets`
   - exit code: 3
8. **失败路径：runner 内部失败（exit 2）**（60s）：通过 `examples/m1-report/inspectors/sleep_timeout.yaml` 提供一个 manifest，关键字段集 = `name: demo.sleep_timeout` / `version: 1.0.0` / `tags: [demo, timeout-test]` / `targets: [local]` / `requires_binaries: [sleep]` / `parameters: {type: object, properties: {sleep_seconds: {type: integer, minimum: 1, maximum: 300}}, required: [sleep_seconds]}`（**integer 类型不带 pattern**，与 archived `inspector-plugin-system` spec §需求:Manifest loader 必须 reject string parameter 缺少 `pattern` 或 `enum` 约束 中"pattern 是 string 字段约束"对齐）/ `collect.command: "sleep {{ sleep_seconds }}"`（**integer parameter 直接 Jinja 插值，不走 `| sh` filter**——archived spec sh filter 仅对 string 类型 parameter 强制）/ `collect.timeout_seconds: 60` / `parse.format: raw` / `output_schema: {type: object}` / `findings: []`；放在用户 inspector 搜索路径下后跑 `HOSTLENS_INSPECTORS_SEARCH_PATHS=./examples/m1-report/inspectors hostlens inspect local-host --inspector demo.sleep_timeout --parameters '{"sleep_seconds": 30}' --timeout 1; echo "exit=$?"` 期望 exit 2 + stdout Report 含 `status: timeout`；通过 `--timeout 1` 让 CollectSpec 重构后注入 1s 上限（远小于 sleep 30s 实际时间）触发 runner 内部 timeout 路径
9. **允许 root 验证**（10s）：`sudo hostlens inspect local-host --inspector hello.echo` 必须正常返回 exit 0（只读命令，与 `inspectors list/show` 一致）
10. **CI replay 验证**（60s）：`pytest tests/reporting/ tests/cli/test_inspect.py -v` 全绿；其中至少 1 个 syrupy snapshot 测试对 `render_markdown.render(report)` 的 golden 文件做字节级对比

完成所有步骤后 `examples/m1-report/README.md` 把以上 10 步固化为可复制粘贴的命令；与 proposal Demo Path 严格一致。
