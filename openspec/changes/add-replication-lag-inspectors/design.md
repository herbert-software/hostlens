## 上下文

`add-replication-inspector-spike`(已归档)用 redis 证明了 `replication-inspector-contract`(7 条需求:继承单实例契约 / 归一三元组 + 声明 lag 语义类 / 三态复制健康 by-finding / 副本侧 identity / 两个语义不同 semantic-abnormal / 不进单实例 cohort + 独立 crosscheck / 裁定 lag 语义异构),并把 `redis.replication_lag` 作为探针交付。spike 的裁定(D-8)给 wave-2c 钉死了范围:mysql 高置信同形(apply_lag)、postgres 形态分叉(待录制验证)。

本批次(wave-2c)在该契约上**铺 mysql**,并对 **postgres 设录制验证门**。mysql 与 redis 的关键不同:redis 副本侧单连接拿到的是 **link_freshness**(距上次主从 IO 秒数,非数据滞后);mysql `Seconds_Behind_Source` 是 **apply_lag**(SQL 线程应用落后秒数)——这正是契约「lag 语义随 DB 异构、须声明语义类」要管的事。

**复用基础**:`mysql.connection_usage`(secret `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` remap、TSV→JSON collector、role/version 注意);spike 的录制 lane(`_compose_record.py` + `wait_until` + `DockerExecTarget` + `record_fixture`、compose 共享 project 起 primary+replica)与 `_record_redis_replication_lag.py` 的双轨真造 + poll-until-condition 模式;泛化前的 `test_replication_contract_crosscheck.py`。

## 目标 / 非目标

**目标:**
- 铺 `mysql.replication_lag`:副本侧 `SHOW REPLICA STATUS` 归一成契约三元组(语义类 apply_lag),遵守既有 7 条契约需求(由泛化 crosscheck 机械证明)。
- 对 postgres 设**录制验证门**:验证副本侧 timestamp 路径(含 idle-guard)能否干净归一,据结果纳入或推 wave-3。
- 把复制 crosscheck 从 redis 单条**泛化为枚举所有已交付复制 inspector**;在契约里**追加冻结覆盖矩阵**。

**非目标:**
- ❌ 不重复 redis;不强铺撑不住的 postgres;不改契约既有 7 需求 / 单实例 cohort;不实现主库侧聚合;不引新 parse/secret 机制。

## 决策

> 决策标号 W-n(wave-2c)。涉及契约项处引用 spike 的 D-n(D-3 三元组 + 语义类、D-4 三态 + role-contextual fail-loud、D-5 双轨真造 + poll-not-sleep、D-6 独立 crosscheck)。

### W-1:mysql.replication_lag 走副本侧 `SHOW REPLICA STATUS`,归一成 apply_lag 三元组

