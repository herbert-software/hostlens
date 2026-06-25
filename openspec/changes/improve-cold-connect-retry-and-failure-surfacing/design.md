# 设计

## 背景：契约的两态假设撞上 Tailscale 三态现实

`ssh-execution-target` 原契约把首次 connect 设计成**两态**：`asyncssh.connect(connect_timeout=10)` 一次，成功 → 复用；超时/网络错 → 立即 `ssh_connect_timeout`，判 host 宕。规范甚至**显式禁止**首次 connect 重试，理由正当：当时若把首连塞进重连循环，会让真宕的 host 空等 21s 且报错 kind 错。但生产 fleet 全走 Tailscale（`100.x` CGNAT），空闲节点的冷路径首次建连是**逐步收敛过程**（NAT 穿透 / DERP→direct / wireguard 握手），实测 cloudcone 在 2026-06-24 08:00 那次花 ~70s 才建好。这是**第三态：可达但慢**。`connect_timeout` 是单次尝试上限，对会逐步建立的路径无论设多大都是赌博；正解是**在总预算内反复尝试**——每次 connect 尝试都推进握手，直到路径通或预算耗尽。

## 决策 1：预算是构造参数、按调用路径 opt-in，不是 per-target entry 字段

谁需要重试由**调用意图**决定，不是 per-host 配置：

| 调用路径 | 构造点 | 传预算? | 理由 |
|---|---|---|---|
| 定时 fleet 巡检 | `cli/schedule.py` 调度 registry 构造 | **是**（90s） | 日推送不该因冷路径误报——本提案目标 |
| 纳管探活 `target import` / `propose_target_import` | `targets/probe.py` 调 `registry.build_one_target`（**该 helper 在 `registry.py:215`、非 `probe.py`**；它内部调 `build_registry_from_config`，故 helper 自身也加参透传） | **是**（90s） | 纳管新 Tailscale 机本就可能冷，容忍（用户选择） |
| `hostlens doctor` | `cli/doctor.py` registry 构造 | 否 | docstring 承诺 5s 响应；down host 不该阻塞 90s |
| `hostlens target test` | `cli/target.py` registry 构造 | 否 | 交互命令，快速失败 |
| 临时 `hostlens inspect` | `cli/inspect.py` registry 构造 | 否 | 交互单机巡检，快速失败 |
| `mcp serve` / `fix` | `cli/mcp.py` / `cli/fix.py` registry 构造 | 否 | 默认 None，零行为变更（核实均位置参数调用） |
| `target list` | `cli/target.py` registry 构造 | 否 | 只读 `.capabilities`、永不 `.exec`，预算 inert |
| demo | `demo/assembly.py` | 否 | replay-only target、永不 SSH-dial，预算 inert |

故预算是 `SSHTarget.__init__(cold_connect_retry_budget_seconds: float | None = None)`，**默认 None = 单次尝试 = 完全保持现状**（所有现有 caller 零行为变更），由 `build_registry_from_config(..., cold_connect_retry_budget_seconds=None)` 透传，仅上表前两路传 `_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS = 90.0`。注：`cli/schedule.py` 的 `_build_target_registry` 被 list/trigger/daemon/status **四命令共享**，故 list/status 的 registry 也带 90s 预算——但它们**永不** `target.exec`（list 读 `_next_fire_time`、status 读 `RunStore`），冷连接根本不发起，预算在那两条 **inert**，无需为零可观测差异拆分 helper（YAGNI）。

**为何不放 per-target `SSHEntry` 字段**（review 否决的初版）：①per-entry 字段会被 **所有** 读同一 entry 的 caller 继承（doctor/test/probe 无法区分），正是要避免的横向回归；②需 4 处接线（`SSHEntry` 字段 + `__repr__` 元组 + `_entry_to_dict` 往返 + `SSHTarget.TargetEntry` Protocol），漏 `_entry_to_dict` 会让 override 在 `save_targets_config` 往返时**静默丢失**。构造参数把这 4 处接线全省掉，且天然按路径分流。per-target 调优是 YAGNI——真有单机需求再加 entry 字段。`# ponytail: 硬默认 90s，需 per-host 调优时再提 SSHEntry 字段或 Settings.ssh`。

