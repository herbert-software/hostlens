## 上下文

M2.8 已交付"双回放层"：每个 incident 场景有 `ReplayTarget` fixture（execution 层 canned 命令输出）+ `PlaybackBackend` cassette（LLM 层录制响应），由真 `PlannerAgent` → `AgentLoop` → `ToolsAdapter` → `run_inspector` → `InspectorRunner` 管线在冻结时钟下离线确定性回放。这套装配逻辑住在 `tests/incidents/_harness.py` 的 `build_incident_planner()`，资产住在 `tests/fixtures/`，仅被 snapshot 测试消费。

`hostlens inspect --intent "..."`（`cli/inspect.py::_run_intent`）已是面向人类的完整 Planner 管线：`build_planner` 装配 → `PlannerAgent.run(observer=RichLiveObserver())` → `render_planner_result` → 4 值退出码（0 健康 / 1 critical / 2 degraded / 3 用法错误），进度流到 stderr、报告到 stdout。

demo 命令的本质 = `inspect --intent` 的管线 + `_harness` 的回放装配 + 打包资产 + 人类友好外壳。它不是新 Agent 能力，是一次资产产品化 + 命令组装。

**约束**：pip 包 src/ layout 不含 tests/；mypy --strict 无 `Any`；async-first；单一 SOT 哲学反对资产漂移；`ReplayTarget.__init__(name, *, fixture: str|Path)` 与 `PlaybackBackend.__init__(*, cassette_path: Path)` 均只吃文件系统路径（已读码验证）。

## 目标 / 非目标

**目标：**
- 干净机器 `pip install` 后一条 `hostlens demo run <scenario>` 离线出带根因假设的 markdown 报告，5 分钟内、零外部依赖。
- 实时展示 Agent 工作过程（复用 `RichLiveObserver`），体现"这是 Agent 不是脚本"。
- replay 资产单一 SOT，tests 与 demo 不漂移。
- demo 完全自包含，不读用户配置、不需密钥。

**非目标：**
- 见 proposal「非目标」：不做 live、不要求密钥、不改 Agent 行为、不维护双份资产、不建别名表、生产代码不冻结时钟、不改核心类输入契约。

## 决策

### D1：资产归属 —— 产品包为 SOT，tests 反向引用（C 方案）

8 套 fixture+cassette 迁移到 `src/hostlens/demo/scenarios/<key>/`（每场景 `fixture.json` + `cassette.jsonl`），成为唯一 SOT。`tests/incidents/_harness.py` 与 `_scenarios.py` 改为从 `hostlens.demo` 取资产路径与场景元数据。

**理由**：单一 SOT、零漂移、pip 可见，三者只有 C 同时满足。

**替代方案**：
- **A（资产复制进包，tests 各留一份）**：制造双份资产，违反单一 SOT 哲学，迁移后 cassette 重录要改两处必漂移。否决。
- **B（资产留 tests/，demo 引用相对路径）**：`pip install` 到 site-packages 后 tests/ 不可见，验收"干净机器跑通"直接挂。否决。

**对已归档 M2.8 的影响边界（OpenSpec 纪律）**：C 触碰 `_harness.py` / `_scenarios.py`，但 `incident-pack` spec 的需求是"每场景提供 cassette + fixture + snapshot 测试且 replay 模式运行"，未规定资产物理路径——迁移后这些需求全部依然成立，故 **incident-pack 不开 modified delta**。scope 严格限定"路径来源反转 + 资产迁移 + 引用更新"，禁止改 Agent 行为 / 场景语义 / replay 内容 / snapshot 断言。这是把已验证 fixture 产品化，不是重写测试逻辑。

### D2：资产访问 —— `importlib.resources` 桥接，核心类零改动

迁入 `src/` 后资产是已安装包的 package data，不能用 `Path(__file__).parent` 相对路径（zip-safe wheel / site-packages 不可靠）。demo 层用 `importlib.resources.files("hostlens.demo.scenarios")` 定位资源，用 `importlib.resources.as_file()` 把资源落成临时真实路径，再喂给 `ReplayTarget(fixture=...)` / `PlaybackBackend(cassette_path=...)` 的现有签名。打包机制见 D9（hatchling，非 setuptools package-data）。

