## 为什么

M4 daemon 优雅停机的 shutdown grace 固定为 `_GRACE_SECONDS = 30.0`（`scheduler/runner.py`），但**单次 LLM API 调用的 timeout 就是 60s**（`agent/loop.py:_MESSAGES_CREATE_TIMEOUT`），一次定时巡检 job = 一整个 Agent loop（Planner + Diagnostician，多次 API 调用 + Inspector 采集），「正常但慢」路径 ≈ 90–100s。**30s grace 比单次 LLM timeout 还小**：daemon 收 SIGTERM 时，正在等模型响应的 job 几乎必被 `task.cancel()` 切断、落成虚假的 `Run(status=daemon_stopped)`，而它本可在再多等一会儿后自然完成。这让「优雅停机」对真实工作负载形同虚设，也污染 `schedule status`/doctor 的状态分布。

此外当前 grace 是**固定常量、不可配**（`runner` 构造器 `grace_seconds` 可注入但 daemon CLI 用默认、未接 `Settings`），运维无法按自己的 job 时长调整。docs/OPERABILITY.md §5.3 原定 `120s + 可配`，与实现存在偏差。

## 变更内容

- **提高 shutdown grace 默认值 `30s → 120s`**（行为变更）：修掉「grace 比单次 LLM timeout(60s) 还小、必切断正在等模型的 job」这个明显缺陷。**诚实边界**：120s 覆盖的是「单 turn、无重试、单次 API + 单 inspector + drain ≈ 90–100s」这个**常见基线**；**它不保证覆盖所有「正常但慢」的 job**——多 turn、或命中 backend 503 重试退避（1/4/16s，单次 API 最坏 ~261s）的 job 仍可能 >120s 被切成 `daemon_stopped`。120s 是「比 30s 显著更好的默认」，**不是「大多数 job 都能优雅完成」的保证**；真正的逃生舱是本变更引入的**可配性**（运维按自己 job 的 turn 数/重试特征上调）。停机响应上界由该配置控制（默认 2 分钟）。
- **新增 `DaemonSettings.shutdown_grace_seconds` 配置项**：`float = 120.0`，`Field(ge=1, le=600)`；挂在 `Settings.daemon: DaemonSettings = Field(default_factory=DaemonSettings)`——**与 `ssh: SshSettings` 同构（`default_factory`，恒非 None）**，**不同于** `agent: AgentSettings | None = None`（那是 optional LLM 块、可整块缺省）；daemon 块必须恒可读（停机 grace 不能 NPE），故用 `default_factory`。env 覆盖 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`（既有 `env_nested_delimiter="__"`；注意与既有顶层 `daemon_mode: bool`（env 单下划线 `HOSTLENS_DAEMON_MODE`）不冲突——双 `__` 路由进 namespace、单 `_` 映射平铺字段）。
- **daemon / run 启动把配置值传给 runner**：`cli/schedule.py` 的 `_build_runner` 用 `settings.daemon.shutdown_grace_seconds` 构造 `SchedulerRunner(grace_seconds=...)`，替代当前的 `_GRACE_SECONDS` 默认。`runner.py` 的注入形参已存在、**无需改 runner**。
- **不引入 CLI `--grace` 选项**：daemon 级参数走 env/config（12-factor），不在 `daemon`/`run` 两条路径重复一个选项。
- 同步 docs/OPERABILITY.md §5.3 的「待对齐」注记为已落地（默认 120s + 可配 + env 名）。

## 功能 (Capabilities)

### 新增功能

（无新功能；本变更是对既有 daemon 停机行为的修改 + 一个由既有 `core-services` Settings 加载机制覆盖的新配置字段。）

### 修改功能

- `schedule-cli-command`: daemon SIGTERM 优雅停机需求新增「shutdown grace 默认 120s、且可经 `settings.daemon.shutdown_grace_seconds`（env `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`，范围 1–600s）配置」的契约。

## 影响

- Affected specs: `schedule-cli-command`（MODIFIED —— grace 默认值 + 可配性）。`core-services` Settings 加载机制不变（新 `daemon` namespace 字段由其既有 env/`.env`/ConfigError 契约覆盖，无需 delta）。
- Affected code:
  - `src/hostlens/core/config.py`（改）—— 新增 `DaemonSettings(BaseModel)` + `Settings.daemon: DaemonSettings = Field(default_factory=DaemonSettings)`
  - `src/hostlens/cli/schedule.py`（改）—— `_build_runner` 传 `grace_seconds=settings.daemon.shutdown_grace_seconds`
  - 复用（**不改 runner**）：`scheduler/runner.py` 的 `grace_seconds` 构造器形参已存在。**`_GRACE_SECONDS = 30.0` 常量保留不动**作为「直接构造 runner（库内/测试）时的 fallback 默认」；**生产唯一真相是 `DaemonSettings.shutdown_grace_seconds = 120`**——daemon/run 路径**强制**经 `_build_runner` 注入它，不依赖构造器默认。runner 的构造器默认处加一行注释指向 settings，避免读者误以为 30s 仍是生产默认（两个默认值各司其职、不矛盾）。
  - `docs/OPERABILITY.md §5.3`（改）—— 移除「待对齐」注记，记默认 120s + 可配
- 对外契约影响：新增 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS` env / `daemon.shutdown_grace_seconds` 配置项；daemon 停机 grace 默认值 30→120s（行为变更，但更保守——更少误杀，不破坏既有 API/schema）。
- Migration: 无配置迁移；未设该 env 的用户自动从 30s 升到 120s 默认（更宽松的优雅停机，无破坏性）。

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS` 非法（非数 / 超范围 1–600） | 启动加载 | `load_settings()` 既有路径 raise `ConfigError`（含字段名 + 期望范围 + 实际值），daemon fail-loud 不启动（沿用 core-services 契约） |
| job 耗时 > grace（即便 120s） | SIGTERM 后超 grace | 沿用 D-5：`task.cancel()` → shield+drain 落 `Run(status=daemon_stopped)`（语义不变，只是更少触发） |
| 用户显式设极小 grace（如 1s） | 配置合法但激进 | 行为可预测——大多数 in-flight job 会落 `daemon_stopped`（用户自选权衡，范围下界 1s 防 0/负值） |

### Operational Limits

grace 范围 `1–600s`（`Field(ge=1, le=600)`）：下界防 0/负值，上界防停机被无限拖。与 `misfire_grace_time`（cron 300s / interval `max(30, interval//2)`，控制触发补跑）是独立概念、不共享逻辑。`max_instances=1` 在 `scheduler.pause()` 后无 overlap。SIGKILL（-9）不留记录的已知限制与 grace 大小无关。

### Security & Secrets

无新增凭据面。`shutdown_grace_seconds` 是非敏感数值字段（不匹配 `(?i)(key|token|secret|password|credential)`），ConfigError 中保留实际值便于调试（符合 core-services 脱敏规则）。

### Cost / Quota Impact

无。grace 仅影响停机等待时长，不改变调用量。更大的 grace 意味着停机时可能多等一个正在跑的 job 自然完成（最多多花其剩余时长），但避免了被切断后下次触发重跑的潜在浪费。

### Demo Path

```bash
# 默认 120s（未设 env）
hostlens schedule daemon            # 启动日志/行为：grace=120s

# env 覆盖
HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=60 hostlens schedule daemon   # grace=60s

# 非法值 fail-loud
HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=0 hostlens schedule daemon    # exit 2, ConfigError 指出范围 1–600
```

CI 验收：`pytest tests/ -m 'not live'` 全绿（DaemonSettings 默认/范围校验 / env 覆盖 / `_build_runner` 传值 / 既有 SIGTERM grace 测试改用注入的小 grace 仍过）；`openspec-cn validate add-configurable-shutdown-grace` 通过。

## 非目标

- CLI `--grace` 选项（走 env/config，不加命令行旗标）。
- per-manifest 的 grace（grace 是 daemon 级、非单 schedule 级）。
- `misfire_grace_time` 的可配置化（独立概念，本变更不动）。
- daemon 内存监控 / HTTP healthz 端点（OPERABILITY §5.2 规划项，与本变更无关）。
