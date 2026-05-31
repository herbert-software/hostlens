# 最小可用 Incident Pack（M2.8）

## Why

M2 已经跑通「自然语言意图 → Agent 自选 Inspector → 出报告」的管线，但目前 builtin 只有 `hello.echo` / `system.uptime` 两个玩具 Inspector —— Agent 还**不能诊断任何一个真实故障**。架构再漂亮，"你能诊断的第一个真实故障"才是用户和面试官判断这个项目的标尺。本提案交付一套覆盖 8 个真实运维场景的最小 Incident Pack，并补齐让这 8 个场景能在 CI 上**离线、确定性**回放的两层基建（LLM 层已有 cassette；执行层尚缺）。

## What Changes

- 新增 11 个 builtin Inspector，覆盖 8 个真实故障场景（全部走纯 YAML manifest + Finding DSL，`collect.command` 自行输出 JSON，**不引入 hook.py / sql_result**）：
  - CPU 饱和：`linux.cpu.top_processes` + `linux.system.load_avg`
  - 内存压力 / OOM：`linux.memory.pressure` + `linux.kernel.oom_killer`
  - 磁盘满 / inode 耗尽：`linux.disk.usage` + `linux.fs.inode_pressure`
  - systemd 失败单元：`linux.systemd.failed_units`
  - 最近错误突增：`log.tail.error_burst`
  - 文件描述符耗尽：`linux.process.fd_usage`
  - 依赖服务连通性：`net.dependency.tcp_check`
  - TLS 证书过期：`net.tls.cert_expiry`
- **修改 `inspector-plugin-system`**：实现 `collect.sampling_window` 时窗采集（runner 注入 `window_start` / `window_end` 给 Jinja2 与 Finding DSL 求值上下文），供 `log.tail.error_burst` 使用。这是 M1 显式 deferred 到本提案的字段。
- **新增 `ReplayTarget`**（执行层回放）：一个 `ExecutionTarget` 实现，按 `cmd` 匹配 fixture 中预录的 `ExecResult`，让 Inspector 在 CI 上无需真实故障主机即可产出确定性"故障"输出，走完整 target → collect → parse → findings 真实路径。
- 为 8 个场景各录 1 个 LLM cassette（消费 M2.6 已交付的录制基建）+ 1 个 snapshot 测试：在 `ReplayTarget` + `PlaybackBackend` + 冻结时钟下跑完整 `--intent` 管线，对**确定性投影**（叙事 `loop_result.final_text` + findings 结构化排序投影；测试内确定性 helper 渲染，**排除** duration / Rich 装饰 / run_id / 时间戳）做 snapshot 比对，并断言 `ReplayTarget.misses == []`（strict-consumption）+ Agent tool_use 序列含该场景核心 Inspector。验证回放管线端到端打通、产出含对应 severity finding 的报告。**不**比对 `render_planner_result` 的 Rich 终端输出（含 `duration_s` 非确定字段 + Rich 宽度相关换行），更不走 `reporting.render_markdown`。CI 默认 replay 模式，不消耗 API 额度、不需 SSH、不需真实生产访问。

## Capabilities

- New: `incident-pack` - 策划的 8 场景诊断覆盖契约（场景 → Inspector 映射 + 每场景必备 cassette/snapshot/离线可复现）
- New: `replay-execution-target` - fixture 驱动的 `ExecutionTarget`，为离线确定性巡检回放预录命令输出
- Modified: `inspector-plugin-system` - 新增 `collect.sampling_window` 时窗采集与 `window_start`/`window_end` 注入

## Non-Goals

- ❌ **M2.9 demo 交付物**（`hostlens demo <scenario> --replay` CLI、`examples/` 场景包、README GIF、stress-ng 真实复现）—— 留给后续独立提案 `add-demo-cli`；本提案只保证「snapshot 测试证明可离线诊断」，不提供面向人类的 demo 命令
- ❌ **M3 Diagnostician 根因假设章节** —— 本提案只用 Planner Agent 出报告，不含 "📌 根因假设" / 跨信号关联
- ❌ **`hook.py` Python 扩展 / `parse.format: sql_result`** —— 8 个 Inspector 全部用 `collect.command` 直接输出 JSON 留在 YAML+DSL 内，复杂解析留给 M6
- ❌ **M6 完整 Inspector 覆盖矩阵** —— 只做这 8 个场景所需的 11 个，不扩到 cpu.throttling / nginx / mysql 等
- ❌ **新的 cassette 录制工具链** —— 复用 M2.6 `add-llm-cassette-testing` 已交付的录制/门禁/回放基建
- ❌ **SSH / Docker / K8s 真机集成** —— 本提案的离线验证全部走 `ReplayTarget`；真机由 demo 提案与 M6 覆盖

## 对外契约影响

