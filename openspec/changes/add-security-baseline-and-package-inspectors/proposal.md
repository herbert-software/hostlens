## 为什么

M6 inspector 库已铺到 53 个、核心 OS/服务域覆盖（见 `TODO.md` §M6 覆盖矩阵），但矩阵里仍有两个**完全空白（0 inspector）**的 OS/Linux 故障域：**安全基线**（failed logins / sudo history / world-writable 目录）与**包管理**（待升级包 / 安全补丁 / held-back 包）。这两个域是真实运维巡检的高频关注点（「有没有人爆破登录」「有没有挂着的安全补丁没打」），且二者机制完全一致——**纯 OS shell 即时快照**，零外部服务依赖、零 secret，正是 `add-os-shell-inspectors-wave1`（已归档）证明过的 **Mode A 纯 YAML** 最适配的批量铺量场景。

本提案是 **os-shell wave-2**：把 security 与 pkg 两个空白域各铺到 3 个 inspector，达到 §M6 退出条件「每域 ≥3 + 各有 snapshot + replay fixture」。JVM/Go 运行时探测（需运行时进程在场）与 net 域 tls.chain（扩既有 net 域）机制不同，**不在本提案**、各自独立 proposal。

> **避免与既有 inspector 重叠**：security 域**不**新增「白名单外监听端口」探针——既有 `net.listening_ports`（net 域）已用 `for_each` + DSL `p.port not in allowed_ports` 做了该检测；本提案 security 域的第三个探针取**正交**的 `security.world_writable_dirs`（提权向量：缺 sticky bit 的全局可写目录），不与 `net.listening_ports` 重复。

## 变更内容

- 在现有 53 个 builtin inspector 基线上，**新增 6 个纯 shell builtin inspector**，覆盖 §M6 矩阵两个空白域，命名启用 `security.*` / `pkg.*` 新命名空间（文件落 `builtin/security/` / `builtin/pkg/`）：

  | 域 | 新增 inspector（registry name） | 采集手法（preflight-gated binary） |
  |---|---|---|
  | 安全基线 | `security.failed_logins` | `TZ=UTC journalctl _SYSTEMD_UNIT=ssh.service --since "{{ window_start }}"` → 时窗内失败登录计数 + top 源（明细进 evidence） |
  | 安全基线 | `security.sudo_history` | `TZ=UTC journalctl _COMM=sudo --since "{{ window_start }}"` → 时窗内 sudo 调用计数 + 失败 sudo |
  | 安全基线 | `security.world_writable_dirs` | `find <syspaths> -xdev -type d -perm -0002 -not -perm -1000` → 缺 sticky bit 的全局可写目录（提权向量） |
  | 包管理 | `pkg.pending_updates` | collector 内 `command -v apt-get`/`dnf` 自适应 → 可升级包计数（精确 `grep -c '^Inst'`） |
  | 包管理 | `pkg.security_patches` | apt（security 源过滤）/`dnf updateinfo -q list security` → 待打安全补丁计数 |
  | 包管理 | `pkg.held_back` | `apt-mark showhold`/dnf versionlock → 被 hold/pin 挡住升级的包列表 |

- 每个 inspector 是**纯 YAML manifest**，严格遵守《Inspector 作者契约》：全解析压进 collector / 单 `for_each` 或聚合标量 / 注入安全（参数若进 shell 必经 `{{ x | sh }}`，loader 加载期强制；参数若只进 DSL 成员测试则用 array 类型不进 shell）/ collector 对数据源不可达**fail-loud**（主命令 `|| { echo …>&2; exit 1; }`，**禁** `|| true` 掩盖）。
- 每个 inspector 用 fixture 录制器对真实 Linux host 录 `ReplayTarget` 兼容 fixture，**且至少含一份触发预期 finding 的异常场景 fixture + 一份数据源不可达 fixture**（不止 happy-path），配 snapshot 测试，CI 全程离线回放。
- **不改任何对外契约**——纯铺量在现有 schema 字段集（4 种 parse format、现有 capability enum、`sampling_window` 等字段，无新增）内完成。

