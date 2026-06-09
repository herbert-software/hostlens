## 上下文

M1 已落地 `ExecutionTarget` Protocol + `LocalTarget` / `SSHTarget` 两个实现，并把扩展点固化在 spec 里：`base.py` 的 `Capability` enum 含 `DOCKER_CLI`、`type` 的 `Literal` 含 `"docker"`，`docs/ARCHITECTURE.md §5` 把 Docker 列为四种 target 之一。换言之，加 DockerTarget 的「契约面」早被前序里程碑预留好——本提案是「填实现」而非「改契约」，这正是 ExecutionTarget 抽象设计成功的验证。

约束：

- 技术栈锁定 docker-py（CLAUDE.md §2），但 docker-py 是**同步阻塞 SDK**，与项目 async-first 红线冲突——必须用 `asyncio.to_thread` 桥接。
- DockerTarget 必须结构化满足既有 `ExecutionTarget` Protocol，不改 Protocol、不改 `Capability` enum、不改 `CAPABILITY_ALLOWLIST`（`DOCKER_CLI` 已在其中）、不改 doctor/target-test（已 type-agnostic）。唯一的 spec 级契约变更是 `TargetsConfig` discriminated union 接受 `type: docker`。
- 安全红线：env 经参数注入（不拼 cmd string）、只读（无生命周期操作）、10MB read 上限、错误只在 transport 边界 raise。

利益相关者：Inspector 作者（透明复用现有 inspector 到容器）、Agent loop（对 target 类型无感知）、运维（`targets.yaml` 新增 `type: docker`）。

## 目标 / 非目标

**目标：**

- `DockerTarget`：`ExecutionTarget` 第三个并发实现，对已存在 running 容器做只读 `exec` / `read_file`。
- `DockerEntry` 配置条目接入 `TargetEntry` discriminated union。
- `registry.build_registry_from_config` 加 docker 分支。
- docker-py 作为 optional-dep（`hostlens[docker]`），核心包不强依赖。
- 真实 docker 容器集成测试 + 无 daemon 时 skip；配置层单元测试无需 daemon。

**非目标：**

- KubernetesTarget（`add-kubernetes-target` 独立提案）。
- 容器生命周期管理 / Swarm / compose 拓扑。
- 写类 capability（M9 Remediation）。
- 远程 docker over TCP+TLS 凭据体系（`docker_host` 字段预留，加载逻辑 follow-up）。

## 决策

### D1：docker-py + `asyncio.to_thread`，不用 aiodocker

**选 docker-py（同步）+ `asyncio.to_thread` 包裹每个阻塞调用**，不引入 `aiodocker`。

- 理由：CLAUDE.md §2 明确锁定 docker-py；`aiodocker` 是另一个依赖且维护活跃度低于官方 docker-py。SSHTarget 用原生 async 的 asyncssh，但 docker 生态官方 SDK 只有同步版——`to_thread` 是项目既有的「同步 CPU/IO 工作」标准桥（CLAUDE.md §2「同步 CPU 工作用 `asyncio.to_thread`」）。
- 代价：每次 docker 调用占一个默认线程池 worker。可接受——Inspector 并发本就受控（OPERABILITY §1），docker 调用是短时 IO。
- 替代方案：(a) `aiodocker`——多一个低活跃依赖，违反「是否值得多一个依赖」；(b) 直接 subprocess 调 `docker` CLI——丢失结构化错误类型（NotFound / APIError），错误分类退化成解析 stderr，脆弱。

### D2：超时用外层 `asyncio.wait_for`，不依赖 docker-py 原生 timeout

docker-py `container.exec_run` **无** per-exec timeout 参数（client 级 timeout 是连接超时，不是命令执行超时）。因此 `timed_out` 语义由 `asyncio.wait_for(asyncio.to_thread(exec_run...), timeout)` 在 hostlens 侧实现。

- 超时到期：`wait_for` 抛 `TimeoutError` → 返回 `ExecResult(timed_out=True, exit_code=None)`，满足 ExecResult 不变量。
- 残留进程：被 wait_for 取消的是 to_thread 包裹的阻塞调用，docker exec 进程仍在容器内运行——hostlens **不**做进程组 kill（docker exec 进程不在 hostlens 进程组），残留由 docker daemon / 容器负责，与 SSHTarget「远端进程清理由 sshd 负责」同语义。这是 SSH/Docker 这类「远程执行」target 的共性，已在 ssh-execution-target spec 明确，DockerTarget 沿用。
- 替代方案：在容器内包 `timeout <n> <cmd>`——依赖容器内有 `timeout` 命令（alpine busybox 有但不普适），且把超时责任下放到被测环境，不可靠。

### D3：`exec` 用 `["/bin/sh", "-c", cmd]`，env 走 `environment=`

为与 LocalTarget/SSHTarget 的 shell 语义一致（管道 / 重定向 / `$VAR` 展开），`exec_run` 的 cmd 必须是 `["/bin/sh", "-c", cmd]` 而非裸 token。env 经 `exec_run(environment=env)` 注入容器进程环境。

- 理由：Inspector 的 collect 脚本是 shell 片段，依赖 shell 求值；env 拼进 cmd string 会让 secret 进容器 process list / shell history（§4.3 红线）。
- 注意：与 SSHTarget 不同，容器内 `exec_run(environment=...)` **不**受 sshd `AcceptEnv` 过滤限制——env 直达进程环境，所以 docker 场景 env 注入比 SSH 更直接可靠（这是 DockerTarget 的一个优势，文档应点出）。
- demux=True：分离 stdout/stderr，对齐 ExecResult 的 stdout/stderr 分字段。

