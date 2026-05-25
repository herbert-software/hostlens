## 为什么

M1 第二块基石。`add-execution-target-abstraction` 落地后，Hostlens 已经能"在 Local / SSH target 上跑 shell 命令拿 ExecResult"——但**还没有 Inspector**：没有 manifest 加载器、没有 finding 求值引擎、没有 runner，`InspectorRegistry` 在 `hostlens.tools.base` 里还是只暴露 `list_summaries()` 的 stub Protocol；`list_inspectors` / `run_inspector` 这两个 M2 首批 ToolSpec handler 拿到的是空数据。M2 手写 Agent loop 上线时必须有真实 Inspector 数据可供 Planner 选择，否则 demo path 跑不通。

CLAUDE.md §4.2 已把 Inspector 定位钉死："**Inspector 是 SOT，Agent 是调度者**——Agent 只决定调哪几个 Inspector + 按什么顺序合并结果，**不在 prompt 里写死巡检步骤**"；docs/ARCHITECTURE.md §4 已把 manifest 字段集、Finding DSL 求值语义、shell 注入防御规则、安全边界全部锁定。本提案的任务是把这些契约从架构文档搬进 spec 与 `src/hostlens/inspectors/`，并交付 **manifest 加载器 + 4 个 parse format + Finding DSL 引擎 + Inspector Runner + 2 个内置 inspector（验证管线）+ `hostlens inspectors list/show` CLI**，同时**消除 `InspectorRegistry` stub**，把 `list_inspectors` / `run_inspector` ToolSpec handler 接通真实 registry。

完成后 M1 退出条件中"新增简单检查项 = 加一个 YAML 文件，零 Python 代码"这一条对管线侧成立；剩下的 `hostlens inspect <target> --inspector <name>` 端到端命令依赖 Report 数据模型 + markdown 渲染，留给下一提案 `add-report-data-model`（M1.6 + M1.7 收尾）。

## 变更内容

**新增（Inspector manifest 与 schema）：**

- `hostlens.inspectors.schema.InspectorManifest` Pydantic 模型：完整字段集严格对齐 docs/ARCHITECTURE.md §4 manifest 字段速查表的 M1 子集（含 `name` / `version` / `description` / `tags` / `targets` / `requires_capabilities` / `requires_binaries` / `requires_files` / `privilege` / `parameters` / `secrets` / `collect.command` / `collect.timeout_seconds` / `parse.format` / `parse.columns` / `output_schema` / `findings`）；**M1 不实现**的字段（`collect.sampling_window` / `artifacts`）在 schema 用 `extra="forbid"` 直接拒收（防止用户写了误以为生效）
- `hostlens.inspectors.schema.FindingRule` Pydantic 模型：四字段 DSL `for_each` / `when` / `severity` / `message`；loader 校验聚合模式 `message` 不引用 `for_each` 变量（按 ARCHITECTURE §4 Finding DSL 求值语义）
- `hostlens.inspectors.schema.ParseFormat` Literal：M1 范围**恰好** `{"raw", "table", "json", "kv"}` 四种（`sql_result` 留给 M6 PostgreSQL Inspector；防止"声明了但不实现"陷阱）
- `hostlens.inspectors.schema.Privilege` Literal：`{"none", "sudo", "root"}`；M1 runner 对 `privilege != "none"` 的 Inspector 在未 `--allow-privileged` opt-in 时**直接拒绝运行**

**新增（manifest loader）：**

