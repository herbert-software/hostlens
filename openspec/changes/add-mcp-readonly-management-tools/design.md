## 上下文

M7 的双层 capability 架构（`ToolSpec` Layer 1 + surface adapter Layer 2）对 registry 内容是泛型的：`McpToolsAdapter.list_for_mcp` / `dispatch` / `build_server` 不关心工具数量与种类，只强制「`surfaces ∋ "mcp"` + `sensitive_output is not None` + `side_effects ∉ {write,destructive}` + `requires_approval=False`」。本提案的 7 个只读工具天然满足这些门，因此**无需触碰 adapter/server/gate**——这正是 M7 架构「加只读工具 = 只加 ToolSpec」的兑现。

真正的设计张力不在「怎么过 gate」，而在「7 个 handler 的依赖从哪来」：它们需要 scheduler store / loader / runner、report store / diff、notifier config，而这些**都不在** `ToolContext` 的 ADR-008 锁死 6 字段里。

当前装配链（`cli/mcp.py serve`）：`ToolRegistry()` → `register_default_tools(registry)` → `build_server(registry, context_factory)`；`context_factory` 每次 dispatch 造一个 `ToolContext(target_registry, inspector_registry, config, logger, NoopApprovalService, cancel)`。

## 目标 / 非目标

**目标：**
- 7 个只读管控工具上 MCP（+ agent）surface，零 adapter/server/gate 改动。
- handler 拿到 scheduler/report/notifier 依赖，**而不扩 `ToolContext`**。
- `run_schedule_now` 复用既有 Runner（含 Run 留痕 + Report 持久化），只抑制 notify 派发。
- `side_effects` 在每个 ToolSpec 上保持**静态可判定**。

**非目标：**
- 不建写工具 / approval 机制（写期提案）。
- 不缓存依赖到 module-level singleton（违反 §6 No global state）。
- 不优化「manifest 长驻缓存」——优先 freshness（见 D-4）。

## 决策

### D-1：依赖走「注册期闭包注入」，不扩 ToolContext
**选择**：新增 `register_mcp_management_tools(registry, *, deps: ManagementToolDeps)`，把 scheduler/report/notifier 依赖在 `serve` 装配期闭包绑进 7 个 spec factory 的 handler；`ToolContext` 6 字段冻结集**不动**。

**理由**：本仓已有先例——`register_default_tools(clock=…, collector=…)` 正是用闭包注入把「非 ToolContext 依赖」绑进 `run_inspector` handler，而非塞进容器。沿用同一模式保持一致性，且守住 ADR-008：`ToolContext` 一旦为某个 handler 开口子加字段，所有 handler（含 Inspector 侧）都能拿到，边界失守。

**备选**：
- ❌ 扩 `ToolContext` 加 `schedule_store/report_store/notifier_config` 字段——破坏冻结 ABI，每加一类 handler 就动核心容器，且把只读管控依赖暴露给全体 handler。
- ❌ handler 内从 `ctx.config` 自建 store——反 DI、不可测（无法注入 fake store）、且每次 dispatch 重建连接。

### D-2：依赖打包成 frozen dataclass `ManagementToolDeps`，不用 N 个 kwargs
**选择**：
```python
@dataclass(frozen=True)
class ManagementToolDeps:
    load_manifests: Callable[[], list[ScheduleManifest]]      # 每调用 fresh load
    run_store: RunStore
    report_store: ReportStore
    load_channel_summaries: Callable[[], list[ChannelSummary]]  # 只读 raw name/type，不展开 ${ENV}
    build_runner: Callable[[ToolContext, list[ScheduleManifest]], SchedulerRunner]
```
**理由**：5 个依赖用 5 个 kwargs 易错且签名脆；frozen dataclass 单一参数更稳、可整体 mock。`build_runner` 是工厂而非现成 runner——因为 Runner 需要 `ctx` 里的 target/inspector registry（dispatch 时才有），故 `run_schedule_now` handler 在调用时 `deps.build_runner(ctx, manifests)`，把 ctx 侧依赖与闭包侧依赖在调用点汇合。工厂返回的是 **`SchedulerRunner`**（`scheduler/runner.py:141` 的真实类名，**不是** `ScheduleRunner`——后者不存在）。

