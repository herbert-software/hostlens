# execution-target 规范

## 目的

定义 Hostlens 远程执行的统一抽象（M1 落地范围）：`ExecutionTarget` Protocol、`Capability` Enum、`ExecResult` / `TargetError` 数据模型、`LocalTarget` 实现、`TargetsConfig` / `TargetEntry` YAML 配置加载器与 `TargetRegistry` 索引/查询接口。本规范不含 SSH target 实现细节（见 `ssh-execution-target` 规范），也不含 docker / k8s target（留给 M8 / M9）。
## 需求
### 需求:`ExecutionTarget` Protocol 必须定义完整接口

`hostlens.targets.base.ExecutionTarget` 必须是 `typing.Protocol`，定义以下成员：

- `name: str`：target 实例的唯一标识；必须匹配正则 `^[a-z][a-z0-9_\-]{0,63}$`（用于 yaml key 与 CLI 引用）。**正则必须在以下三个入口同时 enforce**（任一缺失 = 允许非法 name 绕过）：
  1. `TargetsConfig` loader（`TargetEntry.name` 字段的 Pydantic `Field(pattern=...)`，见下方 `TargetsConfig` 需求）
  2. 具体 `ExecutionTarget` 实现的构造器（`LocalTarget.__init__` / `SSHTarget.__init__` 在赋值 `self.name` 前 `re.fullmatch(...)`，不匹配 raise `TargetError(kind="invalid_target_name", target=name)`）
  3. `TargetRegistry.register(target, entry)` 入口（在 register 前再校验 `target.name`，作为最后一道防线）
- `type: Literal["local", "ssh", "docker", "k8s"]`：与 docs/ARCHITECTURE.md §5 锁定的 4 种 target 类型一致；**禁止**自定义 type 名（如 `kubernetes` 必须用 `k8s`）
- `async def exec(self, cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> ExecResult`：异步执行 shell-evaluated 命令；`timeout` 单位秒，必填；`env` dict 通过实现侧的 subprocess `env=` 参数注入，**禁止**实现侧把 env 拼到 cmd string
- `async def read_file(self, path: str) -> bytes`：异步读远端文件；最大 10 MB（超出 raise `TargetError(kind="file_too_large", target=self.name, path=path, size=size)`）
- `capabilities: set[Capability]` 属性：返回该 target 当前支持的 Capability 集合（运行时探测结果）

Protocol 必须支持 mypy `--strict` 静态校验。

#### 场景:Protocol 形状完整

- **当** 检查 `ExecutionTarget` 的 `__annotations__` 与方法签名
- **那么** 必须**恰好**含 `name` / `type` / `capabilities` 属性 + `exec` / `read_file` 异步方法（不多不少）；`exec` 必须有 `cmd` 位置参数 + `timeout` 与 `env` keyword-only 参数

#### 场景:exec 是 async 方法

- **当** 检查 `inspect.iscoroutinefunction(SomeTarget.exec)`（任意实现类）
- **那么** 必须返回 `True`

#### 场景:type 字段值域受限

- **当** 实例化 `LocalTarget(name="x")` 并检查 `target.type`、实例化 `SSHTarget(name="y")` 并检查 `target.type`（SSH 连接信息从 `_entry: TargetEntry` 拿，由 `TargetRegistry.register` 注入；构造器保持 `__init__(name: str)` 单参数签名与 task 4.1a/4.1b 一致）
- **那么** `LocalTarget.type` 必须为 `"local"`、`SSHTarget.type` 必须为 `"ssh"`（**`type` 是类常量 / 只读属性，不是构造器参数**——构造器签名是 `__init__(name: str, ...)` 不接 `type` kwarg）；任何在子类里把 `type` 重写为 `"kubernetes"` / `"vm"` / 其他 Literal 之外字符串的实现必须在 mypy 阶段报错（Protocol 的 `Literal["local", "ssh", "docker", "k8s"]` 注解 enforce）

#### 场景:read_file 文件超过 10MB raise

- **当** 调用 `await target.read_file("/var/log/huge.log")` 且文件 ≥10 MB
- **那么** 必须 raise `TargetError`，错误 kind 为 `"file_too_large"`，含 target name 与 path（**不含**文件内容）

### 需求:`Capability` Enum 必须含 M1 最小集且与 ToolRegistry allowlist 严格相等

`hostlens.targets.base.Capability` 必须是 `enum.Enum`，M1 阶段**恰好**含以下 5 个成员（不多不少）：

- `SHELL = "shell"`：能跑 shell 命令（所有 M1 target 都有）
- `FILE_READ = "file_read"`：能读文件（所有 M1 target 都有）
- `SSH = "ssh"`：通过 SSH 协议访问（仅 SSHTarget）
- `SYSTEMD = "systemd"`：远端有 systemd（运行时探测）
- `DOCKER_CLI = "docker_cli"`：远端能跑 `docker` CLI（运行时探测）

未来扩展由对应里程碑提案负责（**禁止**预留尚未落地里程碑才用的 placeholder）。**M9 受控修复经评审决定 NOT 新增写类 Capability**——撤回早期「M9 加 `FILE_WRITE`」的承诺：写操作走既有 `SHELL`（`target.exec`）+ 审批/audit/rollback 的 shell 串，不引入受限写 API（受限写 API 必然逼出 `exec_raw` 逃生舱、反降低审批人警觉，详见 M9 架构不变量）。**`add-kubernetes-target`（M8 K8s 半边）经评审决定 NOT 新增 Capability**——KubernetesTarget 提供既有 `{SHELL, FILE_READ}` 并懒探测既有 `SYSTEMD`/`DOCKER_CLI`（与 DockerTarget 同模型），inspector 声明的是 target-agnostic 能力（如 `SHELL`）而非 `K8S_EXEC`，故**无 `K8S_EXEC` 成员**；早期文档/注释里「M8 加 K8S_EXEC」「M9 加 FILE_WRITE」的前向引用已 stale，由对应提案订正（含 `base.py` / `list_targets.py` / `test_capability.py` 注释）。

Enum 成员名必须**全大写**，值必须**全小写**（与 docs/ARCHITECTURE.md §5 一致）。**禁止**在加载 Inspector manifest 时接受 Enum 之外的 capability token —— 未知 capability 必须在 manifest 加载时 raise（防止 silent skip）。

#### 场景:Capability 恰好含 M1 最小集

