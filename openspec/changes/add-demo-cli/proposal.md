## 为什么

Hostlens 的核心卖点是"理解意图 + 推理诊断"的 Agent，但今天一个第一次接触项目的人（面试官 / 评估者 / 潜在用户）**无法在不配置真实 SSH 主机和付费 Anthropic API key 的前提下看到 Agent 跑起来**。M2.8 已经为 8 个真实故障场景交付了"双回放层"（`ReplayTarget` fixture + `PlaybackBackend` cassette），让端到端管线可以离线确定性回放——但这套资产住在 `tests/` 里、只被 snapshot 测试消费，不进 pip 包，普通用户碰不到。

M2.9 把这套已验证资产**提升为产品资产**，包一个面向人类的 `hostlens demo` 命令：干净机器 `pip install` 后一条命令、5 分钟内、零外部依赖（无 SSH / 无付费 API / 无真实生产访问），就能离线 reproduce 出一份带根因假设的 markdown 报告，并在终端实时看到 Agent 调度 Inspector、收集 finding、生成 narrative 的过程。这是把"这是个 Agent 而不是脚本"直接展示出来的最低成本路径。

## 变更内容

- 新增 `hostlens demo run <scenario>` 命令：对打包的 incident 场景跑完整 Planner Agent 管线（`ReplayTarget` + `PlaybackBackend`），默认在 stderr 流式展示 Agent 进度（复用 `RichLiveObserver`），完整报告渲染到 stdout。
- 新增 `hostlens demo list` 子命令：列出可用场景 + 每个一句话描述，输出来自同一 scenario registry（SOT）。
- 8 套 incident replay 资产（fixture + cassette）从 `tests/fixtures/` **物理迁移**到产品包 `src/hostlens/demo/scenarios/` 成为单一 SOT；`tests/incidents` 反向从 `hostlens.demo` 取资产路径，消除"测试一份、demo 一份"的漂移风险。
- demo 命令**完全自包含**：进程内自行装配 `TargetsConfig`（`ReplayEntry` 指向打包 fixture），不读取用户 `~/.config/hostlens/targets.yaml`，安装后开箱即跑。
- `pyproject.toml` 声明 package-data，把 `*.json` / `*.jsonl` 资产打进 wheel。
- 场景命名以 snake_case key 为唯一 SOT，CLI 接受 kebab-case 并纯归一化（`-`→`_`，如 `cpu-saturation` → `cpu_saturation`）；不维护别名表，故 `cpu-spike` 这类**不同词**不解析到 `cpu_saturation`（它不是 kebab 变体）。

**非破坏性**：不改 Agent loop / Planner 行为 / Inspector 语义 / replay 内容；`incident-pack` 的 spec 级行为契约不变（每场景仍有 cassette + fixture + snapshot 测试、仍在 replay 模式跑），变的只是资产物理位置。

## 功能 (Capabilities)

### 新增功能
- `demo-cli-command`: `hostlens demo run <scenario>` / `hostlens demo list` 的命令契约——参数、flag、退出码、stdout/stderr 分离、场景命名归一化、自包含装配（不读用户配置）、package-data 资产访问（`importlib.resources`）。

### 修改功能
<!-- 无。incident-pack 的 spec 只规定"每场景提供 cassette + fixture + snapshot 测试且在 replay 模式运行"，未规定资产物理路径；资产从 tests/ 迁到 src/ 不改变该 spec 级行为，故不开 modified delta。详见 Impact。 -->

## 影响

**对外契约影响（CLI 命令）**：

- 新增 CLI 命令 `hostlens demo run <scenario>` 与 `hostlens demo list`（挂到现有 Typer `app`，与 `inspect` / `target` / `inspectors` 并列）。无现有命令签名变更。

**代码影响**：

