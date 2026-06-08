## 新增需求

### 需求:主库侧采集视角必须按冻结归约函数归约

当一个复制 inspector 的采集视角是**主库侧**(单连接读到**多行** per-replica 状态,如 postgres `pg_stat_replication`),其归约**必须**在 collector 内按下列**冻结的 normative 函数**完成(把契约此前的「前向暂定方向」升级为可验收、可测试的规则),归一出与副本侧同形态的三元组;DSL **只**比较归约后的三元组标量,**禁止**理解任何 DB 专有 per-replica 字段。归约函数:

- `replication_configured = (在线副本行数 > 0)`。
- `link_healthy = replication_configured ? AND_over_rows(单行链路健康) : false`(**链路逻辑与**:任一在线副本链路不健康即 `false`)。
- `lag_seconds = replication_configured ? max_over_rows(单行滞后秒数, 仅取 non-NULL 行) : null`(**滞后取最大**);若所有在线行滞后均 NULL **则** `lag_seconds=null`(无从测量,不当 0)。

**归约层与整数化(normative)**:多行归约**必须**下推进**单条 SQL 聚合**(`SELECT count(*), bool_and(coalesce(state::text,'') = 'streaming'), FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM pg_stat_replication`),由 collector 的 psql 一次往返返回**已归约的单行**,shell 只做空集短路与 JSON 成形——**禁止**让 shell 对多行做 awk 逐行归约(mysql 单行 awk 范例不覆盖多行,且 shell 浮点 max 易错)。

- **`bool_and` 的 NULL-state 三值逻辑必须显式中和(normative)**:SQL `bool_and` **忽略 NULL**——若某行 `state` 因欠权(`pg_monitor` 缺失,别人的 walsender 行 state 读成 NULL)或未来未知值读成 NULL,**裸** `bool_and(state='streaming')` 会**跳过该 NULL 行**,使「一行 streaming + 一行 NULL」聚合出 `t` → 假健康(实测 postgres 16:`bool_and over (true, NULL)=t`)。这绕过「未知 state 偏 fail-safe 判 false」意图(NULL 不是「未知枚举成员」,`state='streaming'` 对 NULL 求值是 NULL 不是 false)。故**必须**用 `coalesce(state::text,'') = 'streaming'` 把 NULL state 落进 **false**,使每行贡献非 NULL 布尔、`bool_and` 在 `count>0` 时恒非 NULL——NULL/未知/欠权 state 一律 `link_healthy=false`→critical(响错可接受,暴露权限问题,而非静默假健康)。**禁止**裸 `bool_and(state='streaming')`。
- **psql boolean 渲染必须映射成 JSON bool(normative)**:`psql -tA` 把 boolean 打成 `t`/`f`(**非** `true`/`false`、非 `1`/`0`,mirror mysql 显式 `Yes`→`true` 映射)。collector 的 JSON 成形步骤**必须**映射 `t`→`true`、其余(`f`)→`false`(空集已被 count 短路、不会落到这里);**禁止**把 `t`/`f` 直接塞进 JSON(`{"link_healthy":t}` 是非法 JSON → parse 崩 → 把健康主从录成 exception)。
- **`lag_seconds` 必须为整数**(契约 `output_schema` 是 `integer | null`):`EXTRACT(EPOCH FROM replay_lag)` 返浮点秒,**必须** `FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint` 取整(向下取整、单调,等价于先 floor 再 max);psql 把 SQL NULL 渲染成**空字符串**(非字面 `NULL`),collector 据 lag 字段为空 → 归一 `lag_seconds=null`;**禁止**输出带小数的 `lag_seconds`(会被 integer 校验 reject 或静默截断)。`replay_lag` 是 typed interval、经 `FLOOR(...)::bigint` 恒返 integer|NULL,故**无** mysql 那种字符串字段的「非数值 fail-loud」风险(唯一 fail-loud 是 psql ERROR 泄进 stdout,由 command-sub + 退出码捕获)。`replay_lag` 是主库**单时钟**测得(非跨时钟差),真负值不现实;若极端出现负值则 `FLOOR(负)` 更负、`>=warn` 为假 → 视健康,**接受**该残留、不额外裁定。

