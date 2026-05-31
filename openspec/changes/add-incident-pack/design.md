# Design: 最小可用 Incident Pack（M2.8）

## Context

见 [proposal.md](proposal.md)。约束前提：

- LLM 层已有回放（`PlaybackBackend` + `tests/cassettes/` 录制基建，M2.6 交付）。
- 执行层只有 `LocalTarget` / `SSHTarget`，**没有回放层** —— CI 上 `ps`/`free`/`df`/`systemctl` 不可能真复现「CPU 飙高 / OOM / 磁盘满」。
- Inspector schema（M1）已支持 `parameters`（带 pattern 防注入）/ `raw`·`table`·`json`·`kv` 四种 parse format / Finding DSL；**未**支持 `collect.sampling_window`、`hook.py`、`sql_result`。
- 本提案只用 Planner Agent 出报告（无 Diagnostician 根因章节）。

核心张力：要离线、确定性地证明「离线回放管线能复现 8 个真实故障场景的 findings」（model 轮次为脚本化回放，验证的是 Inspector + DSL 诊断与管线串联，**非**证明 Agent 自主决策），必须补齐**执行层回放**，与 LLM 层回放对称，形成「双回放层」。

## Goals / Non-Goals

- Goal：8 个真实场景，每个有 builtin Inspector + ReplayTarget fixture + LLM cassette + snapshot 测试，CI 默认 replay 跑通、零 API 额度、零 SSH、零真机。
- Goal：执行层回放（`ReplayTarget`）做成 shippable（`src/`）而非 test-only，供 M2.9 `demo --replay` 复用。
- Goal：`collect.sampling_window` 落地为向后兼容的可选 manifest 字段。
- Non-Goal：`hostlens demo` CLI / examples 包 / GIF（M2.9）；Diagnostician（M3）；hook.py / sql_result（M6）；真机集成。

## Decisions

### D1 — 双回放层：新增 `ReplayTarget`（执行层），与 `PlaybackBackend`（LLM 层）对称

`src/hostlens/targets/replay.py` 实现 `ExecutionTarget` Protocol：按渲染后的 `cmd` 字符串匹配 fixture 中预录的 `ExecResult`。

- **为何 shippable 而非 test-only golden fixture**：(1) 对齐项目「新 target = 实现一个 Protocol，不改 Inspector」卖点；(2) 走完整 `target → collect → parse → findings` 真实路径，测覆盖高（golden 注入会绕过执行层）；(3) M2.9 `demo --replay` 可直接复用；(4) 与 LLM 层 `PlaybackBackend` 形成对称的「双回放」设计，简历可读性强。
- **冒充既有 target type（避免改 Literal 枚举）**：`InspectorManifest.targets` 是 `Literal["local","ssh"]`、`ExecutionTarget.type` 是 `Literal["local","ssh","docker","k8s"]`，且 runner preflight 第一步校验 `target.type in manifest.targets`。若给 ReplayTarget 一个新 `.type="replay"`，全部 11 个 Inspector 的 preflight 会判 `requires_unmet`。**决策**：ReplayTarget 的**运行时** `.type` 返回它所冒充的既有类型（fixture 顶层 `impersonate: "local"|"ssh"`，默认 `local`），preflight 与 capability 匹配对它透明。配置层的判别值 `type: replay`（TargetsConfig union 新成员）与运行时 `.type` 是两个独立概念，二者均不触碰上述 Literal。替代方案（扩两个 Literal + 加 execution-target / inspector-plugin-system 两份 delta）更重且与「ReplayTarget 本就是代演某真实 type」语义不符 —— 否决。
- **fixture 格式**（JSON，`tests/fixtures/incident_pack/<scenario>.json`，runtime 也可放 `~/.config` 供 demo 用）：
  ```json
  {
    "impersonate": "local",
    "capabilities": ["shell", "file_read"],
    "commands": [
      {"cmd": "command -v ps", "stdout": "/usr/bin/ps", "stderr": "", "exit_code": 0},
      {"cmd": "<完整渲染后的主命令字符串>",
       "stdout": "...", "stderr": "", "exit_code": 0, "duration_seconds": 0.01}
    ],
    "files": {"/proc/sys/fs/file-nr": "..."}
  }
  ```
