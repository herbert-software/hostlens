## 上下文

M6 wave-2 把 inspector 套件从 OS 层(wave-1)推到 service 层。第一个 spike(`add-service-inspector-contract-spike`,2026-06-05 已归档)立了**单实例** `service-inspector-contract`,并**显式声明边界止于单实例**——把 replication 的整套维度(角色识别、lag 归一、未配置 vs 故障区分、多副本聚合、确定性 lag fixture 录制)留给一个独立 replication spike,「禁止被援引为多实例 inspector 的完备依据」。

`replication_lag` 在 redis / mysql / postgres 各有一个对应物,表面共性("查副本滞后→比阈值")掩盖了真正的难点:**lag 信号在三种 DB 里形态各异且语义不同类**(详见 D-1 / D-8),且**确定性制造非零 lag 比单实例的"低阈值触发"难一个量级**——需要真起 master+replica 拓扑、真打断/真拖慢复制、还不能用固定 `sleep` 凑(会 flake)。

基底单实例契约已 apply + archive(本提案的 gating 条件已满足)。本 spike 用 **redis 一种 DB** 把这层契约从一个真例长出来,再裁定 mysql/postgres 能否沿用——不预先为三种立法。

**复用基础**(均来自归档的单实例 spike,本 spike 不重新立法):
- `service-inspector-contract` 的注入安全三件套、`HOSTLENS_*`→client 原生 env remap、fail-loud 失败三态、超时与输出纪律、跨 local/ssh 无分叉。
- `inspector-fixture-recorder` 的双轨 fixture(finding-trigger + semantic-abnormal)、录制脱敏、冻结非确定性采样。
- `inspector-authoring-contract` 的"一切派生在 collector 内"、裸标量键 vs `results/items/records`、文档式版本前提声明。
- 录制 lane:`tests/inspectors/compose/docker-compose.yml` + `_compose_record.py`(healthcheck poll readiness,无固定 sleep)+ `_record_*.py` 入口。

## 目标 / 非目标

**目标:**
- 立 `replication-inspector-contract`(多实例契约),作为单实例契约的**继承式扩展层**:只补单实例契约显式排除的拓扑维度,不重复其采集要求。
- 用 `redis.replication_lag` 一个探针**真实证明**:副本侧复制健康自报 + 三态复制健康分类 + 确定性"真实链路断 / 链路陈旧"fixture 录制(禁固定 sleep)。
- 给 wave-2c(`add-replication-lag-inspectors`)一个明确**裁定**:哪些 DB 能沿用本契约、哪些(因 lag 语义形态分叉)推 wave-3,**并把"统一 lag_seconds 跨 DB 语义异构"这一发现固化进裁定**。

**非目标:**
- ❌ 不实现 mysql / postgres 的 `replication_lag`(只证 redis)。
- ❌ 不实现主库侧多副本聚合(契约把其归约函数列为**前向/暂定**规则,探针走副本侧 N=1;聚合实现 + 归约函数定稿留 wave-2c)。
- ❌ 不测 redis 的**数据 apply 滞后**(需 `master_repl_offset` − 副本已处理 offset 的字节差 + 主连接,超出副本侧单连接范围;见 D-1 / D-8)。本 spike 的 redis `lag_seconds` 是 **link-freshness(链路新鲜度)** 语义,非 apply 滞后。
- ❌ 不把 `redis.replication_lag` 加入单实例 cohort(`_ALL_SERVICE_MANIFESTS`/`_SECRET_SERVICE_MANIFESTS` 计数冻结不变)。
- ❌ 不引入新 manifest schema 字段 / parse format / secret 机制 / hook.py / 新 Python 运行期依赖。
- ❌ 不修改 `service-inspector-contract` / `service-inspector-suite` 任何需求。

## 决策

> 沿用基底 spike 的决策标号体例(D-n)。涉及继承项处显式引用基底决策(基底 D-3=失败分类、基底 D-4=双轨 fixture、基底 D-5=录制 lane),不复制其内容。