**两处类型订正（防实现期 ImportError / 不可 type-check）**：
- 真实类名是 `SchedulerRunner`，本提案早期草稿误写 `ScheduleRunner`，全部订正。
- **`NotifierConfig` 类型不存在**于代码库；既有 notifier 加载器是 `load_channels(settings, registry) -> dict[str, Notifier]`，它在加载期把 `${ENV_VAR}` 展开成**明文 secret** 注入每个 Notifier 实例 config，**不适合**喂给 `list_channels`（否则 token 明文进输出）。故 `list_channels` 的依赖是一个**新增的只读 raw 通道摘要加载器** `load_channel_summaries`：直接解析 `notifiers.yaml` 原文（复用既有 `_parse_yaml` 等价逻辑），每次 fresh-load 仅取每个通道的 `name`（实例 key）与 `type`，**不经** `load_channels` 的展开路径。`ChannelSummary` 是本提案新增的 `{name, type}` 只读模型（以 `extra="forbid"` 封死形状，物理实现 list_channels 的正向白名单语义，与 spec 一致）。这是本提案承认的**唯一一处新增读取逻辑**（非「零业务逻辑改动」——见 D-7），其余 handler 仍是既有 store/loader 的薄复用。

**备选**：沿用 `register_default_tools` 的散 kwargs 风格——2 个依赖时尚可，5 个时可读性差。

### D-3：`run_schedule_now` 抑制 notify 走 runner 新增 `dispatch_notify` 参数（穿透 4 层）
**选择**：`SchedulerRunner.trigger(name, *, dispatch_notify: bool = True)`；`run_schedule_now` handler 传 `False`，最终跳过 `_dispatch_notify`。默认 `True` 使 daemon / `schedule trigger` CLI **零行为变更**。

**理由**：Runner 的 job body（pipeline → 持久化 Report → 派发 notify → 写 Run）是单一 SOT；复制一份「不发通知版」会重复 Run 记录 / RunStatus 映射 / Report 持久化逻辑，违反「复用不重写」。一个向后兼容的 keyword-only 开关是最小切口。

**实现拓扑（非「只改 trigger 一行」）**：真实 notify 派发点是 `_map_outcome` 调 `_dispatch_notify`（`runner.py:548`），距 `trigger` 4 层：`trigger`(337)→`_run_job`(373)→`_finalize_outcome`(465)→`_map_outcome`(492)。且 **timer 路径注册的 job body 是 `_run_job` 而非 `trigger`**（`runner.py:299` `add_job(self._run_job, …)`）。因此 `dispatch_notify` 必须**逐层穿透** `_run_job`/`_finalize_outcome`/`_map_outcome` 直到 `_dispatch_notify` 调用点，**每层默认 `True`**——timer 以 `_run_job(name)` 调用、不传参，唯有默认 `True` 才保 timer/daemon 字节不变。「最小切口」指语义切口最小（单一开关、单一抑制点），不指物理改动只一行。`scheduler-engine` spec 的「参数穿透契约」固化此不变量。

**备选**：
- ❌ `run_schedule_now` 自己重建 pipeline——重复 runner 编排，且漏掉 Run 留痕。
- ❌ 新增 `runner.run_once_without_notify()` 方法——与 `trigger` 90% 重叠，徒增表面。

### D-4：`side_effects` 静态分类 + notify 拆分
**选择**：`run_schedule_now` 静态标 `side_effects="read"`（跑只读 inspector + 本地持久化，无 host/外部状态变更）；发通知能力**不**作为 `notify=true` 参数挂在本工具上，而是拆到写期独立工具 `notify_report`。

**理由**：`ToolSpec.side_effects` 是静态字段，`dispatch` gate 据此静态判定。若靠 `notify` 参数让同一工具在 read/write 间横跳，gate 无法静态判定该次调用是否该走 approval。拆分使每个 ToolSpec 的 side_effects 单一确定。token 消耗是「成本」不是「副作用等级」——read 分类指的是不可变性，不是免费。

### D-5：manifest / 通道摘要每次调用 fresh load
**选择**：`load_manifests` / `load_channel_summaries`（**与 D-2 同名字段，非旧名 `load_notifier_config`**）是闭包注入的 **callable**，每次 handler dispatch 重新 walk `schedules/*.yaml` / 解析 `notifiers.yaml` 原文（仅取 name/type，不经 `load_channels` 的 `${ENV}` 展开），而非装配期加载一次缓存。

**理由**：MCP server 长驻；用户可能在 server 运行期间编辑 schedule/notifier 配置。Fresh load 与 CLI（每次调用重载）语义一致，避免 stale。查询类工具开销低（一次文件 walk），freshness 优先。`run_store` / `report_store` 是 SQLite 句柄（连接可复用），不属此列。

