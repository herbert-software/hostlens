# inspector-authoring-contract 规范（delta）

> 目的：「容器适用性」需求 EXCLUDE 枚举中「同理未来 K8s 域 inspector……属独立提案」的预言句改为现在时，点名 `k8s.*` 控制面管控类（5 个新 inspector，`targets: [local, ssh]`，契约由 `k8s-inspector-suite` 承载）。需求标题与其余正文、场景逐字不变。

## 修改需求

### 需求:容器适用性——inspector 声明容器类 target（docker / k8s）的判据

一个 inspector **仅当**其采集信号在「collector 命令跑在单个容器的 PID / mount / net namespace 内、读取该容器自身的进程 / 应用 / 文件 / 网络状态」时**正确且有意义**，才**允许**在 `targets` 中声明容器类 target（`docker` / `k8s`）。**禁止**把读取 host 全局硬件 / 内核 / init / 块设备 / 物理内存 / 时钟 / host 包管理 / host 认证状态的 inspector 声明容器类 target——这类信号在容器内要么读不到、要么读到的是 **host 共享值造成误归因**（最危险，因为不报错而是静默报错值；k8s 上读到的是 **node** 全局值，用户连 node 是哪台都未必知道，误归因更隐蔽）、要么容器视角本身误导。

**判据按 collector 的实际读取源逐项判定，禁止按域名通配符整域放行**——同一域内不同 inspector 的读取源可能分属容器隔离与 host 全局两侧（如 `log.exception_burst` 读容器文件 vs `log.tail.error_burst` 读 host journal；`linux.process.zombies` 走 PID namespace vs `linux.process.fd_usage` 读 `/proc/sys` 内核全局 sysctl）。作者必须打开 collector 命令确认其读取的每个源在容器内是否隔离，**不得仅凭 inspector 落在「进程域」「日志域」就声明容器类 target**。

**docker ⇔ k8s 奇偶约束**：容器安全是按读取源判定的**一个属性**，不随容器运行时分裂为两个——KubernetesTarget 与 DockerTarget 同为「exec 进单个容器内跑 shell 命令」、capability 集逐位相同。故允许名单内的 inspector **必须**同时声明 `docker` 与 `k8s`，**禁止**只声明其一。已知的合法打破场景**仅**为未来的 k8s-only 读取源 inspector（如读 `/var/run/secrets/kubernetes.io/serviceaccount/token` 做到期检查——docker 容器内无此文件），届时**必须**同步修改本判据与奇偶 guard 断言，作为一次显式决定。

**k8s pod 语义注记**（判据本体不变，pod 与裸容器的已知差异）：

- k8s 的 net namespace 是 **pod 级共享**（docker 是容器级）——网络类 inspector 在 pod 内看到的是 pod netns 含 sidecar socket，视角更宽**不是误归因**（pod IP 即诊断对象），仍允许声明。
- `shareProcessNamespace: true` 的 pod 内，进程类 inspector 看到 pause + 兄弟容器进程——pod-scope 非 host-scope，安全。
- 多容器 pod 未显式配 `container:` 时 KubernetesTarget 默认 exec 进 `spec.containers[0]`（可能是 sidecar）——属部署配置问题非判据问题，运维文档**必须**载明「多容器 pod 强烈建议显式配 `container:`」。

**collector 禁止裸读 stdin**：容器类 cohort 的 collector **禁止**包含从 stdin 读取输入的裸命令（如无参 `cat`、`awk -f -`）——KubernetesTarget 的 exec 把整个渲染脚本经 stdin 喂给 `/bin/sh` 且 v4 exec 协议无 stdin half-close，裸读 stdin 的命令在 docker 上诚实失败（EOF），在 k8s 上会吞掉脚本尾部的 `exit $?` 后阻塞到 timeout。本约束由作者评审执行（带参 `cat {{log_path}}`、管道中游的过滤命令均合法，token pattern 机械检测假阳太高，**不**设机械 guard）。

