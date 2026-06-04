## 新增需求

### 需求:runner 必须在 Report 持久化后派发 notify 并落地结果

当且仅当 job 体产出了 Report（`status in {ok, partial}`）时，runner 必须在 `ReportStore.save` 之后、构造终态 `Run` 之前，按 manifest 的 `notify` 路由把（已脱敏的）Report 发送到对应通道，并把每通道 `NotifyResult` 写入 `Run.notify_results`。无 Report 的状态（`failed_*` / `missed` / `skipped_due_to_running` / `budget_exhausted` / `daemon_stopped`）**禁止**派发 notify（无内容可推），`notify_results` 为 `[]`。

notify 阶段必须**失败隔离**：任何通道在**路由（`only_if` 求值）/ 渲染 / 发送**任一环节的异常**禁止**冒泡出 job 体（Report 已留痕），仅记为 `NotifyResult(status="failed", error=...)`；notify 整体不得改变已裁定的 `RunStatus`。隔离面**含 `only_if` 求值的任何运行期异常**（含但不限于 `TypeError` / `NameNotDefined` / `TimeoutError` / `simpleeval.InvalidExpression` 等，详见 notify-routing「`only_if` 运行期求值异常」需求），不限于渲染/发送。多通道发送必须并发执行（`asyncio.gather` + 并发上限，默认 4），通道间互不阻塞；`gather` 必须以「单通道异常不取消其它通道」的方式收集（如 `return_exceptions=True` 或每通道独立 try）。`channel_registry` 必须经 runner 构造器注入（与既有 `RunStore` / `ReportStore` / `backend_factory` 同列，无 module-level singleton），daemon / `schedule run` / `trigger` 共用同一装配。

`Run.notify_results` 持久化/反序列化沿用既有 RunStore 的 `model_validate_json` 路径。**已知可接受弱化（F15）**：`notify_results` 从 M4 的 `list[object]`（宽松）收紧为 `list[NotifyResult]`（严格）后，理论上新增「单条畸形 NotifyResult 记录拖累 `schedule status` 整表查询」的反序列化失败面——但 M5 起 `notify_results` 仅由本 runner 用强类型 `NotifyResult` 写入，正常运行不产畸形记录；单记录读取隔离继承 baseline RunStore 查询契约（本 delta 不收紧、不新增该保证），跨期 schema 演进的兼容性留后续里程碑，非本提案目标。

#### 场景:有 Report 的触发派发并记录每通道结果

- **当** job 体产出 `ok` Report，manifest 配两个通道（一个 `only_if` 真、一个假）
- **那么** `Run.notify_results` 必须含两条：真的记 `sent`（或 `failed`），假的记 `skipped`；`RunStatus` 仍为 `ok`

#### 场景:通道发送异常不改变 RunStatus 且不冒泡

- **当** 某通道 `send` 持续失败耗尽重试
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`

#### 场景:only_if 运行期求值异常不改变 RunStatus 且不冒泡

- **当** 某通道 `only_if` 运行期抛异常（如类型不匹配 / 拼错名 / 求值超时）
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`，其它通道照常派发

#### 场景:无 Report 状态不派发 notify

- **当** 触发结果为 `failed_api_unavailable`（无 Report）
- **那么** 必须不发生任何通道发送，`Run.notify_results == []`

## 修改需求

### 需求:`Run` 模型必须强制 `report_id ⇔ status` 不变量

`hostlens.scheduler` 必须定义 Pydantic v2 `Run` 模型，字段：`run_id: str` / `schedule_name: str` / `triggered_at: datetime`（tz-aware）/ `started_at: datetime | None` / `finished_at: datetime | None` / `status: RunStatus` / `report_id: str | None` / `error: str | None` / `notify_results: list[NotifyResult] = []`。**M5 起 `notify_results` 收紧为 `list[NotifyResult]`**（`NotifyResult` 由 `hostlens.notifiers` 提供，M5 起存在，不再有 M4 的 `ImportError` 约束）：有 Report 的触发经 notify 派发后填入每通道结果；无 Report 的状态恒为 `[]`。M4 写入的空数组 `[]` 反序列化仍合法（向后兼容、无迁移）。

为满足 CLAUDE.md §4.6 / ARCHITECTURE §7 第 819 行「每次触发留痕含 who/when/target/inspectors/report_hash」，`Run` 还必须含：

