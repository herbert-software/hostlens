## 修改需求

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

## 新增需求

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
