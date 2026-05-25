## 上下文

`add-execution-target-abstraction` 落地后 `ExecutionTarget` Protocol、`LocalTarget`、`SSHTarget`、`TargetRegistry`、`Capability` Enum、`ExecResult`、`TargetError` 已全部就绪，但 Inspector 层完全是空的：`src/hostlens/inspectors/` 只有 `__init__.py`；`hostlens.tools.base.InspectorRegistry` 是 stub Protocol，仅暴露 `list_summaries()`；`list_inspectors` / `run_inspector` 这两个 M2 首批 ToolSpec 的 handler 拿不到真实数据。

CLAUDE.md §4.2 与 docs/ARCHITECTURE.md §4 已经把"做什么"钉得很死：

- Inspector manifest 是 SOT；新增简单检查 = 加 YAML
- Finding DSL 是固定四字段 `for_each` / `when` / `severity` / `message`
- 求值上下文固定：`output` 顶层字段 + `parameters` + 注入运行时变量 + 内置只读函数
- Shell 注入防御靠"loader 静态拒绝"而非"runner 转义"
- Secrets 只走 env var
- Inspector 不能调 LLM
- LocalTarget / SSHTarget 已落地 5 种 Capability：`SHELL` / `FILE_READ` / `SSH` / `SYSTEMD` / `DOCKER_CLI`

本文档只回答"**怎么实现**"——尤其是必须做 trade-off 的几个决策点：parse format 集合的边界、`raw_extract_regex` 这个"反 hook.py 妥协"、simpleeval 而非完整 eval、Jinja2 sh filter 而非 runner-time shlex.quote、builtin 路径不可配置、`InspectorResult` 与 M3 Report 模型的兼容路径。

约束（先列再决策）：

- Python 3.11+，async-first，mypy `--strict`，pydantic v2，禁用 LangChain / LlamaIndex
- ExecutionTarget 已落地，本提案**只**新增 inspector / cli/inspectors / tools/default_tools 这几个模块；**禁止**改 ExecutionTarget Protocol（已在 spec 上锁死）
- M2 已经在 `tool-registry-capability-layer` spec 锁定 `RunInspectorOutput` / `InspectorSummary` 字段集；本提案 handler 投影必须严格匹配，**不能**改这两个 schema
- M3 `add-report-data-model` 将定义完整的 `Finding` identity 模型；本提案的 `Finding` 是 M3 的**子集**（少 `id` / `inspector_run_id` / `seen_at` 等持久化字段），但字段名与类型必须与 M3 在共有字段上 100% 兼容

## 目标 / 非目标

**目标：**

- 让"加一个 YAML 文件"成为引入新 Inspector 的**唯一**方式（M1 范围内）
- 把 shell 注入防御从"runner 转义"前移到"loader 加载时拒绝"——攻击面在 manifest 写作阶段就堵死
- Finding DSL 引擎在表达力（够用）与安全（不能 RCE）之间选 simpleeval，**而不是**重新实现一个表达式语言
- InspectorRunner 对 TargetError / parse error / DSL error 全部转成 `InspectorStatus` 枚举值，**不**让异常透出——让 M2 Planner Agent 拿到 5 种确定性状态时能继续调度其他 Inspector，而不是被异常炸掉整个 loop
- 消除 `InspectorRegistry` stub 并保持 `RunInspectorOutput` / `InspectorSummary` schema 不变
- 内置 2 个 demo inspector 必须**不依赖 hook.py**（验证"零 Python 代码"主张可信）

**非目标：**

- 不做 `hook.py` 加载机制（M6 复杂场景如 PostgreSQL bloat / TLS expiry 解析才需要；M1 demo path 走不到）
- 不做 `parse.format = "sql_result"`（同上，M6 PostgreSQL Inspector 才用）
- 不做 `collect.sampling_window` 时窗采集（M2.8 incident pack 的 log.tail.error_burst 才需要）
- 不做 `artifacts` 字段（M3 Report 模型支持 attachment 才能用）
- 不做 `hostlens inspect <target> --inspector <name>` 端到端命令（需要 Report 模型 + markdown 渲染；下一提案 `add-report-data-model`）
- 不做 Inspector marketplace / 远程加载 / 版本升级
- 不做 Inspector 调度优先级 / 依赖图（M2 Planner Agent 通过 tool_use 自然语言决定）

## 决策

