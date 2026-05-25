## 为什么

M1 第一块基石。Hostlens 所有巡检最终都要"在某个地方执行命令并读文件"——本地子进程 / 远程 SSH / Docker / K8s pod 都不一样。如果 Inspector 直接 import `asyncio.create_subprocess_shell` 或 `asyncssh`，未来 M8 加 Docker / K8s 时 Inspector 要全部改一遍；而且 Tool Registry 已落地的 `ToolContext.target_registry`（M2 首批 ToolSpec `list_targets` / `run_inspector` 都要它）目前是 stub Protocol，再不落地 M2 就无法 dispatch。

CLAUDE.md §4.3 与 docs/ARCHITECTURE.md §5 已经把 Protocol 形状钉死（`name` / `type` / `exec(cmd, *, timeout, env)` / `read_file(path)` / `capabilities`）。这次提案的任务是把契约从架构文档搬进 spec 与 `src/hostlens/targets/`，并交付 **LocalTarget + SSHTarget** 两个 M1 必需实现 + 一个 `TargetRegistry` + 一组 `hostlens target` CLI 命令，让"非 root 用户 → 写 yaml 配 SSH 主机 → 跑命令拿结果"端到端可跑。

不在本提案范围：DockerTarget / KubernetesTarget（M8 单独提案）、Inspector 调度逻辑（下一个提案 add-inspector-plugin-system）、target 凭据的高级管理（macOS Keychain / SOPS 留到 M5+ 路线）。

## 变更内容

**新增（execution-target 核心 Protocol 与基础类型）：**

- `hostlens.targets.base.ExecutionTarget` Protocol：`name` / `type` / `async exec(cmd, *, timeout, env)` / `async read_file(path)` / `capabilities` 属性
- `hostlens.targets.base.Capability` Enum：M1 最小集 = `{SHELL, FILE_READ, SSH, SYSTEMD, DOCKER_CLI}`（5 个；与 docs/ARCHITECTURE.md §5 锁定一致）。M8 DockerTarget / K8sTarget 提案再扩 `K8S_EXEC` 等；M9 Remediation 再扩 `FILE_WRITE` 等。这套值与 `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST` 在 M1 落地后通过 `frozenset({c.value for c in Capability})` 严格相等，详见 §修订
- `hostlens.targets.base.ExecResult` Pydantic 模型：`exit_code: int | None` / `stdout` / `stderr` / `duration_seconds` / `timed_out`（超时时 `exit_code=None` 且 `timed_out=True`；`None` 表示"无 OS-level exit code"，与"非零退出"语义完全分离；避免 Linux 上 `-1` 与 SIGHUP/signal 终止 exit code 冲突）
- `hostlens.targets.local.LocalTarget`：`asyncio.create_subprocess_shell(cmd, env=..., start_new_session=True)` 实现；`type="local"`；capabilities = `{SHELL, FILE_READ}` + 运行时探测；**超时时必须用 `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` 杀整个进程组**（不只是 `proc.kill()`），保证 shell fork 的子进程（如 `sleep`）也被回收
- `hostlens.targets.registry.TargetRegistry`：按 name 索引 target 实例；API 为 `register(target, entry)` / `get(name)` / `get_entry(name)` / `names()` / `list()` / `list_entries()`；**同时持有 ExecutionTarget 实例与对应 `TargetEntry` 元数据双索引**（让 `list_targets` handler 能拿到 `display_name`/`description`/`tags`/`enabled` 等 ExecutionTarget Protocol 上不暴露的字段）；name 冲突 raise `TargetError(kind="duplicate_target", target=name)`
- `hostlens.targets.config.TargetsConfig` Pydantic 模型 + loader：从 `~/.config/hostlens/targets.yaml` 加载 target 配置（M1 LocalTarget 单条配置即可；SSHTarget 见下）

**新增（ssh-execution-target 实现 + 凭据约束）：**

