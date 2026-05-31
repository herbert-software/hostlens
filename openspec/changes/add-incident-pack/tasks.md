# Tasks: 最小可用 Incident Pack（M2.8）

> 依赖顺序：sampling_window 基建 → ReplayTarget → 11 个 Inspector → fixtures/cassettes/snapshot → 文档收尾。
> 全程 `mypy --strict` 0 错误；测试默认 replay（`-m 'not live'`），不消耗 API 额度。

## 1. collect.sampling_window 基建（inspector-plugin-system delta）

- [x] 1.1 `src/hostlens/inspectors/schema.py`（**非 builtin/models.py**）：`CollectSpec` 加可选 `sampling_window`（嵌套 frozen `SamplingWindow{duration_seconds: int = Field(gt=0)}` —— 用约束而非散文，`0`/负值 load 时即报错避免 `window_start == window_end`）；省略 = None，`frozen=True, extra="forbid"` 不破坏既有 manifest
- [x] 1.2 `src/hostlens/inspectors/runner.py`（**非 builtin/runner.py**）：`InspectorRunner` 增加可注入 `clock: Callable[[], datetime]`（默认真实 UTC，既有调用方不传 = 旧行为）；声明 `sampling_window` 时计算 `window_start`/`window_end`(`YYYY-MM-DD HH:MM:SS` UTC str，journalctl 友好)/`window_seconds`(int)，注入 `_render_command` Jinja 上下文（现为 `**parameters`）与 Finding DSL 上下文（现为 `{**output, **parameters}`）两处
- [x] 1.3 `src/hostlens/inspectors/loader.py`：校验 `parameters` 不得声明保留名 `window_start`/`window_end`/`window_seconds`，撞名拒绝加载并给字段级错误
- [x] 1.4 `tests/inspectors/test_sampling_window.py`：注入冻结时钟，验证 (a) 窗口变量进渲染命令且 start 早于 end 恰好 duration、格式为 `YYYY-MM-DD HH:MM:SS`；(b) `window_seconds` 进 DSL 上下文；(c) 省略字段时三变量均不出现且旧行为不变；(d) 冻结时钟下两次渲染逐字节相同；(e) parameter 撞保留名被 loader 拒绝

## 2. ReplayTarget（replay-execution-target capability）

- [x] 2.1 `src/hostlens/core/exceptions.py`：新增 `ReplayMiss`(**继承 `HostlensError`，不继承 `TargetError`** —— 否则被 runner `except TargetError` 吞成 `target_unreachable`，漂移不报错)
- [x] 2.2 `src/hostlens/targets/replay.py`：`ReplayTarget` 实现 `ExecutionTarget`；fixture JSON 加载（`impersonate` → 运行时 `.type`，默认 `local`；`commands[]` 按「逐行 rstrip 后 SHA256」建索引 + `files{}` + `capabilities[]` 投影成 `set[Capability]`）；`exec` 命中返回预录 `ExecResult`、`env` 接受但不参与匹配、miss 抛 `ReplayMiss`；`read_file` 走 `files`、miss 抛 `ReplayMiss`；**`exec`/`read_file` 每次 miss 记入可读 `self.misses`（即便同时抛 ReplayMiss）—— strict-consumption 主保障**
- [x] 2.3 `src/hostlens/targets/config.py` + `registry.py`：`TargetsConfig` union 加 `type: replay`（+ `fixture: <path>`）成员；`build_registry_from_config` 识别注册；read-only 不受 EUID==0 写约束
- [x] 2.4 `tests/targets/test_replay.py`：命中返回预录结果；运行时 `.type` 等于 `impersonate` 声明；未命中命令/文件抛 `ReplayMiss` 且无真实子进程/文件访问；**每次 miss 记入 `target.misses`，全命中时 `misses` 为空**；`ReplayMiss` 经 runner 不被映射成 `target_unreachable`；`capabilities` 等于 fixture 声明；从配置构建可用

## 3. 11 个 Inspector（incident-pack capability）

> 全部纯 YAML；`collect.command` 用固定 `-o`/`--output` 字段锁列序、派生值 shell 内算定；string 参数声明 pattern 防注入。
> **两个陷阱（见 design D3）**：(a) kv 场景 `output_schema` 字段声明为 `string`，数值判定在 DSL 内 `float()`/`int()` 强转（否则 jsonschema 校验挂 → status=exception → snapshot 静默变空）；(b) json 场景空结果也要输出合法顶层 object（`{"oom_events":[]}` / `{"endpoints":[]}`），否则 `parse_json` 崩。