**归约正确性的验证层(normative,新技术非 mirror mysql)**:因 `ReplayTarget` 冻结的是 **collector 命令的最终 stdout(已归约三元组 JSON)**、回放时归约逻辑(SQL 聚合)**不重跑**,`max_over_rows`/`AND_over_rows` 的正确性**禁止**声称由回放 fixture 证明;**必须**由**录制时断言**保证。**注意这是一项新录制技术、不是 mirror mysql**:mysql 是 **N=1**(副本侧单值 `Seconds_Behind_Source`),其录制器只断言**已归约的标量本身**(`out["lag_seconds"]>=阈值`),**没有**多行 `max`/`AND` 可重算;postgres 的录制时 **raw-row 重算**是净新增逻辑,mysql 录制器只提供 compose/poll/脱敏脚手架、**不**提供可照抄的 reduction 重算。

录制时断言**必须同快照重算(normative)**:`replay_lag` 是**实时漂移**量(主库持续写 + apply_delay 下每刻不同),故**禁止**用「两次独立 psql 往返」(一次聚合、一次取 raw)再断言相等——两次往返跨不同 MVCC 快照、`replay_lag` 漂移会使断言 race/flaky 或假通过。**必须**用**单条查询在同一快照内同时取 raw 多行与聚合**(如 `WITH r AS (SELECT state, replay_lag FROM pg_stat_replication) SELECT json_agg(row_to_json(r)) AS raw, (SELECT count(*) FROM r), (SELECT bool_and(coalesce(state::text,'')='streaming') FROM r), (SELECT FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM r) FROM r LIMIT 1`),录制器在 Python 端从该 `raw` 独立算 `max(FLOOR(EPOCH))`/`all(state=='streaming')`,断言**等于同一查询返回的聚合列**——这验证的是 collector 所用聚合 SQL 的**逻辑**在一致快照上正确(独立于另被冻结的 collector 输出)。**禁止**把录制时断言退化成「只断言已归约三元组自身」(那样 reduction 等于自证、未被测)。多 standby fixture 的作用是「冻结一个由多行派生的三元组供 DSL/parse 回放 + 作录制时同快照重算的留痕证据」,**不是**在回放时证伪 reduction bug。

**单行链路健康的 per-DB 定义必须穷举该 DB 的状态全集(normative)**:主库侧 inspector **禁止**只论证"健康"状态而对其余状态隐式兜底——**必须**对该 DB per-replica 状态字段的**全部枚举成员**显式裁定每个成员算 `link_healthy` 真还是假。对 postgres,`pg_stat_replication.state` 枚举全集为 `{streaming, catchup, startup, backup, stopping}`:**仅 `streaming` 算单行链路健康**,其余四个成员(`catchup`/`startup`/`backup`/`stopping`)**一律**算 `link_healthy=false`(继承「意外/未知 state 值偏 fail-safe 判不健康」)。其中 `backup`(standby 正在 `pg_basebackup` 拉基线)与 `startup`/`stopping`(walsender 握手/关停瞬态)是**已知会被判 critical 的正常操作态**——本契约**接受**这组**误报**(rationale:非 streaming 的副本当下不提供 apply-lag 保护、值得在快照里浮出;瞬态在单次调度快照命中率低;给其宽限则 link_down 语义无处可造,破坏「两个语义不同 semantic-abnormal」)。该"接受的误报集"**必须**写进 description 与 design,**禁止**留作未论证的隐式行为。

**空集守卫(normative)**:collector **必须先**判 `count(*)`(在线副本行数)**再**采信任何聚合标量;`count(*)==0`(零在线副本)时 collector **必须**短路输出 `(replication_configured=false, link_healthy=false, lag_seconds=null)`,**禁止**采信空集上的聚合值。注意 SQL 聚合在空集上的真实返回:`SELECT count(*), bool_and(coalesce(state::text,'')='streaming'), max(...)` 对空 `pg_stat_replication` **返回一行** `(0, NULL, NULL)`(**`bool_and` over 空集是 NULL、不是 true**,coalesce 不改变此点——无行可聚合)——故 shell **必须**据 `count==0` 显式短路成 unconfigured,**禁止**把 `bool_and` 的 NULL 当真、也**禁止**把它当 `link_healthy=true`(无论走 SQL 的 NULL 还是 shell 逐行 `AND` 的 vacuous-true,空集都**禁止**漏出 `link_healthy=true`,把单机主库捏造成健康)。

