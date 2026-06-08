## 上下文

`replication-inspector-contract`(7 需求 + 覆盖矩阵)由 redis spike 建立、wave-2c 铺 mysql 兑现。两者共享一个**从未被点破的前提**:采集视角是**副本侧 N=1**(连副本、读其自报 link/lag,聚合退化为 identity)。契约只对「主库侧多副本归约」做了**前向(暂定)描述**(方向:lag 取 max、link 取 AND),并明说「精确函数留验证、本 spike 不冻结、不实现、不测试」。

wave-2c 的 postgres 录制验证门(W-6)实证:postgres **副本侧 timestamp 路径是死的**——

| 状态 | `recv==replay` | `now()-replay_ts` | `pg_stat_wal_receiver` streaming | 朴素 idle-guard | 真相 |
|---|---|---|---|---|---|
| **receiver 断(primary 停)** | **t** | 10.4→22.5↑ | **0** | **lag=0(假健康)** | **任意滞后** ✗ |
| standalone | recv_lsn=**NULL** | — | — | configured=false | 单机 ✓ |

诚实稳健的 postgres apply-lag 是**主库侧** `pg_stat_replication.replay_lag`——属契约前向规则的主库侧聚合。postgres 单元格冻结为 **deferred-to-wave-3**。本变更是 wave-3:第一次实现主库侧路径,因此**必须把前向暂定归约规则冻结成 normative + 测试**,这是契约演进而非「机械铺第三个 DB」。

**复用基础**:`postgres.connection_usage`(`HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD` remap、`PGCONNECT_TIMEOUT=5`、`psql -tA -F'|'`、command-sub 非 pipe 以免 mask psql 失败);wave-2c 的复制录制 lane(`_compose_record.py` + `wait_until` + `record_fixture`、共享 compose project 起 primary+standby)与 `_record_mysql_replication_lag.py` 的双轨真造 + poll-until-condition;已泛化的 `test_replication_contract_crosscheck.py`(`_REPLICATION_MANIFESTS` 枚举 + 计数守卫)。

## 目标 / 非目标

**目标:**
- 铺 `postgres.replication_lag`:主库侧 `pg_stat_replication.replay_lag` 归一成契约三元组(语义类 apply_lag),遵守既有 7 条契约需求(由泛化 crosscheck 机械证明)。
- **冻结主库侧归约函数**:把契约前向暂定的「lag 取 max、link 取 AND」从「方向」升级为 normative 验收 + 测试(ADD 兄弟需求 + 退役④的前向场景)。
- 覆盖矩阵 postgres 单元格 `deferred → delivered`;复制 cohort 计数守卫 2 → 3。

**非目标:**
- ❌ 不重复副本侧 timestamp 路径(wave-2c 已否决);不引「期望副本数」参数测全断;不泛化④成「按视角聚合」(用 ADD 兄弟需求,避 RENAMED);不改契约既有需求 normative 核心;不动单实例 cohort 计数(11/6);不引新 parse/secret;不做 cascading standby / `write_lag`·`flush_lag` / `synchronous_commit` 档位语义。

## 决策

> 决策标号 W3-n。涉及契约项处引用 spike 的 D-n / wave-2c 的 W-n。炸雷点编号 L1–L6 贯穿。

### W3-1:主库侧 `pg_stat_replication.replay_lag`,归一成 apply_lag 三元组

**决策**:`postgres.replication_lag` 连接**主库**,查 `pg_stat_replication`(每个在线 standby 一行),collector 内归一(派生在 collector 内,DSL 只比标量):
- `replication_configured: bool` —— **行数 > 0**(至少一个 standby 在线)。
- `link_healthy: bool` —— `configured ? AND_over_rows(state == 'streaming') : false`。
- `lag_seconds: int | null` —— `configured ? max_over_rows(replay_lag→秒, 仅 non-NULL) : null`;若所有行 `replay_lag` 均 NULL → `null`。语义类 = **apply_lag**(`replay_lag` ≈ 近期事务对查询可见前的延迟,源端测量)。