- **fixture 必须覆盖 preflight 探测命令**：一次 Inspector run 里 runner 会先发 `command -v <binary>`（每个 `requires_binaries`，runner.py `_probe_binary`），再发主 `collect.command`。fixture 的 `commands[]` **必须**预录这些探测命令，否则 preflight 阶段就 `ReplayMiss`。带 `requires_files` 的 Inspector 须预录其探测命令的**精确形式** `[ -r <path> ]`（runner.py 用 exit-code 判可读，**非** `read_file`）；注：本 pack 的 11 个 Inspector 都用 `collect.command` 直接 `cat /proc/...` 取数，预期不声明 `requires_files`，但若声明则 fixture 必须含该 `[ -r <path> ]` 串。
- **匹配键**：`sha256(每行 rstrip 后的渲染命令)` —— 只归一化行尾空白，其余精确匹配。
- **miss 语义（第 2 轮 review 修正）**：`exec`/`read_file` 未命中时抛 `ReplayMiss`（继承 `HostlensError` 而非 `TargetError` —— 语义上 fixture miss 是 infra/编程错误，不是目标传输失败；也避免被 runner `except TargetError → target_unreachable` 吞）。**但**：经完整 `--intent` 管线时，`ReplayMiss` **不会**冒泡成测试红 —— `ToolsAdapter.dispatch`（tools_adapter.py，loop 的工具分发）有 blanket `except Exception`，把 tool handler 的任意异常（除 `ToolPolicyViolation`/`CancelledError` 等显式放行项）catch 成 `is_error` tool_result envelope 喂回模型（设计如此，让模型自适应工具失败）。所以异常会被吸收，drift 表现为「模型多走一轮 → 与 cassette 录的对不上 → `CassetteMiss`」而非干净的 ReplayMiss。
- **漂移检测 = strict-consumption（不依赖异常冒泡）**：`ReplayTarget` **记录每一次 miss 到 `self.misses` 列表**（即使 exec 同时抛了 ReplayMiss）。snapshot 测试在管线跑完后断言 `target.misses == []`。这样无论 loop.py 是否吞掉异常，命令漂移都会让该断言失败（测试红）。这是「响亮失败」的**主**保障；单元层「`ReplayTarget.exec` 直接抛 ReplayMiss」是补充（仅在直接单测 ReplayTarget 时可见）。**绝不回落真实 shell**。
- **capabilities**：从 fixture 顶层 `capabilities` 读取并投影成 `set[Capability]`（systemd 场景须含 `"systemd"`，几乎所有场景须含 `"shell"`），让 `requires_capabilities` preflight 按场景通过/skip。
- **env / secrets**：`exec` 仍接受 `env=` 但**不参与匹配**（8 个场景均无 secrets）；`read_file` 走 fixture `files` 映射，缺失抛 `ReplayMiss`。
- **接线**：`targets.yaml` 支持 `type: replay` + `fixture: <path>`，`build_registry_from_config` 识别；测试直接构造 `ReplayTarget(fixture=...)`。read-only，无 EUID 限制。

### D2 — `collect.sampling_window` + 可注入时钟

- **manifest 字段**（与既有 `timeout_seconds` 命名风格一致）：
  ```yaml
  collect:
    command: journalctl --since "{{ window_start }}" --until "{{ window_end }}" -p err -o json | wc -l
    sampling_window:
      duration_seconds: 300
  ```
- **runner 注入**：声明 `sampling_window` 时，runner 计算 `window_end = now`、`window_start = now - duration_seconds`，把 `window_start` / `window_end`（字符串）与 `window_seconds`（int）注入 **Jinja2 渲染上下文**（现 `_render_command` 上下文为 `**parameters`）与 **Finding DSL 求值上下文**（现为 `{**output, **parameters}`）—— 两处都要加。
- **窗口字符串格式 = journalctl 友好**：用 `"YYYY-MM-DD HH:MM:SS"`（UTC）而**非** `datetime.isoformat()` 的 `2024-01-01T12:00:00+00:00` 形式 —— journalctl `--since/--until` 接受前者、对带 `T`/时区偏移的 ISO 串处理不一致。这保证 fixture 在真机录制时命令真能跑。
- **可注入时钟（关键，决定 snapshot 稳定性）**：`now` 来自注入的 clock（`InspectorRunner` 增加可选 `clock: Callable[[], datetime]` 参数，默认返回真实 UTC `datetime`；既有调用方不传 = 旧行为，签名向后兼容）。理由：window 进入**渲染后的命令字符串**，`ReplayTarget` 精确匹配该字符串 —— `now` 漂移则永远 miss；冻结时钟同时保证 snapshot 稳定。对齐项目「`Date.now()` 不确定性必须可注入」纪律。
- **保留变量名防撞名**：`window_start` / `window_end` / `window_seconds` 为运行时注入的保留名；loader 校验时若 manifest `parameters` 声明了同名字段则拒绝加载（避免 parameter 覆盖注入变量造成歧义）。
- **省略时零行为变化**：未声明 `sampling_window` 时三个变量都不注入，渲染与 DSL 上下文与本 delta 之前完全一致 → 向后兼容，不碰已有 Inspector。

