## 新增需求

### 需求:`hostlens demo run <scenario>` 必须离线回放一个打包场景并渲染报告

`hostlens demo run <scenario>` 命令必须对一个打包的 incident 场景跑完整 Planner Agent 管线（`ReplayTarget` + `PlaybackBackend`），并把渲染后的报告输出到 stdout。该命令禁止发起任何真实 Anthropic API 调用、禁止建立任何 SSH / 远程连接、禁止要求 Anthropic API key 或 target 凭据。`<scenario>` 必须支持 `md`（默认）与 `json` 两种渲染格式（`-f` / `--format`），并支持 `-o` / `--output FILE` 把报告写入文件而非 stdout。

#### 场景:对已知场景跑通离线回放
- **当** 用户在已安装 Hostlens 的干净机器（无 `~/.config/hostlens/targets.yaml`、无 `ANTHROPIC_API_KEY`）上运行 `hostlens demo run cpu_saturation`
- **那么** 命令必须经回放管线产出该场景对应的 markdown 报告到 stdout，报告含与故障对应 severity 的 finding 与 narrative

#### 场景:不触达 API 的结构性保证（可断言）
- **当** demo 装配完成时检查其 LLM backend，以及在缺失 `ANTHROPIC_API_KEY` 下运行
- **那么** backend 必须是 `PlaybackBackend` 实例（绝不构造 `AnthropicAPIBackend`），且缺 key 不影响运行（仍正常出报告）——以此结构性事实断言"不触达 Anthropic API"，而非仅文档声明

#### 场景:`--output` 写文件
- **当** 用户运行 `hostlens demo run cpu_saturation -o report.md`
- **那么** 渲染后的报告必须写入 `report.md`，stdout 不再输出报告正文

#### 场景:`--output` 写到不可写路径
- **当** 用户运行 `hostlens demo run cpu_saturation -o /不可写路径/report.md`
- **那么** 命令必须向 stderr 输出单行写失败错误并以退出码 3 结束，stdout 不输出报告正文，禁止输出 Python traceback

#### 场景:json 与 md 退出码一致
- **当** 用户对同一场景分别以 `-f md` 与 `-f json` 运行
- **那么** 两次运行的退出码必须一致（退出码由 terminal_status 与 finding severity 决定，与渲染格式无关）

### 需求:`hostlens demo list` 必须列出可用场景

`hostlens demo list` 必须列出所有可用 demo 场景的 snake_case key 与每个场景的一句话描述，输出来源必须是与 `demo run` 共享的同一 scenario registry（单一 SOT），禁止维护第二份场景清单。

#### 场景:列出场景
- **当** 用户运行 `hostlens demo list`
- **那么** 命令必须输出每个可用场景的 key 与一句话描述，且该清单与 `demo run` 能接受的场景集合完全一致

#### 场景:registry 为空时不崩
- **当** scenario registry 注册的场景数为 0（如收敛到极小集或装配异常）
- **那么** `hostlens demo list` 必须输出"无可用场景"提示并以退出码 0 结束，禁止输出 Python traceback

### 需求:场景名必须以 snake_case 为唯一 SOT 并接受 kebab-case 归一化

scenario registry 必须以 snake_case key 作为唯一命名真相源。CLI 必须把用户输入中的 `-` 归一化为 `_` 后再查表，使 `cpu-saturation` 等 kebab-case 写法解析到对应 snake_case key（`cpu_saturation`）。这是纯机械的 `-`→`_` 规则；禁止维护独立别名映射表作为第二命名源（故 `cpu-spike` 这类与 key 不同的词不解析到 `cpu_saturation`）。

#### 场景:kebab-case 输入归一化到 snake_case
- **当** 用户运行 `hostlens demo run cpu-saturation`
- **那么** 命令必须把 `cpu-saturation` 归一化为 `cpu_saturation` 并正常跑通，结果与 `hostlens demo run cpu_saturation` 一致

#### 场景:未知场景报错并指引 list
- **当** 用户运行 `hostlens demo run not-a-scenario`，归一化后仍不在 registry 中
- **那么** 命令必须向 stderr 输出单行错误（含 `unknown scenario` 与指引运行 `hostlens demo list`），以退出码 3 结束，禁止输出 Python traceback

### 需求:`demo run` 默认必须流式展示 Agent 进度且可关闭

`hostlens demo run` 默认必须通过 `RichLiveObserver` 把 Agent 进度（Planner 调用 Inspector、收集 finding、生成 narrative）展示到 stderr，报告正文输出到 stdout（进度与报告必须分流，禁止把进度写到 stdout 污染报告）。`--quiet` 与 `--no-progress` 必须是同一个布尔开关的两个拼写（同一 flag），置位时关闭进度流、仅输出报告。`RichLiveObserver` 在非 TTY（管道 / CI）下自动降级为 plain line 输出，此降级禁止把进度写到 stdout。

#### 场景:进度到 stderr 报告到 stdout 不互相污染
- **当** 用户运行 `hostlens demo run cpu_saturation`（默认开进度）
- **那么** 进度必须只写 stderr，完整报告只写 stdout；即使在非 TTY（管道）下，stdout 也必须是纯报告、不含任何进度字符

