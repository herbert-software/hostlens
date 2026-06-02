# inspect-cli-command 规范

## 目的

`hostlens inspect <target> --inspector <name>` CLI 命令的行为契约——6 个选项与 1 个位置参数、4 值语义化退出码（0 healthy / 1 critical finding / 2 runner failure / 3 usage error）、`--timeout` 经 CollectSpec 重构注入触发 validation、Typer UsageError → 3 改写（`--help` 仍 exit 0）、stdout/stderr 分离、`--parameters` 双语法解析。本 capability 由 add-report-data-model 提案于 M1.7 引入；M2 Planner Agent 提案将扩展为 `--intent` 自然语言入口。
## 需求
### 需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数

`hostlens inspect <target>` 必须接受以下参数（Typer 定义；标题沿用 M1.7 名称作为稳定标识符，M2.7 新增 `--intent` 后选项总数实为 7，下列为权威清单）：

- 位置参数 `target: str`（必填）：target 名；从 `TargetRegistry` 查询
- 选项 `--inspector / -i <name>: str | None = None`（可选）：inspector 名；从 `InspectorRegistry` 查询。与 `--intent` **互斥**
- 选项 `--intent <自然语言>: str | None = None`（可选）：自然语言巡检意图，触发 `PlannerAgent` 自主规划。与 `--inspector` **互斥**
- 选项 `--output / -o <FILE>: Path | None = None`：输出文件路径；缺省 stdout
- 选项 `--format / -f <md|json>: Literal["md", "json"] = "md"`：输出格式
- 选项 `--parameters / -p <JSON>: str | None = None`：JSON 字符串或 `@<path>` 文件引用（如 `@./params.json`）；缺省传 `{}` 给 InspectorRunner（仅 `--inspector` 路径有意义）
- 选项 `--allow-privileged: bool = False`：opt-in 允许跑 `privilege != "none"` 的 Inspector（仅 `--inspector` 路径有意义）
- 选项 `--timeout <SECONDS>: int | None = None`：单 Inspector 超时（整数秒；不接受 float）；缺省 None = 不覆盖 manifest `collect.timeout_seconds`；**值校验**：`1 <= value <= 300`（与 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 严格一致），不在范围 → exit 3 + stderr `invalid --timeout: must be in [1, 300]`；**实现路径**：`InspectorRunner.run()` 不接受 timeout 覆盖参数（保持其 inspector-plugin-system spec 中的契约不变），CLI 必须在 dispatch 前**重构 CollectSpec 让 Pydantic validation 生效**：`from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})` 再传给 runner —— **禁止**直接用 `manifest.collect.model_copy(update={"timeout_seconds": cli_timeout})` （Pydantic v2 `model_copy(update=...)` **不**触发字段 validation，会让 [1, 300] 外的值静默写入）；CollectSpec 的 `Field(ge=1, le=300)` 在构造时会触发 validation 作为防御纵深第二道；**禁止** 改 `InspectorRunner.run()` 签名或修改 archived `inspector-plugin-system` spec 的 runner 契约；`--timeout` 仅对 `--inspector` 路径生效，`--intent` 路径忽略它并在 stderr 提示一行

**`--inspector` 与 `--intent` 互斥校验**：二者必须**恰好提供其一**。两者都缺、或同时提供，都必须以 usage error（exit 3）失败，stderr 给出一行说明，不泄露 Python traceback。仅 `--inspector` 走 M1 单 Inspector 管线（行为不变）；仅 `--intent` 走 Planner Agent 路径。

**`--help` 输出必须** 列出全部参数 + 简要描述 + 退出码语义（4 行：`0: healthy / 1: critical finding / 2: runner failure / 3: usage error`）。

**Typer 默认 usage exit 转换**：Typer 自身对 `Missing argument` / `Missing option` / `Invalid value for` 等 usage 错误默认 exit 2 —— 这与本提案 exit 2 = runner failure 定义冲突。**CLI 入口必须** 包裹 Typer app 调用，**仅**针对 usage error（`click.exceptions.UsageError` 及其子类 `BadParameter` / `MissingParameter`，或 `SystemExit(code=2)`）**改写 exit code 为 3**；**禁止** 改写其他 exit code（如 `--help` 的 `SystemExit(code=0)`、`--version` 的 `SystemExit(code=0)`、runner 失败的 `SystemExit(code=2)` —— 区分方式：本提案 CLI 中只有 Click usage exception 会触发 code=2，runner 失败走 `typer.Exit(2)` 显式构造且发生在 try 内部业务路径之外）。互斥校验失败由命令体显式 `typer.Exit(code=3)` 抛出（不依赖 Click usage 改写）。

