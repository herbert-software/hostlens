## 上下文

`core-services` 的 `Settings`（`pydantic_settings.BaseSettings`，`env_file=".env"`、`env_prefix="HOSTLENS_"`）从 `.env` 读 backend / agent / 路径等强类型字段。但 pydantic-settings 读 `.env` 的实现是**直接解析文件喂给模型**，**不把 `.env` 写进 `os.environ`**。

而项目里另有两类配置走**裸 `os.environ`**：

- **`${VAR}` 占位解析**：`notifiers/config.py:_expand_value`（`os.environ.get`）、`targets/config.py:337`、`onboard.py:90/93`、`cli/notify.py:341` —— notifiers.yaml / targets.yaml 的密钥与凭据。
- **Inspector secrets**：`inspectors/runner.py:514/626`、`recorder.py:259` —— `os.environ[name]`。

二者读 `os.environ`，看不到 `.env`。实测后果（ts.mac-mini 部署通知通道）：notifier 密钥放 `.env` 不生效，只能靠 daemon wrapper `source` 一个独立 secrets 文件桥接进 `os.environ`。配置方式分裂。

## 目标 / 非目标

**目标：**

- CLI 启动时把 cwd 的 `.env` 加载进 `os.environ`，使 `Settings` + `${VAR}` 解析 + inspector secrets 共享 `.env` 唯一来源。
- 保留显式 `export`（已存在的 `os.environ`）作为覆盖手段。
- 对无 `.env` 的环境零影响（缺文件静默）。

**非目标：**

- 不改 `Settings` 的 pydantic 加载契约本身（仍 `env_file=".env"`、`HOSTLENS_` 前缀）。
- 不改平台级 / 非应用配置的裸 `os.environ` 读：`XDG_DATA_HOME`、`os.environ.copy()`（子进程 env）、`KUBERNETES_SERVICE_HOST`、doctor 的 `name in os.environ` 存在性检查。
- 不改 yaml 结构化配置、不引入新配置文件格式、不做 `.env` 向上递归查找（与 `Settings` 的 cwd 语义保持一致）。

## 决策

1. **加载点 = `hostlens.cli` 根 `@app.callback()`**（所有子命令 body 之前、`load_settings()` 之前）。一处加载、所有命令受益；与「CLI 是唯一应用入口」一致。
2. **`dotenv_values(dotenv_path=Path(".env"))` + `os.environ.setdefault`**（`python-dotenv`，不用 `load_dotenv`）：
   - **cwd-relative `.env`**，与 `Settings(env_file=".env")` 的 cwd 语义一致（不向上递归 `find_dotenv`，避免「父目录的 .env 意外生效」这类 `Settings` 同款不一致）。
   - **`setdefault` ≡ override=False**：已存在的 `os.environ`（显式 `export`）**优先**，`.env` 只填补缺失项 —— `export` 仍是覆盖手段。
   - **不用 `load_dotenv`**：① `load_dotenv(override=False)` 以 os.environ 优先解析 `${VAR}` 插值，与 pydantic `dotenv_values`（文件优先）不同序，会让插值型 Settings 字段取值漂移；`dotenv_values` 同序，保证决策 4 的取值不变性。② `load_dotenv` 在 `PYTHON_DOTENV_DISABLED` 置位时静默变 no-op，`dotenv_values` 不受影响。
   - **缺失 / 不可读 / 目录静默**：缺文件 `dotenv_values` 返回空 dict；不可读抛 `OSError`、目录在 python-dotenv ≥1.0.1 返回空 dict（旧版抛 `OSError`），均被 `try/except OSError` 捕获静默跳过——否则一个权限错的 `.env` 会让 `hostlens doctor` 等所有走根回调的命令崩（`--help` 短路在回调前，恰好不崩）。无 `.env` 的环境零影响。
3. **依赖 `python-dotenv`**：标准库无 `.env` 解析；pydantic-settings 的 dotenv 解析不导出 `os.environ`（正是本问题根因），无法复用；`python-dotenv` 是最小且事实标准。
4. **`Settings` 行为不变性论证**：pydantic-settings 取值优先级 `init > os.environ > .env file > default`。`dotenv_values` + `setdefault` 后 `os.environ` 含 `.env` 值，`Settings` 从 `os.environ`（而非 `.env file` 层）取到**同一个值** —— 取值结果不变，仅命中层级前移。关键：`dotenv_values` 以「文件优先」解析 `${VAR}` 插值，与 pydantic 同序，故连插值型字段也取到同一值；若改用 `load_dotenv(override=False)`（os.environ 优先插值）反而会破坏此不变性。`Settings` 自身的 `env_file=".env"` 保留（对无 export 的字段是冗余但无害的二次来源）。

## 风险 / 权衡

- **cwd 依赖**：`dotenv_values(Path(".env"))` 与 `Settings` 一样 cwd-relative。从无 `.env` 的目录运行 hostlens 则不加载 —— 与现状一致（`Settings` 本就如此），daemon 从 `~/hostlens`（含 `.env`）运行。文档需明确「从含 `.env` 的目录运行，或用 export」。
- **`.env` 落盘密钥**：`.env` 现已存密钥（`Settings` 读它），本变更不新增暴露面；权限仍由用户维护（建议 0600）。
- **override=False 语义**：若同一变量既 `export` 又在 `.env`，`export` 胜。这是有意的覆盖语义，须在文档点明（防「改了 .env 没生效，其实被 export 覆盖了」的困惑）。
- **测试隔离**：CLI 测试若在含 dev `.env` 的仓根运行，新加载会注入 dev 值污染断言 —— 复用既有 `_isolate_env`（chdir tmp + delenv）模式（CLAUDE.md 红线 [[project_tests_must_isolate_dev_env_or_ci_red]]）。
