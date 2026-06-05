## 为什么

M6 spike（`add-inspector-authoring-contract`，已归档）已用三个最硬用例证实 **Mode A**：复杂度压进 collector 命令、吐 JSON、Finding DSL 只判阈值的**纯 YAML** 模式可行，无需 `hook.py` / `sql_result`；并交付了《Inspector 作者契约》spec + fixture 录制器 dev-tool。spike 的 D-1 裁决定了 **infra-risk-layered 切法**：先在最硬条件证明模式，再分 wave 机械铺量。

现在到了铺量阶段。但若不切波、把整个 M6 矩阵（≥40 个）一次铺完，会让 PR 体积失控、review 失焦。本提案是 **wave-1**：先铺**风险最低、最机械**的一批——OS/Linux 纯 shell 探针。它们零外部服务依赖（不需起真容器 / 真 DB），fixture 直接从 shell 输出录制，是验证「作者契约 + 录制器」能否支撑批量产出的最佳试金石。中间件 / 服务域（nginx / mysql / postgres / redis / docker / k8s）留 wave-2 及以后。

## 变更内容

- 在现有 16 个 builtin inspector 基线（2 个 M1 探针 `hello.echo`/`system.uptime` + 11 个 incident-pack + 3 个 spike）上，**新增 23 个 OS/Linux 纯 shell builtin inspector**，覆盖**仓根** `TODO.md`（注：CLAUDE.md §9 误引为 `docs/TODO.md`，实际在仓根）§M6 矩阵的以下故障域。命名沿用既有 `linux.*` / `net.*` / `log.*` 命名空间（见 design D-8 命名空间决策；下表括注矩阵短标签）：

  | 域 | 新增 inspector（registry name） |
  |---|---|
  | 计算 CPU | `linux.cpu.throttling`、`linux.cpu.cpufreq` |
  | 内存 | `linux.memory.swap`、`linux.memory.hugepages` |
  | 磁盘 / FS | `linux.disk.io`、`linux.disk.smart`、`linux.fs.mount_health`、`linux.fs.logrotate` |
  | 网络 | `net.connections`、`net.listening_ports`、`net.dns.resolve`、`net.ntp.drift` |
  | 进程 | `linux.process.zombies`、`linux.process.total`、`linux.process.critical_alive` |
  | 服务管理器 / 调度器 | `linux.systemd.timer_status`、`linux.systemd.masked`、`linux.cron.last_runs`、`linux.cron.failures` |
  | 内核 / 系统 | `linux.system.reboot_required`、`linux.kernel.taint`、`linux.kernel.messages` |
  | 日志 | `log.exception_burst` |

- 每个 inspector 是**纯 YAML manifest**，严格遵守《作者契约》：全解析在 collector / 单 `for_each` / 注入安全三件套（`| sh` + `pattern` + 不裸拼）/ 运行前提（Linux-only、GNU coreutils、版本门）**文档式声明**于 `description`+`tags`。输出键命名约定见 design D-4（for_each 可迭代结果集顶层键取 `results`\|`items`\|`records`；聚合模式标量键沿用裸键、只需不与任一 parameter 同名——与既有 `system.uptime`/`linux.memory.pressure` 一致）。
- 每个 inspector 用 spike 交付的 fixture 录制器对真实 Linux host 录 `ReplayTarget` 兼容 fixture，**且至少含一份触发预期 finding 的异常场景 fixture**（不止 happy-path），配 snapshot 测试，CI 全程离线回放。
- 勾上 `TODO.md` §M6 覆盖矩阵对应单元格。

**不改任何对外契约**——这正是论点：纯铺量在**现有 schema 字段集**（4 种 parse format、现有 capability enum、含已落地的 `sampling_window` 等字段、无新增 schema 字段）内完成，证明 spike 裁决在批量尺度上成立。

## 功能 (Capabilities)

