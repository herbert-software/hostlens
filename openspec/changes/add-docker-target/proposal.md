## 为什么

M1 落地了 `ExecutionTarget` 抽象的两个实现（`LocalTarget` / `SSHTarget`），证明了「新 target = 实现一个 Protocol，不改 Inspector」的扩展点。但 Hostlens 至今**只能巡检裸机 / VM**——容器化部署（一个 host 上跑 N 个隔离容器、每个容器有独立 rootfs / 进程空间）无法直接巡检。要让 59+ 个内置 Inspector 复用到容器场景，需要 `ExecutionTarget` 的第三个并发实现。

这是 M8 的第一步：`DockerTarget`。`base.py` 早已为它预留了 `Capability.DOCKER_CLI` 与 `type: Literal[..., "docker", ...]`，且 `docs/ARCHITECTURE.md §5` 已把 Docker 列入四种 target 类型。本提案把这块「文档已预期但代码未落地」的空缺补上，同时作为「业务通用化、可扩展」的第二个硬证明（继 Notifier 之后）。Kubernetes target 是独立的 follow-up 提案（`add-kubernetes-target`），不在本提案范围。

## 变更内容

- **新增 `DockerTarget`**（`src/hostlens/targets/docker.py`）：`ExecutionTarget` 的第三个实现，对**已存在的**容器做只读 `exec` / `read_file`。
  - `type = "docker"`（class-level 常量，镜像 `SSHTarget.type` 写法）。
  - `exec` 走 docker-py `container.exec_run(cmd, environment=env)`：`env` 经 `environment=` 参数注入，**禁止**字符串拼接 `export VAR=...; cmd`（继承 §4.3 与 SSHTarget 约定）。
  - `read_file` 走 `container.get_archive(path)` 取 tar stream 解出单文件，沿用 10MB 上限（超限 raise `TargetError(kind="file_too_large")`）。
  - `capabilities` 初始 `{SHELL, FILE_READ}`；首次 `exec` 后 lazy probe `DOCKER_CLI`（容器内有 `docker` 罕见）/ `SYSTEMD`，与 LocalTarget/SSHTarget 探测时机一致。
- **新增 `DockerEntry` 配置条目**（`src/hostlens/targets/config.py`）：`type: Literal["docker"]`，加入 `TargetEntry` discriminated union；Docker-specific 字段 = `{container, docker_host}`（`container` 必填——容器 name 或 id；`docker_host` 可选——默认本机 `unix:///var/run/docker.sock`）。沿用既有 `name` 正则与通用字段（`enabled` / `display_name` / `description` / `tags`）。
- **registry 工厂加分支**（`src/hostlens/targets/registry.py`）：`build_registry_from_config` 增加 `entry.type == "docker"` 分支构造 `DockerTarget`。
- **docker-py 作为 optional-dep**：`pip install "hostlens[docker]"`（镜像 `[mcp]` 模式）；核心包不强依赖 docker-py，未装时构造 DockerTarget raise 清晰的 `TargetError(kind="docker_sdk_unavailable")`。
- **async-first 包裹**：docker-py 是同步 SDK，所有阻塞调用（`exec_run` / `get_archive` / `containers.get`）必须 `asyncio.to_thread` 包裹；`timed_out` 语义由外层 `asyncio.wait_for` 实现（docker-py exec_run 无原生 timeout）。

## 功能 (Capabilities)

### 新增功能
- `docker-execution-target`: `DockerTarget` 的完整契约——基于 docker-py 的 `exec` / `read_file` 实现、env 注入边界、`timed_out`/`exit_code` 分离、容器引用解析、错误分类（容器不存在 / daemon 不可达 / socket 权限 / SDK 未安装）、capabilities 运行时探测、真实 docker 容器集成测试约定。镜像 `ssh-execution-target` 的结构。

### 修改功能
- `execution-target`: `TargetsConfig` 需求的 discriminator `Literal["local", "ssh"]` 扩展为接受 `"docker"`，并新增 DockerEntry-specific 字段集 `{container, docker_host}`（`extra="forbid"`）；同步更新规范开头「不含 docker / k8s target」的 scope 描述句（docker 现落地）。`ExecutionTarget` Protocol / `Capability` Enum / `CAPABILITY_ALLOWLIST` / doctor `targets` 检查 / `target test` 均**不变**（type Literal 与 `DOCKER_CLI` 已预留、doctor/target test 已是 type-agnostic）。

