## 1. 录制门实证(先证后铺 —— 在冻结 manifest 行为前跑)

- [x] 1.1 compose 加 `pg-repl-primary` + `pg-repl-standby` + **`pg-repl-standby-2`** + **`pg-repl-standby-3`**(均 streaming replication,`pg_basebackup -Xs -R` 拉起,trust 复制;pin image 同既有 postgres `@sha256`),healthcheck 用 `pg_isready`;**3 个 standby 用于 multi_replica 单载体的 3 行拓扑**(2 distinct non-NULL streaming + 1 非 streaming;spec 要求 max 与 AND 在同一 fixture 同时非平凡兑现,故单 standby/双 standby 均不够);验收:multi_replica 录制窗口 `psql -h primary -c "SELECT count(*) FROM pg_stat_replication"` 返回 **3**(单/双 standby 场景如 healthy/link_down 各自只起所需数量)
- [x] 1.2 **W3-6 实证**:建一个仅 `CONNECT`(无 `pg_monitor`)的账户连主库查 `pg_stat_replication`,记录 `state` 列与 `replay_lag` 列在欠权下分别是否读成 NULL;**并实证多 standby 下"部分行欠权"**(对自己 slot 行可见、对别人 walsender 行 NULL)时 `bool_and(coalesce(state::text,'')='streaming')` 的输出(确认 coalesce 已把 NULL state 落 false);裁定 collector 是否还需「state 可见而仅 lag 列单独 NULL」的残留防护分支,结论写进 design「未决问题」
- [x] 1.2b **强制下游门(消除负面结论无下游)**:若 1.2 实证为「`state` 可见而仅 lag 列 NULL」(静默假健康),**必做**三件套——① collector 加防护分支;② 录一个欠权-NULL 回归 fixture(见 3.8);③ crosscheck 加对应断言;**禁止**因结论负面就停在 design 散文。验收:1.2 结论 ∈ {state 也 NULL→无需防护 / state 可见→三件套已落地},二者之一必须可勾
- [x] 1.3 **W3-5/L2 实证**:主库灌大积压 + 起/重连 standby,poll 主库直到该行 `state=='catchup'`,记录稳定 latch 所需积压量(mirror mysql `_BACKLOG_DOUBLINGS` 实测注释)
- [x] 1.4 **W3-5 实证**:standby 设 `recovery_min_apply_delay`、主库开写循环,poll 主库直到该行 `replay_lag>=` 默认 critical 阈值且 `state=='streaming'`,确认 lagging 窗口可稳定 poll(非固定 sleep)

## 2. manifest 实现(postgres.replication_lag)

- [x] 2.1 写 `src/hostlens/inspectors/builtin/postgres/replication_lag.yaml`:主库侧采集,**归约下推单条 SQL 聚合**——`SELECT count(*), bool_and(coalesce(state::text,'') = 'streaming'), FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM pg_stat_replication`(`psql -tA -F'|'` 返单行)——**禁止** shell 对多行逐行 awk 归约。三个 collector 必守:
  - **`coalesce(...)`(L1 blocker)**:`bool_and` 忽略 NULL → 裸 `bool_and(state='streaming')` 在「一行 streaming + 一行 NULL(欠权/未知)」聚合出 `t` 假健康(实测 pg16);**必须** `coalesce(state::text,'')='streaming'` 让 NULL state→false→critical(响错)
  - **`t`/`f`→JSON bool(A)**:`psql -tA` boolean 打成 `t`/`f` 非 `true`/`false`;JSON 成形**必须** `t`→`true`、**`else`→`false`**(catch-all,**非** `f` 严格等值——任何非 `t` token 偏 fail-safe 落 false→critical 而非产非法 JSON;禁直塞 `{"link_healthy":t}`);psql NULL 渲染**空串**(非字面 NULL),lag 字段空→`null`
  - **空集守卫**:shell 据 `count==0`(第一字段)显式短路成 `(false,false,null)`,**禁止**采信空集聚合(空集 `bool_and`=NULL)
  - 复用 `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD`、`PGCONNECT_TIMEOUT=5`、command-sub 非 pipe