- `hostlens.targets.ssh.SSHTarget`：基于 `asyncssh` 的实现；`type="ssh"`；**初始** capabilities = `{SSH, SHELL, FILE_READ}`（构造时即有）；`SYSTEMD` / `DOCKER_CLI` 在首次 `exec` 后通过 `which systemctl` / `which docker` 探测命中时加入并缓存（与 ssh-execution-target spec 一致）
- **SSH connection pool（必须）**：每个 `SSHTarget` 实例维护**一个** asyncssh control connection（per-process per-target，类似 OpenSSH `ControlMaster auto`）；每次 `exec` 在该连接上**新建 channel**（`conn.create_session` / `conn.run`），不重新 SSH；空闲超过 `ssh.idle_timeout_seconds`（默认 300s）才关闭；连接断开 / EOF 时**1 次自动重连**（指数退避 1s → 4s → 16s），仍失败 raise `TargetError(kind="ssh_connection_lost", target=name)`。**严格对齐 docs/OPERABILITY.md §2.2 硬约束**——「连接中断 → 1 次自动重连（指数退避 1s→4s→16s）」+「不允许『每个 Inspector 重新 SSH 一次』」
- 凭据加载：**仅支持 key 认证为主**（M1 也支持 password 但 doctor 必须 warn）；`password` / `passphrase` 字段通过 `${ENV_VAR}` 占位从环境读（明文落 yaml 触发 doctor warning）；`key_path` 字段是普通文件路径（路径本身非 secret，文件内容由 asyncssh 加载）
- SSH env 传递的限制：远端 sshd 默认 `AcceptEnv` 仅允许 `LANG LC_*`；Hostlens 不假装能传任意 env，docs 与 doctor 必须明确说明——**禁止**通过 `export VAR=value; cmd` 拼到 cmd string 的方式传 secret（会进 process list / shell history，与 docs/ARCHITECTURE.md §4 secret 边界冲突）；推荐方式：(a) 用户在远端 sshd 配 `AcceptEnv HOSTLENS_*`；(b) Inspector 通过 stdin 传 secret
- 集成测试：CI 起 `linuxserver/openssh-server` 容器跑真实 sshd（**不**mock `asyncssh`，按 CLAUDE.md §6 测试规则）；测试容器必须自带 `AcceptEnv HOSTLENS_TEST_*` 配置；env 注入测试**只**断言带 `HOSTLENS_TEST_` 前缀的 var 能透传（不假装任意 env 都能跑）

**新增（CLI 命令集）：**

- `hostlens target add <name> --type local|ssh [--host ... --user ... --port 22 --key-path PATH --password-env VAR --passphrase-env VAR]`：写 yaml + 校验。**写操作命令 (`add` / `remove`) 在 EUID==0 时直接拒绝并 exit 1**（CLAUDE.md §4.5 + 全局 §"写操作必须拒绝 root"）
- `hostlens target list [--json]`：列出已配置 target + 是否启用 + capability 集合（只读，允许 root）
- `hostlens target remove <name>`：从 yaml 删除（默认交互确认，`--yes` 跳过；非交互无 `--yes` exit 1）；EUID==0 拒绝并 exit 1
- `hostlens target test <name>`：跑一次 `echo hostlens-probe-$$` 验证连通性 + capability 探测（只读，允许 root）
- `hostlens doctor` 增加 `targets` section：每个 target 连通性 + 凭据来源 + 明文密码警告

**修订（Tool Registry stub Protocol 落地）：**

- `hostlens.tools.base.ToolContext.target_registry` 字段从 stub Protocol 切到真实 `TargetRegistry` 类型；删除 `hostlens.tools.base.TargetRegistry` stub Protocol 定义（含其 `list_summaries()` 方法签名）
- `register_default_tools` 注入的 `list_targets` handler 从原 `ctx.target_registry.list_summaries()` 迁移到 `ctx.target_registry.list()`（返回 `list[ExecutionTarget]`），并在 handler 内做 `ExecutionTarget → TargetSummary` 投影（应用脱敏 + allowlist 过滤）
- `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST` 更新为 `frozenset({c.value for c in Capability})`（M1 Capability Enum 是 SOT，allowlist 派生）；任何依赖原 allowlist 具体值（`{shell, file_read, file_write, docker, k8s_exec}`）的测试同 PR 更新

## 功能 (Capabilities)

### 新增功能

