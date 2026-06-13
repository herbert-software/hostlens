## 1. 依赖注入骨架 + scheduler 抑制路径

- [x] 1.1 新增 `ManagementToolDeps` frozen dataclass（`load_manifests` / `run_store` / `report_store` / `load_channel_summaries`（产 `ChannelSummary={name,type}` 列表，**不经** `load_channels` 的 `${ENV}` 展开）/ `build_runner`）+ `ChannelSummary` 只读模型 + `register_mcp_management_tools(registry, *, deps)` 装配函数骨架（先不挂工具）。验收：空装配不报错；单测断言 `ToolContext` 字段集**仍恰为 6 个**（无 `schedule_store`/`report_store`/`channel_summaries` 等新字段）
- [x] 1.2 `scheduler/runner.py` 加 keyword-only `dispatch_notify: bool = True`，**逐层穿透** `trigger`→`_run_job`→`_finalize_outcome`→`_map_outcome` 直到 `_dispatch_notify` 调用点（每层默认 `True`）；`False` 时在 Report 持久化后跳过 `_dispatch_notify` 整段（连 `only_if` 求值），`notify_results=[]`，其余留痕不变。验收：单测 `dispatch_notify=False`（**配了 notify 通道**的 schedule）→ Report 持久化 + `notify_results==[]` + 以 spy/mock 断言通道 `only_if` 求值与 `send` **调用计数均为 0**（不只查空列表——空列表在无通道时也成立）+ `status`/`report_id` 与 `True` 路径一致；`dispatch_notify=True` 默认行为字节不变（既有 runner/daemon 测试全绿）；**timer 路径零变更**（timer 注册 `_run_job` 不经 `trigger`，靠各层默认 `True`）；**既有 SIGTERM 优雅停机 / misfire 合并不补跑 / 单实例 backlog 策略行为零变更**（既有 scheduler 测试全绿，本任务不触碰调度面）

## 2. 纯查询工具（零 LLM，薄适配层，复用既有 store/loader）

- [x] 2.1 `list_schedules`：input/output schema + handler（复用 schedule loader，**每调用 fresh-load**；`next_fire_time` 由 manifest trigger 直接以 apscheduler `CronTrigger`/`IntervalTrigger` 计算，**禁止 import `cli/` 私有符号** `_next_fire_time`——`tools/` 不反向依赖 `cli/`）+ ToolSpec(`side_effects=none`, `sensitive_output=True`)。验收：fresh-load manifests 含 name/schedule 表达式/`next_fire_time`/targets/intent/`notify`（每条绑定 channel + only_if，兑现路由可见性承诺）；**输出无 `enabled` 字段**（`ScheduleManifest` 无此字段，M4 无 schedule 启停概念）；空 `schedules/` 返回空列表不抛；fresh-load 抛 `ConfigError`（manifest 畸形/未知 target/`only_if` 语法错——**注：未知 channel 不在 fresh-load 校验**，`load_schedules` 不读 `notifiers.yaml`，channel 存在性在 runner 装配期校验）→ dispatch 通用 except 包脱敏信封不裸传
- [x] 2.2 `get_schedule_status`：schema(`name?`, `limit` 默认 10 / 上限 100，**钳制在 handler 侧**——`RunStore.list_recent` 默认 20、无上限) + handler（复用 `RunStore`）+ ToolSpec。验收：`limit` 上限钉死单测（传 99999 → 实际 ≤100）；输出每条含 ledger `run_id` 与 `report_id` 两 ID（**无-Report 状态的 Run 其 `report_id`/`report_hash` 为 `None`——输出 schema 须容 nullable**）；`notify_results` 经 `redact_secret_text` 脱敏——**密钥不进输出**测试；空/无 `runs.db` 返回空列表
- [x] 2.3 `list_channels`：`ChannelSummary={name,type}`（`extra="forbid"` 封死形状）+ handler（**新增只读 raw 通道摘要读取**：解析 `notifiers.yaml` 原文取 name/type，**不复用 `load_channels`**——它展开 `${ENV}` 成明文 secret，fresh-load）+ ToolSpec。验收（**正向白名单非黑名单**）：配含 `bot_token: ${TG_TOKEN}`/`webhook_url: ${HOOK}`/`secret: ${SIGN}` 的 telegram/lark 通道 → 输出每条**恰含且仅含 name/type**；断言**任何其它 raw 键（bot_token/webhook_url/secret/chat_id）及其 `${ENV}` 字面量本身**（如 `"${TG_TOKEN}"`）均不出现、无 token 明文 / 无 `${ENV}` 展开值；**输出无 `enabled`/`only_if` 字段**（通道模型无此二字段）
- [x] 2.4 `list_reports`：schema(`target` **必填**, `limit` 默认 20) + handler（复用 `ReportStore.list_runs(target_id)`，1:1 不新增 store 方法）+ ToolSpec。**输出 ID 命名**：`RunIndexRow.run_id` 字段值实为 report-store 键（= `show_report` 的有效键），output schema 必须以 **`report_id`** 命名暴露该 id（不直返 `RunIndexRow` 的 `run_id` 字段名，避免与 `get_schedule_status` 的 ledger `run_id` 混淆）。验收：空/无 `reports.db` 时以具体 target 调返回空列表；远程 LLM 经 `list_targets` 枚举 target 再逐一查；**断言输出 id 字段名为 `report_id` 且无 `run_id` 键**、该 `report_id` 可直接喂 `show_report`。注：只读复用既有 store，**不改保留/压缩策略**（retention/compaction 验收 N/A）
- [x] 2.5 `show_report`：schema(`report_id`) + handler（`ReportStore.get_run(report_id)`）+ ToolSpec。验收：存在 `report_id`（= `get_run` 的键 = 其它工具输出的 `report_id`）返回含 findings/hypotheses 的 Report；不存在 → 结构化 not-found 信封，消息经脱敏**不含内部文件路径**
- [x] 2.6 `diff_reports`：schema(`report_id_a`,`report_id_b`) + handler（直接调 `reporting/diff.compute_diff` 并自捕 `ValueError`，**禁止 import** `cli/reports.py` 的 `_compute_diff_or_exit`——它 `raise typer.Exit`，MCP 进程内不可用）+ ToolSpec(`side_effects=read`, `sensitive_output=True`)。验收：两份存在且同 target 的报告产 diff；任一不存在 → 结构化 not-found；**两份跨 target（`compute_diff` 抛 `ValueError`）→ 结构化错误信封**（语义对齐 `_compute_diff_or_exit` 但不 import 它，不让裸 `ValueError` 透传）

