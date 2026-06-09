# inspector-authoring-contract 规范增量

## ADDED Requirements

### 需求:容器适用性——inspector 声明 docker target 的判据

一个 inspector **仅当**其采集信号在「collector 命令跑在单个容器的 PID / mount / net namespace 内、读取该容器自身的进程 / 应用 / 文件 / 网络状态」时**正确且有意义**，才**允许**在 `targets` 中声明 `docker`。**禁止**把读取 host 全局硬件 / 内核 / init / 块设备 / 物理内存 / 时钟 / host 包管理 / host 认证状态的 inspector 声明 `docker`——这类信号在容器内要么读不到、要么读到的是 **host 共享值造成误归因**（最危险，因为不报错而是静默报错值）、要么容器视角本身误导。

**判据按 collector 的实际读取源逐项判定，禁止按域名通配符整域放行**——同一域内不同 inspector 的读取源可能分属容器隔离与 host 全局两侧（如 `log.exception_burst` 读容器文件 vs `log.tail.error_burst` 读 host journal；`linux.process.zombies` 走 PID namespace vs `linux.process.fd_usage` 读 `/proc/sys` 内核全局 sysctl）。作者必须打开 collector 命令确认其读取的每个源在容器内是否隔离，**不得仅凭 inspector 落在「进程域」「日志域」就声明 `docker`**。

- **允许声明 `docker`**（逐项列举，不用整域通配）：
  - 应用服务类（容器「一容器一应用」，经容器内 CLI 连本容器内服务）：`nginx.{config_test,error_rate,health}` / `mysql.{connection_usage,replication_lag,slow_queries}` / `postgres.{bloat_tables,connection_usage,long_queries,replication_lag}` / `redis.{memory_usage,persistence,replication_lag,slowlog}`（逐项列举——本契约禁止整域通配，新增同域 inspector 须重新按读取源评审，不自动继承）
  - 语言运行时类（容器内单进程）：`jvm.{gc,heap,threads}` / `go.{goroutines,heap}`
  - 进程级（走 PID namespace 的命令）：**仅** `linux.process.zombies`（`ps axo`）/ `linux.process.critical_alive`（`pgrep`）
  - 应用日志类（读容器自身日志文件）：**仅** `log.exception_burst`（`cat {{log_path}}`，mount namespace）
  - 网络类（容器 netns 视角即为目标视角）：`net.{connections,listening_ports}` / `net.dns.resolve` / `net.dependency.tcp_check` / `net.tls.{cert_expiry,chain_validity}`
- **禁止声明 `docker`**（保持 `local` / `ssh`）：
  - host 硬件：`linux.cpu.*`（cpufreq / throttling / 全局 top_processes）
  - host 块设备与文件系统：`linux.disk.*` / `linux.fs.*`
  - host 共享内核：`linux.kernel.*`（dmesg / oom / taint）
  - host 物理内存与 swap：`linux.memory.*`（容器读 host `/proc/meminfo` 是 host 内存，**非** cgroup 限制——误归因）
  - **读 `/proc/sys/*` 内核全局 sysctl 的进程类**：`linux.process.fd_usage`（`/proc/sys/fs/file-nr`）/ `linux.process.total`（`used_pct` 的分母 `/proc/sys/kernel/pid_max`）——`/proc/sys/*` 非 namespace 隔离，容器内读到 host 全局值（同样的误归因，与「进程域」名义无关）
  - **读 host systemd journal 的日志类**：`log.tail.error_burst`（`journalctl`）——容器多无 journald（空假阴性）或 bind-mount 到 host journal（误归因），是 host-journal inspector 而非 app-log inspector
  - host init / 调度：`linux.systemd.*` / `linux.cron.*`
  - host 系统级：`linux.system.*`（load_avg / reboot_required）/ `system.uptime`（实抽 host load average，`/proc/uptime` 与 `uptime` 均非 namespace 隔离）
  - host 时钟：`net.ntp.drift`
  - host 包管理与补丁：`pkg.*`
  - host 认证与安全基线：`security.*`
  - docker 自身管控类：`docker.*`（需 docker-in-docker，非目标）