- `execution-target`: `ExecutionTarget` Protocol、`Capability` enum、`ExecResult` 模型、`TargetRegistry`、`LocalTarget` 实现、`hostlens target` CLI 命令集与 `targets.yaml` 配置加载
- `ssh-execution-target`: `SSHTarget` 实现、凭据从环境变量加载的约束、env 注入限制说明、集成测试用真实 sshd 容器

### 修改功能

- `tool-registry-capability-layer`: `ToolContext.target_registry` 字段类型从 stub Protocol 切到真实 `TargetRegistry`；`list_targets` 与 `run_inspector` ToolSpec 的 handler 现在能拿到真实 target 数据（M2 提案中标注的"stub 占位"被替换）

## 影响

**代码：**

- 新增 `src/hostlens/targets/{__init__.py, base.py, local.py, ssh.py, registry.py, config.py}`
- 新增 `src/hostlens/cli/target.py`（Typer 子命令组）；注册到 `cli/__init__.py`
- 修改 `src/hostlens/cli/doctor.py`：增加 targets 健康检查 section
- 修改 `src/hostlens/tools/base.py`：把 `TargetRegistry` 从 stub Protocol 切到真实类型 import；M2 落地的 stub 删除
- 修改 `src/hostlens/tools/default_tools.py`：`list_targets` handler 接通真实 registry 数据
- 新增测试：`tests/targets/test_local.py`、`tests/targets/test_ssh_integration.py`（docker-based）、`tests/cli/test_target.py`、`tests/tools/test_list_targets_with_real_registry.py`

**依赖（PEP 508 语法，与现有 pyproject.toml `>=` 风格一致；**禁止**用 Poetry caret `^`）：**

- 新增 runtime 依赖：`asyncssh>=2.18,<3`
- 新增 dev 依赖：`pytest-docker>=3.1,<4`（用于 sshd 容器集成测试 fixture）+ `pytest-rerunfailures>=14.0,<16`（CI 集成测试 retry）

**配置文件：**

- 新增 `~/.config/hostlens/targets.yaml` 约定路径；M0 已落地的 `Settings` 增加 `targets_config_path` 字段（默认 `~/.config/hostlens/targets.yaml`）

**文档：**

- 更新 `docs/ARCHITECTURE.md` §5：把"M1 落地"标注从"待办"改为本提案 PR 编号
- 新增 `docs/operations/targets.md`：targets.yaml 配置示例 + SSH 凭据 best practice + 远端 sshd `AcceptEnv` 限制说明
- README "快速开始"小节增加 `hostlens target add` 示例

**对外契约影响：**

- **CLI 命令**：新增 `hostlens target` 子命令组（add / list / remove / test）—— 这是 M0 之后第一次扩展 CLI 表面
- **Inspector schema**（未来）：M1 下一提案 `add-inspector-plugin-system` 的 `targets:` 字段值域 = 本提案落地的 target `type` 枚举（`local` / `ssh`）
- **Agent tool schema**：`list_targets` ToolSpec 输出从 stub 切到真实数据；`TargetSummary` schema 不变（已在 tool-registry-capability-layer spec 锁定）
- **MCP tool schema**：M7 才暴露，本提案不影响
- **Notifier Protocol / Schedule manifest**：不影响

## 非目标（Non-Goals）

明确**不在**本提案范围，防止范围蔓延：

- ❌ DockerTarget / KubernetesTarget 实现（M8 单独提案）
- ❌ macOS Keychain / Linux Secret Service / SOPS 加密密钥（M5+ 路线，本提案仅支持环境变量占位）
- ❌ Bastion / Jump Host / Agent forwarding：M1 直连，bastion 推到有用户需求时
- ❌ SSH password 加密存储：M1 password **推荐**走 `${ENV_VAR}` 占位；**允许**字面明文落 yaml（loader 接受，仅 doctor warn，与决策 5 一致；M2+ 才考虑升级为加载时 error）
- ❌ Inspector 调度逻辑：下一提案 `add-inspector-plugin-system` 处理
- ❌ Capability 自动发现的完备性：M1 只做 SSH/LOCAL 基础检测；SYSTEMD / DOCKER_CLI 探测靠运行时跑 `which systemctl` / `which docker`（POSIX 标准、轻量；与 execution-target spec 一致），false negative 可接受
- ❌ 写操作 target API：M1 `exec` 只用于读类命令（与 M2 ToolRegistry `side_effects ∈ {none, read}` 一致）；M9 Remediation 才扩展写语义
- ❌ Target health 持续监控 / 自动 disable 失败 target：M1 失败由调用方处理，不做后台守护
- ❌ **不**做：SSH 跨进程连接共享（OpenSSH `ControlMaster` 用 unix socket 共享给其它进程）—— 本提案的 per-target connection pool 是**进程内**复用，跨进程不共享

