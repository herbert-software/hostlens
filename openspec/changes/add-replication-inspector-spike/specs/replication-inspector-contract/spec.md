## 新增需求

### 需求:复制 inspector 契约建立在单实例契约之上并继承其采集要求

`replication-inspector-contract` **必须**以独立 capability 建立在 `service-inspector-contract` 之上,**继承**其全部单实例采集要求——连接参数注入安全三件套、secret 经 `HOSTLENS_*` 声明并 remap 到 client 原生 env 通道(从不进 argv、从不 `{{ }}` 插值)、service 层失败三态(`requires_unmet` / `exception` / `ok`)、超时与输出纪律、跨 local 与 SSH target 无分叉。本契约**禁止**重复立法这些已有要求,只**补充**单实例契约显式排除的多实例 / 复制维度。一个复制 inspector **禁止**绕过本契约直接援引单实例契约作为完备依据。

#### 场景:继承项不重复立法但被复跑验证

- **当** 一个复制 inspector(如 `redis.replication_lag`)落地
- **那么** 它的连接注入安全、secret remap、失败三态、超时、无分叉**必须**由其独立 crosscheck 测试**复跑**断言通过(继承被机械证明,而非仅在文档里声明),且本契约 spec **不**复制这些要求的条文

#### 场景:禁止援引单实例契约为多实例完备依据

- **当** 评审一个多实例 / 复制 inspector 是否满足契约
- **那么** 评审**必须**以 `replication-inspector-contract` 为依据;仅满足 `service-inspector-contract`(单实例)**不**构成多实例 inspector 的完备合规证明

### 需求:复制 inspector 必须归一出统一形态三元组并声明 lag 语义类

每个复制 inspector 的 `output_schema` **必须**归一出统一**形态**的三元组,无论底层 DB 的原始信号形态:
- `replication_configured: bool` —— 本实例是否处于复制关系(跨 DB 真正同形)。
- `link_healthy: bool` —— 复制链路当前是否正常(跨 DB 真正同形)。
- `lag_seconds: integer | null` —— 副本侧可测得的滞后秒数;未配置复制或无法测量时为 `null`。

`lag_seconds` 的**形态统一**(秒 + null guard,DSL 比法跨 DB 一致),但其**语义随 DB 而异**:每个复制 inspector **必须**在 manifest `description` 与其 spec 中**显式声明** `lag_seconds` 的语义类——`link_freshness`(链路新鲜度,如 redis `master_last_io_seconds_ago`=距上次主从 IO 的秒数,**非**数据 apply 滞后)或 `apply_lag`(数据应用滞后,如 mysql `Seconds_Behind_Source` / postgres replay-timestamp 差)。本契约**禁止**假装 `lag_seconds` 跨 DB 语义统一;两个语义类**不可直接跨 DB 比较**(详见「裁定」需求)。各 DB 原始信号**必须**在各自 collector 命令内换算成该形态三元组。Finding DSL **只允许**对三元组标量做比较;**禁止**让 DSL 理解任何 DB 专有原始字段或单位。

#### 场景:redis 副本归一出三元组并声明 freshness 语义

- **当** `redis.replication_lag` 对一个 `role:slave`、`master_link_status:up`、`master_last_io_seconds_ago:3` 的副本采集
- **那么** collector 输出 `replication_configured=true`、`link_healthy=true`、`lag_seconds=3`(语义类 `link_freshness`,在 description 与 spec 中声明),且 finding 规则只对这三个标量比较

#### 场景:无法测量时 lag 为 null 而非 0

- **当** 实例未配置复制(`replication_configured=false`)
- **那么** `lag_seconds` **必须**为 `null`(而非伪造的 `0`),以区分"没有滞后"与"无从测量"

#### 场景:redis 同步哨兵 -1 归一为 null

- **当** redis 副本在初始同步 / 链路重建瞬间吐 `master_last_io_seconds_ago:-1`
- **那么** collector **必须**把 `-1` 归一成 `lag_seconds=null`(无从测量),**禁止**输出 `lag_seconds=-1`(否则 `-1>=阈值` 永假、把"正在同步"误当健康)