- **Inspector manifest schema**：`collect.sampling_window.duration_seconds` 为**新增可选字段**，向后兼容（省略 = 旧行为）；新增运行时注入变量 `window_start` / `window_end` / `window_seconds`（仅在声明窗口时出现）。
- **ExecutionTarget 配置契约**：`targets.yaml` 新增配置判别值 `type: replay`（+ `fixture: <path>`），是 TargetsConfig union 的新成员；不影响 `local` / `ssh`。**关键**：`ReplayTarget` 的**运行时** `.type` 属性返回它所**冒充**的既有类型（`local` / `ssh`，由 fixture 声明），因此 `ExecutionTarget.type` 的 `Literal["local","ssh","docker","k8s"]` 与 `InspectorManifest.targets` 的 `Literal["local","ssh"]` **都无需改动** —— runner preflight 的 `target.type in manifest.targets` 与 capability 匹配对 ReplayTarget 透明通过。配置层的 `"replay"` 判别值与运行时 `.type` 是两个独立概念。
- **Agent tool schema**：**无变更** —— 复用既有 `run_inspector` / `list_inspectors` / `list_targets`，本提案只是增加可被调用的 Inspector 实例。
- **MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令**：均**无变更**（`hostlens demo` 留给 M2.9）。

## 新 Inspector manifest 示例

最复杂的一个（参数化 + `date` 内算派生值 + JSON 输出），证明 8 场景均可闭合在 YAML+DSL 内：

```yaml
name: net.tls.cert_expiry
version: 1.0.0
description: Check TLS certificate expiry for configured endpoints
tags: [network, tls, security]
targets: [local, ssh]                # ReplayTarget 冒充 local/ssh 跑, 不新增 target type
requires_binaries: [openssl]

parameters:
  type: object
  required: [endpoints]
  properties:
    endpoints:
      type: array
      items: { type: string, pattern: "^[a-zA-Z0-9.-]+:[0-9]+$" }   # pattern 防注入
    warn_days:     { type: integer, default: 30 }
    critical_days: { type: integer, default: 7 }

collect:
  command: |                                  # days_until_expiry 在 shell 内算定, 输出 JSON
    printf '{"endpoints":['
    sep=""
    for ep in {{ endpoints | map('sh') | join(' ') }}; do
      host=${ep%%:*}
      end=$(echo | openssl s_client -servername "$host" -connect "$ep" 2>/dev/null \
            | openssl x509 -noout -enddate | cut -d= -f2)
      days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
      printf '%s{"endpoint":"%s","days_until_expiry":%d}' "$sep" "$ep" "$days"
      sep=","
    done
    printf ']}'
  timeout_seconds: 30

parse:
  format: json

output_schema:
  type: object
  properties:
    endpoints:
      type: array
      items:
        type: object
        properties:
          endpoint:          { type: string }
          days_until_expiry: { type: integer }

findings:
  - for_each: "endpoints as e"
    when: "e.days_until_expiry <= critical_days"
    severity: critical
    message: "TLS cert for {e.endpoint} expires in {e.days_until_expiry} days"
  - for_each: "endpoints as e"
    when: "e.days_until_expiry <= warn_days and e.days_until_expiry > critical_days"
    severity: warning
    message: "TLS cert for {e.endpoint} expires soon ({e.days_until_expiry} days)"
```

## Agent 行为与 Prompt Caching

- 不改 Planner 系统 prompt 结构，**沿用 M2.5 已落地的两层 prompt cache 策略**（系统 prompt + Inspector registry 概览打 `cache_control: ephemeral`）。
- 新增 11 个 Inspector 会让 registry 概览静态块变长 → 进 cache，单场景多轮 Agent loop 第二轮起命中缓存；snapshot 测试在 replay 下不产生真实 token，cache 命中率验证沿用 M2.5 既有单测（本提案不新增 LLM 调用点，只新增可调用 Inspector）。
- token 影响：见下方 Cost / Quota。

## Failure Modes

- **命令漂移（fixture 没重录）**：`ReplayTarget.exec` 抛 `ReplayMiss`（继承 `HostlensError`，语义=infra 错误）。但 runner（`except TargetError`）与 `ToolsAdapter.dispatch`（blanket `except Exception`）两层都会吞它，所以管线里 ReplayMiss **不冒泡成测试红**，drift 表现为模型多走一轮 → `CassetteMiss`。**响亮失败的主保障是 strict-consumption**：`ReplayTarget` 记录每次 miss 到 `self.misses`，snapshot 测试断言 `target.misses == []` —— 无论异常被哪层吞，drift 都让该断言红。降级：绝不回落真实 shell。
- **报告含非确定字段**：snapshot 比对**确定性投影**（`final_text` + findings 排序投影，排除 duration/Rich/run_id/时间戳），不比对 `render_planner_result` 的 Rich 终端输出（其面板含 `duration_s` + Rich 宽度相关换行）。降级：若实施者误比对 Rich/markdown 输出 → snapshot flaky，design D4 已显式禁止并规定确定性 helper。
- **跨平台 `date` 不可移植**：TLS 的 `date -d`（GNU）与 `error_burst` 的 journalctl 时间格式在 macOS/BSD 不成立，开发者本机录制会失败。降级：fixture 录制与真机运行假定 Linux 目标（与 Inspector `targets:[local,ssh]` + Linux 故障域一致）；design Risks 已承认，命令用 `date -u` 并标注 Linux-only。
- **CassetteMiss（LLM 漂移）**：model 名 / `messages`（intent、tool_use 输入、tool_result）/ `tools_count` 变更 → `CassetteMiss`（绝不回落真实 API）。**注意**：request key **不含** `system` 与 tools schema 内容（design `add-llm-cassette-testing` D-6），故 Planner system prompt 改写、向后兼容的 tools schema-content 漂移（`tools_count` 不变）**不**触发 `CassetteMiss` —— 后者需 `cassette_lint.py --check-schema-drift --current-tools-hash`（opt-in + 仅告警；CI 默认模式不跑该 flag）检出或靠重录纪律。
- **冻结时钟缺失**：含 `sampling_window` 的 Inspector 在测试中未注入固定时钟 → 渲染命令含漂移时间戳 → `ReplayMiss`。降级：测试红（暴露缺陷），不静默通过。
- **table parse 列漂移**：`ps`/`df` 因 locale/内核差异列错位。降级：command 用固定 `-o`/`--output` 锁列序；解析失败 Inspector status=exception 而非崩整个 run。
- **fixture 数据失真**：人造故障数据与真实分布偏差 → 报告判定与真机不符。降级：fixture 注释标注构造依据；M2.9/M6 接真机校正。

