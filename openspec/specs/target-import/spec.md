# target-import 规范

## 目的
待定 - 由归档变更 add-cli-target-import 创建。归档后请更新目的。
## 需求
### 需求:`CandidateTarget` 必须先提升为 `TargetEntry`,经 registry 构造注入 `_entry` 才能探测,提升失败归 `invalid_candidate` 不崩

探测需要一个真实 target 实例,且 SSH/local target 的连接参数(host/user/凭据)**只能经 `TargetRegistry.register` 注入的 `self._entry` 读取**——裸 `SSHTarget(name=...)` 的 `_entry is None`,首次 `exec` 直接 raise `TargetError(kind="ssh_no_entry")`(见 `targets/ssh.py`)。故系统**必须**:

1. **提升**:把 `CandidateTarget`(来源层宽松模型)映射为 `TargetEntry` 并经 `LocalEntry`/`SSHEntry` 的 Pydantic 校验(`_NAME_PATTERN` + `extra="forbid"`)。
2. **构造(必须注入 `_entry`)**:提升后用 `build_one_target(entry, settings)` helper 构造单 target——其实现**必须复用既有 construct+register 路径**(把单个 `TargetEntry` 包成 `TargetsConfig(version="1", targets=[entry])` 喂 `build_registry_from_config(config, settings)` 再 `.get(name)`),从而构造**并 register**、注入 `self._entry`。**禁止**裸实例化 `SSHTarget(name=...)` 不 register(`_entry=None` → `exec` raise `ssh_no_entry`,首批 tizi SSH 全失败)。`build_one_target` 的 `settings` 仅 SSH 分支消费(对齐 `build_registry_from_config` 的 `_settings` 注入),local 分支不用——保留为签名对称。

提升**失败**(name/字段不符 entry schema)**必须**归入 `ImportPlan.invalid_candidate`(附 `ValidationError` 脱敏摘要)、整批其余继续,**禁止**抛异常崩整批。**理由(非防御性 fallback)**:source 层(规范化)与 config 层(`TargetEntry` Pydantic 校验)是**两个独立契约边界**,跨边界不变量由**显式分类**承接而非 silent crash——不是「给不可能分支加兜底」(CLAUDE.md §6),而是两套独立校验的边界对账。提升的校验真相**归 `execution-target` 的 `LocalEntry`/`SSHEntry` schema**(后者字段集变更时,本提升映射须同步评审——跨 capability 契约依赖点)。

#### 场景:合法候选提升后经 registry 构造注入 `_entry`、可 exec
- **当** 一个 name 合法、字段完整的 SSH `CandidateTarget` 被提升 + `build_one_target` 构造
- **那么** 产出的 target 已绑定 `_entry`(经 `build_registry_from_config` 的 register 注入),`exec` **不** raise `ssh_no_entry`,可真实探活

#### 场景:提升失败归 invalid_candidate 不崩
- **当** 某候选提升时 `LocalEntry`/`SSHEntry` 校验 `ValidationError`
- **那么** 该候选归入 `ImportPlan.invalid_candidate`(附脱敏摘要),整批其余候选继续,**禁止**抛异常崩整批

### 需求:`TargetProbe` 必须复用 ExecutionTarget、先 exec 判可达、产可序列化脱敏 `ProbeResult`

系统必须提供 `TargetProbe`,把「可达性探活 + 能力探测 + OS/runtime 指纹」编排成一次探测,**必须复用既有 `ExecutionTarget`**(不另起连接栈)。`capabilities` 是 **lazy-probe**(`targets/base.py`,首次 `exec` 才填),故探测**必须先触发一次只读 `exec`**,并顺带读到该 target 的 `capabilities` 与指纹。

**`reachable` 判定(精确,消除「exec 成功」歧义)**:`LocalTarget.exec`/SSH `exec` 对**超时**返回 `ExecResult(timed_out=True, exit_code=None)`、对**非零退出**返回 `ExecResult(exit_code≠0)`——两者**都不抛**(仅 transport 级失败 / `target_disabled` 抛 `TargetError`)。故 `reachable` **不能**以「exec 不抛」判定,否则超时会被误判 reachable、与 `error_kind=timeout`/「慢台进 failed_probe」自相矛盾。判定规则:`reachable := exec 返回 `ExecResult` 且 `not result.timed_out``(transport 成功 + 命令跑完);`timed_out=True` → `reachable=False` + `error_kind="timeout"` → `failed_probe`;**`exit_code≠0` 仍算 `reachable=True`**(能登能跑即可达——探测命令非零退出如 minimal 容器无 `/etc/os-release` 不该判不可达,只是 fingerprint 部分缺失)。`error_kind="timeout"` 来源有二:`TargetError(kind="ssh_connect_timeout")`(连接期)与 `ExecResult.timed_out=True`(命令期),两者统一映射 `timeout`。

