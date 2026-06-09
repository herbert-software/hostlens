# 设计：放开 Inspector 的 Docker target 支持

## Context

`DockerTarget`（`src/hostlens/targets/docker.py`，#81）已实现完整 `ExecutionTarget` 协议：`exec` 以 `["/bin/sh","-c",cmd]` 在容器内执行、`read_file` 经 `get_archive` 边读边累计 10MB 上限、capability 探测（`SHELL`/`FILE_READ` 基线 + 运行时探测 `SYSTEMD`/`DOCKER_CLI`）、故障分类（`docker_unavailable`/`container_not_found`/`exec_failed`/`file_too_large`）。

但 inspector 侧两道门把它锁在外面：

1. **manifest 加载门**：`schema.py:586` `targets: Annotated[list[Literal["local","ssh"]], Field(min_length=1)]`——声明 `docker` 直接 Pydantic 报错。
2. **runner preflight 门**：`runner.py:499` `if target.type not in manifest.targets: return "requires_unmet", ["target_type"]`——即便绕过加载门，docker target 也被判 unmet。

第二道门是**有意的双重 gate**（见 memory `project_new_target_type_inert_until_inspector_targets_widened`）：加 ExecutionTarget 只过 `base.py`，inspector 默认对新 type inert，「让 inspector 支持容器」是独立的、需逐项评审容器安全的 follow-up。本提案就是这个 follow-up 的 Docker 半边。

约束：CLAUDE.md §4.2（Inspector 是 SOT）、§6（async-first / mypy strict / 真 fixture）、authoring-contract「零新 infra」纪律（不加字段 / 不 enable hook / 不加 parse format / 不加 capability 值）。

## Goals / Non-Goals

**Goals**：
- 把 `docker` 加入 `InspectorManifest.targets` 与 `ReplayTarget.impersonate` 的 Literal 取值域。
- 建立「容器适用性」作者判据（spec 化），逐项评审现有 65 个 inspector 的容器语义，给容器安全的 cohort 追加 `docker`。
- 用 `ReplayTarget(impersonate="docker")` 的离线回放证明 docker 派发路径端到端跑通。

**Non-Goals**：
- 不实现 KubernetesTarget、不放开 `k8s`。
- 不改任何 collector / parse / output_schema / findings。
- 不做 cgroup-aware 采集（这正是排除 memory 类的理由）。
- 不对每个被放开的 inspector 都录真容器 fixture（见 Decision 3）。

## Decisions

### Decision 1：取值域加 `docker` 但**不**加 `k8s`

`targets` Literal 与 `impersonate` Literal 都只加 `docker`，`k8s`/`kubernetes` 继续被拒。

**理由**：`ExecutionTarget.type` 的 Literal 本就是 `["local","ssh","docker","k8s"]`（execution-target spec 已锁定 4 种），但 KubernetesTarget **尚未实现**。若现在放开 inspector 声明 `k8s`，会产生「manifest 合法声明了一个没有任何 target 实现能匹配的类型」——加载通过、运行永远 `requires_unmet`，是误导性的死代码。等 KubernetesTarget 提案落地时一并放开 `k8s` 才自洽。

**备选（弃）**：一次放开 docker+k8s 对齐 `ExecutionTarget.type` 全集——弃，因为 k8s 无实现，且容器 vs pod 的语义判据不同（pod 多容器、sidecar、ephemeral），需独立评审。

### Decision 2：容器适用性靠**作者判据 + 代码 review**，capability gate 仅兜底

哪些 inspector 可声明 `docker` 由 authoring-contract 的判据决定，逐项人工评审，**不**引入任何运行时机器门来自动判定。

**理由**：危险的是**误归因类**——`linux.memory.*` 在容器内读 `/proc/meminfo` 返回的是 host 物理内存而非 cgroup 限制，**不报错**、静默给出错误数值。capability gate（preflight step 2）只能挡住「要求 `ssh`/`systemd` capability 而 DockerTarget 没有」的 inspector，挡不住误归因。所以容器语义正确性必须在作者侧由人判定，spec 把判据写死成可评审的 include/exclude 表。

**备选（弃）**：给 manifest 加 `container_safe: bool` 字段做机器门——违反 authoring-contract「零新字段」纪律，且 bool 无法表达「为什么安全」，软分类会失控（见 §7 反模式「软分类一定失控」）。