- `hostlens.inspectors.loader.load_manifest(path: Path) -> InspectorManifest`：YAML 解析 + Pydantic 校验 + 自定义 post-validation（见下）
- **Shell 注入静态校验**（核心安全保证；对齐 ARCHITECTURE §4 命令渲染安全规则）：loader 必须 reject 以下 manifest（**加载时**而非运行时）：
  - `parameters` schema 中 `type: string` 字段未声明 `pattern` 或 `enum`
  - `collect.command` 模板中 string parameter 未走 `| sh` filter（除非显式 `unsafe_raw: true` 且 manifest level 注释说明理由）
  - `collect.command` 模板中 `array` parameter（`items.type == "string"`）未走 `| map('sh') | join(<delim>)` filter chain 或单元素被解引用（如 `endpoints[0]`）后未走 `| sh`——数组 string 元素同样是 shell 注入向量，**与单 string 同等对待**
  - `secrets` 名字出现在 `collect.command` 模板插值位置（必须通过 env var `$VAR_NAME` 引用，不能 Jinja 插值）
  - `requires_files` / `requires_binaries` 在 manifest 字段层校验严格字符集（path 必须匹配 `^/[A-Za-z0-9._/-]+$`；binary 名匹配 `^[a-zA-Z0-9._-]+$`），**且** runner 在 preflight 探测时仍用 `shlex.quote` 包路径再做 `[ -r ... ]` —— 防御纵深，loader 字符集是第一道闸 + runner quote 是第二道
- **Finding DSL 静态校验**：loader 必须 reject `when` 表达式语法错误、聚合模式 message 引用 `for_each` 变量、`severity` 不在 `{info, warning, critical}` 三值之内的 manifest

**新增（InspectorRegistry）：**

- `hostlens.inspectors.registry.InspectorRegistry`：按 `name` 索引 `InspectorManifest`；API 为 `register(manifest)` / `get(name)` / `names()` / `list()` / `list_summaries()`；name 冲突 raise `InspectorError(kind="duplicate_inspector", inspector=name)`
- `hostlens.inspectors.registry.build_registry_from_search_paths(paths: list[Path], *, settings: Settings) -> RegistryBuildResult`（返回 `RegistryBuildResult(registry, errors)` 双值——`registry` 是装配后的 InspectorRegistry；`errors` 是用户路径下文件级加载错误的清单）：装配工厂；扫 builtin 目录（`src/hostlens/inspectors/builtin/**/*.yaml`）+ 用户目录（`~/.config/hostlens/inspectors/**/*.yaml`，由 `Settings.inspectors_search_paths` 配置）；**用户 manifest 同名 builtin 必须 raise duplicate_inspector（fatal，security 关键不进 errors）**；**用户路径单文件加载错误 collect 到 `errors` 列表**（**不**阻塞其他 inspector 加载，让 CLI / doctor 决定 exit code）；**builtin 路径文件级错误仍 raise**（仓库自带 bug 必须立即暴露）

**新增（Inspector runner + Finding DSL 引擎）：**

- `hostlens.inspectors.runner.InspectorRunner`：核心 API `async run(manifest, target, parameters, *, allow_privileged=False) -> InspectorResult`
- 求值顺序固定为：preflight（`requires_capabilities` / `requires_binaries` / `requires_files` / `privilege`）→ render command（Jinja2 + sh filter + secrets env 注入）→ exec via target → parse → output_schema 校验 → findings 求值 → 返回 `InspectorResult`
- preflight 任一条件不满足 → 返回 `InspectorResult(status="requires_unmet", ...)`（**不**走 collect）
- collect 超时 → 返回 `InspectorResult(status="timeout", ...)`
- target 不可达（`TargetError` 抛出）→ 返回 `InspectorResult(status="target_unreachable", ...)`
- parse 失败 / output_schema 不匹配 → 返回 `InspectorResult(status="exception", error=...)`，**不**抛异常（让 Planner 能继续调度其他 Inspector）
- finding DSL 引擎用 `simpleeval`，**只允许只读表达式**（与 ARCHITECTURE §4「Inspector 不能调 LLM」边界一致）

**新增（4 种 parse format 实现）：**

- `raw`：stdout 直接放入 `output["raw"]`（string 类型）；适合 `hello.echo` / 简单 ping 类
- `table`：按空白拆列（兼容 `ps`/`df` 等 POSIX 表格输出）；`parse.columns` 必填；首行作为 header skip 由 `parse.skip_header_rows`（默认 1）控制
- `json`:`json.loads(stdout)` 后放 `output`；失败 raise（被 runner 转成 `status="exception"`）
- `kv`：每行 `key=value` 解析；`parse.delimiter` 默认 `=`；适合 `/proc/meminfo` 类输出

**新增（InspectorResult 数据模型，M1 最小可用）：**

