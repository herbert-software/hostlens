## 上下文

M4（`add-scheduler`，archived 2026-06-04）的 daemon 优雅停机机制（design D-5）：SIGTERM → `scheduler.pause()` → `asyncio.wait(in-flight, timeout=grace)` → 超 grace 的 pending `task.cancel()` → 主协程 `gather` drain → 被硬切的 job 落 `Run(status=daemon_stopped)`（shield+drain 保证终态写不丢）。

`grace` 由 `SchedulerRunner.__init__(grace_seconds: float = _GRACE_SECONDS)` 注入（`scheduler/runner.py`），`_GRACE_SECONDS = 30.0` 固定常量；daemon/run CLI（`cli/schedule.py:_build_runner`）当前**用该默认、未接 `Settings`**。

既有事实（Codex review 核实）：
- 单次 LLM API timeout `_MESSAGES_CREATE_TIMEOUT = 60.0`（`agent/loop.py`），重试 3 次退避 1/4/16s → 单次 API 最坏 ~261s。
- 一次**单 turn、无重试**的 job（1 次 LLM 60s + 1 次 inspector 30s + drain）≈ 90–100s。30s grace < 单次 LLM timeout，必切断正在等模型的 job。⚠️ 注意 90–100s 是**最乐观基线**：多 turn、或命中 503 重试退避的 job 会显著更长（单次 API 最坏即 ~261s）——见「风险/权衡」对 120s 覆盖率的诚实评估。
- `Settings`（`core/config.py`）已是 `BaseSettings`，`env_prefix="HOSTLENS_"` + `env_nested_delimiter="__"`。两类嵌套命名空间形态**不同**：`ssh: SshSettings = Field(default_factory=SshSettings)`（恒非 None），而 `agent: AgentSettings | None = None`（optional，整块可缺省——M0/M1 无 LLM 配置时为 None）。另有既有顶层字段 `daemon_mode: bool`（env 单下划线 `HOSTLENS_DAEMON_MODE`，daemon 安全门 seam）。
- `runner` 的注入形参已存在 —— 本变更**不改 runner**，只接通配置→CLI→runner 的传值。

## 目标 / 非目标

**目标：**
- shutdown grace 默认 `30s → 120s`，修掉「grace 比单次 LLM timeout(60s) 还小」的明显缺陷（覆盖单 turn、无重试基线；多 turn/重试 job 靠可配性上调，见风险段）。
- grace 可经 `Settings.daemon.shutdown_grace_seconds`（env `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`，范围 1–600）配置，运维按 job 时长自调。
- 非法值经既有 `load_settings()` fail-loud（`ConfigError`），不静默回退。

**非目标：**
- CLI `--grace` 旗标（daemon 级参数走 env/config，12-factor）。
- per-manifest grace（grace 是 daemon 级、非单 schedule 级）。
- `misfire_grace_time` 可配置化（独立概念，不动）。
- daemon 内存监控 / HTTP healthz（OPERABILITY §5.2 规划，无关）。
- 覆盖理论最坏 job 时长（全重试 × 多 turn）——那是 SIGKILL/超时边界，本就接受。

## 决策