**备选**：装配期加载一次——省一次 walk，但长驻 server 下 `list_schedules` 会展示过期数据，误导诊断。

### D-6：一个新 capability `mcp-management-tools` 承载 7 工具 + 装配契约
**选择**：7 个 ToolSpec、各自 `sensitive_output`/双 description、`register_mcp_management_tools` 装配函数、`ManagementToolDeps` 形状，全部归入新 spec `mcp-management-tools`。

**理由**：它们是一组同质能力（MCP 管控工具集），共享装配与注入契约；归一个 capability 便于写期提案将来 MODIFY 同一 spec 追加写工具。`mcp-server`/`mcp-tool-adapter` 保持不变（泛型不受影响）。

### D-7：契约对齐真实数据模型（review 后修正的 5 处「薄复用」断层）
首版提案假设「7 个工具皆零业务逻辑、纯薄复用既有 store/loader」。对抗性 review 逐条核对真实代码后，发现 5 处该假设不成立，须在 spec 落地前对齐，否则实现期必翻车：

1. **`list_channels` / `list_schedules` 不得输出不存在的字段**：通道配置（`notifiers.yaml`，`load_channels`）无 `enabled` 字段、`only_if` 属 `NotifyConfig`（`scheduler/schema.py:166`）的 per-schedule 绑定不属通道；`ScheduleManifest`（`extra="forbid"`）无 `enabled` 字段。故 `list_channels` 输出收窄为 `{name, type}`、`list_schedules` 去掉 `enabled`（M4 无 schedule 启停概念）。路由可见性诉求由 `list_schedules` 的 `manifest.notify[].only_if` 满足，不挂在 `list_channels`。
2. **`list_reports.target` 必填**：`ReportStore.list_runs(target_id: str, *, limit)` 的 `target_id` 必填、无 all-targets 枚举方法；既有 `reports list <target>` CLI 同样必填。为守「薄复用、不新增 store 方法」，`target` 改必填，远程 LLM 先 `list_targets` 枚举再逐一查。（备选「新增 `ReportStore.list_all_runs`」= 新业务逻辑，与本提案非目标冲突，否决。）
3. **`run_id` 与 `report_id` 是两个 ID 空间**：scheduler ledger `Run.run_id`（`runner.py:503` 新 UUID）≠ report-store 键（在 `Run` 上以 `report_id` 暴露，= `meta.run_id` = `ReportStore.save` 返回键）。`show_report`/`diff_reports` 的查询键是 **report-store 键**（`get_run(...)`）。故 `run_schedule_now`/`get_schedule_status` 输出须同时暴露 ledger `run_id` 与 `report_id`；`list_reports` 复用的 `RunIndexRow.run_id` 字段名虽叫 `run_id` 但**值实为 report-store 键**，故 `list_reports` 输出也必须以 `report_id` 命名暴露该 id（spec ID 命名契约固化三工具一致）。文案明确指示远程 LLM **据 `report_id` 调 `show_report`**——否则用 ledger `run_id` 调会命中 not-found（跨工具契约陷阱）。
4. **未知 schedule name 须 handler 前置检查**：`SchedulerRunner.trigger(name)` 对未知 name 抛裸 `KeyError`（`runner.py:343`），而 `McpToolsAdapter.dispatch` 对 `KeyError` 是**原样透传**（不包脱敏信封）。故 `run_schedule_now` handler 须在 fresh-load manifests 后**前置检查命中**、返回结构化 not-found，不能直接复用 `trigger` 的 `KeyError`。注：「schedule 引用未知 target」不走此路——`load_schedules`（fresh-load）在加载期已校验 target 存在（`loader.py:99-107`），未知 target 的 manifest 在 fresh-load 即 `ConfigError`（被 dispatch 通用 except 包脱敏信封），故 runner `_run_job` 的 `target_registry.get` `KeyError` 路径不可达。
5. **`get_schedule_status.limit` 钳制在 handler 侧**：`RunStore.list_recent` 默认 `limit=20`、无上限钳制，SQL 直接透传任意值。故 default 10 / max 100 的语义由 handler 实现，不是「复用 store 自带」。