### 决策 1：parse format 集合**恰好** 4 种（`raw` / `table` / `json` / `kv`），M1 砍掉 `sql_result`

**选择**：M1 实现 raw / table / json / kv，loader 用 `Literal["raw", "table", "json", "kv"]` 限定值域；`sql_result` 写到 manifest 时 Pydantic 直接 raise。

**理由**：

- 4 种格式覆盖 M1 退出条件（hello.echo + system.uptime + M2.8 incident pack 中绝大多数 Linux 系统类 Inspector）
- `sql_result` 的真实使用场景在 M6 的 PostgreSQL / MySQL Inspector，**而那些 Inspector 同时需要 hook.py 做表 bloat 公式计算**——`sql_result` 单独落地价值低
- "声明了但不实现"是反模式（用户写 `format: sql_result` 不会立即报错，跑到 runner 才炸）；用 Pydantic Literal 在加载时就 reject，错误前置

**替代方案**：

- (A) M1 上 5 种 format 全做 → 否决：sql_result 没有真实 caller；落地后维护负担 + 测试矩阵更复杂
- (B) M1 只做 raw + json → 否决：M2.8 incident pack 几个 Inspector（`ps`/`df`/`uptime`/`free`）的输出天然是表格，runner 不内置 table parser 等于强迫每个 Inspector 走 hook.py，违反"零 Python 代码"主张

### 决策 2：`raw` 模式额外支持 `parse.raw_extract_regex`，**回避** M1 引入 hook.py

**选择**：`raw` 格式可选附带 `parse.raw_extract_regex: str | None`（命名捕获组按 `parse.columns` 映射到 output 顶层字段）。

**理由**：

- `system.uptime` 的输出是 `12:34:56 up 1 day, 2:34, 3 users, load average: 0.42, 0.50, 0.55`——既不是 table 也不是 kv 也不是 json；最干净的解析是一个正则
- 不加这个字段就被迫上 hook.py（哪怕只是 5 行解析代码）；hook.py 加载机制涉及动态 import / sandbox 评估 / 测试隔离 / 跨 manifest 命名冲突，至少**多一个 spec + 多 200 行实现**
- 正则只读、纯字符串、可被静态校验（loader 检查正则编译成功 + 命名组数 == columns 数）；攻击面比 hook.py 小一个数量级

**替代方案**：

- (A) 把 hook.py 加进 M1 → 否决：成本远大于一个 regex 字段；hook.py 留给 M6 真正用得到的场景
- (B) `system.uptime` 不放 builtin，M1 demo path 只用 `hello.echo` → 否决：`hello.echo` 太弱，demo path 看不出"Hostlens 在干嘛"
- (C) `system.uptime` 改 `parse.format=raw` 直接把整行当 finding evidence，findings DSL 自己用 simpleeval 做字符串切片 → 否决：把解析逻辑塞到 DSL 里让两层职责混在一起，且 simpleeval 对字符串切片支持有限

**约束**：`raw_extract_regex` 仅在 `parse.format=raw` 下接受；其他 format 出现该字段 → loader raise；正则**禁止**用具名组之外的捕获组（防止意外丢字段）。

### 决策 3：Finding DSL 用 `simpleeval`，**不**用 `eval` / `ast.literal_eval` / 自写 parser

**选择**：`hostlens.inspectors.dsl.evaluate(expr, context)` 包装 `simpleeval.SimpleEval`；预绑定函数集**恰好** `{len, sum, min, max, any, all, now}`；attribute access 仅允许"绑定变量.属性"形式（用于 `for_each` 行变量的字段访问，如 `p.cpu_pct`）；import / lambda / 属性赋值 / 切片赋值 全部禁用。

**理由**：

- `eval` 是 RCE；`ast.literal_eval` 不支持比较运算符（无法写 `x > 70`）；自写 parser 是 6+ 周工作量
- simpleeval 是社区已 battle-tested 的"只读表达式"库，单测可写"已知恶意 payload 矩阵"做回归
- 与 ARCHITECTURE §4 安全边界"findings.when 用 simpleeval 不是 eval，只允许只读表达式"原文一致

**替代方案**：

