# Tasks

## 1. `SSHTarget` 构造参数 + 常量（无 per-entry 字段）

- [x] 1.1 `SSHTarget.__init__` 加 `cold_connect_retry_budget_seconds: float | None = None`（默认 None = 单次尝试 = 现状）+ `self._cold_connect_failed_at: float | None = None`。模块常量 `_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS = 90.0`、`_COLD_CONNECT_RETRY_DELAY = 1.0`、`_COLD_CONNECT_NEG_TTL = 120.0`。**不**改 `targets/config.py`（不加 `SSHEntry` 字段、不动 `__repr__`/`_entry_to_dict`/Protocol）。
- [x] 1.2 `targets/registry.py:build_registry_from_config` 加 `cold_connect_retry_budget_seconds: float | None = None`，构造每个 `SSHTarget` 时透传。**同文件 `registry.py:build_one_target`（≈line 215，探活单 target 构造助手——它内部调 `build_registry_from_config(config, settings)`）也加同名参数并透传给该内部调用**（否则 probe 路径无处可传，Codex/CR 抓到的缺链）。
- [x] 1.3 仅两处**调用点**传 `_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS`：`cli/schedule.py` 的调度 registry 构造（即 `_build_target_registry` / daemon serve 路径；该 helper 被 list/trigger/daemon/status 共享，但 list/status 永不 `target.exec`，故预算在那两条 inert，无需拆分）；`targets/probe.py` 调 `registry.build_one_target` 处（纳管探活）。**doctor（`cli/doctor.py`）/ `target test`·`list`（`cli/target.py`）/ `inspect`（`cli/inspect.py`）/ `mcp serve`（`cli/mcp.py`）/ `fix`（`cli/fix.py`）/ demo（`demo/assembly.py`，replay-only 永不 dial）的 registry 构造一律不传**（默认 None → 快速失败，零行为变更）——这些处经核实均以位置参数 `(config, settings)` 调 `build_registry_from_config`，新增尾部 kw-default 参向后兼容，本提案保持原样。`target list` / status / demo 即便（经共享 helper）带了预算也 inert（永不 `.exec`）。

## 2. `SSHTarget._ensure_connection` 锁体改造（精确顺序）

- [x] 2.1 锁体顺序固定：① idle sweep（不变）→ ② `if self._conn is None`: 负缓存 fast-fail 检查 → ③ 预算重试循环 → ④ 成功赋值 / ⑤ 耗尽 stamp+raise。
- [x] 2.2 负缓存 fast-fail（步 ②，仅当 `_conn is None`）：`self._cold_connect_failed_at is not None and time.monotonic() - self._cold_connect_failed_at < _COLD_CONNECT_NEG_TTL` → 立即 raise `TargetError(kind="ssh_connect_timeout", target=self.name)`。
- [x] 2.3 预算重试循环（步 ③，仅当 budget 非 None；budget None → 维持单次 `_open_connection` 现状）：`start = monotonic()`；loop：`remaining = budget - (monotonic()-start)`；`remaining <= 0` → break 去 stamp+raise；以 `connect_timeout = min(self._connect_timeout(), remaining)` 调 `_open_connection`；成功 → 步 ④；捕获 `TargetError` 且 `kind == "ssh_connect_timeout"` → **重算 `remaining = budget - (monotonic()-start)`，`remaining <= 0` 则不再 sleep、直接 break 去 stamp+raise；否则 `await asyncio.sleep(min(_COLD_CONNECT_RETRY_DELAY, remaining))`** 续循环（**退避前界住截止线**，否则耗尽会迟 ~1s 才判、冲出预算，Codex/RC 抓到）；**其它 kind（`ssh_auth_failed` / `ssh_connect_failed` / `ssh_no_entry`）立即 re-raise**（不重试）。
- [x] 2.3a `_open_connection` 加 `connect_timeout: float | None = None` 参（None 时取 `self._connect_timeout()`），`_connect_kwargs` 接受并覆盖默认的 `"connect_timeout"`——这是 cap 唯一生效点（CR 抓到：现 `_connect_kwargs` 硬编 `self._connect_timeout()`，不加 override 则 cap 静默失效、测试可能因末次恰好成功而假绿）。重连循环复用既有 `entry.connect_timeout or 10`，不受影响。
- [x] 2.4 成功（步 ④，首次 connect 成功）：`self._conn = conn` + `self._last_used_at = monotonic()` + `self._cold_connect_failed_at = None`（清负缓存）。**这是 stamp 的唯一清除点**——`_reconnect` **不**清 stamp：stamp 仅在耗尽时写、此时 `self._conn is None`，而 `_reconnect` 仅在 `self._conn` 曾成功建立（必经步④已清 stamp）后断开才触发，二者实际不可达地共存；且负缓存读受 `_conn is None` 门控屏蔽残留 stamp，最坏 TTL 内一次假 fast-fail、非正确性破坏。**不加防御性清除**（对齐项目「不写不可能分支的兜底」）。
- [x] 2.5 耗尽（步 ⑤）：`self._cold_connect_failed_at = monotonic()`（**只此一处 stamp**）+ raise `ssh_connect_timeout`。
- [x] 2.6 重连算法、idle sweep、keepalive 不动；`_connect_kwargs` 仅按 2.3a 加 timeout override 参（安全项不动）；`__init__` 既有连接状态字段不动。