- **允许声明容器类 target**（逐项列举，不用整域通配；每项同时声明 `docker` 与 `k8s`）：
  - 应用服务类（容器「一容器一应用」，经容器内 CLI 连本容器内服务）：`nginx.{config_test,error_rate,health}` / `mysql.{connection_usage,replication_lag,slow_queries}` / `postgres.{bloat_tables,connection_usage,long_queries,replication_lag}` / `redis.{memory_usage,persistence,replication_lag,slowlog}`（逐项列举——本契约禁止整域通配，新增同域 inspector 须重新按读取源评审，不自动继承）
  - 语言运行时类（容器内单进程）：`jvm.{gc,heap,threads}` / `go.{goroutines,heap}`
  - 进程级（走 PID namespace 的命令）：**仅** `linux.process.zombies`（`ps axo`）/ `linux.process.critical_alive`（`pgrep`）
  - 应用日志类（读容器自身日志文件）：**仅** `log.exception_burst`（`cat {{log_path}}`，mount namespace）
  - 网络类（容器 netns 视角即为目标视角；k8s 上为 pod netns 视角）：`net.{connections,listening_ports}` / `net.dns.resolve` / `net.dependency.tcp_check` / `net.tls.{cert_expiry,chain_validity}`
- **禁止声明容器类 target**（保持 `local` / `ssh`）：
  - host 硬件：`linux.cpu.*`（cpufreq / throttling / 全局 top_processes）
  - host 块设备与文件系统：`linux.disk.*` / `linux.fs.*`
  - host 共享内核：`linux.kernel.*`（dmesg / oom / taint）
  - host 物理内存与 swap：`linux.memory.*`（容器读 host `/proc/meminfo` 是 host/node 内存，**非** cgroup 限制——误归因）
  - **读 `/proc/sys/*` 内核全局 sysctl 的进程类**：`linux.process.fd_usage`（`/proc/sys/fs/file-nr`）/ `linux.process.total`（`used_pct` 的分母 `/proc/sys/kernel/pid_max`）——`/proc/sys/*` 非 namespace 隔离，容器内读到 host/node 全局值（同样的误归因，与「进程域」名义无关）
  - **读 host systemd journal 的日志类**：`log.tail.error_burst`（`journalctl`）——容器多无 journald（空假阴性）或 bind-mount 到 host journal（误归因），是 host-journal inspector 而非 app-log inspector
  - host init / 调度：`linux.systemd.*` / `linux.cron.*`
  - host 系统级：`linux.system.*`（load_avg / reboot_required）/ `system.uptime`（实抽 host load average，`/proc/uptime` 与 `uptime` 均非 namespace 隔离）
  - host 时钟：`net.ntp.drift`
  - host 包管理与补丁：`pkg.*`
  - host 认证与安全基线：`security.*`
  - 容器自身管控类：`docker.*`（需 docker-in-docker / pod 内无 docker socket，非目标）与 `k8s.*` 控制面管控类（`k8s.pods.{oom_killed,evicted,stuck_pending}` / `k8s.nodes.conditions` / `k8s.events.warnings`——pod OOMKilled / evicted / stuck-pending / node conditions / warning events 是 API server 控制面状态，需 kubectl / API 视角，pod 内无 kubectl；跑在配有 kubeconfig 的管理机上，契约见 `k8s-inspector-suite`）

**capability gate 是兜底而非主防线**：DockerTarget / KubernetesTarget 均不声明 `Capability.SSH`、其 `systemd` capability 靠探测 `systemctl` 是否存在——故要求 `ssh` / `systemd` capability 的 inspector 即便误声明容器类 target 也会被 preflight `requires_unmet` 挡掉。但**误归因类**（如 memory 读 host `/proc/meminfo`、`linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`）capability gate **挡不住**——它们只要 `shell` capability，preflight 不拦，collector 照跑、静默返回 host/node 值。必须靠本判据 + 内容式 meta-guard（见下场景）在作者侧拦住。

#### 场景:应用服务 inspector 允许声明容器类 target