### D3 — 11 个 Inspector 全部留在纯 YAML：`collect.command` 自带「判定友好」输出

不引入 hook.py / sql_result。每个 Inspector 的 `collect.command` 负责把原始输出整理成 parse 层能吃、Finding DSL 能判定的结构；`days_until_expiry`、错误计数等**派生值在 command 内用 shell 算好**，避免 DSL 做日期/复杂运算。

| 场景 | Inspector | parse.format | 关键判定（Finding DSL 概要） |
|---|---|---|---|
| CPU 饱和 | `linux.cpu.top_processes` | table | `for_each` 进程，`cpu_pct > 阈值` |
| | `linux.system.load_avg` | kv | `load1 / ncpu > 阈值` |
| 内存 / OOM | `linux.memory.pressure` | kv | `avail_pct < 阈值` |
| | `linux.kernel.oom_killer` | json | `len(oom_events) > 0`（窗口内 OOM）|
| 磁盘 / inode | `linux.disk.usage` | table | `for_each` 挂载点，`use_pct >= 阈值` |
| | `linux.fs.inode_pressure` | table | `for_each`，`iuse_pct >= 阈值` |
| systemd | `linux.systemd.failed_units` | json | `len(failed) > 0`（`requires_capabilities: [systemd]`）|
| 错误突增 | `log.tail.error_burst` | kv | `error_count > 阈值`（用 `sampling_window`）|
| FD 耗尽 | `linux.process.fd_usage` | kv | `allocated / max > 阈值` |
| 依赖连通 | `net.dependency.tcp_check` | json | `for_each` endpoint，`reachable == false`（参数化 host:port）|
| TLS 过期 | `net.tls.cert_expiry` | json | `days_until_expiry <= critical/warn`（command 内 `date` 算好天数）|

> 具体列名/阈值/默认参数是实现细节，落在 tasks 与 manifest 里；本表只锚定 parse format 与判定形态，证明 8 场景均可在 YAML+DSL 内闭合。

### D4 — Snapshot 测试 = 双回放层组合

每个场景一个测试（`tests/incidents/test_<scenario>.py`）：

1. `target = ReplayTarget(fixture="incident_pack/<scenario>.json")`（冻结时钟注入 runner）
2. `backend = PlaybackBackend(cassette="incident_<scenario>.jsonl")`
3. **不经** `inspect_cmd`/`_run_intent` CLI 入口（CliRunner 拿不到 target 引用，且 `_run_intent` 内部从磁盘读 `targets.yaml` 自建 registry、无注入缝）—— 测试直接用预构造的 `TargetRegistry`（含上面的 `ReplayTarget`）+ `build_planner(...)` 装配 `PlannerAgent`，以便持有 `target` 引用做 step 5 断言；用真实意图（如「检查这台机器 CPU 为什么飙高」）跑 `planner.run`
4. 比对**确定性投影**（见下）== `tests/incidents/snapshots/<scenario>.md`
5. 断言 `target.misses == []`（strict-consumption，见 D1）+ Agent tool_use 序列含该场景核心 Inspector

**为什么不比对 `render_planner_result` 的终端输出（修正第 2 轮 review）**：`cli/_intent.py: render_planner_result` 有两重非确定性：(a) 报告面板含 `Duration: {report.duration_s:.2f}s` —— wall-clock 实测耗时，回放下不确定；(b) 整条输出走 **Rich**（`Panel`/`Table`/`Markdown`，含 box-drawing 字符、按终端宽度换行、可能 ANSI），逐字节比对必 flaky。`reporting.render_markdown` 更糟（`run_id=uuid4()` / 时间戳 / `duration_seconds`）。