- **当** 检查 `set(Capability.__members__.keys())`
- **那么** 必须**恰好**为 `{"SHELL", "FILE_READ", "SSH", "SYSTEMD", "DOCKER_CLI"}`（不多不少；防止偷偷预留未来 milestone 才用的 placeholder；`add-kubernetes-target` 不加成员，集合保持 5 个）

#### 场景:Capability 值是小写 string

- **当** 检查每个 `Capability` 成员的 `.value`
- **那么** 必须是该成员名的 lower case（如 `Capability.SSH.value == "ssh"`）

#### 场景:capabilities 与 `CAPABILITY_ALLOWLIST` 严格相等

- **当** 同时检查 `frozenset({c.value for c in Capability})` 与 `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST`
- **那么** 两者**必须严格相等**（M1 Capability Enum 是 SOT；M1 落地后 allowlist 必须更新为 `frozenset({c.value for c in Capability})`；原 M2 占位值 `file_write` / `docker` 必须删除——`file_write` **不**回填（M9 经评审不新增写类 capability）、`k8s_exec` 同样**不**回填（`add-kubernetes-target` 不引入该 capability））

### 需求:`ExecResult` 必须把 `timed_out` 与 `exit_code` 字段分离，超时时 `exit_code=None`

`hostlens.targets.base.ExecResult` 必须是 Pydantic v2 模型，含以下字段：

- `exit_code: int | None`：命令的 OS-level 返回码；**`None` 表示 "无 OS-level exit code"**（超时被 hostlens 主动取消 / 远端连接断开未拿到 exit code 等）；非 None 时是真实 wait status（含 signal-killed 时的 `128 + signum`）。**禁止**用 `-1` 魔数表达超时（与 Python subprocess 在某些平台返回的 signal exit code 冲突，语义不清）
- `stdout: str`：UTF-8 解码后的标准输出（非 UTF-8 字节用 `errors="replace"` 容错）
- `stderr: str`：同上
- `duration_seconds: float`：实际执行时长（含连接 + 等待）
- `timed_out: bool`：是否因 `timeout` 参数到期被取消；调用方判断超时**必须**用此字段，**不**用 `exit_code` 值判断
- **不变量**：`timed_out is True` 蕴含 `exit_code is None`（模型层 `model_validator` 强制；违反 raise `ValidationError`）；反之 `exit_code is None and not timed_out` 也允许（如远端断开未拿到 exit code）

`model_config = ConfigDict(frozen=True, extra="forbid")` 必须设置。

#### 场景:超时时 timed_out=True 且 exit_code=None

- **当** 调用 `await target.exec("sleep 100", timeout=1)`
- **那么** 返回 `ExecResult.timed_out is True`、`exit_code is None`、`duration_seconds >= 1.0`

#### 场景:正常返回非零 exit_code

- **当** 调用 `await target.exec("exit 42", timeout=10)`
- **那么** 返回 `ExecResult.timed_out is False`、`exit_code == 42`

#### 场景:signal-killed 命令返回 128+signum

- **当** 调用 `await target.exec("sh -c 'kill -SEGV $$'", timeout=10)`
- **那么** 返回 `ExecResult.timed_out is False`、`exit_code == 139`（128 + SIGSEGV=11）；**禁止**与超时的 `None` 混淆

#### 场景:模型层强制 timed_out 蕴含 exit_code=None

- **当** 试图构造 `ExecResult(timed_out=True, exit_code=0, ...)`
- **那么** 必须 raise `pydantic.ValidationError`（不变量违反）

#### 场景:stdout/stderr 非 UTF-8 字节不 raise

- **当** 命令输出含 `\xff\xfe` 等非 UTF-8 字节
- **那么** 必须不 raise；`stdout` 中对应位置必须是 Unicode replacement character `�`

#### 场景:ExecResult 实例不可变

- **当** 已构造的 `result` 试图赋值 `result.exit_code = 0`
- **那么** 必须 raise `pydantic.ValidationError`（frozen=True 生效）

### 需求:`LocalTarget` 必须基于 `asyncio.create_subprocess_shell` 实现且超时杀整个进程组（POSIX-only）

`hostlens.targets.local.LocalTarget` 必须：

- **POSIX-only**：M1 LocalTarget **只**支持 POSIX 宿主（Linux / macOS）；用的 `os.killpg` / `os.getpgid` / `start_new_session=True` 都是 POSIX 专有 API。Windows 宿主**禁止**在 import 时 silent fallback，必须在 `hostlens.targets.local` 模块 import 时检查 `sys.platform == "win32"` 并 raise `ImportError("LocalTarget requires POSIX host (Linux/macOS); Windows support is not in M1 scope")`，给清晰错误（不是运行时 cryptic 错误）
- `type == "local"`
- `capabilities` 至少含 `{SHELL, FILE_READ}`；运行时探测必须用 `which <bin>`（不是 `<bin> --version`，因为某些远端只有 binary 没有 PATH 中 alias；`which` 是 POSIX 标准且更轻）—— 如 `which docker` 成功则加 `DOCKER_CLI`；如 `which systemctl` 成功则加 `SYSTEMD`。**探测时机**：与 SSHTarget 一致采用 **lazy probe（首次 `exec` 时探测一次并缓存到实例属性 `_probed_caps`，后续 exec 复用）**——`__init__` 内**禁止**做 subprocess probe（subprocess IO 与项目 async-first 约定冲突；让构造函数保持纯类属性赋值；探测发生在 async exec 路径里，不需要 `asyncio.to_thread` 包装）；首次 `exec` 完成前 `capabilities` 仅含 `{SHELL, FILE_READ}`（不报错，但 list_targets / doctor 拿到的能力集会不完整，调用方应至少跑一次 `target test` 触发探测后再观察）
- `exec` 实现走 `asyncio.create_subprocess_shell(cmd, env=..., start_new_session=True)`，**禁止**走 `create_subprocess_exec`（M1 Inspector 命令含 pipe / redirect 必须 shell 解析）；`start_new_session=True` 是必需的——shell 会 fork 子进程（如 `sh -c 'sleep 60'` 实际进程树是 `sh → sleep`），只 SIGKILL 顶层 shell 不会回收 sleep
- 超时实现：`asyncio.wait_for` 包裹 `proc.communicate()` 抛 `TimeoutError` 时，**必须**调用 `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` 杀整个进程组，然后 `await proc.wait()` 确保 reaped；**禁止**只 `proc.kill()`（只杀顶层 shell，留下 zombie sleep）
- `env` 参数传入时**合并**到 `os.environ.copy()` 之上（不是替换），保留 PATH 等关键 env var