### D-8：MCP serve 的 backend 工厂强制 daemon-safe（前瞻门 + 当前 placeholder 双覆盖）
`run_schedule_now` 经 `build_runner` → **daemon-safe `backend_factory`**（闭包捕获 `daemon_mode=True` 的 settings）构造 backend，但 daemon-safety 门 `ensure_safe_for_daemon` 仅在 `settings.daemon_mode is True` 触发，而 `mcp serve` 默认不置 `daemon_mode`。MCP server 是长驻、接受远程 LLM 指令的进程——正是 CLAUDE.md §4.11 rule 3 所禁的语境。**选择**：serve 以 `model_copy(daemon_mode=True)` 的 settings 构造管控工具 `backend_factory`、并在启动期 eager 构造一个**探针 backend**（复用 `cli/schedule.py:430/441` 的 daemon 翻转 + boot-eager 模式）。

**eager 探针语义 + 同源不变量**：eager 构造的 backend 实例**仅供 daemon-safe 校验、随即丢弃**；后续每次 dispatch 由 `build_runner` 经**同一 `backend_factory`** 按需重建（per-fire backend，ADR-008 不进 ToolContext）。不变量：eager 探针与 `backend_factory` 闭包**必须绑同一份 `daemon_mode=True` settings**，否则「探针校验通过、factory 误绑原始 settings」会在订阅 backend 实装后留下「dispatch 期绕过 daemon 门」的洞。

**诚实边界 + 退出码**：daemon 门 `ensure_safe_for_daemon`→`BackendDaemonUnsafe` 是**前瞻保护**，待订阅 backend 实装（M10.5）后才以该具体异常触发；**当前** `bedrock`/`vertex`/`claude_subscription` 在 `create_backend` 均为 placeholder、直接抛 `NotImplementedError`（先于 daemon 门）。两路径净效果一致（远程 LLM 不可驱动这些 backend），但异常类型不同。故 serve 的 eager 探针构造**必须同时 catch `BackendDaemonUnsafe` / `NotImplementedError` / `ConfigError`**——既有 `_serve` 只 catch `BackendDaemonUnsafe`+`ConfigError`（`cli/schedule.py:442-445`），MCP serve 不可照搬、须补 `NotImplementedError`，否则 placeholder backend 会漏成裸 traceback。**退出码**与既有 daemon `_serve` 一致：`BackendDaemonUnsafe`/`NotImplementedError`（backend 不可用的启动期拒绝）→ exit 1（`_serve` 的 `BackendDaemonUnsafe` 经 `_fail` 即 exit 1）；`ConfigError` → exit 2。

### D-9：serve 退出码——exit 1 共 4 生产者（2 既有 + 2 新增），exit 2 新增 ConfigError 一类
全局退出码约定 `0` 成功 / `1` 业务失败·optional-dep 缺失·启动期拒绝 / `2` 参数·配置错。退出码 `1` 在本提案落地后共 **4 个生产者**，归同一语义类（启动期拒绝 / backend·SDK 不可用）：
- ① mcp SDK 未安装（`ImportError`，**既有**，mcp.py:119）；
- ② `build_server` eager fail-closed 自检抛 `ToolPolicyViolation`（**既有**，mcp.py:139）；
- ③④ daemon-safe 探针的 `BackendDaemonUnsafe` / `NotImplementedError`（**本提案新增**——serve 当前根本未捕获这两个异常，须新增 catch；退出码对齐既有 daemon `_serve` 的 `_fail`→exit 1）。

退出码 `2`（**本提案新增**「`ConfigError`→2」一类）：管控依赖构造失败（如 `notifiers.yaml` 不可读）统一 **2**，对齐 serve 既有 `_load_settings_or_exit`（mcp.py:68）/ `cli/schedule.py` 的 `_fail_config`。**澄清**：早期草稿曾误写「不新增退出码 1 用途」——那是错的，③④ 确是本提案新增的 exit-1 生产者。准确表述是「exit 1 语义类不变（仍是启动拒绝），但本提案在该类下新增 ③④ 两个异常映射；配置错恒走 exit 2 与之区分」。

## 风险 / 权衡

