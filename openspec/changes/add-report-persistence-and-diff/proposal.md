## 为什么

M2 已交付「自然语言意图 → Planner Agent 自选 Inspector → 出 Report」的最小闭环，但 Report **跑完即弃**：没有落盘、无法回看历史、更无法回答运维最关心的问题——「**这次相对上次，多了什么、好了什么、谁变严重了**」。

M3「Diagnostician + 报告体系」要让报告**带根因假设**并支持**回归对比**。这两件事都依赖同一块地基：**报告 schema 的字段扩展**——

- Diagnostician（后续提案 `add-diagnostician-agent`）要往 Report 写 `hypotheses`（根因假设）与运行状态 `ReportStatus`；
- Regression diff 要靠 `Finding` 的稳定身份（`id` / `inspector_version` 指纹）做跨 run 匹配，靠 `ReportStatus == ok` 选基线，靠 `BaselineRef` 记录基线来源。

当前 `report-data-model` spec 把「`Report` 字段集严格」「`Finding` 严格四字段」锁死了（虽已预留 add-only 扩展位）。按 CLAUDE.md §5「改动已有契约必须先过 spec，不允许跳过 spec 直接写代码」，**本提案是 M3 整条链的前置**：先把 schema 扩展 + 持久化 + diff 这块地基立稳，Diagnostician 才有地方写假设、diff 才有历史可比。

## 变更内容

1. **扩展报告 schema（add-only，不破坏 M1/M2 契约）**——修改 `report-data-model`：
   - `Finding` 增 `id` / `inspector_name` / `inspector_version`（均带默认值 None，旧构造方零改动）；
   - `Report` 增 `meta: ReportMeta` 与 `hypotheses: list[RootCauseHypothesis] = []`，**保留**全部 M1 扁平字段（路线 A，见 design）；
   - `schema_version` Literal 由 `"1.0"` 扩为 `"1.0" | "1.1"`，新增字段时写 `"1.1"`；
   - 新增子模型：`ReportMeta` / `RootCauseHypothesis` / `ReportStatus`(enum) / `InspectorRun` / `BaselineRef` / `TokenUsage`。
   - 本提案**只定义 `hypotheses` 字段与 `RootCauseHypothesis` 形状，不产生假设内容**（内容由 Diagnostician 提案填充；M3 本提案下 `hypotheses` 恒为 `[]`）。

2. **报告持久化**（新能力 `report-persistence`）：
   - `reporting/store.py`——SQLite，按 run 存**脱敏后**报告 JSON（blob）+ 索引列（从内存 `report` 投影：target_id/target_name/status/timestamp/schema_version 取自 `meta`，finding_count = `len(report.findings)`）；`save() -> SaveResult`（含 `stored_as_orphan` / `orphan_path`，区分正常入库与降级，**不**用裸 `str` 返回）；
   - `stored_as_orphan` 降级路径：主库写失败时 fallback 落地到 `~/.local/share/hostlens/orphan_reports/`，不丢报告；
   - 暴露基线查询 API（取某 target 最近一次 `status == ok` 且**早于 current** 的 run），供 diff 消费；
   - 持久化入口接**产出 `Report` 的机械路径** `hostlens inspect <target> --inspector <name> --persist`（Agent/PlannerResult 路径不接，见非目标 8）；
   - CLI：`hostlens reports list <target>` / `hostlens reports show <run_id>`。

3. **回归对比引擎**（新能力 `report-regression-diff`）：
   - `reporting/diff.py`——`RegressionDiff` 模型 + 基线选取规则 + 对比算法，输出 `added` / `resolved` / `changed_severity` 三类；
   - finding 跨 run 匹配指纹 = `(inspector_name, inspector_version, severity-agnostic message identity)`，由 `Finding.id` 锚定；
   - CLI：`hostlens reports diff <run_id_a> <run_id_b>`（也供 M5 定时巡检复用，但**本提案不接 Notifier**）。

