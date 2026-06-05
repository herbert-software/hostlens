## 上下文

M6 spike（`add-inspector-authoring-contract`）已固化模式与裁决：

- **承重墙**（代码核实）：Finding DSL 白名单仅 `len/sum/min/max/any/all/now/float/int`（`inspectors/dsl.py`），无 string/split/regex；`for_each` 单绑定；finding 上下文 `output` 键被同名 `parameter` 遮蔽；manifest `extra="forbid"` 无 `min_binary_version`/`hook`/`sql_result` 字段；`ReplayTarget` 已存在、命令字节级匹配。
- **裁决 D-7**：M6 零新 infra，纯 YAML（Mode A）可行。
- **工具**：fixture 录制器（`src/hostlens/inspectors/recorder.py`）驱动真 runner 录制、字节级匹配、冻结时窗、脱敏 secret。

现有 16 个 builtin（2 个 M1 探针 `hello.echo`/`system.uptime` + 11 个 incident-pack + spike 的 pg/docker/redis）。本设计是 wave-1：在最低风险的 OS/Linux shell 域机械铺量，验证「契约 + 录制器」能否支撑批量产出。

现成参考样板：`builtin/docker/containers_restart_loop.yaml`（fail-loud + jq + `{results:[...]}` 包裹）、`builtin/net/dependency_tcp_check.yaml`（数组参数 `| map('sh')` + `pattern` + 输出键避让）、聚合型 `builtin/system/uptime.yaml` 与 `builtin/linux/memory_pressure.yaml`（裸标量键 `load1`/`avail_pct`，无 `results` 包裹）。本设计不发明新模式，只复用这些样板的纪律。

## 目标 / 非目标

**目标：**

- 落地 23 个 OS/Linux 纯 shell inspector，覆盖 8 个域（清单见 proposal/tasks）。
- 每个 inspector 复用样板纪律：复杂度压进 collector 吐 JSON、DSL 只判、输出键防遮蔽（D-4）、注入安全三件套、fail-loud。
- 每个 inspector 用录制器对真 Linux host 录 fixture + snapshot，CI 离线回放。
- 用「零对外契约变更」实证 spike 裁决在批量尺度成立。

**非目标：**

- 见 `proposal.md` §（不铺中间件/服务域 / 不 enable hook.py / 不加 sql_result / 不扩 capability enum / 不加 min_binary_version / 不做 list_inspectors 过滤 / 不做 Docker·K8s target）。
- 不为单个 inspector 立 spec（D-9）；不发明新 collector 模式；不做覆盖矩阵之外的「锦上添花」探针。

## 决策

### D-1 按域分组实施，但单一提案收口（不再切子提案）

wave-1 内部按域分组实施（见 tasks §2–9 的 8 个域组 + §1 准备 / §10 验收 / §11 收尾），但**不**为每个域另起 OpenSpec 子提案。理由：spike 已消解架构风险（D-1 infra-risk-layered 的「risk」层已过），wave-1 是纯机械铺量，再切子提案只增流程开销。PR 体积由「域分组 commit + 单 PR」控制；若 review 时单 PR 过大，按域拆成多个 PR（同一 change、多次 squash-merge），但 spec/proposal 不拆。

### D-2 每个 inspector 必须 fail-loud，区分「真空结果」与「数据源不可达」

复刻 `docker/containers_restart_loop.yaml` 的 fail-loud 契约：runner **不**检查主命令退出码、只解析 stdout，故 collector 每段命令必须各自 `|| exit 1`，仅在「命令成功但结果确为空」时才吐 `{"results":[]}`。例：`process.zombies` 若 `ps` 失败必须非零退出（→ `status=exception`），而非吐空数组被祝福为「无僵尸进程」。`set -o pipefail` 非 POSIX（目标 `sh` 可能是 dash），故跨管线失败用命令替换捕获退出码兜底（同样复刻 docker 样板的 `$(... | xargs ...)` 手法）。

### D-3 计数器类时窗派生量由 collector 自行双读，sampling_window 只供时间戳

**先澄清 `sampling_window` 的真实语义**（核对 `runner.py:566-598` `_build_window_context`）：它**只**计算 `window_end = clock()` / `window_start = window_end - duration` / `window_seconds` 三个变量注入渲染与 DSL 上下文，**collector 命令仍只执行一次**——runner **不**做两次采样。故它适合「查某时间窗内的历史区间」类命令（如 `journalctl --since {window_start}` 数近窗错误），**不**能自动对 `/proc/diskstats` 这类**当前计数器快照**做差分。