**改为比对确定性投影**：snapshot 测试用测试内一个**确定性 helper** 渲染以下三块（全部在回放下字节稳定，**显式排除** duration / Rich 装饰 / run_id / 时间戳）：
- **叙事**：`loop_result.final_text`（来自 cassette，确定）
- **findings**：从 `PlannerResult.findings`（扁平 `list[Finding]`）取**真实字段** `(severity, message, tags)`，按 `(severity_rank, message)` **稳定排序**后渲染（`severity_rank = {critical:0, warning:1, info:2}` 显式映射 —— 因 `Severity(str, Enum)` 默认按字符串字面比较会得到反直觉的 `critical/info/warning` 字母序，事故报告按严重度降序更可读；映射后仍完全确定）。**注意**：`Finding` 模型只有 `severity`/`message`/`evidence`/`tags` —— **无 `title`、无 `inspector_name`**（condensation 时 `inspector_name` 在 `RunInspectorOutput` 顶层、不在每个 Finding 上，已被 `findings.extend(...)` 丢弃）；投影**不要**引用这两个不存在的字段。投影端这一次显式排序使 snapshot **不依赖** Inspector 内部 finding 顺序（= fixture stdout 行序）或管线收集顺序 —— 实施时**不要**在管线中途排序/重排，统一在投影 helper 里排。"哪个 Inspector 被调用"由 step 5 的 tool_use 序列断言单独覆盖，不靠 findings 投影
- **token**（可选）：`loop_result.usage_totals.input_tokens` / `.output_tokens`（`LoopResult.usage_totals: LoopUsage`，字段名是 `input_tokens`/`output_tokens`，**非** `usage.tokens_in/out`；来自 cassette usage，确定）

> 注：不依赖 Rich 输出 = 测得的是「回放出的报告内容（findings + 叙事）」而非「终端排版」，正是本提案要验证的东西。若未来想测 Rich 渲染本身，另起测试并固定 `Console(width=..., no_color=True, force_terminal=False)`。

录制流程：用 M2.6 `HOSTLENS_LLM_MODE=record` + 真 key 录 cassette；ReplayTarget fixture 通过**跑一遍真实 Inspector 捕获其实际 exec 的全部命令**（preflight 探测 + 主命令）采集，避免人工漏录。**录制须在 Linux 目标上进行**（见 Risks 的 `date` 跨平台条）。

### D5 — 冻结时钟经工具装配边界注入（实现期决策，Codex 评审）

D2 要求 `sampling_window` 命令在回放下确定性，需把冻结时钟注入到 `InspectorRunner`。但 group 1 只给 `InspectorRunner.__init__` 加了 `clock` 参数，未接线到 Agent 路径：`run_inspector_handler` 在 tool dispatch 内部构造 runner，`ToolContext` 是 ADR-008 锁定的六字段集（禁止扩字段）。

**决策**：给 `register_default_tools(registry, *, clock=None)` 加可选时钟参数；传入时注册一个 clock-bound 的 `run_inspector` ToolSpec（handler 闭包把时钟透传给 `InspectorRunner`），默认（`clock=None`）注册既有 module-level spec、行为不变。时钟经**工具装配边界**注入，`ToolContext` 字段集不变。

**否决的替代**：(a) 给 `ToolContext` 加第 7 个 `clock` 字段 —— 动了 §4.10 锁定的六字段集 + 需 tool-registry-capability-layer spec delta（超出本提案声明的 affected specs）；(b) 测试内 monkeypatch runner 模块的 `datetime` —— 与本项目「时钟可注入而非 patch」纪律相悖，且 `datetime` 是不可变 C 类型、runner 用 `self._clock()` 而非模块级 `datetime.now`，patch 在 Agent 路径根本不生效。Option C 不碰锁定契约、留在 inspector-plugin-system scope，胜出。

### D6 — Agent 表面数组参数经 JSON 编码字符串传入（实现期决策，Codex 评审）

`net.dependency.tcp_check` / `net.tls.cert_expiry` 的 `endpoints` 是数组参数，但 Agent 表面 `RunInspectorInput.parameters` 是 M2-locked 的 `dict[str, str]` —— 数组值穿不过 `ToolsAdapter.dispatch` 的 `model_validate`，单字符串又被 runner 的 `jsonschema.validate`（`type: array`）拒，2/8 网络场景经 Agent 路径无法产出 finding。

**决策**：在 runner 的 `_coerce_parameters` 增加分支 —— 对 manifest 声明为 `array`/`object` 的 string 值尝试 `json.loads`，成功且类型一致则采用，否则保留原值由 jsonschema 拒（与既有 int/float/bool coercion 同一 permissive-coerce-then-validate 不变式）。cassette 里手写的 model 响应将 `endpoints` 作为 JSON 编码字符串传入。`dict[str, str]` tool schema **不变**。

**安全**：解码后 `jsonschema.validate` 仍按 `items.pattern`（`^[a-zA-Z0-9.-]+:[0-9]+$`）逐项校验，坏字符（`;` / `$()` 等）被拒；通过校验的端点经 `| sh`（`shlex.quote`）渲染。无任何路径让未校验值到达 `target.exec`（Codex 审计确认）。

