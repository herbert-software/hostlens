> 两个既有 os-shell inspector 的诊断质量校准：load 改用持续信号门控（`linux.system.load_avg`）、systemd 时间锚+uptime gate+severity 校准。均在套件「禁新基础设施」契约内（仅用现有 kv/json parse / for_each / parameters；不用 gawk-only `systime()`）。

## 新增需求

### 需求:`linux.system.load_avg` 负载告警必须按持续信号（load5/load15）判定、不以单次 load1 触发

`linux.system.load_avg` 已按 per-core（`load / ncpu`）判定，但当前**门控用 `load1`（单次 1 分钟采样）**，致单核机一次进程突发即误标「严重」（真机 `load1=2.45` 单核 → critical 是已暴露的误报）。**必须**改为用**持续信号** `load5` 与 `load15` 的 per-core 比值门控:

- 告警门控**必须只用 `load5` 与 `load15`**（持续信号），**禁止**让 `load1` 参与任一 finding 的 `when`。`load1` 可保留 output 供展示，但**不得**产生 finding。
- severity 分级:`critical` 当 **`load15/ncpu >= crit_per_core` 且 `load5/ncpu >= crit_per_core`**（持续且高度过载，AND 而非 OR——load5 已回落即视为正在恢复、不 critical）;`warning` 当二者 `>= warn_per_core`（且未达 critical）。两者**必须**为 `parameters`（默认 `warn_per_core=1.0` / `crit_per_core=2.0`，沿用现值，numeric 免 `pattern`），可按机型在 schedule 覆盖。
- 既有 collect（`/proc/loadavg`+`nproc`，`parse: kv`，output 含 `load1/load5/load15/ncpu`）**不变**；仅 findings 的 `when`/`message` 变。message 保持英文（该 inspector 在 i18n `_BACKLOG`，无需迁移 allowlist）。`version` bump → finding-id churn（regression diff 旧 id resolved + 新 id added）。**不动** `system.uptime`（另一个固定阈值 load-avg inspector，非本次范围）。

#### 场景:单次 load1 尖峰不告警（病 2 修复，确定性锚）
- **当** 一台单核主机（`ncpu==1`）`load1` 短暂冲到 `2.45`，但 `load5`/`load15` 正常（如 `0.41`/`0.33`，即 `load15/ncpu==0.33 < warn_per_core`）
- **那么** `linux.system.load_avg` **禁止**产生任何 finding（瞬时尖峰非故障），报告**不得**因此被标 `warning`/`critical`

#### 场景:持续高负载才告警（load5+load15 双门）
- **当** `load15/ncpu` 与 `load5/ncpu` **同时** `>= crit_per_core`（持续过载且此刻仍在过载）
- **那么** **必须**产生 `critical` finding;若二者只达 `warn_per_core` 区间则产 `warning`;`load1` 无论多高都**不**单独决定 severity

### 需求:`linux.systemd.failed_units` 必须携带时间锚（含系统 uptime）并按 oneshot/历史校准 severity

`linux.systemd.failed_units` **必须**为诊断师提供「历史/开机一次性失败」与「近期失败」的区分依据，且**禁止**对**任何** failed 单元一刀切 `critical`（历史 cloud-init 残留把稳定运行数十天的整队标「严重」是已暴露的误判）。