**`ProbeResult` 价值边界(纠正错误前提)**:探测的产物**不写进 `targets.yaml`**——`capabilities` **从不是** `TargetEntry` 字段、不持久化(`target list`/`test` 每次从 target 实例**实时** lazy-probe)。故探测的落盘价值是**可达性判定**(决定候选进 `to_add` 还是 `failed_probe`),`capabilities`/指纹仅供 `ImportPlan` **展示**(及未来 inspector 选择参考),**不**进 `targets.yaml`。spec **禁止**出现「不先 exec → 写进 targets.yaml 的能力是空集」这类伪命题表述。

**`ProbeResult` 形状(可序列化 + 脱敏)**:必须只持脱敏标量——`reachable: bool` / `capabilities: list[str]` / `fingerprint: dict[str, str]` / `error_kind: str | None`。三类硬约束:

- **`capabilities` 来自 target 的 lazy-probe、非探测命令**:`capabilities` = `target.capabilities`(`Capability` 枚举值投影,闭集 `{shell, file_read, ssh, systemd, docker_cli}`,`targets/base.py`)。探测命令里的 `command -v docker podman kubectl` 结果**不**直接进 capabilities(`Capability` 枚举**无** PODMAN/KUBECTL),而进 `fingerprint.runtime`(字符串)。**禁**塞探测异常文本。
- **`error_kind` 是闭集枚举 + 全映射表**:取值闭集 = `{"unreachable", "auth_failed", "timeout", "exec_failed"}`(`reachable=True` 时为 `None`);实现须像 `_INSPECTOR_ERROR_KINDS` 钉死闭集 + 构造校验,**禁即兴加 kind**。从 `TargetError.kind`(probe 经 `exec` 可达的全集,见 `targets/ssh.py`)**完整映射**:`ssh_connect_timeout → timeout`;`ssh_auth_failed → auth_failed`;`{ssh_connect_failed, ssh_connection_lost} → unreachable`;`{ssh_no_entry, target_disabled, 及任何未来未列 kind} → exec_failed`(**fallback 显式归 `exec_failed`**,使未来新增 `TargetError.kind` 有确定归宿、不破闭集)。**禁止**承载自由文本异常消息 / `original` 链 / traceback。
- **`fingerprint` 键白名单 + 值脱敏**:键白名单 = `{"os", "kernel", "arch", "runtime"}`,**禁** `hostname`(内网拓扑情报;`hostname` 命令可跑判 reachable 但输出不进 fingerprint)。**值同样不可信**(`/etc/os-release` 是远端可控文件,攻击者可在 `PRETTY_NAME` 塞内网主机名/IP 经合法键 `os` 走私):每个值必须**截断 ≤64 字符 + 剥离控制字符/换行**;fingerprint 值仅作**展示标签**,**禁**作安全判定依据(spec 显式信任声明:值来自远端不可信文件)。

**禁止**内嵌不可 JSON 序列化对象。探测命令必须是**不含任何 inventory 派生字段插值的固定字面量**;采 os-release 用 `cat /etc/os-release`(发起方解析 `KEY=value`)**而非** `. /etc/os-release`(`source` 会在远端 shell **执行**不可信文件)。连接参数(host/user/port)仅经 `ExecutionTarget` 参数化接口传递,**禁止**字符串拼接进命令体。`probe_many` 必须并发 + 信号量限流;单台探活失败(`TargetError`/timeout)隔离进 `failed_probe`,**禁止**抛、**禁止**拖垮整批。

#### 场景:exec 返回且未超时判 reachable、capabilities 进 ProbeResult 不进 yaml
- **当** `TargetProbe.probe` 探测一个可达 target,`exec` 返回 `ExecResult(timed_out=False)`(exit_code 可为 0 或非 0)
- **那么** `reachable=True`(由 `exec 返回且 not timed_out` 判定,**不**以 capabilities 非空判定——local target 构造即有非空 capabilities);`capabilities` 读入 `ProbeResult` 供 plan 展示,**不**写进 `targets.yaml`(yaml schema 无此字段)