**capability gate 是兜底而非主防线**：DockerTarget 不声明 `Capability.SSH`、其 `systemd` capability 靠探测 `systemctl` 是否存在——故要求 `ssh` / `systemd` capability 的 inspector 即便误声明 `docker` 也会被 preflight `requires_unmet` 挡掉。但**误归因类**（如 memory 读 host `/proc/meminfo`、`linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`）capability gate **挡不住**——它们只要 `shell` capability，preflight 不拦，collector 照跑、静默返回 host 值。必须靠本判据 + 内容式 meta-guard（见下场景）在作者侧拦住。

#### 场景:应用服务 inspector 允许声明 docker

- **当** `redis.memory_usage` 经容器内 `redis-cli` 连接本容器内的 redis 实例采集内存
- **那么** **允许**在其 manifest `targets` 声明 `docker`（采集信号是本容器内 redis 的真实状态，容器视角即目标视角）

#### 场景:host 内存 inspector 禁止声明 docker

- **当** `linux.memory.pressure` 读取 `/proc/meminfo` / `/proc/pressure/memory`
- **那么** **禁止**在其 manifest `targets` 声明 `docker`——容器内读到的是 host 物理内存而非该容器的 cgroup 限制，会造成静默误归因；该 inspector 必须保持 `targets: [local, ssh]`

#### 场景:host 共享资源 inspector 禁止声明 docker

- **当** 一个 inspector 读取 host 硬件 / 内核 / 块设备 / init / 时钟 / host 包管理 / host 认证状态（如 `linux.cpu.throttling` / `linux.kernel.oom_killer` / `linux.systemd.failed_units` / `net.ntp.drift` / `pkg.pending_updates` / `security.failed_logins`）
- **那么** **禁止**声明 `docker`，必须保持 `local` / `ssh`

#### 场景:读 /proc/sys 内核全局 sysctl 的 inspector 禁止声明 docker（与域名无关）

- **当** 一个 inspector 的 collector 读取 `/proc/sys/*`（内核全局 sysctl，非 namespace 隔离），即便它落在「进程域」（如 `linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`、`linux.process.total` 的 `used_pct` 分母读 `/proc/sys/kernel/pid_max`）
- **那么** **禁止**声明 `docker`——容器内读到 host 全局值造成静默误归因；判据看**读取源**不看域名，同域的 `linux.process.zombies` / `linux.process.critical_alive`（走 PID namespace）才允许

#### 场景:读 host journal 的日志 inspector 禁止声明 docker

- **当** 一个日志域 inspector 用 `journalctl` 查 systemd journal（如 `log.tail.error_burst`）而非 `cat` 容器内日志文件
- **那么** **禁止**声明 `docker`——容器内多无 journald（空假阴性）或读到 bind-mount 的 host journal（误归因）；同域的 `log.exception_burst`（`cat {{log_path}}` 读容器文件）才允许

#### 场景:内容式 meta-guard 机械拦截误归因类声明 docker

- **当** 任一 builtin manifest 的 `collect.command` 含 host 全局读取标记（`/proc/sys/`、`/proc/meminfo`、`journalctl`、`/proc/loadavg`、`/proc/uptime`）
- **那么** 测试套件 **必须**断言该 manifest 的 `targets` **不含** `docker`（内容式 guard，覆盖人工维护的 EXCLUDE 名单之外、防未来作者据域名误加）；**禁止**仅靠人工维护的 INCLUDE/EXCLUDE 名单断言

#### 场景:docker 派发路径必须有代表性回放验证

- **当** 本提案放开一批 inspector 的 `docker` target 支持
- **那么** **必须**至少对「应用服务 / 语言运行时 / 进程级 / 网络」各类中的代表性 inspector 提供经 `ReplayTarget(impersonate="docker")` 的端到端回放测试，断言 `InspectorResult.status == "ok"`、且 snapshot 匹配；**禁止**仅靠机械追加 `targets: docker` 而无任何 docker 派发路径的测试覆盖