- `hostlens.inspectors.result.InspectorResult` Pydantic 模型：含 `name` / `version` / `status` / `target_name` / `duration_seconds` / `output` / `findings` / `error` 字段
- `InspectorStatus` Literal：M1 范围**恰好** `{"ok", "timeout", "target_unreachable", "requires_unmet", "exception"}` 五值
- `Finding` Pydantic 模型：含 `severity` / `message` / `evidence`（与 M2 stub `FindingSummary` 字段集兼容；M3 `add-report-data-model` 提案才扩展为完整 finding identity 模型）

**新增（2 个内置 Inspector，验证管线 + Demo Path 用）：**

- `src/hostlens/inspectors/builtin/hello/echo.yaml`：跑 `echo hello`，`parse.format=raw`，findings 聚合判定 stdout 非空，返回 info-level "hello from <target>"；本提案 Demo Path 用
- `src/hostlens/inspectors/builtin/system/uptime.yaml`：跑 `uptime`，`parse.format=raw`，配合 hook.py-free 的简单正则提取（runner 内置 `parse.raw_extract_regex` 字段，单组捕获负载均值）—— **修订**：为避免引入 hook.py，本提案在 `raw` format 上额外加 `parse.raw_extract_regex: str | None = None` 字段，捕获组按 `parse.columns` 命名映射到 output 顶层字段；M6 复杂场景再考虑 hook.py

**新增（CLI 命令集）：**

- `hostlens inspectors list [--tag <tag>] [--target-kind <kind>] [--json]`：列出已注册 Inspector + 标签 + 兼容 target 类型；只读，允许 root
- `hostlens inspectors show <name> [--json]`：打印单个 Inspector 的 manifest（脱敏后；secrets 字段只显示名字不显示值）；只读，允许 root
- `hostlens doctor` 增加 `inspectors` section：加载错误清单（per-file path + 字段级错误）+ secrets 占位预检（manifest 声明的 env var 是否存在）

**修订（tool-registry-capability-layer，消除 InspectorRegistry stub）：**

- `hostlens.tools.base.InspectorRegistry` stub Protocol（含其 `list_summaries()` 方法签名）**完全删除**
- `hostlens.tools.base.ToolContext.inspector_registry` 字段类型从 stub Protocol 切到真实 `hostlens.inspectors.registry.InspectorRegistry`
- `register_default_tools` 注入的 `list_inspectors` handler 保留对 `ctx.inspector_registry.list_summaries()` 的调用形态不变（投影逻辑在真实 `InspectorRegistry.list_summaries()` 内完成；handler 只做 `tag` / `target_kind` 过滤）；本提案的修订点仅是 `ctx.inspector_registry` 类型从 stub 切到真实 `InspectorRegistry`，handler 数据来源从 stub 切到真实 manifest（与 `tool-registry-capability-layer` MODIFIED Requirements 严格一致）
- `register_default_tools` 注入的 `run_inspector` handler 从 stub no-op 改为真实 dispatch：`InspectorRunner(ctx).run(manifest, target, args.parameters)` → 投影 `InspectorResult → RunInspectorOutput`；**仍**遵守 `requires_approval=False` + `side_effects="read"` 的 M2 policy（Inspector 本身是只读巡检）

## 功能 (Capabilities)

### 新增功能

- `inspector-plugin-system`: `InspectorManifest` Pydantic schema、loader（含 shell 注入静态校验）、`InspectorRegistry`（含 builtin + 用户目录搜索）、`InspectorRunner`（4 种 parse format + Finding DSL 求值引擎 + preflight + 5 种 `InspectorStatus`）、2 个内置 Inspector、`hostlens inspectors list/show` CLI、`hostlens doctor` inspectors section

### 修改功能

- `tool-registry-capability-layer`: `ToolContext.inspector_registry` 从 stub Protocol 切到真实 `InspectorRegistry`；`list_inspectors` 与 `run_inspector` handler 接通真实数据（M2 提案中标注的 stub 占位被替换）；删除 `hostlens.tools.base.InspectorRegistry` stub 定义

## 影响

**代码：**

