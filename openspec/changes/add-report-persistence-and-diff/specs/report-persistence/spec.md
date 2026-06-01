## 新增需求

### 需求:`ReportStore` 必须把报告以脱敏 JSON 持久化到 SQLite

`hostlens.reporting.store.ReportStore` 必须提供异步 `save(report: Report) -> SaveResult` 把报告落盘到 SQLite。`save` **必须**要求 `report.meta is not None`（缺 meta → raise `ValueError`，因索引列从 `report.meta` 投影）；返回值见 §需求:`save` 必须用 `SaveResult` 明示 orphan 降级。

- **库位置**：构造时可注入 db 路径（测试用临时库）；CLI 默认 `$XDG_DATA_HOME/hostlens/reports.db`（缺省 `~/.local/share/hostlens/reports.db`）。库与父目录不存在时自动创建。
- **WAL 模式**：连接启用 `PRAGMA journal_mode=WAL`。
- **同步驱动异步包装**：`sqlite3` 同步操作必须包在 `asyncio.to_thread`（async-first 约束）。
- **存脱敏 JSON 作 blob**：`report_json` 列存 `render_json.render(report)`（该入口已强制脱敏，对齐 OPERABILITY §7.2「写入 SQLite 的字符串都过 redact」）——**禁止**直接存内存 raw `Report`。
- **索引列从内存 `report` 投影**（**非**从脱敏 JSON 反解，避免脱敏漏穿 meta 时索引失效）：`run_id` = `meta.run_id`、`target_id` = `meta.target_id`、`target_name` = `meta.target_name`、`status` = `meta.status`、`report_schema_version` = `meta.report_schema_version`、`timestamp` = `meta.timestamp`、`finding_count` = `len(report.findings)`、`schedule_name` = `meta.schedule_name`。
- **表 `runs`**：列含 `run_id TEXT PRIMARY KEY`、`target_id TEXT`、`target_name TEXT`、`schedule_name TEXT`、`status TEXT`、`report_schema_version TEXT`、`timestamp TEXT`、`finding_count INTEGER`、`report_json TEXT`、`created_at TEXT`（落盘时填，审计用）；索引 `(target_id, status, timestamp DESC)`。表保留 SQLite 隐式 `rowid`（TEXT 主键不触发 WITHOUT ROWID），`rowid` 是**单调插入序**，用作 `meta.timestamp` 并列/非单调时的确定性 tie-break（见基线查询需求）。
- **已知限制（target 命名）**：索引列 `target_id`/`target_name` 用**未脱敏**原值（它们是标识符，不应含 secret），而 blob 中同名字段经 `render_json` 的 `redact_text`（对普通主机名是 no-op）。若 target 名**恰好命中** secret pattern（如含 `sk-`/`bearer-`/`token=`），blob 与索引会分叉——M3 不支持此类命名（target 名应是标识符，非 secret）。

#### 场景:save 落盘并可回读

- **当** 对一个 `meta` 非空的报告调 `await store.save(report)`
- **那么** 必须返回 `SaveResult`（`stored_as_orphan=False`、`run_id == report.meta.run_id`），且随后 `await store.get_run(run_id)` 必须返回该报告（JSON round-trip）

#### 场景:save 拒绝缺 meta 报告

- **当** 对 `report.meta is None` 的报告调 `await store.save(report)`
- **那么** 必须 raise `ValueError`（索引列无法投影；工厂产出的报告必带 meta，此分支防御误用）

#### 场景:落盘内容已脱敏

- **当** 报告含敏感字符串（如某 finding message 含 `sk-ABCDEFGHIJKLMNOPQRSTUVWX1234`）→ `await store.save(report)` → 读取库中 `report_json` 列原文
- **那么** `report_json` 中**不**含完整 `sk-ABCDEFGHIJKLMNOPQRSTUVWX1234` 字面量（落盘前过 render_json 脱敏）

#### 场景:finding_count 索引正确

- **当** 报告含 3 个 flatten finding，save 后查 `runs` 行
- **那么** `finding_count == 3`（供 `reports list` 直接显示 finding 数，无需加载 report_json）

#### 场景:run_id 主键唯一（防御性，非正常流）

- **当** 用同一 `run_id` 连续 save 两次（正常流每次 `uuid4()` 不撞；此为外部导入 / 重放守门）
- **那么** 第二次必须以「主键冲突」语义处理（覆盖或 raise，由实现声明；**禁止**静默产生两条同 run_id 行）

### 需求:`save` 必须用 `SaveResult` 明示 orphan 降级

