## 新增需求

### 需求:CLI 启动必须把 `.env` 加载进 `os.environ` 以统一 env 配置源

`hostlens` CLI 根回调（`@app.callback()`，**先于任何子命令 body 执行**，故先于子命令 body 内的 `load_settings()`）**必须**把 **cwd 的 `.env`** 加载进 `os.environ`，使所有 env-based 配置（pydantic `Settings`、`${VAR}` 占位解析、inspector secrets）共享 `.env` 这一唯一来源。**必须**经 `python-dotenv` 的 `dotenv_values(dotenv_path=Path(".env"))` 读取并对每个键 `os.environ.setdefault(key, value)`——**不用** `load_dotenv`：`dotenv_values` 以「文件优先」解析 `${VAR}` 插值，与 pydantic `Settings` 同序，保证 Settings 取值不变；且对 `PYTHON_DOTENV_DISABLED` 环境变量免疫（该变量会让 `load_dotenv` 静默变 no-op）。`setdefault` 保证已存在的 `os.environ` / 显式 `export` 优先（`.env` 只填补缺失项）。`.env` 缺失（`dotenv_values` 返回空 dict）、不可读（抛 `OSError`，由 `try/except OSError` 捕获）、或是目录（python-dotenv ≥1.0.1 返回空 dict；旧版抛 `OSError` 同样被捕获）**必须**静默跳过（不抛异常、不打印路径或缺失提示）；**禁止**向上递归查找父目录的 `.env`——**必须**通过显式 `dotenv_path=Path(".env")`、**不调用** `find_dotenv`（与 `Settings(env_file=".env")` 的 cwd 语义保持一致）；加载过程**禁止**打印任何变量值（密钥不入日志）。

#### 场景:`.env` 中的密钥可被 `${VAR}` 解析读到
- **当** cwd 存在 `.env` 含 `TELEGRAM_BOT_TOKEN=...`，`notifiers.yaml` 的 telegram 通道写 `bot_token: ${TELEGRAM_BOT_TOKEN}`，运行加载通道的命令（如 `notify channels`），且该变量未经 `export`
- **那么** `${TELEGRAM_BOT_TOKEN}` **必须**解析为 `.env` 中的值、通道加载成功，无需额外 `export`

#### 场景:显式 export 覆盖 `.env`（override=False）
- **当** `os.environ["X"]="from_export"` 已设置，且 cwd 的 `.env` 含 `X=from_dotenv`
- **那么** 加载后 `os.environ["X"]` **必须**仍为 `"from_export"`（已存在值不被 `.env` 覆盖）

#### 场景:无 `.env` 静默零影响
- **当** cwd 不存在 `.env`，运行任一 CLI 命令
- **那么** **禁止**抛异常、**禁止**打印 `.env` 路径或缺失提示；命令照常执行（env 仅来自真实 `os.environ`）

#### 场景:不可读 / 目录 `.env` 静默跳过（不崩 CLI）
- **当** cwd 存在 `.env` 但不可读（权限）或是一个目录，运行任一会经根回调的 CLI 命令（如 `hostlens doctor`）
- **那么** 加载**必须**静默跳过（等同缺文件）：不可读的 `.env` 让 `dotenv_values` 抛 `OSError`、由 `try/except OSError` 捕获；目录在 python-dotenv ≥1.0.1 直接返回空 dict（旧版抛 `OSError` 同样被捕获）。**禁止**抛异常 / 打印 traceback；命令照常执行（env 仅来自真实 `os.environ`）

#### 场景:`PYTHON_DOTENV_DISABLED` 不得使 bootstrap 失效
- **当** 环境设置了 `PYTHON_DOTENV_DISABLED=1` 且 cwd 的 `.env` 含 `HOSTLENS_LOG_MODE=dev`
- **那么** 加载后 `os.environ["HOSTLENS_LOG_MODE"]` **必须**为 `"dev"`（用 `dotenv_values` 而非 `load_dotenv`，后者会因该变量静默 no-op）

#### 场景:根回调的 cwd 副作用扩大测试隔离义务（CI 红防线）
- **当** 测试在仓库根（含开发用 `.env`，写有 `HOSTLENS_BACKEND__TYPE` / secret 等真值）通过 `CliRunner` 或直接调用 exercise CLI 根回调
- **那么** 根回调的 `.env` 加载（`dotenv_values` + `setdefault`）会把该 `.env` 注入 `os.environ`、污染本测试 env——本变更把「需隔离 dev `.env` 的测试集」从「仅 exercise `load_settings()` 者」**扩大**到「任何 exercise 根回调者」。故契约要求:任何 exercise 根回调的测试**必须**隔离 cwd（`chdir` 到无 `.env` 的 tmp）并按需 `delenv` 清掉 `HOSTLENS_*` / secret 占位变量，使「干净 CI（无 `.env`）」与「本地仓库根（有 dev `.env`）」行为一致;**禁止**依赖运行目录恰好无 `.env`（否则本地绿、干净 CI 红或反之）（注:经实测，仓根 dev `.env` 当前仅含 `HOSTLENS_*` 键，pydantic `Settings(env_file=".env")` **本变更前已**读取它们，故本变更对这些键**无新增污染**；新增污染面仅限非 `HOSTLENS_` 前缀变量，当前 dev `.env` 无此类键。对既有未隔离测试补 `tests/cli/conftest.py` autouse 隔离 fixture 是前向稳健性的**独立后续清理**，不阻塞本变更。）

#### 场景:Settings 取值不因加载改变
- **当** `.env` 含 `HOSTLENS_LOG_MODE=dev` 且无对应 `export`
- **那么** `load_settings()` **必须**仍得到 `log_mode="dev"`（取值结果与加载前一致，仅命中来源从 `.env file` 层前移到 `os.environ` 层）