**决策**:`mysql.replication_lag` 连接**副本**,跑 `SHOW REPLICA STATUS`(8.0.22+;5.7–8.0.21 `SHOW SLAVE STATUS`),collector 内归一(派生在 collector 内,DSL 只比标量):
- `replication_configured: bool` —— `SHOW REPLICA STATUS` 返回**非空结果集**(配置了复制)。
- `link_healthy: bool` —— `Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(5.7 `Slave_IO_Running`/`Slave_SQL_Running`)。
- `lag_seconds: int | null` —— `Seconds_Behind_Source`(5.7 `Seconds_Behind_Master`);**NULL → null**(MySQL 精确 NULL 条件:**applier〔SQL〕线程未运行**,或 **applier 已消费完所有 relay log 且 receiver〔IO〕线程未运行**;**活跃追赶期 SBS 是正整数、不是 NULL**)。语义类 = **apply_lag**(在 `description` 与覆盖矩阵 spec 中声明)。

**理由**:`Seconds_Behind_Source` 是 mysql 副本侧自报的**真数据应用滞后秒数**,语义比 redis 的 freshness 更贴「滞后」,单位即秒,无需换算,与契约三元组直接贴合。这是 D-8「mysql 高置信可沿用」的兑现。

**apply-stall 归类(明确)**:`Replica_SQL_Running=No` 而 `Replica_IO_Running=Yes`(SQL 应用停滞,IO 仍收)时,按 `link_healthy=IO&&SQL`→`false`、且 SBS=NULL→`lag_seconds=null`,**归类为 critical「link down」而非 lag finding**——因为 mysql 在 applier 停时不报 SBS,无法以秒数表达该停滞;由 link_healthy=false 兜住。这是刻意选择(applier 停=链路侧故障,非「滞后」),非遗漏。

**替代方案**:主库侧 `SHOW REPLICAS` / `performance_schema.replication_*` 聚合多副本——否决:引入多行聚合(契约前向规则、本批次不实现),且偏离「副本上巡检它落后多少」诉求。

### W-2:role-contextual fail-loud 兑现到 mysql

**决策**(兑现 spike D-4):collector 先判**有无复制行**——`SHOW REPLICA STATUS` **空结果集**(非复制 mysql 实例,合法单机)→ unconfigured 路径输出 `replication_configured=false`/`link_healthy=false`/`lag_seconds=null`,status=`ok`、**无 finding、不 exit 1**;mysql client 非零退出 / Access denied / 连不上 → fail-loud(exit 1 + 空 stdout)→ `exception`;缺 `mysql` client → `requires_unmet`。**禁止**把「非复制实例的空结果集」当 fail-loud 而误判 exception(对应 redis role:master standalone 的处理)。

**理由**:与 redis 的 role-branch 同构——「未配置复制 vs 复制故障」区分,一台合法单机不能被巡检判成故障。

### W-3:secret + 权限前提复用,不引新机制

**决策**:复用 `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` remap(逐字对齐 `mysql.connection_usage`,密码不进 argv、不 `-p<pwd>`、不 `{{ }}`)。文档式声明运行前提:**inspector 只读**跑 `SHOW REPLICA STATUS` 需账户具 `REPLICATION CLIENT` 权限(8.0+ 亦可 `REPLICATION_SLAVE_ADMIN`;或弃用的 `SUPER`)——**`PROCESS` 不足**(它管 `SHOW PROCESSLIST` 等,非 replica status)。**录制器**的 `START/STOP REPLICA` 控制语句需 `REPLICATION_SLAVE_ADMIN`(8.0+)/ `REPLICATION SLAVE`,与 inspector 只读权限**分开**(录制账户更高权,inspector 巡检账户只需 `REPLICATION CLIENT`)。写进 `description` + tags(`mysql8`),不经 manifest 字段机器门(`extra=forbid`)。

**理由**:secret 面与 connection_usage 完全一致,无新机制;权限前提是文档诉求(同 SSH `AcceptEnv` 前提的处理)。inspector 与录制器权限分离避免巡检账户被授予不必要的控制权。

### W-4:mysql primary+replica 录制 —— 两个语义不同的真造 semantic-abnormal

**决策**(兑现 spike D-5):compose 加 `mysql-repl-primary` + `mysql-repl-replica`(各 pin `--server-id`、primary `--log-bin`、replica 经 `CHANGE REPLICATION SOURCE TO ... ; START REPLICA` 或 GTID 自动接入;录制器建立复制账户)。`_record_mysql_replication_lag.py` 产**两个语义不同**的 semantic-abnormal:

1. **链路断**(`link_down`):`STOP REPLICA IO_THREAD`(或停 primary 容器)→ poll 副本直到 `Replica_IO_Running==No`(`link_healthy=false`)→ 冻结。critical「link down」。(此态 SBS 可能 NULL/陈旧——无所谓,finding 由 `replication_configured && !link_healthy` 触发,不看 SBS。)
2. **真实 apply 滞后**(`lagging`):造法**必须**让 applier〔SQL〕线程**正在运行且落后**(SBS 仅在 applier 运行时报正整数)——**唯一可行 recipe**:(a) `STOP REPLICA SQL_THREAD`;(b) 在 primary 上写入**足量大事务**积压 relay log(积压须够大,使追赶期 SBS 在阈值上**停留足够久**好 latch);(c) `START REPLICA SQL_THREAD`;(d) **追赶期间** poll 直到 `Seconds_Behind_Source>=` 默认 critical 阈值 **且** `Replica_IO_Running==Yes` **且** `Replica_SQL_Running==Yes` → 冻结。`link_healthy=true` 但 lag 高。**禁止**「SQL_THREAD 停期间 poll SBS」——**applier 停时 SBS=NULL,该路径物理不可能**(MySQL 文档:applier 未运行则该字段 NULL)。

两个 fixture **语义必须不同**:`link_down` 录制断言 `Replica_IO_Running==No`(`link_healthy=false`);`lagging` 录制断言 `Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(`link_healthy=true`)且 `Seconds_Behind_Source` 非 NULL 且 >=阈值。readiness 全走 poll-until-condition(poll `Replica_IO/SQL_Running` / `Seconds_Behind_Source`),**禁固定 sleep**。冻结后 ReplayTarget 回放,CI 不起容器。