- [x] 2.2 `description` 巨响声明:语义类 `apply_lag`、硬前提 `pg_monitor`/superuser(欠权 lag 列 NULL→静默假健康)、**指向 primary**(指 standby 则 `pg_stat_replication` 空→假未配置);tags 含 `postgres`/`service`;参数名只 host/port/user/dbname/warn_seconds/critical_seconds(**回避**多实例词 replica/primary/replication/lag/instances/nodes)
- [x] 2.3 findings:`configured and not link_healthy`→critical,**message 必须含子串「link down」**(如「PostgreSQL replication link down (standby not streaming)」)以兼容既有 crosscheck `test_replication_contract_crosscheck.py:516` 的 `"link down" in findings[0].message.lower()` 硬断言——**禁止**只写「not streaming」漏「link down」致既有 crosscheck 变红;`link_healthy and lag_seconds!=None and lag_seconds>=critical_seconds`→critical;`...>=warn_seconds and <critical_seconds`→warning;DSL 只比三元组标量(catchup→link_healthy=false→lag finding 的 `link_healthy` guard 为假→不双触发,继承 mysql by-finding 结构)
- [x] 2.3b **state 全集裁定**:`link_healthy=AND(state=='streaming')`,即 `{catchup,startup,backup,stopping}` 一律 false→critical;`description` **必须**把这组列入「接受的误报集」并给 rationale(非 streaming 副本当下不提供 apply-lag 保护 / 瞬态快照命中率低 / 否则 link_down 无处造);意外/未知 state 值偏 fail-safe 判 false
- [x] 2.4 fail-loud 校验:psql 非零 / 非数值 → exit 1 + 空 stdout → exception;空结果集 → ok 无 finding(非 exception);缺 `psql` → requires_unmet;验收:`hostlens inspect postgres.replication_lag --target local`(主库)
- [x] 2.5 **secret 脱敏测试**:特殊字符密码经 `HOSTLENS_POSTGRES_PASSWORD` 注入,断言不进 argv(`ps`)、不进日志/报告/Report payload;验收:脱敏单测对录制 fixture 断言无明文
- [x] 2.6 **ExecutionTarget 非 root 跑通**:local + ssh target(远端 sshd 配 `AcceptEnv HOSTLENS_*`)均以非 root 用户跑通同一 manifest,无分叉

## 3. 录制 fixture 矩阵(ReplayTarget 逐字回放,CI 不起容器)

> 不止「接线」fixture:reduction(max/AND)、空集守卫、idle-NULL 都是本提案冻结的 normative,**必须各有真录制 fixture 兑现其 scenario 的「那么」**,否则单行 fixture 下 `max(x)=x`/`AND(x)=x` 退化 identity = reduction 未测。