- 新增 `src/hostlens/inspectors/{__init__.py, schema.py, loader.py, registry.py, runner.py, result.py, parsers/__init__.py, parsers/raw.py, parsers/table.py, parsers/json.py, parsers/kv.py, dsl.py}`
- 新增 `src/hostlens/inspectors/builtin/hello/echo.yaml`、`src/hostlens/inspectors/builtin/system/uptime.yaml`
- 新增 `src/hostlens/cli/inspectors.py`（Typer 子命令组）；注册到 `cli/__init__.py`
- 修改 `src/hostlens/cli/doctor.py`：增加 `inspectors` section
- 修改 `src/hostlens/tools/base.py`：删除 `InspectorRegistry` stub Protocol；`ToolContext.inspector_registry` 类型切换
- 修改 `src/hostlens/tools/default_tools.py`：`list_inspectors_handler` 与 `run_inspector_handler` 接通真实 registry + runner
- 修改 `src/hostlens/core/config.py`：新增 `Settings.inspectors_search_paths: list[Path]` 字段（默认 `[~/.config/hostlens/inspectors]`，env `HOSTLENS_INSPECTORS_SEARCH_PATHS` 以 `:` 分隔 override；builtin 路径**不**走配置，由代码 hardcode）
- 新增测试：`tests/inspectors/test_schema.py`、`tests/inspectors/test_loader.py`（含注入 payload 矩阵）、`tests/inspectors/test_registry.py`、`tests/inspectors/test_runner.py`、`tests/inspectors/test_dsl.py`、`tests/inspectors/parsers/test_*.py`、`tests/inspectors/test_builtin.py`、`tests/cli/test_inspectors.py`、`tests/tools/test_list_inspectors_with_real_registry.py`、`tests/tools/test_run_inspector_with_real_registry.py`

**依赖（PEP 508 语法，与现有 pyproject.toml `>=` 风格一致）：**

- 新增 runtime 依赖：
  - `simpleeval>=1.0,<2`（Finding DSL 求值；只读表达式）
  - `jinja2>=3.1,<4`（collect.command 模板渲染；与 M5 Notifier 复用同一版本）
  - `jsonschema>=4.20,<5`（manifest `output_schema` 校验 + `parameters` schema 校验）
  - `pyyaml>=6.0,<7`（manifest 加载；M0 可能已有，本提案 follow-up 在 pyproject 确认）

**配置文件：**

- 新增 `~/.config/hostlens/inspectors/` 约定路径（用户自定义 Inspector）；M0 已落地的 `Settings` 增加 `inspectors_search_paths` 字段

**文档：**

- 更新 `docs/ARCHITECTURE.md` §4：把 Inspector 章节"待办"状态改为"M1 落地（PR #<本提案 PR 号>）"；明确"M1 不含 `sampling_window` / `artifacts` / `hook.py` / `sql_result` format"
- 新增 `docs/operations/inspectors.md`：manifest 写作 best practice + shell 注入防御要点 + builtin Inspector 列表
- 新增 `docs/operations/inspector-authoring.md`：从零起一个新 Inspector 的 5 分钟教程（含 `hostlens doctor` 调试循环）
- README "快速开始"小节增加 `hostlens inspectors list` / `hostlens inspectors show hello.echo` 示例

**对外契约影响：**

- **CLI 命令**：新增 `hostlens inspectors` 子命令组（list / show）
- **Inspector schema**：本提案首次定义；后续 Inspector 提案（如 M6 内置 Inspector 库扩充）都必须 conform 此 schema；M2.8 incident pack 的 8 个 Inspector 也走此 schema
- **Agent tool schema**：`list_inspectors` / `run_inspector` ToolSpec 的 input/output schema **不变**（M2 已锁定）；仅 handler 从 stub 切到真实实现
- **MCP tool schema**：M7 才暴露，本提案不影响
- **Schedule manifest**：M4 才落地的 `schedule.inspectors:` 字段值域 = 本提案落地的 Inspector `name` 字段；schema 在 M4 单独定义
- **Notifier Protocol / Backend Protocol**：不影响

## 非目标（Non-Goals）

明确**不在**本提案范围，防止范围蔓延：