#### 场景:exec 超时判 failed_probe(不误判 reachable)
- **当** 探测命令 `exec` 返回 `ExecResult(timed_out=True, exit_code=None)`(命令期超时,不抛)
- **那么** `reachable=False` + `error_kind="timeout"` → `failed_probe`(**禁**因「exec 不抛」误判 reachable=True)

#### 场景:探活失败隔离且 error_kind 为闭集枚举
- **当** 探测一个连不上 / auth 失败的 target
- **那么** 返回 `ProbeResult(reachable=False, error_kind ∈ {"unreachable","auth_failed","timeout","exec_failed"})`(闭集枚举,**不**含 host IP / user@host / traceback 明文),**禁止**抛异常、**禁止**影响同批其他主机

#### 场景:fingerprint 不含 hostname
- **当** 探测产出 `fingerprint`
- **那么** 键仅取白名单 `{os, kernel, arch, runtime}`,**不**含 `hostname`(即便探测命令跑了 `hostname` 判 reachable)

#### 场景:ProbeResult 可 JSON round-trip
- **当** `ProbeResult` 经 `model_dump_json` 序列化再 `model_validate_json` 反序列化
- **那么** 等价(只含标量字段,无内嵌不可序列化对象)

#### 场景:探测命令为固定字面量不插值
- **当** `TargetProbe` 构造探测命令
- **那么** 命令是不含任何 inventory 派生字段(host/user/tag)插值的固定字面量,连接参数仅经 `ExecutionTarget` 参数化接口传递

#### 场景:并发探测受限流约束
- **当** `probe_many` 探测一批主机
- **那么** 同时在途连接数受信号量上界约束(默认 ≤ 10,`--concurrency` 可配),避免握手风暴

### 需求:`ImportPlan` 必须四分类、可序列化 round-trip、渲染禁泄露

系统必须定义 `ImportPlan`(**纯 Pydantic v2,可 `model_dump_json`/`model_validate_json` round-trip**——为 dry-run 产物落盘 + 提案 B `add-mcp-target-import-propose` 的 `--from-plan` 复用留缝):把候选分四桶,**每桶元素是具名 Pydantic model(非裸 tuple——tuple 在 Pydantic v2 走位置反序列化、叠加 `TargetEntry` 判别联合歧义大,与 `ProbeResult` 具名建模不一致)**:

- `to_add: list[PendingAdd]`,`PendingAdd{entry: TargetEntry, password_env: str|None, passphrase_env: str|None}` —— 探活成功且 name 不冲突(落盘需 entry + 凭据 env 引用,见 save 需求);**`entry.password`/`passphrase` 恒为 `None`**(凭据仅经 `password_env`/`passphrase_env` 字段透传,**禁**内联 `${VAR}` 进 entry 造双写)
- `skipped: list[str]` —— name 已在 `targets.yaml`(幂等不覆盖,只需 name)
- `failed_probe: list[FailedProbe]`,`FailedProbe{entry: TargetEntry, result: ProbeResult, password_env: str|None, passphrase_env: str|None}` —— 探活失败(已提升);**携带凭据 env 引用**(同 `PendingAdd`)——`--include-unreachable` 落盘 `enabled=False` 条目时须保全 `${VAR}` 占位,否则操作者日后 re-enable 该主机会丢失 auth
- `invalid_candidate: list[InvalidCandidate]`,`InvalidCandidate{candidate: CandidateTarget, error_summary: str}` —— 提升失败(脱敏摘要)

`ImportPlan` 是写盘前最后一个**只读**产物,必须可渲染成人可读 diff + `--json`。渲染(diff + `--json`)对 `failed_probe`/`invalid_candidate` **禁止**输出未脱敏的 host 原始地址 / `user@host` / `original` 异常链 / traceback(对齐 `_emit_target_error` 纪律:只 `error_kind` 枚举 + 候选 name);`to_add` 必须**完整列出每个候选的最终连接地址(host)**,使操作者能在 `--yes` 前审计是否有非预期主机(把 dry-run 从「预览」升为「落盘前地址审计点」)。`name` 规范化的 `原始标识 → 派生 name` 映射须可在 plan 展示。

**序列化落盘安全**:`ImportPlan` 经 `model_dump_json` **落盘**(dry-run 产物 / 提案 B `--from-plan`)时含 `to_add` 的明文 host(横向移动地图),**必须复用 `save_targets_config` 的 `0600` 原子写纪律**(同 `mkstemp` + `0600`),**禁止** world-readable 落盘绕过 `targets.yaml` 的 `0600` 防线。`--json` 输出到 stdout 是操作者面向的(可含 host,审计需要),但落盘文件必须 `0600`。

