## 修改需求

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