- ❌ `hostlens inspect <target> --inspector <name>` 端到端命令：依赖 Report 数据模型 + markdown 渲染，留给下一提案 `add-report-data-model`（M1.6 + M1.7 收尾）
- ❌ Inspector `hook.py` Python 扩展机制：M1 用 `raw + raw_extract_regex` + 4 种内置 parse format 覆盖 80% 简单场景；hook.py 留给 M6 复杂场景（PostgreSQL bloat / TLS expiry 等示例都用到）单独提案
- ❌ `parse.format = "sql_result"`：留给 M6 PostgreSQL Inspector
- ❌ `collect.sampling_window` 时窗采集（`window_start` / `window_end` 注入）：留给 M2.8 incident pack 的 "log.tail.error_burst" Inspector 单独提案
- ❌ `artifacts` 字段（附加产物声明）：留给 M3 报告系统（需要 Report 模型支持 attachment）
- ❌ Inspector 优先级 / 依赖图 / 串行链：M2 Planner Agent 通过 tool_use 自然语言决定调度顺序，不需要 manifest 层声明依赖
- ❌ Inspector 输出缓存 / 增量执行：M1 每次跑都完整执行
- ❌ Inspector 跨 target 聚合（如"对所有 web target 跑同一个 Inspector 再聚合"）：M4 Scheduler 层处理 fan-out，Inspector 只看单 target
- ❌ Inspector 版本升级 / migration：M1 不允许同名不同版本共存（loader 报错）；版本管理留给 M10 文档发布
- ❌ Inspector marketplace / 远程加载：M1 只扫本地目录
- ❌ `privilege="sudo"` / `privilege="root"` 的 sudo 调用集成：M1 runner 只检查 `--allow-privileged` opt-in 并标 status；真正经过 sudo 拿到 root shell 是 M9 Remediation 范围（且经过 approval gate）

## Failure Modes

| 故障 | 行为 | 用户可见状态 |
|---|---|---|
| YAML 语法错误 | loader raise `InspectorError(kind="manifest_parse_error", path=path, line=line, column=col)`；CLI 与 doctor 都显示文件路径 + 行列号 | `hostlens inspectors list` 跳过该文件但 exit 1（防止 silent 加载失败）；doctor exit 1 |
| Pydantic schema 校验失败（字段缺失 / 类型错 / 未知字段） | loader raise `InspectorError(kind="manifest_validation_error", path=path, errors=...)`；errors 是字段级 dict | 同上 |
| 未声明 `pattern`/`enum` 的 string parameter | loader raise `InspectorError(kind="parameter_missing_charset_constraint", path=path, parameter=name)`（**加载时**而非运行时） | 同上 |
| `collect.command` 中 string parameter 未走 `\| sh` filter | loader raise `InspectorError(kind="unquoted_parameter_in_command", path=path, parameter=name, position=col)` |
| `collect.command` 中 array(items.type=string) parameter 未走 `\| map('sh') \| join(...)` filter chain | loader raise `InspectorError(kind="unquoted_array_parameter_in_command", path=path, parameter=name)`（与单 string 同等对待 shell 注入向量） |
| `requires_files` 路径含 shell 元字符（如 `/tmp/x; curl evil`） | loader 字段级正则 `^/[A-Za-z0-9._/-]+$` 在 manifest 加载时 raise `pydantic.ValidationError`（字段层校验）；runner 探测时**仍**用 `shlex.quote(path)` 包路径作为防御纵深 | 同上 |
| `secrets` 名字出现在 Jinja2 插值位置 | loader raise `InspectorError(kind="secret_inlined_in_command", path=path, secret=name)` | 同上 |
| 同名 Inspector 重复注册（builtin vs builtin / 用户 vs builtin） | registry raise `InspectorError(kind="duplicate_inspector", inspector=name, paths=[...])` | doctor exit 1 + 显示冲突文件路径 |
| `requires_binaries` / `requires_files` / `requires_capabilities` 不满足 | runner 返回 `InspectorResult(status="requires_unmet", missing=[...])`；**不**走 collect | finding-level: requires_unmet；Planner Agent 收到后选择其他 Inspector |
| `privilege != "none"` 但未 `--allow-privileged` opt-in | runner 返回 `InspectorResult(status="requires_unmet", missing=["privilege_opt_in"])` | 同上 |
| `collect.command` 超时（exec 返回 `timed_out=True`） | runner 返回 `InspectorResult(status="timeout", duration_seconds=...)` | finding-level: timeout |
| target 不可达（exec raise `TargetError(kind="ssh_connection_lost")` 等） | runner 捕获 + 返回 `InspectorResult(status="target_unreachable", error="ssh_connection_lost")`；**不**让 TargetError 透出（让 Planner 能继续调度） | finding-level: target_unreachable |
| parse 失败（json.loads 抛 / table 列数不对 / output_schema mismatch） | runner 返回 `InspectorResult(status="exception", error="parse_failed: ...")`；error 字段含失败原因（不含 stdout 原文，避免泄漏） | finding-level: exception |
| `findings[].when` simpleeval 求值异常（如除零 / 名字未绑定） | 该 finding rule 被 skip + 记 warning log；其他 finding rule 继续；**不**让整个 InspectorResult 失败 | finding 数量可能比预期少；log 含 finding rule 索引 |
| simpleeval 表达式触发 `simpleeval.InvalidExpression`（如不允许的语法节点） | 同上 + 额外标记 `dsl_unsafe`；doctor 应预先 detect | doctor exit 1 |