#### 场景:四分类 + 元素类型
- **当** 一批候选含「已存在 name」「探活失败」「提升失败」各一
- **那么** 分别进 `skipped`(name 串) / `failed_probe`(entry+ProbeResult) / `invalid_candidate`(候选+错误摘要),其余探活成功且 name 不冲突的进 `to_add`(entry+凭据 env 引用)

#### 场景:空 inventory → 空 plan → exit 0
- **当** inventory 合法但零主机条目(空文件 / 空分组)
- **那么** 产出空 `ImportPlan`(四桶皆空),CLI 输出「nothing to import」,**exit 0**(非错误)

#### 场景:plan 可 JSON round-trip
- **当** `ImportPlan` 经 `model_dump_json` 再 `model_validate_json`
- **那么** 等价(各桶元素纯标量/Pydantic,无内嵌不可序列化对象)

#### 场景:plan 落盘必须 0600
- **当** `ImportPlan` 经 `model_dump_json` 落盘(dry-run 产物 / 提案 B `--from-plan`)
- **那么** 文件权限 `0600`(复用 `save_targets_config` 原子写纪律),**禁止** world-readable

#### 场景:渲染不泄露 host/凭据
- **当** `failed_probe` / `invalid_candidate` 渲染进 diff / `--json`
- **那么** 仅含 `error_kind` 枚举 + 候选 name,**禁止**未脱敏 host IP / user@host / 异常 traceback / fingerprint 中的敏感值

#### 场景:to_add 完整列出连接地址供审计
- **当** dry-run 渲染 `ImportPlan`
- **那么** `to_add` 每条完整列出最终 `host` 地址(操作者落盘前可审计有无非预期主机);`原始标识 → 派生 name` 在两者不同时一并展示(diff 的 `name (from <raw>)` + `--json` 的 `raw_identifier` 字段)

#### 场景:host/user 含控制字符提升即拒(预览与落盘一致)
- **当** 候选的 `host` 或 `user` 含控制 / 双向(bidi)/ 行分隔字符(Unicode 类目 `Cc`/`Cf`/`Zl`/`Zp`)
- **那么** **提升即失败**归 `invalid_candidate`(不进 `to_add`、不落盘),使「预览展示的地址」恒等于「落盘写入的地址」——杜绝「预览脱敏成干净串、`--yes` 却写入不同的原始连接串」的伪造面

### 需求:import 落盘必须经 execution-target 的 `save_targets_config`,`enabled` 按可达性定

import 的写盘**必须**调用 `execution-target` capability 新增的 `save_targets_config`(原子 / 幂等 upsert / `${VAR}` 保全 / 0600 权限——契约见 `execution-target` spec),**不**在 import 侧自建写逻辑。因序列化器 `_entry_to_dict` 从**独立的 `password_env`/`passphrase_env` 参数**(非 `entry.password`,后者可能是展开后的明文)还原 `${VAR}`,import 落盘**必须把 `to_add` 桶里每条的凭据 env 引用(来自 `CandidateTarget.password_env`/`passphrase_env`)一并传给 `save_targets_config`**(`to_add` 元素即 `(TargetEntry, password_env?, passphrase_env?)`),否则带凭据引用的 SSH 条目写不出 `${VAR}` 占位(首批 tizi cred-less 不触发,但 yaml 标准 schema 把 `password_env`/`passphrase_env` 列为一等公民,契约须支持)。写出条目的 `enabled` 字段(`_CommonEntryFields.enabled: bool=True`)按可达性定:`to_add`(探活成功)写 `enabled=True`;`--include-unreachable` 强制登记的 `failed_probe` 候选写 `enabled=False`(登记但不激活)。落盘序列化复用 `_entry_to_dict`(`enabled is False` 才显式写、`port != 22` 才显式写),保证与 `target add` 写出**逐字段同形**(crosscheck 覆盖 cred-less ssh + key_path ssh + local 三类条目)。

#### 场景:to_add 带凭据引用透传 env 名落盘
- **当** 一个带 `password_env` 的 SSH 候选探活成功落盘
- **那么** `to_add` 元素携带该 `password_env`,`save_targets_config` 经 `_entry_to_dict(entry, password_env=...)` 写出 `password: ${VAR}` 占位

#### 场景:to_add 写 enabled=True
- **当** 探活成功的候选落盘
- **那么** 写出条目 `enabled=True`(复用 `_entry_to_dict` 的「True 时省略」约定),与 `target add` 输出同形