**理由**:`replay_lag` 是 postgres 主库侧唯一诚实的「单值秒数」apply-lag(副本侧无 `Seconds_Behind_Source` 等价物——wave-2c 已证)。`interval` 经 `EXTRACT(EPOCH FROM replay_lag)` 在 collector 内换算成秒,DSL 只比三元组标量,不懂 DB 专有字段(继承 authoring-contract「派生在 collector 内」)。

**替代方案**:副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard——**否决**(wave-2c 门实证:receiver 断时假健康)。`write_lag`/`flush_lag`——非目标(apply-lag 用 `replay_lag` 最贴「数据应用滞后」)。

### W3-2(L5):`replay_lag` NULL guard —— 空闲已追平归一成 null 而非 0

**决策**:[PostgreSQL 文档实证] 完全追平的空闲系统(无 WAL 活动)`replay_lag` 回落为 NULL。collector **必须**把 NULL 归一成 `lag_seconds=null`(无从测量),**禁止**当 0(否则「正常空闲」与「滞后 0」混淆,且会让 finding 阈值比较失真)。reduction 取 non-NULL 行的 max;全行 NULL → null。

**理由**:与 mysql SBS 的 NULL 纪律同构(契约「无法测量时 lag 为 null 而非 0」)。

### W3-3(L6):reduction 空集 vacuous-true 守卫 —— 先判 configured 再 AND

**决策**:collector **必须先**判 `configured = (行数>0)`;未配置(空集)直接输出 `(replication_configured=false, link_healthy=false, lag_seconds=null)`,**禁止**让空集落进 `AND_over_rows(...)` ——空集的逻辑与是 vacuous true,会把单机主库捏造成 `link_healthy=true`。仅 `configured==true` 才对各行求 `AND(state=='streaming')` 与 `max(replay_lag)`。

**理由**:这是「先证后铺」的实现侧正确性守卫;空集→true 是主库侧特有的、redis/mysql 副本侧不存在的 reduction bug。

### W3-4(L2 + 空集裁定):主库侧 `pg_stat_replication` 空集一律 ok 无 finding,显式划边界

**决策**:主库侧空结果集**一律** status=`ok`、`replication_configured=false`、`lag_seconds=null`、**无 finding**——因为从主库侧**无法区分**:
- (a) 这台就是单机主库(从没配过复制),合法,ok;
- (b) 本来有 standby、全断了(primary 宕的对端 / 网络分区 / standby 全挂)= wave-2c 门裁定的漏洞①搬到主库侧。

两者 `pg_stat_replication` 都是空集,主库侧物理上不可区分。spec **必须显式声明「主库侧 inspector 无法检测副本全断,该场景留给副本侧 receiver-health 或外部拓扑探测」**——诚实划边界,继承「不假装能测撑破的东西」(同 wave-2c 否决副本侧 timestamp 的 spirit)。

**替代方案**:参数化「期望 standby 数 / application_name 列表」,少于期望判 critical——**否决**:违背 redis/mysql 的零配置自描述风格,且参数名会撞契约禁用的多实例词(`replica`/`nodes`/`instances`),并把运维拓扑塞进 inspector 配置。

### W3-5(L2 + L3):semantic-abnormal 两个 fixture 在主库侧拓扑的造法 + link_down 重定义

**决策**(兑现契约「两个语义不同 semantic-abnormal、真造、poll-not-sleep」):

