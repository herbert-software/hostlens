## 新增需求

### 需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数

`hostlens inspect <target> --inspector <name>` 必须接受以下参数（Typer 定义）：

- 位置参数 `target: str`（必填）：target 名；从 `TargetRegistry` 查询
- 选项 `--inspector / -i <name>: str`（必填）：inspector 名；从 `InspectorRegistry` 查询
- 选项 `--output / -o <FILE>: Path | None = None`：输出文件路径；缺省 stdout
- 选项 `--format / -f <md|json>: Literal["md", "json"] = "md"`：输出格式
- 选项 `--parameters / -p <JSON>: str | None = None`：JSON 字符串或 `@<path>` 文件引用（如 `@./params.json`）；缺省传 `{}` 给 InspectorRunner
- 选项 `--allow-privileged: bool = False`：opt-in 允许跑 `privilege != "none"` 的 Inspector
- 选项 `--timeout <SECONDS>: int | None = None`：单 Inspector 超时（整数秒；不接受 float）；缺省 None = 不覆盖 manifest `collect.timeout_seconds`；**值校验**：`1 <= value <= 300`（与 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 严格一致），不在范围 → exit 3 + stderr `invalid --timeout: must be in [1, 300]`；**实现路径**：`InspectorRunner.run()` 不接受 timeout 覆盖参数（保持其 inspector-plugin-system spec 中的契约不变），CLI 必须在 dispatch 前**重构 CollectSpec 让 Pydantic validation 生效**：`from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})` 再传给 runner —— **禁止**直接用 `manifest.collect.model_copy(update={"timeout_seconds": cli_timeout})` （Pydantic v2 `model_copy(update=...)` **不**触发字段 validation，会让 [1, 300] 外的值静默写入）；CollectSpec 的 `Field(ge=1, le=300)` 在构造时会触发 validation 作为防御纵深第二道；**禁止** 改 `InspectorRunner.run()` 签名或修改 archived `inspector-plugin-system` spec 的 runner 契约

**`--help` 输出必须** 列出全部参数 + 简要描述 + 退出码语义（4 行：`0: healthy / 1: critical finding / 2: runner failure / 3: usage error`）。

**Typer 默认 usage exit 转换**：Typer 自身对 `Missing argument` / `Missing option` / `Invalid value for` 等 usage 错误默认 exit 2 —— 这与本提案 exit 2 = runner failure 定义冲突。**CLI 入口必须** 包裹 Typer app 调用，**仅**针对 usage error（`click.exceptions.UsageError` 及其子类 `BadParameter` / `MissingParameter`，或 `SystemExit(code=2)`）**改写 exit code 为 3**；**禁止** 改写其他 exit code（如 `--help` 的 `SystemExit(code=0)`、`--version` 的 `SystemExit(code=0)`、runner 失败的 `SystemExit(code=2)` —— 区分方式：本提案 CLI 中只有 Click usage exception 会触发 code=2，runner 失败走 `typer.Exit(2)` 显式构造且发生在 try 内部业务路径之外）：

```python
# 示意（src/hostlens/cli/__init__.py 或 inspect.py 入口）
import click
import sys
try:
    inspect_app()
except (click.UsageError, click.BadParameter, click.MissingParameter):
    sys.exit(3)
except SystemExit as e:
    # 仅当来自 Click 的 usage error 时改写；--help / --version 的 exit 0 不改写；
    # runner 失败的 exit 2 由业务路径主动构造 typer.Exit(2)，不经此路径
    if e.code == 2 and getattr(e, "_from_click_usage", False):
        sys.exit(3)
    raise
```

**实现注**：Click 的 UsageError 通过 ctx.fail() 抛 SystemExit(2)；为安全起见，包裹层用 `click.exceptions.UsageError` 异常类型而非 `SystemExit` code 数字识别（避免误判 typer.Exit(2)）。`--help` / `--version` 走 Click 内部 `HelpOption` / `--version` 直接 SystemExit(0)，不经过 `UsageError`，因此不会被误改写。

**只读命令；允许 EUID==0**：与 `hostlens inspectors list/show` / `hostlens target list` 一致；**禁止** 加 EUID==0 检查。

#### 场景:`--help` 输出含全部参数

- **当** 执行 `hostlens inspect --help`
- **那么** 输出必须含 `--inspector` / `--output` / `--format` / `--parameters` / `--allow-privileged` / `--timeout` 6 个选项名

#### 场景:`--help` 退出码必须为 0（不被 usage 改写误伤）

- **当** 执行 `hostlens inspect --help`
- **那么** exit code 必须为 0；**禁止** 被 Typer usage exit 改写逻辑误判为 usage error 改成 3

#### 场景:缺位置参数 target 报错

- **当** 执行 `hostlens inspect`（无 target）
- **那么** stderr 必须含 `Missing argument 'TARGET'` 且 exit code 必须为 3（CLI 入口包裹 Typer usage 错误并改写为 3，与全局退出码方案对齐）

#### 场景:缺 --inspector 报错

- **当** 执行 `hostlens inspect local-host`（无 --inspector）
- **那么** stderr 必须含 `Missing option '--inspector' / '-i'` 且 exit code 必须为 3

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