#### 场景:Windows 宿主 import 时 raise ImportError

- **当** 在 Windows（`sys.platform == "win32"`）python 进程里 `import hostlens.targets.local`
- **那么** 必须 raise `ImportError`，消息含 "LocalTarget requires POSIX host (Linux/macOS)"；**禁止**模块加载成功但 exec 时才 cryptic 失败

#### 场景:LocalTarget exec 走 shell 解析

- **当** 调用 `await local.exec("echo a | wc -c", timeout=5)`
- **那么** 必须返回 `exit_code=0`、`stdout` 含 `"2\n"`（pipe 被 shell 解析，不是被当作字面字符串）

#### 场景:LocalTarget 超时回收整个进程组无 zombie

- **当** 调用 `await local.exec("sleep 60", timeout=1)`，记录 subprocess 的 `proc.pid` 为 `parent_pid`
- **那么** 必须在 ~1s 后返回 `ExecResult(timed_out=True, exit_code=None)`
- **且** 必须用 **两层** 检查验证无残留进程：
  1. `psutil.pid_exists(parent_pid)` 必须为 `False`（subprocess 已 reaped）
  2. **全用户范围 sleep 扫描**：`[p for p in psutil.process_iter(['cmdline','username']) if p.info['username']==getpass.getuser() and p.info['cmdline'] and 'sleep' in p.info['cmdline'][0] and any('60' in arg for arg in p.info['cmdline'])]` 必须为空集
     —— 这层捕获 `start_new_session=True` 让子进程 reparent 到 PID 1 后**不**出现在 hostlens 后代树里的场景；如果 `os.killpg` 漏掉某进程，孤儿会被 init 收养但仍在 process table 里
- **且** subprocess 必须已被 `await proc.wait()` reaped（无 defunct/zombie）

#### 场景:LocalTarget env 合并而非替换

- **当** 调用 `await local.exec("echo $PATH:$MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** stdout 必须**同时**含原 `PATH` 内容与 `:x`（合并到 os.environ.copy 之上）

#### 场景:LocalTarget capabilities 运行时探测

- **当** 在装有 docker 的机器上构造 `LocalTarget("my-local")`
- **那么** `local.capabilities ⊇ {Capability.SHELL, Capability.FILE_READ, Capability.DOCKER_CLI}`
- **且** 在无 docker 的机器上构造 → `Capability.DOCKER_CLI ∉ local.capabilities`

### 需求:`TargetRegistry` 必须按 name 索引且同时持有 target 实例与配置元数据

`hostlens.targets.registry.TargetRegistry` 必须提供：

- `register(target: ExecutionTarget, entry: TargetEntry) -> None`：注册一个 target 实例**连同其源配置 `TargetEntry`**（含 `display_name` / `description` / `tags` / `enabled` 等 `ExecutionTarget` Protocol 上不存在的字段）；必须先校验 `target.name == entry.name`（否则 metadata 会与 target 错配 —— bind to 错误的 name 入口），不匹配 raise `TargetError(kind="target_entry_name_mismatch", target=target.name, entry_name=entry.name)`；name 冲突（与已注册的 name 撞）raise `TargetError(kind="duplicate_target", target=name)`；校验通过后**必须**执行 `target._entry = entry` 把 metadata 注入 target 实例（让 `exec` / `read_file` 能检查 `enabled`、读取 `connect_timeout` 等 per-target 字段）
- `get(name: str) -> ExecutionTarget`：未找到 raise `KeyError`
- `get_entry(name: str) -> TargetEntry`：返回配置元数据；未找到 raise `KeyError`
- `names() -> set[str]`：返回所有已注册 target 的 name 集合
- `list() -> list[ExecutionTarget]`：按 name 字典序返回（保证测试 / Tool Registry 投影可复现）
- `list_entries() -> list[TargetEntry]`：按 name 字典序返回所有 `TargetEntry`（供 `list_targets_handler` 投影 `TargetSummary` 时拿元数据使用）

Registry **不**持有连接状态 —— 它只是 (name → target 实例 + name → TargetEntry 元数据) 双索引；连接生命周期由各 target 实现内部管理。

**配套契约**：`ExecutionTarget` Protocol **不暴露** `display_name` / `description` / `tags` / `enabled` 字段（只有 `name` / `type` / `capabilities` / `exec` / `read_file`）。任何需要这些 metadata 的调用方（如 `list_targets_handler`）**必须**通过 `TargetRegistry.get_entry(name)` / `list_entries()` 从 `TargetEntry` 读取；具体行为契约由 `tool-registry-capability-layer` spec §场景:TargetSummary metadata 字段必须来自 TargetEntry 而不是 ExecutionTarget Protocol 规定（用"有意分歧"的 target/entry 对作为可测试断言，避免依赖"handler 源码不含 `getattr`"这种脆弱的实现细节检查）。

#### 场景:register 冲突 raise

- **当** registry 已含 `name="prod-web"` target，再次 `registry.register(another_target_named_prod_web, entry)`
- **那么** 必须 raise `TargetError`，错误 kind 为 `"duplicate_target"`，含 name；**不**覆盖原 target

#### 场景:list 按 name 字典序

- **当** 注册顺序为 `["zeta", "alpha", "beta"]`
- **那么** `registry.list()` 必须返回 `[alpha, beta, zeta]`（按 name 排序）

#### 场景:get 未找到 raise KeyError

- **当** `registry.get("not-exist")`
- **那么** 必须 raise `KeyError`（**不是** `TargetError` —— 这是 lookup miss 不是业务错误）

#### 场景:get_entry 与 list_entries 返回元数据

- **当** 注册 `target` + `entry=TargetEntry(name="prod-web", display_name="Prod Web", tags=["prod"], enabled=True, ...)`
- **那么** `registry.get_entry("prod-web")` 必须返回该 entry；`registry.list_entries()` 必须按 name 字典序返回 entries，能让调用方拿到 `display_name` / `description` / `tags` / `enabled`

#### 场景:register 拒绝非法 name target（绕过 loader 路径）

- **当** 测试代码直接构造 `LocalTarget(name="Prod-Web")` 或 `SSHTarget(name="1web")`（绕过 yaml loader，name 不匹配 `^[a-z][a-z0-9_\-]{0,63}$`）
- **那么** target 构造器**必须**在 `__init__` 中 raise `TargetError(kind="invalid_target_name", target=name)`
- **且** 假设构造器漏校验直接拿到 target 实例，调用 `registry.register(target, entry)` 也**必须** raise `TargetError(kind="invalid_target_name", target=target.name)`（registry 是最后一道防线）

#### 场景:register 拒绝 target.name 与 entry.name 不一致

- **当** `t = LocalTarget(name="a-good")`，`e = TargetEntry(name="another-name", type="local", ...)`，调用 `registry.register(t, e)`
- **那么** 必须 raise `TargetError(kind="target_entry_name_mismatch", target="a-good", entry_name="another-name")`（避免 metadata 与 target 错绑）；registry 状态不变（不部分注册）

### 需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开

`hostlens.targets.config.TargetsConfig` 必须是 Pydantic v2 模型：

- 顶层结构：`version: Literal["1"]` + `targets: list[TargetEntry]`
- `TargetEntry` 通用字段（**所有 type 共有**）：
  - `name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_\-]{0,63}$")]`（必填；正则与 `ExecutionTarget.name` 约束严格一致 —— Pydantic 在 yaml 加载时 enforce，**禁止**仅在 Protocol 文档上声明而 loader 不校验）
  - `type: Literal["local", "ssh", "replay", "docker", "k8s"]`（必填，discriminator；`docker` 在 `add-docker-target` 提案落地路由到 `DockerEntry`，`k8s` 在 `add-kubernetes-target` 提案落地路由到 `K8sEntry`。`replay` 由 `replay-execution-target` spec 定义其 entry 字段集与运行时语义——本需求只负责把它纳入 discriminator 值域，不在此重复其字段说明）
  - `enabled: bool = True`（默认 enabled；可在 yaml 显式设 false 暂停某 target）。**disabled 行为约定**（**禁止**漂移）：
    - loader：`load_targets_config` 仍加载 disabled target 到 `TargetsConfig.targets`（**不**过滤）
    - registry 装配：`build_registry_from_config` 仍将 disabled target 注册到 registry（**不**过滤 —— 让 list_targets / doctor 能看到所有配置项，方便管理）
    - `registry.list()` / `list_entries()` / `names()` 返回所有 target（含 disabled）
    - `hostlens target test <name>` 对 disabled target 必须 exit 1 + stderr 含 `"target 'xxx' is disabled in targets.yaml"`，**不**触发连接
    - 任何 `ExecutionTarget.exec(...)` / `read_file(...)` 调用前必须检查 entry.enabled；disabled 时 raise `TargetError(kind="target_disabled", target=self.name)`，**不**触发底层连接
    - `hostlens doctor` 对 disabled target 标 `connectivity: "skipped"`（已在 doctor 需求里规定）
    - `list_targets` ToolSpec handler 行为对齐 M2 锁定的 `ListTargetsInput.include_disabled: bool = False` 字段语义：默认 `include_disabled=False` 时 handler **必须过滤掉** `enabled=False` 的 target（输出**只含** enabled）；`include_disabled=True` 时输出**所有** target（含 `enabled=False`），每条 `TargetSummary.enabled` 如实反映；这保持字段名与行为一致，避免"字段叫 include_disabled 但默认仍返回 disabled"的语义混乱
  - `display_name: str | None = None`（人类友好名，可选；缺省时 list_targets 投影用 `name`）
  - `description: str | None = None`（可选说明）
  - `tags: list[str] = Field(default_factory=list)`（默认空 list；Pydantic v2 必须用 `default_factory` 而不是可变默认值 `[]` 避免实例间共享；list_targets 投影直接透传）