1. **lagging**(`link_healthy=true && lag_seconds>=阈值`):standby 设 `recovery_min_apply_delay='<T>s'`(T > 默认 critical 阈值)、主库**持续写 WAL** → standby 保持 `state='streaming'`、receive/flush 紧跟、**仅 replay 延迟** → 主库 `pg_stat_replication.replay_lag` 可控涨到阈值 → poll 主库直到 `replay_lag>=critical` 且该行 `state='streaming'` → 冻结。
2. **link_down**(`link_healthy=false`):主库侧**物理断开 standby = 那行消失 = 空集**(单 standby)→ 落进 W3-4「空集→ok」边界、**测不到**(加 standby 数也没用:死的那行永远消失,在线行仍 streaming)。故 link_down **必须重定义为「行还在、`state≠'streaming'`」**。**录制实证(2026-06-08)**:`catchup` 在快速本地 loopback 上**太瞬态**(WAL 秒级 ship 完、catchup 窗口 < poll 间隔,且大积压会超 `wal_keep_size` 致回收无法 catchup),**不可靠 latch**;**改用可保持的 `backup` 态**——一个 **throttled `pg_basebackup --max-rate=256k`** 会在 `pg_stat_replication` 持续显示一个 `state='backup'` 的 walsender(实测稳定保持十余分钟),是确定性可控的非 streaming 行。`backup` 同属「接受的误报集」、满足 normative `state != 'streaming'`。`link_healthy=AND(streaming)`→false → critical「link down」。

两 fixture 语义**必须不同**:lagging 录制断言该行 `state=='streaming'`;link_down 录制断言存在 `state!='streaming'` 行(录制取 `backup`)。readiness 全 poll-until-condition,禁固定 sleep;冻结后 ReplayTarget 回放。

**L3 裁定(state 全集的 link_healthy 取舍 —— 必须穷举,不只 catchup)**:`pg_stat_replication.state` 枚举全集 = `{streaming, catchup, startup, backup, stopping}`。`link_healthy=AND(state=='streaming')` 把**全部非 streaming 成员**判 `link_healthy=false`→critical;契约要求**显式裁定每个成员**(禁止只论证 catchup 而对其余隐式兜底)。逐成员:
- `streaming` → healthy。
- `catchup` → false→critical。standby 重启/追赶,接受噪音。
- `startup` → false→critical。walsender 握手瞬态,命中率低。
- `backup` → false→critical。**这是已知误报**:standby 正 `pg_basebackup` 拉基线、是正常初始化操作,被判 critical 并非「病」。
- `stopping` → false→critical。walsender 正常关停瞬态。

**接受这组误报集 `{catchup, startup, backup, stopping}`**,理由:(i) 非 streaming 的副本**当下不提供 apply-lag 保护**,在快照里浮出有价值;(ii) `startup`/`stopping`/`backup` 多为瞬态或一次性操作,单次调度快照命中率低;(iii) 给任一非 streaming 状态宽限(算健康)则 link_down fixture 无处可造(回到 L2 死结),破坏契约「两个语义不同 semantic-abnormal」;(iv) 意外/未知 state 值统一偏 fail-safe 判 false(误报 critical 而非静默假健康)。**关键录制注意**:本提案录制 healthy/lagging fixture 用 `pg_basebackup` 拉 standby,**录制 readiness 必须 poll 至 `state=='streaming'`**——否则撞 `backup` 窗口会把 critical 录成 healthy(见 tasks 3.2)。spec/description **必须**把这组「接受的误报集」显式写出(已落 spec scenario),并注明与副本侧「`master_link_status:down`=真断」的**语义差**(主库侧 link_down = 非 streaming 状态,非物理断开)。**残留**:`backup`=critical 是真误报;若实践太噪,后续 wave 可追加「establishing/backup 宽限」需求(append-only),本期不做。

**理由**:`recovery_min_apply_delay` 是 postgres 原生、确定性强的 lagging 旋钮(主库写 → replay 延迟可控),优于 mysql「大积压赌追赶窗口」(W-4)、无需停容器。link_down 的重定义是 W3-4「空集→ok」裁定的直接推论,不是遗漏。

**替代方案**:`pg_wal_replay_pause()` 造 lagging——可行但等价于 apply_delay 且 `state` 仍 streaming,不能兼造 link_down;`tc` 注延迟——否决(需 NET_ADMIN、不稳)。

### W3-6(L1):`pg_monitor` 硬前提 + 欠权静默假健康 → 录制门实证 `state` 列可见性

**决策**:[PostgreSQL 文档实证] 看全 `pg_stat_replication` 的 lag 列需 `pg_monitor`(或 superuser);欠权账户在「别人的 walsender 行」里很多列读成 NULL。于是欠权 inspector 账户 `replay_lag` 读成 NULL → collector **无法区分**「空闲 NULL(L5,真健康)」与「欠权 NULL(实际任意滞后)」→ **静默假健康**(比 mysql 欠权 Access denied 走 exception 更阴)。