### D-1:选 redis 作探针 DB(及其 lag 语义的诚实边界)

**决策**:探针用 redis,而非 mysql / postgres。redis 探针测的是**副本侧单连接可得的复制健康信号**:`role`、`master_link_status`(up/down)、`master_last_io_seconds_ago`(距上次主从 IO 的秒数)。

**lag 语义的诚实边界(关键)**:redis 副本侧单连接**没有干净的"数据 apply 滞后秒数"**。`master_last_io_seconds_ago` 是**距上次收到主库 IO 的秒数**——一个 **link-freshness / 链路陈旧度** 信号,**不是**副本数据集落后主库多少。健康副本即便在写压力下该值也接近 0(主库按 `repl-ping-replica-period`(默认 10s)周期性 ping,持续复位它);它在链路沉默/卡顿时上升。真正的 redis 数据滞后是 `master_repl_offset`(主)− 副本已处理 offset 的**字节差**,而副本侧单连接拿不到主库的**当前** offset(副本 `INFO replication` 里的 `master_repl_offset` 是副本自身视角,非主库实时值),故真 apply 滞后需**主连接**,超出 D-2 的副本侧单连接范围。**∴ 本 spike 的 redis `lag_seconds` 明确是 link-freshness 语义**(见 D-3),与 mysql/pg 的 apply 滞后**不同类**(见 D-8)。

**理由**:即便如此,redis 仍是**阻力最小的探针**——单连接、免特权(不像 mysql `SHOW REPLICA STATUS` 需 `REPLICATION CLIENT`)、免 LSN 换算、compose 最轻(`replicaof` 一行),足以证明本 spike 的真正目标:**多实例 fixture-capability(真起 master+replica + 真造故障 + 冻结)+ 三态复制健康分类**。lag 语义不同类这一点不是 redis 的缺陷,而是 spike 要暴露的契约发现(D-8)——用 redis 反而最早暴露它。

**替代方案**:
- postgres:lag 形态分叉(主库 `pg_stat_replication` 聚合 vs 副本 `pg_last_wal_replay_lsn`/`pg_last_xact_replay_timestamp`),最能压力测试契约,但也最重、最易让 spike 卡在换算细节。→ 留作裁定对象(D-8)。
- mysql:`Seconds_Behind_Master`(8.0.22+ `Seconds_Behind_Source`)副本侧自报**真 apply 滞后秒数**,语义比 redis 更贴"滞后",但需建复制账户 + 授权,compose 更重,且字段在某些状态会 NULL/失真。→ 裁定为"可沿用"的高置信项(D-8)。

### D-2:副本侧自报 vs 主库侧聚合 —— 探针走副本侧

**决策**:`redis.replication_lag` 连接**副本**实例,读其自报的 link/freshness(N=1,聚合=identity)。契约**前向定义**主库侧聚合规则,但本 spike **不实现、不冻结其归约函数**。

**理由**:副本侧自报是三种 DB 的最大公共子集(redis/mysql 都副本自报,postgres 副本侧也有视图),证一条路即可立契约骨架。主库侧聚合(redis `connected_slaves` + 每个 `slaveN` 行,postgres `pg_stat_replication` 多行)引入"多行→标量"的聚合语义,是真正的多副本难点——契约**前向描述**其归约方向(滞后取最大、链路取逻辑与),好让 wave-2c 有据可依,但**作为暂定规则**(精确归约函数留 wave-2c 真有主库侧 inspector 时验证并可能修订),探针不背这个实现成本,本 spike 也不为其写测试。

**替代方案**:探针走主库侧(读 `connected_slaves` + per-slave offset 差)。否决:per-slave 解析把 collector 复杂度顶上去,且主库侧拿不到"本副本与主的链路是否 up"这种副本自身视角,反而偏离最常见的"在副本上巡检它是否掉队"诉求。

### D-3:复制健康归一成统一三元组(lag 语义随 DB 声明)