- `TargetEntry` SSH-specific 字段集**恰好**为 `{host, user, port, key_path, password, passphrase, connect_timeout}`（7 个字段；`connect_timeout: int | None = None` 允许 per-target override `asyncssh.connect(connect_timeout=...)` 默认值；`extra="forbid"` 防 typo）
- `TargetEntry` Docker-specific 字段集（`type: docker` → `DockerEntry`，**恰好** 2 个字段，`extra="forbid"` 防 typo）：
  - `container: Annotated[str, Field(min_length=1)]`（必填，**非空**）—— 目标容器的 name 或 id。`min_length=1` 由 Pydantic 在 yaml 加载时 enforce（`container: ""` 必须 raise `pydantic.ValidationError`，**不**接受空字符串后在 runtime 才暴露成 `container_not_found`）
  - `docker_host: str | None = None`（可选）—— docker daemon 端点；缺省（`None`）时 DockerTarget 用 `docker.from_env()`（默认本机 `unix:///var/run/docker.sock`）。本提案范围内仅支持本机 unix socket / 无凭据端点；**远程 docker over TCP+TLS 的凭据加载是 follow-up（非目标），不在本字段语义内**。**docker_host 接受集精确定义**（loader 校验，非 `None` 时）：**接受集是唯一窄例外、拒绝是默认 catch-all**——`docker_host` 当且仅当满足「`startswith("unix:///")`（**大小写敏感**小写 `unix://` + **socket 路径以 `/` 开头的绝对路径**，即整体三斜杠起步 `unix:///...`）**且** `unix:///` 之后**非空**」时才接受；**其余任何值一律** raise `ConfigError(kind="docker_host_remote_not_supported", field="docker_host", target=target_name)`。示例性拒绝输入（非穷举，凡不满足接受谓词都拒）：① 远程 scheme（`tcp://` / `ssh://` / `http://` / `https://` / `npipe://`）；② 无 scheme 的裸路径（如 `/var/run/docker.sock`——docker-py 虽接受裸路径，但本提案要求显式 `unix://` 前缀消除歧义）；③ 空 socket 路径（`unix://`）；④ 大小写不符的 scheme（`UNIX://x` / `Unix://x`——`startswith` 大小写敏感，不满足接受谓词即落入默认拒绝）；⑤ socket 路径非绝对（`unix://foo`，相对无前导斜杠——歧义相对 socket，要求绝对路径消除歧义）。**不**静默接受明文 TCP / 裸路径 / 任何歧义端点（否则用户可配出无 TLS 的明文 daemon 连接或歧义端点，与本提案 Security「不扩大网络攻击面、仅本机 socket」声称冲突）
  - **凭据约定**：DockerEntry **不含**任何 secret 字段（本提案默认本机 socket）；`${ENV}` 占位规则对 DockerEntry 无适用 secret 字段——由于既有占位校验是**字段名 allowlist**（`${...}` 仅允许出现在 `password` / `passphrase`），`container` / `docker_host` 含 `${...}` 时已被既有 placeholder walker 自动拒绝为 `env_placeholder_not_allowed_here`，无需 DockerEntry 额外加逻辑
  - **两类校验的执行顺序**（必须固定）：`${...}` 占位拒绝发生在 `_expand_placeholders` 阶段（`model_validate` **之前**，字段名 allowlist 判定）；`docker_host` 的 `unix://` scheme 校验需要 typed `DockerEntry`，发生在 `model_validate` **之后**（实现为遍历 typed entries 的 loader 步骤，或 `DockerEntry` 的 `field_validator` 中 raise `ConfigError` —— **不**能用 `Field(pattern=...)`，因为它 raise `ValidationError` 而非 `ConfigError`）。故 `docker_host: ${X}` 必须**先**命中 `env_placeholder_not_allowed_here`（占位阶段），轮不到 scheme 校验