4. **渲染扩展（最小）**：`render_markdown` 增 `## 根因假设` 章节占位（M3 本提案下假设为空时显示 `_暂无根因假设_`；以 **MODIFY 既有 GFM 结构需求**把 section 并入，不并列新需求）；`ReportMeta.inspectors_used` 从现有 `InspectorResult` 字段机械投影，不新增采集逻辑。

5. **fixture / snapshot 迁移（精确范围）**：只有**经渲染器的 sink** 受影响——`inspect` 路径的 `.ambr` 与直接断言 `Report`/`Finding`/`schema_version` 的 reporting/cli 单测，因 `from_inspector_results` 现写 `schema_version="1.1"` + meta、`render_markdown` 加根因章节、`render_json`（`exclude_none=False`）多出新字段而变。**incidents/demo snapshot 不变**——它们由 `tests/incidents/_harness.py:project_planner_result` 投影（只取 severity+message+tags，**不**走 render），新字段与根因章节不波及（diff 验收是**新增**测试，非改这些 snapshot）。

## 功能 (Capabilities)

### 新增功能
- `report-persistence`: 报告 SQLite 持久化、run 索引、`stored_as_orphan` 降级、基线查询 API，以及 `hostlens reports list/show` CLI。
- `report-regression-diff`: 两份报告的回归对比（`added`/`resolved`/`changed_severity`）、基线选取规则、`RegressionDiff` 模型与 `hostlens reports diff` CLI。

### 修改功能
- `report-data-model`: 主要 add-only 扩展——`Finding` 增 `id`/`inspector_name`/`inspector_version`（可选默认 None，旧 JSON 仍可加载）；`Report` 增 `meta`/`hypotheses` 并保留扁平字段；`schema_version` 扩为 `"1.0"|"1.1"`；新增 `ReportMeta`/`RootCauseHypothesis`/`ReportStatus`/`InspectorRun`/`BaselineRef`/`TokenUsage` 模型。**MODIFIED 四条需求**（`Finding` / `Report` / `from_inspector_results` 工厂 / `render_markdown` GFM 结构——后者把根因章节并入既有结构需求，避免与已锁渲染需求并列）。其中**绝大多数已锁场景 verbatim 保留**；**少数描述真实行为变更的场景按变更显式修订**（这正是 MODIFY 的本意，非「丢场景」）：(a) `from_inspector_results` flatten 现返回带身份字段的 `model_copy` **副本**而非原 finding 对象——旧场景 `report.findings == [f1,f2,f3]`（裸对象相等）不再成立、改为断言顺序/数量/内容保持 + 身份字段填充（既有断此相等的测试需迁移，见 §影响 / tasks 7.2）；(b) `from_inspector_results` 锁定 `schema_version` 由 `"1.0"` 改 `"1.1"`。**新增字段本身** add-only（旧 JSON 兼容），但工厂**返回副本**与 **schema 1.1** 是有意的行为修订。

## 对外契约影响

| 契约面 | 影响 | 兼容性 |
|---|---|---|
| `Report` / `Finding` Pydantic schema | 新增字段（add-only，全部带默认值） | M1/M2 构造方 **零改动**；旧 JSON 经 `model_validate` 仍可加载（缺字段走默认） |
| `render_json` 输出 | 因 `exclude_none=False`，新增字段一律出现在 JSON | **经 render 的 sink（inspect `.ambr` + 相关单测）需重录**；incidents/demo snapshot 不走 render，不变 |
| `render_markdown` 输出 | 新增 `## 根因假设` 章节（MODIFY 既有 GFM 结构需求） | inspect snapshot 重录 |
| `inspect --inspector` 输出 | `from_inspector_results` 现写 `schema_version="1.1"` + meta | M1 inspect 既有 json/snapshot 断言（锁 `"1.0"`）随之更新 |
| CLI 命令面 | **新增** `hostlens reports` 子命令组（`list`/`show`/`diff`）+ `hostlens inspect --persist` flag | 纯新增，不改既有命令语义 |
| Agent tool schema / MCP / Notifier / Schedule manifest | **不涉及** | — |