### 完整 manifest 示例（`security.failed_logins`）

```yaml
name: security.failed_logins
version: 1.0.0
description: >
  Count failed SSH/PAM login attempts within the sampling window from the
  systemd journal. Linux + systemd only. A burst signals brute-force. The
  per-source IP breakdown goes to evidence; the message stays count-only
  (no raw IP) so it is safe to forward into LLM context / notifications.
tags: [security, linux]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [journalctl, grep]
privilege: none

parameters:
  type: object               # parameters 是一份完整 JSON Schema 文档 (顶层 type:object
  properties:                # + properties + additionalProperties:false), 不是扁平 map;
    threshold:               # 否则 pattern/type 落错层级、对参数完全不生效。
      type: integer
      default: 20
  additionalProperties: false

collect:
  sampling_window:
    duration_seconds: 3600
  # FAIL-LOUD (作者契约 rule 8): journalctl 缺失/不可读必须非零退出 (→ status=exception),
  # 而非吐 {"failed":0} 被祝福为「无失败登录」(security 假阴性陷阱)。先把输出存进
  # raw= 变量并在该命令上 || exit 1 (而非裸管道 journalctl | grep, 否则 journalctl
  # 失败会被下游 grep 退出码吞掉 → 假 0)。
  # 跨发行版 unit 名: Debian/Ubuntu=ssh.service, RHEL/Fedora/SUSE/Arch=sshd.service;
  # journalctl 同字段多值为 OR, 两个都列才不会在 RHEL 家族上 journalctl 成功返 0 行
  # → 漏掉全部失败登录 (数据源可达型假阴, fail-loud 不触发)。
  # TZ=UTC: runner 注入的 window_start 是无 TZ 后缀的 UTC 墙钟串; journalctl --since
  # 对裸时间戳按系统本地时区解释, 故须 TZ=UTC 把时窗对齐到注入的 UTC 值。
  # grep -c 的 || true 只豁免「零匹配」(grep 无匹配返 1, 非错误); 此处 grep 跑在变量
  # 上、journalctl 失败已被上一行 || exit 1 拦住, 故安全。
  command: |
    raw=$(TZ=UTC journalctl _SYSTEMD_UNIT=ssh.service _SYSTEMD_UNIT=sshd.service \
      --since "{{ window_start }}" --no-pager 2>/dev/null) \
      || { echo "journalctl unavailable" >&2; exit 1; }
    count=$(printf '%s\n' "$raw" | grep -c "Failed password" || true)
    printf '{"failed":%d}' "$count"
  timeout_seconds: 30

parse:
  format: json

output_schema:
  type: object
  properties:
    failed: { type: integer }
  required: [failed]
  additionalProperties: false

findings:
  # message 不含 {failed} 计数 (str.format 不能在 {} 调函数, 且计数留 when);
  # 也不裸打源 IP —— top 源明细由 collector 写进 evidence, 交报告/adapter 脱敏层。
  - when: "failed > threshold"
    severity: warning
    message: "时窗内失败登录数超过阈值（疑似爆破，明细见 evidence）"
```

> **参数注入两条路径（《作者契约》）**：(a) 参数**进 shell** 时必须经 `{{ x | sh }}`（loader 对 `string` 类型参数无条件强制 `| sh` filter，`pattern` 不豁免；`| sh` 对纯数字串是 no-op）；(b) 参数**只进 DSL 成员测试**（如 `net.listening_ports` 的 `allowed_ports`）时用 **array 类型**、不插值进 command、无需 `| sh`。本示例的 `threshold` 走 DSL 比较不进 shell。security 域不重复 `net.listening_ports` 的端口白名单检测。

## 功能 (Capabilities)

### 新增功能
- 无新 capability。本提案的 inspector 属 OS/Linux 纯 shell 快照，归既有 `os-shell-inspector-suite` 套件契约管辖。