- `TargetEntry` K8s-specific 字段集（`type: k8s` → `K8sEntry`，**恰好** 5 个字段，`extra="forbid"` 防 typo）：
  - `pod: Annotated[str, Field(min_length=1)]`（必填，**非空**）—— 目标 pod 名。`min_length=1` 由 Pydantic 在 yaml 加载时 enforce（`pod: ""` 必须 raise `pydantic.ValidationError`，不接受空 pod 引用后在 runtime 才暴露成 `pod_not_found`）
  - `namespace: str = "default"`（可选，默认 `"default"`）—— pod 所在 namespace
  - `container: str | None = None`（可选）—— 多容器 pod 内指定容器名；缺省（`None`）走 pod 默认容器（k8s exec API `container=None`）。**与 DockerEntry.container 语义不同**：DockerEntry.container 是必填的容器引用，K8sEntry.container 是 pod 内可选的容器选择器
  - `kubeconfig: str | None = None`（可选）—— kubeconfig 文件路径；缺省时按标准顺序解析（`$KUBECONFIG` / `~/.kube/config`），或在 in-cluster 环境用 ServiceAccount。**路径本身非 secret**（凭据在文件内容 / ServiceAccount token，不落 yaml）
  - `context: str | None = None`（可选）—— kubeconfig 内的 context 名；缺省用 kubeconfig 的 current-context
  - **凭据约定**：K8sEntry **不含**任何明文 secret 字段（凭据走 kubeconfig 文件 / in-cluster ServiceAccount，配置只存路径/context 引用）；`${...}` 占位对 K8sEntry 所有字段（`pod` / `namespace` / `container` / `kubeconfig` / `context`）均**不允许**——它们都是非 secret 字段，含 `${...}` 时被既有 placeholder walker 拒绝为 `env_placeholder_not_allowed_here`
- **凭据字段命名约定**（与 CLI 参数 + proposal Demo Path 严格一致）：
  - `key_path: str | None` —— SSH 私钥文件路径（路径本身非 secret，文件内容才是）；CLI 参数 `--key-path PATH`
  - `password: str | None` —— SSH 密码；CLI 参数 `--password-env VAR`（CLI 不接受明文 `--password`，仅 env 占位）；yaml 中可以是 `${VAR}` 占位或字面值（字面值触发 doctor warn）
  - `passphrase: str | None` —— 加密私钥的 passphrase；CLI 参数 `--passphrase-env VAR`；yaml 同 `password` 规则
- yaml 中 `${VAR_NAME}` 占位必须在加载时展开（从 `os.environ` 读取）；未设置时 raise `ConfigError(kind="missing_env_var", var_name=VAR_NAME, target=target_name)`（依赖 M1 落地的 ConfigError 扩展，见下方需求 §需求:`ConfigError` 必须扩展支持结构化 kind/extra 字段）
- `${...}` 占位**仅**允许出现在 secret 字段（`password` / `passphrase`）—— 出现在 `host` / `user` / `port` / `key_path` / `container` / `docker_host` / `pod` / `namespace` / `kubeconfig` / `context` 等非 secret 字段时 raise `ConfigError(kind="env_placeholder_not_allowed_here", field=field_name, target=target_name)`
- 加载文件不存在时返回空 `TargetsConfig(version="1", targets=[])`（**不**是 TargetRegistry——装配 registry 由 `build_registry_from_config` 负责）；`load_targets_config` 必须**同时**通过 structlog 输出 INFO 级日志「config file not found, returning empty TargetsConfig」+ doctor 会以 hint 状态显示「没有任何已配置 target，跑 `hostlens target add` 开始」—— 不 raise 但也不静默通过

加载入口：`hostlens.targets.config.load_targets_config(path: Path) -> TargetsConfig`

#### 场景:`${ENV}` 占位展开

- **当** yaml 含 `password: ${HOSTLENS_DEMO_PWD}`，环境变量 `HOSTLENS_DEMO_PWD=demo123`
- **那么** 加载后的 `TargetEntry.password == "demo123"`（占位被替换）

#### 场景:env 未设置 raise ConfigError