### 新增功能
- `os-shell-inspector-suite`: OS/Linux 纯 shell inspector 套件的**覆盖契约与质量门**——规定 wave-1 必须**按域覆盖**的 OS/Linux 故障域、以及每个 builtin inspector 的强制**质量门**（纯 YAML 遵守《作者契约》+ `ReplayTarget` fixture + 含异常场景的 snapshot 测试 + 覆盖矩阵勾选）、以及「零新基础设施」约束（不改 manifest schema / 不扩 capability enum / 不加 parse format）。**遵守 spike D-9：不为单个 inspector 立行为 spec**——具体 inspector 清单（名称/采集手法）是**实现**、列在 proposal/tasks、由 snapshot 验收；本 capability 的规范性内容是**套件层的覆盖方法论与质量基线**（这是《作者契约》未覆盖的新需求类，故另立 capability 而非塞进 contract），**不**枚举或规定任一 inspector 的 input/output 行为。

### 修改功能
- 无。`inspector-plugin-system`（loader/schema 机制）不变；`inspector-authoring-contract`（编写规则）不变、仅被本套件**引用与遵守**，不修改其需求；`inspector-fixture-recorder`（工具契约）不变、仅被使用。
  - 注记（既有漂移，非本提案引入）：`inspector-plugin-system` spec 文本仍把 `collect.sampling_window` 列为 M1-disabled，而现行 `schema.py` 已支持该字段（`log.tail.error_burst` 已在用）。本提案 design D-3 依赖该**现有 schema 字段**、**不**修改 plugin-system spec（避免范围蔓延）；该 spec 文本与代码的同步留独立 docs chore。

## 影响

- **新增代码**：复用现有 `src/hostlens/inspectors/builtin/linux/`、`builtin/net/`、`builtin/log/` 目录，新增 23 个 `.yaml`（按 `linux.*`/`net.*`/`log.*` 命名空间就近归域，不新建顶层目录）。
- **新增测试**：`tests/inspectors/` 下各 inspector 的 snapshot 测试 + fixture（`tests/inspectors/fixtures/`），并扩 `test_builtin_inspectors.py` / `test_builtin_capability_gate.py` 的 loader 与 capability-gate 断言。
  - 偏离登记：`TODO.md` §M6 退出条件原文要求 fixture 落 `examples/` 并给 demo 场景。本 wave 统一把 fixture 落 `tests/inspectors/fixtures/`（CI replay 单一来源，与 spike 既有 fixture 落点一致），`examples/` 装饰性 demo 留后续/可选——与 spike 的 `add-demo-cli` 采 scenario-registry 而非 `examples/` 散列同向，避免第二份场景清单。
- **文档**：勾选仓根 `TODO.md` §M6 矩阵单元格。
- **对外契约影响**：
  - **Inspector manifest schema**：不变（不增删字段、不扩 parse format、不扩 capability enum）。
  - **Inspector registry（对 Agent 可见）**：扩 23 个 builtin inspector；Agent 仍只见 `list_inspectors` / `run_inspector` 两个工具，**工具数组不变**。
  - **不涉及** Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令变更。
- **依赖**：不新增 Python 依赖。各 inspector 的 `requires_binaries`（如 `smartctl`/`ss`/`dig`/`chronyc`/`journalctl`）由 preflight 探测，缺失时 inspector `status=requires_unmet` 自动 skip（非报错）。

## Failure Modes