- (A) `eval(expr, {"__builtins__": {}}, context)` → 否决：`{"__builtins__": {}}` 不能阻止 attribute access 链跳出（如 `().__class__.__base__.__subclasses__()` 经典逃逸）
- (B) 写一个迷你 expression evaluator → 否决：单测覆盖成本巨大，且每加一个内置函数都要做安全审计
- (C) 用 Jinja2 的表达式子集 → 否决：Jinja2 的 sandbox 是为模板设计，不为"返回 bool/数值"设计，把它绕成 expression engine 比 simpleeval 复杂

**约束**：单表达式硬 timeout 1.0s（防恶意循环表达式，虽然 simpleeval 默认禁止 list comprehension 已经基本堵死）；超时 / 求值异常 → finding rule skip + warning log，**不**让整个 Inspector 失败。

### 决策 4：Jinja2 + **自定义 `sh` filter**（基于 `shlex.quote`），**强制** parameters 走 `| sh`

**选择**：`hostlens.inspectors.runner.render_command(manifest, parameters)` 用 `jinja2.Environment(autoescape=False)` + 注册 `sh` filter（实现 = `shlex.quote(str(value))`）；loader 在加载阶段静态扫 manifest 的 `collect.command`，要求每个 `{{ <param_name> }}` 形式的引用必须紧跟 `| sh` filter（除非显式 `unsafe_raw: true`）；secrets 名字出现在 Jinja2 表达式位置 → loader 直接 raise。

**`array` 参数同等对待**：JSON Schema `type: array` 且 `items.type == "string"` 的字段在命令模板中出现时，必须经过 `| map('sh') | join(<delim>)` filter chain（多元素拼接）或 subscript 后单元素继续走 `| sh`（如 `{{ endpoints[0] | sh }}`）；否则 loader raise。数组 string 元素同样是 shell 注入向量（如 `endpoints=["host1; rm -rf /", "host2"]`），不能因为容器类型不同就豁免——loader AST 遍历必须对 `nodes.Name` 检查其引用的 schema type 与 filter chain 的组合。

**理由**：

- Loader 做静态校验，比 runner-time 转义更安全——攻击者写恶意 manifest 时**写**的那一刻就被拒，不依赖 runner 做对
- `sh` filter 强制声明，让"开发者必须主动选择是否信任此参数"成为代码上的显性决策；漏 `| sh` 不是默认安全，而是默认报错
- 数组类型与 string 类型在 shell 注入风险上等同，proposal 与 spec 对二者**同等强制**
- Jinja2 是项目已锁定依赖（M5 Notifier 模板 + M3 报告渲染都要用），不引入新依赖

**替代方案**：

- (A) 不上模板引擎，runner 自己用 `str.format` + 内部 shlex.quote → 否决：format 字符串里的 `{...}` 与 finding message 模板的 `{...}` 语法冲突；用户视角混乱
- (B) 全部走 envsubst 风格 `$VAR` 替换 → 否决：参数命名 collision 麻烦（Linux env name 限定 `[A-Z_][A-Z0-9_]*`，但 parameters 可以是 `host`/`port` 这种小写）；且不支持复杂模板（如 `{{ endpoints | map('sh') | join(' ') }}`）
- (C) Jinja2 默认 autoescape=True → 否决：autoescape 是 HTML escaping，shell 注入需要的是 shlex.quote；二者不能混用

**约束**：`sh` filter 实现**禁止** swallow 空值——`None` / 空 list raise 而非返回空字符串（防止用户写错 manifest 时 silent 失败）。

### 决策 5：Secrets **只能**通过 `ExecutionTarget.exec(env=...)` 注入，loader 拒绝 Jinja2 插值

**选择**：manifest `secrets: [PGPASSWORD]` 声明的名字 → runner 从 `os.environ` 读 → 通过 `exec(cmd, env={"PGPASSWORD": "..."})` 传给 target；命令中用 `$PGPASSWORD`（shell 求值）引用。Loader 静态扫 `collect.command`，任何 `{{ <secret_name> }}` 形式（即 secrets 出现在 Jinja2 表达式位置）→ raise `InspectorError(kind="secret_inlined_in_command")`。

**理由**：

- Jinja2 插值会让 secret 进入 cmd string 字面量，最终落 process list / shell history / 错误日志栈帧——所有这些点都会泄漏
- env var 路径已经在 ExecutionTarget Protocol 中作为 first-class 支持（`exec(cmd, *, env, timeout)`），不需要新加抽象
- 与 ARCHITECTURE §4 安全边界原文一致

**替代方案**：