**主库侧检测边界(normative)**:主库侧空结果集**无法区分**「单机主库(从未配置复制)」与「曾有副本、现全部断开/全挂」——两者 `pg_stat_replication` 都是空集。故主库侧 inspector **必须**把空集**一律**判为 `ok` 无 finding(继承「先证后铺、不假装能测撑破的东西」),且 spec/description **必须显式声明「主库侧 inspector 无法检测副本全断,该场景留给副本侧 receiver-health 或外部拓扑探测」**。该声明**必须同时注明**:此 fallback(副本侧 receiver-health inspector,如查 `pg_stat_wal_receiver`)在**本仓当前不存在、也无本期计划**——即 postgres apply-lag 链对「standby 全挂/指错到 standby」当前**无任何 inspector 兜底**,**禁止**让读者误以为存在该兜底。主库侧 inspector **禁止**引入「期望副本数 / application_name 列表」参数去检测全断(会撞契约禁用的多实例参数词、并把运维拓扑塞进 inspector 配置)。

本需求与既有「副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL」需求**并存**:副本侧视角(N=1)归约退化为 identity,主库侧视角(N 行)按本需求的冻结函数归约;两者都**必须**在 collector 内完成归约,**禁止**把 per-replica 明细回吐给 DSL。

#### 场景:postgres 主库侧多副本按冻结函数归约(SQL 聚合 + 整数化)