## 决策 2：只重试 `ssh_connect_timeout`，每次尝试 cap 到剩余预算

**只重试网络瞬时类**。`_open_connection` 已把所有 connect 异常分类成 `{ssh_connect_timeout, ssh_auth_failed, ssh_connect_failed}`：

| kind | 来源 | 重试? |
|---|---|---|
| `ssh_connect_timeout` | `TimeoutError`/`OSError`/`socket.gaierror`/`ConnectionRefused`（明确网络层） | **是**——这才是冷路径瞬时态 |
| `ssh_auth_failed` | `PermissionDenied`/`HostKeyNotVerifiable`/`KeyExchangeFailed` | 否——永久错，重试放大延迟 + 触发远端 sshd「too many auth failures」 |
| `ssh_connect_failed` | `asyncssh.Error` 兜底 + `DisconnectError` 分支 | **否**——兜底里可能含 **host-key 不匹配等永久错误**，盲目重试 = 把改过的 host key 当瞬时态反复探测（**安全隐患**，Codex 抓到） |

初版「重试 `ssh_connect_timeout` + `ssh_connect_failed`」**错**：`ssh_connect_failed` 是未分类兜底，retry 它会把永久错误（host-key 漂移等）当瞬时。收紧到 **timeout-only** 既覆盖 Tailscale 冷路径（失败是 `TimeoutError`→`ssh_connect_timeout`），又关掉安全洞。这也使既存 spec/code 漂移（live spec line 22 写 `BadHostKeyError`、码用 `HostKeyNotVerifiable`；`_reconnect` 兜底 catch `ssh_connect_failed`）**不影响本提案安全性**——无论 host-key 落哪个 catch，它都不是 `ssh_connect_timeout`，永不进重试；漂移本身作为独立 follow-up，不在本提案扩 scope 修。

**每次尝试 + 退避都 cap 到剩余预算**（Codex/RC 抓到的两处溢出）：循环里 `remaining = budget - (monotonic() - start)`；① 本次 `connect_timeout` 用 `min(self._connect_timeout(), remaining)`（需给 `_open_connection`/`_connect_kwargs` 加 timeout override 参，否则 cap 静默失效——`_connect_kwargs` 现硬编 `self._connect_timeout()`），否则贴着 90s 截止线开始的最后一次尝试会跑满 `connect_timeout`（~10s）冲出预算；② 捕获 `ssh_connect_timeout` 后**重算 `remaining`，`<= 0` 不再退避直接 stamp+raise，否则 `sleep(min(1.0, remaining))`**——否则末次失败后无条件 `sleep(1.0)` 会让耗尽迟一个退避才判、总耗时 `budget + 1s`。两处都界住后总耗时 ≤ budget + 末次连接尝试。`remaining <= 0` 即不再发起新尝试、stamp + raise。

**为何 1s 小退避而非指数**：Tailscale 冷路径靠反复 connect 推进握手，激进重探比长退避更快收敛；1s 间隔避免 `ConnectionRefused` 这种瞬时返回把循环变忙等。

**与重连循环两条独立路径**（不变）：重连（`[1,4,16]s`、catch `ConnectionLost`/`ChannelOpenError`、耗尽 `ssh_connection_lost`）仅用于「已建连断开」；冷连接重试仅用于 `self._conn is None` 的首连。两者严格分开。

## 决策 3：批内负缓存——锁体精确顺序

**长生命周期事实**：`cli/schedule.py:_context_factory` 闭包共享 daemon 启动建的同一 `TargetRegistry`，`SSHTarget` 实例**跨 run 长存**。一次巡检里一台机 N 个 inspector 共享该实例、并发争锁。真宕场景无负缓存：第一个耗尽 90s 失败、`_conn` 仍 None、释放锁；第二个获锁见 None → 起自己的 90s……`N×90s`。负缓存让兄弟 inspector fast-fail，收敛回 ~90s（锁串行化保证：第一个付预算的 90s 里其余阻塞在锁上，首个释放后微秒级依次命中 fast-fail）。