### 修改功能
- `os-shell-inspector-suite`: **追加** security + pkg 的按域覆盖需求 —— 新增规范性需求规定本 cohort 必须覆盖 security 基线与包管理两域、各 ≥3 inspector，并复用套件既有质量门（纯 YAML 遵守《作者契约》+ ReplayTarget fixture + 含异常/不可达场景 snapshot + 矩阵勾选 + 零新基础设施）。**追加式冻结 cohort**：不 MODIFY wave-1 既有需求，只新增本 cohort 需求（与 service-inspector-suite cohort 冻结纪律一致）。

## 影响

- **新增代码**：新增 `src/hostlens/inspectors/builtin/security/`（3 个 `.yaml`）+ `builtin/pkg/`（3 个 `.yaml`）。
- **新增测试**：`tests/inspectors/` 下 6 个 inspector 的 snapshot 测试 + fixture（`fixtures/security/`、`fixtures/pkg/`）；扩 `test_builtin_inspectors.py` 的 loader 注册断言 + **冻结 cohort 计数 == 6** guard（仿既有 wave count-frozen）；扩 `test_builtin_capability_gate.py` 的 capability-gate 断言。
- **文档**：勾选 `TODO.md` §M6 矩阵的 security / pkg 单元格。
- **对外契约影响**：
  - **Inspector manifest schema**：不变（不增删字段、不扩 parse format、不扩 capability enum）。
  - **Inspector registry（对 Agent 可见）**：扩 6 个 builtin inspector；Agent 仍只见 `list_inspectors` / `run_inspector` 两个工具，**工具数组不变**。
  - **不涉及** Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令变更。
- **依赖**：不新增 Python 依赖。各 inspector 的 `requires_binaries`（`journalctl`/`grep`/`find`/`apt-mark` 等真实工具）由 preflight 探测，缺失 → `requires_unmet` skip。**pkg inspector 的 `requires_binaries` 不硬列 apt-get+dnf**（否则两类机都过不了 preflight）——只列必然在的 `[awk]`/`[grep]`，包管理器存在性在 collector 内 `command -v` 判 + fail-loud。

## 非目标（Non-Goals）

- **不重复 `net.listening_ports`** —— 白名单外监听端口检测已由既有 net 域 inspector 用 `for_each` + DSL `not in` 完成；security 域不再做端口白名单探针。
- **不做 JVM/Go 运行时 inspector**（独立 proposal `add-runtime-inspectors`）；**不做 tls.chain_validity**（独立 proposal `add-tls-chain-validity-inspector`）。
- **不引入入侵检测 / 实时告警 / 日志全文搜索 / SIEM** —— 只做「即时快照计数 + 阈值判定」的巡检型 inspector；security inspector 只统计时窗内计数与 top 源，不做行为分析。
- **不改 manifest schema / 不扩 capability enum / 不加 parse format**。
- **不为单个 inspector 立行为 spec**（spike D-9）—— 具体 inspector 的 input/output 由 snapshot 验收，capability 只规定套件层覆盖方法论与质量门。

## Failure Modes

1. **目标缺二进制**（无 `journalctl`（非 systemd 机）/ 无 `apt-get` 且无 `dnf`）→ security 走 `requires_binaries` preflight → `requires_unmet`；pkg 走 collector 内 `command -v` 双探测、二者都无 → `exit 1` → `status=exception`（见 spec pkg-fail-loud 场景）。
2. **非 Linux / 无 auth 日志权限**（容器内无 journald、journal 不可读）→ 主命令 fail-loud `|| { echo>&2; exit 1; }` → `status=exception`（诚实），**绝不**伪造 `status=ok failed=0` 把「读不到日志」误判成「无失败登录」。这是 security inspector 最危险的假阴性面，由 spec「fail-loud 不假阴」场景 + 强制 unreachable fixture 锁死。**禁用 `|| true` 掩盖主命令失败**。
3. **`window_start` 时区错位** → collector 用 `TZ=UTC journalctl --since`（journalctl 对裸时间戳按本地时区解释，runner 注入的是 UTC 墙钟串）→ 时窗对齐 UTC，不漏算/多算 TZ offset 的事件。
4. **pkg 计数把 dry-run 噪声当升级**（`apt-get -s upgrade` 输出混杂）→ collector 用精确 `grep -c '^Inst'` 而非 `wc -l`，snapshot fixture 含真实多包升级样本验证计数准确。
5. **fixture 与 runner 命令漂移** → 强制 fixture 录制器（驱动真 runner 录制、字节级匹配、冻结时窗），禁手写。