**否决的替代**：(a) 把 tool schema 改 `dict[str, Any]` —— 动 M2-locked 契约 + 需 tool-registry spec delta，超出 proposal「tool schema 无变更」承诺；(b) 网络场景延后只做 6 个 —— 削弱 M2.8「8 场景」退出条件。Option E 保留契约字面、留在 inspector-plugin-system scope，胜出。

## Risks / Trade-offs

- **fixture / cassette drift**：Inspector 命令或 tools schema 变更需重录。响亮失败手段：(1) 命令变 → ReplayTarget miss → tool_result 偏移 → 下一轮 `CassetteMiss`，且 `target.misses != []` strict-consumption 断言红（**不**靠 `ReplayMiss` 冒泡，它在管线里被 tools_adapter 吞，见 D1）；(2) tools 数量变（增删工具）或 schema 改动使已录 tool_use 输入失效（dispatch `model_validate` 报错 → is_error tool_result → message 偏移）→ `CassetteMiss`。**重要纠正**：request key 只 hash `{model, messages, tools_count}`（**排除** `system` 与 tools schema 内容，design `add-llm-cassette-testing` D-6），故 (a) Planner system prompt 改写、(b) **向后兼容的 tools schema-content 漂移**（如改 description / 加可选字段，`tools_count` 不变且不使已录输入失效）**都不会**触发 `CassetteMiss`。后者只能由 `cassette_lint.py --check-schema-drift --current-tools-hash <hex>` 检出，而该检查是 **opt-in 且仅告警**（返回 0）；**CI 当前只跑默认模式**（secret-scan + MessageResponse schema + 重复键），不传 drift flag。缓解：README 写清重录步骤；tools schema 变更后靠重录纪律或手动跑 drift lint（CI 不强制）。
- **ReplayTarget 精确匹配脆弱**：命令含未冻结的易变 token（时间戳）会 miss。缓解：D2 冻结时钟；manifest 命令尽量单行、派生值在 command 内算定。
- **场景数据真实性**：canned 输出是人造的，可能与真实分布有偏差。缓解：fixture 注释标注数据来源/构造依据；M2.9/M6 接真机时校正。
- **table parse 的列健壮性**：`ps`/`df` 列因 locale/内核版本有差异。缓解：command 用固定 `-o`/`--output` 字段列表锁定列序，不依赖默认格式。
- **`date` / journalctl 跨平台不可移植**：TLS 用 `date -d "$end" +%s`（GNU date，macOS/BSD 是 `date -j -f`）；journalctl 仅 Linux。开发者本机若为 macOS，无法直接录制 fixture / 跑真机。缓解：8 个场景均为 Linux 故障域、Inspector `targets:[local,ssh]` 隐含 Linux 目标；fixture 录制与真机运行明确假定 Linux；命令统一用 `date -u`，并在 inspector-authoring 文档标注 Linux-only。回放路径（CI/demo）不真跑这些命令，故 CI 不受开发者本机 OS 影响。
- **ReplayMiss 在管线里被两层吞掉**（见 D1）：(1) runner 的 `except TargetError → target_unreachable`（故 ReplayMiss 必须继承 `HostlensError` 不继承 TargetError）；(2) 更上层 `ToolsAdapter.dispatch` 的 blanket `except Exception`（tools_adapter.py，把 tool handler 任意异常包成 `is_error` envelope 喂回模型）。**所以异常冒泡不可靠**。缓解（主）：strict-consumption —— `ReplayTarget.misses` 记录 + 测试断言 `target.misses == []`，不依赖任何异常路径；单元层直接断言 `ReplayMiss` 仅作补充。

## Migration Plan

纯增量，无破坏性变更：

1. `inspectors/schema.py` 的 `CollectSpec` 加可选 `sampling_window`（省略 = 旧行为）；`inspectors/runner.py` 加窗口注入 + `InspectorRunner.__init__` 可选 `clock` 关键字参数（默认真实 UTC，旧调用不变）。
2. 新增 `targets/replay.py` + registry/config 识别 `type: replay`（不影响 local/ssh）。
3. 新增 11 个 builtin Inspector(纯新增文件)。
4. 新增 fixtures / cassettes / snapshot 测试。

回滚 = 删新增文件 + 还原两处可选字段；无数据迁移。

## Open Questions

- ReplayTarget 是否需要「同一 cmd 多次调用返回不同输出」的有序响应？当前 8 场景每命令一输出，**暂不支持**（YAGNI），未来 demo 若需交互式场景再加。
- `sampling_window` 是否需要支持人类可读时长（`"5m"`）而非纯 `duration_seconds`？本提案先做 `duration_seconds`，与 `timeout_seconds` 对齐；人性化解析留待 Scheduler（M4）统一处理。