## 3. run_schedule_now（复用 runner，触发 LLM pipeline）

- [x] 3.1 实现 `build_runner` 工厂闭包（`(ctx, manifests) -> SchedulerRunner`：ctx 取 target/inspector registry，闭包取 channels/report_store/run_store/backend_factory），注入 `ManagementToolDeps`。**backend_factory 必须 daemon-safe**（闭包绑 `daemon_mode=True` 的 settings 构造 backend，使订阅 backend serve 启动期即 raise，见 5.1 / `mcp-cli-command` spec）。**同源不变量**：serve 启动期 eager 探针 backend 与本 `backend_factory` 闭包**必须绑同一份 `daemon_mode=True` settings**（防探针校验通过、factory 误绑原始 settings 致 dispatch 期绕过 daemon 门）。验收：工厂构造的 runner 与 `cli/schedule.py` 的 `_build_runner` 的**装配数据等价**（crosscheck 测试，对齐注入的 store/channels/registry）；注意「等价」仅指装配数据，**错误处理不照搬** `_build_runner` 的 `typer.Exit`（MCP 进程内不得 `typer.Exit`，须 raise 让 handler/serve 处理）；同源不变量验收**须以 spy/monkeypatch 固化**（当前无可构造 backend 能可观测触发 daemon 门——`AnthropicAPIBackend` 的门是 no-op、placeholder 在门前抛 `NotImplementedError`，故「构造没崩」是 vacuous）：spy `create_backend`，断言启动期探针与 `backend_factory` 两次调用传入的 settings **是同一对象/值且 `daemon_mode is True`**
- [x] 3.2 `run_schedule_now`：schema(`name`) + handler（fresh-load manifests → **前置检查 name 命中**（未知 → 结构化 not-found，**不**调 `build_runner`/`trigger`、**不**让 `trigger` 的裸 `KeyError` 透传）→ `build_runner` → `trigger(name, dispatch_notify=False)`）+ ToolSpec(`side_effects=read`, `sensitive_output=True`, 长 `timeout` ≥120s)。输出含 ledger `run_id` / `status` / `report_id`（**文案明示据 `report_id` 调 `show_report`**）。验收：
  - cassette 回放跑通 pipeline + 持久化 Report + `notify_results==[]`（以 spy 断言 channel `send` 调用计数=0，配了通道的 schedule，证明抑制真生效），返回非空 ledger `run_id`/`report_id`
  - 返回的 `report_id` 可直接喂 `show_report` 取回该 Report（证明是 `get_run` 有效键，ledger `run_id` 不是）
  - 未知 `name` → 结构化 not-found 信封，**不触发任何 pipeline**、**无裸 `KeyError` 透传**
  - **prompt cache hit rate 验证**：复用 pipeline 既有两层 cache，cassette 第二次 run `cache_read_input_tokens>0`（不新增 cache 策略）
  - **Anthropic 429 with retry-after 严格 honor + backend 完全宕机 → `failed_api_unavailable`（无 Report）/ token 退化 → `partial`**（复用 pipeline 既有 backend 失败语义；**runner 从不构造 `budget_exhausted`**；handler 返回脱敏错误信封不抛裸异常）