**只读命令；允许 EUID==0**：与 `hostlens inspectors list/show` / `hostlens target list` 一致；**禁止** 加 EUID==0 检查。`--intent` 路径同为只读巡检入口，同样允许 EUID==0。

#### 场景:`--help` 输出含全部参数

- **当** 执行 `hostlens inspect --help`
- **那么** 输出必须含 `--inspector` / `--intent` / `--output` / `--format` / `--parameters` / `--allow-privileged` / `--timeout` 7 个选项名

#### 场景:`--help` 退出码必须为 0（不被 usage 改写误伤）

- **当** 执行 `hostlens inspect --help`
- **那么** exit code 必须为 0；**禁止** 被 Typer usage exit 改写逻辑误判为 usage error 改成 3

#### 场景:缺位置参数 target 报错

- **当** 执行 `hostlens inspect`（无 target）
- **那么** stderr 必须含 `Missing argument 'TARGET'` 且 exit code 必须为 3（CLI 入口包裹 Typer usage 错误并改写为 3，与全局退出码方案对齐）

#### 场景:缺 --inspector 且缺 --intent 报错

- **当** 执行 `hostlens inspect local-host`（既无 --inspector 也无 --intent）
- **那么** exit code 必须为 3 且 stderr 含一行说明必须提供 `--inspector` 或 `--intent` 之一

#### 场景:--inspector 与 --intent 同时提供报错

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --intent "检查健康"`
- **那么** exit code 必须为 3 且 stderr 含一行说明 `--inspector` 与 `--intent` 互斥

#### 场景:--format 不在 md/json 报错

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --format html`
- **那么** stderr 必须含 `Invalid value for '--format' / '-f'` 且 exit code 必须为 3

#### 场景:允许 EUID==0 运行

- **当** 以 root 用户（EUID==0）执行 `hostlens inspect local-host --inspector hello.echo`
- **那么** 命令**不** 因 root 而 refuse；正常流程继续

#### 场景:--timeout 0 或负数被拒绝

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 0`
- **那么** exit code 必须为 3 + stderr 含 `invalid --timeout:` 前缀（提示 `must be in [1, 300]`）

#### 场景:--timeout 必须经 CollectSpec 重构注入触发 validation

- **当** CLI 收到合法 `--timeout 5` 调度 inspector 时，**禁止**直接 `manifest.collect.model_copy(update={"timeout_seconds": 5})`（绕过 validation）；**必须**用 `CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": 5})` 构造新 CollectSpec
- **那么** 注入路径触发 Field(ge=1, le=300) validation；测试用 monkeypatch 假设 CLI 上限校验绕过（manually patch CLI 的 [1, 300] 校验），传 `--timeout 9999`，期望下游 CollectSpec 构造 raise `pydantic.ValidationError`

#### 场景:--timeout 超过上限被拒绝

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 301`
- **那么** exit code 必须为 3 + stderr 含 `invalid --timeout:` 前缀（提示 `must be in [1, 300]`）

#### 场景:--timeout 上限 300 边界值接受

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 300`
- **那么** CLI 接受参数（exit 0 / 1 / 2 取决于 runner 结果，**不**为 3 拒绝）

#### 场景:--timeout 通过 CollectSpec 重构注入到 runner

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 5`，CLI 在内部用 `from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": 5}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})` 构造新 manifest 实例（**不**用 `manifest.collect.model_copy(update=...)`——后者会绕过 Pydantic validation）
- **那么** runner 收到的 `manifest_for_run.collect.timeout_seconds == 5`；InspectorRegistry 中的原始 manifest 实例**未被修改**（CLI 只做临时拷贝；原 manifest 仍走 InspectorRegistry 提供的引用）；CollectSpec 构造过程触发 `Field(ge=1, le=300)` validation 作为防御纵深