**决策**:契约规定每个复制 inspector 的 `output_schema` **必须**归一出三元组:
- `replication_configured: bool` —— 本实例是否处于复制关系(redis:`role=slave`)。
- `link_healthy: bool` —— 复制链路是否正常(redis:`master_link_status=="up"`)。
- `lag_seconds: int | null` —— 副本侧单连接可测得的滞后秒数;未配置复制或无法测量时为 `null`。

**lag_seconds 语义随 DB 而异,每个 inspector 必须在 manifest `description` 与其 spec 中显式声明其语义类**(`link_freshness` 或 `apply_lag`),因为两者**不可直接跨 DB 比较**(D-8)。redis:`lag_seconds = master_last_io_seconds_ago`,语义类 = **link_freshness**(链路新鲜度,见 D-1)。mysql/pg(wave-2c):`lag_seconds` = **apply_lag**(数据应用滞后)。Finding DSL **只允许**对该三元组的标量做比较;**禁止**让 DSL 理解任何 DB 专有原始字段或单位。

**理由**:`replication_configured` / `link_healthy` 两个布尔是三种 DB 真正同形的部分,统一无争议。`lag_seconds` 这个标量形态统一(都是"秒 + null guard"、DSL 比法一致),但**语义不统一**——强行假装统一会让 redis 的 freshness 与 mysql 的 apply-lag 混为一谈、产生错误的跨 DB 比较。故契约统一**形态**、要求**声明语义类**、并由 D-8 记录异构,既得到 finding 规则跨 DB 同形的好处,又不撒"语义统一"的谎。

**哨兵值处理**:redis 在初始全量同步 / 链路重建瞬间会吐 `master_last_io_seconds_ago:-1`(数值,会绕过 fail-loud 的非数值检查)。collector **必须**把 `-1` 归一成 `lag_seconds=null`(无从测量),而**不是**输出 `lag_seconds=-1`(否则 DSL 比阈值时 `-1 >= warn` 永假、却把"正在同步"误当"健康")。同理 `master_link_status` 非 `up` 时 `link_healthy=false`,与 `-1` 哨兵相互印证。

**替代方案**:(a) 暴露各 DB 原始字段各自命名——否决,每加一个 DB 就改 finding 形态。(b) 假装 `lag_seconds` 跨 DB 语义统一——否决,这正是 review 抓出的 over-claim,会误导 wave-2c 把 freshness 当 apply-lag 比较。

### D-4:三态复制健康分类(扩展基底 D-3 失败分类)

**决策**:在基底失败三态(`requires_unmet` / `exception` / `ok`)之上,`ok` 内部按复制语义再分三态,**由 finding 规则(非 status)表达**:

| 复制语义 | 判据(redis) | status | finding |
|---|---|---|---|
| 未配置复制 | `role!=slave`(无 master_host) | `ok` | **无**(standalone/primary 不是故障) |
| 配置但链路断 | `replication_configured && !link_healthy` | `ok` | **critical**「replication link down」 |
| 配置且陈旧/滞后 | `link_healthy && lag_seconds>=阈值` | `ok` | warn / critical 按 `lag_seconds` |
| 配置且健康 | `link_healthy && lag_seconds<warn` | `ok` | 无 |
| 连不上副本 / NOAUTH | redis-cli 非零退出 + 空 stdout | `exception` | —(继承基底 D-3) |
| 缺 redis-cli | preflight 缺二进制 | `requires_unmet` | —(继承基底 D-3) |

**关键**:"未配置复制"**必须**映射成 `ok` + `replication_configured=false` + **无 finding**,而**不是** `exception`、也不是伪造一个 lag。这是 proposal 点名的"未配置复制 vs 复制故障"区分——一个 standalone redis 被错当成"复制故障"会制造假告警。基底失败分类(reachable 且返回有效数据→ok)与此一致:standalone redis 可达且返回有效 `role:master`,故 `ok` 正确。

