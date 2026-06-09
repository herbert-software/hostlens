# 提案：放开 Inspector 的 Docker target 支持

## Why

M8 的 `DockerTarget`（`targets/docker.py`，#81）已落地，但它对 Inspector **当前是 inert 的**：`InspectorManifest.targets` 仍是 `Literal["local", "ssh"]`，任何 inspector 声明 `docker` 会在 manifest 加载期被 Pydantic 拒掉；即便绕过，runner preflight 第一步 `target.type not in manifest.targets` 也会判 `requires_unmet`。结果是「有一个能连容器的 target，却没有一个 inspector 能跑在上面」。本提案把 DockerTarget 从 inert 变可用 —— 这是 M8 的 Docker 半边收尾（K8s target 留到独立提案）。

## What Changes

- **MODIFY** `InspectorManifest.targets` 的 Literal 取值域：`["local", "ssh"]` → `["local", "ssh", "docker"]`；`k8s` / `kubernetes` 仍被拒（K8sTarget 尚不存在，放开会造成「manifest 声明了一个没有实现的 target 类型」的不一致）。
- **NEW**（authoring contract 增量）容器适用性规则：定义一个 inspector **何时**可以声明 `docker` —— 仅当其采集信号在「collector 跑在单个容器的 PID/mount/net namespace 内、读取该容器自身的进程/应用/文件/网络状态」时**正确且有意义**；**禁止** host 全局硬件/内核/init 状态类 inspector 声明 `docker`（容器看不到、或看到的是 host 值造成误归因、或容器视角误导）。
- **MODIFY** `ReplayTarget.impersonate` 的 Literal：`["local", "ssh"]` → `["local", "ssh", "docker"]`，使 fixture 可以以 `impersonate: docker` 回放、透明通过 runner preflight 的 `target.type in manifest.targets`，从而对「docker 派发路径」做离线 snapshot 测试。
- **机械改动**：给经评审判定为容器安全的初始 cohort 的 manifest 追加 `docker` 到 `targets`（collector 命令**不动** —— 同一条 collector 仅经 DockerTarget 而非 SSHTarget 派发）。初始 cohort（design.md 出最终 include/exclude 全表与逐项理由）：
  - 应用服务（容器「一容器一应用」的典型场景，逐项非整域）：`nginx.{config_test,error_rate,health}`、`mysql.{connection_usage,replication_lag,slow_queries}`、`postgres.{bloat_tables,connection_usage,long_queries,replication_lag}`、`redis.{memory_usage,persistence,replication_lag,slowlog}`
  - 语言运行时（单进程）：`jvm.*`（gc/heap/threads）、`go.*`（goroutines/heap）
  - 进程级（容器 PID namespace 内有意义）：`linux.process.{zombies,critical_alive}`（`ps axo` / `pgrep` 走 PID namespace）
  - 应用日志：`log.exception_burst`（`cat {{log_path}}` 走 mount namespace 读容器自身日志文件）
  - 网络（容器 netns 视角有意义）：`net.{connections,listening_ports}` / `net.dns.resolve` / `net.dependency.tcp_check` / `net.tls.{cert_expiry,chain_validity}`
- **明确排除**（保持 `local`/`ssh`，design 给逐项理由）：`linux.cpu.*`（host 硬件）/ `linux.disk.*` / `linux.fs.*`（host 块设备与挂载）/ `linux.kernel.*`（共享 host 内核 dmesg）/ `linux.memory.*`（容器读 host `/proc/meminfo` 误归因）/ `linux.process.{fd_usage,total}`（读 `/proc/sys/fs/file-nr` 与 `/proc/sys/kernel/pid_max`——**内核全局 sysctl，非 namespace 隔离**，容器内静默报 host 值）/ `linux.systemd.*` / `linux.cron.*` / `linux.system.*`（load_avg / reboot_required 均 host 级）/ `system.uptime`（实抽 host load average，非 namespace 隔离）/ `log.tail.error_burst`（`journalctl` 读 host systemd journal，**非** app 日志文件）/ `net.ntp.drift`（host 时钟）/ `pkg.*`（host 打补丁语义）/ `security.*`（host 认证日志）/ `docker.*`（需 docker-in-docker，非目标）。

### 非目标（Non-Goals）

- **不实现 KubernetesTarget**，不放开 `k8s` target 类型 —— 这是后续独立提案。
- **不改任何 collector 命令 / parse 逻辑 / output_schema / findings** —— 本提案只动 `targets` 字段与三处 Literal 取值域；collector 正确性已由既有 local/ssh fixture 锁定。
- **不新增 manifest 字段、不 enable `hook.py`、不加 capability 值、不加 parse format**（沿用 authoring-contract「零新 infra」纪律）。
- **不实现容器内 cgroup-aware 采集**（如「读 cgroup 限制而非 host /proc/meminfo」）—— 这正是把 memory 类 inspector 排除在外的理由，cgroup-aware 是未来独立提案。
- **不改 DockerTarget 本身**（capability 探测 / 故障分类 / 10MB 上限等已由 `docker-execution-target` spec 锁定）。

## Capabilities

### 修改功能（Modified Capabilities）

- **`inspector-plugin-system`** —— `InspectorManifest.targets` 字段的 Literal 取值域与「targets 必须非空且仅含允许值」场景：`docker` 由「必须 raise」改为「必须接受」；`kubernetes` / `k8s` 仍「必须 raise」。
- **`inspector-authoring-contract`** —— 新增「容器适用性」需求：规定 inspector 声明 `docker` 的判据（容器 namespace 内信号正确性）与 host 级 inspector 禁止声明 `docker` 的约束。
- **`replay-execution-target`** —— `ReplayTarget.impersonate` 的 Literal 取值域加 `docker`，使 docker 派发路径可被离线 fixture 回放验证。