#### 场景:--timeout 与 --intent 组合被忽略并提示

- **当** 执行 `hostlens inspect local-host --intent "检查健康" --timeout 5`
- **那么** `--timeout` 不影响 Planner 路径（Agent 工具超时由 ToolSpec 固定），CLI 在 stderr 提示一行 `--timeout 对 --intent 模式无效，已忽略`，不报错、不改变退出码逻辑

### 需求:`hostlens inspect` 退出码必须语义化 4 值

`hostlens inspect` 命令退出码必须**恰好** 取以下 4 个值之一：

- `0`：所有 finding `severity <= "warning"` **且** `inspector_result.status == "ok"`（用户视角：巡检通过）
- `1`：`inspector_result.status == "ok"` **且** 至少一个 finding `severity == "critical"`（用户视角：业务问题）
- `2`：`inspector_result.status != "ok"`（`timeout` / `target_unreachable` / `requires_unmet` / `exception`）—— **runner 内部失败优先于 critical finding**（即使同时有 critical finding 也返回 2）
- `3`：参数 / 配置错误（target / inspector 未找到、`--parameters` JSON 解析失败、`--parameters` 文件读取失败、`--output` 文件写入失败、Typer usage error 经包裹改写、`Report.from_inspector_results` 触发空 inspector_results 的 invariant ValueError）。**注**：`Report` model_validator 触发 finished_at < started_at（如系统时间倒流）归 exit 2 而非 3——理由：started_at / finished_at 由 CLI 内部计时器写入，**用户无法直接影响**，model_validator 失败是环境 / runtime 异常而非 usage 错

退出码冲突优先级：`3 > 2 > 1 > 0`（用户错最优先，然后 runner 失败，然后业务 finding，最后 healthy）。

**stdout 与 stderr 分离**：渲染后的 Report 必须写入 stdout（或 `--output` 文件）；所有错误信息 / warning（如 evidence 字节数过大）写入 stderr；**不** 写入 stdout 的非 Report 内容（避免 `hostlens inspect ... > report.md` 把日志混入文件）。

#### 场景:healthy 退出 0

- **当** 跑 `hello.echo` 在 `local-host`，得到 InspectorResult(status="ok", findings=[Finding(severity="info", ...)])
- **那么** CLI exit code 必须为 0

#### 场景:critical finding 退出 1

- **当** 跑 inspector 得到 InspectorResult(status="ok", findings=[Finding(severity="critical", ...), Finding(severity="info", ...)])
- **那么** CLI exit code 必须为 1

#### 场景:warning finding 仍退出 0

- **当** 得到 InspectorResult(status="ok", findings=[Finding(severity="warning", ...)])
- **那么** CLI exit code 必须为 0

#### 场景:status=timeout 退出 2

- **当** 得到 InspectorResult(status="timeout", error="collect.command exceeded 60 seconds")
- **那么** CLI exit code 必须为 2 且 stdout 仍输出完整 Report（含 inspector_result 的 timeout 状态）

#### 场景:status=target_unreachable 退出 2

- **当** 得到 InspectorResult(status="target_unreachable", error="ssh_connection_lost")
- **那么** CLI exit code 必须为 2

#### 场景:status=requires_unmet 退出 2

- **当** 得到 InspectorResult(status="requires_unmet", missing=["nginx"])
- **那么** CLI exit code 必须为 2

#### 场景:status=exception 退出 2

- **当** 得到 InspectorResult(status="exception", error="parse_failed: ...")
- **那么** CLI exit code 必须为 2

#### 场景:runner 失败优先于 critical finding

- **当** 得到 InspectorResult(status="timeout", findings=[Finding(severity="critical", ...)])（理论场景）
- **那么** CLI exit code 必须为 2（不是 1）

#### 场景:target 未找到退出 3

- **当** 执行 `hostlens inspect ghost-host --inspector hello.echo`（ghost-host 不在 TargetRegistry）
- **那么** CLI exit code 必须为 3 且 stderr 含 `target not found: ghost-host; run 'hostlens target list' to see registered targets`

#### 场景:inspector 未找到退出 3