## Failure Modes

| 故障 | 行为 | 用户可见状态 |
|---|---|---|
| LocalTarget exec 超时 | `asyncio.wait_for` 取消 + `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` 杀整个进程组（`start_new_session=True` 保证 shell fork 的子进程也被回收）；返回 `ExecResult(timed_out=True, exit_code=None)` | finding-level: `inspector_status: timeout`（M1 下一提案体现）；本提案 ExecResult 用 `exit_code=None + timed_out=True` 表达，**禁止**用 `-1` 魔数（与 Linux signal-killed exit code 冲突） |
| SSHTarget `ConnectionLost` / `ChannelOpenError`（control connection 已建立后断开 / channel open 失败） | 1 次自动重连（指数退避 1s → 4s → 16s，对齐 OPERABILITY §2.2），仍失败则 raise `TargetError(kind="ssh_connection_lost", target=name)`；按 OPERABILITY §2.2 + §9 下游传导：下游 Inspector status 变为 `target_unreachable`。完整传导规则推迟到下一份 inspector proposal（add-inspector-plugin-system）落地；本提案的 ExecutionTarget 仅负责抛出异常，标记 status 由调用方做 | exit 1 + run status `partial`（M4 Scheduler）+ inspector status `target_unreachable`（M1 下一提案）|
| SSHTarget 主机不可达（DNS / 拒绝 / 防火墙） | 单 target 标 `target_unreachable`；其它 target 继续；error log 含目标 host + 错误码（**不含**凭据） | run status: `partial`（M4 Scheduler RunStatus 兜底） |
| SSHTarget 凭据错误（key permission denied / wrong password） | raise `TargetError(kind="ssh_auth_failed", target=name)`；error message 必须按"三层脱敏"顺序清洗（**(1)** 用 `self._entry.password` / `passphrase` 等已知 secret 做 `str.replace(secret, "***")` 精确替换 → **(2)** `scrub_exception_message`（agent-tool-adapter spec 已定义的 5 类正则）→ **(3)** bare credential keyword scrub `(?i)(password\|passwd\|...)\s+\S+`）；**禁止**单独依赖 `redact_sensitive`（只按 key 名脱敏 mapping，无法清洗 string 子串）；CLI 显示 `hostlens target test <name>` 建议 | exit 1；doctor 也能预先 detect |
| `targets.yaml` 引用的 `${ENV_VAR}` 未设置 | 加载时 raise `ConfigError(kind="missing_env_var", var_name=var_name, target=target_name)`（M1 落地需扩展 ConfigError 加 `kind` 与结构化 keyword 字段，详见 execution-target spec §需求:`ConfigError` 必须扩展支持结构化 kind/extra 字段）；**禁止**默默用空 string 当 password | hostlens doctor exit 1，CLI 命令启动 fail-fast |
| SSH env 注入但远端 sshd 拒收（`AcceptEnv` 限制） | env 被远端静默丢弃；本地无法检测；docs 必须说明；M1 不在 runtime 验证（成本太高）；推荐用户配 `AcceptEnv HOSTLENS_*` 前缀 | docs 警告 |
| LocalTarget read_file 路径不存在 | raise `FileNotFoundError`；上层（Inspector）按 `requires_files` 自动 skip | finding-level: `requires_unmet` |
| SSHTarget read_file SFTP 不可用 | raise `TargetError(kind="sftp_unavailable", target=name)`；**禁止** fallback 到 `cat` shell 命令（含命令注入风险且无法保证字节完整性） | exit 1；用户需在远端启用 sftp-server subsystem |

## Operational Limits

参考 docs/OPERABILITY.md §1：

