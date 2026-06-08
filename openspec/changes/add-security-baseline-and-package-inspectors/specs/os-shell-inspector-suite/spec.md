## 新增需求

### 需求:安全基线与包管理域必须按域覆盖（os-shell 后续 wave）

本套件**必须**在 wave-1 既有基线之上、按 `TODO.md` §M6 覆盖矩阵新增覆盖以下两个此前空白（0 inspector）的 OS/Linux 故障域的纯 shell inspector：**安全基线**与**包管理**。每个域**必须**至少新增 3 个 inspector（达 §M6「每域 ≥3」退出条件）。**遵守 spike D-9**：本需求约束的是**套件层的域覆盖度**，**不**为任一具体 inspector 规定 input/output 行为契约——具体 inspector 清单（名称与采集手法）是**实现**，列在本变更的 `proposal.md` 与 `tasks.md`，由 snapshot 测试验收。

**追加式冻结 cohort**：本需求是 os-shell 套件的**追加**需求，**禁止** MODIFY wave-1 的「wave-1 必须按域覆盖」需求；二者 cohort 各自冻结、互不回溯（与 service-inspector-suite 的 cohort 冻结纪律一致）。wave-1 spec 中「中间件/服务域留 wave-2」的注记指的是**服务域**（已由独立的 `service-inspector-suite` capability 承接），与本需求的 security/pkg OS-shell 域**正交**；本需求是 os-shell 套件按 OS 故障域继续铺量的后续 cohort。

本 cohort 的 inspector **必须**仅含零外部服务依赖的 OS/Linux shell 探针（读本机日志/端口/包数据库），**禁止**纳入依赖外部服务（nginx / mysql / postgres / redis / docker / k8s）或语言运行时（JVM / Go）的 inspector。

#### 场景:cohort 清单中的 inspector 全部干净注册

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 `proposal.md`/`tasks.md` 列出的每个 security/pkg inspector（共 6 个，以本变更**归档时冻结**的清单为准；后续 wave 另立 change，不回溯改本 spec）**必须**以其声明 `name` 出现在 registry 中，且 registry `errors == []`

#### 场景:两个目标域都达 ≥3 覆盖且勾上矩阵

- **当** 评估本 cohort 是否达成域覆盖
- **那么** 安全基线域与包管理域**必须**各自至少新增 3 个 inspector，且每个落地的 inspector **必须**勾上 `TODO.md` §M6 覆盖矩阵对应单元格

#### 场景:安全日志不可达时必须 fail-loud 不假阴

- **当** security inspector（如 `security.failed_logins` / `security.sudo_history`）运行在数据源不可达的目标上（无 journald、journal 因权限不可读、非 systemd 机无 `journalctl`）
- **那么** 该 inspector **必须**以 `status=requires_unmet`（binary 缺失，preflight 拦）或 `status=exception`（数据源读取失败，collector fail-loud `|| exit 1`）呈现，**禁止**伪造 `status=ok` 把「读不到安全日志」误判为「无失败登录 / 无 sudo 活动」（security 域的关键假阴性防护）；collector **禁止**用 `|| true` 掩盖主命令失败
- **且** 本 cohort 的 snapshot 测试**必须**含至少一份「数据源不可达」fixture 断言此非假阴行为

#### 场景:pkg inspector 在采集失败时必须非 ok（无包管理器 或 命令失败）

- **当** pkg inspector（`pkg.pending_updates` / `pkg.security_patches` / `pkg.held_back`）的采集失败——**无论**是「既无 `apt-get` 又无 `dnf`」（collector 内两路 `command -v` 均失败）**还是**「包管理器存在但其主命令失败」（dpkg 锁 / 网络 / 元数据损坏，主命令非零退出）
- **那么** 该 inspector **必须**以 `status=exception`（collector fail-loud `exit 1`）呈现，**禁止**产出 `status=ok` 且计数为 0 的结果（防止「采集失败 → 误判无待升级 / 无安全补丁」的假阴）；collector **禁止**用裸管道 `<主命令> | grep -c`（管道吞主命令退出码 → 假 0），**必须** raw-capture 后判退出码或 `set -o pipefail`
- **且** 本 cohort 的 snapshot 测试**必须**含「无包管理器」与「包管理器存在但主命令失败」两类 fixture 各至少一份断言此行为

#### 场景:security 日志型 inspector 不得因数据源可达但语义错配而假阴

- **当** `security.failed_logins` / `security.sudo_history` 运行在数据源**可达**但与硬编码标识不匹配的目标上（如 RHEL/Fedora/SUSE 家族 sshd 的 systemd unit 名为 `sshd.service` 而非 Debian 的 `ssh.service`），且时窗内**确有**失败登录
- **那么** 该 inspector **禁止**因 unit 名错配而 journalctl 成功返 0 行 → 伪 `status=ok` 计数 0（数据源可达型假阴，fail-loud 不触发，最隐蔽）；collector **必须**同时匹配跨发行版的标识（如 `_SYSTEMD_UNIT=ssh.service _SYSTEMD_UNIT=sshd.service` 多值 OR）
- **且** 本 cohort 的 snapshot 测试**必须**含一份命令串级断言：捕获的 `failed_logins` 主命令同含 `_SYSTEMD_UNIT=ssh.service` 与 `_SYSTEMD_UNIT=sshd.service`（确保 RHEL 家族 sshd.service 不被漏匹配）；**journalctl OR 语义本身**因 D-7 offline 录制（fixture 录 collector 最终 JSON、不跑 journalctl）**只在命令串级锁定**，其「sshd.service 有失败记录 → 检出非 0」的计数边界正确性须在带真实 journald 的 Demo Path 上验证——offline fixture **不**声称锁 OR 执行正确性，下游检出由通用 finding-trigger fixture 证（与下方过滤器场景同构，见本变更 tasks.md 偏离登记）

#### 场景:含过滤逻辑的 pkg inspector 的过滤器正确性须命令串级锁 + 真机验证

- **当** `pkg.security_patches` 的 security 源过滤逻辑（apt 的 security 源 grep / `dnf updateinfo` 过滤）错配（正则写错 / 源名不匹配），可能令「确有补丁」假 0（与 security 日志型「语义错配」假阴同构）
- **那么** 本 cohort snapshot 测试**必须**含一份「post-filter 计数非 0 → 检出 finding」的 finding-trigger fixture，锁住**下游计数 + finding 触发链**；**过滤器 regex 本身**因 D-7 offline 录制（fixture 录 collector 最终 JSON、不跑 shell 过滤器）**只在命令串级锁定**（verbatim 捕获的命令含正确过滤 regex），其**计数边界正确性须在带真实 apt/dnf 的 Demo Path 上验证**——offline fixture **不**声称锁过滤器执行正确性（见本变更 tasks.md 偏离登记）

#### 场景:cohort 内 inspector 不得依赖外部服务或语言运行时

- **当** 评估本 cohort 某 inspector 是否合规
- **那么** 其 `requires_binaries` 与 `collect.command` **禁止**引用外部服务客户端（`nginx` / `mysql` / `redis-cli` / `psql` / `docker` 等）或语言运行时工具（`jstat` / `jcmd` / pprof）——本 cohort **仅**含读本机日志 / 文件权限 / 包数据库的零外部依赖 OS shell 探针
