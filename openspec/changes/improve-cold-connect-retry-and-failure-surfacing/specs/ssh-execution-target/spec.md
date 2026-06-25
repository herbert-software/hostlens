## 修改需求

### 需求:`SSHTarget` 必须基于 AsyncSSH 实现且复用 per-target control connection

`hostlens.targets.ssh.SSHTarget` 必须：

- `type == "ssh"`
- 实现 `ExecutionTarget` Protocol（见 `execution-target` spec）
- **持有一个 per-target asyncssh control connection**：首次 `exec` 时按需建立 `asyncssh.connect(...)`；之后每次 `exec` 在该连接上**新建 channel**（通过 `conn.run(cmd, env=env)`，asyncssh 内部为每次 run 开 channel）—— **禁止**每次 exec 都重新 `asyncssh.connect`（对齐 docs/OPERABILITY.md §2.1 / §2.2 硬约束「不允许『每个 Inspector 重新 SSH 一次』—— 这是 M1 实施 SSH target 时必须 enforced 的硬约束」）
- 连接管理用 `asyncio.Lock` 保护"是否已建连 + 是否需重连 + 冷连接负缓存"状态机；channel 创建本身**无需**加锁（asyncssh 原生支持并行 channel）
- `connect_timeout`（单次 `asyncssh.connect` 尝试的上限）默认 10s，可在 `TargetEntry` 配置中按 target override
- **`cold_connect_retry_budget_seconds`（首次 connect 的总重试预算）是 `SSHTarget` 的构造参数，默认 `None`**（`None` = 不重试 = 单次尝试，完全保持既有行为）。**它是构造参数、不是 `TargetEntry` 字段**——谁需要重试由**调用路径意图**决定、不是 per-host 配置：`build_registry_from_config(..., cold_connect_retry_budget_seconds=None)` 透传（同文件 `build_one_target` 也加同名参并转发其内部 `build_registry_from_config`），**仅**两条路径传非 None（硬默认 `_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS = 90.0`）：① 定时 fleet 巡检（`cli/schedule.py` 的调度 registry 构造）；② 纳管探活（`targets/probe.py` 调 `registry.build_one_target` 处，含 `target import` / `propose_target_import`）。**doctor（`cli/doctor.py`）/ `target test`（`cli/target.py`）/ 临时 `inspect`（`cli/inspect.py`）/ `mcp serve`（`cli/mcp.py`）/ `fix`（`cli/fix.py`）** 的 registry 构造一律**不**传（默认 `None` → 保持各自 5–12s 快速失败的响应契约，零行为变更）。**不**在 `Settings.ssh` 加全局字段、**不**在 `SSHEntry` 加 per-target 字段（YAGNI；upgrade path 是真有 per-host 调优需求时再加）。
- `ssh.idle_timeout_seconds` 默认 300s：control connection 空闲超过此值时自动 close；下次 exec 按需重连。**该配置由 M0 `Settings.ssh.idle_timeout_seconds` 提供**（M1 通过 task 4.3a 扩展 Settings 加入 `ssh` 子 namespace；env var `HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS` 可 override）；**不**放进 `TargetEntry` 字段集（M1 范围内 per-process 单值即可，per-target override 推到有用户需求时）
- **首次 connect、冷连接重试、重连三条路径必须严格分开**：
  - **首次 connect**（lazy 建立 `self._conn`）按异常类型分类 raise（**不变**）：
    - `asyncio.TimeoutError` / `OSError` / `socket.gaierror` / `ConnectionRefusedError`（网络层 / DNS / 防火墙） → `TargetError(kind="ssh_connect_timeout", target=self.name)`
    - `asyncssh.PermissionDenied` / `asyncssh.HostKeyNotVerifiable` / `asyncssh.misc.KeyExchangeFailed`（认证 / host key / KEX） → `TargetError(kind="ssh_auth_failed", target=self.name)` + **三层 password scrub**
    - 其它 asyncssh 异常 → `TargetError(kind="ssh_connect_failed", target=self.name, original=exc)`（兜底，**不**走重连循环）
  - **冷连接预算重试**（**本提案新增，仅当 `cold_connect_retry_budget_seconds` 非 None 且 `self._conn is None`**）：在该预算内反复尝试首次 connect，**仅重试 `ssh_connect_timeout`** kind。理由：生产 fleet 走 Tailscale，空闲节点冷路径首次建连是逐步收敛过程，实测可达机器需 >10s（如 ~70s）才建好；单次 `connect_timeout` 无法覆盖。算法：
    - `start = time.monotonic()`；循环：`remaining = budget - (monotonic() - start)`；`remaining <= 0` → 退出去 stamp+raise；
    - 以 **`connect_timeout = min(self._connect_timeout(), remaining)`** 调首次 connect（**每次尝试 cap 到剩余预算**——否则贴着截止线开始的最后一次尝试会超出预算整整一个 `connect_timeout`。`_open_connection` / `_connect_kwargs` 必须支持本次 `connect_timeout` override，否则 cap 静默失效）；
    - 成功 → 见下「锁体顺序」步④；
    - 捕获 `TargetError` 且 **`kind == "ssh_connect_timeout"`** → **重算 `remaining`，`remaining <= 0` 则不再退避、直接去 stamp+raise；否则 `await asyncio.sleep(min(1.0, remaining))`**（1s 小固定退避激进重探推进握手而非忙等，但退避前必须界住截止线——否则耗尽会迟一个退避才判、冲出预算，使总耗时 ≤ budget + 末次尝试而非 budget + 退避）后续循环；
    - **`kind ∈ {ssh_auth_failed, ssh_connect_failed, ssh_no_entry}` 立即 re-raise，不重试**。认证/host-key 是永久错（重试放大延迟 + 触发远端 sshd「too many auth failures」）；`ssh_connect_failed` 是 asyncssh 未分类兜底，**可能含 host-key 不匹配等永久错误**，盲目重试 = 把改过的 host key 当瞬时态反复探测（安全隐患）。故重试集**严格只含 `ssh_connect_timeout`**（明确网络瞬时态）。
    - 预算耗尽（`remaining <= 0`）→ stamp 负缓存后 raise `TargetError(kind="ssh_connect_timeout", target=self.name)`（**kind 不变**，对调用方契约稳定，只是来得更晚）
  - **冷连接失败的批内负缓存短路**：定时巡检里一台机的 N 个 inspector 共享同一个跨 run 长生命周期 `SSHTarget`（`context_factory` 闭包共享 `TargetRegistry`）、并发争连接锁。若不短路，首个 inspector 耗尽预算失败后每个兄弟 inspector 会各起一轮预算（N×预算）。故耗尽预算时 stamp `self._cold_connect_failed_at = time.monotonic()`，锁体内在 `self._conn is None` 分支检查该 stamp 是否在 TTL（`_COLD_CONNECT_NEG_TTL = 120.0`）内 → 命中则立即 raise `ssh_connect_timeout`（fast-fail）。stamp **只在预算耗尽时写一次**（非每次尝试失败）、**只在锁入口读一次**（非循环中途）。**清除点唯一**：首次 connect 成功（步④）清 `self._cold_connect_failed_at = None`。**`_reconnect` 不清 stamp**——常态下 stamp 仅在耗尽时写（此时 `self._conn is None`），而 `_reconnect` 仅在 `self._conn` 曾成功建立（必经步④已清 stamp）后断开才触发，二者实际不可达地共存；且负缓存读受 `self._conn is None` 门控，`_conn` 非 None 时任何残留 stamp 被屏蔽，即便构造出极罕见交错（活跃连接被兄弟误判 idle——inspector 超时 ≪ idle_timeout 300s 已阻止），最坏后果也仅是 TTL（≤120s）内一次假 fast-fail，非正确性破坏。故**不**为此路径加防御性清除（对齐「不写不可能分支兜底」）。该负缓存覆盖**所有获取连接的入口**（`exec` / `read_file` / preflight `command -v`·`[ -r ]` 探测都经 `_ensure_connection`），不止 inspector exec——同一冷 host 一次 stamp 后其余入口一并 fast-fail，正确且符合预期。TTL 远短于日巡间隔故不污染下一次 run；连续 trigger（间隔 < TTL）间真机恢复会假阴性一次（有意权衡）。纳管探活每台**新建** `SSHTarget` 后 `aclose`，stamp 不跨 host，故 probe 只吃预算、不享负缓存（每台一次 exec，无 N× 问题）。
  - **重连（仅当 `self._conn is not None` 且已经成功建立过连接，随后检测到 `asyncssh.ConnectionLost` / `asyncssh.ChannelOpenError`）**：按下方精确算法（**不变**）