- 把 `pg_monitor`(或 superuser)写成**硬前提**进 `description` + tags(类比 mysql `REPLICATION CLIENT`),文档式声明(不经 manifest 字段机器门)。
- **录制门必须实证**:欠权账户下 `state` 列是否**也** NULL。
  - 若 `state` 也 NULL → `link_healthy=AND(state=='streaming')`=false → 喷 critical(**响错**,可接受:用户会去查,发现是权限);
  - 若 `state` 可见而仅 lag 列 NULL → **静默假健康**,collector **必须**额外防护(如 `state='streaming' && replay_lag IS NULL` 这种「streaming 但 lag 测不到」组合在 collector 内不当健康——但不污染 status,仍 ok + lag_seconds=null,由 description 前提兜底;具体形态待门实证后定)。

**理由**:这是 postgres 主库侧特有的隐藏 false-healthy 模式,继承「先证后铺」:不在 spec 里赌 `state` 可见性,留录制门证。

### W3-7(L4):拓扑反转 —— 指向主库,description 巨响声明

**决策**:`postgres.replication_lag` 巡检视角是**主库侧**(redis/mysql 是副本侧)。`description` **必须巨响声明「指向 primary」**:用户若按 mysql 习惯把它指向 standby,standby 的 `pg_stat_replication` 为空(无下游)→ `configured=false` → 假「未配置 ok」(静默误导)。

**开放题**(见下):standby 上(`pg_is_in_recovery()=true`)是否 emit 一条提示 finding——倾向**不 emit**(inspector emit 操作建议越界),靠 description 兜底。

**理由**:拓扑反转是 redis/mysql 没有的 usability 陷阱;参数名仍只 host/port/user/dbname/阈值(不含多实例词,合契约)。

### W3-8:secret + 连接前提复用,不引新机制(W3 兑现 wave-2c W-3 的 postgres 版)

**决策**:逐字对齐 `postgres.connection_usage`:`HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD`(密码不进 argv、不 `-W`、不 `{{ }}`)、`PGCONNECT_TIMEOUT=5` < `timeout_seconds=15`、`psql -tA -F'|'`、值用 command-sub 取(非 `psql|awk` 管道,以免 awk 退出码 mask psql 失败)、fail-loud(psql 非零 / 非数值 → exit 1 + 空 stdout → exception)、缺 `psql` → requires_unmet。host/dbname 走 `| sh`,port 是 int。

**理由**:secret 面与 connection_usage 完全一致,无新机制;`pg_monitor` 权限前提是文档诉求(W3-6)。

### W3-9:契约 spec delta 形态 —— ADD 兄弟需求 + 退役④前向场景 + 翻 postgres 单元格

**决策**:对 `replication-inspector-contract` 三块 delta:
1. **ADD**「主库侧采集视角必须按冻结归约函数归约」需求——冻结 reduction(`configured=行数>0`;`link_healthy=configured?AND(state=='streaming'):false`;`lag_seconds=configured?max(replay_lag非NULL秒):null`,全 NULL→null;空集守卫 W3-3;主库侧测不到全断 W3-4),配独立 crosscheck 枚举验收。与既有「副本侧 N=1 identity」需求**并存**(append-only)。
2. **MODIFY**「副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL」需求——**仅退役**其「主库侧多副本归约方向为前向暂定规则」场景(该场景明说「不冻结、不测试」,已被本提案兑现,留着会让归档后主 spec 自相矛盾),改为引用上面冻结的兄弟需求;**标题不变**、副本侧 identity 规范核心与 N=1 场景**不动**(不触发 RENAMED 归档坑,见 memory `project_openspec_modified_rename_archive`)。MODIFIED 须含整需求块全文。
3. **MODIFY**「复制 inspector 覆盖矩阵随 wave 追加冻结」需求——postgres 单元格 `deferred → delivered`(主库侧 `pg_stat_replication.replay_lag`、语义类 apply_lag),追加 postgres 场景;**同时退役**主 spec 该需求里「postgres 录制验证门裁定纳入或推 wave-3」场景(deferred 已兑现为 delivered,该「纳入或推」决策已 resolved,留着与翻成 delivered 的单元格矛盾);redis/mysql 已冻结单元格**不动**(append-only);复制 cohort 计数守卫文字 2 → 3。MODIFIED 须含整需求块全文。