- `targets: list[str]`（本次触发的 target 集合，所有状态恒有）。
- `inspectors: list[str]`（本次**实际跑**的 inspector 集合快照）。**来源契约**：有 Report 时从 `report.meta.inspectors_used`（`reporting.models.ReportMeta.inspectors_used: list[InspectorRun]`，每个 `InspectorRun.name`）投影各 inspector name；job 体未启动的状态（`missed` / `skipped_due_to_running` / `budget_exhausted`）Planner 从未运行、本就无 inspector 执行，`inspectors` 为 `[]` 是**语义正确的「本次无 inspector 执行」**而非漏记。
- `report_hash: str | None`（**完整性锚点**，非跨运行去重键）。**算法契约**：`report_hash = hashlib.sha256(reporting.render_json.render(report).encode()).hexdigest()`，其中 `reporting.render_json.render`（公开函数，`__all__=["render"]`；`ReportStore.save` 内部以 `render_report_json` 别名调它，store.py:178）产出的是 `ReportStore.save` **持久化的同一份**确定性渲染（`model_dump_json` 字段序固定、已脱敏）。**计算时机/对象**：runner 对它产出的 Report（save **之前**持有的对象）`render` 取指纹。语义：锚定本次诊断 Report 的内容、把它绑定到这条 Run，使事后可校验内容未被篡改/确实对应这条 Run。**正常 db 路径**（`report_storage="db"`）：`ReportStore.save` 持久化的就是同一份 `render(report)` 字节，故 `report_hash` 与 reports.db 落库 JSON **逐字节一致**（完整性锚点完全成立）。**orphan 路径**（`report_storage="orphan"`）：`ReportStore.save` 会先把 `meta.status` 改写为 `stored_as_orphan` 再 `render` 写 orphan 文件（store.py:234-239），故 orphan 文件字节与 `report_hash`（基于原 status 的 render）**不**逐字节相等——`report_hash` 此时锚定的是**逻辑诊断内容**而非 orphan 文件字节（这是 orphan 降级的可接受弱化）。**不**声称跨运行可复现（Report 含本次时间戳）；只保证同一 Report 对象渲染得同一 hash（确定性、可测）。无 Report 的状态 `report_hash` 为 `None`。
- `report_storage: Literal["db", "orphan"] | None = None`（报告存储形态，**确定的 orphan 注记载体**，不复用 `error` 字段）：`ReportStore.save` 正常入库 → `"db"`；降级写 orphan JSON（`SaveResult.stored_as_orphan=True`，`report_id` 指向的内容不在 reports.db、`get_run` 取不到）→ `"orphan"`；无 Report 的状态 → `None`。供 `schedule status` 区分「report_id 必可经 `get_run` 取回（db）」与「需走 orphan 文件（orphan）」。

`Run` 必须用 `model_validator(mode="after")` 强制硬不变量：

1. **`report_id is None` 当且仅当 `status not in {RunStatus.ok, RunStatus.partial}`**。违反（`ok`/`partial` 却无 `report_id`，或非这两者却挂了 `report_id`）必须 raise `ValidationError`。
2. **`started_at is None` 当 `status in {missed, skipped_due_to_running, budget_exhausted}`**——这三值 job 体在拿到执行/配额前即被裁定、从未真正 started，**对齐 ARCHITECTURE §7 第 842 行的三值集合**。其余状态（含 `failed` / `failed_api_unavailable` / `daemon_stopped`）`started_at` 允许非 None。违反必须 raise `ValidationError`。
3. `report_hash is not None` 仅允许在 `status in {ok, partial}`（有 Report 才有指纹）；其余状态 `report_hash` 必须为 `None`。
4. `report_storage is not None` 当且仅当 `status in {ok, partial}`（有 Report 才有存储形态）；其余状态 `report_storage` 必须为 `None`。

#### 场景:ok 状态必须带 report_id

- **当** 构造 `Run(status=ok, report_id=None, ...)`
- **那么** 必须 raise `ValidationError`（不变量违反）

#### 场景:missed 状态禁止带 report_id

- **当** 构造 `Run(status=missed, report_id="r1", ...)`
- **那么** 必须 raise `ValidationError`（不变量违反）

#### 场景:partial 带 report_id 合法

- **当** 构造 `Run(status=partial, report_id="r1", started_at=<dt>, ...)`
- **那么** 必须成功构造

#### 场景:skipped_due_to_running 的 started_at 为 None

- **当** 构造 `Run(status=skipped_due_to_running, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（该状态 job 未启动，started_at 必须 None）

#### 场景:missed 的 started_at 为 None

- **当** 构造 `Run(status=missed, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（该状态 job 未启动，started_at 必须 None）

#### 场景:budget_exhausted 的 started_at 为 None

- **当** 构造 `Run(status=budget_exhausted, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（早期失败、job 体未启动，started_at 必须 None，对齐 §7 三值集合）

#### 场景:failed 状态允许 started_at 非 None

- **当** 构造 `Run(status=failed, report_id=None, started_at=<dt>, finished_at=<dt>, error="...", ...)`
- **那么** 必须成功构造（`failed` 是 job 体已启动后抛异常，started_at 应非 None）

#### 场景:report_hash 仅在有 Report 状态非空

- **当** 构造 `Run(status=missed, report_id=None, report_hash="abc", ...)`
- **那么** 必须 raise `ValidationError`（无 Report 状态禁止挂 report_hash）

#### 场景:report_hash 对同一 Report 确定且等于持久化字节指纹

- **当** 对同一个 `Report` 对象计算 `report_hash` 两次
- **那么** 两次必须相等，且等于 `hashlib.sha256(reporting.render_json.render(report).encode()).hexdigest()`（`render` 即 `ReportStore.save` 持久化用的同一函数、同一份渲染字节，可被测试逐字节核对）

#### 场景:notify_results 收紧为 NotifyResult 且 M4 空数组兼容

- **当** 反序列化一条 M4 写入的 `notify_results: []` 记录，以及构造一条 M5 含 `[NotifyResult(channel="x", status="sent")]` 的记录
- **那么** 两者必须均合法；`notify_results` 元素类型为 `NotifyResult`（非 `object`）；空数组向后兼容无需迁移