- 新增 `src/hostlens/demo/`：scenario registry（场景 key → 描述 + intent + 资产路径）、资产桥接层（`importlib.resources.files(...)` + `as_file()`）、进程内装配函数（产出 `PlannerAgent`）。
- 新增 `src/hostlens/cli/demo.py`：Typer 命令。**自写**自包含装配函数（镜像 `tests/incidents/_harness.build_incident_planner_over_fixture` 的形状——手动构造 `PlaybackBackend` + `TargetsConfig(ReplayEntry)` + `register_default_tools` + `PlannerAgent`）；**不复用** `cli/_intent.py::build_planner`（它强绑 `create_backend` 与真实 Anthropic backend，与 demo 零 key 前提冲突）。真正可复用的是三个纯件：`RichLiveObserver` / `render_planner_result` / `_compute_intent_exit_code`（后者只判 0/1/2；exit 3 的 caller 分支 demo 自写，见 design D8）。
- 修改 `src/hostlens/cli/__init__.py`：注册 demo 子命令。
- 修改 `pyproject.toml`：hatchling wheel 含 `hostlens.demo.scenarios` 下 `*.json` / `*.jsonl`（hatchling 机制，非 setuptools package-data；`.gitignore` 已核查不排除这些；多半默认即含，仅烟测失败才加 `force-include`，见 design D9）。

**测试资产迁移（触碰已归档 M2.8 的测试装配，但不改其行为契约）**：

- `tests/fixtures/incident_pack/<key>.json` 与 `tests/fixtures/cassettes/incident_<key>.jsonl` 物理迁移到 `src/hostlens/demo/scenarios/<key>/`。
- 消费点迁移（**全部** reader + **两个** writer + **一个 CI 校验器**，缺一即 incident 测试红 / 重录复活双份 / 公开 cassette 脱离脱敏门）：`_harness.py`（`FIXTURES_DIR` reader / `CASSETTES_DIR` writer-dir 来源）、`_scenarios.py`（`intent` 来源）、`tests/conftest.py::llm_cassette`（`incident_` 前缀 cassette 的 reader=replay / writer=record 双模式分流）、`test_drift.py`（硬编码 fixture basename）、**两个 writer**：`_generate.py`（scripted 重录）+ `llm_cassette` record 分支（真 key 重录）、**CI 校验器** `scripts/cassette_lint.py`（脱敏门，扫描范围须覆盖新位置，否则公开 cassette 漏扫）——**仅来源/写目标反转 + 引用更新**，Agent 行为 / 场景语义 / replay 内容 / snapshot 断言一律不动。
- **字段边界**：只有 `{key, intent, 一句话描述}` 进 `hostlens.demo` registry（产品关注）；`{narrative, inspectors, main_stdout}` 留 `_scenarios.py`（fixture 录制原料，纯测试，禁止污染产品包）。intent SOT 为 registry，迁移走有序逐字节相等门（见 design 决议 + tasks 组 4）。
- `incident-pack` spec **不需要** delta：其需求是"每场景提供 cassette + fixture + snapshot 测试且 replay 模式运行"，资产物理位置不在该 spec 的契约范围内，迁移后这些需求依然全部成立。
- **新增耦合（显式记录）**：反转后 `hostlens.demo` registry 若有 import error，8 个 incident 测试在 collection 阶段全挂（不只 demo 测试）。可接受，但不藏在"纯路径反转"下。

**显式无影响**：

- 不触及 Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest。
- 不触及 `ReplayTarget` / `PlaybackBackend` 的构造契约（二者只吃文件系统路径，已验证；demo 层用 `importlib.resources.as_file()` 桥接，不拓宽它们的输入类型）。

**依赖影响**：无新增第三方依赖（`importlib.resources` 是标准库）。

## 场景数量（review 时可调）

**推荐首发全部 8 个场景**（cpu_saturation / memory_oom / disk_inode / systemd_failed / error_burst / fd_exhaustion / dependency_unreachable / tls_expiry）。