**理由**:用 ADD 承载主库侧、用最小 MODIFY 保持主 spec 内部一致——既兑现用户「加兄弟需求、不泛化④」的选择,又不留矛盾场景。翻 postgres 单元格是 wave-2c 预留的 deferred 占位的**预期兑现**,非「回溯改已冻结单元格」(那条 append-only 禁令针对 redis/mysql 语义类的回溯改写)。

### W3-10:归约层走 SQL 聚合 + 整数化 + reduction 由录制时断言(非回放)

**决策**:多行归约**下推进单条 SQL 聚合**——`SELECT count(*), bool_and(coalesce(state::text,'') = 'streaming'), FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM pg_stat_replication`,collector 的 psql 一次往返返回**已归约单行**,shell 只做 `count==0` 短路与 JSON 成形。五个连带裁定:
1. **不用 shell 多行 awk 归约**:mysql 范例是单行 awk 抽标量,多行 `AND`/`max` 在 shell 里易错(尤其浮点 max);SQL 原生聚合更稳。
2. **`bool_and` 的 NULL-state 必须 coalesce 中和(R3 blocker L1)**:SQL `bool_and` **忽略 NULL**——裸 `bool_and(state='streaming')` 在「一行 streaming + 一行 state NULL(欠权对别人 walsender 行 / 未知值)」聚合出 `t` → 假健康(实测 pg16 `bool_and over (true,NULL)=t`)。这绕过「未知 state 偏 fail-safe」意图(NULL 不是未知枚举成员,`state='streaming'` 对 NULL 求值是 NULL)。故**必须** `coalesce(state::text,'')='streaming'` 让 NULL state→false,`bool_and` 在 `count>0` 时恒非 NULL。**附带收益**:全欠权(所有行 state NULL)→ 全 false → critical(响错),把 W3-6 的「静默假健康」残留**收窄到「state 可见而仅 lag 列单独 NULL」**这一窄缝。
3. **psql boolean `t`/`f` 必须映射 JSON bool(R3 major A)**:`psql -tA` 打 boolean 为 `t`/`f`(非 `true`/`false`),JSON 成形**必须** `t`→`true`/`f`→`false`(mirror mysql `Yes`→`true`);直塞 `{"link_healthy":t}` 是非法 JSON → parse 崩 → 健康主从录成 exception。
4. **`lag_seconds` 整数化**:`EXTRACT(EPOCH FROM replay_lag)` 返浮点秒(如 `3.47`),契约 `output_schema` 是 `integer | null` → **必须** `FLOOR(...)::bigint`;psql NULL 渲染空串(非字面 NULL),lag 字段空→`null`。`replay_lag` typed interval 经此恒返 integer|NULL,无 mysql 字符串字段的非数值 fail-loud 风险。
5. **空集在 SQL 的真实返回**:聚合对空 `pg_stat_replication` **返回一行** `(0, NULL, NULL)`(`bool_and` over 空集 = **NULL**)——故 shell **必须据 `count==0` 显式短路**成 `(false,false,null)`,**禁止**采信空集聚合(W3-3 空集守卫在 SQL 路径:count-first 短路、不采信 NULL 聚合)。
6. **reduction 的验证层(净新增技术,非 mirror mysql)**:`ReplayTarget` 冻结的是 collector 最终 stdout(已归约三元组 JSON),回放按命令 SHA 返回冻结 JSON、**SQL 聚合不重跑**(实证:mysql `lagging.json` 冻结的就是 `{"...":...,"lag_seconds":30}`)。故 `max`/`AND` 正确性**禁止**声称由回放 fixture 证明——**必须由录制器在录制时另跑一条不带聚合的 `SELECT state, replay_lag` 拿 raw 多行、在录制器内独立算 `max(FLOOR(EPOCH))`/`all(streaming)` 并断言等于冻结三元组**。**这是 postgres 净新增逻辑、不是 mirror mysql**:mysql N=1(副本侧单值 SBS),其录制器只断言**已归约标量自身**(`out["lag_seconds"]>=阈值`,见 `_record_mysql_replication_lag.py:351`),**没有**多行 reduction 可重算/可照抄;mysql 录制器只提供 compose/poll/脱敏脚手架。**禁止**把「mirror mysql」当借口把录制时断言退化成「只断言已归约三元组自身」(那是自证、reduction 未测)。**拓扑须使 max/AND 都非平凡**:≥2 个 non-NULL distinct streaming 行(测 max 取较大者非 identity)+ ≥1 非 streaming 行(测 AND 从混合得 false)——**禁止**「一行有值一行 catchup-NULL」(catchup `replay_lag` 典型 NULL → max 退化 identity)。