- **`_ensure_connection` 锁体精确顺序**（pin 死，防 stale-stamp 误判）：① idle sweep（`_conn` 非 None 且超 idle → close 置 None，不变）→ ② **若 `_conn is None`**：负缓存 fast-fail 检查（命中 raise）→ ③ 进首次 connect（budget None：单次 `_open_connection`；budget 非 None：预算重试循环）→ ④ 成功：赋 `self._conn` + `self._last_used_at` + 清 stamp → ⑤ 耗尽：stamp + raise。
- **重连精确算法**（不变）：
  ```python
  # Pre-condition: self._conn 之前已成功建立，现在 exec(cmd) 时检测到 ConnectionLost
  conn_timeout = entry.connect_timeout or 10
  for delay in [1.0, 4.0, 16.0]:        # 严格按 OPERABILITY §2.2 的退避序列
      await asyncio.sleep(delay)
      try:
          self._conn = await asyncssh.connect(..., connect_timeout=conn_timeout)
          return await self._run_on_channel(cmd, timeout=timeout, env=env)
      except (asyncssh.ConnectionLost, asyncssh.ChannelOpenError):
          continue                       # 已建过连接 + 这一类错误才重试
  raise TargetError(kind="ssh_connection_lost", target=self.name)
  ```
  其中 `self._run_on_channel(cmd, timeout, env)` 是 exec 的实际 channel 调用，与首次正常路径同一 helper。这定义为 **"1 次自动重连尝试块（一组 3 段退避 + 3 次 connect 尝试）"**——总共最多 3 次 `asyncssh.connect` + 3 次 sleep（共 21s 上限）。**重连循环的 catch 范围仍严格限 `ConnectionLost` / `ChannelOpenError`，禁止扩到 `OSError`**。**冷连接预算重试是与重连循环完全独立的路径**——前者仅用于 `self._conn is None` 的首连、仅退 1s、仅重试 `ssh_connect_timeout`、报 `ssh_connect_timeout`；后者仅用于已建连断开、退 `[1,4,16]s`、报 `ssh_connection_lost`。两者互不混入。