**理由**:mysql 能造**真 apply 滞后**(redis 副本侧造不到、只能 freshness),所以 mysql 的 `lagging` fixture 比 redis 的 `link_stale` 语义更贴「滞后」——正是 D-8 异构的体现。但 SBS 仅在 **applier 运行**时报正值,故 lagging 只能在 `START REPLICA SQL_THREAD` 后的**追赶窗口**录制(applier 运行 + 落后),不能在 applier 停期间录制。

**风险/权衡**:追赶窗口可能很短(小积压秒级追平,SBS 跌回 0 来不及 latch)——**缓解**:积压足量大事务(W-4 步 b),使 SBS 在阈值上停留 ≥ 一个 poll 周期;录制器 poll 命中即冻结。

**替代方案**:用 `tc` 注网络延迟——否决(需 NET_ADMIN、不稳定);大积压 + 追赶窗口 poll 更可控、原生。

### W-5:复制 crosscheck 泛化为枚举所有已交付复制 inspector

**决策**:把 `test_replication_contract_crosscheck.py` 从硬编码 redis 单 manifest 改为**枚举 `_REPLICATION_MANIFESTS` dict**(redis.replication_lag + mysql.replication_lag〔+ postgres if 门通过〕),对每条参数化复跑:继承的单实例契约项(注入安全 / secret remap 不进 argv / 失败三态 / 超时 / 无分叉 / 输出形态)+ 复制专属项(三元组 + 语义类声明 / 三态 by-finding / 两个语义不同 semantic-abnormal 默认触发)。加**计数守卫**冻结复制 cohort 规模(2 或 3)。各 inspector 的 per-probe snapshot 仍在各自 `test_<db>_replication_lag.py`。

**理由**:契约的「独立 crosscheck 复跑继承项」需求(spike D-6)随 DB 增多必须泛化,否则每加一个 DB 就复制一份 crosscheck。枚举 + 计数守卫沿用 `test_service_contract_crosscheck.py` 的非真空模式(glob/dict 匹配空集不许过)。

**实现注**:泛化后须核验**单实例 cohort 的 `_ALL_SERVICE_MANIFESTS`(11)/`_SECRET_SERVICE_MANIFESTS`(6)与 `test_builtin_*` 全量 rglob 测试不因新增 mysql.replication_lag 而红**(继承 spike D-6 的 rglob 消费面核验:静态 capability gate 通过 + 全量注册 `errors==[]` + 单实例计数冻结);mysql.replication_lag 参数名回避多实例子串。

### W-6:postgres 录制验证门(条件交付)