#### 场景:include-unreachable 候选写 enabled=False
- **当** `--include-unreachable` 强制登记一个探活失败的候选
- **那么** 写出条目 `enabled=False`(显式写),避免后续巡检对其报噪声

### 需求:`hostlens target import` 必须 dry-run 默认预览、--yes 落盘、拒 root、退出码与项目契约一致

系统必须提供 `hostlens target import <inventory>` 命令,串联 source → 提升 → probe → plan → save 流水线。语义:

- **`--dry-run`(默认)**:渲染 `ImportPlan` 后**停止**、exit 0,**不写** `targets.yaml`;输出须醒目标注「DRY-RUN,未写盘,传 `--yes` 落盘」。**注意**:dry-run 仍会**探活**(对 inventory 主机发起只读 SSH exec 拿 reachable)——它对**本地 `targets.yaml` 零副作用**,但对远端有只读连接;"零副作用"仅指不改本地配置,不指不连远端。
- **`--yes`**:关闭 dry-run 并落盘 to_add(+`--include-unreachable` 时含 failed_probe)。import 用**显式 `--yes` 落盘**,**无**逐条交互 y/N prompt(批量场景不可行);故缺 `--yes` 即走 dry-run 预览 exit 0(不写盘即满足「非交互不默默写盘」,无「非交互退 1」分支)。
- **拒 root**:`EUID==0` 时(若本次会落盘,即带 `--yes`)直接 exit 1,`targets.yaml` 不被创建/修改(复用 `_refuse_root_for_write`)。
- **`--dry-run` 与 `--yes` 互斥**:同时传 → exit 2(参数错),消除「谁优先」歧义(fail-safe,防 muscle-memory 带 `--dry-run` 又加 `--yes` 误落盘)。
- **`--skip-unreachable`(默认)**只纳管探活成功的;`--include-unreachable` 逃生舱强制登记 failed_probe(enabled=False)。
- **既有 `targets.yaml` 前置校验**:落盘前(及算 skipped name 集时)必须 `load_targets_config(expand_env=False)` 校验既有文件 schema(镜像 `target add` 的前置校验)——既有文件含非法占位字段(如 `host: ${X}`)/ 损坏 → raise `ConfigError` → exit 2(**不**用纯 raw round-trip 静默放行损坏文件)。**写**阶段才用 `_load_raw_targets_dict`(保 `${VAR}`)。
- **退出码**(与项目契约一致):`0` 成功(含 dry-run 预览、`--include-unreachable` 全失败仍登记、空 inventory);`1` 业务失败(拒 root、`--yes` 落盘但全部探活失败且非 `--include-unreachable`);`2` 参数·配置错。**关键**:`--source` **必须**实现为裸 `str` 选项 + 命令体内手动校验 → 未知值 raise `typer.Exit(2)`;**禁止**用 `Choice`/`Enum`(后者未知值触发 Click `UsageError`,经 `cli/__init__.py` 包装为 **exit 3**,与「参数错=exit 2」契约冲突)。`--dry-run`+`--yes` 同传 / inventory 解析失败 / 既有 `targets.yaml` 损坏 / **`load_settings()` / 配置加载失败**(镜像 `target add` 的 `ConfigError`→exit 2,见干净 CI 缺 backend 配置场景)→ exit 2。

#### 场景:--dry-run 默认只预览不写盘
- **当** 运行 `hostlens target import inv.yml`(无 flag,默认 dry-run)
- **那么** 渲染 `ImportPlan` + 醒目「DRY-RUN」标注,exit 0,`targets.yaml` **不**被修改

#### 场景:--yes 落盘
- **当** 运行 `hostlens target import inv.yml --yes` 且有 to_add 项
- **那么** to_add 条目经 `save_targets_config` 原子写入 `targets.yaml`,exit 0

#### 场景:非交互缺 --yes 走 dry-run 不退 1
- **当** 无 TTY 运行 `hostlens target import inv.yml`(无 `--yes`)
- **那么** 走 dry-run 预览 exit 0(不写盘,满足「不默默写盘」),**不** exit 1(import 无交互 prompt,缺 --yes 即预览)

#### 场景:EUID==0 落盘前 exit 1
- **当** 以 root 运行 `hostlens target import inv.yml --yes`
- **那么** 落盘前 exit 1,`targets.yaml` **不**被创建/修改

#### 场景:未知 --source 经手动校验 exit 2(非 UsageError exit 3)
- **当** 运行 `hostlens target import inv.yml --source nonesuch`
- **那么** 命令体手动校验未知 source → `typer.Exit(2)`(**不**经 Choice/Enum 的 UsageError→exit 3 路径)