**fail-loud 必须按 role 上下文(否则把 standalone 错判 exception)**:`role:master` 的 standalone/primary 的 `INFO replication` **本就没有** replica-only 字段(`master_link_status`/`master_last_io_seconds_ago`/`master_host`)。collector **必须先读 `role`**:`role` 缺失或 redis-cli 非零退出才 fail-loud(→exception);`role!=slave` 走未配置路径(`replication_configured=false`/`link_healthy=false`/`lag_seconds=null`,无 finding),**不**把"缺 replica-only 字段"当 fail-loud;只有 `role==slave` 而 replica-only 字段缺失(真异常)才 exit 1。把"字段缺失"一刀切成 exit 1 会让合法单机变 exception,违反上面的"未配置不告警"。

**理由**:status 层管"能不能采到",复制健康管"采到的拓扑健不健康",两层正交。把"未配置"塞进 exception 会让一台合法单机在巡检里红;把"链路断"也塞成无 finding 的 ok 会漏掉真故障。三态 by-finding 让两者都对。

**替代方案**:用 status 直接表达复制健康(如 `status=replication_broken`)。否决:破坏基底失败三态的封闭集,且 status 是 runner 层语义不该被业务复制状态污染。

### D-5:确定性制造故障的 fixture 录制法(禁固定 sleep)

**freshness 阈值的诚实前提**:`master_last_io_seconds_ago` 在副本**收到主库任何 IO**(含周期性复制 ping,`repl-ping-replica-period` 默认 10s)时复位。故健康**空闲**副本上该值在 `0..repl-ping-replica-period`(约 0..10s)间振荡——**默认阈值必须高于 ping 周期**(否则健康空闲副本误报),且 `healthy` fixture 须在**主动写入/刚 ping 后**录制使其 ~0。本 spike 取 `warn_seconds=15` / `critical_seconds=30`(均 > 10s ping 周期)。compose **必须**显式 pin `--repl-ping-replica-period 10` 与一个**很大的 `--repl-timeout`(如 3600)**,使 `DEBUG SLEEP` 期间链路**不因超时判 down**(让 stale 与 down 两个 fixture 语义可控分离),链路断只由 TCP 断开(停容器)触发。redis-repl-master **必须**额外加 `--enable-debug-command yes`(redis 7.x 默认 `no`/`local`,不开则 `DEBUG SLEEP` 被拒、link_stale 录制无法进行)——**仅录制 lane 的 master 用**,不带进生产 fixture。

**决策**:compose 加 `redis-repl-master` + `redis-repl-replica`(replica `replicaof master 6379`,两者 pin 上述 repl 参数)。录制器 `_record_redis_replication_lag.py` 产**两个语义不同的 semantic-abnormal**(都真造,非低阈值凑):

1. **链路断**(`replication_lag_link_down.json`):建立复制 → poll 副本 `INFO replication` 确认 `master_link_status==up` → **停 master 容器**(TCP 断开;不依赖 repl-timeout)→ **poll 副本直到 `master_link_status==down`** → 冻结该快照。`link_healthy=false`。
2. **链路陈旧**(`replication_lag_link_stale.json`):建立复制并确认 link up → 在 master 上**异步** `DEBUG SLEEP <T>`(T 取 ~35s,既 > `critical_seconds=30` 又 << `repl-timeout=3600` 故链路保持 up;DEBUG SLEEP 阻塞主、录制器须后台发起再用**另一条连到副本**的连接 poll)→ **poll 副本直到 `master_link_status==up` 且 `master_last_io_seconds_ago>=30`** → 冻结该真实值。`link_healthy=true` 但 `lag_seconds` 高——redis 的 freshness 阈值路径,与"链路断"语义不同(一个 up-but-stale、一个 down)。

**readiness 一律 poll-until-condition,禁固定 `sleep N`**(继承基底 D-5)。冻结后 ReplayTarget 逐字回放录制的 stdout,replay 时不读时钟 → snapshot 确定性。录制不进 CI,只 replay 进 CI;录制器头注释写明重录步骤。