- [x] 3.1 CPU：`builtin/linux/cpu_top_processes.yaml`(table) + `builtin/linux/system_load_avg.yaml`(kv)
- [x] 3.2 内存/OOM：`builtin/linux/memory_pressure.yaml`(kv) + `builtin/linux/kernel_oom_killer.yaml`(json)
- [x] 3.3 磁盘/inode：`builtin/linux/disk_usage.yaml`(table) + `builtin/linux/fs_inode_pressure.yaml`(table)
- [x] 3.4 systemd：`builtin/linux/systemd_failed_units.yaml`(json，`requires_capabilities: [systemd]`)
- [x] 3.5 错误突增：`builtin/log/tail_error_burst.yaml`(kv，用 §1 `sampling_window`)
- [x] 3.6 FD：`builtin/linux/process_fd_usage.yaml`(kv)
- [x] 3.7 依赖连通：`builtin/net/dependency_tcp_check.yaml`(json，参数化 `endpoints: [host:port]`，pattern 约束)
- [x] 3.8 TLS：`builtin/net/tls_cert_expiry.yaml`(json，参数化 `endpoints`，command 内 `date` 算 `days_until_expiry`)
- [x] 3.9 `tests/inspectors/test_incident_pack_manifests.py`：11 个 manifest 全部加载零错误；断言无 `hook.py`、`parse.format != sql_result`；注入 payload（`'; whoami; #` / `$(curl evil)`）验证参数化 Inspector 渲染转义正确

## 4. 双回放层 fixtures / cassettes / snapshot 测试

> 每场景：ReplayTarget fixture（人造故障数据）+ LLM cassette（生成器确定性录制）+ snapshot 基线 + 测试。
> **实现期路径调整**（对齐既有布局）：cassette 落 `tests/fixtures/cassettes/incident_<scenario>.jsonl`（复用 conftest `llm_cassette` fixture + `cassette_lint`），非 `tests/cassettes/`。

- [x] 4.0a 生产接线（Option C，Codex 评审，见 design D5）：`tools/default_tools.py` 抽出 `build_run_inspector_spec(handler)`；`run_inspector_handler` 加可选 `clock`；`register_default_tools(registry, *, clock=None)` 传入时注册 clock-bound `run_inspector`。`ToolContext` 六字段不变。
- [x] 4.0b 生产接线（Option E，Codex 评审，见 design D6）：`inspectors/runner.py` 的 `_coerce_parameters` 对 array/object 声明的 string 值 `json.loads`，失败留原值由 jsonschema 拒。`dict[str,str]` tool schema 不变。
- [x] 4.1 `tests/fixtures/incident_pack/<scenario>.json` ×8：生成器经 `_CaptureTarget` 跑真实 Inspector 捕获 preflight 探测 + 渲染主命令（不手算命令串）；含 `impersonate` + `capabilities` + 故障态 stdout；字节稳定（无 IP/FQDN/home 路径，端点用单标签服务名）
- [x] 4.2 `tests/fixtures/cassettes/incident_<scenario>.jsonl` ×8：生成器以 `RecordingBackend(inner=FakeBackend(手写 responses))` 跑真实 Planner 管线确定性录制（零 API key），request 与回放字节一致；过 `cassette_lint` secret-scan
- [x] 4.3 确定性投影 helper（`_harness.project_planner_result`）+ `tests/incidents/snapshots/<scenario>.md` ×8：渲染 `narrative` + `PlannerResult.findings` 取 `(severity, message, tags)` 按 `(severity_rank, message)` 排序，排除 duration/Rich/run_id/时间戳；冻结时钟下生成
- [x] 4.4 `tests/incidents/test_<scenario>.py` ×8：`ReplayTarget` + `PlaybackBackend` + 冻结时钟驱动 `--intent` 管线 → 投影逐字节等于 snapshot + `target.misses == []` + tool_use 序列含核心 Inspector + 报告含对应 severity finding（不比对 Rich 输出）
- [x] 4.5 漂移测试（`tests/incidents/test_drift.py`）：drifted fixture（删一条主命令）经完整管线后 `target.misses != []`（strict-consumption，不依赖 ReplayMiss 冒泡）；单元层断言 `ReplayTarget.exec(<未录命令>)` 直抛 `ReplayMiss` 并记入 `misses`

## 5. 文档与校验

- [x] 5.1 `tests/incidents/README.md`（双回放层 + 生成器重录步骤）+ `tests/fixtures/cassettes/README.md` 补 `incident_*` 由生成器产出的指针（cassette 实际落 `tests/fixtures/cassettes/`）
- [x] 5.2 `docs/operations/inspectors.md`：补 `collect.sampling_window` 字段说明 + ReplayTarget 用法
- [x] 5.3 勾掉 `TODO.md` M2.8 的 8 个场景 checkbox
- [x] 5.4 `openspec-cn validate add-incident-pack`（positional）通过；`mypy --strict` + `ruff` + `pytest -m 'not live'` 全绿