### 新增功能（New Capabilities）

无 —— Docker 执行能力由既有 `docker-execution-target` spec 提供；本提案只放开 inspector 侧的接入门。

## Impact

- **代码**：
  - `src/hostlens/inspectors/schema.py:586` —— `targets` Literal 加 `docker`。
  - `src/hostlens/targets/replay.py` —— **两处** Literal 都要加 `docker`：fixture 模型的 `impersonate` 字段（:87）**与**实例属性 `self.type: Literal["local","ssh"] = data.impersonate`（:141）；漏改后者则 `impersonate="docker"` 在 mypy 阶段报错。
  - 初始 cohort 各 manifest 的 `targets:` 行（机械追加 `docker`）。
  - `src/hostlens/inspectors/recorder.py:291` —— `impersonate = target.type if target.type in ("local","ssh") else "local"` 的允许集是否纳入 `docker`：仅当选择「对真容器录制 fixture」时才需要；design 决策（默认倾向手写/ReplayTarget 回放，避免触碰 recorder）。
- **测试**：新增 docker 派发路径的代表性测试（schema 接受 docker / 拒 k8s；preflight target-type 匹配；DockerTarget capability gate 对 host-only inspector 判 requires_unmet；≥1 个 inspector 经 `ReplayTarget(impersonate=docker)` 端到端 `ok`）。
- **依赖**：无新增依赖。
- **文档**：`docs/operations/inspectors.md` 增「哪些 inspector 可跑在 docker target / 容器视角注意事项」一节。

## Failure Modes

1. **容器无 `/bin/sh`（distroless/极简镜像）**：DockerTarget `exec_run` 抛 `APIError` → 既有契约归 `TargetError(kind="exec_failed")` → runner 译为 `target_unreachable`，单个 inspector 失败隔离、不冒泡毁整轮。降级：报告里该 inspector 标 unreachable，其余照常。
2. **容器缺采集所需二进制**（如 alpine 无 `ss`/`mysql` client）：preflight `requires_binaries` 探测 `command -v` 非 0 → `requires_unmet(["bin:xxx"])`，不执行主命令、不误报。
3. **误把 host-only inspector 声明成 docker**（评审漏判）：collector 在容器内读到 host 共享值（如 `/proc/meminfo` 是 host 内存）→ 静默误归因，**最危险**因为不报错。缓解：design 的逐项理由表 + 代码 review 把每个 cohort 成员的容器语义写清；capability gate 兜底挡掉 systemd 类（容器无 systemctl → SYSTEMD cap 探测失败）。
4. **inspector 要求 `ssh` capability 却派到 docker target**：DockerTarget 不声明 `Capability.SSH` → preflight step 2 `requires_unmet(["ssh"])`，正确拒绝。
5. **fixture impersonate=docker 但实现未放开 Literal**：Pydantic 校验 fixture 时 raise → 测试加载期失败（fail-loud，非静默跑错 target 类型）。

## Operational Limits

- **并发预算**：不变 —— 派发并发由 orchestration pipeline 控制，target 类型不影响并发模型。
- **内存预算**：不变；`read_file` 10MB 上限由 DockerTarget 已锁定（`get_archive` 边读边累计中止）。
- **超时**：沿用 preflight 既有超时（`command -v` 10s、`[ -r ]` 5s）+ inspector `collect.timeout`；DockerTarget `exec_run` 受同一 timeout 约束。

## Security & Secrets

- **不引入新密钥**：服务类 inspector 的 `HOSTLENS_*` secret 注入路径（env → DockerTarget.exec 的 env）沿用既有契约。
- **攻击面**：`targets` Literal 多一个合法值 `docker`，不放宽任何注入防线（shlex.quote 三件套、字段正则、path component 校验全部不变）。docker socket 路径非敏感（已由 `docker-execution-target` spec 明确不脱敏）。
- **不扩大暴露**：MCP surface 不变 —— inspector 是否可跑在 docker 是 target 侧的事，`run_inspector` MCP 工具的 schema/敏感性声明不动。

## Cost / Quota Impact

- **零 LLM 影响**：本提案纯属 inspector/target 接入层，不调用 LLM、不改 Agent loop、不影响 prompt cache。token 消耗 0、API 调用频次不变、对 Anthropic 配额无影响。

## Demo Path

5 分钟内、无 SSH / 无付费 API 的 cassette/replay 路径优先：

1. **schema 门**：`python -c "from hostlens.inspectors.schema import InspectorManifest; ..."` 加载一个 `targets: [docker]` 的 manifest → 成功；改成 `targets: [k8s]` → `ValidationError`。
2. **离线派发**：`pytest tests/inspectors/test_docker_target_dispatch.py -q` —— 用 `ReplayTarget(impersonate="docker")` 回放一个服务类 inspector，断言 `InspectorResult.status == "ok"` 且 snapshot 匹配。
3. **（可选，需 docker daemon）真容器**：`docker run -d --name hl-redis redis:7`，然后在 `~/.config/hostlens/targets.yaml` 手写一个 docker target（`hostlens target add` 当前**只支持** `--type local|ssh`，docker target 经 yaml 配置——CLI 写入 docker target 留作独立 follow-up）：

   ```yaml
   targets:
     - name: hl-redis
       type: docker
       container: hl-redis
   ```

   再 `hostlens inspect hl-redis --inspector redis.memory_usage` → 输出容器内 redis 的真实内存采集。