- **当** `postgres.replication_lag` 连接主库,`pg_stat_replication` 返回多行(每个在线 standby 一行)
- **那么** collector 经**单条 SQL 聚合**返回已归约单行:`replication_configured=(count(*)>0)`、`link_healthy=bool_and(coalesce(state::text,'')=='streaming')`(NULL state→false)、`lag_seconds=FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(**整数**;全行 `replay_lag` NULL → `lag_seconds=null`),shell 把 `t`→`true`/`f`→`false` 成形,DSL 只对该三元组标量比较

#### 场景:lag_seconds 必须整数化(EXTRACT EPOCH 浮点不得直出)

- **当** `max(replay_lag)` 经 `EXTRACT(EPOCH FROM ...)` 算出带小数的秒(如 `3.472814`)
- **那么** collector **必须** `FLOOR(...)::bigint` 取整成 `3` 再出,`lag_seconds` 形态恒为 `integer | null`(契约 output_schema);**禁止**直出浮点(会被 integer 校验 reject 或静默截断,使 max-of-rows 在亚秒抖动下不确定)

#### 场景:空集不得捏造健康(SQL 聚合 NULL 与 shell vacuous-true 都禁漏)

- **当** 主库侧 `pg_stat_replication` 返回零行(单机主库,或副本全断)——`SELECT count(*), bool_and(coalesce(state::text,'')='streaming'), max(...)` 此时**返回一行** `(count=0, bool_and=NULL, max=NULL)`(`bool_and` over 空集即便加 coalesce 仍是 NULL,因无行可聚合)
- **那么** collector **必须据 `count==0` 显式短路**输出 `(replication_configured=false, link_healthy=false, lag_seconds=null)`;**禁止**采信空集聚合值——`bool_and` over 空集是 `NULL`(不是 `true`),**禁止**当真或当 `link_healthy=true`;status=`ok`、无 finding

#### 场景:主库侧无法检测副本全断须显式声明边界

- **当** 一个曾有 standby 的主库其副本**全部**断开(`pg_stat_replication` 变空集)
- **那么** 主库侧 inspector 判为 `ok` 无 finding(与单机主库不可区分),且 spec/description **必须**显式声明「主库侧无法检测副本全断」;**禁止**为检测全断而引入期望副本数 / application_name 列表参数

#### 场景:滞后取最大、链路取逻辑与(max 与 AND 都须非平凡)

- **当** 主库侧多行,A `state='streaming'` 且 `replay_lag=3s`、B `state='streaming'` 且 `replay_lag=40s`(**两个 non-NULL 且 distinct** 的 streaming 行)、C `state` 为非 streaming(`replay_lag` NULL;录制取 `backup`——见 semantic-abnormal 场景的 recipe)
- **那么** `lag_seconds=40`(`max` 在两个 non-NULL 值间**取较大者 40 而非 3**——非平凡 max,**禁止**用「一行有值一行 NULL」的拓扑,那会让 max 退化成 identity 而无从验证)、`link_healthy=false`(`AND(streaming,streaming,非streaming)`——非平凡 AND,**false 来自 C 而非单行**);归约在 collector 内完成,DSL 不见 per-replica 行。此拓扑同时非平凡兑现 `max_over_rows`(≥2 distinct non-NULL)与 `AND_over_rows`(≥1 非 streaming 混 streaming),是 reduction 录制时断言的载体

#### 场景:catchup 行 replay_lag 常为 NULL 时 lag_seconds 反映 streaming 行

- **当** 主库侧两行,A `state='streaming'` 且 `replay_lag=2s`、B 为非 streaming 行(`backup`/`catchup`)且 `replay_lag=NULL`(非 streaming 行通常无 replay 时间样本,典型现实)
- **那么** `link_healthy=false`(B 非 streaming)、`lag_seconds=2`(B 的 NULL 被 max 跳过,只见 A);**故障由 `link_healthy=false` 的 critical 兜住、不靠 lag_seconds**——spec/description **必须**说明「`link_healthy=false` 时 `lag_seconds` 可能反映健康行而非落后行,仅作信息、不作故障判据」,**禁止**用乐观的「catchup 必有数值 lag」示例掩盖此点
- **且** 反过来,当 `link_healthy=false` 时即便某 streaming 行有**大滞后**(如 A streaming `replay_lag=50s` + B catchup),lag finding 的 `link_healthy` guard 为假 → **不**触发 lag finding,A 的 50s 真滞后**不在本快照体现**(只喷 link-down critical);这是 AND/critical-tier 模型的固有取舍,spec/description **必须**明示「混合拓扑下 `link_healthy=false` 会吞掉 streaming 行的真实滞后」,**禁止**让读者误以为 lag 维度在 link 不健康时仍生效

#### 场景:非 streaming 正常操作态被判 critical 属接受的误报

- **当** 一个 standby 正在 `pg_basebackup` 拉基线(其 walsender 行 `state='backup'`),或 walsender 处于 `startup`/`stopping` 瞬态
- **那么** `link_healthy=AND(state=='streaming')=false` → 产生 critical「link down」finding(**已知误报**:这些是正常操作态);本契约**接受**该误报,且 description 与 design **必须**把 `{catchup, startup, backup, stopping}` 显式列入「接受的误报集」并给 rationale;录制 healthy fixture 时**必须** poll 至该行 `state=='streaming'`(**禁止**在 `backup`/`startup` 窗口冻结 healthy,否则录进一个 critical 当健康)

#### 场景:NULL state(含部分欠权)经 coalesce 落 false 而非被 bool_and 吞

- **当** 多 standby 拓扑下**部分行** `state` 因欠权(对别人的 walsender 行)或未知值读成 NULL,另有真 streaming 行(裸 `bool_and(state='streaming')` 会跳过 NULL 行、聚合出 `t` 假健康)
- **那么** collector 的 `bool_and(coalesce(state::text,'')='streaming')` 把 NULL state 落进 **false** → `link_healthy=false` → critical(**响错**:暴露权限/异常,而非静默假健康);**禁止**裸 `bool_and(state='streaming')`(实测 postgres 16 `bool_and over (true,NULL)=t`)。该 coalesce 中和**消除** state 列欠权/全欠权的假健康路径(全欠权→所有行 false→critical)

#### 场景:streaming 但 replay_lag NULL 的残留可信度边界(仅 lag 列欠权 vs 空闲)

- **当** 某行 `state='streaming'`(state 列可见)但 `replay_lag` **单独**读成 NULL —— 二义:(a) 空闲已追平(真健康)/ (b) 巡检账户对 lag 列欠权(state 可见、仅 lag 列不可读;实际任意滞后)
- **那么** collector 归一输出 `link_healthy=true` + `lag_seconds=null`(形态不可区分该残留二义);经上一场景的 coalesce 中和后,**仅剩「state 可见而 lag 列单独 NULL」这一窄残留**靠 description 硬前提 `pg_monitor`/superuser 兜;**且** wave-3 录制门(tasks 1.2)**必须**实证欠权下 `state` 与 `replay_lag` 列各自的可见性——若该窄残留(state 可见、lag 列单独 NULL)实证可达,collector **必须**加防护分支并补回归 fixture,**禁止**把该已知假健康路径只留在 design 散文里而 spec 无痕、fixture 无守卫

## 修改需求

### 需求:副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL

副本侧自报视角(N=1,本 spike 探针所走)的聚合**必须**退化为 identity——输出即该副本的三元组,无需归约。任何复制 inspector **禁止**把多行 per-replica 明细回吐给 DSL 让其自行聚合(违反 authoring-contract"派生在 collector 内")——多副本归约**必须**在 collector 内完成。

**主库侧多副本视角的归约函数**(postgres `pg_stat_replication` 多行;redis `connected_slaves` 为潜在未来路径,当前已交付的 `redis.replication_lag` 走**副本侧** `INFO replication`、不是主库侧)由独立需求「主库侧采集视角必须按冻结归约函数归约」承载——该函数已被 wave-3(`postgres.replication_lag`)**冻结为 normative 并测试**(`lag_seconds` 取所有副本最大滞后、`link_healthy` 取所有副本链路逻辑与、空集守卫、主库侧检测边界)。本需求**只**规范副本侧 identity 退化;主库侧归约**禁止**在本需求重复立法,以兄弟需求为准。

#### 场景:副本侧 N=1 聚合为 identity

- **当** `redis.replication_lag` 连接单个副本读其自报 link/lag
- **那么** 聚合为 identity(N=1),输出即该副本的三元组,无需归约;collector **禁止**把 per-replica 明细交给 DSL

#### 场景:主库侧归约以冻结的兄弟需求为准

- **当** 评审一个主库侧复制 inspector(如 `postgres.replication_lag`)的多行归约
- **那么** 评审**必须**以「主库侧采集视角必须按冻结归约函数归约」需求为依据(滞后取最大 / 链路取逻辑与 / 空集守卫 / 检测边界);本「副本侧 identity」需求**不**承载主库侧归约的 normative 验收

### 需求:复制 inspector 覆盖矩阵随 wave 追加冻结

`replication-inspector-contract` **必须**维护一个**覆盖矩阵**,记录每个已交付复制 inspector 的 DB、采集路径与 `lag_seconds` 语义类;矩阵**追加式冻结**——每个 wave 只**追加**单元格,**禁止**回溯 MODIFY 已冻结单元格(mirror `service-inspector-suite` 的 cohort 冻结纪律)。已冻结单元格:

- **redis**(spike 交付):`INFO replication` 副本侧,语义类 `link_freshness`。
- **mysql**(wave-2c 交付):`SHOW REPLICA STATUS` 副本侧,语义类 `apply_lag`。
- **postgres**(wave-3 交付):**主库侧** `pg_stat_replication`,`lag_seconds = FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(整数化见「主库侧采集视角必须按冻结归约函数归约」需求),语义类 `apply_lag`。副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard 路径已被 wave-2c 录制门**否决**(receiver 断时 `recv==replay` 仍 TRUE → 捏造 lag=0;`recv_lsn` 未流式时 NULL),见 wave-2c design「门裁定」。postgres 是**首个主库侧单元格**,其多行归约遵守「主库侧采集视角必须按冻结归约函数归约」需求(空集→ok、主库侧测不到副本全断、`pg_monitor`/superuser 读 lag 列前提);本单元格**兑现了 spike 裁定的「postgres 形态分叉 → 推 wave-3」分支**(spike 历史裁定为待验证档,wave-3 经主库侧路径交付)。