7. **负 `replay_lag`(时钟偏移)按健康处理(R4 minor)**:`replay_lag` 是主库**单时钟**测得(主库自身 WAL 时间戳 vs standby 回报的 replay 位置),非跨时钟差,真负值不现实;若极端出现负值,`FLOOR(负小数)` 更负、`>=warn` 为假 → 视健康,**不**额外裁定(记为接受残留,非 blocker)。

**理由**:RC 第二/三/四轮 A/B/L1/reduction 戳穿——若归约留 shell 多行且不裁定取整,既撞 output_schema(浮点)又使「fixture 测 reduction」落空(回放不重跑);裸 `bool_and(state='streaming')` 因 SQL 三值逻辑忽略 NULL 而在部分欠权下假健康;`t`/`f` 非 JSON bool;「mirror mysql」是假先例(N=1 无 reduction 可镜像)易让实现者把录制时断言建成自证。SQL 聚合 + coalesce 中和 + `t/f` 映射 + 整数化 + **净新增的 raw-row 重算断言(非平凡拓扑)** 是统一解,诚实承认 reduction 的 CI 可测性上限是录制时断言(回放不重跑)。

**替代方案**:shell 逐行 awk 归约——否决(浮点 max 易错、多行 awk 超出 mysql 范例、仍不解决回放不重跑);`lag_seconds` 改 number 类型容浮点——否决(破坏契约既有 `integer | null` 三元组形态、跨 DB 不一致)。

## 风险 / 权衡

- **[归约层/整数化/reduction 验证(W3-10)]** → 归约走 SQL 聚合 + `FLOOR(...)::bigint` 整数化;reduction 正确性由录制时断言(回放不重跑,与 mysql awk 同源);空集据 count 短路不采信 NULL 聚合。**残留**:reduction 的 CI 可测性上限 = 录制时断言(无 postgres 的 CI 跑不了 SQL 聚合),靠 5.2b 把录制证据落 PR 兜。
- **[L1 欠权静默假健康]** → `pg_monitor` 写硬前提 + 录制门实证 `state` 列可见性决定 collector 防护形态(W3-6);不在 spec 里赌。
- **[L2 link_down 非 streaming 行的可录性]** → 录制实证:`catchup` 太瞬态不可靠 latch;改用 throttled `pg_basebackup --max-rate` 制造可保持的 `backup` 态 walsender(确定性、保持十余分钟),作 link_down 与 multi_replica 的非 streaming 行。
- **[L3 catchup=critical 噪音]** → 接受(理由见 W3-5);spec 注明与副本侧语义差;若实践证明太噪,后续 wave 可追加「establishing 宽限」需求(append-only),本期不做。
- **[L4 拓扑反转误用]** → description 巨响 + 开放题(standby 上是否提示)留 design 决议;倾向不 emit。
- **[复制 crosscheck 计数 2→3 触动单实例 cohort / rglob 全量测试]** → 核验 `_ALL_SERVICE_MANIFESTS`(11)/`_SECRET_SERVICE_MANIFESTS`(6)冻结 + 全量 rglob(`test_builtin_capability_gate` 直接 / `test_builtin_inspectors` 经 registry)主动通过(静态 capability gate + `errors==[]`);参数名回避多实例子串(继承 spike D-6 / wave-2c W-5)。
- **[postgres 复制录制比 mysql 重(`pg_basebackup -Xs -R` / `recovery_min_apply_delay` / 大积压)]** → compose 多两服务 + 录制器多几步,但确定性可控(poll `state` / `replay_lag`,非固定 sleep);只在录制 lane 跑,CI 回放。
- **[`recovery_min_apply_delay` 下主库须持续写才有 replay_lag]** → 录制器 lagging 阶段开写循环灌 WAL,poll `replay_lag>=阈值` 命中即冻结;主库空闲则 replay_lag 仍 NULL,故写循环是录制前提。