- (A) 允许 Jinja2 插值但 runner 把 secret 替换成占位符再写日志 → 否决：栈追踪 / 第三方库 log / Python `subprocess.SubprocessError.cmd` 都拿不到替换后的版本，secret 仍会泄漏
- (B) 用 stdin 传 secret → 接受但不强制；M1 范围内 stdin 注入由 Inspector 作者通过自定义 collect.command 实现（如 `cmd: 'psql ... <<< "$PGPASSWORD"'`），runner 不显式支持

**约束**：runner 在 dispatch 前必须校验 `secrets:` 声明的 env var 全部存在；缺失 → 返回 `InspectorResult(status="requires_unmet", missing=["env:PGPASSWORD"])`（与 binary missing 行为对齐）。

### 决策 6：`InspectorRegistry` 装配走 `build_registry_from_search_paths`，**不允许**用户覆盖 builtin

**选择**：`build_registry_from_search_paths(paths: list[Path], *, settings: Settings) -> RegistryBuildResult` 按顺序扫 builtin（`src/hostlens/inspectors/builtin/`，**hardcode 路径**）+ 用户目录（`Settings.inspectors_search_paths`，默认 `[~/.config/hostlens/inspectors]`，环境变量 override）；返回 `RegistryBuildResult(registry, errors)` 双值。任何 name 冲突（builtin vs builtin / 用户 vs builtin / 用户 vs 用户）→ raise `InspectorError(kind="duplicate_inspector")`（**fatal**，不进 errors）；用户路径下单文件加载错误（parse / validation 等）collect 到 `errors` 列表，**不**阻塞其他 inspector 装配。

**理由**：

- "用户能 silent 覆盖 builtin"是巨大安全风险——攻击者放一个同名 manifest 在 `~/.config/hostlens/inspectors/system.uptime.yaml` 就能让 hostlens 跑任意命令；走拒绝 + 报错路线更稳
- builtin 路径 hardcode 而非配置是为了防止"配置漂移"——M3+ 提案的 demo path 都假设 `hello.echo` / `system.uptime` 存在，配置可改时就会有用户改完忘记复原的场景
- 用户想 override 时正确路径是 fork manifest + 重命名（如 `system.uptime` → `myorg.system.uptime`），命名空间清晰

**替代方案**：

- (A) 用户路径 > builtin（用户优先）→ 否决：见上方安全理由
- (B) 用户能通过 `Settings.allow_builtin_override = True` 显式允许 → 否决：增加 surface area 且没有真实用例（M6 内置 Inspector 库扩充后 builtin 才会多）
- (C) builtin 路径也走配置（`Settings.builtin_inspectors_path`）→ 否决：配置漂移风险

### 决策 7：`InspectorRunner` 把所有故障转成 `InspectorStatus` 枚举值，**不**透出异常

**选择**：runner 的 `run()` 方法签名是 `async def run(...) -> InspectorResult`，**永远**返回 `InspectorResult`；TargetError / parse 失败 / DSL 异常等全部捕获并映射到 `InspectorStatus`（`requires_unmet` / `timeout` / `target_unreachable` / `exception` / `ok`）；唯一会让 runner raise 的是**调用方传错参数**（如 manifest 为 None、cancel event 已被取消等编程错误，用 `ValueError` 表达）。

**理由**：

- M2 Planner Agent 会并行调 N 个 Inspector，单个 Inspector 失败不应炸掉整个 tool_use turn——返回确定性 status 让 Planner 能继续工作
- "5 种 status 枚举" vs "20 种 Exception 类"，前者对 Agent prompt 注入更友好（Planner 看到的 schema 是闭集，可以在 system prompt 里穷举）
- 与 OPERABILITY §9 降级路径一致（"单 Inspector 失败 → 标 partial，其他继续"）

**替代方案**：

- (A) Runner 透出 TargetError，让 Planner Agent 在 tool dispatch 失败时处理 → 否决：Anthropic tool_use 失败 default 行为是 Agent 重试同一 tool，会浪费 token
- (B) 用 `Result[T, E]` 模式（Rust 风） → 否决：Python 没有 std 支持，引入 dataclass 实现成本 > 收益

**约束**：runner **必须**记录每种降级状态的 structured log（含 inspector name / status / 失败原因），供 doctor 与 debug 排查；log 中**禁止**包含 stdout / stderr 完整内容（防止 secret 泄漏），只记长度统计与 hash。