**理由**：`ReplayTarget` / `PlaybackBackend` 是核心类，为 demo 一个需求让它们接受 bytes/stream 是 CLAUDE.md 反模式（拓宽核心契约服务边缘需求）。桥接逻辑留在 demo 层，核心类一行不改。

**替代方案**：让核心类接受 `bytes` / file-like → 拓宽两个核心类输入契约，污染 execution / backend 层。否决。

**reader 路径 vs writer 路径（关键 —— 两种访问模式不可混为一个常量反转）**：资产有两类消费者，访问模式根本不同：

- **reader（demo 运行期 + tests/incidents 读取）**：用 `importlib.resources.files("hostlens.demo.scenarios")` 定位 → `as_file()` 落临时真实路径喂给 `ReplayTarget`/`PlaybackBackend`。兼容 zip-safe wheel（资源未必是 FS 真文件）。**reader 消费者不止 `_harness`**：incident snapshot 测试实际经 `tests/conftest.py::llm_cassette("incident_<key>")` → `_CASSETTES_DIR/f"{name}.jsonl"` 解析 cassette（9 个 call sites：8 个 `test_<key>.py` + `test_drift.py`），`test_drift.py` 还硬编码读 `FIXTURES_DIR/"cpu_saturation.json"`（旧 basename）。这些都是 reader 消费点，迁移必须一并改（见 tasks 组 2）——`incident_<key>` 前缀的 cassette 走 bridge `reader_path(key,"cassette")`，非 incident cassette（`planner_health_check` / `list_inspectors_demo` / `deepseek_*`）留在 `tests/fixtures/cassettes/` 不动。
- **writer（**两条独立重录路径**）**：必须写**源码树里的可写真路径**（`src/hostlens/demo/scenarios/<key>/{fixture.json,cassette.jsonl}`）。**`as_file()` 给的是只读临时副本，写进去等于丢弃**——writer 绝不能用 reader 的 `as_file` 路径。两个 writer 都要迁：(1) `_generate.py`（scripted FakeBackend 重录 fixture+cassette）；(2) **`tests/conftest.py::llm_cassette` 的 record 分支**（`HOSTLENS_LLM_MODE=record` 跑 `test_<key>.py` 时用真 key 重录 cassette，conftest.py:202-236）——同一 `cassette_path` 变量同时喂 replay 的 `PlaybackBackend`（reader）和 record 的 `RecordingBackend`（writer #2），故对 `incident_` 前缀必须 **按 mode 分叉**：replay→`reader_path`、record→`source_tree_path`。writer 用源码树路径：`Path(importlib.util.find_spec("hostlens.demo").origin).parent / "scenarios" / key / ...`。

- **CI 校验器（第三类消费者，非 reader/writer）**：`scripts/cassette_lint.py`（CI 脱敏门，`.github/workflows/ci.yml` 调）硬编码扫 `tests/fixtures/cassettes/*.jsonl`（非递归）。迁移后 incident cassette 离开该目录 → **静默脱离持续脱敏门**，而资产此刻变公开 wheel 内容、脱敏更重要。必须扩 lint 扫描范围覆盖 `src/hostlens/demo/scenarios/**/cassette.jsonl`（tasks 2.8）。这类消费者是前 4 轮 reader/writer 二分法的盲区——枚举消费者时必须含"谁校验这些资产"，不止"谁读/写"。

故桥接层必须暴露**两个 helper**：`reader_path(key, kind) -> ContextManager[Path]`（as_file，给运行期/测试读）与 `source_tree_path(key, kind) -> Path`（源码树真路径，给两个 writer 写、给开发期断言）。**文件名 schema 同时变化**：`<key>.json` → `<key>/fixture.json`、`incident_<key>.jsonl` → `<key>/cassette.jsonl`——不只是目录常量，basename 拼接也要进桥接层映射；`incident_` 前缀的 name→bare key 映射在 conftest 内 strip（call sites 不变）。**完整消费者清单（迁移须全覆盖）**：reader（`_harness` fixture、conftest replay、`test_drift`、demo 运行期）+ writer（`_generate.py`、conftest record）+ CI 校验器（`cassette_lint.py`）。