## 3. notifier 「失败检查」段（reason 先 key on status）

- [x] 3.1 `notifiers/_filters.py`：加 `failed_checks(inspector_results)` 过滤器——筛 `status in _FAILED_STATUSES`（复用既有 frozenset，**不**用负谓词），按 `(target_name, label)` 分组并保序，返回 `[(target_name, label, [inspector_name...])]`。`label = _FAIL_STATUS_LABELS[status]`（`{timeout: 执行超时, target_unreachable: 不可达, exception: 采集异常}`）；**仅** `status == "target_unreachable"` 时 `label = _FAIL_KIND_LABELS.get(error, _FAIL_STATUS_LABELS["target_unreachable"])`（`_FAIL_KIND_LABELS = {ssh_connect_timeout: 连接超时, ssh_auth_failed: 认证失败, ssh_connect_failed: 连接失败, ssh_connection_lost: 连接中断}`；未知 kind 回退「不可达」）。**不**以自由文本 `error` 为 `timeout`/`exception` 的键。注册进 telegram + lark 两个 Jinja env。
- [x] 3.2 `templates/telegram/report.md.j2`：覆盖行后、发现段前插「失败检查」段，仅 `report.inspector_results | failed_checks` 非空时渲染；每组一行 `主机 · label`（`mdv2_escape`）+ inspector 名单。
- [x] 3.3 `templates/lark/report.card.j2`：同构插「失败检查」节，仅 failed 非空渲染，走 `tojson`；节**无 leading 逗号、自带 trailing 逗号**（镜像覆盖行，发现/健康态首元素保持无 leading）；**条件插入保持 JSON 合法**——验证 `failed>0`∧无 finding / `failed>0`∧有 finding / `failed>0`∧meta-None / `failed==0` 四种排列均无悬空/双逗号。

## 4. 测试

- [x] 4.1 `tests/targets/test_ssh*`（单元，**非** integration——integration spec 禁 mock asyncssh）：默认 budget=None 仍单次 connect（回归锚，现有行为不变）。
- [x] 4.2 budget 非 None：patch `asyncssh.connect` 前 K 次 `TimeoutError`、第 K+1 次返回 mock conn → exec 成功、connect 调用 K+1 次（monkeypatch budget/delay 为小值避免测试慢）。
- [x] 4.3 budget 耗尽 → raise `ssh_connect_timeout` + stamp 写入；验证每次尝试 `connect_timeout` 被 cap（用 patch 的 `connect` 断言收到的 `connect_timeout` ≤ 剩余预算）；**且总耗时 ≤ budget + ε**（退避已 `min(delay, remaining)` 界住、耗尽不再 sleep——可断言不超 budget 一个明显裕量，证明 2.3 的溢出修复）。
- [x] 4.4 `ssh_auth_failed`（patch connect 抛 `PermissionDenied`）一次即 raise、connect 只调 1 次（不重试、不 stamp）；同理验证 `ssh_connect_failed` 兜底不重试。
- [x] 4.5 负缓存短路：首个 exec 耗尽预算失败后，TTL 内第二个 exec 立即 raise、connect 调用计数**不增**。
- [x] 4.6 TTL 过期后 exec 重新进重试（connect 计数再增）。
- [x] 4.7 既有 ssh 场景回归（首次复用只 connect 1 次、并行 exec 共享、idle 重建、断开重连、exec timeout）全绿。
- [x] 4.8 `tests/notifiers/`：fleet 多目标失败——`timeout` status 显「执行超时」（**非**生英文 `collect.command exceeded …`）、`exception` 显「采集异常」、同主机多个 `exception`（`error` 文本各异）合**一**组（非 N 组）、`target_unreachable`+`error=ssh_connect_timeout` 显「连接超时」、未知 kind（`ssh_no_entry`）回退「不可达」；`failed==0` 不渲染段；Telegram `mdv2_escape` / Lark `tojson` 转义；覆盖行计数与失败检查段一致。Lark **四**种逗号排列均 `json.loads` 通过：`failed>0`∧无 finding / `failed>0`∧**有 finding** / `failed>0`∧meta-None / `failed==0`（段省略不留悬空逗号）。Telegram **meta-None ∧ failed>0**：抬头后直接渲失败检查段（覆盖行因 meta 缺省略），格式无误。

## 5. 归档前

- [x] 5.1 `mypy --strict` / `ruff` / 全量 `pytest` 绿。
- [ ] 5.2 对抗性 review 收敛（APPROVE/APPROVE-DEGRADED）后开 PR。
- [ ] 5.3 真机验证（可选）：daemon 重启后下一次 `daily-health-fleet` 对 cloudcone 不再 `partial`；或手动 `schedule trigger` 抽检。