`ReportStore.save` 的返回类型必须是 `SaveResult`（Pydantic/dataclass，`extra="forbid"`），含 `run_id: str`、`stored_as_orphan: bool`、`orphan_path: str | None`——使 caller 能区分「正常入库」与「降级落 orphan」（**禁止**用裸 `str` 返回，那样无法表达降级状态）。当 SQLite INSERT 失败（磁盘满 / 锁 / 权限），`save` 必须 1 次重试后仍失败则：

- 把报告 `meta.status` 改写为 `stored_as_orphan`（因 `Report` frozen，用 `report.model_copy(update={"meta": report.meta.model_copy(update={"status": "stored_as_orphan"})})`）再 `render_json` 得脱敏 JSON——与 ARCHITECTURE §9「SQLite 写入失败 → `meta.status = stored_as_orphan`」一致；这是 `ReportStatus.stored_as_orphan` 的**唯一产出点**
- 把该脱敏 JSON 写 `~/.local/share/hostlens/orphan_reports/<run_id>.json`
- 写文件前**必须**校验 `run_id` 为合法 UUID 字符串（防路径穿越）
- 返回 `SaveResult(run_id, stored_as_orphan=True, orphan_path=<path>)`，使 CLI 能以非 0 退出码提示但**报告不丢**
- **禁止**在主库失败时静默丢弃报告

#### 场景:主库不可写时落 orphan 并标记

- **当** 注入一个不可写的 db 路径（模拟磁盘/权限失败）调 `await store.save(report)`
- **那么** 报告必须被写入 `orphan_reports/<run_id>.json`（其 `meta.status == "stored_as_orphan"`），返回 `SaveResult.stored_as_orphan == True` 且 `orphan_path` 指向该文件，报告内容不丢

#### 场景:正常入库 stored_as_orphan 为 False

- **当** 正常 `await store.save(report)` 成功入库
- **那么** 返回 `SaveResult.stored_as_orphan == False` 且 `orphan_path is None`

#### 场景:非法 run_id 不写文件

- **当** orphan fallback 时 `run_id` 不是合法 UUID（如含 `../`）
- **那么** **禁止**写入文件系统（防穿越），必须 raise 明确错误

### 需求:`ReportStore` 必须提供 run 查询与基线查询 API（基线排除当前 run）

`ReportStore` **必须**提供：

- `list_runs(target_id: str, *, limit: int = 20) -> list[RunIndexRow]`：按总序 `(timestamp DESC, rowid DESC)` 返回某 target 的 run 索引行。`RunIndexRow` 是 `extra="forbid"` 模型，字段**恰为** `run_id: str` / `timestamp: datetime` / `status: ReportStatus` / `finding_count: int`（不含完整 report_json）
- `get_run(run_id: str) -> Report | None`：取完整报告（`report_json` 经 `Report.model_validate`）；不存在返回 None
- `latest_ok_baseline(target_id: str, *, schedule_name: str | None = None, before_run_id: str | None = None) -> BaselineRef | None`：在同 `target_id`（且同 `schedule_name`，None 不限）中取**总序最大**的一条 `status == "ok"` run，投影成 `BaselineRef`。**总序 = `(timestamp DESC, rowid DESC)`**——`meta.timestamp` 可能并列（两次背靠背 inspect 的 `started_at` 同值）甚至因 NTP 回拨非单调，故用单调的 `rowid` 做确定性 tie-break，**禁止**仅按 `timestamp` 排序。**`before_run_id` 给定时**：先查该 run 的 `(timestamp, rowid)`，只在总序上**严格早于**它的 run 中选基线（排除 current 自身及其之后），防自基线。`BaselineRef.inspector_versions` **必须**从该基线的 `report_json` blob（或经 `get_run`）的 `meta.inspectors_used` 投影 `name->version`，**不可**留空 `{}`（否则破坏 diff rule 5 版本对齐）。无合格基线返回 None。

> 关于 legacy schema 1.0 报告：本 store 的 `save` 拒绝 `meta is None`，故**经本 store 写入的行必带 meta**，`get_run` 不需要重建 meta。仅当从 orphan 文件 / 外部手工导入 1.0 JSON 时才会遇到 `meta is None`——该路径不在本提案范围，`get_run` 对库内行不做 legacy meta 重建。

#### 场景:list_runs 按时间倒序且受 limit 约束

- **当** 某 target 存了 3 条 run，调 `list_runs(target_id, limit=2)`
- **那么** 必须返回 2 条，且第一条 `timestamp` 不早于第二条，每条含 `finding_count`

#### 场景:get_run 不存在返回 None

- **当** 调 `get_run("00000000-0000-0000-0000-000000000000")` 且该 run 不存在
- **那么** 必须返回 None（不 raise）

#### 场景:latest_ok_baseline 跳过非 ok run

- **当** 某 target 最近一条 run 是 `partial`、更早一条是 `ok`
- **那么** `latest_ok_baseline(target_id)` 必须返回那条 `ok` run 的 `BaselineRef`