- **当** yaml 含 `password: ${UNSET_VAR}`，环境无 `UNSET_VAR`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"missing_env_var"`，含 var 名与 target name

#### 场景:占位出现在非 secret 字段 raise

- **当** yaml 含 `host: ${HOST_PLACEHOLDER}` 或 `user: ${USER_PLACEHOLDER}` 或 `container: ${CONTAINER_PLACEHOLDER}`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（防止 host/user/container 通过 env 注入意外暴露）

#### 场景:配置文件不存在返回空 TargetsConfig

- **当** `~/.config/hostlens/targets.yaml` 不存在
- **那么** `load_targets_config(path)` 必须返回 `TargetsConfig(version="1", targets=[])`，**不** raise

#### 场景:unknown type raise

- **当** yaml 含 `type: vm`
- **那么** 加载必须 raise `pydantic.ValidationError`（type 字段是 Literal）

#### 场景:TargetEntry name 不匹配正则 raise

- **当** yaml 含 `name: Prod-Web`（含大写）或 `name: 1web`（数字开头）或 `name: prod web`（含空格）
- **那么** 加载必须 raise `pydantic.ValidationError`，错误指明 name 必须匹配 `^[a-z][a-z0-9_\-]{0,63}$`（loader 强制 enforce，不依赖 ExecutionTarget Protocol 文档的声明）

#### 场景:TargetEntry SSH 字段集严格

- **当** SSH `TargetEntry` 实例化时多传一个未声明字段（如 `agent_forwarding=True` 或 `compression=False`）
- **那么** 必须 raise `pydantic.ValidationError`（`extra="forbid"`），错误指明 unknown field name
- **且** SSH 字段集**恰好**是 `{host, user, port, key_path, password, passphrase, connect_timeout}` 7 个

#### 场景:TargetEntry docker 字段集严格

- **当** yaml 含 `type: docker` 且省略 `container` 字段
- **那么** 加载必须 raise `pydantic.ValidationError`（`container` 必填）
- **且** `container: ""`（空字符串）必须 raise `pydantic.ValidationError`（`min_length=1`），**不**接受空容器引用
- **且** docker `TargetEntry` 多传未声明字段（如 `host=...` 或 `image=...`）必须 raise `pydantic.ValidationError`（`extra="forbid"`）；Docker-specific 字段集**恰好**是 `{container, docker_host}` 2 个

#### 场景:type docker 路由到 DockerEntry

- **当** yaml 含 `name: web-ct`、`type: docker`、`container: my-app`
- **那么** 加载后该条目必须是 `DockerEntry` 实例，`type == "docker"`、`container == "my-app"`、`docker_host is None`

#### 场景:docker_host 远程 scheme 被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: tcp://10.0.0.5:2376`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（只允许 `unix://` 或缺省；不静默接受明文 TCP 端点）

#### 场景:docker_host 无 scheme 裸路径被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: /var/run/docker.sock`（无 `unix://` 前缀的裸路径）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（要求显式 `unix://` 前缀消除歧义；裸路径与空 `unix://` 同样拒绝）

#### 场景:docker_host 空 unix:// 被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix://`（`unix://` 之后 socket 路径为空）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（socket 路径必须非空）

#### 场景:docker_host 相对 socket 路径被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix://foo`（socket 路径 `foo` 相对、无前导 `/`）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（socket 路径须绝对，`unix:///...` 三斜杠起步）

#### 场景:docker_host 合法 unix:// 被接受

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix:///var/run/docker.sock`
- **那么** 加载必须**成功**，该条目是 `DockerEntry`、`docker_host == "unix:///var/run/docker.sock"`（合法本机 socket 端点被保留，**不** raise——验证 loader 不是「全拒型」）

#### 场景:docker_host 占位先于 scheme 校验命中

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: ${SOME_VAR}`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（占位拒绝在 `model_validate` 前的字段名 allowlist 阶段命中，**先于** `unix://` scheme 校验）

#### 场景:type k8s 路由到 K8sEntry

- **当** yaml 含 `name: web-pod`、`type: k8s`、`pod: my-app`
- **那么** 加载后该条目必须是 `K8sEntry` 实例，`type == "k8s"`、`pod == "my-app"`、`namespace == "default"`（缺省）、`container is None`、`kubeconfig is None`、`context is None`

#### 场景:TargetEntry k8s 字段集严格

- **当** yaml 含 `type: k8s` 且省略 `pod` 字段
- **那么** 加载必须 raise `pydantic.ValidationError`（`pod` 必填）
- **且** `pod: ""`（空字符串）必须 raise `pydantic.ValidationError`（`min_length=1`），**不**接受空 pod 引用
- **且** k8s `TargetEntry` 多传未声明字段（如 `image=...` 或 `host=...`）必须 raise `pydantic.ValidationError`（`extra="forbid"`）；K8s-specific 字段集**恰好**是 `{pod, namespace, container, kubeconfig, context}` 5 个

#### 场景:k8s 非 secret 字段占位被拒

- **当** yaml 含 `pod: ${POD_VAR}`（或 `namespace: ${NS_VAR}` / `kubeconfig: ${KC}` / `context: ${CTX}` / `container: ${C}`，type: k8s）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（K8sEntry **全 5 字段** pod/namespace/container/kubeconfig/context 均非 secret，不允许 env 占位）

### 需求:`ConfigError` 必须扩展支持结构化 kind/extra 字段

M0 已落地的 `hostlens.core.exceptions.ConfigError` 当前签名是 `ConfigError(message: str, *, original: Exception | None = None)`，**无法**承载 `kind` / `var_name` / `target` 等结构化字段。M1 落地必须扩展 `ConfigError` 签名为：

```python
class ConfigError(HostlensError):
    def __init__(
        self,
        message: str | None = None,
        *,
        kind: str | None = None,
        original: Exception | None = None,
        **extra: object,  # structured fields (var_name / target / field / ...)
    ) -> None:
        ...
```

- `kind: str | None` —— 结构化错误码（如 `"missing_env_var"` / `"env_placeholder_not_allowed_here"`），便于 doctor 输出结构化诊断
- `**extra` 收集任意 keyword 参数为 `self.extra: dict[str, object]`，让 caller 传 `var_name=...` / `target=...` / `field=...` 等上下文
- 向后兼容：M0 caller `ConfigError("some message")` 必须仍 work（`message` 是位置参数，`kind` 默认 `None`）
- `__str__` 输出格式：`f"{kind}: {message}" if kind else message`，附带 `extra` 字段的 `key=value` 列表（脱敏后，**不**含 secret 值）
- 同期更新 M0 spec `core-services` 中 `ConfigError` 的描述（在本提案的 `core-services` 增量 spec 文件中以 MODIFIED 形式给出）

#### 场景:ConfigError 接受结构化 kind + extra

- **当** `err = ConfigError(kind="missing_env_var", var_name="HOSTLENS_PWD", target="prod-web")`
- **那么** `err.kind == "missing_env_var"` / `err.extra == {"var_name": "HOSTLENS_PWD", "target": "prod-web"}` / `str(err)` 必须含 `"missing_env_var"` + `"var_name=HOSTLENS_PWD"` + `"target=prod-web"`

#### 场景:ConfigError M0 调用风格仍 work

- **当** `err = ConfigError("invalid yaml")`（M0 风格）
- **那么** 必须成功；`err.kind is None` / `err.extra == {}` / `str(err) == "invalid yaml"`

#### 场景:ConfigError extra 不泄露已知 secret 字段名值

- **当** `err = ConfigError(kind="invalid_field", field="password", target="prod-web")` —— 注意 caller 传了 `field="password"` 但**不**传 password 值本身
- **那么** `str(err)` 含 `"field=password"` 但**不**含任何 password 实际值（caller 不传，extra 也存不进来）；**禁止** caller 通过 extra 传具体 secret 值（约定层面，spec 文档说明 caller 责任）