**pre-flight 存在性检查用 Traversable，不用 `os.path.exists`**：pre-flight（D8）确认资产存在时用 `files(...).joinpath(name).is_file()`（`Traversable` API，zip-safe、不 materialize），**禁止** `os.path.exists`/`Path(...).exists()`（对 zip 资源误判 False → 假 exit 3，且只在 wheel 烟测才暴露）。镜像 `PlannerAgent._render_system_prompt` 已有的 `files(...).joinpath(...).read_text()` Traversable 访问范式。

**生命周期 —— reader 用 `ExitStack` 持整个 run，不靠"运行期不触盘"不变量**：`as_file()` 临时路径仅在 with 块内有效。已读码核查 `ReplayTarget`/`PlaybackBackend` 当前构造期 eager 载入、运行期纯内存（`exec`/`read_file` 读 dict、`messages_create` 遍历 records、`CassetteMiss.__str__` 对字符串属性调 `Path(...).name` 不 open），故"构造完即释放"当前安全。**但这是当前错误格式化代码的偶然属性，无 spec 禁止将来运行期重读文件**，押在此隐式不变量上脆。因此装配函数用 `contextlib.ExitStack` 在整个 demo run 生命周期内持有所有 reader `as_file()` 上下文（到 `PlannerAgent.run()` 返回才统一 `close()`），临时文件全程有效，消除对"运行期不触盘"的依赖。`ExitStack` 在 CLI 命令 `try/finally` 关闭。

### D3：只做 replay，replay 即默认（张力 2）

demo 仅离线回放，不需 `--replay` flag——命令语义天然代表 replay demo。live（真 stress-ng + 真 LocalTarget + 真 API key）切出 scope，降级为 `examples/README` 文字说明。

**理由**：验收只卡离线；live 依赖真 key 与"5 分钟干净机器"卖点冲突；`inspect --intent` 已能跑 live，live demo 边际价值低。

### D4：流式 observer 默认开启（反推 inspect 一致）

demo run 默认 `PlannerAgent.run(observer=RichLiveObserver())`，进度到 stderr、报告到 stdout，与 `inspect --intent` 同路径同行为。`--quiet` / `--no-progress` 关闭进度只出报告。

**理由**：demo 的核心展示价值就是"看 Agent 思考"，静态报告体现不出 Agent 属性；复用 `cli/_intent.py` 已有件，成本近零。

### D5：场景命名 —— snake_case SOT + kebab 归一化（张力 3）

scenario registry 以 snake_case key 为唯一键；CLI 入口把用户输入的 `-` 替换为 `_` 后查表（`cpu-saturation` → `cpu_saturation`）。不维护独立别名表（故 `cpu-spike` 这类非 kebab 变体的不同词不解析到 `cpu_saturation`）。

**理由**：8 个 key 全可规则化，别名表会在文档 / 测试 / 资产三处制造第二命名源。归一化是单行机械规则。

### D6：只有一个时钟 —— 工具时钟必须冻结；demo 输出本就无时间戳（暗礁 B）

**关键事实（已读码核查，推翻早期"双时钟"臆想）**：demo 渲染路径是 `PlannerAgent.run(intent)` → `PlannerResult` → `render_planner_result`。

- `PlannerResult`（planner.py:61-75）字段只有 `{narrative, findings, loop_result, intent}`；`LoopResult` 也无 datetime 字段。`render_planner_result`（_intent.py:205-233）输出 narrative + findings + 一行 `turns=.. status=.. tokens=..`——**整条输出零时间戳 / 零 duration / 无 run_id**。带 `started_at`/`finished_at` 的 `Report.from_inspector_results` **只在 `inspect` 的 `--inspector`（M1）路径**用，Planner/`--intent` 路径**完全不经过它**。
- 所以**根本不存在"报告元数据时钟"**——早期 D6 的"双时钟、报告时钟真实"是对一个不存在字段的臆想。