> `run_inspector` tool 的 `FindingSummary = Finding` type alias 会自动获得新字段（add-only），但 Agent tool 投影 schema 仅取必要字段，不扩大 Agent 可见面。

## 非目标 (Non-Goals)

1. **不实现 Diagnostician Agent** 与 `correlate_findings` / `request_more_inspection` 工具（属 `add-diagnostician-agent`）。本提案只**定义** `hypotheses` 字段与 `RootCauseHypothesis` 形状，**不产生**假设内容（M3 本提案下恒为 `[]`）。
2. **不实现 `RunStatus` 与 Scheduler 层 `Run` 记录**（属 M4 `add-scheduler`）。本提案只实现 **Report 层** `ReportStatus`；二者是独立 enum（ARCHITECTURE §7 已明确边界）。
3. **不实现 HTML 渲染** `render_html.py`（M3.4 单列提案）。本提案只给 markdown 加根因章节占位。
4. **不实现 extended-thinking / `ThinkingBlock`**（属 `support-extended-thinking`）。
5. **不把 diff 接入定时巡检推送 section**（属 M5 Notifier）。本提案只产出 `RegressionDiff` 模型 + `reports diff` CLI。
6. **不做 `--no-redact` opt-in**（沿用 M1 渲染边界强制脱敏；该选项留待后续提案）。
7. **不做报告 retention / 自动清理策略**（落盘只增不删；retention 留 M4+ 运维提案）。
8. **不持久化 Agent/PlannerResult 路径**（`hostlens inspect --intent` / `hostlens demo run`）。这两条路径产物是 `PlannerResult`（无 `inspector_results`，无法 `from_inspector_results`），装配成 `Report` 需要 fabrication——属 `add-diagnostician-agent` 范围。本提案 `--persist` 仅作用于产出 `Report` 的 `--inspector` 机械路径。

## Failure Modes

| # | 故障场景 | 降级行为 |
|---|---|---|
| 1 | SQLite 主库不可写（磁盘满 / 权限 / 锁竞争） | 报告落 `~/.local/share/hostlens/orphan_reports/<run_id>.json`，`ReportStatus` 标 `stored_as_orphan`，CLI 退出码非 0 但**报告不丢**；stderr 单行错误，无 traceback |
| 2 | `reports diff` 找不到合格基线（首次 run / 历史全部非 `ok` / 自动模式排除 current 后无更早 ok） | 不报错；CLI 直接输出「无可比基线」文本（**不**构造 `RegressionDiff`，故 `baseline_unavailable` 不入 `diff_skipped_reason`）；退出码 0 |
| 3 | 两 run 的 `inspector_version` 不一致（Inspector 升级） | 该 inspector 的 finding **不参与** added/resolved 匹配，归入 `inspector_upgraded` 提示而非误报「全 resolved + 全 added」 |
| 4 | 读取旧 `schema_version="1.0"` JSON（无 meta / finding 无 id；仅 orphan / 外部导入可能出现，本 store 写入行必带 meta） | `model_validate` 用默认值补齐（`meta=None`、finding `id=None`）不崩；此类报告**不能做可靠 diff**——`compute_diff` 见 `id is None` 即 `diff_skipped_reason="missing_finding_ids"` 跳过，不误判 |
| 5 | `reports show <run_id>` run 不存在 | stderr 单行 `run not found: <run_id>` + 指引 `reports list`，退出码 3，无 traceback |
| 6 | target 改名（M3 `target_id == target_name`，非稳定 ID） | per-target 隔离按 name 走：改名后新报告与旧报告 target_id 不同 → diff 视作新 target、首次无基线（基线重置）。**已知限制**：稳定 `target_id` 留 M4（design Open Question 1）；不误判、只基线重置 |

## Operational Limits

- **存储**：单报告落盘前过 `render_json` 脱敏；CLI 沿用 8 MiB「large report」阈值（超阈值仅警告不阻断）。SQLite 单库（`~/.local/share/hostlens/reports.db`），WAL 模式，无并发写 daemon（M3 仍是单进程 CLI；多进程并发留 M4 scheduler 评估）。
- **内存**：diff 一次只加载 2 份报告 JSON 到内存比对，不全量扫库；`reports list` 走索引表分页（默认 limit 20）。
- **超时**：SQLite 操作走 `asyncio.to_thread`（同步驱动），单次操作软超时 5s。