## Operational Limits

参考 docs/OPERABILITY.md §1：

- **单 Inspector exec 默认超时**：60s（manifest `collect.timeout_seconds` 缺省值；上限通过 `Settings.concurrency.inspector_timeout_seconds_max` 兜底 = 300s；超过上限 manifest 在 loader 阶段 raise）
- **manifest 文件大小上限**：256 KB（防止恶意/误填超大文件耗 memory）；loader 检查文件大小 + raise `manifest_too_large`
- **inspector registry 总数上限**：M1 暂不设硬上限（M6 内置 Inspector 库扩充到 40+ 时再评估）；当前预期总数 < 100
- **single inspector runner 内存占用**：M1 runner 不缓存中间结果 / 不持久化 output；stdout / stderr 各最大 1 MB（来自 ExecutionTarget exec 层的默认）
- **Finding DSL 求值 timeout**：simpleeval 单表达式硬上限 1.0s（防止恶意/写错的表达式拖慢 runner）；超时 → finding rule skip + warning log
- **Inspector preflight `requires_binaries` 探测**：每个 binary 走一次 `command -v <bin>`（POSIX shell builtin）；多 binary 并发探测，total timeout 10s

## Security & Secrets

参考 docs/OPERABILITY.md §7：

- **新增密钥来源**：无；本提案沿用 `add-execution-target-abstraction` 已落地的 `${ENV_VAR}` 占位 + ExecutionTarget env 注入路径
- **Secret 注入路径**（**唯一允许的方式**）：manifest `secrets:` 声明 → runner 从 `os.environ` 读 → 通过 `ExecutionTarget.exec(cmd, env={...})` 注入；命令中通过 `$VAR_NAME` 引用
- **加载时拒绝模式**（loader 强制 reject）：
  1. `secrets` 名字出现在 `collect.command` 模板插值位置（如 `{{ PGPASSWORD }}`）—— 见上方 Failure Modes
  2. `parameters` 中 string 字段没声明 `pattern` 或 `enum`
  3. `collect.command` 中 string parameter 未走 `| sh` filter
- **`hostlens inspectors show` 输出脱敏**：`secrets:` 字段只显示**名字列表**（如 `["PGPASSWORD"]`），**不**读取 env var 显示真实值；`parameters` 字段的 default value 如果引用 `${ENV_VAR}` 也只显示占位符
- **Inspector 日志脱敏**：runner 在记录 `inspector_started` / `inspector_finished` log 时，**禁止**记录 `parameters` 完整字典（可能含敏感参数如 db host:port）；只记录 inspector name / version / target name / status / duration
- **shell 注入攻击面**：本提案的 loader 静态校验（pattern + sh filter + secrets-not-in-command）是核心防御；测试矩阵必须含 ≥10 个真实注入 payload（`'; rm -rf /; #` / `$(curl evil)` / `\`whoami\`` / `\x00` / Unicode RTL override 等）
- **simpleeval 攻击面**：DSL 引擎默认禁用 attribute access（除 dot notation 访问绑定变量字段）/ 禁用 import / 禁用 lambda；测试矩阵含尝试越界的 payload
- **YAML 反序列化**：用 `yaml.safe_load`（**禁止** `yaml.load` 走 default loader，避免任意 Python 对象构造）；loader 内 grep 校验