**唯一相关的时钟 = 工具/Inspector 时钟**（`register_default_tools(clock=...)` → `InspectorRunner._clock`，runner.py:111/586）：只驱动 `sampling_window` 类 Inspector 把时间戳烘焙进 `collect.command` 字符串。

**决策**：

- **demo 必须冻结工具时钟**（注入 `FROZEN_DT`）—— 不是可选项。8 场景里 `error_burst` 是 `sampling_window` 类，命令带时间戳；fixture 录的是基于 `FROZEN_DT` 的命令 key，工具时钟用真实 `now` 则命令与 key 不匹配 → `ReplayMiss` → 报告 degraded。冻结是让全部 8 场景（含 `error_burst`）正确回放的前提。
- **demo 输出的确定性是免费的**：既然渲染输出无任何时间戳，冻结工具时钟后输出即确定，**无需任何"报告时钟注入"**（那个动作没有对象）。
- **demo 集成测试断言 demo 真实渲染路径 `render_planner_result(result, "md")`，不复用 `project_planner_result`**：后者是 `tests/incidents/_harness.py` 的**测试私有投影**（住在无 `__init__.py` 的目录、产品测试无法干净 import），且与 demo 实际渲染**不是同一函数**——`render_planner_result`（_intent.py:205，demo run 真正调用）findings **不排序**、格式 `- {severity}: {message}`；`project_planner_result` findings 按 `(severity_rank, message)` 排序、格式不同。用后者做 oracle 会测一条 demo 永不走的路径，且掩盖 `render_planner_result` 可能的 finding 顺序抖动。故 demo 集成测试对 `render_planner_result` 的 md 输出建一份 **demo 自有 snapshot**；若 finding 顺序在冻结回放下不稳定，加一条顺序确定性断言（回放下 tool_use 序列录死、Inspector 执行确定，预期稳定，snapshot 即守卫）。
- `error_burst` 命令里嵌的 `FROZEN_DT` 采样窗口字符串是 ReplayTarget **内部匹配的命令 key，不进渲染输出**（snapshot `error_burst.md` 渲染的是 `247 error log entries in the last 300s`，无任何日期）——故早期担心的"evidence 日期不一致"在 Planner 渲染路径下不存在。

**理由**：读码后发现要兼顾的"真实时间戳"既不存在于输出、也不可在此路径产生；问题消失，方案塌缩为"冻结工具时钟"一条。

### D7：自包含装配 —— 进程内构造 TargetsConfig，不读用户配置（暗礁 C）

demo 装配函数**自写**（净新增），镜像 `_harness.build_incident_planner_over_fixture` 的**形状**——**不复用 `cli/_intent.py::build_planner`**（后者第一行 `create_backend(settings)` 强绑真实 Anthropic backend，与 demo 的 `PlaybackBackend` + 零 key 前提直接冲突，见 D8 可复用清单）。装配步骤（全部在 D2 的 `ExitStack` 内完成 `ReplayTarget`/`PlaybackBackend` 构造）：

```
settings = Settings(agent=AgentSettings())                       # 不调 load_settings 读用户配置
TargetsConfig(version="1", targets=[ReplayEntry(                  # version 是 Literal["1"]，必填
    name=..., type="replay", fixture=<as_file 桥接临时路径>)])
build_registry_from_config(...) → target_registry
build_registry_from_search_paths([], settings=settings) → 内置 Inspector
register_default_tools(tool_registry, clock=FROZEN_DT)           # 工具时钟冻结（D6 必需）
PlannerAgent(PlaybackBackend(cassette_path=<as_file 桥接>), tool_registry, settings, context_factory)
```

全程不触 `load_settings()` / `load_targets_config(settings.targets_config_path)` / `create_backend`。