理由：资产桥接模式（`importlib.resources.as_file()` + 进程内装配）一旦在第一个场景（cpu_saturation）端到端验证通过，其余 7 个场景的边际成本只是"copy 2 个文件 + 加 1 行 registry"，纯机械；而 demo 的展示价值随场景广度增长——评估者想看 disk / memory / TLS 不止 CPU。tasks 结构上把"场景 1 跑通证明 bridge 模式"作为独立里程碑，"场景 2-8 批量迁移"作为后续 task，即使中途收敛到更少场景也已有可用 demo。**最终场景数在 review tasks 时拍板。**

## 非目标 (Non-Goals)

- **不做 live demo**：不真跑 `stress-ng` / `yes >/dev/null` 等破坏性负载、不连真实 LocalTarget / SSH、不要求 Anthropic API key。`inspect --intent` 已能对真实 target 跑 live；live"真制造故障"降级为 `examples/README` 的文字说明，不进 demo CLI scope。
- **不要求任何外部配置或密钥**：demo 不读 `~/.config/hostlens/targets.yaml`、不需要环境变量。
- **不改 Agent 行为**：不动 Agent loop / Planner 决策逻辑 / Inspector 采集语义 / 场景 narrative / replay 录制内容。
- **不维护双份资产**：迁移后 `src/hostlens/demo/scenarios/` 是唯一 SOT，tests 反向引用；不允许 tests 与 demo 各存一份。**迁移必须同步更新 `_generate.py` 重录写目标**（否则重录复活双份，见 design 决议）。
- **不引入别名表**：场景命名用 snake_case key 单一 SOT + kebab 归一化规则，不维护第二套独立命名映射。
- **工具/Inspector 时钟必须冻结**：`sampling_window` 类 inspector（如 `error_burst`）的命令带时间戳，不冻结则 fixture miss → degraded。demo 渲染路径（`PlannerResult` → `render_planner_result`）**本身不含任何 timestamp/duration/run_id**，故无"报告时钟"一说，冻结工具时钟后输出即确定（见 design D6）。
- **不把 stdout 改成 Rich UI**：进度流在 stderr，报告在 stdout，保持可重定向 / 可测试。
- **不拓宽 `ReplayTarget` / `PlaybackBackend` 输入契约**：不为 demo 让它们接受 bytes/stream，桥接逻辑留在 demo 层（`ExitStack` 持有 `as_file` 上下文到 run 结束，见 design D2）。

## Failure Modes

1. **打包资产缺失 / wheel 未包含**（hatchling 未含资产，`importlib.resources` 找不到 `*.json` / `*.jsonl`）→ demo 在装配**之前**的 pre-flight 资产解析检查 fail-loud，单行 stderr 报"missing scenario asset: <key>"，退出码 3（用法/配置错误类）；绝不输出 Python traceback。built-wheel 烟测防此回归。
2. **未知场景 key**（用户 typo，归一化后仍不在 registry）→ pre-flight 检查 stderr 提示"unknown scenario: <key>; run 'hostlens demo list'"，退出码 3。
3. **cassette miss**（**Agent 行为与录制漂移**，运行期 `messages_create` 找不到匹配 record）→ `PlaybackBackend` 抛 `CassetteMiss`，CLI 边界包成单行 `internal: CassetteMiss: ...`，退出码 2。注意：这与"资产被破坏"不同——cassette JSON 格式坏是**装配期** `ValueError`（也 exit 2 但不同阶段），见 design D8 映射表。
4. **ReplayTarget fixture miss**（fixture 与 Inspector command 漂移）→ `ReplayMiss` 被 `ToolsAdapter.dispatch` 吸收为 finding 缺失，报告 degraded（terminal_status 非 ok），退出码 2；M2.8 已有 loud-failure 契约，demo 复用。committed 资产下不会发生，仅资产损坏/漂移时退化。
5. **临时路径桥接失败**（`importlib.resources.as_file()` 在只读 / 受限文件系统落临时文件失败）→ 单行 stderr `internal: <kind>: ...`，退出码 2。
6. **`--output` 写失败**（路径不可写）→ 单行 stderr，退出码 3（复用 `_emit_output` 风格）。