#### 场景:inventory 解析失败 exit 2
- **当** inventory 文件语法畸形(source `parse` raise `ConfigError`)
- **那么** exit 2,不进入探测

#### 场景:--dry-run 与 --yes 同传 exit 2
- **当** 运行 `hostlens target import inv.yml --dry-run --yes`
- **那么** 互斥冲突 → `typer.Exit(2)`,**不**落盘(fail-safe)

#### 场景:既有 targets.yaml 含非法占位 exit 2
- **当** 既有 `targets.yaml` 某条 `host: ${X}`(非 secret 字段含占位),跑 import
- **那么** 前置 `load_targets_config(expand_env=False)` raise `ConfigError(env_placeholder_not_allowed_here)` → exit 2(**不**经 raw round-trip 静默放行损坏文件)

#### 场景:配置加载失败 exit 2
- **当** 干净环境缺 backend 配置,`load_settings()` raise `ConfigError`
- **那么** exit 2(镜像 `target add` 的配置错处理)

#### 场景:--yes 候选全部失败(探活或提升)退 1
- **当** `--yes` 但**没有任何候选可纳管**——候选要么探活失败(且未带 `--include-unreachable`)、要么提升失败归 `invalid_candidate`(空 inventory / 全 `skipped` 除外)
- **那么** 无可纳管项,exit 1,`targets.yaml` 不被修改;区别于「空 inventory / 全已存在」(无失败)走 exit 0

#### 场景:--include-unreachable 全失败仍登记成功
- **当** `--yes --include-unreachable` 且候选全部探活失败
- **那么** 全部以 `enabled=False` 落盘,exit 0(逃生舱语义)

#### 场景:成功计数反映实际追加数(非 len(entries))
- **当** `--yes` 落盘时部分 to_add 的 name 已存在于 `targets.yaml`(`save_targets_config` 幂等跳过)
- **那么** CLI 报告的「imported N」为**实际追加**条数(已存在者计入 skipped 后缀),不得用 `len(save_entries)` 高报

### 需求:`hostlens target import --from-plan <path>` 必须从序列化 `ImportPlan` 直接落盘、跳过 source/probe、inventory 与 --from-plan 恰好二选一、复用 --yes/拒 root 门

`hostlens target import` **必须**支持 `--from-plan <path>` 模式：经 `ImportPlan.load(path)`（`yaml.safe_load` → `model_validate`，因 JSON⊂YAML 故同时容 YAML 与 JSON）加载一个序列化的 `ImportPlan`（`mcp-target-import-propose` 工具产出经 client 序列化的文件，或 dry-run 持久化产物 `ImportPlan.save` 写出的 YAML），**跳过 source 解析与 probe 探测**，直接 `assemble_save_entries` → `save_targets_config` 落盘。该模式存在的意义是：让远程经 MCP propose 的计划在本地**逐字、确定性**落地（不重跑探测以免 probe 结果漂移）。

为支持 `--from-plan`，inventory 位置参数从必填变为可选（`Argument(None)`），并加约束：**恰好** inventory（`ref`）与 `--from-plan` 二选一——皆缺 / 皆给均为参数错误 → exit 2（与既有 `--source` 一样走裸 str 手动校验 + `typer.Exit(2)`，**不**走 Click `UsageError`→exit 3，对齐既有 exit-2-not-3 纪律）。`--source` / `--concurrency` 是纯 parse/probe 期参数，`--from-plan` 既跳过 parse 又跳过 probe，故二者与 `--from-plan` 同传 = 静默无效 → 也 exit 2（不留沉默 no-op）；`--json`（渲染加载 plan 的 JSON）、`--include-unreachable`、`--yes`/`--dry-run` 仍生效。

`--from-plan` 沿用既有写门语义：
- `--dry-run`，或缺 `--yes`（且非 `--dry-run`）→ 渲染加载 plan 的 `render_diff` 预览、**不写盘**、exit 0（`--from-plan --dry-run` 是合法预览，与既有 dry-run 语义一致——**不**与 `--from-plan` 互斥）。
- `--yes` → 落盘。
- `--dry-run` 与 `--yes` 同传仍 exit 2（复用既有互斥）。
- 落盘前 **EUID==0 → exit 1**（拒 root，复用既有守卫）。
- `path` 不可读 / 内容非法 / 不符 `ImportPlan` schema → 均视为参数错误 exit 2（结构化报错，**禁止**裸 traceback、**禁止**静默成功）；其中 `version` 为非 `"1"` 值即属此类，而**缺** `version` 键的 A 旧 `.save` 产物经 `version` 字段默认值加载为 v1（显式向后兼容，不报错）。
- `--include-unreachable` 对 `--from-plan` **同样生效**：加载 plan 里 `failed_probe` 桶在该标志下以 `enabled=False` 登记（与 ref 模式逐字段同形）；不带该标志时 `failed_probe` 不落盘。
- 落盘仍经 `save_targets_config` 的原子 / 幂等 upsert / `${VAR}` 保全（`PendingAdd.password_env` / `passphrase_env` 透传），name 已存在者幂等跳过。