### 需求:`hostlens target` CLI 命令集且写命令拒绝 root

`hostlens target` Typer 子命令组必须提供：

- `add <name> --type local|ssh [--host HOST --user USER --port PORT --key-path PATH --password-env VAR --passphrase-env VAR]`：写 `targets.yaml` + 校验；name 已存在时 raise + exit 2（参数错误）；**写操作命令在 EUID==0 时直接 exit 1**（CLAUDE.md §4.5 + 全局"写操作必须拒绝 root"）
- `list [--json]`：表格（默认）或 JSON 输出已配置 target + 每个 target 当前 capabilities + enabled 状态（只读，允许 root）
- `remove <name>`：默认交互确认 y/N；`--yes` 跳过；非交互无 TTY 无 `--yes` 必须 exit 1；**EUID==0 时直接 exit 1**
- `test <name>`：跑 `echo hostlens-probe-$$` 验证连通性；输出 ExecResult + 探测到的 capabilities；连通失败 exit 1（只读，允许 root）
- `import <inventory> [--source ssh_config|yaml] [--dry-run] [--yes] [--skip-unreachable|--include-unreachable] [--concurrency N] [--json]`：从 inventory 来源批量纳管（流水线 source → 提升 → probe → plan → save）；**`--dry-run` 默认**（预览 + exit 0，不写盘）；`--yes` 落盘；**写操作命令在 EUID==0 且本次落盘时直接 exit 1**；行为契约见 `target-import` capability。**`--source` 必须实现为裸 `str` + 命令体手动校验**（未知值 `typer.Exit(2)`），**禁止** `Choice`/`Enum`（其 `UsageError` 经 `cli/__init__.py` 包装为 exit 3，与"参数错=exit 2"冲突）

CLI 参数命名约定（**禁止**漂移；与 proposal Demo Path 与 `TargetEntry` 字段严格一致）：

- `--key-path PATH` 对应 `TargetEntry.key_path`（**禁止**用 `--key-env` / `--key` / `--identity-file` 等别名）
- `--password-env VAR` 对应在 yaml 写 `password: ${VAR}`（CLI 不接受明文 `--password STR`）
- `--passphrase-env VAR` 对应在 yaml 写 `passphrase: ${VAR}`
- `import` 的 `--source` / `--dry-run` / `--yes` / `--skip-unreachable` / `--include-unreachable` / `--concurrency` / `--json`（**禁止**漂移成 `--inventory-type` / `--apply` / `--parallel` 等别名）

所有命令必须使用 M0 已落地的 structlog logger；错误输出走 stderr，数据走 stdout。

#### 场景:target add EUID==0 直接 exit 1

- **当** 以 root（`os.geteuid() == 0`）跑 `hostlens target add my-local --type local`
- **那么** 命令必须**立即** exit 1（**先于**参数校验和 yaml 写入），stderr 含修复建议（"请用普通用户运行；如必须以 root 部署 daemon，先在普通用户下创建配置文件再 chown"）
- **且** `targets.yaml` 必须**未**被创建或修改

#### 场景:target remove EUID==0 直接 exit 1

- **当** 以 root 跑 `hostlens target remove my-local --yes`
- **那么** 命令必须 exit 1；`targets.yaml` 必须**未**被修改

#### 场景:target list / test 允许 root

- **当** 以 root 跑 `hostlens target list --json` 或 `hostlens target test my-local`
- **那么** 命令必须正常执行（**不**因 EUID==0 拒绝）；只读语义无 root-owned 配置污染风险

#### 场景:target add 名称冲突 exit 2

- **当** `targets.yaml` 已有 `name: prod-web`，**以非 root** 跑 `hostlens target add prod-web --type local`
- **那么** 命令必须 exit 2（参数错误），stderr 含 `"target 'prod-web' already exists"`

#### 场景:target add 凭据参数命名一致

- **当** 跑 `hostlens target add my-ssh --type ssh --host x --user y --key-path /tmp/id_rsa --password-env PWD --passphrase-env PASS`
- **那么** 必须成功（参数命名匹配 `TargetEntry` 字段名）；**禁止** CLI 接受 `--key-env` / `--password` / `--passphrase` 等别名（参数 typo 必须 exit 2）

#### 场景:target remove 无 TTY 无 --yes exit 1

- **当** 在非交互环境跑 `hostlens target remove prod-web`（非 root，无 stdin TTY，且未传 `--yes`）
- **那么** 必须 exit 1，stderr 提示 `"--yes required in non-interactive mode"`；**禁止**默默执行删除

#### 场景:target list --json 输出结构化

- **当** 跑 `hostlens target list --json`
- **那么** stdout 必须是合法 JSON，含 `targets: [{name, type, enabled, capabilities: [...]}]`

#### 场景:target test 连通失败 exit 1

- **当** 跑 `hostlens target test ssh-prod` 但远端不可达
- **那么** 必须 exit 1，stderr 含 `ssh-execution-target` spec 定义的标准 error kind（M1 范围内：`ssh_connect_timeout` / `ssh_connection_lost` / `ssh_auth_failed`；CLI 直接把 `TargetError.kind` 当 error kind 输出），但**不含**凭据；stdout 为空

#### 场景:target import EUID==0 落盘时直接 exit 1

- **当** 以 root 跑 `hostlens target import inv.yml --yes`
- **那么** 命令必须在落盘前 exit 1，`targets.yaml` **未**被创建/修改（dry-run 预览不落盘，故不受 EUID 拒绝——但带 `--yes` 的落盘路径必须拒 root）

### 需求:`hostlens doctor` 必须新增 targets 健康检查

`hostlens doctor` 必须扩展输出新增 `targets` section，对每个已配置 target 报告：

- `connectivity`：`ok` / `failed` / `skipped`（disabled 的 target 标 `skipped`）
- `credential_source`：`env_var` / `inline_plaintext`（后者必须 warn）
- `capabilities`：探测到的 capability 集合

`--json` 输出必须含 `targets` key；任一 target `connectivity == "failed"` 必须使 doctor 整体 exit 1（与 M0 doctor 退出码语义一致）。

#### 场景:doctor 检测明文密码 warn

- **当** `targets.yaml` 含 `password: literal-pwd-not-env-placeholder`
- **那么** `hostlens doctor` 必须输出 warning（含 target name 与修复建议）；但 doctor 整体**不** exit 1（仅 warning 不阻塞）