### D-1：新增 `DaemonSettings` 子命名空间，采用 `default_factory`（同 `ssh`、非 `agent` 的 Optional）
`core/config.py` 新增 `class DaemonSettings(BaseModel)`，字段 `shutdown_grace_seconds: float = Field(default=120.0, ge=1, le=600)`；`Settings` 加 `daemon: DaemonSettings = Field(default_factory=DaemonSettings)`。env 覆盖自动为 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`（既有 `env_nested_delimiter="__"`）。

**为何 `default_factory` 而非 Optional**：grace 默认值必须**恒可读**（`settings.daemon.shutdown_grace_seconds` 在停机路径不能 NPE），故选 `default_factory`（与 `ssh: SshSettings = Field(default_factory=SshSettings)` 同构）。**注意不要照搬 `agent: AgentSettings | None = None`**——那是「optional LLM 配置块、可整块缺省」的语义，daemon 块**不应缺省**。

**与既有 `daemon_mode` 顶层字段并存**：`Settings.daemon_mode: bool`（env `HOSTLENS_DAEMON_MODE`，单下划线，daemon 安全门 seam，D-12）与新 `daemon` namespace（env `HOSTLENS_DAEMON__...`，双下划线）**不冲突**——`env_nested_delimiter="__"` 按双下划线路由 namespace、单下划线映射平铺字段。本变更**不**把 `daemon_mode` 迁进 namespace（非本变更范围；其签名被 `is_daemon_mode(settings)` 锁定）。

**否决** 顶层 `Settings.shutdown_grace_seconds` 平铺字段——未来 daemon 还会有别的参数（如 OPERABILITY 规划的 memory_limit_mb），先立 namespace。**否决** contextvar / 全局——`Settings` 注入是项目既有依赖注入风格。

> 范围 `1–600s`：下界 1 防 0/负值（`asyncio.wait(timeout=0)` 会立刻超时切断所有 in-flight，等于无优雅停机）；上界 600 防停机被无限拖（运维 Ctrl-C 后等 10 分钟已是上限）。

### D-2：daemon/run 启动把配置传入 runner，runner 不改
`cli/schedule.py:_build_runner` 构造 `SchedulerRunner(..., grace_seconds=settings.daemon.shutdown_grace_seconds)`。**`runner._GRACE_SECONDS = 30.0` 常量保留不动**，作为「直接构造 runner（库内/测试）不传 grace 时」的 fallback 默认；**生产唯一真相是 `DaemonSettings.shutdown_grace_seconds = 120`**——daemon/run 路径强制经 `_build_runner` 注入它、不依赖构造器默认。runner 构造器默认处加一行注释指向 settings（说明 30s 仅为库内 fallback、生产默认在 `DaemonSettings`），消除「两个默认真相源」误解。**最小改动**：runner 注入接口 M4 已就位，本变更只是「把一直用默认的地方改成读 settings」。

### D-3：非法值走既有 `load_settings()` ConfigError 路径，不另造校验
`Field(ge=1, le=600)` 的范围校验由 Pydantic 完成；`load_settings()`（core-services 契约）已把 `ValidationError` 转 `ConfigError`（含字段名 + 期望 + 实际值，非敏感字段保留实际值）。daemon CLI 既有 `_load_settings_or_exit` → `_fail_config`（exit 2）已覆盖。**不**为 grace 写专门的校验分支。

### D-4：CLI 不加 `--grace` 旗标
daemon 级运行参数走 env/config 更符合 12-factor，且避免在 `daemon` 与 `run` 两条命令重复一个选项 + 与 settings 的优先级歧义。`schedule daemon` 现仅有 `--log-file`，保持精简。

## 风险 / 权衡

- [默认值 30→120 是行为变更] → 方向更保守（更少误杀 in-flight job），对未配置用户是「停机时多等至多一个 job 的剩余时长」，无破坏性；既有 daemon_stopped 用例**本就显式注入极小 grace**（如 0.05s，见 tests/cli/test_schedule.py 与 tests/scheduler/test_runner.py 各自的本地 `_build_runner`），**不依赖 30s 默认、无需改动**，故默认值变化不影响它们。
- **[120s 覆盖率：诚实评估，不是「大多数 job 优雅完成」的保证]** → 120s 只覆盖「单 turn、无重试」的乐观基线（~90–100s）。**多 turn 的 Agent loop、或命中 backend 503 重试退避（1/4/16s，单次 API 最坏 ~261s）的 job 仍可能 >120s 被切成 `daemon_stopped`**——这正是提案要缓解的失败模式，120s 缓解但**不消除**它。这是本变更的核心权衡：grace 无法设到覆盖理论最坏（那是分钟级、会让停机无限拖），所以 (a) 默认取 120s「比 30s 显著更好」的折中、(b) **引入可配性让运维按自己 job 的 turn 数/重试特征上调**才是真正的逃生舱。超 grace 的 job 仍按 D-5 落 `daemon_stopped`（语义不变）。spec 措辞已避免把「120s 覆盖高概率路径」写成无限定的契约断言。
- [新 `daemon` namespace 目前只一个字段] → 可接受的前瞻：为 daemon 级参数立命名空间，避免未来 memory_limit_mb 等再平铺；成本仅一个空壳 BaseModel。
- [grace 与 misfire_grace_time 概念混淆] → 文档（OPERABILITY §5.3 vs §6.1）已分别澄清；两者不共享代码。