## Cost / Quota Impact

参考 docs/OPERABILITY.md §3：

- **LLM token 消耗**：**零**；本提案纯本地管线，不调 LLM
- **Anthropic API 调用频次**：**零**；与 M2 Agent loop 解耦（M2 调 LLM 时通过 `run_inspector` ToolSpec 间接触发本提案的 runner，但 runner 本身不调 LLM）
- **下游 LLM 影响（M2 提案集成时）**：`list_inspectors` ToolSpec 返回的 `InspectorSummary` 列表会进入 system prompt cache prefix（Planner Agent 概览 Inspector 能力）；本提案确保 `InspectorSummary` payload 稳定可缓存（`tags` / `compatible_target_kinds` 按字典序输出）；M1 内置仅 2 个 Inspector + 用户态预计 < 20，prefix 大小 < 5 KB，对 cache hit 影响可忽略

## Demo Path

> 目标：交付后任何人在干净 macOS / Linux 上 ≤5 分钟跑通"加载内置 inspector → 列出 → 查看 manifest → 通过 ToolRegistry dispatch 一次"。**无 SSH、无付费 API、无远端访问。**

1. **环境准备**（30s）：clone 仓库 → `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
2. **加载验证**（30s）：`hostlens doctor --json | jq '.inspectors'` —— 期望输出 `{"status": "ok", "loaded": 2, "errors": []}`（builtin hello.echo + system.uptime 都加载成功）
3. **列出 Inspector**（10s）：`hostlens inspectors list --json` —— 期望输出 2 条 InspectorSummary（按 name 字典序：`hello.echo` / `system.uptime`），每条含 `tags` / `compatible_target_kinds`
4. **查看单个 manifest**（10s）：`hostlens inspectors show hello.echo --json` —— 期望输出完整 manifest（secrets 字段为空数组，因为 hello.echo 不用 secret）
5. **配置 local target**（30s；复用 `add-execution-target-abstraction` 落地路径）：`hostlens target add local-host --type local`
6. **通过 ToolRegistry dispatch（模拟 M2 Agent loop 调用）**（60s）：写一个 5 行 Python script `examples/m1-inspectors/dispatch.py` 用 `register_default_tools` 装配 ToolRegistry + 构造 ToolContext + `await registry.dispatch("run_inspector", RunInspectorInput(target_name="local-host", inspector_name="hello.echo"))`；期望拿到 `RunInspectorOutput(findings=[FindingSummary(severity="info", message="hello received: hello\n", evidence={})])`（与 spec §需求:内置 Inspector 中 `hello.echo` 的 finding message 模板 `"hello received: {raw}"` 严格一致——`{raw}` 来自 parse output 顶层字段 = `echo hello` 的 stdout）
7. **失败路径验证**（60s）：故意把 `~/.config/hostlens/inspectors/bad.yaml` 写成含 `'; rm -rf /` 注入的 manifest（string parameter 未声明 pattern）→ `hostlens doctor` 必须 exit 1 + 显示 `parameter_missing_charset_constraint` 错误 + 文件路径 + 字段名
8. **拒绝 root 验证**（10s；read-only 命令允许 root，确认*不*拒绝）：`sudo hostlens inspectors list` 必须正常返回（list/show 是只读，与 `hostlens target list` 行为一致）
9. **CI replay 验证**（30s）：`pytest tests/inspectors/ tests/cli/test_inspectors.py tests/tools/test_list_inspectors_with_real_registry.py tests/tools/test_run_inspector_with_real_registry.py -v` 全绿

完成所有步骤后，`examples/m1-inspectors/README.md` 把以上 9 步固化为可复制粘贴的命令；与 proposal Demo Path 严格一致。