**锁体精确顺序**（review 要求 pin 死，防 stale-stamp 误判）：
1. idle sweep（`_conn` 非 None 且超 idle → close，置 None）——**不变**；
2. **若 `_conn is None`**：检查负缓存 stamp（`_cold_connect_failed_at` 非 None 且 `monotonic() - stamp < TTL`）→ 立即 raise `ssh_connect_timeout`（fast-fail，不付预算）；否则进预算重试循环；
3. 循环**成功** → 赋 `self._conn` + `self._last_used_at` + **清 `_cold_connect_failed_at = None`**；
4. 循环**预算耗尽** → **stamp 一次** `_cold_connect_failed_at = monotonic()` + raise。

**不变量**：stamp **只在耗尽写**（非每次 `_open_connection` 失败）、**只在锁入口读**（非循环中途——首个 inspector 自己的重试循环不会被自己的 stamp 干扰）、**只在步④（首次 connect 成功）清**。**`_reconnect` 不清 stamp**：stamp-set 态恒 `_conn is None`，而 `_reconnect` 恒要求 `_conn` 曾成功建立（必经步④已清 stamp）后断开，二者实际不可达地共存；且负缓存读受 `_conn is None` 门控，`_conn` 非 None 时残留 stamp 被屏蔽——即便构造出极罕见交错（活跃连接被兄弟误判 idle，而 inspector 超时 ≪ idle_timeout 300s 已阻止），最坏也仅 TTL 内一次假 fast-fail、非正确性破坏。故在该路径加清除是**不可达分支的兜底**，删之（对齐项目「不写防御性 fallback」；初版 round-1 加的 _reconnect-clear 经 round-2/3 分析证为死代码、且后果良性）。负缓存覆盖**所有获取连接的入口**（`exec` / `read_file` / preflight `command -v`·`[ -r ]` 探测都经 `_ensure_connection`），同一冷 host 一次 stamp 后其余入口一并 fast-fail——正确且预期。

**TTL（默认 120s，标定旋钮）**：兄弟 inspector 在首个付预算的 90s 里就阻塞在锁上、首个释放后即命中，实际只需 > 0；120s 是对 semaphore 排队致某 inspector 晚到的裕量，远短于 24h 日巡。**已知权衡**：连续 `schedule trigger`（< TTL）间真机恢复假阴性一次——日巡（24h）不命中，接受换零 plumbing。`# ponytail: 时间窗口负缓存覆盖单次扇出、明天前过期；back-to-back trigger 假阴性是已知上界`。**纳管探活每台新建 `SSHTarget` 后 `aclose`，stamp 不跨 host**，故 probe 只吃预算不享负缓存（每台一次 exec，无 N× 问题）。

**TTL vs daemon grace**：负缓存 TTL 120s 与 `shutdown_grace_seconds` 默认 120s 无耦合；预算 90s < grace 120s，SIGTERM 时冷连接 sleep 可取消，优雅停机不受影响（若未来抬预算 > grace 需复核）。

## 决策 4：吞吐——单冷目标不拖长，多冷目标诚实披露 N_cold×90s

fleet 扇出在 `deterministic.py` 走 `Semaphore(8)`，gate 所有 `target×inspector` 对。