### D4：read_file 用 `get_archive`（tar stream），不用 `exec_run("cat")`

`read_file` 走 `container.get_archive(path)` 拿 tar 流解出单文件，与 SSHTarget 禁止 cat-fallback 的理由同源：

- 安全：`exec_run("cat " + path)` 有命令注入风险（`path="x; rm -rf /"`）；二进制经 shell stdout 可能编码变换破坏字节完整性。
- 大文件中断：边界 `> 10MB`（恰好 10MB 放行）。无条件 backstop = 边读 tar 边累计、>10MB 立即中止；`get_archive` 返回的 stat header 若含可信 `size` 则额外允许解 tar 前提前 raise（优化，非唯一防线——stat 缺 size 时累计 backstop 兜底）。
- 代价：`get_archive` 即使读单文件也返回 tar 封装，需解 tar 取第一个 regular file 条目。目录路径会得到多条目 → raise `not_a_file`。

### D5：docker-py 作为 optional-dep + 模块可 import

镜像 `[mcp]` extra 模式：`pip install "hostlens[docker]"`。关键约束——`hostlens.targets.docker` 模块顶层 import 必须容错（`try: import docker except ImportError: docker = None` 置标志），让**未装 docker 的环境仍能 import 该模块**（mypy 全量检查、registry 分支注册都需要能 import），只在**实际构造/使用** DockerTarget 时才 raise `TargetError(kind="docker_sdk_unavailable")` 附安装提示。

- 理由：registry.py 的 `build_registry_from_config` 无条件 import docker.py 模块（它要引用 `DockerTarget` 类做分支）；若顶层硬 import docker 包，没装 `[docker]` 的核心安装会在 import registry 时就崩——破坏「核心包不强依赖」。
- 替代方案：registry 里延迟 import docker.py 模块——可行但把 optional 处理散到 registry，不如集中在 docker.py 模块内一处容错清晰。

### D6：`TargetsConfig` discriminator 只加 `docker`，不动 replay/k8s

`execution-target` spec 的 `TargetsConfig` 需求里 discriminator literal 当前是 `["local", "ssh"]`。本提案 MODIFY 为 `["local", "ssh", "docker"]`——只加 docker，不碰 replay（replay 的 config 路径在 incident-pack / replay-execution-target spec 自治，本提案不声明它，避免跨提案耦合）、不预留 k8s（k8s 留给 `add-kubernetes-target`，禁止预留 placeholder——与 spec「禁止预留未来 milestone token」一致）。

## 风险 / 权衡

- **[CI 无 docker daemon 导致集成测试覆盖缺失]** → 集成测试标 `@pytest.mark.docker_integration`，session fixture 探测 daemon 不可达即 skip；配置/registry 层单元测试无需 daemon 保证核心逻辑在 CI 必跑。本地 Demo Path + 集成测试在有 daemon 的开发机 / 自托管 runner 兜底正确性。与 D-7 os-shell fixture 教训一致：offline 不验证的部分靠真机集成测试 + Demo 锁。
- **[`to_thread` 线程池耗尽]** → docker 调用是短时 IO，Inspector 并发受 OPERABILITY §1 预算约束；不引入额外并发上限，复用 asyncio 默认 executor。若未来高并发场景暴露问题，再评估 per-target 信号量（非本提案范围）。
- **[docker socket = 宿主 root 等价权限]** → 不是代码缺陷而是 docker 模型固有风险。缓解：在 `docs/operations/targets.md` 显式记述该风险（「docker socket 访问等价宿主 root，确保 targets.yaml 不暴露给非授信用户」）；本提案默认本机 socket，不扩大网络攻击面。**不**为此扩 doctor 检查（doctor `targets` 保持 type-agnostic 不变），避免引入无 spec 落点的运行时契约——安全提示落文档。
- **[get_archive 解 tar 的边界情况]**（符号链接 / 特殊文件 / 大目录）→ read_file 契约限定单 regular file；目录/多条目/非 regular file（含符号链接）逐条迭代时即 raise `not_a_file`（not_a_file 判定优先于 size）；size 用无条件累计 backstop（stat header 仅作可选优化）。
- **[docker-py 异常类型跨版本漂移]** → pin `docker>=7`（API 稳定的大版本）；错误分类捕获 `docker.errors.NotFound` / `docker.errors.APIError` / `docker.errors.DockerException` 三个稳定基类，不依赖细分子类。

## Migration Plan

- 纯新增，无数据迁移。现有 `targets.yaml`（仅 local/ssh）完全兼容——discriminator 扩展是向后兼容的（新增可选 type，旧 type 行为不变）。
- 部署：用户 `pip install "hostlens[docker]"` 后才能用 docker target；未装时构造 docker target 给清晰的 `docker_sdk_unavailable` 错误 + 安装提示。
- 回滚：移除 DockerTarget / DockerEntry / registry 分支即可，无持久化状态依赖。

## Open Questions

- `docker_host` 远程端点（TCP / TLS）的凭据加载——本提案预留字段但不实现远程凭据，留 follow-up。是否合并进 `add-kubernetes-target` 还是独立提案，到时再定。
- ~~容器内 shell 不是 `/bin/sh`（distroless）~~——**已决议**：本提案假设容器有 `/bin/sh`，distroless 是非目标；无 `/bin/sh` 时 `exec_run` 的 OCI 错误被归类为 `exec_failed`（不误归 `docker_unavailable`），见 docker-execution-target spec §需求:故障分类。未来若要支持 distroless 巡检（exec_run 裸命令模式）再单独提案。