因此**计数器差分类**派生量（`linux.disk.io` 的 IO 利用率、`net.ntp.drift` 若需速率）**必须由 collector 命令自身**完成 `读 → sleep N → 再读 → 算差`，**禁止**误用 sampling_window 当差分器、也**禁止**在 finding 层现算。collector 内含 `sleep` 时，manifest 的 `collect.timeout_seconds` 必须 > sleep 时长。**区间查询类**（如 `linux.kernel.messages` / `log.exception_burst` 数近窗事件）才用 sampling_window 的 `window_start`/`window_end` 时间戳；录制器冻结时钟保证这些时间戳渲染稳定、replay 命中。

### D-4 输出键命名：列表集用 `results`/`items`/`records`，聚合标量沿用裸键不撞参数名

**契约的「顶层结果键取 `results`/`items`/`records`」约定针对的是可迭代结果集**（配 `for_each` 遍历的那个数组），目的是防它与同名 parameter 在合并上下文里被遮蔽（承重墙 3）。它**不**要求聚合型把每个标量都塞进 `results`——既有聚合型 `system.uptime`（`load1`/`load5`/`load15`）、`linux.memory.pressure`（`avail_pct`）就用裸标量键、无 `results` 包裹，且工作正常。

故 wave-1 两种形态：
- **列表型**：`{"results": [...]}`，`for_each: "results as x"`。`results`（或 `items`/`records`）**必须**不与任一 parameter 同名。
- **聚合型**：裸标量顶层键（如 `{"zombie_count": N, "total": M}`），message 用 `.format(**output)` 读；每个被 finding 引用的键**必须**不与任一 parameter 同名。

两形态的不变量都是「输出键 ≠ 任一 parameter 名」（承重墙 3）；只有列表集额外强制三选一命名。该约定是 prose+snapshot 验收（D-6），loader 不机器校验输出键命名。

**已知文档张力（登记为 gap，不在本提案修——与 D-3 sampling_window 漂移注记同构处理）**：`inspector-authoring-contract` 需求2 的字面正文写「collector 输出的**顶层结果键**必须取自 `results`/`items`/`records` 之一」，**未**在文本里显式限定「仅 for_each 可迭代集」。本 D-4 采纳「该约定仅约束 for_each 迭代集、聚合型沿用裸键」的读法，依据是：(i) 契约该需求的立法意图是防 parameter 遮蔽（承重墙 3），裸标量键只要不撞参数名即满足该意图；(ii) 既有 `system.uptime`/`linux.memory.pressure` 已是裸键先例（若按字面宽读，这两个既有 builtin 即违约，显然非契约本意）；(iii) 契约该需求的场景举例本身是「列表输出 + endpoints 参数」碰撞。本提案**不**修改契约文本（遵守非目标「不改契约」），契约正文的澄清（明确区分迭代集 vs 聚合标量）留**独立 docs/spec chore**。归档时若按契约字面校验聚合型 inspector，以本 gap 注记为准。

### D-5 窄 scope 与 Linux-only 文档式声明

承重墙 5：无 `min_binary_version` 字段。Linux-only（读 `/proc`/`/sys`、GNU `date -d`、`journalctl`、`ss`、`chronyc`）一律在 `description` 写明 + 加 tag（如 `linux`、`systemd`、`gnu-coreutils`）。不满足时 preflight `requires_binaries` 探测失败或主命令失败兜底（`status=requires_unmet` / `exception`），接受「非结构化版本不匹配」代价——机器式版本门是 wave-2 前的独立基础设施提案。

### D-6 privilege 声明而非静默降权

需 root 的探针声明 `privilege` 字段；runner 在未 `--allow-privileged` opt-in 时拒绝 dispatch（`runner.py:510-511` step 3 → `requires_unmet`/`privilege_opt_in`）。**能用非特权路径的优先非特权**（如 `journalctl -k` 替代特权 `dmesg`、`/sys/block/*/queue` 与 `/sys/class/block/*/stat` 替代特权工具）。`linux.disk.smart` 因此**收敛为单个非特权 inspector**（读 `/sys/block/*/queue/rotational`、可用 `smartctl --json` 时附带，但不强制 root）；「需 root 的完整 SMART 自检属性」**defer 到独立提案**——见 §待解决问题已转决策。

### D-7 录制器是唯一 fixture 来源