- **单冷目标**：它本是 long pole，巡检无法在它的 inspector 完成前收尾、它需 ~90s，故总 wall-clock ≈ 90s+ε，无额外拖长（其余目标 inspector <1s，冷目标解析后 ~5s 跑完）。
- **多冷目标**（review 纠正初版「≈90s+ε」谎言）：M 个冷目标各占满槽位串行重试，最坏 **`M×90s`**（semaphore 饿死其他目标）。本提案**接受**此最坏吞吐、**诚实披露**，**不**改 scheduler（per-target preflight / 释放槽位重连是已声明非目标——会引入编排层连接管理，越界）。理由：fleet 多台同时冷是罕见态（Tailscale 抖动通常单点），日巡非延迟敏感（08:00 cron）；真高频多冷再单独提 scheduler 优化提案。负缓存把单台真宕从 `N_inspectors×90s` 收敛回 ~90s（这是负缓存的价值，与多目标串行正交）。

## 决策 5：失败检查 reason 先 key on `status`，仅 target_unreachable 用 error 细化

**关键数据事实**（三源 review 独立命中，初版踩坑）：`InspectorResult.error` **不是** status 串：

| status | `error` 实际值（`inspectors/runner.py`） | 可映射? |
|---|---|---|
| `target_unreachable` | `exc.kind`（`ssh_connect_timeout`/`ssh_auth_failed`/`ssh_no_entry`/`target_disabled`/docker·k8s kinds…，**枚举串**） | 是 |
| `timeout` | `"collect.command exceeded N seconds"`（**自由文本句**） | 否 |
| `exception` | `"parse_failed: …"` / `"parameter_validation_failed: …"` / `str(exc)`（**自由文本**） | 否 |

且模型校验器（`result.py`）**强制** failed 状态 `error` 非空——所以初版「`error` 非 None 优先、否则 `status`」的 `else status` **永不触发**，映射表 `timeout`/`exception` 键是**死键**：渲染会漏生英文，且按自由文本 `error` 分组会把同主机多个 `exception` 拆成 N 组（破坏「合一组防刷屏」）。这个 bug 在 cloudcone 例子上**看起来对**（那是 `target_unreachable`/ssh-kind），却对另两类 failure 撒谎。

**正确设计**：
- 分组键 `(target_name, label)`，`label` **先按 `status`**（5 值闭枚举，恒可映射）取桶级中文：`_FAIL_STATUS_LABELS = {timeout: 执行超时, target_unreachable: 不可达, exception: 采集异常}`；
- **仅当 `status == target_unreachable`**（此时 `error == TargetError.kind` 是枚举串）用 `_FAIL_KIND_LABELS` 细化：`{ssh_connect_timeout: 连接超时, ssh_auth_failed: 认证失败, ssh_connect_failed: 连接失败, ssh_connection_lost: 连接中断}`；
- **未知 kind**（`ssh_no_entry` / `target_disabled` / docker·k8s kinds…）→ **回退到桶级中文「不可达」**，**不**漏生英文；
- 过滤用既有 `_FAILED_STATUSES` frozenset（`status in _FAILED_STATUSES`），**不**用 `status not in {ok, requires_unmet}` 负谓词——对齐 `coverage_line` 的闭集纪律（防未来第 6 个 status 静默归 failed）。

`label` 对 `timeout`/`exception` 不读 `error`，故同主机同 status 恒合一组；`target_unreachable` 同 kind 合一组。零模型改动（`_redact_inspector_result` 保留 `status`/`target_name`/`error`；`error` 即便敏感也已 `redact_text`——安全，但本设计对两类 status 根本不读 `error` 文本）。

## 诚实边界

- **重试不是 SLA**：90s 预算覆盖实测 ~70s + 裕量，更慢路径仍判失败——有意上界。
- **负缓存是批级优化、非健康判定**：TTL 一过即重探；back-to-back trigger 假阴性是已知上界。
- **多冷目标吞吐**：最坏 `N_cold×90s`，接受不改编排。
- **预算无 per-host 旋钮**：硬默认 90s 针对当前 Tailscale fleet；upgrade path 是 entry 字段 / Settings，YAGNI 到有需求。
- **不修既存漂移**：retry-timeout-only 绕开 host-key 安全影响；`BadHostKeyError`/`HostKeyNotVerifiable` spec/code 漂移、`_reconnect` 兜底 catch 作为独立 follow-up。