### 决策 8：`InspectorResult.findings` 用本提案的最小 `Finding` 模型，M3 扩展时**只加字段不改字段**

**选择**：本提案 `Finding` 模型字段集 = `{severity, message, evidence}`；M3 `add-report-data-model` 将扩展为 `{id, inspector_run_id, severity, message, evidence, inspector_version, seen_at, ...}`——所有新增字段都是 **add-only**，本提案这三个字段名 / 类型保持不变。

**理由**：

- M2 stub `FindingSummary` 已经用这三个字段且字段名锁定（已 ship）
- M3 加字段时本提案的测试 fixture / 内置 Inspector 都不用改
- 让 InspectorRunner 在 M3 提案落地前能跑通完整测试矩阵

**替代方案**：

- (A) 等 M3 提案先落地完整 Finding 模型，本提案再实现 runner → 否决：违反 M1 节奏，M2 Agent loop 就要拿到 runner 数据
- (B) 本提案直接定义 M3 完整字段集，runner 填空字段 → 否决：跨提案契约（`id` / `inspector_run_id` 等持久化字段是 SQLite store 才生成）的字段过早定义会迫使本提案承担 M3 责任

### 决策 9：新增依赖**只**引入 simpleeval / jinja2 / jsonschema / pyyaml

**选择**：runtime 依赖在 pyproject.toml 加 `simpleeval>=1.0,<2`、`jinja2>=3.1,<4`、`jsonschema>=4.20,<5`、`pyyaml>=6.0,<7`（pyyaml 可能 M0 已有，loader 任务时确认）。

**理由**：

- 这 4 个都是 long-term maintained、单一职责、零运行时依赖膨胀的库
- 与 M5 Notifier（Jinja2）/ M2 Tool Registry（Pydantic JSON Schema 生成）依赖一致

**替代方案**：

- (A) 引入 `lark`/`pyparsing` 自写 DSL → 否决：维护成本
- (B) 引入 `cerberus`/`marshmallow` 替代 jsonschema → 否决：标准 JSON Schema 与 manifest `output_schema` / `parameters` 字段语义一致，第三方 schema 库会增加用户学习成本

### 决策 10：CLI `inspectors list/show` 走 ToolRegistry CLI adapter **还是**直接调 InspectorRegistry？

**选择**：**M1 直接调 InspectorRegistry**，不走 ToolRegistry CLI adapter。

**理由**：

- ToolRegistry CLI surface adapter 尚未落地（CLAUDE.md §4.10 M2 范围 "M2 只做 Layer 1 + Agent adapter；MCP adapter 到 M7"，CLI adapter 留到后续 milestone）
- `list_inspectors` / `run_inspector` ToolSpec 当前 surfaces = `{"agent"}`，**不含** `"cli"`；强行扩 surfaces 会让 policy gate 提前打开
- `hostlens inspectors list/show` 是纯本地查询、零网络调用、零 LLM——直接 ctx → registry → 输出，比走 ToolRegistry dispatch 路径少 3 个抽象层；面试官读 CLI 代码也更易懂
- 等 CLI adapter 真正落地时（独立提案）再统一迁移

**替代方案**：

- (A) 给 `list_inspectors` ToolSpec 加 `"cli"` surface 并实现 CLI adapter → 否决：扩大 M1 范围；ToolRegistry CLI adapter 独立 spec 才是正确节奏
- (B) `hostlens inspectors run <name>` 直接走 ToolRegistry `dispatch` → 否决：那是 `hostlens inspect <target> --inspector <name>` 的活，留给下一提案

## 风险 / 权衡