## 影响

- **代码**：新增 `src/hostlens/targets/docker.py`；改 `config.py`（DockerEntry + union）、`registry.py`（docker 分支）；`pyproject.toml` 加 `[project.optional-dependencies] docker = ["docker>=7"]` + dev 自引用 `hostlens[docker]`（CI 需要跑集成测试）。
- **对外契约影响（CLI 命令）**：`hostlens target list/test`、`hostlens doctor` 自动覆盖 docker target（无需新命令，复用既有 type-agnostic 逻辑）；`targets.yaml` 新增合法 `type: docker`。
- **对外契约影响（其余）**：Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest **均不变**——DockerTarget 通过既有 `ExecutionTarget` 抽象接入，Inspector / Agent / Scheduler 对 target 类型无感知。
- **依赖**：新增 optional `docker>=7`（docker-py，仅 `[docker]` extra 安装）；核心包依赖不变。
- **测试**：新增 `tests/targets/test_docker_integration.py`，用真实 docker 容器（不 mock docker-py），CI 无 docker daemon 时 skip。

## Non-Goals（非目标）

- ❌ **KubernetesTarget** —— 独立 follow-up 提案 `add-kubernetes-target`（M8 第二步，引入 `kubernetes-asyncio` 与 `K8S_EXEC` capability）。
- ❌ **容器生命周期管理**（create / start / stop / rm / restart）—— DockerTarget 只对**已存在**的容器做只读 exec / read_file，不管理容器状态。
- ❌ **任何写类 capability**（`FILE_WRITE` 等）—— 归 M9 Remediation；本提案纯只读。
- ❌ **Docker Swarm / docker-compose 编排** —— 单容器引用，不解析 compose 拓扑。
- ❌ **远程 docker over TCP + TLS 的完整凭据体系** —— 本提案默认本机 unix socket（`docker_host` 字段预留，远程 TLS 凭据加载留 follow-up）。
- ❌ **给 Agent 暴露任意容器 exec** —— Agent 只通过受限 Inspector 调 target（§7 红线），不新增 `exec_arbitrary_command` 类工具。
- ❌ **让现有 Inspector 在 docker target 上运行** —— 本提案交付的是 **DockerTarget 执行层**（`exec` / `read_file` / `target test` / `doctor` 连通性可用），**不**改 Inspector manifest 契约。现状 inspector manifest 的 `targets` 字段值域是 `Literal["local", "ssh"]`（`src/hostlens/inspectors/schema.py`），runner preflight 对 `target.type not in manifest.targets` 返回 `requires_unmet/target_type`——故**任何现有 inspector 跑在 docker target 上都会干净地报 `requires_unmet`、不产 finding（不崩溃）**。要让 inspector 真正巡检容器，需独立 follow-up：把 inspector manifest `targets` Literal 扩入 `docker`（MODIFY `inspector-authoring-contract`）+ 逐个评审哪些 inspector 是「容器安全」的并 opt-in。这块**明确不在本提案范围**，是 M8-docker 之后的下一步。

## Failure Modes

1. **docker daemon 不可达 / socket 权限不足**：构造或首次 exec 时 `docker.errors.DockerException` → 包装为 `TargetError(kind="docker_unavailable", target=name)`；doctor 标 `connectivity: "failed"` 使 doctor exit 1，不冒泡为未捕获异常。
2. **目标容器不存在 / 已停止**：`containers.get(ref)` 抛 `NotFound` / 容器 `status != running` → `TargetError(kind="container_not_found", target=name)` / `TargetError(kind="container_not_running", target=name)`；`target test` exit 1。
3. **docker-py SDK 未安装**（用户没装 `[docker]` extra）：构造 DockerTarget 时 import 失败 → `TargetError(kind="docker_sdk_unavailable", target=name)` 附 `pip install "hostlens[docker]"` 提示，**不**让裸 `ImportError` 冒泡。
4. **exec 超时**：长命令超过 `timeout` → 外层 `asyncio.wait_for` 取消 to_thread → 返回 `ExecResult(timed_out=True, exit_code=None)`（满足 ExecResult 不变量）；尽力 close exec 实例（docker exec 进程不在 hostlens 进程组，清理由 daemon 负责，与 SSHTarget 同语义）。
5. **read_file 超 10MB**：边界 `> 10MB`（恰好 10MB 放行，与 local/ssh 一致）。累计读取是无条件 backstop（边读 tar 边累计、>10MB 立即中止 raise `TargetError(kind="file_too_large")`，不缓冲完整大文件）；`get_archive` 的 stat header 给出可信 size 时额外允许在解 tar 前提前 raise（优化，非唯一防线）。