### 需求:复制 inspector 必须区分未配置复制 / 复制故障 / 复制滞后三态

在继承的失败三态(`requires_unmet` / `exception` / `ok`)之上,复制 inspector **必须**在 `ok` 内部按复制语义再分三态,且该区分**必须由 finding 规则表达,禁止污染 status**:
- **未配置复制**(`replication_configured=false`,如 standalone 或 primary):status **必须**为 `ok`,且**禁止**产生任何 finding(合法单机不是故障)。
- **配置但链路断**(`replication_configured=true && link_healthy=false`):status `ok`,**必须**产生 critical finding。
- **配置且滞后**(`link_healthy=true && lag_seconds>=阈值`):status `ok`,**必须**按 `lag_seconds` 产生 warn / critical finding。

连不上副本 / 认证失败**必须**走继承的 `exception`(collector fail-loud:非零退出 + 空 stdout);缺 client 二进制**必须**走 `requires_unmet`。**禁止**把"未配置复制"映射成 `exception`,也**禁止**把"链路断"吞成无 finding 的 `ok`。**fail-loud 必须按 role 上下文**:未配置实例(如 redis `role:master`)的采集输出**本就缺** replica-only 字段(链路/lag 字段),collector **禁止**把这种"缺字段"当 fail-loud 而误判 `exception`——**必须**先据 role 判定未配置后走 `ok` + `replication_configured=false` 路径;只有处于复制关系的实例(`role:slave`)而其链路/lag 字段缺失才算真异常。

#### 场景:未配置复制不告警

- **当** 对一个 `role:master`(无 `master_host`、无 `master_link_status`/`master_last_io_seconds_ago` replica-only 字段)的 standalone redis 采集
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空(不产生假告警);collector **禁止**把缺失的 replica-only 字段当 fail-loud 而返回 `exception`

#### 场景:链路断产生 critical

- **当** 副本 `master_link_status:down`(`replication_configured=true && link_healthy=false`)
- **那么** status=`ok` 且产生一条 critical finding「replication link down」

#### 场景:连不上副本走 exception 而非伪造健康

- **当** redis-cli 连副本失败 / NOAUTH(非零退出 + 空 stdout)
- **那么** status=`exception`,**禁止**伪造一个 `link_healthy=true` 的健康结果

### 需求:副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL

副本侧自报视角(N=1,本 spike 探针所走)的聚合**必须**退化为 identity——输出即该副本的三元组,无需归约。任何复制 inspector **禁止**把多行 per-replica 明细回吐给 DSL 让其自行聚合(违反 authoring-contract"派生在 collector 内")——多副本归约**必须**在 collector 内完成。

**前向(暂定)规则,本 spike 不冻结、不实现、不测试**:当一个复制 inspector 的采集视角覆盖多个副本时(主库侧视角,如 redis `connected_slaves` / postgres `pg_stat_replication` 多行),归约**方向**为 `lag_seconds` 取所有副本最大滞后、`link_healthy` 取所有副本链路逻辑与(任一断即 `false`)。该归约**函数**的精确定义(是否需滞后副本计数等)留 wave-2c 真有主库侧 inspector 时验证并可能修订,本 spike 仅前向描述方向,**不作为冻结的 normative 验收**。

#### 场景:副本侧 N=1 聚合为 identity

- **当** `redis.replication_lag` 连接单个副本读其自报 link/lag
- **那么** 聚合为 identity(N=1),输出即该副本的三元组,无需归约;collector **禁止**把 per-replica 明细交给 DSL

#### 场景:主库侧多副本归约方向为前向暂定规则

- **当** 设计一个未来的主库侧复制 inspector(本 spike 不实现)
- **那么** 其归约**方向**(滞后取最大、链路取逻辑与)由本契约前向描述,但精确归约函数**留 wave-2c 验证并可能修订**,本 spike 不为其冻结 normative 验收、不写测试

### 需求:复制 inspector 的 semantic-abnormal fixture 必须制造两个语义不同的真实故障且禁低阈值凑

