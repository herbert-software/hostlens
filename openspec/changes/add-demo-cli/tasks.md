# Tasks — add-demo-cli (M2.9)

> **排序原则（review 修订后）**：资产迁移 + tests 引用反转必须**原子先做（一次性迁全 8 套）**，让 incident 测试始终绿；再在其上建 demo CLI、用 cpu_saturation 证明 bridge 模式。理由见 design 迁移计划——`_harness` 对全部 8 场景用同一路径常量，只迁 1 套会让其余 7 套的 incident 测试在窗口期变红。
>
> **资产数 vs demo 暴露数**：8 套资产必须全部迁入（incident 测试依赖，非可调）；"demo 暴露几个场景"是 registry 子集决定（review 可调，见 proposal「场景数量」）。
>
> **组间硬依赖**：组 1 的代码（registry/桥接/装配函数）可先写，但**端到端验证依赖组 2.1 资产迁入**（资产不在则 `reader_path` 的 `is_file()` 全 False、装配跑不通）；组 3 CLI 同理依赖组 2 完成。实施顺序：1（写代码）→ 2（迁资产+反转+incident 绿）→ 3（CLI 端到端）→ 4 → 5。

## 1. demo 包骨架 + 资产桥接层（reader / writer 双路径）

- [x] 1.1 建 `src/hostlens/demo/__init__.py` 与 `src/hostlens/demo/scenarios/__init__.py`（package 化，使 `importlib.resources.files("hostlens.demo.scenarios")` 可用）
- [x] 1.2 写 scenario registry（`src/hostlens/demo/registry.py`）：snake_case key → {一句话描述, intent} 强类型映射（字段边界：**仅** `{key, intent, 描述}` 进 registry，`{narrative, inspectors, main_stdout}` 留 `_scenarios.py`）；`list_scenarios()` / `get_scenario(key)`；key 归一化 `-`→`_`（design D5）
- [x] 1.3 写桥接层，暴露**两个 helper**（design D2 reader/writer 分离）：`reader_path(key, kind) -> ContextManager[Path]`（`files(...).joinpath(...)` + `as_file()`，给运行期/测试读，zip-safe）；`source_tree_path(key, kind) -> Path`（`Path(find_spec("hostlens.demo").origin).parent / "scenarios" / key / ...`，给 `_generate.py` 写、开发期断言）。basename 映射：`kind="fixture"→fixture.json` / `kind="cassette"→cassette.jsonl`。pre-flight 存在性用 `files(...).joinpath(name).is_file()`（**禁** `os.path.exists`）。禁 `__file__` 相对路径
- [x] 1.4 写自包含装配函数（**净新增**，镜像 `tests/incidents/_harness.build_incident_planner_over_fixture` 形状，**不复用** `build_planner`——它绑 `create_backend`）：进程内 `TargetsConfig(version="1", targets=[ReplayEntry])` + `build_registry_from_config` + `build_registry_from_search_paths([])` + `register_default_tools(clock=FROZEN_DT)`（工具时钟冻结，design D6）+ `PlaybackBackend` → `PlannerAgent`；`ReplayTarget`/`PlaybackBackend` 构造在 `ExitStack` 持有的 reader `as_file` 路径上完成、ExitStack 持到 run 结束（design D2）；不调 `load_settings`/`load_targets_config`/`create_backend`（design D7）

## 2. 原子迁移全 8 套资产 + 反转 tests 引用（incident 测试保持绿）