## Operational Limits

- **并发预算**：不引入新并发；单 inspector `collect.timeout_seconds` 默认 ≤30s（`world_writable_dirs` 的 `find` 限定在 `/etc /usr /var` 等系统路径 + `-xdev`，不扫全盘）。
- **内存预算**：collector 输出为小 JSON（失败登录计数 / 升级包名列表 / 可写目录列表，典型 <50KB）；日志扫描类按 D-6.1 pipe-safe 把**时窗内**输出存进 `raw=$()` 变量再计数（pipe-safe 必需，非流式），规模由 `sampling_window` 时窗限定（典型时窗内匹配行远小于全量 journal）；shell 变量不受 ARG_MAX 约束（赋值与 `printf` builtin 不走 exec），唯一残余是「单时窗内异常海量失败登录」极端场景的变量驻留，由时窗时长可调兜底。
- **超时设置**：日志扫描类用 `sampling_window` 注入的 `window_start` 限时窗；`find` 类用 `-xdev` + 限定路径 + `timeout_seconds` 兜底。

## Security & Secrets

- **不引入新密钥**：security/pkg inspector 全部读本机系统状态（日志/权限/包数据库），无凭据、无 `HOSTLENS_*` secret 注入，不走 service-inspector 的 native-env remap 路径。
- **脱敏**：security inspector 输出可能含**源 IP**（failed_logins top 攻击源）与**用户名**（sudo history）—— finding **message 不裸打**这些（用计数 + 「明细见 evidence」），敏感明细放 `evidence` 字段，交报告渲染层 + Agent/MCP adapter 的 `scrub_exception_message` / `TargetSummary` 脱敏。
- **攻击面**：不开网络端口、不写远端状态、纯只读采集；参数进 shell 必经 `{{ x | sh }}`（loader 强制）+ 不裸拼。
- **权限**：`privilege: none`，不要求 sudo。journal/auth 不可读的机器上以非 root 跑 → fail-loud `status=exception`（Failure Mode 2），不静默假阴。

## Cost / Quota Impact

- **零 LLM token 消耗**：inspector 是采集层，不调 LLM。新增 6 个 inspector 仅在被 `run_inspector` 选中时执行 shell 采集。
- **对 Agent 上下文的影响**：`list_inspectors` 返回列表 +6 项（registry 元数据，非大体积）；prompt caching 不受影响（inspector schema 列表本就缓存）。
- **API 调用频次**：inspector 本身不产生 Anthropic API 调用。

## Demo Path

无需 SSH / 无需付费 API 的 5 分钟本地复现（全程离线 replay）：

```bash
pytest tests/inspectors/ -k "security or pkg" -v
# 核心验证点:
#   - 6 个新 inspector 全部 loader 加载、过 capability-gate、冻结 cohort 计数 == 6
#   - security.failed_logins: 多条失败的 journal fixture（过阈值）→ finding;
#     journal 不可读 fixture → status=exception（不假阴, 关键防护）
#   - security.world_writable_dirs: 含缺 sticky bit 全局可写目录的 fixture → finding
#   - pkg.pending_updates: apt 多包 fixture + dnf 多包 fixture（覆盖双分支）+ 无包管理器 fixture（exception）
hostlens inspectors list | grep -E "security\.|pkg\."   # 列出 6 个新 inspector
```

核心验证点：每个 inspector 至少一份**触发 finding 的异常场景 fixture** + security/pkg 各一份**数据源不可达 / 无包管理器 fixture**（证明 DSL 比较生效 + 假阴性防护，非只有 happy-path）。
