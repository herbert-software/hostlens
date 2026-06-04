## 1. DaemonSettings 配置项（design D-1/D-3，spec: schedule-cli-command）

- [x] 1.1 `src/hostlens/core/config.py`：新增 `class DaemonSettings(BaseModel)`，字段 `shutdown_grace_seconds: float = Field(default=120.0, ge=1, le=600)`（与 `SshSettings`/`AgentSettings` 同风格）
- [x] 1.2 `Settings` 加 `daemon: DaemonSettings = Field(default_factory=DaemonSettings)`，env 覆盖经既有 `env_nested_delimiter="__"` 自动为 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`
- [x] 1.3 单测（`tests/core/test_config.py` 就近）：默认 `settings.daemon.shutdown_grace_seconds == 120.0` / env 覆盖 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=60` → 60.0 / 非法值（0、-1、601、非数）经 `load_settings()` raise `ConfigError`（含字段名 + 范围）；验收 `pytest tests/core/test_config.py -q`

## 2. CLI 接通配置 → runner（design D-2/D-4）

- [x] 2.1 `src/hostlens/cli/schedule.py`：`_build_runner` 用 `grace_seconds=settings.daemon.shutdown_grace_seconds` 构造 `SchedulerRunner`，替代当前依赖 runner 的 `_GRACE_SECONDS` 默认（daemon/run 两条路径都经 `_build_runner`，一处改动覆盖）
- [x] 2.2 不加 CLI `--grace` 旗标（保持 `daemon` 仅 `--log-file`）；`runner.py` **仅在 `__init__` 的 `grace_seconds` 默认（`_GRACE_SECONDS`）附近加一行注释**说明「30s 仅为直接构造 runner 的库内 fallback，生产唯一真相是 `DaemonSettings.shutdown_grace_seconds=120`，daemon 路径经 `_build_runner` 注入」——除该注释外不改 runner 逻辑
- [x] 2.3 单测（`tests/cli/test_schedule.py`）：验证「配置值传入 runner」——spy **生产** `hostlens.cli.schedule.SchedulerRunner` 构造、捕获其 `grace_seconds` 实参（**不要** patch `_build_runner`——它签名里没有 `grace_seconds`、捕不到真实传参；且需与 test_schedule.py 内同名 test-local helper 区分），并 patch `_serve_loop`/调度循环让其即刻返回（daemon loop 会阻塞至信号，**不能**靠 `invoke` 自然返回断值——必须 patch 让其不阻塞）：默认 → runner 收 grace=120.0；设 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=60` → runner 收 60.0；非法 grace（0/601/非数）→ 命令 fail-loud exit 2（这条可直接 `invoke` 断退出码，load_settings 阶段就挂、不进 loop）。**既有 daemon_stopped 用例无需改动**——它们已用各自本地 `_build_runner` 显式注入极小 grace（如 0.05s）、不依赖 30s 默认，默认值变化不影响。验收 `pytest tests/cli/test_schedule.py -q`

## 3. 文档与收尾

- [x] 3.1 `docs/OPERABILITY.md §5.3`：移除「grace 取值待对齐」注记，记为「默认 120s，可经 `daemon.shutdown_grace_seconds`（env `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`，1–600s）配置」
- [x] 3.2 `mypy --strict src/hostlens/core/config.py src/hostlens/cli/schedule.py` 通过（无 `Any` 泄漏）
- [x] 3.3 全量 `pytest tests/ -m 'not live'` 绿
- [x] 3.4 `openspec-cn validate add-configurable-shutdown-grace` 通过
- [ ] 3.5 PR 前对抗性 review（CLAUDE.md §5.3；含运行时行为变更——默认值 30→120 + 新配置项，应跑）