### Decision 3：docker 派发用 **ReplayTarget(impersonate=docker) 回放**证明，不录真容器 fixture

证明 docker 派发路径不需要对每个放开的 inspector 录真容器 fixture。collector 命令与 target 类型**正交**——同一条 collector 经 DockerTarget 还是 SSHTarget 派发，命令字符串与 parse 逻辑完全相同，collector 正确性已被既有 local/ssh fixture 锁定。docker 路径**新增的风险只有**：

- (a) schema 接受 `docker` / 拒 `k8s`
- (b) preflight `target.type in manifest.targets` 对 docker 匹配
- (c) DockerTarget capability gate 对 host-only inspector 判 `requires_unmet`
- (d) 端到端：docker-typed target → InspectorResult `ok`

(a)–(c) 是单元测试；(d) 用 `ReplayTarget(impersonate="docker")` 回放代表性 inspector（每类一个：服务 / 运行时 / 进程 / 网络）即可证明。

**理由**：避免 28 个 inspector × 录真容器 fixture 的巨量工作与 CI docker 依赖。`ReplayTarget` 的 `impersonate` 正是为「让 preflight `target.type in manifest.targets` 透明通过」而设计（replay-execution-target spec §运行时 type 冒充）。

**recorder.py 处理**：docker 派发的**测试**走回放 + 手写/翻转 fixture（`impersonate: docker`），**不**依赖 recorder 录真容器。但 `recorder.py:291` 原 `impersonate = target.type if target.type in ("local","ssh") else "local"` 有个**静默 coerce 隐患**——docker 进入 `ReplayTarget.impersonate` Literal 后，若有人拿 DockerTarget 调 recorder，该行会把 docker 静默塌成 `"local"`，落地一个 target-type 标错的 fixture（命令/输出对、标签错）。这与本提案别处的 fail-loud 纪律（`_Fixture.impersonate` Literal 拒非法值、preflight fail-loud）不一致。故 review 后**收紧 recorder**：透传 `local`/`ssh`/`docker`（与 `ReplayTarget.impersonate` Literal 对齐，docker 录制现会正确标 docker），对其余未实现类型（`k8s`/`replay`/未知）**fail-loud** `raise ConfigError(kind="recorder_unsupported_target_type")`，把「recorder 只产合法 impersonate 值」从隐式约定变成强制断言；`RecordedFixture.impersonate` 类型由 `str` 收紧为 `Literal["local","ssh","docker"]`。这是**最小正确性硬化**，**不**新增对真容器的录制工作流（仍无 shipped 路径主动录 docker），只消除静默错标。

**备选（弃）**：起真 redis/nginx 容器跑集成测试——弃为**默认**路径（引入 CI docker 依赖、慢、flaky），但保留为 Demo Path 第 3 步（可选、需本地 docker daemon）的人工验证。

### Decision 4：初始 cohort 的最终 include/exclude 全表

逐项评审 65 个 inspector，按 authoring-contract 判据分类：

**INCLUDE（追加 `docker`，共 28 个）**：
| 域 | inspector | 容器语义 |
|---|---|---|
| nginx | config_test / error_rate / health | 容器内 nginx 的配置与健康 |
| mysql | connection_usage / replication_lag / slow_queries | 容器内 mysql 实例 |
| postgres | bloat_tables / connection_usage / long_queries / replication_lag | 容器内 pg 实例 |
| redis | memory_usage / persistence / replication_lag / slowlog | 容器内 redis 实例 |
| jvm | gc / heap / threads | 容器内单 JVM 进程 |
| go | goroutines / heap | 容器内单 Go 进程 |
| linux.process | zombies / critical_alive | 容器 PID namespace（`ps axo` / `pgrep`）|
| log | exception_burst | 容器内应用日志文件（`cat {{log_path}}`，mount namespace）|
| net | connections / listening_ports / dns.resolve / dependency.tcp_check / tls.cert_expiry / tls.chain_validity | 容器 netns 视角 |

**EXCLUDE（保持 local/ssh，共 37 个）**：`linux.cpu.*`（3）/ `linux.disk.*`（3）/ `linux.fs.*`（3）/ `linux.kernel.*`（3）/ `linux.memory.*`（3）/ `linux.process.{fd_usage,total}`（2，读 `/proc/sys/*` 内核全局 sysctl，见下注）/ `linux.systemd.*`（3）/ `linux.cron.*`（2）/ `linux.system.*`（2，load_avg / reboot_required）/ `system.uptime`（1，实抽 host load average）/ `log.tail.error_burst`（1，`journalctl` 读 host systemd journal）/ `net.ntp.drift`（1）/ `pkg.*`（3）/ `security.*`（3）/ `docker.*`（3，docker-in-docker）/ `hello.echo`（1，纯 demo）。