矩阵内每个已交付复制 inspector **必须**遵守本契约既有的全部需求(继承单实例契约 / 归一三元组 + 声明语义类 / 三态 by-finding / 副本侧 identity 或主库侧冻结归约 / 两个语义不同 semantic-abnormal / 不进单实例 cohort)。`lag_seconds` 的语义类**随单元格而异且不可直接跨 DB 比较**(redis `link_freshness` ≠ mysql/pg `apply_lag`);本需求**禁止**抹平该差异。

#### 场景:mysql 单元格按 apply_lag 归一并声明语义类

- **当** `mysql.replication_lag` 对一个配置了复制的副本采集
- **那么** collector 从 `SHOW REPLICA STATUS`(8.0.22+;5.7–8.0.21 `SHOW SLAVE STATUS`)归一出 `replication_configured=true`、`link_healthy`=`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(5.7 `Slave_*`)、`lag_seconds`=`Seconds_Behind_Source`(5.7 `Seconds_Behind_Master`;**NULL 必须归一成 `lag_seconds=null` 而非 0**),语义类 `apply_lag` 在 `description` 与覆盖矩阵中声明

#### 场景:非复制 mysql 实例走未配置路径而非 exception

- **当** 对一个**未配置复制**的 mysql 实例采集(`SHOW REPLICA STATUS` 返回空结果集)
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空;collector **禁止**把「空结果集」当 fail-loud 而返回 `exception`(role-contextual fail-loud,对应 redis role:master standalone)

#### 场景:postgres 单元格按主库侧 apply_lag 归一并声明前提

- **当** `postgres.replication_lag` 连接**主库**,`pg_stat_replication` 返回在线 standby 行
- **那么** collector 归一出 `replication_configured=(行数>0)`、`link_healthy=bool_and(coalesce(state::text,'')=='streaming')`(NULL state→false)、`lag_seconds=FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(全 NULL→null),语义类 `apply_lag` 在 `description` 与覆盖矩阵中声明;`description` **必须**声明硬前提 `pg_monitor`(或 superuser,否则 lag 列读成 NULL → 静默假健康)与「指向 primary」(指向 standby 则 `pg_stat_replication` 空 → 假未配置)