## Operational Limits

- **并发预算**：DockerTarget 复用单个 docker-py client（per-target）；并发 exec 通过 `asyncio.to_thread` 落到默认线程池（受 `asyncio` default executor max workers 限制），与 Inspector 并发预算（docs/OPERABILITY.md §1）一致，不引入额外并发上限。**已知限制**：exec 超时后，`asyncio.wait_for` 取消协程，但 `to_thread` 包裹的阻塞 `exec_run` 仍在后台线程跑到容器内进程结束——高频超时场景下超时残留线程会累积占用 executor worker，与「短时 IO」假设冲突。本提案**不**设 per-target 信号量兜底（design Open Questions 列为未来评估项）；docstring 会标注此影响，运维侧通过合理 timeout + Inspector 并发预算约束规避。
- **内存预算**：`read_file` 10MB 硬上限沿用 ExecResult 契约；exec stdout/stderr 不额外缓冲（docker-py 返回完整 bytes，由现有 Inspector 输出大小约束兜底）。
- **超时**：exec `timeout` 必填（秒），外层 `asyncio.wait_for` 实现；docker client 连接超时默认沿用 docker-py 默认（不新增配置字段，避免范围蔓延）。

## Security & Secrets

- **不引入新密钥**：本提案默认本机 unix socket，无远程凭据；`docker_host` 字段预留但远程 TLS 凭据加载是 follow-up（非目标）。
- **env 注入安全**：`env` 经 `exec_run(environment=...)` 注入，**禁止**拼进 cmd string（避免进容器 process list / shell history），与 SSHTarget §需求:env 注入只走参数 同源约束。
- **攻击面**：访问本机 docker socket 等于本机 root 等价权限——此风险在 `docs/operations/targets.md` 的 docker target 章节显式记述（「docker socket 访问 = 宿主 root 等价权限，确保 targets.yaml 不暴露给非授信用户」）。本提案**不**为此新增 doctor 检查需求（doctor `targets` 检查保持 type-agnostic 不变，见下「对外契约影响」），安全提示落在文档而非 doctor 运行时输出，避免引入无 spec 落点的契约。不扩大网络攻击面（本机 socket，无新监听端口）。
- **脱敏**：DockerTarget 不持有 secret 字段，错误信息无凭据；复用既有 `scrub_exception_message` 包装 transport 异常。

## Cost / Quota Impact

- **零 LLM 成本**：DockerTarget 是纯执行层，不调用 Anthropic API，对 token 消耗 / 配额无任何影响。Agent loop / prompt caching 路径完全不变。

## Demo Path

5 分钟本地复现（需本机 docker daemon）：

```bash
pip install -e ".[dev,docker]"
docker run -d --name hostlens-demo alpine sleep 3600   # 起一个待巡检容器
cat >> ~/.config/hostlens/targets.yaml <<'YAML'
  - name: demo-docker
    type: docker
    container: hostlens-demo
YAML
hostlens target test demo-docker          # 看到 echo probe 成功（容器内 echo）+ 探测到的 capabilities
hostlens doctor --json                    # checks.targets 里 demo-docker connectivity: ok、type: docker
```

> **注**：`hostlens inspect --inspector <linux inspector>` 跑在 docker target 上目前会报 `requires_unmet/target_type`（inspector manifest `targets` 值域尚不含 `docker`，见 Non-Goals）——本提案交付的 DockerTarget 执行层经 `target test`（真在容器内 exec）+ `doctor`（连通性）端到端验证；「inspector 巡检容器」是显式 follow-up。

无 docker daemon 的 CI / 评审者：集成测试自动 skip（标 `@pytest.mark.docker_integration`）；单元层用 `tests/targets/test_docker_config.py` 验证 DockerEntry 解析 + registry 分支（不需 daemon）。