每个复制 inspector **必须**附双轨 fixture(继承单实例契约的双轨要求):finding-trigger(健康拓扑 + 降低阈值,只证接线)与 semantic-abnormal(**真实**异常拓扑 + **默认**阈值触发,证检出能力)。复制 inspector 的 semantic-abnormal **必须**通过**真造**复制故障状态获得,**禁止**用降低阈值在健康副本上凑出 finding,且**必须**覆盖**两个语义不同**的故障分支——`link_healthy=false`(链路断)与 `link_healthy=true && lag_seconds>=阈值`(链路陈旧/滞后),两个 fixture 语义**禁止**重合(否则只测了一个分支)。录制 readiness **必须**用 poll-until-condition(poll 一个状态条件,如 `master_link_status==down` / `master_link_status==up && master_last_io_seconds_ago>=N`),**禁止**用固定 `sleep N` 等待。录制产物**必须**冻结(ReplayTarget 逐字回放),使 replay 不依赖时钟、snapshot 确定可复现。

#### 场景:真实链路断 fixture（link_down）

- **当** 录制 semantic-abnormal「链路断」
- **那么** 录制器建立真实 master+replica 复制 → poll 确认 link up → **停 master 容器**(TCP 断开,链路秒级判 `down`,**不依赖 `repl-timeout`**——`repl-timeout` 仅服务于 link_stale 的 link-up 保持)→ **poll 副本直到 `master_link_status==down`** → 冻结该快照(`link_healthy=false`);snapshot 在**默认**阈值下触发 critical

#### 场景:真实链路陈旧 fixture（link_stale）用 poll 而非 sleep 且与 link_down 语义不同

- **当** 录制 semantic-abnormal「链路陈旧」
- **那么** 录制器在 master 上异步 `DEBUG SLEEP <T>`、**T 大于默认 `critical_seconds` 且远小于 pin 的 `repl-timeout`**(冻结主事件循环、暂停发 ping 但不至判链路 down)→ **poll 副本(经另一连接)直到 `master_link_status==up` 且 `master_last_io_seconds_ago>=` 默认阈值** → 冻结该真实值(`link_healthy=true` 但 `lag_seconds` 高);默认阈值**必须**高于 `repl-ping-replica-period`(否则健康空闲副本误报);**禁止**固定 `sleep` 等值长出来;录制时**必须**断言本 fixture 的 `master_link_status==up`(与 link_down 的 `==down` 语义区分)

### 需求:复制 inspector 不进单实例 cohort 且须独立 crosscheck

复制 inspector **禁止**加入 `service-inspector-suite` 的单实例 cohort 与 `test_service_contract_crosscheck.py` 的 `_ALL_SERVICE_MANIFESTS` / `_SECRET_SERVICE_MANIFESTS`(显式 dict 枚举,其单实例计数冻结 11 / 6 **必须**保持不变)。复制 inspector **必须**由独立的 `test_replication_contract_crosscheck.py` 验收,该 crosscheck **必须**同时:(a) 复跑断言全部继承的单实例契约项;(b) 断言复制专属项(归一三元组在 output_schema、lag 语义类已声明、三态 by-finding、副本侧 N=1、`link_down` 与 `link_stale` 两个语义不同的 semantic-abnormal fixture 在默认阈值触发)。复制 inspector 的参数名**禁止**引入多实例词(`replica`/`primary`/`replication`/`lag`/`instances`/`nodes`),其多实例语义**必须**体现在 output_schema 三元组与 fixture 拓扑,而非参数名。新增复制 inspector 文件会被 builtin 全量测试(均经 `rglob` 枚举全部 builtin yaml——`test_builtin_capability_gate.py` 直接 `rglob`、`test_builtin_inspectors.py` 经全量 registry 构建间接 `rglob`)自动纳入,**必须主动通过**(而非仅"计数不误红"):①静态 capability gate(只声明静态 `requires_capabilities`);②全量注册 `errors == []`(干净加载)。

#### 场景:被全量测试纳入仍主动通过