| 风险 | 缓解措施 |
|---|---|
| Jinja2 `sh` filter 静态扫描漏判（用户用 `{{ host }}` 不带 filter，loader 没识别） | loader 用 AST 级 Jinja2 parser（`jinja2.Environment().parse(source)`）遍历 `nodes.Name` 节点而非 regex；单测覆盖 ≥10 种边角写法（`{{- p -}}` / `{{ p|default("x") }}` / `{% if p %}{{ p }}{% endif %}` 等） |
| simpleeval 升级引入新攻击面（如未来版本默认开 attribute access） | requirements 锁 `<2`；CI 必须有"已知恶意 payload 矩阵"测试，每次 simpleeval 升级跑全套 |
| 用户写超大 manifest（10 MB YAML）耗内存 | loader 检查文件大小，>256 KB raise；CI 测试用 1 MB fixture 验证 raise |
| 用户用同名 Inspector 试图 silent override builtin（攻击场景） | builtin vs 用户的 name 冲突 = raise（决策 6）；CI 测试覆盖 |
| 用户在 `requires_binaries` 写 `; rm -rf /` 试图注入 | loader 用 Pydantic `pattern=r"^[a-zA-Z0-9._-]+$"` 限制 binary 名字符集 |
| Inspector runner 调 `command -v <bin>` 探测 binary 时被 target 端别名劫持（如远端 sshd 用户 shell rc 注册了恶意 `command -v` 函数） | runner 用绝对路径 `/usr/bin/command -v` 或 fallback 到 `which`；POSIX 规定 `command` 是 builtin 但用户 shell rc 仍可干扰；接受这个风险并在 docs/operations/inspectors.md 说明 |
| `raw_extract_regex` 用户写出 catastrophic backtracking 正则导致 runner 卡死 | **诚实约束**：Python `re` 在 C 层跑回溯时无法被 `asyncio.wait_for` / `signal.alarm`（非主线程）/ `threading` 中断——这些"软 timeout"都不可靠。M1 选**纯静态防御**：loader 在 `ParseSpec.raw_extract_regex` 字段层强制**四层闸**（任一失败 raise；与 spec §需求:`CollectSpec`/`ParseSpec`... §raw_extract_regex 字段约束严格一致）：(1) 长度 ≤ 200 字符；(2) `re.compile()` 成功；(3) 所有捕获组都是命名组、命名组数 == `len(columns)`；(4) **AST-level ReDoS 拒绝**——用 `sre_parse.parse(regex)` 拿 AST 并 walk 节点，按 6 类 known-bad 模式 tag（`nested_quantifier` / `quantifier_on_assert` / `groupref_forbidden` / `atomic_group_forbidden` / `prefix_subset_alternation` / `quantifier_on_empty_matchable`）逐项检测。**禁止**用 regex 字面扫描（漏判风险高，且与 spec 显式约束冲突——只能用 `sre_parse` AST walk）。runner 仍然用 `asyncio.wait_for(timeout=1.0)` 作为兜底，但**不**作为主要防御。M6 起若引入复杂正则需求时再评估接入 `google-re2`（线性时间保证，但需 C++ 编译）。 |
| InspectorRunner 不抛异常的契约让 bug 难发现（runner 自身的逻辑错误也被吞掉变 `status="exception"`） | **按调用层 scope，不按异常类全局分类**——runner 在每个业务调用点用**精确的 except 列表**只捕获该调用点合法可能抛的异常类型，并就地转 status / skip finding；runner 自身代码路径上的 `AttributeError` / `KeyError` / `TypeError` 不在任何 except 子句的捕获范围内，会自然 propagate 暴露 bug。具体映射：(a) `target.exec` 调用点只 catch `TargetError`、`asyncio.TimeoutError` 间接通过 `ExecResult.timed_out` 表达；(b) parser 调用点只 catch `InspectorError(kind="parse_json_not_object")` / `json.JSONDecodeError`；(c) `jsonschema.validate` 调用点只 catch `jsonschema.ValidationError`；(d) Jinja2 render 调用点只 catch `jinja2.UndefinedError` / `jinja2.TemplateError`；(e) DSL `evaluate` 调用点只 catch `simpleeval.InvalidExpression` / `simpleeval.FeatureNotAvailable` / `simpleeval.NameNotDefined` / `simpleeval.NumberTooHigh` / `simpleeval.WrongType` / `simpleeval.IterableTooLong` / `asyncio.TimeoutError`；(f) `format_message` 调用点（`str.format(**ctx)`）只 catch `KeyError` / `IndexError` / `AttributeError` —— **这是唯一允许 catch `KeyError`/`AttributeError` 的调用点**，因为它们是用户 manifest 写错变量名时的合法表达，会被记录为 finding rule skip + warning log。**禁止**在 runner 顶层写 bare `except Exception`——任何"漏网"异常都应该作为 runner 自身 bug 暴露出来。 |
| `RunInspectorOutput` schema 锁死后未来 M3 加 finding 字段需要扩展但破坏向后兼容 | M3 提案对 `RunInspectorOutput` 用 spec MODIFIED 块，明确扩展是 add-only；本提案只往 `Finding` 加字段不动 schema 顶层结构 |
| 用户写错 `output_schema`（如声明 `processes: array` 但 parse 返回 dict）→ 用 jsonschema 校验失败 → runner 标 `exception` → 用户看不到 schema 校验失败的具体字段 | runner 在 `exception` 状态时 `error` 字段写 jsonschema `ValidationError.message`（含 jsonpath 路径）；测试覆盖 |
| Jinja2 的 `secrets` 静态扫漏（用户用 `{{ vars["PGPASSWORD"] }}` 而不是 `{{ PGPASSWORD }}`）| loader 不实现 `vars[...]` subscript 检测（Jinja2 AST `nodes.Getitem`）；要求 manifest 必须用直接变量引用，subscript 形式由 loader raise `secret_inlined_in_command`（除非该 subscript 不引用 secrets 名） |
| simpleeval 单表达式 timeout 1.0s 在 macOS 与 Linux 上实现不同（macOS 的 `signal.alarm` 在线程中行为差） | M1 用 `asyncio.wait_for(asyncio.to_thread(simpleeval.SimpleEval().eval, expr), timeout=1.0)`；接受 to_thread 开销（DSL 求值通常 < 10ms，开销可忽略） |

