## 为什么

基底 `service-inspector-contract`(2026-06-05 已归档)**显式把边界止于单实例**(见其「本契约边界止于单实例」需求:「primary/replica 角色识别与选择、replication lag 语义与单位归一、未配置复制与复制故障的区分、多副本指标聚合成标量、确定性制造 lag 的 fixture 录制——明确不在本契约范围」,且「本契约**禁止**被援引为多实例 inspector 的完备依据」)。

但 `replication_lag` 在 mysql / postgres / redis 各出现一次,共性是"查副本滞后→阈值",却引入单实例契约**未覆盖**的一整层维度:primary/replica 角色识别、副本自报 vs 主库聚合的连接选择、lag 语义与单位归一(秒 / 字节 / LSN 差)、**未配置复制 vs 复制故障**的区分、多副本聚合成标量、以及最难的——**确定性制造并冻结非零 lag 的 fixture 录制**(禁 `sleep` 竞态)。

复刻 M6「先证后铺」:**先用一个 replication spike 证明多实例 fixture-capability,而非三个 replication inspector 一起实现,也不直接推 wave-3**。spike 选**一种数据库**证明拓扑契约,再裁定另两种能否沿用。多实例契约从一个真例长出来,不预先为三种 DB 立法。

## 变更内容

- **选 redis 作探针**(候选 redis / postgres / mysql 中定):redis 副本侧 `INFO replication` 单连接自报 `role` / `master_link_status`(up/down)/ `master_last_io_seconds_ago`(秒),免特权、compose 最轻,足以证明 spike 的真正目标——**多实例 fixture-capability + 三态复制健康分类**。**诚实边界**:redis 副本侧单连接测得的 `master_last_io_seconds_ago` 是 **link-freshness(距上次主从 IO 的秒数,链路新鲜度)**,**不是数据 apply 滞后**(健康副本因主库周期 ping 而恒接近 0;真 apply 滞后需 offset 字节差 + 主连接,超副本侧单连接范围)。故本 spike 的 redis `lag_seconds` 是 freshness 语义——这正是 spike 要暴露的契约发现(lag 语义跨 DB 异构,见裁定)。实现**一个** `redis.replication_lag` inspector(`builtin/redis/replication_lag.yaml`)作为契约探针。
- **新立 `replication-inspector-contract`**(多实例契约,建立在单实例契约之上、继承其采集要求、在其显式排除的拓扑维度上扩展):
  - 归一成统一三元组 `(replication_configured, link_healthy, lag_seconds)`:`replication_configured`/`link_healthy` 两布尔跨 DB 真正同形;`lag_seconds` **形态统一**(秒 + null guard,DSL 比法一致)但**语义随 DB 而异**,每个 inspector **必须声明其 lag 语义类**(`link_freshness` / `apply_lag`)——redis=freshness,mysql/pg(wave-2c)=apply 滞后;契约**不假装语义统一**(见裁定);
  - **三态复制健康**:未配置复制(`role!=slave`)→ `ok` + `replication_configured=false` + 无 finding(standalone/primary 不是故障);配置但链路 `down` → finding;配置且 link up 但 lag_seconds 高 → finding。连不上副本/NOAUTH → `exception`(继承 base);缺 `redis-cli` → `requires_unmet`(继承 base);
  - **多副本聚合**:探针走副本侧自报(N=1,聚合=identity);契约**前向描述**主库侧归约方向(滞后取最大、链路取逻辑与),但**作为暂定规则、不冻结归约函数**,本 spike 不实现、不为其写测试,留 wave-2c 验证并可能修订;
  - **真实故障 fixture 录制法**:compose 起 redis-master + redis-replica(`replicaof`,pin `--repl-ping-replica-period 10` + 大 `--repl-timeout`),两个**语义不同**的 semantic-abnormal——链路断(停 master TCP 断开,poll 副本直到 `master_link_status==down`,冻结,`link_healthy=false`)+ 链路陈旧(master 异步 `DEBUG SLEEP`,poll 副本直到 `master_link_status==up` 且 `master_last_io_seconds_ago>=` 默认阈值,冻结,`link_healthy=true` 但 lag 高)。默认阈值 `warn_seconds=15`/`critical_seconds=30` 须高于 ping 周期(否则健康空闲副本误报)。录制 readiness 用 poll-until-condition,**禁固定 sleep**,冻结值让 replay 确定性。