## Operational Limits

- **并发**：沿用既有 Inspector Runner 并发预算（同 turn 内多 tool_use 并行，受 Agent loop 上限约束）；本提案不引入新并发路径。
- **内存**：ReplayTarget fixture 全量载入内存，单 fixture 上限沿用 manifest 256KB 量级约束（canned 输出小）；snapshot 基线为小文本文件。
- **超时**：各 Inspector `collect.timeout_seconds` 显式声明（CPU/磁盘类 10s、网络/TLS 类 30s）；ReplayTarget 即时返回不触发超时。

## Security & Secrets

- **不引入新密钥**：8 个场景 Inspector 均无 `secrets:`（tcp_check / tls 走公开探测，无凭据）。
- **攻击面**：参数化 Inspector（`tcp_check` / `tls.cert_expiry`）的 string 参数全部声明 `pattern` 约束字符集，命令模板走 `| sh` filter 强制 shellquote；loader 既有注入静态校验五件套覆盖，本提案加注入 payload 测试（`'; whoami; #` / `$(curl evil)`）。
- **脱敏**：ReplayTarget fixture / cassette 为提交物，复用 M2.6 `cassette_lint.py` secret-scan + 冻结合成数据（无真实 hostname/IP/路径）；snapshot 报告同样过脱敏边界。
- ReplayTarget 只读，无写操作，不触发 EUID==0 约束。

## Cost / Quota Impact

- **运行时（CI / demo replay）**：零 Anthropic token、零 API 调用 —— 全程 `PlaybackBackend` 回放。
- **一次性录制成本**：8 个 cassette 各录 1 次（`HOSTLENS_LLM_MODE=record` + 真 key），每场景 1 次 Planner 多轮 loop，估算单场景 < 30K input / < 4K output token，8 场景合计一次性 ≈ 数十万 token 量级，远低于日常配额；重录仅在命令/schema 变更时发生。

## Demo Path（5 分钟内本地 reproduce，无 SSH / 无付费 API）

```bash
pip install -e ".[dev]"
# 离线跑某个场景的 snapshot 测试（ReplayTarget + PlaybackBackend 双回放, 零 API）
pytest tests/incidents/test_cpu_saturation.py -v
# 看 11 个 Inspector 已注册
hostlens inspectors list | grep -E 'linux\.|net\.|log\.'
# 看任一 Inspector manifest
hostlens inspectors show net.tls.cert_expiry
```

> 面向人类的 `hostlens demo cpu-spike --replay` 命令留给 M2.9 `add-demo-cli`；本提案的离线可复现性由上面的 snapshot 测试保证。

## Impact

- Affected specs:
  - 新增 `openspec/specs/incident-pack/`（本提案 delta）
  - 新增 `openspec/specs/replay-execution-target/`（本提案 delta）
  - 修改 `openspec/specs/inspector-plugin-system/`（sampling_window delta）
- Affected code:
  - `src/hostlens/inspectors/builtin/{linux,net,log}/*.yaml`（11 个新 Inspector）
  - `src/hostlens/inspectors/schema.py`（`CollectSpec` 加 `sampling_window` 字段）+ `inspectors/runner.py`（窗口变量注入 + 可注入时钟）
  - `src/hostlens/targets/replay.py`（新 ReplayTarget）+ `targets/registry.py` / `config.py`（注册接线 + TargetsConfig union 加 `replay` 成员）
  - `src/hostlens/core/exceptions.py`（新增 `ReplayMiss`，继承 `HostlensError` 而非 `TargetError`）
  - `tests/fixtures/incident_pack/*.json`（ReplayTarget 命令录制）
  - `tests/cassettes/incident_*.jsonl`（8 个 LLM cassette）
  - `tests/incidents/`（8 个 snapshot 测试）
- Migration: 无 —— 纯新增 Inspector / Target + 一个向后兼容的可选 manifest 字段（`sampling_window` 省略时行为不变）