#### 场景:postgres 主库侧空集走未配置路径而非 exception

- **当** 对一个**无在线 standby** 的 postgres 主库采集(`pg_stat_replication` 空结果集)
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空;collector **禁止**把空集当 fail-loud 而返回 `exception`(主库侧测不到副本全断,显式划边界);psql 非零退出 / 连不上 / 认证失败才走 `exception`,缺 `psql` 走 `requires_unmet`

#### 场景:postgres semantic-abnormal 两个 fixture 语义不同且主库侧拓扑造

- **当** 录制 `postgres.replication_lag` 的两个 semantic-abnormal fixture
- **那么** `link_down` **必须**用「行在、`state≠'streaming'`」(录制实证:`catchup` 在快速 loopback 太瞬态不可靠 latch,**改用可保持的 `backup` 态**——throttled `pg_basebackup --max-rate` 在 `pg_stat_replication` 持续显示一个 `state='backup'` walsender;`backup` 同属接受误报集、满足 `state != 'streaming'`)而**非**物理断开(物理断开使该行消失→空集→ok,测不到);`lagging` **必须**用 standby `recovery_min_apply_delay` + 主库持续写(poll 主库直到该行 `replay_lag>=` 默认 critical 阈值且 `state=='streaming'` → 冻结,`link_healthy=true` 且 lag 高);两 fixture 语义**禁止**重合(link_down 行 `state!='streaming'`、lagging 行 `state=='streaming'`),全程 poll-until-condition 禁固定 sleep
- **且** link_down finding 的 message **必须含子串「link down」**(大小写不限)——既有 `test_replication_contract_crosscheck.py` 对所有复制 inspector 的 link_down fixture 硬断言 `"link down" in findings[0].message.lower()`;postgres 即便语义是「非 streaming」,message 也**必须**写成如「PostgreSQL replication link down (standby not streaming)」以兼容该泛化断言,**禁止**只写「not streaming」而漏「link down」导致既有 crosscheck 变红

#### 场景:postgres 多行归约由录制时断言兑现(回放 fixture 不重跑归约)