- **单 Inspector exec 默认超时**：60s（`concurrency.inspector_timeout_seconds`）；本提案的 `ExecutionTarget.exec(timeout=...)` 接受调用方传入值，**不**在 target 层加默认
- **单 target 并行 exec 数**：4（`concurrency.max_concurrent_inspectors_per_target`）—— 本提案不实现 semaphore（Inspector Runner 层负责，下一提案）；SSHTarget 在**同一 control connection 上并行开 channel**（asyncssh 原生支持），不会因并行竞争一个 stdin/stdout
- **同时巡检 target 数**：8（`concurrency.max_concurrent_targets`）—— 同上，本提案不实现
- **TargetRegistry 实例数**：进程内单例，由 Settings 注入时构造
- **SSH 连接建立超时**：10s（asyncssh `connect_timeout`），可在 targets.yaml per-target 配
- **SSH idle timeout**：300s（`ssh.idle_timeout_seconds`），control connection 空闲超过后自动 close + 下次 exec 时按需重连；对齐 docs/OPERABILITY.md §2.1
- **SSH 自动重连退避**：1s → 4s → 16s（指数）共 1 次重连尝试（对齐 OPERABILITY §2.2；超出 raise `TargetError(kind="ssh_connection_lost", target=name)`）
- **read_file 大小上限**：M1 默认 10 MB，超出 raise `TargetError(kind="file_too_large", target=name, path=path, size=size)`，防止 SSH 一次拉巨大日志 OOM

## Security & Secrets

参考 docs/OPERABILITY.md §7：

- **密钥来源**：仅环境变量（`${ENV_VAR}` 占位）；macOS Keychain / SOPS 加密留到 M5+
- **明文密码警告**：`targets.yaml` 中 `password` 字段不通过 `${...}` 占位（即字面量） → loader 接受但 doctor 标 warning；future M2+ 可升级为加载时 error
- **凭据脱敏（三层）**：
  1. **按 key 名脱敏**（dict 层）：`core/logging.redact_sensitive`（已落地）—— 处理 structured log 的 `password=...` 字段
  2. **按已知 secret 精确替换**（string 层 layer 1）：SSHTarget 用 `self._entry.password` / `passphrase` 等**自己持有的已知 secret 值**在异常字符串上 `str.replace(secret, "***")` —— 这层保证 caller 配的 secret 一定被脱敏，**不依赖正则覆盖范围**
  3. **按未知敏感子串脱敏**（string 层 layer 2 + 3）：先跑 `agent-tool-adapter` spec 已定义的 `scrub_exception_message`（5 类正则：path / IPv4 / IPv6 / 凭据键值对 / email-at-host）；再跑 bare credential keyword scrub `(?i)(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth)\s+\S+` 覆盖 "with password X" / "auth token Y" 这种 key-value 空格形式（layer 2 漏的）；`TargetError.__str__` 与 SSH 错误的 structlog log 都必须按 (2) → (3) 顺序跑这两层
- **攻击面**：新增 SSH client 能力 = 给 hostlens 进程加了对外发起 SSH 连接的能力；不引入入站监听端口；M9 Remediation 才会有"通过 SSH 改远端状态"的能力（届时再做 RBAC）
- **EUID == 0 拒绝（CLI 写操作）**：`target add` / `target remove` 写 `~/.config/hostlens/targets.yaml`（含凭据引用），按 CLAUDE.md §4.5 + 全局"写操作必须拒绝 root"硬约束，EUID==0 时直接 exit 1，输出修复建议（"请用普通用户运行；如必须以 root 部署 daemon，先在普通用户下创建配置文件再 chown"）；`list` / `test` / `doctor` 是只读，允许 root

## Cost / Quota Impact

参考 docs/OPERABILITY.md §3：

- **零 LLM 调用**：本提案纯基础设施，不调 Anthropic API
- **零 token 消耗**：CI 集成测试不需要 cassette（不调 LLM）
- **Anthropic 配额**：不影响（仅基础设施）
- **未来影响**：M2 Agent loop 通过 `run_inspector` ToolSpec 调本提案接口；每次 inspector run 的 LLM token = M2 提案预算

## Demo Path

交付后 5 分钟本地 reproduce（**无 SSH 真实服务器、无付费 API、用普通非 root 用户**）：