**理由**:proposal 红线"semantic-abnormal 必须真造"。两个 fixture **语义必须真正不同**(否则等于只测了一个故障类):`link_down` 测 `link_healthy=false` 分支,`link_stale` 测 `link_healthy=true && lag_seconds>=阈值` 分支——靠 `DEBUG SLEEP T < repl-timeout` 把"陈旧但未断"和"已断"分开,是 redis 原生、确定、无特权的造法。固定 `sleep` 等 freshness 长出来会 flake;poll 一个**条件**(`link_status==down` / `up && last_io>=阈值`)而非等一个**时长**,把竞态消掉;冻结值把 replay 端时钟依赖消掉。

**替代方案**:用 `tc`/`iptables` 注入网络延迟。否决:需 NET_ADMIN cap、compose 更重、延迟值不稳定;`DEBUG SLEEP` 冻结事件循环更可控。另:试图用 redis 造"link up 且数据 apply 滞后"的 fixture——否决,redis 副本侧单连接测不到 apply 滞后(D-1),强造只会得到 freshness 上升,故 fixture 诚实命名为 `link_stale` 而非 `lagging`。

### D-6:不进单实例 cohort,独立 crosscheck 复验继承项

**决策**:`redis.replication_lag` **不加入** `tests/inspectors/test_service_contract_crosscheck.py` 的 `_ALL_SERVICE_MANIFESTS`(保持 11)/ `_SECRET_SERVICE_MANIFESTS`(保持 6)——这两个是**显式 dict 枚举**(非 glob),不主动加入即不计入,11/6 冻结。新建 `tests/inspectors/test_replication_contract_crosscheck.py`,内含:
- **复验继承的单实例契约项**(机械证明"继承"不是嘴上说):注入安全三件套、secret `HOSTLENS_`→`REDISCLI_AUTH` remap 不进 argv、fail-loud 失败三态、超时(`-t 5` < `timeout_seconds`)、跨 local/ssh 无分叉、输出形态纪律。
- **复制专属项**:归一三元组在 output_schema(D-3)、三态复制健康 by-finding(D-4)、两个语义不同的 semantic-abnormal fixture 存在且 default 阈值触发(D-5)、副本侧 N=1(D-2)、lag 语义类已声明、裁定记录存在(D-8)。

**理由**:单实例 cohort 的 `TestSingleInstanceBoundary.test_no_multi_instance_params`(crosscheck.py:954)**明确禁止** `replica`/`primary`/`replication`/`lag`/`instances`/`nodes` 出现在**参数名**里——它是单实例边界的守卫(注:该守卫用 `forbidden not in props` 对参数名 dict **做精确 key 成员判断,非子串匹配**)。把复制 inspector 混进那个 cohort 要么逼着改守卫放水、要么破坏单实例边界完整性。`service-inspector-suite` 的"追加式冻结 cohort 永不 MODIFY 旧 wave"也要求新形态走新 cohort。独立 crosscheck 让"继承"被**复跑**验证,而非仅文档声明。

**builtin 全量消费面(诚实记录,非"无其它枚举者")**:builtin manifest 被两处**全量测试**纳入(均经 `rglob` 枚举全部 builtin yaml——一处**直接** `rglob`、一处**经全量 registry 构建**间接 `rglob`),新增 `replication_lag.yaml` 会被自动纳入,但都**不会误红**——须由 task 4.3 主动核验"通过/干净加载"而非仅"计数不误红":
- `test_builtin_capability_gate.py:46` **直接** `rglob("*.yaml")` 全量参数化:计数守卫是宽松下界(`>=12`,line 64,加一仍满足);新 manifest **必须主动通过** `test_builtin_requires_only_static_capabilities`(只声明 `requires_capabilities:[shell]`=静态,满足);binary/secret gate 用例是**显式 list**(非 glob),不自动纳入。
- `test_builtin_inspectors.py` 的三个 `*_all_register_with_no_errors` 经 `build_registry_from_search_paths`(内部 `registry.py:228` `rglob`)**构建全量 registry 并断言 `result.errors == []`**:新 manifest **必须干净加载**否则三个测试全红;per-wave cohort dict(wave2a==6/wave2b==3)是**子集成员**断言(`set(_WAVEX) - registered == {}`)、非全量相等,故多一个 builtin 不误红。