**`ImportPlan.load` 是信任边界，落盘前必须重申 promotion 不变量**：`--from-plan` 跳过 A 的 source→`promote_candidate`，直接 `model_validate` 外部文件，绕过 promotion 强制、而 `_entry_to_dict` 落盘时不再复核的保证。故 `.load`（或 bucket validator）**必须**对**每个会进入 `save_targets_config` 的 entry**（`to_add` 恒含；`failed_probe` 当 `--include-unreachable` 生效时含——它也经 `assemble_save_entries` 落盘）重申：
- `password` / `passphrase` 恒为 None（凭据只经 `*_env` 引用）；
- `password_env` / `passphrase_env`（非 None 时）匹配**裸 env 名** pattern `^[A-Z_][A-Z0-9_]*$`（`CandidateTarget` 用的同一 pattern；**不是** `${VAR}` 形——`${...}` 仅由 `_entry_to_dict` 落盘时合成）；
- `host` / `user` / `key_path` 不含控制 / 双向覆盖字符——**仅对 `SSHEntry`**（`LocalEntry` 无此三字段，条件应用、mirror `promote_candidate` 的 SSH 分支），复用导出的 `contains_unsafe_display_chars`（类别集 `{Cc,Cf,Cs,Zl,Zp}`，比 display-only 的 `_strip_control_chars` 多 `Cs` 代理）、**非** `_strip_control_chars`；
- `key_path`（`SSHEntry`）不含 `${` 占位——`key_path` **不在** `_PLACEHOLDER_ALLOWED_FIELDS`（仅 `password`/`passphrase`），落盘逐字写，含 `${VAR}` 会毒化后续每次 `load_targets_config(expand_env=True)`（`env_placeholder_not_allowed_here`）→ 持久化共享配置 DoS；source 层 `resolve_key_path` 拒此，`--from-plan` 跳过 promotion 故在此重申；
- `to_add` 项 `enabled is True`（`failed_probe` 经 `assemble_save_entries` 强制 `enabled=False`，故**不**对其要求 True）。

任一违反 → 加载失败 exit 2，不落盘。`skipped`（`list[str]`）/ `invalid_candidate` 桶**不**经 `assemble_save_entries` 投影、永不落盘，故**有意豁免**该校验（对其校验属过度防御，且会误拒合法 `invalid_candidate` plan）。**真实可达向量（非臆想）**：篡改/手改/跨版本畸形 plan 经 `_entry_to_dict` 可写出 ① `enabled=False` 的 disabled target（`_entry_to_dict` honor `enabled is False`）；② 畸形 `*_env`（`_entry_to_dict` 盲目包成 `${非法}` 占位，落盘不可展开 / 注入）；③ 控制字符 `host`/`user`（落盘未净化，spoof 后续审计）。注：`entry.password` 明文**不是**可达向量——`_entry_to_dict` 只从 `*_env` 参数取 password、从不读 `entry.password`；故 `password is None` 检查是契约完整性 / defense-in-depth，不堵明文泄露。这是信任边界处的输入校验（安全关键），不可省。

**`--from-plan` 走独立写分支**：既有 ref 模式写尾含 candidates-failed 启发式（`failed_probe and not include_unreachable` 或 `invalid_candidate` → exit 1）+ 无条件 `render_diff`。`--from-plan` 退出码语义不同（`to_add` 空 → exit 0，不因 plan 自带 `failed_probe` 而 exit 1）、`--yes` happy path 不显 diff，故**必须**在到达共享尾之前用专属分支决定 exit 码与渲染，不穿过 candidates_failed 块。

**文件来源信任**：`--from-plan --yes` 的 happy path **不渲染** `render_diff`（预览只在 `--dry-run`/无 `--yes` 分支）。结合上面的 `.load` 不变量校验（拦明文密钥/disabled/坏占位），`--from-plan --yes` 定性为**信任文件作者身份**（如 `target add --yes` 信任命令行参数）；运维若需审计未预期主机应先 `--from-plan --dry-run` 预览（预览串经 `_strip_control_chars` 剥离、不可伪造）。