> **关键注（误归因，review 抓出）**：`linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`、`linux.process.total` 的 `used_pct = total / pid_max` 中 `pid_max` 读 `/proc/sys/kernel/pid_max`——`/proc/sys/*` sysctl 是**内核全局、非 PID/mount namespace 隔离**，容器内读到的是 **host 全局值**。`total` 的分子（`ps -e`）虽是容器 PID namespace 正确，但分母 `pid_max` 是 host 全局 → `used_pct` 是 scope 混淆的无意义比率、`fd_usage` 更是 100% host 值零容器信号。二者均属本提案「最危险」的静默误归因类，**EXCLUDE**。`linux.process` 仅 `zombies`（`ps axo` 走 PID namespace）与 `critical_alive`（`pgrep`）容器正确，故 INCLUDE。同理 `log.tail.error_burst` 用 `journalctl` 查 systemd journal（容器多无 journald → 空假阴性；host journal bind-mount → 误归因），是 host-journal inspector 而非 app-log inspector，**EXCLUDE**；只有 `log.exception_burst`（`cat {{log_path}}` 读容器自身日志文件）容器正确。

## Risks / Trade-offs

- **[误归因类漏判]** 评审把某 host-shared inspector 错放进 INCLUDE → 容器内静默报 host 值 → 缓解：spec 的逐项 include/exclude 表 + 代码 review 双签；capability gate 对 systemd/ssh 类兜底；`linux.memory.*` 等高危误归因类在 spec 场景里点名 EXCLUDE 作为可测试断言。
- **[net.* 容器视角差异]** `net.connections`/`listening_ports` 在容器 netns 看到的是容器端口而非 host 端口——这是**特性不是 bug**（诊断该容器的网络），但用户若期望 host 视角会困惑 → 缓解：docs/operations/inspectors.md 写明「docker target 上 net.* 是容器 netns 视角」。
- **[alpine/distroless 缺二进制]** 服务类 inspector 需要 `mysql`/`redis-cli`/`ss` 等，极简镜像没有 → preflight `requires_binaries` 探测兜底判 `requires_unmet`，不误报；distroless 无 `/bin/sh` → DockerTarget 归 `exec_failed`→`target_unreachable`，失败隔离。
- **[回放证明的局限]** Decision 3 的回放不验证真 DockerTarget 的 `exec_run`/`get_archive` 行为（那是 docker-execution-target spec 的测试职责）→ 接受：本提案职责边界是「inspector 接入门」，DockerTarget 行为已由其自身 spec 锁定，Demo Path 第 3 步提供真容器人工兜底。

## Migration Plan

- **无数据迁移 / 无破坏性变更**：现存 manifest 全部保持 `local`/`ssh` 合法；本提案只**放宽** Literal（加一个允许值）+ **追加**部分 manifest 的 `docker`。旧 fixture（`impersonate: local/ssh`）不受影响。
- **回滚**：纯加法。回滚 = revert PR（移除 Literal 的 docker + 移除各 manifest 的 docker 行 + 删新增测试），无状态残留。
- 部署：feature branch `feat/enable-docker-inspector-targets` → PR → CI 绿 → squash merge。

## Open Questions

- **net.tls.chain_validity 是否 INCLUDE？**（已收敛为决定，不留悬置）`net.*` 6 个全 INCLUDE，`net.ntp.drift` 已 EXCLUDE（host 时钟）。`net.tls.chain_validity` collector 是从**容器 netns** 用 `openssl s_client -connect <endpoint>` 探外部证书链——这是合法的「从容器所在网络位置看到的链」视角（容器与 host 的网络路径 / CA bundle 可能不同），故 **INCLUDE**。放开无害（容器内有 openssl 即可，无则 preflight `requires_binaries` 兜底）。
- **是否需要 doctor 体现 docker target 上可跑的 inspector 数？** 倾向否——doctor 已有 inspector section，docker 可跑性是 per-target × per-inspector 的笛卡尔积，列出会噪声；留给 `hostlens inspect <docker-target>` 实跑时的 preflight 反馈。
