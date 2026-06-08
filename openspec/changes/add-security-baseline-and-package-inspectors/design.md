## 上下文

`add-os-shell-inspectors-wave1`（已归档）证明了 OS/Linux 纯 shell inspector 的批量铺量模式（Mode A 纯 YAML + 录制器 fixture + snapshot），并建立了 `os-shell-inspector-suite` 套件契约（域覆盖 + 质量门 + 零新基础设施）。当前 53 个 builtin inspector 里 security 与 pkg 两域仍为 0。本提案按同一模式补这两个空白域，复用既有 manifest schema、Finding DSL、录制器、capability-gate 测试，**不引入任何新基础设施**。

约束（《作者契约》+ schema 实测）：纯 YAML（不写 `hook.py`）；4 种 parse format 内（用 `json`）；**参数进 shell 必经 `{{ x | sh }}`，loader 对 `string` 类型参数无条件强制此 filter（`pattern` 不豁免）**；DSL 禁 comprehension/lambda，但 **`for_each` 迭代 + `not in` 成员测试是允许的**（既有 `net.listening_ports` 即用 `for_each: "results as p"` + `when: "... p.port not in allowed_ports"`）；collector 对数据源不可达必须 fail-loud（主命令 `|| { echo>&2; exit 1; }`，禁 `|| true`）。

## 目标 / 非目标

**目标**：security（failed_logins / sudo_history / world_writable_dirs）+ pkg（pending_updates / security_patches / held_back）各 3 inspector，纯 YAML，含 finding-trigger + unreachable fixture，达 §M6 退出条件。

**非目标**：重复 `net.listening_ports` 的端口白名单检测、JVM/Go 运行时、tls.chain、SIEM/入侵检测、改 schema/capability/parse-format。

## 决策

### D-1：apt-or-dnf 用单 manifest 内分支探测；`requires_binaries` 只放必然在的真实工具，不放包管理器、不放 `sh`

**选择**：pkg inspector 的 collector 用 `if command -v apt-get >/dev/null; then <apt 分支>; elif command -v dnf >/dev/null; then <dnf 分支>; else echo "no package manager">&2; exit 1; fi`，输出统一 JSON schema（如 `{"pending": <int>}`）。`requires_binaries` **只列必然在的工具**（如 `awk`/`grep`），**不列** apt-get/dnf（硬列任一会让另一类机 preflight `requires_unmet`，硬列两者会让两类机都 `requires_unmet`），**也不列 `sh`**（`sh` 不是被 `command -v` 探测的语义工具，wave-1 惯例放真实工具）。包管理器存在性在 collector 内判 + fail-loud。

**理由**：Debian 系与 RHEL 系是同一故障域两种实现，语义等价，合并成一个 inspector 对 Agent 更友好。代价是 collector 分支，由 snapshot fixture（apt 样本 + dnf 样本各一 + 无包管理器一份）覆盖三路。

### D-2：白名单/集合差用 array 参数 + DSL `for_each` + `not in`（不压进 collector），与 `net.listening_ports` 同范式