- asyncssh `keepalive_interval` 设为 60s；`agent_forwarding=False` + `x11_forwarding=False` 显式禁用
- `capabilities` 初始值 `{Capability.SSH, Capability.SHELL, Capability.FILE_READ}`；运行时按需探测 `SYSTEMD` / `DOCKER_CLI`（首次 `exec` 后探测一次并缓存）
- 析构（`__del__` / `aclose`）必须 close control connection；测试套不允许 `ResourceWarning: unclosed transport`

#### 场景:SSHTarget 首次 exec 建立连接后复用

- **当** 同一 `SSHTarget` 实例连续调用 `await ssh_target.exec(...)` 3 次，每次间隔 < 5s
- **那么** `asyncssh.connect(...)` 必须**只被调用 1 次**（后续 2 次复用）；3 次都成功返回 ExecResult

#### 场景:默认无预算时首次 connect 单次尝试（回归锚，本提案新增）

- **当** `SSHTarget` 以 `cold_connect_retry_budget_seconds=None`（默认，即 doctor / `target test` / `inspect` 路径）构造，目标 host 不响应
- **那么** 首次 connect **只尝试 1 次**即 raise `ssh_connect_timeout`（**不**进重试循环、**不** stamp 负缓存）——既有快速失败行为零变更

#### 场景:SSHTarget 并行 exec 在同一 connection 上开多 channel

- **当** 用 `asyncio.gather(target.exec(...), target.exec(...), target.exec(...))` 并行触发 3 次 exec
- **那么** `asyncssh.connect` 仍只被调用 1 次；3 个 exec 必须独立完成