- **当** `redis.memory_usage` 经容器内 `redis-cli` 连接本容器内的 redis 实例采集内存
- **那么** **允许**在其 manifest `targets` 声明 `docker` 与 `k8s`（采集信号是本容器内 redis 的真实状态，容器视角即目标视角；pod 内同理）

#### 场景:host 内存 inspector 禁止声明容器类 target

- **当** `linux.memory.pressure` 读取 `/proc/meminfo` / `/proc/pressure/memory`
- **那么** **禁止**在其 manifest `targets` 声明 `docker` 或 `k8s`——容器内读到的是 host/node 物理内存而非该容器的 cgroup 限制，会造成静默误归因；该 inspector 必须保持 `targets: [local, ssh]`

#### 场景:host 共享资源 inspector 禁止声明容器类 target

- **当** 一个 inspector 读取 host 硬件 / 内核 / 块设备 / init / 时钟 / host 包管理 / host 认证状态（如 `linux.cpu.throttling` / `linux.kernel.oom_killer` / `linux.systemd.failed_units` / `net.ntp.drift` / `pkg.pending_updates` / `security.failed_logins`）
- **那么** **禁止**声明 `docker` 或 `k8s`，必须保持 `local` / `ssh`

#### 场景:读 /proc/sys 内核全局 sysctl 的 inspector 禁止声明容器类 target（与域名无关）

- **当** 一个 inspector 的 collector 读取 `/proc/sys/*`（内核全局 sysctl，非 namespace 隔离），即便它落在「进程域」（如 `linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`、`linux.process.total` 的 `used_pct` 分母读 `/proc/sys/kernel/pid_max`）
- **那么** **禁止**声明 `docker` 或 `k8s`——容器内读到 host/node 全局值造成静默误归因；判据看**读取源**不看域名，同域的 `linux.process.zombies` / `linux.process.critical_alive`（走 PID namespace）才允许

#### 场景:读 host journal 的日志 inspector 禁止声明容器类 target

- **当** 一个日志域 inspector 用 `journalctl` 查 systemd journal（如 `log.tail.error_burst`）而非 `cat` 容器内日志文件
- **那么** **禁止**声明 `docker` 或 `k8s`——容器内多无 journald（空假阴性）或读到 bind-mount 的 host journal（误归因）；同域的 `log.exception_burst`（`cat {{log_path}}` 读容器文件）才允许

#### 场景:内容式 meta-guard 机械拦截误归因类声明容器类 target

- **当** 任一 builtin manifest 的 `collect.command` 含 host 全局读取标记（`/proc/sys/`、`/proc/meminfo`、`journalctl`、`/proc/loadavg`、`/proc/uptime`）
- **那么** 测试套件 **必须**断言该 manifest 的 `targets` **既不含 `docker` 也不含 `k8s`**（内容式 guard，覆盖人工维护的 EXCLUDE 名单之外、防未来作者据域名误加）；**禁止**仅靠人工维护的 INCLUDE/EXCLUDE 名单断言

#### 场景:docker 与 k8s 声明必须满足奇偶不变量

- **当** 测试套件检查任一 builtin manifest 的 `targets`
- **那么** **必须**断言 `("docker" in targets) == ("k8s" in targets)`——容器安全是一个属性，禁止只声明其一造成两套容器 cohort 静默漂移；guard 的 docstring **必须**载明合法打破奇偶的 escape hatch（k8s-only 读取源类 inspector，须同步修改本判据与该断言）

#### 场景:容器类派发路径必须有代表性回放验证

- **当** 一个提案放开一批 inspector 的容器类 target（`docker` 或 `k8s`）支持
- **那么** **必须**至少对「应用服务 / 语言运行时 / 进程级 / 网络」各类中的代表性 inspector 提供经 `ReplayTarget(impersonate="docker")` / `ReplayTarget(impersonate="k8s")`（按所放开的类型）的端到端回放测试，断言 `InspectorResult.status == "ok"`、`misses == []` 且 snapshot 匹配；**禁止**仅靠机械追加 `targets` 值而无任何对应派发路径的测试覆盖