**决策**:postgres **不预先承诺**。设一个录制验证门任务:起 `pg-repl-primary` + `pg-repl-standby`(streaming replication),验证副本侧 `now()-pg_last_xact_replay_timestamp()` 能否干净归一成 apply_lag 三元组——关键是 **idle-guard**。**候选判据(由门验证,非已证成立)**:主库空闲(无在途事务)时 `now()-replay_ts` 会虚高,collector 据 `pg_is_in_recovery()`(确认是 standby)+ `pg_last_wal_receive_lsn()==pg_last_wal_replay_lsn()` 判定**本地无 apply 积压**→ 滞后 0/null,而非吐虚高秒数。**已知漏洞(门必须解决)**:① `receive==replay` 只证**本地已应用完所收到的**,**不证 standby 与 primary 同步**——若 receiver 断开/上游 stalled,本地可相等而真实 source 滞后未知;② `pg_last_wal_receive_lsn()` 在**未在流式复制**时返回 NULL。故干净的判据可能须额外:receiver 健康检查 / source-side LSN(心跳)比对 / 无法证明 source currency 时归一成 `lag_seconds=null`。门要判的正是「这套补丁后能否干净归一」。

- **门通过(干净)**:铺 `postgres.replication_lag.yaml` + pg 双轨 fixture + 纳入 crosscheck 枚举 + 覆盖矩阵 postgres 单元格=delivered。
- **门撑破**(idle-guard 不可靠 / 须主库侧 `pg_stat_replication` 聚合才准):design/tasks **记录推 wave-3**(单独 postgres spike),覆盖矩阵 postgres 单元格=deferred,本批次 **mysql-only 交付**。

**理由**:D-8 已判 postgres 形态分叉;不赌、用真实录制裁定。门是「先证后铺」在批次内的落点——证不干净就不铺,提案不因此失败(mysql 仍交付)。

**替代方案**:直接铺 postgres 赌 timestamp 路径干净——否决(D-8 已标分叉,赌输要返工);直接推 wave-3 不验证——否决(可能错过一个其实能干净沿用的路径,门成本低)。

### W-7:覆盖矩阵 append-only 冻结(spec delta 形态)

**决策**:spec delta = 对 `replication-inspector-contract` **ADD** 一条「复制 inspector 覆盖矩阵随 wave 追加冻结」需求(mirror `service-inspector-suite` 的追加式 cohort),记录 redis(spike, link_freshness)/ mysql(wave-2c, apply_lag)/ postgres(门结果)三单元格 + 复制 crosscheck 枚举全部已交付复制 inspector。**不 MODIFY 契约既有 7 需求**(避免 RENAMED 工作流;mysql 直接遵守它们,由泛化 crosscheck 证明)。append-only:后续 wave 只追加单元格,不回溯改 redis/mysql 已冻结单元格。

**理由**:既有 7 需求是 DB-agnostic 契约,mysql 遵守即可、无需改;wave-2c 真正新增的是「覆盖了哪些 DB」这一 cohort 事实——用 ADD 覆盖矩阵需求承载,与 suite 的 wave cohort 同构、且 all-ADDED 避免归档 RENAMED 坑。

## 风险 / 权衡

- **[mysql 复制录制比 redis 重(binlog/server-id/复制账户/CHANGE REPLICATION SOURCE)]** → compose 多几行、录制器多几步,但确定性可控(poll `Replica_IO/SQL_Running` + `Seconds_Behind_Source`,非固定 sleep);只在录制 lane 跑,CI 回放。
- **[`Seconds_Behind_Source` 的 NULL 条件须精确,否则 lagging fixture 录不到]** → NULL = applier 停 OR (applier 追平且 receiver 停);**活跃追赶期返正整数**。归一成 `lag_seconds=null` + 据 `Replica_*_Running` 定 `link_healthy`,不当 0;`lagging` fixture **只能**在 `START REPLICA SQL_THREAD` 后、applier **运行且落后**的追赶窗口取一个非 NULL 的 `>=阈值` 值冻结(W-4),**不能**在 SQL_THREAD 停期间录(那时 SBS=NULL)。
- **[版本列名分叉 `Slave_*`/`Replica_*` + `Seconds_Behind_Master`/`Source`]** → compose pin `mysql:8.0.40`(→ `Replica_*`/`Source`);collector 注明按目标版本择名,tags 声明 `mysql8`。
- **[postgres 门可能撑破]** → 这正是门的意义;撑破推 wave-3、mysql 仍交付,提案不失败。
- **[复制 crosscheck 泛化触动单实例 cohort 计数 / rglob 全量测试]** → W-5 实现注核验 11/6 冻结 + 全量 rglob 主动通过(继承 spike D-6 的精确核验)。

