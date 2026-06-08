## 新增需求

### 需求:复制 inspector 覆盖矩阵随 wave 追加冻结

`replication-inspector-contract` **必须**维护一个**覆盖矩阵**,记录每个已交付复制 inspector 的 DB、采集路径与 `lag_seconds` 语义类;矩阵**追加式冻结**——每个 wave 只**追加**单元格,**禁止**回溯 MODIFY 已冻结单元格(mirror `service-inspector-suite` 的 cohort 冻结纪律)。已冻结单元格:

- **redis**(spike 交付):`INFO replication` 副本侧,语义类 `link_freshness`。
- **mysql**(wave-2c 交付):`SHOW REPLICA STATUS` 副本侧,语义类 `apply_lag`。
- **postgres**(wave-2c 录制验证门 → **撑破,deferred-to-wave-3**):副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard 不能干净归一(receiver 断时 `recv==replay` 仍 TRUE → 捏造 lag=0;`recv_lsn` 未流式时 NULL;真有意义的 apply-lag 是主库侧 `pg_stat_replication.replay_lag`),见 design「门裁定」。本批次 **mysql-only**,postgres 单元格冻结为 **deferred**,wave-3 起独立 postgres 复制 inspector(优先评估主库侧路径)。

矩阵内每个已交付复制 inspector **必须**遵守本契约既有的全部需求(继承单实例契约 / 归一三元组 + 声明语义类 / 三态 by-finding / 副本侧 identity / 两个语义不同 semantic-abnormal / 不进单实例 cohort)。`lag_seconds` 的语义类**随单元格而异且不可直接跨 DB 比较**(redis `link_freshness` ≠ mysql/pg `apply_lag`);本需求**禁止**抹平该差异。

#### 场景:mysql 单元格按 apply_lag 归一并声明语义类

- **当** `mysql.replication_lag` 对一个配置了复制的副本采集
- **那么** collector 从 `SHOW REPLICA STATUS`(8.0.22+;5.7–8.0.21 `SHOW SLAVE STATUS`)归一出 `replication_configured=true`、`link_healthy`=`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(5.7 `Slave_*`)、`lag_seconds`=`Seconds_Behind_Source`(5.7 `Seconds_Behind_Master`;**NULL 必须归一成 `lag_seconds=null` 而非 0**),语义类 `apply_lag` 在 `description` 与覆盖矩阵中声明

#### 场景:非复制 mysql 实例走未配置路径而非 exception

- **当** 对一个**未配置复制**的 mysql 实例采集(`SHOW REPLICA STATUS` 返回空结果集)
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空;collector **禁止**把「空结果集」当 fail-loud 而返回 `exception`(role-contextual fail-loud,对应 redis role:master standalone)

#### 场景:postgres 录制验证门裁定纳入或推 wave-3

- **当** wave-2c 对 postgres primary+standby 真实录制,验证副本侧 `now()-pg_last_xact_replay_timestamp()`(含主库空闲时的 idle-guard)能否干净归一成 `apply_lag` 三元组
- **那么** 干净归一**则**铺 `postgres.replication_lag` 并把 postgres 单元格冻结为 delivered(语义类 `apply_lag`);**否则**(idle-guard 不可靠 / 须主库侧聚合)在 design/tasks 记录推 **wave-3**、postgres 单元格记为 deferred,本批次 mysql-only 交付——**禁止**为凑数把撑破契约的 postgres 强塞进本批次

#### 场景:复制 crosscheck 枚举全部已交付复制 inspector 且单实例 cohort 不受影响

- **当** 运行 `test_replication_contract_crosscheck.py`
- **那么** 它**必须枚举**覆盖矩阵里全部已交付复制 inspector(redis + mysql〔+ postgres if 纳入〕)、对每条复跑继承的单实例契约项 + 复制专属项,并带**计数守卫**冻结复制 cohort 规模;新增复制 inspector **禁止**导致单实例 `_ALL_SERVICE_MANIFESTS`(11)/ `_SECRET_SERVICE_MANIFESTS`(6)计数变化或全量 rglob 测试(`test_builtin_capability_gate` / `test_builtin_inspectors`)误红
