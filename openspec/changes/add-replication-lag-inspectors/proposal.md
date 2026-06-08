## 为什么

M6 wave-2 的「多实例 replication」铺量批次(wave-2c)。`add-replication-inspector-spike`(已归档)用 redis 证明了多实例契约 `replication-inspector-contract`,并产出裁定(spike D-8):

- **mysql** —— ✅ 高置信可沿用:`SHOW REPLICA STATUS` 的 `Seconds_Behind_Source` 副本侧自报、单位即秒、语义即 **apply_lag**(数据应用滞后),与契约三元组贴合,只多一个复制账户授权前提。→ 本批次**机械铺**。
- **postgres** —— ⚠️ 形态分叉:lag 既可副本侧 `pg_last_xact_replay_timestamp`(得秒、但主库空闲虚高须 guard)、又可主库侧 `pg_stat_replication` 聚合。沿用性未经真实录制验证。→ 本批次设**录制验证门**:干净归一则同批纳入,撑破契约则推 **wave-3**。

兑现 spike 的核心发现——**lag 语义跨 DB 异构**:redis 的 `lag_seconds` 是 `link_freshness`,本批次 mysql 的是 **apply_lag**,两者不可直接跨 DB 比较;每个 inspector 声明其语义类(契约已要求)。

## 变更内容

- **mysql.replication_lag**(`builtin/mysql/replication_lag.yaml`,**本批次承诺交付**):副本侧 `SHOW REPLICA STATUS`(8.0.22+;5.7–8.0.21 `SHOW SLAVE STATUS`)归一成契约三元组——`replication_configured`=有复制行(空结果集→false)、`link_healthy`=`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(5.7 `Slave_*`)、`lag_seconds`=`Seconds_Behind_Source`(5.7 `Seconds_Behind_Master`,**NULL→null** 不当 0;NULL 条件=applier 停 OR applier 追平且 receiver 停,活跃追赶期返正整数)、语义类 **apply_lag**。role-contextual fail-loud(非复制实例空结果集→unconfigured `ok`、非 exception)、secret 复用 `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` remap(逐字对齐 `mysql.connection_usage`)、双轨 fixture(两个语义不同的真造 semantic-abnormal:链路断 `Replica_IO_Running=No` + 真实 apply 滞后〔`START REPLICA SQL_THREAD` 后追赶窗口、applier 运行且落后〕`Seconds_Behind_Source` 高)。
- **postgres.replication_lag**(**条件交付**,由 task 5.2 gate 裁定):**本批次承诺的是「跑这个门」,不是「交付 postgres inspector」**。起 pg primary+standby,验证副本侧 `now()-pg_last_xact_replay_timestamp()`(含 **idle-guard**:无在途事务时滞后视为 0/null)能否干净归一成 apply_lag 三元组。门有**二值结局、不留 TBD**:**干净** → 同批铺 `postgres.replication_lag` + 双轨 fixture + 纳入 crosscheck + 覆盖矩阵(cell=delivered、cohort 计数 `==3`);**撑破**(idle-guard 不可靠 / 须主库侧聚合)→ 推 **wave-3**(cell=deferred、cohort 计数 `==2`),本批次 **mysql-only 交付**。归档前 task 5.2 必把覆盖矩阵 postgres cell 落定为 delivered 或 deferred 之一。
- **复制 crosscheck 泛化**:把 `test_replication_contract_crosscheck.py` 从 redis 单 manifest 泛化为**枚举所有已交付复制 inspector**(redis + mysql〔+ postgres if 门通过〕)参数化,对每条复跑继承的单实例契约项 + 复制专属项;计数守卫冻结复制 cohort 规模。
- **不重复 redis**(spike 已交付 `redis.replication_lag`,本批次不动)。

## 功能 (Capabilities)

### 修改功能
- `replication-inspector-contract`:**ADD** 一条「复制 inspector 覆盖矩阵随 wave 追加冻结」需求(mirror `service-inspector-suite` 的追加式 cohort 模式),记录 redis(spike)/ mysql(wave-2c, apply_lag)/ postgres(门结果)三个单元格 + 复制 crosscheck 枚举全部已交付复制 inspector。**不修改**契约既有 7 条需求(mysql 直接遵守它们,由泛化 crosscheck 机械证明)。

### 新增功能
- 无新 capability(复用 spike 立的 `replication-inspector-contract`,只追加覆盖矩阵需求)。

## 影响

- 新增 `builtin/mysql/replication_lag.yaml` + mysql primary+replica compose(binlog/server-id/复制账户)+ `_record_mysql_replication_lag.py` + 双轨 fixture + per-probe snapshot 测试。
- 泛化 `test_replication_contract_crosscheck.py`(redis 单条 → 枚举 redis+mysql〔+postgres〕)。
- postgres:视门结果——`builtin/postgres/replication_lag.yaml` + compose + recorder + fixture(干净)或 design/tasks 推 wave-3 记录(撑破)。
- **对外契约影响**:零新 parse format / 零新 secret 机制(复用 `HOSTLENS_MYSQL_PWD`/`HOSTLENS_POSTGRES_PASSWORD` remap);`replication-inspector-contract` +1 覆盖矩阵需求;单实例 cohort 计数(11/6)**不变**(复制 inspector 不进单实例 cohort,继承 spike D-6)。

## 非目标 (Non-Goals)

- ❌ 不重复 spike 已交付的 `redis.replication_lag`。
- ❌ 不强铺撑不住的 postgres——门撑破即推 **wave-3**,不为凑数塞进本批次(继承「先证后铺、不强塞」)。
- ❌ 不假装 mysql 的 `lag_seconds`(apply_lag)与 redis 的(link_freshness)可跨 DB 比较——契约要求声明语义类。
- ❌ semantic-abnormal fixture **禁止**用低阈值凑——**必须真造**两个语义不同的故障(链路断 `Replica_IO_Running=No` + 真实 apply 滞后〔applier 运行且落后、SBS 非 NULL〕),poll-until-condition、禁固定 `sleep`;**禁止** applier 停期间录 lagging(SBS=NULL 物理不可能,W-4)。
- ❌ 不引入新 parse format / 新 secret 机制 / hook.py / 新 Python 运行期依赖。
- ❌ 不修改 `replication-inspector-contract` 既有 7 需求、不修改 `service-inspector-contract` / `service-inspector-suite`、不把复制 inspector 塞进单实例 cohort。
- ❌ 不实现主库侧多副本聚合(契约前向规则,留真有主库侧 inspector 时)。