- **当** 执行 `hostlens inspect local-host --inspector nonexistent.foo`
- **那么** CLI exit code 必须为 3 且 stderr 含 `inspector not found: nonexistent.foo; run 'hostlens inspectors list' to see available inspectors`

#### 场景:--parameters JSON 解析失败退出 3

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --parameters 'not json'`
- **那么** CLI exit code 必须为 3 且 stderr 含 `invalid --parameters:` 前缀

#### 场景:--parameters 文件读取失败退出 3

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --parameters @/nonexistent/path.json`
- **那么** CLI exit code 必须为 3 且 stderr 含 `failed to read --parameters file:` 前缀

#### 场景:--output 文件写入失败退出 3

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --output /nonexistent/dir/out.md`（目录不存在）
- **那么** CLI exit code 必须为 3 且 stderr 含 `failed to write output:` 前缀；stdout 不应包含 partial Report 内容

#### 场景:Report finished_at < started_at 退出 2

- **当** 由于系统时间倒流导致 `Report.from_inspector_results` 触发 finished_at < started_at 的 ValidationError
- **那么** CLI exit code 必须为 2 且 stderr 含 `internal: report validation failed:` 前缀

### 需求:`hostlens inspect` 必须以 stdout/stderr 分离与默认 stdout 模式工作

`hostlens inspect` 必须遵守 POSIX CLI 输出分离约定：

- **数据写 stdout**：渲染后的 Report（md 或 json）；当 `--output FILE` 指定时改写到文件 + stdout 不写任何 Report 内容
- **错误信息写 stderr**：参数错误、target / inspector 未找到、写文件失败、运行时 warning（如 evidence 字节数 > 8MB）等
- **缺省 stdout 输出**：未指定 `--output` 时，渲染结果**必须** 写到 stdout（不写 `~/.cache/hostlens/...` 等隐式路径）

**禁止** 输出 Python traceback 到用户面（CLI 边界必须包装异常为简短一行错误 + stderr）。

#### 场景:缺省输出 stdout

- **当** 执行 `hostlens inspect local-host --inspector hello.echo`（无 --output）
- **那么** stdout 必须含 md 渲染的 Report；stderr 必须为空（无错误时）

#### 场景:--output 写文件且 stdout 不重复

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --output /tmp/report.md`
- **那么** `/tmp/report.md` 必须含 md Report；stdout 必须为空（无 Report 内容；可有 progress hint 但**不** 写 Report 字节）

#### 场景:错误信息走 stderr

- **当** 执行 `hostlens inspect ghost --inspector hello.echo`（target 不存在）
- **那么** stderr 含错误描述；stdout 必须为空

#### 场景:不输出 Python traceback

- **当** runner 内部某处抛出 `RuntimeError` 被 CLI 边界捕获
- **那么** stderr 含简短一行 `internal: <error_kind>: <brief_msg>`；**不** 含 `Traceback (most recent call last):` 或 `File "..."` 路径

#### 场景:大 Report 触发 stderr warning（>8 MiB）

- **当** `Report.total_evidence_bytes() > 8 MiB` 时执行 `hostlens inspect ...`
- **那么** stderr 必须含一行 `warning: report evidence is <N>.<M> MiB (threshold 8 MiB); output may be large`；exit code 仍由 InspectorResult 决定（warning 不改变 exit code）；stdout 仍输出完整 Report（不截断、不阻塞）

### 需求:`hostlens inspect` 必须支持 `--parameters` 的 JSON inline 与文件引用两种形式

`--parameters` 选项必须接受两种语法：

- **JSON inline**：以 `{` 开头的字符串，按 JSON 解析；如 `--parameters '{"host": "db.prod"}'`
- **文件引用**：以 `@` 开头的字符串，剩余部分作为文件路径；读取文件后按 JSON 解析；如 `--parameters @./params.json`

不以 `{` 或 `@` 开头的值视为参数错误（exit 3）。

解析结果（dict）传入 `InspectorRunner.run(manifest, target, parameters=<dict>)`；runner 内部按 inspector manifest 的 `parameters` JSON Schema 做 runtime validation（runtime 校验失败导致 InspectorResult.status="exception" → CLI exit 2，**不** 是 exit 3）。