#### 场景:--from-plan + --yes 落盘
- **当** `target import --from-plan plan.yaml --yes`，`plan.yaml` 含探活成功的 `to_add` 候选
- **那么** 跳过 source/probe，经 `save_targets_config` 写出这些条目（`enabled=True`、凭据 `${VAR}` 占位保全），退出码与既有 import 落盘契约一致

#### 场景:--from-plan 缺 --yes 或 --dry-run 预览不写盘 exit 0
- **当** `target import --from-plan plan.yaml`（无 `--yes`、无 `--dry-run`），或 `target import --from-plan plan.yaml --dry-run`
- **那么** 渲染加载 plan 的 `render_diff` 预览、不写 `targets.yaml`、exit 0

#### 场景:加载兼容 YAML 与 JSON 两种格式
- **当** `--from-plan` 分别指向一个 YAML 文件（`.save` 产物）与一个 JSON 文件（client `model_dump_json` 产物），内容等价
- **那么** 两者经 `ImportPlan.load` 均加载成功且还原为逐字段等价的 `ImportPlan`

#### 场景:inventory 与 --from-plan 皆给或皆缺 exit 2
- **当** `target import some-inventory.yml --from-plan plan.yaml`（皆给），或 `target import`（皆缺）
- **那么** 作为参数错误 exit 2，不解析任何来源、不写盘

#### 场景:--from-plan 文件不可读或非法 exit 2
- **当** `--from-plan` 指向不存在 / 非法 / 不符 `ImportPlan` schema（`version` 为非 `"1"` 值）的文件
- **那么** 结构化报错 exit 2，不裸传 traceback、不写盘

#### 场景:缺 version 的旧 plan 加载为 v1
- **当** `--from-plan` 指向一个 A 旧 `.save` 写出的、不含 `version` 键的 plan 文件
- **那么** 经 `version` 字段默认值加载为 v1、正常处理（向后兼容，非报错）

#### 场景:--from-plan 与 --source/--concurrency 同传 exit 2
- **当** `target import --from-plan plan.yaml --source yaml`，或 `... --concurrency 50`
- **那么** 作为参数错误 exit 2（纯 probe/parse 期参数在 `--from-plan` 下无意义，不静默忽略），不写盘
- **注** `--concurrency` 默认值须改为 sentinel `None`（ref 模式再派生 10），否则无法区分「显式传 `--concurrency 10`」与默认值，该 exit-2 规则不可实现

#### 场景:--from-plan 加载畸形 plan（disabled/坏 env 名/控制字符 host/key_path ${占位}/内联明文凭据）被拒 exit 2
- **当** `--from-plan` 指向一个落盘向 entry（`to_add`，或 `--include-unreachable` 下的 `failed_probe`）含 `enabled=False`（to_add）、或 `password_env` 非裸 env 名 `^[A-Z_][A-Z0-9_]*$`、或 `host`/`user`/`key_path` 含控制 / 双向覆盖字符、或 `key_path` 含 `${` 占位、或内联明文 `password`/`passphrase` 的 plan 文件
- **那么** `ImportPlan.load` 重申 promotion 不变量后加载失败 exit 2，**不**把 disabled `to_add` / 不可展开 `${非法}` 占位 / 控制字符 host / `${VAR}` key_path / 明文凭据写进 `targets.yaml`

#### 场景:--from-plan EUID==0 落盘前 exit 1
- **当** 以 EUID==0 运行 `target import --from-plan plan.yaml --yes`
- **那么** 落盘前 exit 1（拒 root），不写 `targets.yaml`

#### 场景:--from-plan + --include-unreachable 登记 failed_probe
- **当** `target import --from-plan plan.yaml --yes --include-unreachable`，plan 含 `failed_probe` 候选
- **那么** 这些候选以 `enabled=False` 登记，与 ref 模式 `--include-unreachable` 输出逐字段同形

#### 场景:--from-plan 的 to_add 为空时落盘语义明确
- **当** `target import --from-plan plan.yaml --yes`，plan 的 `to_add` 为空（如全 `skipped`，或全 `failed_probe` 且未带 `--include-unreachable`）
- **那么** 无条目写出、`targets.yaml` 不变、exit 0（「无可落盘项」不是失败；区别于 ref 模式「探活/提升全失败」的 exit 1——`--from-plan` 不重跑探测，不复用 candidates-failed 启发式）