- **当** 验收 ADDED「主库侧采集视角必须按冻结归约函数归约」需求的 `max_over_rows`/`AND_over_rows`
- **那么** 因 `ReplayTarget` 冻结的是 collector 已归约的三元组 stdout、回放**不重跑** SQL 聚合,reduction 正确性**必须由录制时断言**保证:录制器用**单条查询在同一 MVCC 快照内**同时取 raw 多行(`json_agg(row_to_json(...))`)与聚合三元组(`count`/`bool_and(coalesce...)`/`FLOOR(EXTRACT(EPOCH FROM max))::bigint`),在 Python 端从该 raw 独立算 `max(FLOOR(EPOCH))`/`all(state=='streaming')`,断言**等于同一查询的聚合列**(验证聚合 SQL 逻辑在一致快照上正确);**禁止两次独立往返**(`replay_lag` 实时漂移→断言 race)。**拓扑必须使 max 与 AND 都非平凡且在同一 fixture 内**:**≥2 个 non-NULL 且 distinct 的 streaming 行**(令 `max` 须在两真值间取较大者、非 identity;用 `recovery_min_apply_delay` 在 standby 上撑出 distinct lag——**禁止**「只一行有值、其余 NULL」,那让 max 退化成只对唯一 non-NULL 取值)+ **同一 fixture 内 ≥1 个非 streaming 行**(令 `AND` 从混合得 false、非单行 identity;录制取 `backup` 态 walsender——throttled `pg_basebackup`)——**禁止**把 max 与 AND 拆进两个 fixture(spec 要求单载体同时非平凡兑现二者)。录制实测载体:3 个 distinct-lag streaming standby(如 30/2/0 秒)+ 1 个 `backup` walsender,同一快照 `max=30`、`AND=false`。冻结的 `multi_replica` fixture 回放只供 DSL/parse + 作录制时同快照重算的留痕证据,**不**声称在回放时证伪 reduction bug。**这是 postgres 净新增技术、不是 mirror mysql**(mysql N=1 无多行可重算);**禁止**把录制时断言退化成「断言已归约三元组自身」(自证、reduction 未测)

#### 场景:postgres 未配置 / 空闲 NULL 两个稳态由真录制 fixture 守卫

- **当** 验收主库侧空集守卫(N5)与全行 NULL→null(N4)
- **那么** **必须**有真录制的 `unconfigured` fixture(单机主库或 0 在线 standby 的真 `pg_stat_replication` 空集,断言 `ok`/`(false,false,null)`/无 finding,守住 vacuous-true bug 的回归)与 `idle`(streaming 且 `replay_lag` NULL 的空闲稳态,断言 `link_healthy=true`/`lag_seconds=null`/无 finding);注入式 `_UNCONFIGURED_OK_STDOUT` 只测 DSL 旁路、**不**替代 collector 空集分支的真录制守卫

#### 场景:门实证欠权 NULL 后必须有下游强制门(不得静默不改)

- **当** wave-3 录制门(tasks 1.2)实证「`state` 列在欠权下仍可见而仅 lag 列 NULL」(W3-6 的 b 分支,静默假健康)
- **那么** 实现**必须**三件套兜底:① collector 加防护分支;② 补一个欠权-NULL 回归 fixture;③ crosscheck 加对应断言;**禁止**因「门实证结论是负面」就停留在 design 散文、manifest/fixture/crosscheck 三处无痕(负面实证结论必须有强制下游)

#### 场景:复制 crosscheck 枚举全部已交付复制 inspector 且单实例 cohort 不受影响

- **当** 运行 `test_replication_contract_crosscheck.py`
- **那么** 它**必须枚举**覆盖矩阵里全部已交付复制 inspector(redis + mysql + postgres)、对每条复跑继承的单实例契约项 + 复制专属项,并带**计数守卫**冻结复制 cohort 规模为 **3**;新增 postgres 复制 inspector **禁止**导致单实例 `_ALL_SERVICE_MANIFESTS`(11)/ `_SECRET_SERVICE_MANIFESTS`(6)计数变化或全量 rglob 测试(`test_builtin_capability_gate` / `test_builtin_inspectors`)误红