**实现注**:`redis.replication_lag` 的参数名**真正回避**多实例词——用 `host`/`port`/`warn_seconds`/`critical_seconds`(**不含** `lag`/`replica`/`primary` 等子串),既满足守卫(即便将来守卫改成子串匹配也安全),也不靠"守卫是精确 key 匹配"这一实现细节兜底。多实例语义体现在 **output_schema 三元组**与 **fixture 拓扑**,不在参数名。具体核验见 task 4.3。

### D-7:secret 复用,不引新机制

**决策**:`redis.replication_lag` 复用 `redis.memory_usage` 逐字一致的 secret 路径:声明 `secrets: [HOSTLENS_REDIS_PASSWORD]`,collector 内 `REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD"` remap,密码不进 argv、不 `{{ }}` 插值、空密码走 no-auth 分支。

**理由**:副本通常与主同享 `requirepass`;复制巡检的 secret 面与内存巡检完全一致,无新机制可立。继承基底 D-1/D-6 的 `HOSTLENS_` 前缀 + SSH `AcceptEnv HOSTLENS_*` 投递路径。

### D-8:裁定输出(spike 的核心产出物)

**决策**:design 与 spec 各记录裁定,作为 wave-2c 的强条件依赖输入。**spike 的首要发现:统一 `lag_seconds` 跨 DB 语义异构**——

- **核心发现(lag 语义异构)**:redis 副本侧单连接的干净信号是 **link-freshness**(`master_last_io_seconds_ago`,秒;链路新鲜度,非数据 apply 滞后);真 redis apply 滞后需 `master_repl_offset` − 副本已处理 offset 的**字节差** + **主连接**,超副本侧单连接范围。mysql `Seconds_Behind_Source` 与 pg replay-timestamp/LSN 滞后是 **apply 滞后**。**∴ 契约的 `lag_seconds` 形态统一但语义不同类**(freshness vs apply_lag),契约**不得假装统一**——每个 inspector 必须声明其 lag 语义类(D-3),wave-2c 的 mysql/pg `lag_seconds` 与 redis 的 freshness **不可直接跨 DB 比较**。**给 wave-2c 的 hand-off 更正**:`add-replication-lag-inspectors` 骨架里 redis 行写的"offset 差"指的是 apply-lag 路径,本 spike **不交付**该路径(交付的是 freshness);wave-2c 提升时应据此更正其 redis 行(或承认 redis 已由本 spike 以 freshness 语义交付,不重复)。
- **redis**:✅ 已由本探针交付(副本侧 freshness 自报,N=1)。wave-2c 不重复。
- **mysql**:✅ **高置信可沿用**——`Seconds_Behind_Master`(8.0.22+ `Seconds_Behind_Source`)副本侧自报、单位即秒、语义即 apply 滞后,与契约三元组贴合,只多一个复制账户授权前提。wave-2c 机械铺,collector 把 `Seconds_Behind_Source`→`lag_seconds`(语义类 `apply_lag`)、`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`→`link_healthy`。**注意**:① `Seconds_Behind_Source` 在 IO 线程断开/追赶中等状态会返回 NULL,collector 须把 NULL 归一成 `lag_seconds=null` + 据 `Replica_*_Running` 定 `link_healthy`,不可当 0;② IO/SQL 线程列名 8.0.22+ 为 `Replica_IO_Running`/`Replica_SQL_Running`,**5.7–8.0.21 为 `Slave_IO_Running`/`Slave_SQL_Running`**,wave-2c 须按目标版本择名(同 `Seconds_Behind_*` 的版本前提)。
- **postgres**:⚠️ **形态分叉,沿用性待 wave-2c 真实录制验证**——lag 既可副本侧 `pg_last_wal_replay_lsn` vs `pg_last_wal_receive_lsn` 字节差(需 `pg_wal_lsn_diff` 换算 + 估算秒)、又可副本侧 `now() - pg_last_xact_replay_timestamp()`(直接得秒,但**主库空闲时 `now()-replay_ts` 会虚高**,须 guard:无在途事务时滞后应视为 0/null 而非时间差)、又可主库侧 `pg_stat_replication` 多行聚合。契约的**主库侧聚合前向规则**(D-2)正是为它预留。若 wave-2c 录制证明某条副本侧路径能干净归一成 apply_lag 三元组 → 沿用;若主库侧聚合 + LSN/idle 换算复杂到撑破契约 → postgres `replication_lag` 推 **wave-3** 单独立法。