承重墙 4 + spike D-5：手写 fixture 必漂移。每个 inspector 落地后用 `python -m hostlens.inspectors.recorder` 对真 Linux host（开发机 / docker-compose Linux 容器 / thin integration lane）录一次，产出 `ReplayTarget` JSON。snapshot 测试加载该 fixture 经 `ReplayTarget` 回放断言 `InspectorResult`。CI 只跑回放。每个 inspector **必须**至少录一份**触发预期 finding 的异常场景** fixture（防 no-op inspector：仅 happy-path snapshot 无法证明检出能力）。

### D-8 命名空间：沿用既有 `linux.*` / `net.*` / `log.*`，不另起裸命名空间

既有 OS 探针已用 `linux.*`（`linux.cpu.top_processes` / `linux.memory.pressure` / `linux.disk.usage` / `linux.systemd.failed_units` / `linux.kernel.oom_killer` …，落 `builtin/linux/`）、网络用 `net.*`（`net.dependency.tcp_check` / `net.tls.cert_expiry`，落 `builtin/net/`）、日志用 `log.*`（`log.tail.error_burst`，落 `builtin/log/`）。inspector `name` 是 **Agent-visible registry key 且会进 fixture/snapshot**，铺完再改名代价高。故 wave-1 **沿用既有命名空间**、不另起裸 `cpu.*`/`memory.*`，避免同一故障域劈成两个命名空间（如 `linux.cpu.top_processes` 与裸 `cpu.throttling` 并存）：

- `linux.*`（落 `builtin/linux/`）：cpu / memory / disk / fs / process / systemd / cron / system / kernel 域。
- `net.*`（落 `builtin/net/`）：network / dns / ntp 域（`net.connections` / `net.listening_ports` / `net.dns.resolve` / `net.ntp.drift`）。
- `log.*`（落 `builtin/log/`）：`log.exception_burst`。

既有裸 `system.uptime` 不在本 wave 重命名（add-only，不动既有）。`TODO.md` §M6 矩阵用短标签（`cpu.throttling` 等），实际 registry name 加前缀——勾矩阵时以前缀全名落地。

## 风险 / 权衡

- **窄 scope 靠文档而非 schema**（D-5）→ 不满足前提时报 `command not found` 而非结构化「版本不匹配」，运维体验差。**缓解**：description 明写前提下限；`min_binary_version` + 结构化 capability mismatch 列为独立基础设施提案。
- **平台差异**（GNU vs BSD coreutils、systemd vs 非 systemd、cgroup v1 vs v2）→ 同一 inspector 在不同发行版输出格式漂移。**缓解**：wave-1 显式声明 Linux + systemd + cgroup v2 假设；非主流组合走 `requires_binaries` skip；不追求「跨所有 Linux 变体」（那是 hook.py 的活、wave-2 后议）。
- **fixture 需真 Linux host 一次性采集** → CI 不能每次起全部数据源。**缓解**：thin integration lane（docker-compose / nightly）只在 manifest 变化时重录；日常 CI 全走 `ReplayTarget` 回放（spike 已建此 lane）。
- **单 PR 过大**（23 inspector + fixture + 测试）→ review 失焦。**缓解**：D-1 允许同一 change 按域拆多个 PR squash-merge；commit 按域分组。
- **契约部分靠 prose 纪律** → 作者可能违反输出键命名 / 在 DSL 里试图派生。**缓解分层**：注入安全（`| sh` / 参数 `pattern`）由 **loader 加载期机器校验**（`inspector-plugin-system`）；但**输出键命名**（results/items/records、不撞参数名）loader **不**校验，靠 snapshot 测试 + prose 约定兜底；manifest lint（机器校验输出键命名、finding 表达式禁用构造）列为后续 chore，不在本 wave。

## 迁移计划

- **新增**：纯 add-only，新增 yaml + fixture + 测试，不动现有 inspector / schema / 代码路径。
- **回滚**：删除新增文件即可，无数据迁移、无契约破坏、无对历史 run 记录的影响。
- **部署**：随 pip 包发布（builtin 是 package-data）；无配置迁移。

## 待解决问题

（无阻塞性 open question。）

## 实现期注记

- `log.exception_burst` 与已有 `log.tail.error_burst` 的**具体 collector 实现**边界：前者按异常类型聚合（stack-trace 签名），后者按错误行计数。两者**定位已明确不重叠**（属实现期细节，不影响 spec 覆盖契约或交付计数 23）；实现时只需确认 collector 不重叠、各自 fixture 区分场景——非待决设计问题。

> 注：原「`disk.smart` 是否拆两个 inspector」已转为决策 **D-6**（收敛为单个非特权 `linux.disk.smart`，完整 root SMART defer 到独立提案），不再是 open question——避免与 spec「每域按矩阵新增」+ 交付计数 23 冲突。