#### 场景:latest_ok_baseline 排除当前 run

- **当** 某 target 只有一条 `ok` run（run_id=X），调 `latest_ok_baseline(target_id, before_run_id="X")`
- **那么** 必须返回 None（X 被排除，不能做自己的基线）——而 `latest_ok_baseline(target_id)`（不传 before_run_id）返回 X

#### 场景:时间戳并列时按 rowid 确定性选基线

- **当** 同 target 两条 `ok` run 的 `meta.timestamp` **完全相同**（两次背靠背 inspect），先插入 run_id=A、后插入 run_id=B（`B.rowid > A.rowid`），调 `latest_ok_baseline(target_id, before_run_id="B")`
- **那么** 必须返回 A 的 `BaselineRef`（按 `(timestamp DESC, rowid DESC)` 总序，A 严格早于 B），而非 None 或 B 自身——**禁止**因 timestamp 相等而行为不确定

#### 场景:无 ok 基线返回 None

- **当** 某 target 所有 run 都非 `ok`（或无任何 run）
- **那么** `latest_ok_baseline(target_id)` 必须返回 None

### 需求:`hostlens reports list/show` CLI 必须读取 store 且遵循退出码契约

`hostlens reports` 子命令组**必须**提供 `list` 与 `show` 两个命令，从 `ReportStore` 读取持久化报告，并遵循既有 CLI 退出码契约（0 成功 / 3 not-found；非交互错误单行 stderr 无 traceback）。`<target>` 参数**匹配 `meta.target_id`**（M3 阶段 `target_id == target_name`，故用户传 target 名即可）：

- `hostlens reports list <target> [--json]`：列出该 target_id 的 run（run_id / timestamp / status / finding_count）；无 run 时输出「无历史 run」提示并退出码 0；不输出 traceback。`--json` 输出 run 索引行数组，字段集稳定（`run_id`/`timestamp`/`status`/`finding_count`）。
- `hostlens reports show <run_id> [--format md|json]`：渲染指定 run 的报告（默认 md）；run 不存在 → stderr 单行 `run not found: <run_id>` + 指引 `reports list`，退出码 3，无 traceback。
- 命令注册进 `cli-foundation` 的 Typer app（`reports` 子命令组），不改既有命令。

#### 场景:show 未知 run 退出码 3

- **当** `hostlens reports show <不存在的 run_id>`
- **那么** stderr 含 `run not found:` 与 `reports list` 指引，退出码 3，stdout 无报告正文，无 Python traceback

#### 场景:list 空历史退出码 0

- **当** 某 target 无任何持久化 run，运行 `hostlens reports list <target>`
- **那么** 输出「无历史 run」提示，退出码 0，无 traceback

#### 场景:list --json 字段集稳定

- **当** 某 target 有 run，运行 `hostlens reports list <target> --json`
- **那么** stdout 是 `json.loads` 可解析的数组，每元素恰含 `run_id`/`timestamp`/`status`/`finding_count` 字段

### 需求:`hostlens inspect --persist` 必须把机械巡检报告落盘

`hostlens inspect <target> --inspector <name> --persist` 必须在产出 `Report`（经 `from_inspector_results`，本就是该路径的产物）后调 `ReportStore.save(report)` 落盘，便于 `reports list/diff` 消费。`--persist` 默认关闭；写**本地** store 不改变远端状态，**不需** `--yes`/审批（非远端写操作）。落 orphan 时（`SaveResult.stored_as_orphan`）退出码非 0 提示但报告不丢。

> **范围说明（防 Report-assembly 误用）**：`--persist` **仅作用于产出 `Report` 的 `--inspector` 机械路径**。`hostlens inspect --intent` 与 `hostlens demo run` 走 Agent 路径、产物是 `PlannerResult`（无 `inspector_results`，无法调 `from_inspector_results`）——把 `PlannerResult` 装配成 `Report` 需要 fabrication（属 `add-diagnostician-agent` 范围，见 proposal 非目标）。故本提案**不**给 `--intent` / `demo run` 加 `--persist`。

#### 场景:--persist 后报告可被 reports list 看到

- **当** `hostlens inspect local-host --inspector hello.echo --persist` 跑两次（机械路径，确定性输出）
- **那么** `hostlens reports list local-host` 必须列出至少 2 条 run，且每条可被 `reports show <run_id>` 取出

#### 场景:--intent 与 demo run 不接受 --persist

- **当** 对 `hostlens inspect <t> --intent "..."` 或 `hostlens demo run <s>` 试用 `--persist`
- **那么** 命令必须不暴露该 flag（或显式拒绝并说明 Agent 路径持久化属后续提案）——**禁止**在 Agent 路径 fabricate `Report` 落盘