- collect **必须**为每个 failed 单元补 **`Type`**（识别 `oneshot`）与 **失败时刻 `InactiveEnterTimestampMonotonic`**（开机以来微秒，免解析 systemd wall-clock 文本/时区），并补 **系统 `uptime_seconds`**（`/proc/uptime` 首字段，用 **shell 内建** `read -r uptime_seconds _ < /proc/uptime`——**不引入 `cut` 等未声明 binary**；`requires_binaries` 维持 `[systemctl, awk]` 不变）。**禁止**使用 gawk-only 的 `systime()`/`strftime()`/`mktime()`（inspector `targets:[local,ssh]` 会命中 mawk/busybox/BSD awk，致采集崩溃 / 假 `status=exception`）——boot/uptime 一律走 `/proc/uptime` + POSIX。`/proc/uptime` 读取失败 **必须** fail-loud（exit≠0）。per-unit 迭代**必须**用**换行分隔的 `while IFS= read -r unit`**（**禁止** `for unit in $(...)` 的空白分词，否则含空格/转义的单元名会被切碎），并保留既有 awk JSON-escape（unit 名可含 `\`/`"`）。
- 带 `for_each` 的列表输出，可迭代顶层键**必须**取自 `results`/`items`/`records`（套件契约）——本 inspector 用 **`results`**（数组项 `{unit, type, inactive_monotonic_us}`）。output_schema **必须** `required: [uptime_seconds, results]`;**空失败集**仍 **必须** emit 合法顶层对象 `{"uptime_seconds":N,"results":[]}`（`parse_json` 拒顶层数组 / 非对象）。
- **数值字段必须 emit 为裸 JSON 数字（不加引号）**:`uptime_seconds`（`number`）、`inactive_monotonic_us`（`int`）的 awk `printf` **必须**用 `%d`/`%s`-数值形落在引号外（如 `..."inactive_monotonic_us":%d...`）——output_schema 声明 `int`/`number`，若误加引号成字符串则 **jsonschema 校验失败 → `status=exception`**（伪失败，发生在 findings 之前）;只有 `unit`（`string`）走引号 + JSON-escape。
- severity 分级（`for_each: "results as u"`）:**oneshot、失败在开机窗口内、且系统已长跑**（`u.type=='oneshot' and u.inactive_monotonic_us>0 and u.inactive_monotonic_us <= boot_window_seconds*1000000 and uptime_seconds >= min_uptime_seconds`）→ **`warning`**（长跑机的开机一次性历史残留）;其余（非 oneshot、失败晚于开机窗口、**或系统 uptime 不足**、或无失败时刻）→ **`critical`**。`boot_window_seconds`（默认 `180`）与 `min_uptime_seconds`（默认 `3600`）**必须**为 `parameters`。
- **`uptime_seconds >= min_uptime_seconds` 门是必须的**:`InactiveEnterTimestampMonotonic` 是 boot-relative，仅「oneshot 在开机窗口内失败」**不足以**判历史——刚重启的机器上一个本次开机失败的 oneshot 也满足，那是「刚崩」、应 `critical`。须叠加「系统已长跑」才把「长跑机的开机历史残留」与「刚重启后失败」分开。
- message 从单条聚合（`{failed_names}`）改为每单元一条（含 unit + Type）→ **finding 数量 1→N、finding-id churn**（可接受、列入重跑清单）。**message 模板用下标语法** `{u[unit]}` / `{u[type]}`（`str.format` 无法用属性式 `{u.unit}`；属性式 `u.type` 只能在 `when` DSL 表达式里用——与 `docker/containers_restart_loop.yaml` 范式一致：`when: c.restart_count`、`message: {c[name]}`）。该 inspector 是 i18n `_MIGRATED_ALLOWLIST` 唯一成员:**每条 FindingRule（warning + critical 两条）的 message 各**必须保留 ≥1 CJK 字符（`test_message_contains_cjk` 按 FindingRule 参数化、逐条校验）;**只注入**循环变量 `u[...]` 与 `output_schema.properties` 已声明的键(`uptime_seconds`),**禁止**注入 parameter 名(`boot_window_seconds`/`min_uptime_seconds` 不在 output_schema → 触发 `test_injected_fields_are_declared` 的 if-inject-then-declared 失败),也不得引用已删除的 `failed_names`。

#### 场景:长跑机的开机一次性历史残留不拉整队 critical（病 1 修复，确定性锚）
- **当** 一台 `uptime_seconds` 远超 `min_uptime_seconds`（如 up 36 天）的主机有 `cloud-final.service`（`Type=oneshot`）在开机窗口内（`inactive_monotonic_us <= boot_window_seconds*1e6`）失败、无近期失败
- **那么** 该 finding **必须**为 `warning`（**禁止** `critical`），故报告级 `aggregate_severity` 不被这条历史残留拉到「严重」;message **必须**标明其开机一次性/历史性质

#### 场景:刚重启后的开机失败仍 critical（不误降，fresh-reboot 锚）
- **当** 一台 `uptime_seconds < min_uptime_seconds`（刚重启不久）的主机有 `oneshot` 单元在本次开机窗口内失败
- **那么** 该 finding **必须**为 `critical`（**禁止**降为 warning）——低 uptime 下无法判定是历史残留，可能是当前故障

#### 场景:常驻服务失败仍 critical（不误降）
- **当** 一台主机的 `zerotier-one.service`（`Type=notify`/`simple` 等非 oneshot）处于 failed
- **那么** 该 finding **必须**为 `critical`（常驻网络服务 failed 是真问题，即便与其它失败彼此独立——「独立 vs 连锁」是诊断师叙事职责、非 severity）