- [x] 3.1 写 `tests/inspectors/_record_postgres_replication_lag.py`(mirror `_record_mysql_replication_lag.py`:共享 compose project、`wait_until` poll-until-condition、`record_fixture`、注入特殊字符密码后脱敏再落盘)
- [x] 3.2 录 `healthy.json`(主库 + 一个 streaming standby,小 replay_lag < 默认 warn → 无 finding,status=ok);**录制 readiness 必须 poll 至该行 `state=='streaming'`**——禁在 `backup`/`startup` 窗口冻结(否则录进 critical 当健康,见 M6)
- [x] 3.3 录 `finding_trigger.json`(健康拓扑 + 降低 `warn_seconds=0` → 触发 warning,只证接线)
- [x] 3.4 录 `link_down.json`(semantic-abnormal #1:非 streaming 行 via **throttled `pg_basebackup --max-rate` 制造可保持的 `backup` 态 walsender**——catchup 在快速 loopback 太瞬态不可靠 latch、实证改 backup;`link_healthy=false` → 默认阈值 critical;录制断言存在 `state!='streaming'` 行;finding message 含「link down」子串)
- [x] 3.5 录 `lagging.json`(semantic-abnormal #2:`recovery_min_apply_delay` + 主库写循环,poll 到 `replay_lag>=` 默认 critical 且 `state=='streaming'`,`link_healthy=true` 且 lag 高;录制断言 `state=='streaming'`,与 link_down 语义不同)
- [x] 3.6 录 `conn_refused.json`(psql 指向关闭端口 → 非零退出 + 空 stdout → exception);端口须与 crosscheck `_REPLICATION_DB_CONFIG` 的 `conn_refused_port`(4.1,如 `15439`)一致
- [x] 3.7 **录 `multi_replica.json`(reduction 由录制时同快照重算断言,净新增技术非 mirror mysql)**:**单载体 3 行拓扑**(1.1 的三 standby 全在线)——standby-1 `state=streaming` `replay_lag≈2s` + standby-2 `state=streaming` 经 `recovery_min_apply_delay` 撑到 `≈40s`(**两行都 streaming 且值 distinct**,令 max 须取 40 非 identity)+ 一个 **`backup` 态 walsender**(throttled `pg_basebackup`,catchup 太瞬态改 backup;令 AND 从混合得 false);**禁止**「只一行有值其余 NULL」(非 streaming 行 `replay_lag` 典型 NULL→max 退化 identity)、**禁止**把 max 与 AND 拆进两个 fixture(spec 要求单载体同时非平凡)。**录制器用单条查询在同一 MVCC 快照内同时取 raw 多行(`json_agg(row_to_json(...))`)与聚合三元组**,Python 端从 raw 独立算 `max(FLOOR(EPOCH))`/`all(state=='streaming')`,断言**等于同查询聚合列**;**禁止两次独立 SELECT**(`replay_lag` 实时漂移→断言 race/flaky)。**这是 postgres 净新增逻辑,不是 mirror mysql**(mysql N=1 无多行重算可抄;mysql 录制器只给 compose/poll/脱敏脚手架);**禁止**退化成「断言已归约三元组自身」(自证)
- [x] 3.8 **录 `unconfigured.json`(M2,测空集守卫)**:standalone 主库或 0 在线 standby 的真 `pg_stat_replication` 空集;断言 `status==ok && replication_configured==false && link_healthy==false && lag_seconds==null && findings==[]`(守 vacuous-true 回归;注入式 `_UNCONFIGURED_OK_STDOUT` 不替代真录制)
- [x] 3.9 **录 `idle.json`(M3,测全 NULL→null)**:streaming standby + 主库空闲(`replay_lag` 回落 NULL);断言 `link_healthy==true && lag_seconds==null && findings==[]`
- [x] 3.10a **录 `underprivileged_all.json`(coalesce 回归,可确定性录制)**:用**全欠权账户**(仅 `CONNECT`、无 `pg_monitor`)连有 ≥1 streaming standby 的主库 → 全部 walsender 行 `state` 读成 NULL → `bool_and(coalesce(state::text,'')='streaming')` 全 false → 断言 `link_healthy==false`→critical(**响错非静默假健康**;证 coalesce 中和 NULL state,**禁止**裸 `bool_and(state='streaming')` 否则全 NULL 被忽略聚合出 t)。此 fixture **不依赖** 1.2 分支结论、确定性可录(全欠权必产全 NULL state)
- [x] 3.10b **(条件,依 1.2 结论)** 若 1.2 实证「`state` 可见而仅 lag 列单独 NULL」的窄残留可达:录 `underprivileged_lag_null.json` + collector 残留防护分支 + crosscheck 断言(1.2b 三件套②③);若 1.2 实证该窄残留不可达(state 与 lag 列同权)则记录并跳过
- [x] 3.11 写 `tests/inspectors/test_postgres_replication_lag.py` per-probe snapshot 断言(全部 fixture 回放,snapshot 容忍尾换行 `.rstrip("\n")`);PR 注明 `0||` 空集字节经 collector `count==0` 短路的端到端结果由 `unconfigured.json` 守(raw `0||` split 形态本身不另立独立守卫,short-circuit 是整数判等、比 reduction 简单)

## 4. crosscheck 泛化 + 全量测试核验

- [x] 4.1 crosscheck 加 postgres,**逐个点名 4 个模块级 dict**(漏一个→KeyError 而非断言失败):① `_REPLICATION_MANIFESTS` 加 `postgres.replication_lag`;② `_REPL_FIXTURE_DIR` 加 postgres fixture 目录;③ `_SEMANTIC_ABNORMAL_FIXTURES` 加 `("link_down.json","lagging.json")`;④ `_REPLICATION_DB_CONFIG` 加 postgres 行,**逐字段补齐全部 11 个 key**(crosscheck `:96-122` 实证;漏 `timeout_value`/`semantic_class`/`benign_host` 任一 → 对应 parametrized 测试 `cfg[...]` KeyError):`secret_env="HOSTLENS_POSTGRES_PASSWORD"`、`client_native_env="PGPASSWORD"`、`forbidden_flags=("-W",)`(postgres 走 PGPASSWORD env,禁 `-W`;**禁**误填 `PGPASSWORD` 致 `test_secret_remap_not_in_argv` 自相矛盾)、`timeout_token="PGCONNECT_TIMEOUT=5"`、**`timeout_value=5`**(`test_client_timeout_strictly_smaller...` 消费)、**`semantic_class="apply_lag"`**(`test_description_declares_lag_semantic_class` 消费)、**`benign_host="pg.internal"`**(任一 pattern-valid host,`test_benign_host_rides_sh_filter` 消费)、`required_binary="psql"`、`run_params/replay_params` 带 `user`(manifest `required:[user]`,缺则参数校验失败)、`conn_refused_port`(如 `15439`,避开 5432);并在 autouse `_replication_secret_envs` fixture 加 `HOSTLENS_POSTGRES_PASSWORD` 保持三 DB 对称(F)
- [x] 4.2 计数守卫 `test_replication_manifests_count_frozen` 由 `== 2` 改 `== 3`;`_SEMANTIC_ABNORMAL_ITEMS` 下界相应升(>=6)
- [x] 4.3 crosscheck 对 postgres 复跑继承单实例契约项(注入安全 / secret remap 不进 argv / 失败三态 / 超时 / 无分叉)+ 复制专属项(三元组 + apply_lag 语义类声明 / 三态 by-finding / link_down+lagging 两语义不同 fixture 默认触发 / 主库侧归约函数)
- [x] 4.3b crosscheck 加 postgres-specific 断言(M10):`"pg_monitor" in description and "primary" in description.lower()`(或在 per-DB config 加 `required_description_substrings` 字段泛化);并断言 `multi_replica.json` 的 `lag_seconds==max` 且 `link_healthy==false`、`unconfigured.json` 的 `(false,false,null)`+空 finding
- [x] 4.3c **非数值 fail-loud 风险已由 SQL 类型系统消解(无需 per-probe 注入)**:`replay_lag` 是 typed interval、经 `FLOOR(EXTRACT(EPOCH FROM ...))::bigint` 恒返 integer|NULL,不存在 mysql 字符串 SBS 的非数值风险;唯一 fail-loud 是 psql ERROR 泄进 stdout,由 command-sub + 退出码捕获(`conn_refused.json` 已覆盖)。本条仅作记录澄清,不需额外 fixture
- [x] 4.4 **核验单实例 cohort 不误红**:`_ALL_SERVICE_MANIFESTS`==11 / `_SECRET_SERVICE_MANIFESTS`==6 不变;`test_service_contract_crosscheck.py` 不枚举 postgres.replication_lag
- [x] 4.4b **核验 benign_host 注入桩兼容**:`test_benign_host_rides_sh_filter` 对每个 `_REPLICATION_ITEMS`(含 postgres)喂 `_UNCONFIGURED_OK_STDOUT`(`{"replication_configured":false,"link_healthy":false,"lag_seconds":null}`)并断言 ok;postgres `output_schema` 必须为**严格三元组**(`additionalProperties:false` 但**只**这三字段、不加额外字段),否则注入桩字节不符变红
- [x] 4.5 **核验全量 rglob 主动通过**:`test_builtin_capability_gate.py`(直接 rglob)断言 postgres.replication_lag 只声明 `requires_capabilities:[shell]`;`test_builtin_inspectors.py`(经 registry 构建)全量注册 `errors == []`;验收:`pytest tests/inspectors/ -q`(console 模式,pythonpath=src)

## 5. 契约 spec 同步 + 归档准备

- [x] 5.1 确认 `openspec-cn validate add-postgres-replication-lag-inspector --strict` 通过(已通过,实现后复跑)
- [x] 5.2 实现完成后在 temp 副本实测 `openspec-cn archive` 的 rebuild 校验(MODIFIED 需求标题与主 spec 逐字匹配、中文标题 rebuild 通过),防归档才暴露返工(见 memory `project_openspec_modified_rename_archive`)
- [ ] 5.2b **录制门证据落 PR**:把 task 1.2 实测的「欠权账户下 `state` / `replay_lag` 列是否 NULL」原始矩阵 + 1.3/1.4 实证(catchup 太瞬态→改 backup recipe、apply_delay lagging 窗口)粘进 PR 描述(人工 gate,类比 mysql `_BACKLOG_DOUBLINGS` 实测注释;防「门未真跑」无证据)
- [x] 5.3 全套测试绿:`pytest tests/inspectors/ tests/ -q`(双矩阵 py3.11/py3.12,py3.11-only 失败用 pyenv 3.11.15 复现)
- [x] 5.4 PR 前对抗性 review(`/review-loop-codex`):重点 collector 归约正确性(空集 vacuous-true、NULL guard)、W3-6 防护分支、spec delta 与契约一致性