- [x] 2.1 `git mv` 迁移**全部 8 套**资产（保留历史，basename 改名）：`tests/fixtures/incident_pack/<key>.json` → `src/hostlens/demo/scenarios/<key>/fixture.json`；`tests/fixtures/cassettes/incident_<key>.jsonl` → `src/hostlens/demo/scenarios/<key>/cassette.jsonl`（8 场景：cpu_saturation / memory_oom / disk_inode / systemd_failed / error_burst / fd_exhaustion / dependency_unreachable / tls_expiry）
- [x] 2.2 8 场景注册进 registry；**逐字节门 = 迁移 PR 内一个 CI 可跑、随后显式删除的一次性 test**（不进 runtime/registry 模块——写 registry 顶层会在 2.4 删旧常量后引用悬空 → incident 测试 collection 全挂）：加 `tests/.../test_intent_byte_equal_migration.py`，参数化 8 场景断言 `registry.get(key).intent == <旧常量字面量 **inline 进该 test**，不 import _scenarios>`（intent 含 ASCII 标点中文，任何归一化破坏 snapshot key，必须逐字节；inline 字面量使 2.4 删 `_scenarios` 常量后该 test 仍自洽）；CI 跑过此 test（非人肉记录）后，**本 PR 最后一个 task 显式删除该 test 文件**；复核迁移资产仍过提交脱敏门（内容不变）
- [x] 2.3 反转 `tests/incidents/_harness.py` 路径常量（两者都 → `source_tree_path`，但职责不同）：`FIXTURES_DIR` 是 **reader**（`test_drift` 读 `<key>/fixture.json`、`build_incident_planner` 喂 `ReplayEntry`），`CASSETTES_DIR` 是 **writer dir**（`_harness` 自身**不读 cassette**——cassette 由 conftest `llm_cassette` 读；`CASSETTES_DIR` 仅被 `_generate.py` 当写目标 import）。装配逻辑/冻结时钟/断言不动
- [x] 2.4 反转 `tests/incidents/_scenarios.py`：8 个 intent 旧常量删除后，`intent` 字段代理到 registry（registry 为 intent 唯一 SOT）；`{narrative, inspectors, main_stdout}` 本地保留
- [x] 2.5 反转 `tests/incidents/_generate.py`（**writer #1**）：重录写目标改为 `source_tree_path`（写源码树真路径，不可用 reader 的 as_file 只读临时副本；重录用 scripted FakeBackend 零 key）；basename `<key>.json`/`incident_<key>.jsonl` → `<key>/fixture.json`/`<key>/cassette.jsonl`
- [x] 2.6 **反转 `tests/conftest.py::llm_cassette` 的 cassette 解析（覆盖 replay + record 两模式，对 `incident_` 前缀 name）**：conftest 内部 strip `incident_` 前缀得 bare key（call sites 的 9 个 `llm_cassette("incident_<key>")` **保持不变**——映射在 conftest 内做，不留"或"）；**replay 模式 → bridge `reader_path(key,"cassette")`**（as_file，fixture 持 context 到测试结束）；**record 模式（writer #2）→ `source_tree_path(key,"cassette")`**（写源码树真路径，**绝不可写 as_file 只读临时副本**，否则 `RecordingBackend` 重录静默丢失 + 复活旧位置双份）；非 incident cassette（`planner_health_check`/`list_inspectors_demo`/`deepseek_*`）留 `tests/fixtures/cassettes/` 不动。`test_drift.py:62` 硬编码 `FIXTURES_DIR/"cpu_saturation.json"` 改 `cpu_saturation/fixture.json`
- [x] 2.7 跑全量 `tests/incidents/` snapshot 测试 + `ReplayTarget.misses == []` 漂移守卫，确认 8 个 snapshot 断言**完全不变**（回归判据）。验证两条 writer 写对位置：(a) **CI 门**：`_generate.py` 重录 round-trip（scripted FakeBackend 零 key，CI 可跑）写进 `src/` 源码树新路径且 8 snapshot 不变；(b) **CI 门（纯路径断言，不真录）**：断言 conftest record 分支对 `incident_` 前缀解析到 `source_tree_path`（源码树）而非 `reader_path`（as_file 临时）；(c) **本地手动（需真 key，CI 不跑）**：`HOSTLENS_LLM_MODE=record pytest tests/incidents/test_cpu_saturation.py` 真 round-trip
- [x] 2.8 **反转 CI 脱敏门**（review 命中的第六类消费者：CI 校验器，非 reader/writer）：`scripts/cassette_lint.py:48` 硬编码 `DEFAULT_CASSETTE_DIR = tests/fixtures/cassettes` + `:70` 非递归 `glob("*.jsonl")`——迁移后 8 个 incident cassette 落 `src/hostlens/demo/scenarios/<key>/cassette.jsonl`，**静默脱离 CI 密钥扫描门**（而它们此刻变成公开 wheel 内容、脱敏需求升级）。扩 lint 扫描范围覆盖 `src/hostlens/demo/scenarios/**/cassette.jsonl`（多 root 或 `rglob`，scoped 子树不盲扫 repo root）。**关键：CI `ci.yml:40` 是无参调用、`test_existing_cassettes_pass_scan_mode` 也是 `_run_lint([])` 无参——所以必须改 lint 的默认行为（`DEFAULT_CASSETTE_DIR` 扩成多 root / 默认含 src 子树），不能只加可传参 + CI 传参却把无参测试留着**（否则无参测试仍绿但静默不覆盖新位置，正是本 task 要堵的"门自欺"）。lint 测试调用范围必须与 `ci.yml:40` 实际调用范围一致；更新 `test_existing_cassettes_pass_scan_mode` 断言迁移后 incident cassette 新位置确实被默认扫描覆盖

## 3. 建 demo CLI（cpu_saturation 证明 bridge，再验证其余暴露场景）