1. `pip install -e ".[dev]"`
2. 启 sshd 容器（自带 `AcceptEnv HOSTLENS_TEST_*` 配置）：`docker run -d -p 2222:2222 -e USER_NAME=hostlens -e PASSWORD_ACCESS=true -e USER_PASSWORD=demo -e DOCKER_MODS=linuxserver/mods:openssh-server-acceptenv linuxserver/openssh-server`（若 mod 不可用，则手工 `docker exec` 加 `echo 'AcceptEnv HOSTLENS_TEST_*' >> /etc/ssh/sshd_config && kill -HUP 1`）
3. 配 LocalTarget：`hostlens target add my-local --type local`
4. 配 SSHTarget：`export HOSTLENS_DEMO_SSH_PASSWORD=demo && hostlens target add my-ssh --type ssh --host localhost --port 2222 --user hostlens --password-env HOSTLENS_DEMO_SSH_PASSWORD`
5. 验证：`hostlens target list --json | jq` —— 看到两个 target 与各自 capabilities（capabilities 集合 = `{c.value for c in Capability}` 的子集）
6. 连通性：`hostlens target test my-local` 与 `hostlens target test my-ssh` 都返回 ok + 探测到的 capabilities
7. doctor：`hostlens doctor --json | jq .targets` —— 看到健康状态
8. **root 拒绝验证**：`sudo hostlens target add bad-from-root --type local` 必须 exit 1 + 输出修复建议（不创建配置）
9. **SSH 连接复用验证（进程内）**：每次 `hostlens target test` 是独立 Python 进程，pool 是**进程内**的，跨进程不共享 —— 因此 CLI 重复调用**不能**用来验证 pool 行为。改用单进程 Python REPL：
   ```python
   import asyncio
   from pathlib import Path
   from unittest.mock import patch
   import asyncssh
   from hostlens.targets.config import load_targets_config, build_registry_from_config
   cfg = load_targets_config(Path("~/.config/hostlens/targets.yaml").expanduser())
   from hostlens.core.config import Settings
   settings = Settings()
   reg = build_registry_from_config(cfg, settings)
   target = reg.get("my-ssh")
   with patch.object(asyncssh, "connect", wraps=asyncssh.connect) as m:
       async def go():
           for _ in range(3):
               await target.exec("echo hi", timeout=5)
       asyncio.run(go())
       print("asyncssh.connect call count:", m.call_count)  # 必须 == 1
   ```
   3 次 exec 必须只触发 1 次 `asyncssh.connect`（pool 复用 control connection）。
10. **list_targets ToolSpec 端到端验证（可选，需要 M2 已落地）**：在单进程 Python REPL 跑：
    ```python
    import asyncio
    from pathlib import Path
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.default_tools import register_default_tools
    from hostlens.tools.base import ToolContext, NoopApprovalService
    from hostlens.tools.schemas.list_targets import ListTargetsInput
    from hostlens.targets.config import load_targets_config, build_registry_from_config
    from hostlens.core.config import Settings
    import structlog, asyncio as _a

    settings = Settings()
    cfg = load_targets_config(Path("~/.config/hostlens/targets.yaml").expanduser())
    target_reg = build_registry_from_config(cfg, settings)
    tool_reg = ToolRegistry()
    register_default_tools(tool_reg)
    ctx = ToolContext(
        target_registry=target_reg,
        inspector_registry=...,                       # M1 下一提案落地前可用 stub
        config=settings,
        logger=structlog.get_logger(),
        approval_service=NoopApprovalService(),
        cancel=_a.Event(),
    )
    out = asyncio.run(tool_reg.dispatch("list_targets", ListTargetsInput(), ctx))
    print(out.model_dump_json(indent=2))
    ```
    验证 target_registry 注入链路通了；`capabilities` 字段命中 `CAPABILITY_ALLOWLIST` 派生自 `Capability` Enum 的新值；每个 TargetSummary 含 demo 步骤 3/4 的 `name` / `kind` / `enabled` 字段。**该步骤依赖 InspectorRegistry stub**（M1 下一提案才完成），demo runner 在 stub 不可用时跳过。

记录在 `examples/m1-targets/README.md`。