## 迁移计划

无运行时数据迁移。新增 builtin inspector 文件 + compose 两服务 + 录制器 + crosscheck 枚举扩容,纯增量。回滚 = 移除 `postgres/replication_lag.yaml`、还原 crosscheck 计数守卫 2、还原契约 spec delta。fixture 一次性录制冻结,CI 不依赖 Docker。

## 未决问题（录制门已实证结论)

- **W3-6 录制门 → 实证完成(pg16,2026-06-08)**:欠权账户(仅 `CONNECT`、无 `pg_monitor`)查 `pg_stat_replication`——**行可见**(`count(*)=1`、`application_name` 可见),但 **`state` 与 `replay_lag` 列双双 mask 成 NULL**(同属受限列集、同时 NULL,不存在「state 可见而仅 lag 列单独 NULL」的窄残留)。聚合实测:欠权 `1|f|`(`bool_and(coalesce(state::text,'')='streaming')`=**false**,因 NULL state→`''`≠`streaming`)、超级用户 `1|t|0`。**裁定**:走 1.2b 的「**state 也 NULL → link_healthy=false → critical 响错**」分支——coalesce 中和把欠权变成响错(非静默假健康),**无需额外防护分支**;**tasks 3.10b(窄残留 fixture)跳过**(该残留实证不可达)。这正是 coalesce 修复(W3-10.2)的设计兑现。
- **W3-5 link_down 非 streaming 行 → 实证完成(recipe 改 catchup→backup)**:probe 1.3 证明 `catchup` 原则上可经 `docker network disconnect` + 积压 + `connect` latch(t+3s),但**录制时不可靠**——快速 loopback 下 catchup 窗口 < poll 间隔、且大积压超 `wal_keep_size` 致回收。**改用 `backup` 态**:throttled `pg_basebackup --max-rate=256k -X none`(在 primary 容器内对自身跑)→ 一个 `state='backup'` walsender 稳定保持(实测 216MB DB@256k/s≈14min)。录制器据此:`_start_backup_walsender()` → poll `'backup' in states` → 录制 → `_stop_backup_walsender()`。multi_replica 的非 streaming 行同走此 `backup` recipe(与 3 个 distinct-lag streaming 行共存于一快照)。
- **W3-5 lagging → 实证完成**:standby `ALTER SYSTEM SET recovery_min_apply_delay='30s'` + reload + 主库持续写 → state 恒 `streaming`、`replay_lag` 稳定增长(实测 ~2.2s/2s-poll,十余秒达 30)→ poll `replay_lag>=critical 且 state='streaming'` 冻结。
- **基建实证**:`POSTGRES_HOST_AUTH_METHOD=trust` **只**加 `host all all all trust`、**不**加 replication 行 → 须 primary 起来后追加 `host replication all all trust` + reload 才能 basebackup;standby 用「`rm -rf $PGDATA/* && pg_basebackup -h primary -Xs -R -D $PGDATA && exec docker-entrypoint.sh postgres`」(默认 PGDATA + entrypoint,正确处理 socket/权限);standby 默认 `application_name='walreceiver'`。
- **W3-7 开放题**:指向 standby 时是否 emit 提示 finding?倾向不 emit(越界),靠 description;后续 wave 再议。
