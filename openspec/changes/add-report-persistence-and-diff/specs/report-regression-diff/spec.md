## 新增需求

### 需求:`RegressionDiff` 模型必须建模 added/resolved/changed_severity 与版本对齐信息

`hostlens.reporting.diff.RegressionDiff` 必须是 Pydantic v2 模型（`extra="forbid"`），含字段（对齐 §10 Diff 输出结构，字段名与 §10 一致）：

- `baseline_meta: BaselineRef | None`（实际用作基线的 run 的 `BaselineRef`，从 `baseline.meta` 投影。**当且仅当 `baseline.meta is not None` 时非 None**（**与 `current.meta` 是否为 None 无关**——含所有跳过子情形：`baseline_not_ok`/`schema_changed`/finding-id 缺失/`current.meta is None`，只要 `baseline.meta` 在就投影）；**仅 `baseline.meta is None` 时为 `None`**（无 baseline meta 可投影）。**沿用 §10 的 `baseline_meta` 名**，不叫 `baseline_ref`——避免与 `ReportMeta.baseline_ref`（报告自记的基线引用）撞名）
- `added: list[FindingFingerprint]`（current 有、baseline 无）
- `resolved: list[FindingFingerprint]`（baseline 有、current 无）
- `changed_severity: list[SeverityChange]`（两边同 finding id 但 severity 变化）
- `inspector_upgraded: list[str] = []`（baseline 与 current 间 version 不同的 inspector name，信息项；**全提案统一用此名**，不用 `changed_inspector_version`）
- `dst_boundary_crossed: bool = False`（M3 恒 False，DST 窗口对齐占位，属 M4）
- `diff_skipped_reason: Literal["baseline_not_ok", "schema_changed", "missing_finding_ids"] | None = None`（闭集，防 CLI 渲染漂移；**不含** `baseline_unavailable`——「无基线」由 CLI 直接输出文本、不构造 `RegressionDiff`，见 `hostlens reports diff` 需求）

其中 `FindingFingerprint` 至少含 `{id: str, inspector_name: str | None, severity: Severity, message: str}`；`SeverityChange` 至少含 `{id: str, from_severity: Severity, to_severity: Severity, message: str}`。

#### 场景:RegressionDiff 拒绝未声明字段