## 4. surface 投影与文案

- [x] 4.1 7 工具各撰 `mcp_description`（远程 LLM）/ `agent_description`（本地 loop）。验收：单测断言 7 工具两 description 均非空且**互不相等**
- [x] 4.2 7 工具 opt-in `surfaces={"agent","mcp"}`，经 agent adapter + mcp adapter 投影。验收：`registry.list_for("mcp")` 与 `list_for("agent")` 均含全部 7 工具

## 5. serve 装配接线

- [x] 5.1 `cli/mcp.py serve`：从 `Settings` 构造 `ManagementToolDeps`（含 daemon-safe backend_factory），在 `register_default_tools` 后调 `register_mcp_management_tools`。**装配顺序钉死**：mcp SDK 缺失检测（`_import_mcp_server` 的 `ImportError`→exit 1）必须**先于** `ManagementToolDeps`/backend 构造——使「mcp SDK 未装 + notifiers.yaml 同时不可读」的组合输入确定走 exit 1（SDK 缺失优先），而非被 `ConfigError`（exit 2）抢先。验收：`hostlens mcp serve` 的 `list_tools` 返回 **10** 工具（只读三件套 + 7 管控）；选定 daemon-unsafe/未实装 backend（当前 placeholder→`NotImplementedError`）时 serve 启动期 eager 构造**一个探针 backend**（用后即弃）并 catch `BackendDaemonUnsafe`/`NotImplementedError`（→ **exit 1**）/`ConfigError`（→ **exit 2**）→脱敏退出、**无裸 traceback**（退出码与既有 daemon `_serve` 的 `_fail`(exit1)/`_fail_config`(exit2) 一致）
- [x] 5.2 serve 装配依赖构造失败 fail-loud：`notifiers.yaml` 不可读（`ConfigError`）→ 进运行态**前** **exit 2**、stderr 脱敏错误、**无裸 traceback**、不以 exit 0 静默。验收：非交互 CLI **exit 2** 测试（配置错对齐全局约定 + serve 既有 `_load_settings_or_exit` 的 exit 2）。退出码 1 在 serve 落地后共 4 生产者：既有 2 处（mcp SDK 未装 `ImportError`、`build_server` fail-closed `ToolPolicyViolation`）保留 + 本提案新增 2 处（daemon-safe 探针的 `BackendDaemonUnsafe`/`NotImplementedError`，须在 `cli/mcp.py` 新增 catch、不依赖 schedule.py 的 `_fail`）；配置错恒走 exit 2 与之区分

## 6. fail-closed 与回归门

- [x] 6.1 fail-closed 自检测试：构造一个漏声明 `sensitive_output` 的管控工具 → `build_server` eager `list_for_mcp` raise `ToolPolicyViolation`（运行态前 fail-closed）
- [x] 6.2 crosscheck 回归门：对 7 工具逐一硬断言 `sensitive_output is not None` / `surfaces=={"agent","mcp"}` / `requires_approval is False` / `side_effects in {"none","read"}`（防后续新增工具漏配，grep 盲点用 pytest 兜底）
- [x] 6.3 dispatch 投影集成测试：经 `McpToolsAdapter.dispatch` 端到端跑通每个工具（含 not-found 路径错误信封经 `scrub_exception_message` 脱敏）

## 7. 文档与 Demo Path

- [x] 7.1 更新 `docs/integrations/*`（MCP 工具清单 3→10）+ 与各 `mcp_description` 对齐
- [x] 7.2 Demo Path 离线验证：`hostlens mcp serve` → `list_tools`=10 → 6 查询工具 cassette/离线跑通 + `run_schedule_now` cassette 回放（无 SSH / 无付费 API）
- [x] 7.3 勾选 TODO.md「M7-ext 读期 / 提案①」对应行（实现完成后；归档走独立 archive 流程不在本清单）