**选择（修订——原 D-2「做差压进 collector」前提错误）**：经核 `net.listening_ports`，DSL **支持** `for_each: "results as p"` 迭代 + `when: "p.x not in <array_param>"` 成员测试，集合差是在 **DSL** 做的、不是 collector；白名单参数用 **array 类型**（`type: array, items: {type: integer}`）、**不插值进 command**、**无需 `| sh``**。本提案 security 域**不**新增端口白名单探针（已由 `net.listening_ports` 覆盖，见非目标），故本提案无「集合差」需求；如未来需要，**必须**走此 array-param + DSL `for_each` 范式，**禁止**把过滤逻辑写成 DSL comprehension（DSL 硬拒 `ListComp`），也无需 collector 侧做差。

**理由**：原 D-2 基于「DSL 不能做集合差」的错误假设，导致设计绕路且 proposal 示例写错（comprehension + collector-diff）。修订后对齐既有 proven 范式，更简单、更一致。

### D-3：日志扫描类用 `TZ=UTC journalctl --since "{{ window_start }}"` 对齐 UTC 时窗

**选择**：`security.failed_logins` / `security.sudo_history` 声明 `collect.sampling_window.duration_seconds`，collector 用 **`TZ=UTC journalctl --since "{{ window_start }}"`**。runner 注入的 `window_start` 是**无 TZ 后缀的 UTC 墙钟串**（`YYYY-MM-DD HH:MM:SS`），而 `journalctl --since` 对裸时间戳按**系统本地时区**解释（systemd.time(7)）——非 UTC 机上不加 `TZ=UTC` 会令时窗偏移 = TZ offset（漏算/多算事件）。`TZ=UTC` 把 journalctl 的时间解析对齐到注入值。

**理由**：`sampling_window` 是现有 schema 字段（`log.tail.error_burst` 已在用），零新基础设施；TZ 对齐是正确性必需，归档前必须在带 journald 的录制机上验证时窗边界事件计数。

### D-4：security evidence 脱敏边界——message 不裸打源 IP / 用户名，明细进 evidence 交消费侧脱敏

**选择**：security inspector 的 finding `message` 用计数 + 「明细见 evidence」措辞，把源 IP / 用户名等敏感明细放 `evidence` 字段，经报告渲染层 + Agent/MCP adapter 既有脱敏（`scrub_exception_message` / `TargetSummary` 的 IP/identity 正则）处理。

**理由**：security 输出本质含敏感数据（攻击源 IP、sudo 用户名）。message 是最易被原样转发进 LLM context / 通知渠道的字段，故不裸打；evidence 是结构化明细，由统一脱敏层兜底。

### D-5：每个 inspector 至少一份「触发 finding」+ 一份「数据源不可达 / 无包管理器」fixture

**选择**：除 happy-path 外强制录：①**finding-trigger**（failed_logins 给多条失败 journal → 过阈值；world_writable_dirs 给缺 sticky bit 的全局可写目录 → 检出；pkg 给多包待升级 → 计数过阈值）；②**unreachable**（security: journal 不可读 → `status=exception`；pkg: 无 apt-get 且无 dnf → `status=exception`）。两类都进 snapshot，由套件 spec「fail-loud 不假阴」与「pkg 无包管理器非 ok」场景强制。

**理由**：只录 happy-path 的 inspector 是 vacuous——证明不了 DSL 比较生效、也证明不了假阴性防护。这是套件质量门核心。

### D-6：collector fail-loud 的 shell 正确性规则（所有 inspector 强制，防 fail-loud 被击穿）

fail-loud 不是「主命令后面加 `|| exit 1`」就完事——多个 shell 陷阱会让 fail-loud 静默失效或误报。本提案所有 collector **必须**遵守：

- **D-6.1 禁裸管道吞退出码**：`<主命令> | grep -c X` 的管道退出码只反映末段 `grep`，主命令失败被吞 → 假 0 计数（pkg 假阴的根源）。**必须**先把主命令输出存进变量并在该命令上判退出码：`raw=$(<主命令>) || { echo>&2; exit 1; }`，再 `printf '%s' "$raw" | grep -c X`；或在 collector 首行 `set -o pipefail` 并显式检查管道退出码。`security.failed_logins` / `security.sudo_history` / 三个 pkg inspector **全部**适用。
- **D-6.2 `find` 退出码 ≠ 致命错误（但「非零 + 空 stdout」仍 fail-loud）**：GNU/BSD `find` 遇不可访问子树会以**非零退出码**结束（这是**正常**的部分权限拒绝，不是 collector 崩）。`world_writable_dirs` **禁止**用裸 `find ... 2>/dev/null || exit 1`（会把正常的部分权限拒绝误报为 `status=exception` 假阳）。**精确判据**（消解「忽略退出码」与「全不可达须异常」的张力）：`out=$(find ... 2>/dev/null); rc=$?; [ $rc -eq 0 ] || [ -n "$out" ] || { echo "find produced no output and failed" >&2; exit 1; }` —— 即 **`rc != 0` 且 stdout 为空才 `exit 1`**（区分「部分子树拒绝：rc≠0 但有 stdout → ok」与「scan 根全不可达 / find 崩：rc≠0 且 stdout 空 → exception」）；`rc == 0`（含合法零结果 `{"results":[]}`）正常 ok。须录三份 fixture：①含可写目录（finding-trigger）②部分子树不可读但有数据（rc≠0 + 非空 → ok）③全不可达（rc≠0 + 空 → exception）。
- **D-6.3 `dnf check-update` 退出码 100 是「有更新」不是错误**：**仅** `dnf check-update` 有可升级包时返回 **exit 100**、无更新返回 0、真错误返其它。`pkg.pending_updates` 的 dnf 分支用 `dnf check-update`，其 fail-loud **必须豁免 100**：`dnf check-update ...; rc=$?; [ $rc -eq 0 ] || [ $rc -eq 100 ] || { echo>&2; exit 1; }`。**注意**：`dnf updateinfo`（`pkg.security_patches` 用）与 `dnf versionlock`（`pkg.held_back` 用）**不**返回 100、正常返 0——**禁止**把 100-豁免套到这两个子命令（会吞掉真错误）；它们走标准 `|| exit 1`。逐子命令的退出码语义须各自核对，不可机械套用。
- **D-6.4 pkg 分支内主命令失败也要 fail-loud（不只 else 分支）**：apt/dnf **存在但其命令失败**（dpkg 锁 / 网络 / 元数据损坏）时也必须 `exit 1` → exception，不能让 D-6.1 的 raw-capture 之外再留裸管道。`pkg.held_back` 的 `apt-mark showhold` **空输出**有歧义（无 held 包 vs 命令失败）——必须捕获退出码判定，不靠「空输出」推断成功。
- **D-6.5 跨发行版 unit/标识名**：`security.failed_logins` 的 `_SYSTEMD_UNIT` 必须同时列 `ssh.service`（Debian/Ubuntu）+ `sshd.service`（RHEL/Fedora/SUSE/Arch）（journalctl 同字段多值为 OR），否则在 RHEL 家族上 journalctl **成功返 0 行** → 假 `status=ok failed=0` 漏掉全部失败登录（数据源可达型假阴，fail-loud 不触发，最隐蔽）。`security.sudo_history` 的 `_COMM=sudo` 同样录 fixture 验证跨发行版稳定性。

**理由**：B1/B2/B3（unit 名 / 管道 / find 退出码）与 dnf-100、held_back 空输出都是「fail-loud 写了但被 shell 语义击穿」——而假阴防护是这批 security/pkg inspector 的核心价值。D-6 把这些正确性规则集中钉死，每条配「数据源可达但语义错配 / 命令失败」的 fixture（区别于「数据源不可达」fixture），防 fixture 只录 happy-path 与不可达两态、漏掉这些中间假阴面。

## 风险 / 权衡

- **[apt/dnf collector 分支输出格式漂移]** → 缓解：snapshot fixture 录真实 apt + dnf 输出各一；漂移由 CI 立即暴露。残余：小众发行版（pacman/zypper）本期不支持，collector `exit 1` → `status=exception`，不假装成功。
- **[security 日志格式跨发行版差异]** → 缓解：优先 `journalctl`（结构化、跨发行版稳定）；非 systemd 机无 journalctl → `requires_unmet`。auth.log 路径 fallback 留后续（本期 systemd-only，文档式声明于 description）。
- **[`world_writable_dirs` 的 `find` 在大目录树慢 / 权限噪声]** → 缓解：`-xdev` 不跨挂载 + 限定 `/etc /usr /var /tmp /opt` 等系统路径（非 `/`）+ `find ... 2>/dev/null` 吞权限拒绝噪声但主命令仍 fail-loud（`find` 自身崩才 exit 1）+ `timeout_seconds` 兜底。
- **[非 root 读不到 journal 被当无失败登录]** → 由 Failure Mode 2 / spec「fail-loud 不假阴」场景 + unreachable fixture 锁死。

## 迁移计划

- 纯增量：新增 6 个 manifest + fixture + snapshot 测试 + 冻结 cohort 计数 guard + capability-gate 断言扩容。无 schema 迁移、无破坏性契约。
- 回滚：删 `builtin/security/`、`builtin/pkg/` 及对应测试即可；无持久化状态。
- 部署：随包发布，`hostlens inspectors list` 自动出现 6 个新 inspector；目标缺 binary 时 `requires_unmet`/`exception` 自动 skip。

## 待解决问题

- **pkg 第 3 个 inspector 名已锁定 `pkg.held_back`**（tasks 2.3 已删除 `last_update_age` 备选）。本条保留作历史记录：曾备选 `pkg.last_update_age`，已落定 held_back，冻结清单无悬空。
- **`security.sudo_history` 的「失败 sudo」判定**：journald `_COMM=sudo` + grep `authentication failure` 跨发行版是否稳定，录 fixture 时验证；不稳定则只统计 sudo 调用计数、失败判定降为 best-effort。
- **auth.log fallback（非 systemd 机）**：本期 security 日志 inspector systemd-only（无 journalctl → requires_unmet）。是否补 auth.log 路径 fallback 留后续 proposal。