#### 场景:SSHTarget idle timeout 自动关闭连接

- **当** 配置 `ssh.idle_timeout_seconds=2`；exec、sleep(3)、再 exec
- **那么** `asyncssh.connect` 必须被调用**2 次**（首次 + idle close 后第二次）

#### 场景:SSHTarget control connection 断开自动重连

- **当** control connection 因服务端 idle disconnect 抛出 `ConnectionLost`；调用 exec
- **那么** 自动重连**1 次**（退避 1s → 4s → 16s）；成功后 exec 正常返回
- **且** 该重连块穷尽仍失败 → raise `ssh_connection_lost`（**不**raise asyncssh 原生异常）
- **且** 重连路径**不**触碰 `self._cold_connect_failed_at`（stamp-set 态与重连前置态互斥，无需在此清除——见上「清除点唯一」）

#### 场景:SSHTarget 冷连接在预算内重试后成功（本提案新增）

- **当** 以 `cold_connect_retry_budget_seconds=90`（巡检 / 纳管路径）构造，**可达但冷路径慢**的 host：`asyncssh.connect` 前 K 次抛 `asyncio.TimeoutError`、第 K+1 次成功；预算足以容纳 K+1 次尝试 + K 次 1s 退避
- **那么** `exec` 必须**最终成功**返回 ExecResult；`asyncssh.connect` 被调用 K+1 次；建连后 `self._conn` 缓存，后续 exec 复用（不再触发预算重试）

#### 场景:SSHTarget 冷连接耗尽预算后 raise ssh_connect_timeout 且每次尝试 cap 到剩余预算（本提案新增）

- **当** 以小预算构造（测试），目标 host 持续不响应 —— `asyncssh.connect` 恒抛 `TimeoutError`
- **那么** 必须在预算耗尽后 raise `ssh_connect_timeout`（含 target name 与 host:port、不含凭据 / key path），且 stamp 负缓存；**且** 每次尝试的 `connect_timeout` 取 `min(connect_timeout, 剩余预算)`、退避取 `min(1.0, 剩余预算)` 且耗尽不再退避——总耗时 ≤ budget + 末次连接尝试（**不**额外加一整个退避），最后一次尝试不冲出截止线

#### 场景:SSHTarget 冷连接失败后同批兄弟 exec 立即 fast-fail（本提案新增）

- **当** 同一 `SSHTarget` 实例：首个 exec 耗尽预算失败（stamp 负缓存），随后 TTL 内对同实例发起第二个 exec
- **那么** 第二个 exec 必须**立即** raise `ssh_connect_timeout`，**不再**进预算重试（`asyncssh.connect` 调用计数相对第一个 exec 之后**不增加**）

#### 场景:SSHTarget 冷连接负缓存 TTL 过期后重新重试（本提案新增）

- **当** 首个 exec 耗尽预算失败 stamp 后，等待超过负缓存 TTL（测试 monkeypatch 为小值），再对同实例 exec
- **那么** 该 exec 必须**重新进入**预算重试（`asyncssh.connect` 再次被调用）——负缓存是批级短路、非持久健康判定

#### 场景:SSHTarget 首次 connect 认证失败立即 raise 不重试（本提案新增）

- **当** 以非 None 预算构造，首次 connect 时 `asyncssh.connect` 抛 `asyncssh.PermissionDenied`（或 host-key / KEX 失败，归 `ssh_auth_failed`）
- **那么** 必须**立即** raise `ssh_auth_failed`（含三层 scrub），`asyncssh.connect` **只被调用 1 次**（**不**进预算重试、**不** stamp 负缓存）。同理 `ssh_connect_failed`（asyncssh 兜底，可能含 host-key 漂移）**不重试**——重试集严格只含 `ssh_connect_timeout`

#### 场景:SSHTarget exec 超时返回 timed_out 且 channel close

- **当** 调用 `await ssh_target.exec("sleep 60", timeout=2)` 且 control connection 已建立
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=None)`；超时仅 close 该 channel，**不**影响 control connection（下次 exec 仍可复用）