- [x] 3.1 写 `src/hostlens/cli/demo.py` `demo run <scenario>`：调 1.4 自包含装配（**不复用 build_planner**）+ `RichLiveObserver`（`--quiet`/`--no-progress` 同一 bool 两拼写）+ `render_planner_result`（`-f md|json`）+ `-o/--output`；**装配前 pre-flight 资产解析**（registry 命中 + `files(...).is_file()`），失败→exit 3；退出码 0/1/2 复用 `_compute_intent_exit_code`、exit 3 自写，按 design D8 异常→退出码映射表分流；CLI 边界包意外异常为单行 `internal:`，绝不漏 traceback
- [x] 3.2 写 `demo list` 子命令，输出来自 registry（spec 单一 SOT）；空 registry → "无可用场景" + exit 0
- [x] 3.3 在 `src/hostlens/cli/__init__.py` 注册 demo 子命令（`app.add_typer`，与 inspect/target/inspectors 并列）
- [x] 3.4 手动验证 bridge 端到端：`hostlens demo run cpu_saturation` 与 `cpu-saturation`（kebab 纯归一化）跑通出报告、stderr 有进度、stdout 是报告；再验证其余暴露场景（含 `error_burst` 在工具时钟冻结下正确回放）

## 4. demo 自身测试 + packaging

- [x] 4.1 写 demo 集成测试：冻结工具时钟，断言 **demo 真实渲染路径 `render_planner_result(result, "md")`** 对一份 **demo 自有 snapshot**（**不复用** `project_planner_result`——它 import 不了且是 demo 不走的函数，design D6）；同时断言 `target.misses == []`（结构性证明 cassette request key 匹配，design D7 不变量）；无报告时钟可注入，冻结工具时钟即确定
- [x] 4.2 写 demo CLI 测试（覆盖 spec 各场景）：未知场景→exit 3 单行无 traceback；资产缺失(pre-flight `is_file()` False)→exit 3 **vs** 装配期 cassette JSON 损坏(`ValueError`)→exit 2 区分；运行期 `CassetteMiss`→exit 2；`-o` 写不可写路径→exit 3；`--quiet`/`--no-progress` 两拼写一致；非 TTY(管道)下 stdout 纯报告不含进度；kebab 归一化；`demo list` 空 registry→exit 0；md/json 退出码一致；**结构性"不触达 API"断言**：装配产物 backend 是 `PlaybackBackend`、缺 `ANTHROPIC_API_KEY` 仍 exit 0（proposal Cost 节）
- [x] 4.3 改 `pyproject.toml`：用 **hatchling** 机制让 `hostlens.demo.scenarios` 下 `*.json`/`*.jsonl` 进 wheel（**禁 setuptools `package-data`/`MANIFEST.in`**）；先验证 hatchling 默认是否已含（多半已含，`.gitignore` 已核查不排除），**仅 4.4 烟测失败才**加 `[tool.hatch.build.targets.wheel].force-include`/`artifacts`（design D9）
- [x] 4.4 built wheel 烟测：`pip install dist/*.whl` 到干净 venv（非 editable）后 `hostlens demo run cpu_saturation` 跑通（唯一真正证明资产进 wheel 的门，design D9）

## 5. 文档 + 验收

- [x] 5.1 写 `examples/README.md`：每场景一段说明 + 一行 `hostlens demo run <key>` + 期望输出片段；**门面 demo 首推有明确根因链的场景**（`cpu_saturation`/`memory_oom`/`dependency_unreachable`——narrative 给量化根因假设），`error_burst` 等计数器型场景保留但不作首推（避免展示最像 Zabbix 规则匹配的那个）；live"真制造故障"（stress-ng 等）作为文字说明放此处（不进 CLI scope，proposal 非目标）
- [x] 5.2 README 顶部加 demo 用法（GIF 录制可作 follow-up，非阻塞）
- [x] 5.2b 清理 stale 路径文档（迁移后旧路径失效）：`grep -rn "fixtures/incident_pack\|fixtures/cassettes/incident_"` 全仓清零，更新 `docs/operations/inspectors.md`（manifest 示例 fixture 路径）、`tests/incidents/README.md`、`tests/fixtures/cassettes/README.md`（incident 布局描述）到 `src/hostlens/demo/scenarios/<key>/`
- [x] 5.3 验收：干净 macOS/Linux 上 `pip install -e ".[dev]" && hostlens demo run cpu_saturation` 在无网络下完成并 exit 0、离线出带根因假设的报告，零外部依赖（"5 分钟"是 README 上限，不作墙钟断言）
- [x] 5.4 `openspec-cn validate add-demo-cli --strict` 通过；`mypy --strict` + lint 全绿
