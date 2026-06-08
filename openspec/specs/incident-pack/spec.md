# incident-pack 规范

## 目的

定义 M2.8 最小可用「事故诊断包」（Incident Pack）的契约：随包提供覆盖 8 个真实运维故障场景（CPU 饱和 / 内存压力·OOM / 磁盘满·inode 耗尽 / systemd 失败单元 / 最近错误突增 / 文件描述符耗尽 / 依赖服务连通性 / TLS 证书过期）的 builtin Inspector（纯 YAML manifest + Finding DSL，不依赖 `hook.py` 或 `parse.format: sql_result`），每个场景配 `ReplayTarget` fixture + cassette 做离线确定性验证（无需 SSH / 付费 API），并要求回放数据漂移时**响亮失败**而非静默放过——确保「Agent 能诊断的第一个真实故障」始终可在干净机器上 reproduce。

## 需求
### 需求:八场景诊断覆盖

The system SHALL 随包提供覆盖以下 8 个真实运维故障场景的 builtin Inspector，全部用纯 YAML manifest + Finding DSL 实现（不依赖 `hook.py` 或 `parse.format: sql_result`）：CPU 饱和、内存压力/OOM、磁盘满/inode 耗尽、systemd 失败单元、最近错误突增、文件描述符耗尽、依赖服务连通性、TLS 证书过期。

#### 场景:8 场景对应的 Inspector 全部可加载

- **当** `hostlens inspectors list` 在干净安装后执行
- **那么** 输出包含 `linux.cpu.top_processes` / `linux.system.load_avg` / `linux.memory.pressure` / `linux.kernel.oom_killer` / `linux.disk.usage` / `linux.fs.inode_pressure` / `linux.systemd.failed_units` / `log.tail.error_burst` / `linux.process.fd_usage` / `net.dependency.tcp_check` / `net.tls.cert_expiry`，且加载零错误

#### 场景:Inspector 不含 hook.py 或 sql_result

- **当** 加载本 pack 的任一 Inspector manifest
- **那么** 该 manifest 不声明 `hook.py`、`parse.format` 不为 `sql_result`，所有派生值（如 `days_until_expiry`、错误计数）由 `collect.command` 内 shell 算定

### 需求:每场景具备离线确定性验证

The system SHALL 为 8 个场景中的每一个提供一个 LLM cassette、一个 ReplayTarget fixture 与一个 snapshot 测试；该测试在 CI 默认 replay 模式下运行，不消耗 Anthropic API 额度、不需要 SSH、不需要真实生产主机访问。

#### 场景:snapshot 测试离线跑通

- **当** 在 CI 默认模式（`-m 'not live'`）下执行某场景的 snapshot 测试
- **那么** 测试用 `ReplayTarget`（执行层回放）+ `PlaybackBackend`（LLM 层回放）+ 冻结时钟驱动回放管线，对**确定性投影**（叙事 `loop_result.final_text` + `PlannerResult.findings` 取真实字段 `(severity, message, tags)` 按 `(severity_rank, message)` 稳定排序的结构化投影，其中 `severity_rank = {critical:0, warning:1, info:2}` 显式映射，非 `Severity(str,Enum)` 字母序）逐字节等于该场景 snapshot 基线，且全程无真实网络 IO；该投影 MUST 只引用 `Finding` 模型真实存在的字段（`severity`/`message`/`evidence`/`tags`，**无 `title`/`inspector_name`**），MUST 排除 `duration_s` / Rich 终端装饰 / `run_id` / 时间戳，MUST NOT 比对 `render_planner_result` 的 Rich 输出或 `reporting.render_markdown`

#### 场景:回放管线产出含核心 Inspector 调用与对应 finding 的报告

- **当** 以该场景意图运行回放管线
- **那么** 该场景 cassette 录制的 Agent tool_use 序列包含该场景声明的核心 Inspector 调用，且经 `ReplayTarget` 故障数据求值后报告含与故障对应 severity 的 finding（注：回放模式验证的是端到端管线打通与报告产出，cassette 已录死 Agent 行为，不据此声称验证了 Planner 的在线推理质量）

### 需求:回放数据漂移必须响亮失败

The system SHALL 在 Inspector 渲染命令或工具 schema 与已录回放数据不一致时使对应测试失败，绝不静默回落真实 shell 或真实 API。**注意**：`ToolsAdapter.dispatch` 的 blanket `except Exception` 把 tool handler 的异常（除显式放行项）catch 成 `is_error` tool_result 喂回模型，因此经完整管线时 `ReplayMiss` **不会**冒泡成测试红。故漂移检测的**主保障是 strict-consumption**：`ReplayTarget` 记录每次 miss，snapshot 测试 MUST 断言 `target.misses == []`；该断言在 drift 时失败，与异常是否被吞无关。

#### 场景:命令漂移经管线被 strict-consumption 捕获

- **当** 某 Inspector 的 `collect.command` 被修改但其 ReplayTarget fixture 未同步重录，经完整回放管线运行
- **那么** `ReplayTarget` 记录到至少一次 miss，测试对 `target.misses == []` 的断言失败（测试红），绝不静默通过

#### 场景:命令变更在单元层抛 ReplayMiss

- **当** 对该 `ReplayTarget` 单元级直接调用 `exec(<未录命令>)`
- **那么** 抛 `ReplayMiss` 并指出未命中的命令，同时把该次 miss 记入 `target.misses`，不回落真实 shell