**理由**:spike 的价值不只是"做出一个 inspector",而是**给铺量批次一个有依据的范围决策 + 暴露契约的真实边界**(lag 语义异构)。把裁定写进 spec(可归档、可被 wave-2c 援引),而非散在对话里。

## 风险 / 权衡

- **[真实故障录制依赖 `DEBUG SLEEP` / 停容器,只在录制 lane 跑,CI 不跑]** → 与基底 D-5 一致:录制产物冻结进 git,CI 只跑 ReplayTarget 回放;录制器头注释文档化"如何重录"。
- **[`link_stale` 与 `link_down` 若造法不当会语义重合]** → D-5 用 `DEBUG SLEEP T < repl-timeout` 显式把"陈旧但 up"与"已断"分开,录制时**断言** `link_stale` 的 `master_link_status==up`、`link_down` 的 `==down`,语义重合则录制失败。
- **[健康空闲副本上 `master_last_io_seconds_ago` 振荡到 ~`repl-ping-replica-period`(10s),低阈值会误报]** → 默认 `warn_seconds=15`/`critical_seconds=30` 均高于 ping 周期;`healthy` fixture 在主动写/刚 ping 后录制使其 ~0、默认阈值无 finding;finding-trigger 用 lowered `warn_seconds=0`(只证 wiring),`link_stale` 才是真 `>=30`(证检出),双轨分工继承基底 D-4。
- **[redis `lag_seconds` 是 freshness 非 apply 滞后,可能被误用作跨 DB 比较]** → D-3 强制声明 lag 语义类 + D-8 显式记录异构 + 契约要求每 inspector 声明语义;这是 review 抓出的核心 over-claim,已在契约层正面处理而非隐藏。
- **[冻结的值是录制时快照,redis 改字段名会让 fixture 失真]** → `master_last_io_seconds_ago` / `master_link_status` 自 redis 2.8 稳定;manifest `tags` 声明 `redis6`,output_schema 锁字段;字段消失会让 collector fail-loud(非数值 → exit 1)而非静默错。
- **[postgres 形态分叉可能证明"副本自报"假设过窄,契约骨架需返工]** → 这正是 spike 要暴露的;D-8 把 postgres 标为 open,契约的主库侧聚合前向规则为分叉预留接口,最坏情况 postgres 推 wave-3 而契约对 redis/mysql 仍成立(spike 不失败,只是覆盖收窄)。

## 未决问题

- 主库侧聚合的精确归约函数(`max(lag)` 是否够?是否需 `min(link_healthy)` + 滞后副本计数?)留到 wave-2c 真有主库侧 inspector 时再定稿;本 spike 只**前向描述**归约方向(滞后取最大、链路取逻辑与),**不冻结**为 normative MUST。
- postgres 副本侧 `now()-pg_last_xact_replay_timestamp()`(得秒,需 idle guard)vs LSN 字节差(需换算)哪条更干净归一成 apply_lag,留 wave-2c 录制时定。
- 是否在 output_schema 增设一个显式 `lag_semantic` 字段(`link_freshness`/`apply_lag`)以让跨 DB 比较在运行期可判,留 wave-2c 真有第二个 DB、确有跨 DB 比较诉求时再定;本 spike 先以 description/spec/D-8 文档级声明承载,避免为单 DB spike 引入 schema 字段。