- **当** `redis.replication_lag.yaml` 落地并被 `test_builtin_capability_gate.py`(直接 rglob)/ `test_builtin_inspectors.py`(经 registry 构建)自动扫描
- **那么** 它**必须**通过静态-capability 断言(只声明 `requires_capabilities:[shell]`)且全量注册 `errors == []`(干净加载),既有 wave cohort 子集断言与宽松计数下界不因多一个 builtin 而误红

#### 场景:单实例计数冻结不变

- **当** `redis.replication_lag` 落地
- **那么** `test_service_contract_crosscheck.py` 的 `_ALL_SERVICE_MANIFESTS`(11)与 `_SECRET_SERVICE_MANIFESTS`(6)计数**保持不变**,该文件不枚举复制 inspector

#### 场景:独立 crosscheck 复跑继承项

- **当** 运行 `test_replication_contract_crosscheck.py`
- **那么** 它对 `redis.replication_lag` **复跑**注入安全 / secret remap 不进 argv / 失败三态 / 超时 / 无分叉,并断言归一三元组、lag 语义类声明、`link_down` 与 `link_stale` 两个语义不同的 semantic-abnormal fixture 存在

### 需求:复制 spike 必须裁定契约可沿用性并记录 lag 语义异构发现

本 spike **必须**产出一条可归档的**裁定**,作为 wave-2c(`add-replication-lag-inspectors`)的范围依据。裁定**必须**首先记录本 spike 的**核心发现:统一 `lag_seconds` 跨 DB 语义异构**——redis 副本侧单连接的干净信号是 `link_freshness`(`master_last_io_seconds_ago`,非数据 apply 滞后;真 apply 滞后需 offset 字节差 + 主连接),mysql/pg 的是 `apply_lag`;两个语义类**不可直接跨 DB 比较**,契约**禁止**假装统一。在此基础上,对 lag 副本侧自报、与探针契约贴合的 DB **必须**判定为"可沿用,机械铺"(并标注其 lag 语义类与已知坑);对 lag 形态分叉、沿用性未经真实录制验证的 DB **必须**判定为"待 wave-2c 录制验证,否则推 wave-3"。裁定**禁止**停留在对话或 PR 描述里,**必须**写入 spec / design 以便 wave-2c 援引。

#### 场景:记录 lag 语义异构核心发现

- **当** 归档本 spike 的裁定
- **那么** 裁定**必须**显式声明 redis `lag_seconds` 是 `link_freshness`、mysql/pg 是 `apply_lag`、两者不可直接跨 DB 比较,并对 `add-replication-lag-inspectors` 骨架中 redis 行"offset 差"(apply-lag 路径,本 spike 不交付)给出 hand-off 更正

#### 场景:同形 DB 判为可沿用（带已知坑）

- **当** 裁定 mysql(`Seconds_Behind_Source` 副本自报、apply_lag 语义、单位秒)
- **那么** 记录"高置信可沿用,wave-2c 机械铺",给出 `Seconds_Behind_Source`→`lag_seconds`(语义类 `apply_lag`)、`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`→`link_healthy` 的映射,并标注已知坑:`Seconds_Behind_Source`(8.0.22+;5.7–8.0.21 为 `Seconds_Behind_Master`)在 IO 断开/追赶中会返回 NULL(须归一成 `lag_seconds=null` 而非 0)、IO/SQL 线程列名同步在 8.0.22 由 `Slave_*` 改 `Replica_*`(按目标版本择名)

#### 场景:分叉 DB 判为待验证或推 wave-3

- **当** 裁定 postgres(lag 既可副本侧 `pg_last_wal_replay_lsn`/`pg_last_xact_replay_timestamp`、又可主库侧 `pg_stat_replication` 聚合)
- **那么** 记录"形态分叉,沿用性待 wave-2c 真实录制验证;若主库侧聚合 + LSN/idle 换算撑破契约则推 wave-3",并标注坑:`now()-pg_last_xact_replay_timestamp()` 在主库空闲时虚高、须 guard(无在途事务时滞后视为 0/null),而非默认塞进 wave-2c