- **裁定** spike 结论:**核心发现——统一 `lag_seconds` 跨 DB 语义异构**(redis 副本侧单连接是 link-freshness,mysql/pg 是 apply 滞后,不可直接跨 DB 比较);redis 已交付(freshness),mysql 高置信可沿用(`Seconds_Behind_Source` apply 滞后、含 NULL/版本列名注意),postgres lag 形态分叉(主库 `pg_stat_replication` 聚合 OR 副本 LSN/replay-timestamp,后者主库空闲时虚高须 guard)能否沿用须 wave-2c 真实录制验证,否则推 wave-3。结论写进 spec 与 design,作为 `add-replication-lag-inspectors`(wave-2c)的强条件依赖输入。

## 功能 (Capabilities)

### 新增功能
- `replication-inspector-contract`:多实例 / 复制 service inspector 的运行契约——单实例契约的**多实例扩展层**(继承不重复立法)。`redis.replication_lag` 是该契约的探针实现,由独立 `test_replication_contract_crosscheck.py` 验收(复验继承的单实例项 + 复制专属项)。

### 修改功能
- 无。`service-inspector-contract` 与 `service-inspector-suite` 均**不修改**:新契约以独立 capability 援引前者(继承),不触动其「边界止于单实例」需求;`redis.replication_lag` **不加入** `service-inspector-suite` 的单实例 cohort(尊重追加式冻结 + 保持单实例边界守卫完整)。

## 影响

- 新增 1 个 `redis.replication_lag` inspector(`builtin/redis/replication_lag.yaml`)+ redis master+replica compose(`tests/inspectors/compose/`)+ 录制入口(`_record_redis_replication_lag.py`)+ 录制 fixture(healthy / finding_trigger / **link_down semantic-abnormal** / **link_stale semantic-abnormal** / conn_refused;`requires_unmet` 经无二进制 stub 断言、无录制文件,故录制 fixture 共 5 个、其中 2 个真造 semantic-abnormal)。
- 新增 `tests/inspectors/test_replication_contract_crosscheck.py`(独立 cohort,复验继承的单实例契约项 + 复制专属项)。
- **对外契约影响**:预期零 manifest schema 变更、零新 parse format、零新 secret 机制(复用 `HOSTLENS_REDIS_PASSWORD`→`REDISCLI_AUTH` remap)。新增 1 个 capability spec。单实例 crosscheck 的计数冻结(11 / 6)**不变**。

## 非目标 (Non-Goals)

- ❌ 不一次实现三个 DB 的 `replication_lag`(只证 redis 一种,其余 wave-2c / wave-3)。
- ❌ 不预先为三种 DB 的拓扑/lag 语义立完备法(从 redis 一个真例逼出契约,postgres/mysql 沿用性留裁定;主库侧聚合、跨 DB lag 语义统一均**不**作为本 spike 冻结的 normative MUST)。
- ❌ 不测 redis 数据 **apply 滞后**(需 offset 字节差 + 主连接,超副本侧单连接;本 spike redis `lag_seconds` 是 link-freshness 语义)。
- ❌ semantic-abnormal fixture **禁止**用低阈值凑——**必须真造**两个语义不同的故障态:链路断(停 master)+ 链路陈旧(`DEBUG SLEEP <repl-timeout` 使 link up 但 freshness 退化)+ poll-until-condition 后冻结值。
- ❌ 不实现主库侧多副本聚合(契约**前向描述**其归约方向但不冻结归约函数,探针走副本侧 N=1;聚合实现 + 定稿留 wave-2c)。
- ❌ 不把 `redis.replication_lag` 塞进单实例 cohort(`_ALL_SERVICE_MANIFESTS` 保持 11、`_SECRET_SERVICE_MANIFESTS` 保持 6)。
- ❌ 不引入新 parse format / 新 secret 机制 / hook.py / 新 Python 运行期依赖。