- **[远程触发放大 backend 花费]** `run_schedule_now` 让远程 LLM 能引发 Hostlens 自身 backend 的 token 消费 → **缓解**：只能触发**已存在**的 schedule（远程 LLM 无法构造任意巡检）+ pipeline 既有 `agent.token_budget_*` 硬上限 + 较长 timeout 但有限。文档/`mcp_description` 明示该工具非免费。**残留风险（显式承认，接受）**：MCP `dispatch` / server `handle_call_tool` 对 `run_schedule_now` **无 per-tool 速率/并发限制**——远程 LLM 可在 token budget 内高频连发，每次触发一条完整 pipeline + 对主机的只读巡检，单次有界、N 次叠加成本无界。**为何接受**：stdio transport 信任边界下调用方是用户本机配置的 MCP host（非任意网络对端），非公网暴露；单次 token budget 已封顶。未来如需更强约束可在写期或独立提案加简单令牌桶 / in-flight 上限。
- **[per-call runner 构造开销 + 通道存在性校验]** 每次 `run_schedule_now` 经 `build_runner` 造一个 runner，其 `__init__` 会跑 `_validate_notify_channels`（`runner.py:206`）—— 即便本次抑制 notify，runner 仍会校验该 schedule 引用的 notify 通道在 `notifiers.yaml` 中存在；引用了不存在通道的 schedule 会在 `build_runner` raise `ConfigError`（handler 须捕获→结构化错误信封）。**这是有意的 fail-loud**：与 daemon/CLI 对同一 misconfig 的行为一致（不因「本次不发」而放过配置错），不是缺陷。→ **缓解**：仅最重的工具走此路，频次低；manifests 已在同调用内 fresh-load，复用传入。
  - **serve 启动期 eager 通道 secret 解析（启动前提，有意）**：`mcp serve` 在装配 `ManagementToolDeps` 时 eager 经 `load_channels` 展开各通道 `${ENV_VAR}`（闭包绑进 `build_runner` 的 `channels`，实现 D-2），故引用了**未设 env 的通道**会在 serve **启动期** raise `ConfigError(missing_env_var)` → **exit 2**——即便 surface 只读、`run_schedule_now` 抑制通知、`list_channels` 走不展开的 raw reader。`list_channels` 的「不经 `load_channels`」独立性是 **reader 级**（不展开 secret 进输出），**非 boot 级**（serve 仍在启动期校验完整 send 配置）。**为何接受**：与 daemon/CLI 对同一 misconfig 的 fail-fast 一致——一个能 `run_schedule_now` 的长驻 server 在启动期校验其完整 send 配置是合理的；解耦（lazy 通道构造）会改 D-2 的 DI 契约、属独立设计决策。运营者须在 serve 前导出通道 secret 或移除未用通道（见 `docs/integrations/mcp-tools.md` 运行前提）。
- **[fresh load 的 IO]** 每次查询重 walk 文件 → **缓解**：schedule/notifier 配置体量小（个位数文件），walk 开销 << 一次 dispatch 往返；查询工具短 timeout 兜底。
- **[7 工具 sensitive_output 漏声明]** → **缓解**：`build_server` eager `list_for_mcp` 自检在运行态前 fail-closed raise，既有防线天然覆盖；新增单测对 7 工具逐一断言 `sensitive_output is not None`。
- **[handler 与既有 CLI 行为漂移]** 薄适配层若与 `hostlens reports/schedule/notify` CLI 的查询语义不一致会困惑用户 → **缓解**：handler 直接复用 CLI 同款 store/loader 调用，crosscheck 测试对齐输出形状。

## Migration Plan

- 纯增量：新增工具 + 新装配函数 + runner 一个向后兼容 keyword 参数。无数据迁移、无 schema 破坏。
- `dispatch_notify` 默认 `True` → 既有 daemon / `schedule trigger` / cassette 全部行为不变。
- 回滚：移除 `register_mcp_management_tools` 调用即回到 M7 只读三件套；runner 的 `dispatch_notify` 参数无调用方传 `False` 时等价无变更。

## Open Questions（review 后已裁定）

- ~~`list_reports` 的 `target?` 缺省返回全部还是必填~~ → **已裁定必填**（D-7.2）：既有 `ReportStore.list_runs(target_id)` 不支持 all-targets，守「薄复用」故 `target` 必填。
- ~~`get_schedule_status` 的 `limit` 默认值与上限~~ → **已裁定** default 10 / max 100，钳制在 handler 侧（D-7.5），`mcp-management-tools` spec 固化。
- `run_schedule_now` 是否在 `mcp_description` 里要求远程 LLM「先 list_schedules 确认 name 存在再触发」？倾向是——降低无效触发。（实现期定文案，不阻塞 spec；handler 侧已强制前置检查未知 name → 结构化 not-found，见 D-7.4。）