#### 场景:JSON inline 形式解析成功

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --parameters '{"k": "v"}'`
- **那么** runner 必须收到 `parameters={"k": "v"}`

#### 场景:文件引用形式解析成功

- **当** `/tmp/params.json` 含 `{"k": "v"}`；执行 `hostlens inspect local-host --inspector hello.echo --parameters @/tmp/params.json`
- **那么** runner 必须收到 `parameters={"k": "v"}`

#### 场景:不以 { 或 @ 开头的值被拒绝

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --parameters 'plain text'`
- **那么** CLI exit code 3 且 stderr 含 `invalid --parameters: must start with '{' (inline JSON) or '@' (file path)`

#### 场景:runtime parameter validation 失败归 exit 2

- **当** inspector manifest 要求 `parameters.host` 必填，但调用时 `--parameters '{}'`
- **那么** runner 返回 status="exception"；CLI exit code 2（**不** 是 exit 3——参数解析在 CLI 层成功，runtime 校验失败属于 runner 内部失败）

### 需求:`hostlens inspect` 必须按 schema_version 锁定 Report 输出格式

CLI 内部构造 `Report` 必须用 `Report.from_inspector_results(...)`（不直接 `Report(**dict)`）；该工厂方法锁定 `schema_version="1.0"`。

**保证**：所有 `hostlens inspect --format json` 输出 JSON 必须含字段 `"schema_version": "1.0"`；M3 提案修改 `schema_version` Literal 为 `["1.0", "1.1"]` 时本需求 MODIFIED 反映 CLI 选项 / 默认版本。

#### 场景:json 输出含 schema_version 1.0

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --format json`
- **那么** 输出 JSON `json.loads(stdout)` 后 `data["schema_version"] == "1.0"` 必须为 True

#### 场景:md 输出 meta 表含 schema_version 1.0

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --format md`
- **那么** stdout 输出含字符串 `schema_version` 且对应 Value 列含 `1.0`

### 需求:`hostlens inspect` 集成测试必须覆盖 demo path 第 4 / 5 / 6 / 7 步

`tests/cli/test_inspect.py` 必须包含至少以下 4 个集成测试用例（驱动真实 InspectorRegistry 装配 + TargetRegistry + LocalTarget）：

- `test_inspect_hello_echo_md_to_stdout_exit_0`：跑 `hello.echo` 在 LocalTarget，断言 stdout 含标题 / meta 表 / `info: 1` summary，exit 0
- `test_inspect_hello_echo_json_to_file_exit_0`：跑 `hello.echo` 在 LocalTarget，`--format json --output <tmp>`，断言文件存在 + `json.loads()` schema_version=1.0，exit 0
- `test_inspect_nonexistent_inspector_exit_3`：`--inspector nonexistent.foo`，断言 stderr 含 `inspector not found:`，exit 3
- `test_inspect_nonexistent_target_exit_3`：positional `ghost-host`，断言 stderr 含 `target not found:`，exit 3

至少 1 个测试用例必须使用 syrupy snapshot 断言 markdown 渲染的字节级输出（容忍 report_id / timestamp 字段；用 syrupy serializer 把这两个字段替换为 `<UUID>` / `<TIMESTAMP>` 后比对）。

#### 场景:test_inspect_hello_echo_md_to_stdout_exit_0 通过

- **当** 跑 `pytest tests/cli/test_inspect.py::test_inspect_hello_echo_md_to_stdout_exit_0 -v`
- **那么** 必须通过

#### 场景:test_inspect_hello_echo_json_to_file_exit_0 通过

- **当** 跑 `pytest tests/cli/test_inspect.py::test_inspect_hello_echo_json_to_file_exit_0 -v`
- **那么** 必须通过

#### 场景:syrupy snapshot 测试通过

- **当** 跑 `pytest tests/cli/test_inspect.py -k snapshot -v`
- **那么** snapshot 测试必须通过；首次运行后续测试应稳定（report_id / timestamp 已脱敏）

### 需求:`hostlens inspect --intent` 必须装配并运行 PlannerAgent，实时进度走 stderr、报告走 stdout