1. **目标缺少某二进制**（如无 `smartctl` / 无 `chronyc`）→ runner preflight `requires_binaries` 探测失败 → 该 inspector `status=requires_unmet` 被 skip 并在报告中标注，**不影响**同 run 其它 inspector。
2. **非 Linux 目标**（macOS / BSD）跑 Linux-only inspector（如读 `/proc`、GNU `date -d`）→ 主命令失败、非零退出、空 stdout → parse 异常 → `status=exception`（诚实），不伪造 `status=ok`。契约要求 collector **fail-loud**（每段命令各自 `|| exit 1`），杜绝守护进程/数据源不可达时吐空 JSON 被误判为「无异常」。
3. **collector 输出键与 parameter 同名遮蔽** → 由《作者契约》输出键命名约定（for_each 集合用 `results`/`items`/`records`、聚合标量不与 parameter 同名）+ **snapshot 测试**兜底。注意：输出键命名是 **prose 纪律 + snapshot 验收**，loader **不**对输出键做机器校验（design D-6 已述；机器式 manifest lint 列为后续工作）——这与下一条「注入安全」由 loader 强制不同。
4. **注入 payload 经参数进 shell** → 注入安全三件套（`| sh` shlex.quote + `pattern` 收紧取值域 + 不裸拼）；其中 loader **确在加载期**拒绝未走 `| sh` 的 string 参数（机器门，见 `inspector-plugin-system` spec）。
5. **fixture 与 runner 实际命令漂移** → 强制用 spike 录制器（驱动真 runner 录制、字节级匹配、冻结时窗），**禁止**手写 fixture。

## Operational Limits

- **并发预算**：不引入新并发；inspector 在现有 runner 顺序/并行调度内运行，单 inspector `collect.timeout_seconds` 默认 ≤30s（探针类）。
- **内存预算**：collector 输出为小 JSON（进程/连接/挂载点列表，典型 <100KB），无大文件读入；需时窗的探针（如 `linux.disk.io`）在 collector 内自行 read→sleep→read 双读算差，sleep 时长受 `collect.timeout_seconds` 约束。
- **超时设置**：每个 manifest 显式声明 `collect.timeout_seconds`；含内部 sleep 的双读探针（`linux.disk.io`）超时必须 > sleep 时长；慢探针（`smartctl` / `journalctl` 扫描）设更宽但有上限的超时，避免 hang。
- **无 LLM 调用**：本提案纯 Inspector 层，不触发任何 Agent/LLM 调用。

## Security & Secrets

- **不引入新密钥**。本批 inspector 均为只读系统探针，无需 secret 注入（不连 DB / 不带 token）。
- **脱敏**：inspector 输出经现有 `redact_report_for_render` 渲染边界；若某探针可能回显路径/主机名等，遵循现有报告脱敏管线，不新增脱敏需求。
- **攻击面**：所有把参数插入命令的 inspector 走注入安全三件套（见 Failure Modes 4）。`privilege` 默认 `none`；需要 root 的探针（如完整 `smartctl` SMART 读取）**文档式声明** `privilege` 并在未 `--allow-privileged` opt-in 时由 runner 拒绝 dispatch（`runner.py` step 3 → `requires_unmet`/`privilege_opt_in`），不静默降权；能用非特权路径的优先非特权（如 `journalctl -k` 替代特权 `dmesg`）。
- **不扩大攻击面**：不暴露任意命令执行、不新增 Agent 可见工具。

## Cost / Quota Impact

- **零 token 消耗 / 零 API 调用**：本提案不含任何 LLM 调用点，纯 Inspector manifest + fixture + snapshot。
- 对 Anthropic 配额无影响；CI 全程 `ReplayTarget` 离线回放，不消耗任何额度。
- 录制 fixture 时对真实 Linux host 的一次性 shell 采集（thin integration lane / 手动），无外部计费。

## Demo Path

无 SSH、无付费 API、无真实生产访问的本地复现：

1. `hostlens inspectors list --tag network`（或 `--tag memory` 等）能看到新增 inspector 已注册、`errors == []`。
2. `hostlens inspect localhost --inspector linux.process.total`（本机 Linux 时）跑通；或在任意平台用录制好的 fixture 经 `ReplayTarget` 回放 → snapshot 出确定性 `InspectorResult`。
3. `hostlens inspectors show net.connections` 看 manifest + 内嵌 collector 命令 + 注入安全注释。
4. `pytest tests/inspectors -k "wave1 or <inspector_name>"` 全绿——CI 全程离线回放固定 fixture，不依赖网络 / 真实主机。