**request-key 不变量（cassette 回放命中的前提）**：cassette 请求 key 由 `model` + `messages` + `tools_count` 哈希（cassette_key.py）。demo 自写装配**必须产出与录制 harness 字节一致的 request key**——同 `model` 默认（`Settings(agent=AgentSettings())` → `primary_model`，与录制时一致）、同默认工具集（`register_default_tools` + 同一内置 Inspector 集）、同冻结工具时钟（→ 同 `tool_result` 内容）。任何偏离（不同 Settings、多注册一个工具、demo 专属 model 默认）→ 运行期静默 `CassetteMiss` → exit 2、无编译期信号。守卫：demo 集成测试断言 `target.misses == []`（结构性证明 request key 匹配）；此不变量显式声明，防未来改 demo `Settings` 被当"无害重构"提交。

**理由**：demo 必须在没有 `~/.config/hostlens/targets.yaml`、没有 `ANTHROPIC_API_KEY` 的干净机器上跑通，这是验收硬条件，作为显式 spec 需求。

### D8：退出码 —— 复用 0/1/2 判定，exit 3 是 demo 自写的 caller 边界

**可复用清单（已读码核查，只此三件是纯函数、入参即用）**：`RichLiveObserver`（无参可注入、写 stderr、非 TTY 自动降级）、`render_planner_result(result, fmt)`（支持 md/json）、`_compute_intent_exit_code(result)`（**只返回 0/1/2**：0 健康 / 1 ok+critical finding / 2 非 ok terminal_status）。**`build_planner` 与 `_run_intent` 不在复用清单**——前者绑 `create_backend`，后者的 `except Exception → exit 2` 边界与 demo 的 exit 3 来源不同。

**exit 3 必须 demo 自写**（inspect 的 exit 3 全来自 `_load_target_registry`/`_resolve_target`/`build_planner`-ConfigError/`_emit_output` 这些 demo 不走的 caller 路径）。demo 的 exit 3 来源：未知场景、资产缺失/未打包、`--output` 写失败。

**pre-flight 资产解析（解 exit 2 vs 3 区分问题）**：装配**之前**先做一次资产解析检查——场景是否在 registry（归一化后）、`importlib.resources` 能否定位、`files(...).joinpath(name).is_file()` 是否为真（Traversable API，**不用** `os.path.exists`，zip-safe，见 D2）。任何 pre-flight 失败 → 单行 `unknown/missing scenario asset` → **exit 3**。只有**装配成功之后**的失败才走 exit 2。异常 → 退出码映射：

| 阶段 | 异常 | 退出码 |
|---|---|---|
| pre-flight | 未知场景（归一化后不在 registry） | 3 |
| pre-flight | `importlib.resources` 找不到资源 / `FileNotFoundError`（资产未打包/缺失） | 3 |
| 装配期 | `PlaybackBackend` `ValueError`（cassette JSON 格式坏）/ `ReplayTarget` `ConfigError`（fixture schema 坏） | 2 |
| 运行期 | `CassetteMiss`（Agent 行为与录制漂移，`messages_create` 找不到匹配 record） | 2 |
| 运行期 | `ReplayMiss` → 报告 degraded（terminal_status 非 ok，经 `_compute_intent_exit_code`） | 2 |
| 输出 | `--output` 写失败（`OSError`，复用 `_emit_output` 风格） | 3 |

CLI 边界把任何意外异常包成单行 `internal: <kind>: <msg>`，绝不漏 traceback。注意：**"资产被破坏"≠"cassette miss"**——前者是装配期 `ValueError`（exit 2），后者是运行期 Agent 漂移（exit 2）；二者都 exit 2 但触发阶段不同，spec 举例不得混淆。

### D9：打包用 hatchling 机制，不是 setuptools package-data

**关键事实（已读码核查）**：`pyproject.toml` build-backend 是 **`hatchling.build`**，wheel target 为 `[tool.hatch.build.targets.wheel] packages = ["src/hostlens"]`。所以：