- **当** 试图构造 `RegressionDiff(..., not_a_field="x")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:diff_skipped_reason 是闭集

- **当** 试图构造 `RegressionDiff(..., diff_skipped_reason="whatever")`
- **那么** 必须 raise `pydantic.ValidationError`（仅接受三个 Literal 值或 None）

### 需求:`compute_diff` 必须以 severity-agnostic 指纹做集合差且防基线污染

`hostlens.reporting.diff.compute_diff(baseline: Report, current: Report, *, force: bool = False) -> RegressionDiff` 必须按以下规则（对齐 §10 基线选取）：

0. **meta 完整性前置**：若 `baseline.meta is None` 或 `current.meta is None`（legacy 1.0 / orphan / 外部导入报告）→ 返回 `diff_skipped_reason="missing_finding_ids"`、空 added/resolved/changed_severity。**禁止解引用为 None 的那一侧 meta**（否则 None deref 爆 `AttributeError`）；但 `baseline_meta` 仍按字段规则处理——`baseline.meta is not None` 时**照常**从 `baseline.meta` 投影 `BaselineRef`（即便 `current.meta is None`），`baseline.meta is None` 时为 None。这类报告 finding 也必 `id=None`，归同一跳过类别。
1. **per-target 隔离**：`baseline.meta.target_id != current.meta.target_id` → raise `ValueError`（**禁止**跨 target diff）。
2. **finding 身份完整性**：若 baseline 或 current 含 `Finding.id is None` 的 finding（legacy schema 1.0 报告 / 直接构造未走工厂）→ 返回 `diff_skipped_reason="missing_finding_ids"`、空 diff（**禁止**用 None id 做集合差，否则碰撞/类型不符）。
3. **基线状态门槛**：`baseline.meta.status != "ok"` 且非 `force` → 返回 `diff_skipped_reason="baseline_not_ok"`，`added/resolved/changed_severity` 均空（**不**抛错）。
4. **schema 对齐**：`baseline.meta.report_schema_version != current.meta.report_schema_version` → 返回 `diff_skipped_reason="schema_changed"`，空 diff。
5. **inspector 版本对齐**：对每个 inspector，若 baseline 与 current 的 `version` 不同 → 该 inspector 的全部 finding **排除**出 added/resolved/changed_severity，inspector name 计入 `inspector_upgraded`。
6. **指纹集合差**（仅对版本对齐、且 id 非 None 的 inspector finding）：以 `Finding.id`（severity-agnostic 指纹）建两集合——`added` = 仅 current；`resolved` = 仅 baseline；`changed_severity` = 两边同 `id` 但 severity 不同。
7. `baseline_meta` 填基线的 `BaselineRef`（**当且仅当 `baseline.meta is not None` 时非 None**，见 RegressionDiff 字段说明；其 `inspector_versions` 由 `baseline.meta.inspectors_used` 投影 `name->version`，与 `latest_ok_baseline` 同源）；`dst_boundary_crossed` 恒 False（M3）。

#### 场景:同一报告 diff 自身无回归

- **当** `compute_diff(report, report)`（baseline=current，status=ok，findings 全有 id）
- **那么** `added == []` 且 `resolved == []` 且 `changed_severity == []`

#### 场景:current 新增 finding 进 added

- **当** baseline 含 finding 集 `{A}`，current 含 `{A, B}`（同 inspector 同 version，均有 id）
- **那么** `added` 必须含 B 的指纹，`resolved == []`

#### 场景:baseline 有而 current 无的 finding 进 resolved

- **当** baseline 含 `{A, B}`，current 含 `{A}`
- **那么** `resolved` 必须含 B，`added == []`

#### 场景:severity 变化进 changed_severity 而非 added+resolved

- **当** 同一 finding（同 id）baseline severity=`warning`、current severity=`critical`
- **那么** `changed_severity` 必须含该 id 的 `{from_severity:"warning", to_severity:"critical"}`，且该 finding **不**出现在 added/resolved

#### 场景:含 None id 的 finding 跳过 diff

- **当** baseline 或 current 任一 finding `id is None`（直构未走工厂）
- **那么** 返回 `diff_skipped_reason == "missing_finding_ids"`，added/resolved/changed_severity 均空

#### 场景:meta 为 None 的 legacy 报告跳过 diff 不 None-deref

- **当** baseline 或 current 任一 `report.meta is None`（legacy 1.0 / orphan 导入）
- **那么** 返回 `diff_skipped_reason == "missing_finding_ids"`、空 diff，且**不**因解引用 None 侧 `meta.target_id` 抛 `AttributeError`（前置规则 0 在任何对 None 侧 `.meta.` 访问前拦截）

#### 场景:current.meta 缺失但 baseline.meta 在仍投影 baseline_meta

- **当** `current.meta is None` 但 `baseline.meta is not None`（非对称缺失）
- **那么** 跳过 diff（`diff_skipped_reason == "missing_finding_ids"`、空 diff），但 `baseline_meta is not None`（从 `baseline.meta` 照常投影，**不**因 current.meta 缺失而置 None）；反之当 `baseline.meta is None` 时 `baseline_meta is None`

#### 场景:基线非 ok 时跳过 diff

- **当** `baseline.meta.status == "partial"` 且 `force=False`
- **那么** 返回 `diff_skipped_reason == "baseline_not_ok"`，added/resolved/changed_severity 均空

#### 场景:force 覆盖非 ok 基线

- **当** `baseline.meta.status == "partial"` 且 `force=True`
- **那么** 必须正常计算 diff（不因状态门槛跳过）

#### 场景:inspector 版本升级时其 finding 排除出 added/resolved

- **当** inspector `linux.disk.usage` baseline version=`1.0`、current version=`1.1`，两版本各有不同 finding
- **那么** `inspector_upgraded` 必须含 `linux.disk.usage`，且该 inspector 的 finding **不**出现在 added/resolved（避免版本升级被误报为全 resolved + 全 added）

#### 场景:跨 target diff 被拒绝

- **当** `baseline.meta.target_id == "host-a"` 而 `current.meta.target_id == "host-b"`
- **那么** 必须 raise `ValueError`

#### 场景:schema 版本不一致跳过 diff

- **当** `baseline.meta.report_schema_version != current.meta.report_schema_version`
- **那么** 返回 `diff_skipped_reason == "schema_changed"`，空 diff

### 需求:`hostlens reports diff` CLI 必须支持显式两 run 与自动基线两种模式

`hostlens reports diff` **必须**支持两种基线模式（显式两 run / 自动基线），从 `ReportStore` 取报告跑 `compute_diff` 并渲染结果：

- `hostlens reports diff <run_id_a> <run_id_b> [--force]`：a 作 baseline、b 作 current，从 store 取两份报告跑 `compute_diff`，渲染 added/resolved/changed_severity/inspector_upgraded。
- `hostlens reports diff --target <target> [--baseline last_success] [--force]`：current = 该 target **总序最大**（最新）run（经 `get_run` 还原为 `Report`）；baseline = `latest_ok_baseline(target, before_run_id=<current.run_id>)`（**必须排除 current 自身**），其 `run_id` 再经 `get_run` **还原为 `Report`** 才喂 `compute_diff`（`latest_ok_baseline` 返回 `BaselineRef`、无 findings，不能直接做 diff）。
- 任一 run 不存在 → stderr 单行错误 + 退出码 3，无 traceback。
- 无合格基线（首次 run / 全非 ok / 自动模式排除 current 后无更早 ok run）→ 输出「无可比基线」并退出码 0（非错误）。
- 退出码语义对齐既有 CLI（0 成功 / 3 not-found；diff 有回归不改变退出码，回归通过输出表达而非退出码）。

#### 场景:显式两 run diff 输出回归

- **当** 存在两份不同的 ok 报告，运行 `hostlens reports diff <a> <b>`
- **那么** 必须输出 added/resolved/changed_severity 三类结果

#### 场景:自动模式不把唯一 run 当自身基线

- **当** 某 target 只有一条 ok run，运行 `hostlens reports diff --target <target>`
- **那么** 必须输出「无可比基线」（current 被排除后无更早 ok run），退出码 0

#### 场景:未知 run 退出码 3

- **当** `hostlens reports diff <存在的 run> <不存在的 run>`
- **那么** stderr 单行错误，退出码 3，无 traceback

#### 场景:无基线时退出码 0

- **当** `hostlens reports diff --target <无 ok 历史的 target>`
- **那么** 输出「无可比基线」，退出码 0，无 traceback

### 需求:diff 必须可离线确定性验证（机械 Report 路径，不依赖 Agent）

回归对比必须可在无 SSH / 无付费 API / 无 Agent 下确定性验证。验收**走产出 `Report` 的机械路径**（`from_inspector_results`），**不**依赖 demo/Agent 的 `PlannerResult`：

- **CLI 空 diff**：用 `hostlens inspect <target> --inspector <确定性 inspector> --persist` 跑两次得两份内容相同的 `Report`。
- **集成 added/changed**：测试**直调 `InspectorRunner.run` + `ReplayTarget`（不复用 `_harness` 的 Agent 路径）**产出 `InspectorResult`，再经 `from_inspector_results` 组装两份 `Report`（baseline 无 critical；current 由 `linux.memory.pressure` + `linux.kernel.oom_killer` 产出含 critical 的 finding），断言 `compute_diff`。

#### 场景:同输入两次机械巡检 diff 为空

- **当** `hostlens inspect local-host --inspector hello.echo --persist` 跑两次（确定性输出 → 两份 Report 的 findings 逐字相同 → 指纹一致）→ `hostlens reports diff <run1> <run2>`
- **那么** `added`、`resolved`、`changed_severity` 必须均空

#### 场景:不同严重度场景 diff 出 added critical（集成测试，机械组装）

- **当** 测试**直调 `InspectorRunner.run` + `ReplayTarget`**（不走 Agent/`_harness`）组装两份 `Report`：baseline 无 critical、current 由 `linux.memory.pressure` + `linux.kernel.oom_killer` 产出含 critical 的 finding，对两者跑 `compute_diff`
- **那么** `added` 必须含该 critical finding 的指纹（验收在 `tests/incidents/test_diff_replay.py`，全程离线、不调 Agent / 不触 API）