`--intent` 路径必须用 `create_backend(settings)` + 注册了默认工具的 `ToolRegistry` + 产出含 target/inspector registry 的 `ToolContext` 的 context_factory 装配 `PlannerAgent`，并以一个 CLI 端 observer（实现 `LoopObserver` Protocol）调用 `PlannerAgent.run(intent, observer=...)`。Planner 返回后，编排层必须给 findings 盖稳定 id（见 diagnostician-agent 能力），再装配并运行 `DiagnosticianAgent`（复用同一 backend + 受限诊断师注册表 + 固定 target；backend 仍只注入 loop），同样以 CLI 端 observer 透传实时进度。`create_backend` 必须**只调用一次**，Diagnostician 复用 Planner 的同一 backend 实例 —— 不存在二次配置失败点（backend 未配置只在 Planner 装配前发生一次）。盖章与诊断使用的 `InspectorRegistry` 必须与 Planner 跑时同一实例。backend 禁止进入任一 context_factory 产出的 `ToolContext`（ADR-008）。

实时进度（Agent 逐轮的工具调用与每轮返回的 assistant 文本，**非** token 级流式）必须渲染到 **stderr**；Planner 与 Diagnostician 两段进度都必须走 stderr。最终报告必须输出到 **stdout**（或 `--output` 指定文件）。二者必须分离，使脚本消费 stdout 时不被进度输出污染。`--intent` 字符串只能作为模型的 user message，禁止进入任何 shell/命令渲染路径。CLI 边界必须把任何未预期异常（含从 loop 透传上来的非可重试 backend 错误，如 `CassetteMiss`；含盖章时 inspector 已卸载的 fail-loud）包成一行 `internal: <kind>: <msg>` → exit 2，不泄露 Python traceback。

#### 场景:实时进度与报告分流

- **当** `--intent` 运行且 Planner 与 Diagnostician 各调用了若干工具
- **那么** stderr 必须出现两段逐轮/逐工具的实时进度，stdout 必须只含最终报告内容

#### 场景:backend 未配置报配置错误

- **当** `--intent` 运行但 backend 未配置（如缺 `ANTHROPIC_API_KEY`，`create_backend` 抛 `ConfigError`）
- **那么** 必须 exit 3 并在 stderr 给出一行配置错误提示（指向 `hostlens doctor`），不泄露 traceback

### 需求:`hostlens inspect --intent` 必须输出 narrative + findings 摘要 + 根因假设 + 遥测，支持 md/json

`--intent` 路径必须按 `--format` 渲染 `DiagnosticianResult`：md 模式输出诊断 narrative（markdown；**降级路径下 narrative 可能为空字符串，渲染必须容忍空 narrative —— 不报错、不渲染空标题**）+ `## Findings` 摘要（severity / message / tags，来自 `DiagnosticianResult.findings` 顶层 canonical 集合）+ **`## 根因假设` 章节**（每条含 description / confidence / 关联的 finding 证据 / suggested_actions；hypotheses 为空时显示 `_暂无根因假设_` 占位）+ 一行 loop 遥测（turns / terminal_status / token usage）；json 模式输出 `DiagnosticianResult` 的 JSON 序列化（含 narrative / findings(顶层带 id，权威) / hypotheses / status / planner_result(其内嵌 findings 为未盖章原件、非权威) / diagnostician_loop(可能为 null)）。**禁止**组装 `reporting.models.Report`（本提案 Scope-Core，不产忠实 Report）。findings 为空时 md 模式只输出 narrative + 根因假设占位 + 遥测，不报错。

#### 场景:md 模式输出综述、findings 摘要与根因假设

- **当** `--intent --format md` 且诊断师产出了若干根因假设
- **那么** stdout 必须含诊断 narrative、findings 摘要、`## 根因假设` 章节（每条含证据与建议动作），并附 terminal_status / token usage 遥测行

#### 场景:无根因假设时显示占位

- **当** `--intent --format md` 但诊断师未产出任何根因假设
- **那么** stdout 的 `## 根因假设` 章节必须显示 `_暂无根因假设_` 占位，其余内容正常输出，不报错

#### 场景:降级致 narrative 为空时渲染容忍

- **当** `--intent --format md` 但诊断（或 Planner）降级使 `DiagnosticianResult.narrative` 为空字符串
- **那么** md 渲染必须不报错、不输出空的 narrative 标题，仍输出 findings 摘要 + 根因假设占位 + 遥测行