- 提案禁用 setuptools 术语（`[tool.setuptools.package-data]` / `MANIFEST.in` / `package_data`）——hatchling 会忽略未知 `[tool.setuptools.*]`，照抄 setuptools 写法**不报错也不生效**，直到 wheel 烟测才暴露（与全局 CLAUDE.md "mirror 一个 host 的 schema 给另一个 host" 反模式同构）。
- hatchling 默认包含包目录下的非 `.py` 文件，且**遵守 VCS**。已核查 `.gitignore` **不排除** `src/` 下的 `*.json` / `*.jsonl`（只排 `credentials.json` / `.claude/*.json` 等特定项；现有 `tests/fixtures/cassettes/*.jsonl` 本就被 git 跟踪）——故迁入 `src/hostlens/demo/scenarios/` 的资产**大概率默认即进 wheel**。
- 实现策略：**先验证 hatchling 默认是否已含**（跑 tasks 的 built-wheel 烟测）；**仅当烟测失败**才显式加 `[tool.hatch.build.targets.wheel].force-include` 或 `artifacts`。built-wheel 烟测是唯一能真正证明"装进 wheel"的门。

## 风险 / 权衡

- **[迁移触碰已归档测试装配可能引入回归]** → tasks 把"迁移后全量跑 `tests/incidents/` snapshot 测试 + `ReplayTarget.misses == []` 漂移守卫"作为验收门；snapshot 断言不变是回归是否发生的判据。
- **[wheel 未含资产，源码树能跑但 wheel 装完跑不了]** → 见 D9；built-wheel（非 editable）`demo run` 烟测是验证门；`importlib.resources` 路径正是为此选型。
- **[tests 反向依赖 demo 包 → 测试 collection 耦合]** → 反转后 `_harness`/`_scenarios`/`_generate` 都 `import hostlens.demo`。已核查方向是 tests→src，**不构成 import cycle**（tests 不是被 src import 的包；`pyproject` `pythonpath=["src"]` 保证 demo 可 import）。但新增了硬耦合：**demo registry 若有 import error，8 个 incident 测试在 collection 阶段就全挂**（不只是 demo 测试）。可接受，但显式记录，不藏在"纯路径反转"下。
- **[SOT 语义漂移到 registry 层]** → registry 是 intent 唯一 SOT；逐字节相等门是迁移 PR 内 CI 可跑、随后删除的一次性 test（tasks 2.2）。demo 集成测试用 `render_planner_result` 的 **demo 自有 snapshot**，incident snapshot 用 `project_planner_result`——**两份不同投影各钉自己的渲染路径**（这是 D6 刻意拆分的：两函数 findings 排序/格式不同，复用会测错路径）。这是断言冗余（snapshot 是 derived，非 SOT），不是资产漂移（fixture/cassette 只一份）；改 registry-intent 会被两份 snapshot 同时捕获。**勿将两投影"合并"——会重蹈 R3 修复前的覆辙。**
- **[as_file 临时文件在只读/受限 FS 落盘失败]** → Failure Mode 5 已覆盖，包成单行 internal 错误退出 2；多数环境 tmpdir 可写，属边缘。
- **[场景数膨胀拖慢交付]** → tasks 拆"场景 1 证明 bridge"为里程碑、"2-8 批量"为后续，可在任意场景数收敛。
- **[demo report 渲染路径与 inspect 不一致导致两套渲染]** → 复用 `render_planner_result`（`cli/_intent.py`），不新写渲染器。

## 迁移计划

**关键排序原则（解组 2 里程碑 vs incident 测试持续绿的矛盾）**：资产迁移 + tests 引用反转必须**原子完成（一次性迁全 8 套）**，再在其上建 demo CLI。理由：`_harness` 对全部 8 场景用同一 `FIXTURES_DIR` 常量，若只 `git mv` 1 套、其余 7 套还在 `tests/fixtures`，反转后 `_harness` 找不到那 7 套 → incident 测试在窗口期变红。所以**不**按"先迁 1 个证明、再批量 7 个"拆资产迁移；而是先原子迁全 8 + 反转，让 incident 测试始终绿，**再**用 demo CLI 在 cpu_saturation 上证明 bridge 模式。