## Security & Secrets

- **不引入新密钥**。SQLite 是本地文件，无网络。
- **脱敏边界（精确化 OPERABILITY §7.2）**：`report_json` blob 是**已脱敏**的 `render_json` 输出——**自由文本/内容字段全过 `core/redact`**（finding message / evidence / intent / hypotheses 等）。索引列（`target_id`/`target_name`/`status`/`timestamp`/`finding_count`/`report_schema_version`）是**结构化标识符与元数据、非自由文本**，从内存 `meta` **原值**投影（保证查询 / diff 的 target_id 匹配可靠，不被脱敏改写）。故 OPERABILITY §7.2「写 SQLite 的字符串都过 redact」在本提案精确化为：**自由文本内容（blob）强制脱敏；结构化标识符索引列存原值**——target 名是标识符、约定不含 secret（见 report-persistence「已知限制（target 命名）」）。
- **攻击面**：新增本地文件读写，无新增网络监听 / 无新增反序列化外部输入（只 `model_validate` 自己写的 JSON）。`run_id` 作文件名前必须校验为合法 UUID，防路径穿越。

## Cost / Quota Impact

- **零 LLM 调用、零 token、零 Anthropic 配额影响**。本提案是纯数据层（schema + SQLite + diff 算法），**不触发任何 Agent 行为**，故无 prompt caching / token 预算考量（Agent 行为与 token 影响留给消费 `hypotheses` 的 `add-diagnostician-agent`）。

## Demo Path

5 分钟内、无 SSH / 无付费 API / 无 Agent 即可 reproduce 回归对比（走产出 `Report` 的机械 `inspect --inspector` 路径）：

```bash
pip install -e ".[dev]"
hostlens target add local-host --type local
# 同一确定性 inspector 跑两次，落盘两个 run（机械路径，无 LLM）
hostlens inspect local-host --inspector hello.echo --persist     # run A → reports.db
hostlens inspect local-host --inspector hello.echo --persist     # run B → reports.db
hostlens reports list local-host                                  # 看到 A、B 两条 run
hostlens reports diff <run_a> <run_b>                             # 确定性输出 → added/resolved 均空，「无回归」
```

> 更丰富的 added/changed_severity 由集成测试 `tests/incidents/test_diff_replay.py` 离线确定性验证：用 incident inspector 经 `ReplayTarget` 机械 runner 产出 `InspectorResult`，经 `from_inspector_results` 组装两份 `Report`（一份无 critical、一份含 `memory_oom` critical），断言 `compute_diff(...).added` 含该 critical——全程不调 Agent / 不触 API。`--persist` 仅作用于 `--inspector` 机械路径（Agent 路径见非目标 8）。

## 影响

- **代码**：新增 `reporting/store.py` / `reporting/diff.py` / `cli/reports.py`；扩展 `reporting/models.py`（+6 模型；Finding +3 字段、Report +2 字段）/ `reporting/render_markdown.py`（根因章节）/ `reporting/_redact.py`（透传新字段 + 脱敏 meta/hypotheses）/ `cli/__init__.py`（注册 reports 子命令）/ `cli/inspect.py`（`--persist` 接线，仅 `--inspector` 路径）。
- **依赖**：**无新增**（SQLite 走标准库 `sqlite3`；Pydantic / Typer 已在）。
- **测试**：新增 store / diff / reports CLI / diff-replay 测试；更新经 render 的 sink——`inspect` 路径 `.ambr` + 直接断言 `Report`/`Finding`/`schema_version`(1.0→1.1) 的 reporting/cli 单测。**incidents/demo snapshot 不变**（不走 render）。
- **spec**：MODIFY `report-data-model`；ADD `report-persistence` + `report-regression-diff`。
- **文档**：`docs/` 报告体系章节补 store/diff 说明（archive 时同步）。