#### 场景:doctor --json 含 targets section

- **当** 跑 `hostlens doctor --json`
- **那么** stdout 是合法 JSON，必须含 `"targets": [{...}]` key

#### 场景:某 target 连通失败 doctor exit 1

- **当** 已配置 SSH target 不可达；跑 `hostlens doctor`
- **那么** 整体 exit 1；输出含失败 target 名与错误 kind

### 需求:`save_targets_config` 必须原子、幂等、保全 `${VAR}`、文件权限 0600；序列化 helper 下沉至 config 层

系统必须在 `targets/config.py` 新增 `save_targets_config`（与 `load_targets_config` 互逆、同属 `TargetsConfig` 持久化契约、共享 `_PLACEHOLDER_ALLOWED_FIELDS` 占位防线，故归 `execution-target` capability）。

**序列化 helper 下沉 + 原子写原语共享（解 `config↔cli` 反向依赖 + 0600 防线一致性）**:`save_targets_config` 复用的 `_load_raw_targets_dict` 与 `_entry_to_dict` **当前物理在 `cli/target.py`**;若 `targets/config.py` 反向 `from hostlens.cli.target import ...` 会造 `config↔cli` 循环导入、破坏 config 层「free of concrete-target imports / 可安全从 doctor import」的隔离。故本变更**必须把这两个 helper 从 `cli/target.py` 下沉到 `targets/config.py`**（它们只依赖 `LocalEntry`/`SSHEntry` 模型与 yaml/Path，**不**依赖 concrete target，下沉不破坏隔离），`cli/target.py` 的 `add`/`remove` 改为 `from ...config import`。

**0600 原子写原语共享(防既有命令侵蚀)**:本提案前,`targets.yaml` **无任何 0600/原子写**——既有 `add`/`remove`(`cli/target.py`)用裸 `cfg_path.write_text(yaml.safe_dump(...))`(非原子、继承 umask 典型 `0o644` world-readable、父目录 `0o755`)。若仅 `import` 写 0600 而 `add`/`remove` 仍裸 `write_text`,**下一次 `add` 会把文件重写回 0644、抹掉 0600**——0600 防线被 sibling 命令侵蚀、形同虚设。故本变更**必须把原子 0600 写抽成共享原语 `_atomic_write_yaml(path, raw_dict)`**(`mkstemp(dir=同目录)` → 写 → `fchmod(0o600)` → `os.replace`;父目录 `0o700` 收紧),`save_targets_config` **与 `add`/`remove` 的最终写盘步骤都改调它**(add/remove 的 raw-dict 变更逻辑不变,仅末尾 `write_text` → `_atomic_write_yaml`,**输出字节同形 + 获得原子性 + 0600**)。这是「行为改进非破坏」:add/remove 输出逐字段不变,新增原子+0600。

接口与语义：

- **入参带凭据 env 引用**:`save_targets_config` 接收的每条须携带 `(TargetEntry, password_env?, passphrase_env?)`——因 `_entry_to_dict` 从**独立 env 名参数**（非 `entry.password`，后者可能是展开后明文）还原 `${VAR}`;cred-less/key_path 条目传 `None`。
- **原子写**：经 `_atomic_write_yaml`——`tempfile.mkstemp(dir=<targets.yaml 同目录>)`（同文件系统保证 `os.replace` 原子；不可预测名防 symlink/预测路径）→ 写入 → **`os.replace` 前显式 `os.fchmod(fd, 0o600)`**（不靠 mkstemp 默认值，契约可测）→ `os.replace`；中断不留半文件。
- **文件 + 父目录权限（幂等收紧，非仅创建时设）**：文件 `0o600`；父目录 `~/.config/hostlens/` —— 不存在则 `os.makedirs(mode=0o700)`，**已存在则 `os.chmod(parent, 0o700)` 收紧**（既有 `0o755` 目录必须被收紧）。
- **幂等 upsert**：按 `name` upsert，已存在默认 skip 不覆盖。
- **`${VAR}` 占位保全**：复用 `_load_raw_targets_dict` raw round-trip（**不** expand）。
- **与 `target add` 输出同形**：复用 `_entry_to_dict`（含 `enabled is False 才显式写`、`port != 22 才显式写` 约定）。
- **失败映射**：父目录不可写 / `mkstemp` `OSError` → 结构化错误（CLI 映射 exit 2），不裸 `OSError` 透传。

#### 场景:写盘原子不留半文件

- **当** `save_targets_config` 写入过程中进程被中断
- **那么** `targets.yaml` 要么写前旧内容、要么写后新内容，**禁止**半截/损坏文件

#### 场景:文件 0600（显式 fchmod）+ 既有父目录被收紧为 0700

- **当** `save_targets_config` 写盘，且 `~/.config/hostlens/` 已存在且为 `0o755`
- **那么** 文件经 `os.fchmod(fd, 0o600)` 显式设权（不靠 mkstemp 默认），父目录经 `os.chmod` 收紧为 `0o700`；临时文件经 `mkstemp(dir=同目录)` 不可预测名

#### 场景:重跑幂等不重复

- **当** 对同一批已纳管候选再次 `save_targets_config`
- **那么** 已存在 `name` 条目不重复追加、不覆盖

#### 场景:既有 `${VAR}` 占位写回保持

- **当** `targets.yaml` 已有条目引用 `${SOME_ENV}`，本次追加新条目
- **那么** 既有 `${SOME_ENV}` 占位原样保留，**禁止**展平成明文 secret

#### 场景:helper 下沉 + 原子写原语后 add/remove 输出不变但获 0600

- **当** `_entry_to_dict`/`_load_raw_targets_dict` 下沉到 `config.py`、add/remove 末尾写盘改调 `_atomic_write_yaml`
- **那么** `target add`/`remove` 的写盘输出**逐字段不变**（无行为漂移），但写盘变为原子 + 文件 `0o600`（修掉「import 写 0600、下次 add 抹回 0644」的侵蚀）；`import hostlens.targets.config` **无** `config↔cli` 循环

#### 场景:与 target add 输出逐字段同形

- **当** `save_targets_config` 与 `target add` 写出同一逻辑条目（cred-less ssh / key_path ssh / local 各一）
- **那么** 两者产出的 yaml dict 逐字段相等（复用 `_entry_to_dict` + 同一 env 名透传，含 `enabled`/`port` 省略约定）