**资产数 vs demo 暴露数解耦**：incident 测试依赖全部 8 套资产，故**8 套资产必须全部迁入** `hostlens.demo`（非可调）；"demo 暴露几个场景"是 registry 层的子集决定（review 可调，影响 `demo list`/`demo run` 可选集，不影响资产迁移）。

1. 建 `src/hostlens/demo/`，`git mv` 迁移**全部 8 套**资产到 `scenarios/<key>/{fixture.json,cassette.jsonl}`（保留历史；注意 basename 改名），建 scenario registry（内联 dict）+ 桥接层（reader/writer 两 helper + `ExitStack`）。
2. **逐字节相等 SOT 迁移（每场景立即断言，不留窗口）**：每个场景注册进 registry 时立刻断言 `registry.get(key).intent == <该场景旧 _scenarios.py intent 常量>`（逐字节）；8 个全过后才删旧常量、让 `_scenarios.py.intent` 代理到 registry。
3. 反转 `tests/incidents/_harness.py`（reader-path 来源）、`_scenarios.py`（intent 代理）、`_generate.py`（**writer-path** 写目标 + basename schema），跑全量 incident 测试确认 8 snapshot 断言**不变** + `_generate.py` 重录 round-trip（写进 `src/` 源码树真路径、snapshot 不变）。
4. 加 `cli/demo.py` + 注册；加 demo 自身集成测试（冻结工具时钟，复用 incident 的 `project_planner_result` 投影做 snapshot——无报告时钟可注入，见 D6）。先在 cpu_saturation 证明 bridge 端到端，再验证其余暴露场景。
5. 验证 hatchling 默认含资产（built wheel 烟测）；仅当失败才显式加 `force-include`（D9）。

**回滚**：纯增量 + 资产移动，回滚 = revert PR；资产 `git mv` 可逆。

## 决议（原待解决问题）

### scenario 元数据载体 + 字段边界

- **载体**：registry Python 模块内联 dict（不引 `meta.yaml`）——8 场景内联强类型、`demo list` 直接读、避免再引 YAML 解析点。
- **字段边界（关键，划清产品 vs 测试关注）**：
  - 进 `hostlens.demo` registry（**产品关注**）：`{key, intent, 一句话描述}`。
  - 留 `tests/incidents/_scenarios.py`（**纯测试/录制原料，禁止污染产品包**）：`{narrative, inspectors, main_stdout}`——其中 `main_stdout` 是 fixture 生成器 `_generate.py` 的录制输入，与产品 demo 无关。
  - `_scenarios.py` 的 `intent` 字段改为从 registry 取，其余字段本地保留。

### intent SOT 循环定义 → 单向决议

原表述"registry 从 `_scenarios.py` 取、`_scenarios.py` 又从 registry 取"是循环。决议：**registry 是 intent 唯一 SOT**。迁移按上方迁移计划步骤 2 的有序三步执行，逐字节相等门（`registry.intent == 旧 intent`）保证迁移瞬间不改变 snapshot 输入（intent 含 ASCII 标点的中文，任何归一化都会破坏 snapshot key，故必须逐字节）。

### `_generate.py` 写目标必须同步迁移（漏项补回）

`_generate.py` 重录时把 fixture 写到 `_harness.FIXTURES_DIR`、cassette 写到 `CASSETTES_DIR`。迁移后这两个常量指向 `src/hostlens/demo/scenarios/`，故 `_generate.py` 重录会**自动写进新资产位置**——但必须显式验证：迁移后跑一次 `_generate.py` 重录 → 8 snapshot 不变（round-trip）。否则若 `_generate.py` 仍写旧 `tests/fixtures/` 路径，重录会悄悄复活"双份资产"，正是本提案要消灭的。tasks 显式列此验证。

## 待解决问题

- 无剩余阻塞性待决项（原 3 项已在上方决议）。`error_burst` 的 cosmetic 时钟不一致（D6 残留注记）是 review 可调点，非阻塞。