## 迁移计划

本提案是**纯新增**功能 + **stub 替换**，没有数据迁移；但要注意 stub 替换的兼容窗口：

1. **Phase 1 — 实现新模块**：在 `src/hostlens/inspectors/` 下完成 schema / loader / registry / runner / dsl / parsers；CI 通过；**此时 tool-registry 仍指向 stub Protocol**
2. **Phase 2 — 切换 ToolContext 类型**：在同一 PR 中：
   - 删除 `hostlens.tools.base.InspectorRegistry` stub Protocol 定义
   - `ToolContext.inspector_registry` 类型从 stub 切到 `hostlens.inspectors.registry.InspectorRegistry`
   - `list_inspectors_handler` 与 `run_inspector_handler` 接通真实 runner
   - 更新 `tests/tools/test_list_inspectors.py` / `tests/tools/test_run_inspector.py` 用 `result = build_registry_from_search_paths(...); ctx = ToolContext(inspector_registry=result.registry, ...)` 装配真实 registry（**禁止**保留 stub fallback；**禁止**直接把函数返回值当 registry 用 —— 必须用 `result.registry` 解包）
3. **Phase 3 — 文档与 demo**：写 `examples/m1-inspectors/`、`docs/operations/inspectors.md`、更新 `docs/ARCHITECTURE.md` §4 落地状态、README "快速开始"小节
4. **回滚策略**：若 Phase 2 出现回归，回滚整个 PR（stub Protocol 可以从 git 历史恢复）；不做"部分回滚保留新模块"——`ToolContext` 类型迁移是不可分割原子操作

CI / 部署影响：

- 无数据库迁移、无环境变量重命名、无配置文件 schema 变更（仅**新增** `Settings.inspectors_search_paths` 字段，默认值不破坏现有部署）
- pyproject.toml 新增 4 个依赖；用户首次 `pip install -e ".[dev]"` 会拉新包

## Open Questions

> 这些问题在 spec 撰写阶段必须解决；实施阶段如再次冒头说明 spec 没钉死。

1. **`requires_files` 的语义边界**：是"文件必须存在"还是"文件必须可读"？M1 选"可读"（用 `[ -r <path> ]` 探测）但还需 spec 上敲定
2. **`raw_extract_regex` 匹配失败时**：runner 标 `exception` 还是 `ok` 但 output 为空？M1 选"**ok 但 output 为空**"（让 findings DSL 决定是否产生 finding）；要在 spec 写明
3. **`hostlens inspectors show` 的 output_schema / parameters 字段**：完整打印 JSON Schema（可能很长）还是只打字段名列表？M1 选"`--json` 模式完整、默认表格模式只打字段名"
4. **builtin Inspector 文件的版本管理**：每次内置 Inspector schema 变化要不要更新所有 builtin 的 `version`？M1 选"是，按 semver 规则；schema 不变只改 message 文案是 patch；schema 变化是 minor/major"
5. **simpleeval `now()` 内置函数返回的 timezone**：M1 选"UTC tz-aware"；与 M3 Report 模型 `timestamp(tz-aware)` 对齐