## Operational Limits

- **并发**：demo 单场景单次运行，无并发预算诉求；走与 `inspect --intent` 相同的单 target 串行管线。
- **内存**：cassette / fixture 均为 KB 级小文件，全量载入内存无压力；报告 evidence 沿用 inspect 的 8 MiB 软告警阈值。
- **超时**：replay 无网络 IO，Inspector 命令在 ReplayTarget 内即时返回；整体运行受 cassette 回合数限制（每场景 2 回合），实测远低于"5 分钟"验收上限。无新增超时配置。

## Security & Secrets

- **不引入任何新密钥**：demo 纯离线回放，不需要 Anthropic API key、不需要 target 凭据。
- **资产合规（持续门，非一次性复核）**：迁移的 fixture / cassette 已通过 M2.8 的 cassette 提交脱敏门（无 IPv4 字面量 / 无 `/home`·`/Users` 路径 / 无 email / 无带 flagged 后缀的 FQDN，端点用单标签服务名）。迁入 `src/` 后这些资产成为**公开 wheel 内容**（`pip install` 后落用户磁盘），脱敏需求从"测试 fixture 别泄密"**升级**为"公开发布物别泄密"——故 `scripts/cassette_lint.py`（CI 持续脱敏门）的扫描范围**必须扩到新位置**（tasks 2.8），否则迁移后这 8 个公开 cassette 静默脱离持续门控，日后重录引入密钥不再被 CI 拦截。这是 reader/writer 之外的第三类消费者，迁移必须覆盖。
- **攻击面**：demo 无远程连接、不读用户配置，攻击面较 `inspect` 更小；沿用 inspect 的"只读命令容忍 EUID==0"姿态。唯一写操作是用户显式选择的 `-o/--output`——在 `EUID==0`（sudo）下 `-o` 可能产出 root-owned 文件（全局 CLAUDE.md 警示的反模式）；对只读 eval 命令属低风险，**文档说明而不硬拦**（与 inspect `-o` 一致）。

## Cost / Quota Impact

- **零 token 消耗 / 零 API 调用**：demo 走 `PlaybackBackend` 回放 committed cassette，**不触达 Anthropic API**，对配额无任何影响。这正是 demo 相对 `inspect --intent`（live 真 API）的核心价值。
- **"零 API"的可测保证**（不靠话术）：demo 集成测试断言装配产物的 backend 是 `PlaybackBackend` 实例、且在 `ANTHROPIC_API_KEY` 缺失下仍 exit 0——把结构性保证落成断言，而非空泛声明（见 tasks 5.2）。
- **Prompt caching**：不适用（回放路径不发起真实 API 请求，`PlaybackBackend.capabilities.prompt_caching=False`，Agent loop 据 capability 不注入 `cache_control`，符合 §4.8）。

## Demo Path

本提案交付的就是 Demo Path 本身：

```bash
pip install -e ".[dev]"
hostlens demo list                    # 看 8 个可选场景
hostlens demo run cpu_saturation      # 或 cpu-saturation（kebab 纯归一化 -→_）
#   stderr: RichLiveObserver 流式展示 Planner 调 linux.cpu.top_processes
#           / linux.system.load_avg → 收集 findings → 生成 narrative
#   stdout: 完整 markdown 报告（narrative 含根因假设 + findings 列表 + 一行 token/turns 遥测）
hostlens demo run cpu_saturation --quiet -o report.md   # 关进度，写文件
```

验收：干净 macOS / Linux 上述路径在无网络下完成并 exit 0，零外部依赖（无 SSH / 无付费 API / 无真实生产访问 / 无用户配置）。（"5 分钟"是面向人类的 README 上限，replay 实际亚秒级；测试不断言墙钟时间，断言"无网络下完成 + exit 0"。）