#### 场景:json 模式输出可解析的 DiagnosticianResult

- **当** `--intent --format json`
- **那么** stdout 必须是 `DiagnosticianResult` 的合法 JSON（含 narrative / findings / hypotheses / status / planner_result / diagnostician_loop），可被 `DiagnosticianResult.model_validate_json` 往返解析；下游必须以顶层 `findings` 为权威

### 需求:`hostlens inspect --intent` 退出码沿用 4 值语义并由 DiagnosticianResult 映射

`--intent` 路径必须按 `DiagnosticianResult` 映射退出码（与 `--inspector` 路径同一 4 值语义，优先级 3>2>1>0）：`status=ok` 且无 critical finding → `0`；`status=ok` 且 ≥1 `severity=="critical"` finding → `1`；`status` ∈ 降级集合（`degraded_max_turns` / `degraded_token_budget` / `degraded_no_planner` / `degraded_rate_limited` / `empty_response`，无论该值来自 Planner 降级还是 reconcile）→ `2`；参数互斥违规 / backend 配置错误 / `--output` 写失败 / `--format` 非法 → `3`。（注：`failed_api_unavailable` **不在** `status` 降级集合内 —— `DiagnosticianResult.status` 类型是 `ReportStatus`，故意不含该值；它只经下面的 no-result 特例处理。）**Planner `terminal_status=failed_api_unavailable` 的特例**：不产 `DiagnosticianResult`，CLI 必须走 no-result 降级路径 —— stderr 给出一行降级原因、exit `2`、stdout 为空（无 findings 可输出，禁止伪造空报告骨架）。Planner 或 Diagnostician 降级时 CLI 禁止重试（重试单一收口在 loop），有 `DiagnosticianResult` 时仍输出已收集的 findings、（可能为空的）hypotheses 与（可能为空的）narrative。

**消费约定**：脚本消费方判定成功**必须看退出码（0/1）**，**禁止**用「stdout 是否为空」判断 —— no-result 路径 stdout 空 + exit 2，而健康巡检也可能 findings 空但有 narrative/占位（stdout 非空）+ exit 0，二者 stdout 空/非空与成败不构成对应。

**实现约束（不破坏 demo）**：`--intent` 路径的退出码映射与渲染必须由**新增的** DiagnosticianResult 版函数（`_compute_diag_exit_code` / `render_diagnostician_result`）承担；既有 `_compute_intent_exit_code(PlannerResult)` / `render_planner_result` 被 `cli/demo.py` 的 `demo run` 共享，**禁止**改其签名（`--inspector` 路径与 `demo run` 行为不变）。

#### 场景:健康巡检退出 0

- **当** `--intent` 运行结果 `status=ok` 且无 critical finding
- **那么** 必须 exit 0

#### 场景:critical finding 退出 1

- **当** `status=ok` 且收集到至少一条 `severity=="critical"` 的 finding
- **那么** 必须 exit 1

#### 场景:诊断师空响应 empty_response 退出 2

- **当** `DiagnosticianResult.status=empty_response`（诊断师空响应，区别于 end_turn 带文本无假设的 `ok`）
- **那么** 必须 exit 2，stdout 仍输出 findings + 根因假设占位 +（可能为空的）narrative

#### 场景:reconcile 产生的 degraded_no_planner 退出 2

- **当** `DiagnosticianResult.status=degraded_no_planner`（来自 Planner=ok + 诊断师调工具前 API 不可达的 reconcile）
- **那么** 必须 exit 2，stdout 仍输出 Planner 已收集的 findings + 根因假设占位

#### 场景:降级退出 2 且仍输出部分结果

- **当** `status` 为 `degraded_max_turns` / `degraded_token_budget` 等且存在 `DiagnosticianResult`
- **那么** 必须 exit 2，stderr 标注降级原因，stdout 仍输出已收集的 findings、（可能为空的）hypotheses 与（可能为空的）narrative，CLI 未重试

#### 场景:Planner API 不可达无结果退出 2

- **当** Planner `terminal_status=failed_api_unavailable`，不产 `DiagnosticianResult`
- **那么** 必须 exit 2，stderr 给出一行降级原因，stdout 为空（不伪造空报告骨架），CLI 未重试