## 门裁定(task 5.1 结果)

### W-6 裁定:postgres 副本侧 timestamp 路径**撑破** → **deferred-to-wave-3**(本批次 mysql-only)

对真实 pg16 primary+standby streaming replication 录制验证(`pg_basebackup -Xs -R` 拉起 standby,trust 复制),三态归一证据:

| 状态 | `pg_is_in_recovery()` | `recv==replay` | `now()-pg_last_xact_replay_timestamp()` | `pg_stat_wal_receiver` streaming | 朴素 idle-guard 判定 | 真相 |
|---|---|---|---|---|---|---|
| idle(已同步) | t | t | NULL→随空闲增长 | 1 | lag≈0 | 同步 ✓ |
| 有 workload | t | t | active 0.0–0.1 / 停写后 2.1↑ | 1 | lag≈0(idle-guard) | 大致 ✓ |
| **receiver 断(primary 停)** | t | **t** | 10.4→22.5↑ | **0** | **lag=0(假健康)** | **任意滞后、未知** ✗ |
| standalone(从未流式) | f | recv_lsn=**NULL** | — | — | configured=false | 单机 ✓ |

**裁定理由**:
- **漏洞①实证成立**:receiver 断开(primary 宕/网络分区)后 `pg_last_wal_receive_lsn()==pg_last_wal_replay_lsn()` 仍为 TRUE(本地追平了「收到的」WAL),候选 idle-guard `pg_is_in_recovery() && recv==replay → 0/null` 因此**捏造 lag=0/健康**,而 standby 实际已任意滞后于 primary。唯一能揭穿的是 `pg_stat_wal_receiver.status='streaming'`(断开时 =0)——这是与 lag 指标**不同的另一个系统视图**,把 mysql 单条 `SHOW REPLICA STATUS` 的采集面**扩大**成「lag 指标 + receiver 健康」两查询,且 `link_healthy` 必须先门控 lag 才安全。
- **漏洞②实证成立**:standalone(从未流式)`pg_last_wal_receive_lsn()` 返回 NULL,故 `replication_configured` 只能据 `pg_is_in_recovery()` 派生,不能据 recv_lsn。
- 即便补齐 receiver 健康检查,**真正有意义的 apply-lag 窗口(`replay < recv` 且仍 streaming)在本次 workload 下从未出现**(应用始终追平);postgres 副本侧没有 mysql `Seconds_Behind_Source` 这种「单值秒数」等价物。诚实稳健的 postgres apply-lag 是**主库侧** `pg_stat_replication.replay_lag`(interval,源端测量)——属契约前向规则的**主库侧聚合 inspector**,本批次非目标。

→ 不为凑数强塞撑破契约的副本侧 timestamp 路径(继承「先证后铺、不强塞」)。postgres 单元格冻结为 **deferred**;wave-3 起独立 postgres 复制 inspector(优先评估主库侧 `pg_stat_replication.replay_lag` 路径)。复制 cohort 计数守卫定为 **2**(redis+mysql)。

## 未决问题

- mysql `lagging` recipe 的积压大小留录制时定,以「`START REPLICA SQL_THREAD` 后追赶窗口内能稳定 poll 到非 NULL 的 `Seconds_Behind_Source>=阈值` 且 `Replica_IO_Running==Yes && Replica_SQL_Running==Yes`」为准(积压须够大让 SBS 在阈值上停留 ≥ 一个 poll 周期)。**录制实测**:21 次 `INSERT...SELECT` 倍增(~2.1M CHAR(255) 行)使追赶期 SBS 稳定 latch 到 ≥30(峰值 40、停留 ~20s);2^19 行秒级追平、SBS 不达 30——见 `_record_mysql_replication_lag._BACKLOG_DOUBLINGS`。
- ~~postgres idle-guard 的精确判据~~ → 已由上「门裁定」结论:撑破,deferred-to-wave-3。
- ~~复制 cohort 计数守卫最终值~~ → 已定 **2**(redis+mysql)。
