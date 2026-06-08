## 为什么

`add-replication-lag-inspectors`(wave-2c)的 postgres 录制验证门(task 5.1 / 决策 W-6)**撑破**:副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard 不能干净归一成 `replication-inspector-contract` 的 apply_lag 三元组——

- **漏洞①(实证)**:receiver 断开后 `pg_last_wal_receive_lsn()==pg_last_wal_replay_lsn()` 仍为 TRUE,idle-guard 捏造 `lag=0/健康`,而 standby 实际任意滞后;唯一揭穿信号是 `pg_stat_wal_receiver.status='streaming'`(另一系统视图)。
- **漏洞②(实证)**:standalone `pg_last_wal_receive_lsn()` 返回 NULL。
- 副本侧无 mysql `Seconds_Behind_Source` 式单值秒数等价物;诚实稳健的 postgres apply-lag 是**主库侧** `pg_stat_replication.replay_lag`(契约前向规则的主库侧聚合)。

完整证据见 `openspec/changes/archive/2026-06-08-add-replication-lag-inspectors/design.md`「门裁定」表。postgres 单元格被冻结为 **deferred-to-wave-3**;本提案是 wave-3,还这笔债。

**架构意义**:redis(wave-2 spike)与 mysql(wave-2c)都是**副本侧 N=1 identity**(连副本、问它落后多少)。postgres 的诚实路径是**主库侧 `pg_stat_replication`**——**N 行(每个 standby 一行)、真·归约**。这是契约里那条「主库侧多副本归约**方向**(滞后取最大、链路取逻辑与),精确函数留验证、不冻结、不测试」的**前向(暂定)规则第一次被真正实现**的时刻。wave-3 必须把它从「暂定方向」冻结成 normative + 测试。

## 变更内容

- 起独立 `postgres.replication_lag` 复制 inspector,走**主库侧 `pg_stat_replication.replay_lag`**(副本侧 timestamp 路径已被 wave-2c 门否决,不重复)。
  - 归一三元组(派生在 collector 内,DSL 只比标量):`replication_configured = (行数>0)`;`link_healthy = configured ? AND(state=='streaming') : false`;`lag_seconds = configured ? max(replay_lag→秒, non-NULL) : null`,全 NULL → null。语义类 `apply_lag`。
  - **空集裁定(继承「先证后铺、不假装能测撑破的东西」)**:主库侧 `pg_stat_replication` 空集**一律 `ok` 无 finding**——单机主库与「副本全断」从主库侧不可区分,spec **显式声明主库侧无法检测副本全断**,不假装能测。
  - **硬前提**:读全 `pg_stat_replication` 的 lag 列需 `pg_monitor`(或 superuser);欠权账户读到 NULL = 静默假健康风险,写进 description + tags(类比 mysql `REPLICATION CLIENT`)。
  - **拓扑反转**:redis/mysql 指向副本,postgres 指向**主库**;description 巨响声明,防用户按 mysql 习惯指 standby 而得假「未配置 ok」。
- **影响的规范:`replication-inspector-contract`**(MODIFY 既有 capability,不新立):
  - **ADD** 兄弟需求「主库侧采集视角必须按冻结归约函数归约」——把前向暂定归约方向冻结为 normative(配独立 crosscheck 测试),与既有「副本侧 N=1 identity」需求并存(append-only,不泛化④、避 RENAMED 归档坑)。
  - **MODIFY** 既有「副本侧采集视角必须聚合为 identity」需求——**仅退役**其已被本提案兑现的「主库侧多副本归约方向为前向暂定规则」场景(改为引用上面冻结的兄弟需求);标题与副本侧 identity 规范核心不变(不触发 RENAMED)。
  - **MODIFY** 既有「复制 inspector 覆盖矩阵随 wave 追加冻结」需求——postgres 单元格 `deferred → delivered`(语义类 `apply_lag`、主库侧路径),追加 postgres 场景;redis/mysql 已冻结单元格不动。
- 纳入 `test_replication_contract_crosscheck.py` 的 `_REPLICATION_MANIFESTS` 枚举,复制 cohort 计数守卫由 **2 → 3**;核验单实例 cohort(`_ALL_SERVICE_MANIFESTS` 11 / `_SECRET_SERVICE_MANIFESTS` 6)与全量 rglob 测试(`test_builtin_capability_gate` / `test_builtin_inspectors`)不因新增 builtin 误红。
- compose 加 `pg-repl-primary` + `pg-repl-standby`(streaming replication),录制器产 5 个 fixture(healthy / finding_trigger / link_down / lagging / conn_refused),全程 poll-until-condition 不用固定 sleep,ReplayTarget 逐字回放(CI 不起容器)。
  - **lagging** 用 standby 侧 `recovery_min_apply_delay`(主库持续写 → `replay_lag` 可控涨、`state='streaming'` 保持),确定性远优于 mysql 的「大积压赌追赶窗口」。
  - **link_down** 因主库侧物理断开=行消失=空集(测不到),改用「行在、`state≠'streaming'`」(catchup 状态)造,与 lagging(`link_healthy=true`)语义不同。

## 非目标 (Non-Goals)

- ❌ 不重复已被 wave-2c 门否决的副本侧 timestamp idle-guard 路径。
- ❌ 不引入「期望副本数 / application_name 列表」参数去检测副本全断——继承空集裁定,主库侧不假装能测全断。
- ❌ 不泛化契约需求④(副本侧 N=1 identity)成「按视角聚合」——用 ADD 兄弟需求承载主库侧,避 RENAMED。
- ❌ 不改契约既有需求(继承单实例契约 / 归一三元组 + 语义类 / 三态 by-finding / 两个语义不同 semantic-abnormal / 不进单实例 cohort)的 normative 核心;postgres 直接遵守,由泛化 crosscheck 机械证明。
- ❌ 不触动单实例 cohort 计数(11 / 6)、不引新 parse/secret 机制(复用 `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD`)。
- ❌ 不实现 cascading standby(standby 的下游)/ `write_lag`·`flush_lag` 维度 / 主库侧 `synchronous_commit` 档位语义——本期只交付 apply-lag(`replay_lag`)归一。
- ❌ 不区分物理 standby 与逻辑复制订阅者——`pg_stat_replication` 也含逻辑复制 walsender 行(其 `state`/`replay_lag` 同形,归约不崩),本 inspector 把它们一并纳入主库复制健康判定;description 注明此点,精细区分留后续 wave。