#### 场景:`--quiet` / `--no-progress` 关闭进度
- **当** 用户运行 `hostlens demo run cpu_saturation --quiet`（或等价的 `--no-progress`）
- **那么** stderr 必须不出现进度流，stdout 仍输出完整报告，两个拼写行为一致

### 需求:`demo` 命令必须完全自包含，不读取用户配置

`hostlens demo` 命令必须在进程内自行装配 `TargetsConfig`（`ReplayEntry` 指向打包的场景 fixture），禁止读取用户的 `~/.config/hostlens/targets.yaml` 或依赖任何外部 target 配置 / 环境变量。命令必须在没有任何 Hostlens 用户配置的干净机器上开箱即跑。

#### 场景:无用户配置仍可运行
- **当** 系统上不存在 `~/.config/hostlens/targets.yaml` 且未设置任何 Hostlens 相关环境变量，用户运行 `hostlens demo run cpu_saturation`
- **那么** 命令必须正常跑通并产出报告，禁止因缺少用户配置而失败

### 需求:demo 场景资产必须作为 package data 随 wheel 分发并经 `importlib.resources` 访问

demo 场景的 `fixture.json` 与 `cassette.jsonl` 必须位于产品包 `src/hostlens/demo/scenarios/` 内并作为单一 SOT；这些 `*.json` / `*.jsonl` 资产必须随 wheel 分发（通过 hatchling 的 wheel 构建机制——禁止使用 setuptools 的 `package-data` / `MANIFEST.in` 术语，因本项目 build-backend 为 hatchling）。demo 必须通过 `importlib.resources` 定位这些资产（禁止依赖 `__file__` 相对路径），并在需要文件系统路径时用 `importlib.resources.as_file()` 桥接为临时真实路径；桥接上下文必须持续到 `PlannerAgent.run()` 返回（用 `ExitStack`），不得依赖"运行期不再触盘"这一未受 spec 保护的偶然属性。`ReplayTarget` 与 `PlaybackBackend` 的构造输入契约禁止为此被拓宽。

#### 场景:从已安装 wheel 运行
- **当** Hostlens 以非 editable 的 built wheel 安装到 site-packages，用户运行 `hostlens demo run cpu_saturation`
- **那么** 命令必须能定位并载入打包的场景资产并跑通，禁止因资产不在文件系统相对路径而失败

#### 场景:资产缺失时 fail-loud
- **当** 某场景的打包资产缺失或未被打进 wheel，用户运行 `hostlens demo run <该场景>`
- **那么** 命令必须向 stderr 输出单行错误（指明缺失的场景资产）并以退出码 3 结束，禁止输出 Python traceback

### 需求:`demo run` 必须复用 inspect 的 4 值退出码契约

`hostlens demo run` 必须遵循 4 值退出码契约（0/1/2 复用 `_compute_intent_exit_code`，exit 3 为 demo 自写的 caller 边界）：0 表示健康（terminal_status 为 ok 且无 critical finding）、1 表示 ok 且存在至少一个 critical severity finding、2 表示**任何非 ok 的 terminal_status**（closed-set 见 `loop.py::_TerminalStatus`，如 `degraded_no_planner` / `empty_response` 等——不在此枚举编造值）或装配/运行期失败、3 表示用法 / 配置错误。

为可靠区分 exit 3（资产缺失 / 未知场景）与 exit 2（装配/运行期失败），命令必须在装配**之前**执行一次 **pre-flight 资产解析检查**（确认场景归一化后在 registry 中、且资产经 `importlib.resources.files(...).joinpath(name).is_file()` 存在——必须用此 `Traversable` API，禁止 `os.path.exists`/`Path.exists`，后者对 zip-safe wheel 资源误判 False）；pre-flight 失败一律 exit 3。退出码按异常阶段映射：未知场景 / `importlib.resources` 资源缺失（pre-flight）→ 3；cassette JSON 格式坏 / fixture schema 坏（装配期 `ValueError` / `ConfigError`）→ 2；Agent 行为漂移导致运行期 `CassetteMiss` / `ReplayMiss`-degraded → 2；`--output` 写失败 → 3。命令在任何分支均禁止向用户输出 Python traceback；意外异常必须包装为单行 `internal: <kind>: <msg>` 写入 stderr。

#### 场景:critical finding 退出码 1
- **当** 用户运行的场景回放后报告含至少一个 critical severity finding 且 terminal_status 为 ok
- **那么** 命令必须以退出码 1 结束，报告仍完整输出到 stdout

#### 场景:运行期 cassette miss 退出码 2
- **当** Agent 行为与录制漂移，运行期 `messages_create` 找不到匹配 record 而抛 `CassetteMiss`（注：这是运行期失败，不同于"资产被破坏"——后者是装配期 `ValueError`）
- **那么** 命令必须把异常包装为单行 `internal: <kind>: <msg>` 写入 stderr 并以退出码 2 结束，禁止输出 Python traceback

#### 场景:装配期资产损坏退出码 2
- **当** 某场景 cassette 的 JSON 格式被破坏，`PlaybackBackend` 构造期抛 `ValueError`
- **那么** 命令必须以退出码 2 结束（区别于资产**缺失**的 exit 3），单行 stderr，禁止输出 Python traceback
