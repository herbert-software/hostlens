## 为什么

hostlens 现有**三套** env 读取方式，只有 pydantic `Settings` 真正从 `.env` 读（前缀 `HOSTLENS_`）：

1. **pydantic Settings**（`core/config.py`）—— `.env`（`env_file`）+ os.environ（`HOSTLENS_` 前缀）。
2. **`${VAR}` 占位解析**（`notifiers/config.py:85`、`targets/config.py:337`、`onboard.py:90/93`、`cli/notify.py:341`）—— 读**裸 `os.environ`**，任意变量名。
3. **Inspector secrets**（`inspectors/runner.py:514/626`、`recorder.py:259`）—— 读**裸 `os.environ[name]`**。

问题：pydantic-settings 读 `.env` 但**不把它导出到 `os.environ`**。所以把 `TELEGRAM_BOT_TOKEN` 之类写进 `.env` 对 ② ③ **不生效**——必须额外 `export`，配置方式五花八门。实战已踩到：在 ts.mac-mini 给调度配通知通道时，notifier 密钥放不进 `.env`，只能靠 daemon wrapper `source` 一个独立 `.notifier-secrets.env` 桥接进 os.environ。

## 变更内容

在 CLI entrypoint（`hostlens.cli` 根回调，所有子命令执行前）做**一次** `load_dotenv`（`python-dotenv`，`override=False`，从 cwd 的 `.env`，文件缺失静默跳过），把 `.env` 灌进 `os.environ`。之后：

- pydantic `Settings` 行为**不变**（仍从 env + `.env` 读；现 os.environ 含 `.env` 值，pydantic 优先级 `init > os.environ > .env file > default` 仍自洽，同值无冲突）。
- ② `${VAR}` 解析与 ③ inspector secrets **现也能从 `.env` 读**。
- 显式 `export` 仍作覆盖手段（`override=False` 保证已存在的 os.environ 值优先）。
- `.env` 成为**唯一** env 配置源——以后接新通道 / 新 inspector secret / 新 target 凭据，密钥统一放 `.env` 即可，不必再查"这个走 .env 还是走 export"。

**非目标**：不改读取 `XDG_DATA_HOME`（数据目录定位）、`os.environ.copy()`（传子进程 env）、`KUBERNETES_SERVICE_HOST`（in-cluster 探测）、doctor 的 `name in os.environ`（存在性检查）等平台级裸 `os.environ` 读的**代码逻辑**；不改任何 yaml 结构化配置；不引入新配置文件格式；不改 `Settings` 的 pydantic 加载契约本身。

> **澄清（非目标边界）**：`load_dotenv` **不做 key 过滤**——若用户在 `.env` 显式放某平台 key（如 `XDG_DATA_HOME=...`），它会被注入 `os.environ` 并被既有代码读到。这与「`.env` 单一来源」**一致、是预期行为**（且 `override=False` 下真实进程环境的同名变量仍优先）,**不**视为行为回退。即「非目标」指**不动这些 reader 的代码**,**非**「过滤掉 .env 里的平台 key」。文档应提醒用户:`.env` 里别误放平台 key,除非有意覆盖。

## 功能 (Capabilities)

### 新增功能

- `dotenv-env-bootstrap`: CLI 启动时把 cwd 的 `.env` 加载进 `os.environ`（`override=False`、缺失静默跳过、幂等），使所有 env-based 配置（pydantic Settings + `${VAR}` 占位解析 + inspector secrets）共享 `.env` 这一唯一来源；保留显式 `export` 作为覆盖。

### 修改功能

（无——这是新增的启动 bootstrap 步骤，不修改 `core-services` 的「`Settings` 从 env 与 .env 加载」既有契约；design.md 交代二者不冲突。）

## 影响

- **代码**：`hostlens.cli` 根回调新增一次 `load_dotenv` 调用（在 `load_settings()` / 任何子命令逻辑之前）。
- **依赖**：新增 `python-dotenv`（标准库无 `.env` 解析；pydantic-settings 的 dotenv 解析不导出 os.environ；`python-dotenv` 是最小且事实标准——值得这一个依赖）。
- **行为**：CLI 启动新增一次 `.env` → `os.environ` 加载（缺文件静默、override=False 不覆盖已有 env、对无 `.env` 的环境零影响）。
- **文档**：配置说明改为「所有密钥 / env 统一放 `.env`」；ts.mac-mini 部署的 daemon wrapper 里 `source .notifier-secrets.env` 桥接可简化为直接放 `.env`（那行 source 变冗余）。
- **测试**：新增 CLI 启动加载 `.env` 的行为测试（有 `.env` → os.environ 注入、缺 `.env` → 静默、override=False → 已有 env 不被覆盖）；现有测试需确认隔离 dev `.env`（参考既有 `_isolate_env` 模式，见 CLAUDE.md 红线）。
