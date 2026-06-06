## 上下文

基底 `add-service-inspector-contract-spike`(`service-inspector-contract`,8 条需求)与 wave-2a `add-single-instance-service-inspectors`(`service-inspector-suite`,追加式冻结 cohort 结构 + 5 条公共质量门)**均已 apply + 归档**。本变更(wave-2b)是 wave-2 的第二个铺量批次,铺 wave-2a D-3 裁定表划给「持续 workload / 时间窗口累积」类的 service inspector。

经与用户确认,本批次 cohort 定为**稳健 3 个**:`mysql.slow_queries` / `postgres.long_queries` / `nginx.error_rate`;**推后** `nginx.upstream`(开源 nginx 无 upstream 状态面)与 `mysql.deadlocks`(并发竞态 + 自由文本解析,非确定性时序最难冻结)——见 D-8。

**已核验的真实 schema 事实(避免提案与代码漂移)**:`CollectSpec` 字段 = `{command, timeout_seconds, sampling_window}`、`extra="forbid"`;`ParseSpec` 是**独立顶层 `parse:` 块**(format∈`{raw,table,json,kv}`);`FindingRule` = `{for_each, when, severity∈{info,warning,critical}, message}`、`extra="forbid"`、message 是 **Python `.format()` 风格**;`window_start`/`window_end`/`window_seconds` 是 **runner 保留注入名**(loader `_validate_no_reserved_window_params` 拒绝同名 parameter);`collect.sampling_window: {duration_seconds:N}` 让 runner 用 **frozen clock** 算 `[now-N, now]` UTC 窗口、注入 `window_start`/`window_end`/`window_seconds` 进 Jinja render + Finding DSL 两上下文(已被 wave-1 `linux/disk_io`、`cron_*`、`log/tail_error_burst` 使用)。

约束:零对外契约变更(沿用 schema 字段集 / capability enum / parse format / Agent 工具数组)、无新 Python 依赖、纯 YAML 无 hook.py、CI 全程 `ReplayTarget` 离线回放。

## 目标 / 非目标

**目标:**
- 铺 3 个累积/时间窗口 service inspector,每个守 `service-inspector-contract` + `service-inspector-suite` 公共质量门 + `inspector-authoring-contract`、附 ReplayTarget fixture + 双轨 snapshot。
- 钉死 wave-2b 的核心新工程问题:**时间窗口/持续-workload 型 semantic-abnormal fixture 的确定性录制**(D-1 设计脊柱),并显式裁定每个 inspector 用 `collect.sampling_window` 注入窗口还是服务端 `NOW()`+标量冻结。
- 以 ADDED 向 `service-inspector-suite` 追加 wave-2b cohort sibling 覆盖需求,**不** MODIFY wave-2a 冻结清单。

**非目标:**
- 不实现 `nginx.upstream` / `mysql.deadlocks`(D-8 推后)、不实现多实例 replication(spike + wave-2c)。
- 不 MODIFY 已归档的 `inspector-authoring-contract` / `service-inspector-contract` / `service-inspector-suite` 公共需求 / wave-2a 冻结 cohort。
- 不改 Agent / Target / 运行时 schema;不回吐 offending-query 文本明细(D-7)。

## 决策

### D-1:确定性录制脊柱——窗口聚合在采样时刻于目标机内坍缩成标量、冻结进 stdout(贯穿本批次)

wave-2b 与 wave-2a 的根本差异:异常态依赖时间窗口/持续 workload,**天然带非确定性时序**。`ReplayTarget` 按**渲染命令字符串**匹配并原样返回录制 stdout。陷阱:若 collector 回吐**原始带时间戳明细**(逐条慢查询行 / 逐条 access log 行)、让 finding 层或下游按「`now()` 相对窗口」过滤计数,则回放时 `now` 已漂移、窗口内计数随之变化 → 非确定 → 违反 suite「离线回放确定性出结果」需求。

**裁定(本批次所有 inspector 必守)**:窗口/持续态的聚合**必须**在**采样时刻**于**目标机内**算成**最终标量**(计数 / 派生率 / 最长时长),写进 collector stdout JSON;`ReplayTarget` 回放原样返回该**已冻结标量**,**绝不**在回放时重新按时钟聚合;collector **禁止**回吐需回放端再按 `now()` 重聚合的原始时间戳明细。

**两种合规的窗口表达(显式裁定,不静默绕过 `sampling_window`)**:

- **(A) `collect.sampling_window` 注入式冻结窗口**:声明 `sampling_window: {duration_seconds:N}`,runner 用 frozen clock 把 `window_start`/`window_end`(格式化串)+ `window_seconds` 注入命令——渲染命令与窗口边界都被冻结、回放字节稳定。适合「窗口边界须出现在命令文本里」的采集(如 `journalctl --since {{ window_start }}`)。
- **(B) 服务端 `NOW()` + 标量冻结**:命令含**常量** interval(`NOW() - INTERVAL {{ lookback_seconds }} SECOND`,`lookback_seconds` 是普通数值参数、**非**保留名),窗口边界由**目标服务自身时钟**在执行时算、聚合 COUNT 直接坍缩成标量冻进 stdout。渲染命令无 `now()` 变量、字节稳定;回放返回冻结 COUNT。

**逐 inspector 裁定**:`mysql.slow_queries` 用 (B)(理由见 D-2:服务端 `NOW()` 与 `mysql.slow_log.start_time` **同时区**,避开注入 `window_start`(UTC)与 start_time(server TZ)的时区错配);`nginx.error_rate` **不用窗口注入**(D-4:窗口语义 = 当前日志整体,日志轮转即边界,避开 awk 内时间戳解析);`postgres.long_queries` **无窗口**(按查询已运行时长阈值判,非 `[now-N,now]` 窗口)。三者输出都是**与时钟无关的纯标量对象**,回放确定。

### D-2:`mysql.slow_queries` 采集走 `mysql.slow_log` 表 + 服务端 `NOW()` 窗口(参数 `lookback_seconds`,非保留名)+ 监控启用探采

**(1) 走 `mysql.slow_log` 表,不读 slow log 文件**:文件路径/格式跨发行版漂移、`awk` 解析 MySQL slow log 多行块脆,且绕开 secret 通道。表路径用 `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` remap,窗口聚合下推进 MySQL(D-1)。

**(2) 窗口用服务端 `NOW() - INTERVAL {{ lookback_seconds }} SECOND`、参数名 `lookback_seconds`(关键修正)**:**禁**用 `window_seconds` 作 parameter 名——它是 runner 保留注入名,loader 会以 `parameter_reserved_window_name` **拒绝加载**。选 (B) 而非 `collect.sampling_window` 的 (A) 是**有意裁定**:`sampling_window` 注入的 `window_start`/`window_end` 是 **runner 的 UTC** 串,而 `mysql.slow_log.start_time` 存的是 **MySQL 会话/系统时区**——两者直接比较有时区错配风险,须额外 `CONVERT_TZ`/设会话 TZ 才正确;而服务端 `NOW()` 与 `start_time` **同源同时区**、无错配,且 COUNT 标量冻结已满足回放确定性(D-1),故对本 inspector (B) 更稳健。`lookback_seconds`:`type: integer, minimum: 1`,数值参数无需 `| sh`。**输出键只 `slow_query_count` + `slow_log_monitoring_enabled`,不输出 `window_seconds`**(避免与保留名/参数同名遮蔽,见 D-7)。

**(3) 监控启用探采,防 `log_output=FILE` 静默健康盲区(关键修正)**:`mysql.slow_log` 表仅在 `slow_query_log=ON` 且 `log_output` 含 `TABLE` 时有数据(默认 `log_output=FILE`)。若不探采,未启用 TABLE 日志的实例会**永远** `slow_query_count=0` → 被误读为「健康」——监控盲区。故 collector **必须先探采** `@@global.slow_query_log` 与 `@@global.log_output`,派生 `slow_log_monitoring_enabled`(bool)写进 output;finding 分两支:**监控未启用 → `warning`「慢查询 TABLE 日志未启用,无法巡检」**(诚实暴露盲区,**不**伪报 ok+0);监控启用且 `slow_query_count >= warn_count` → `warning`。这把「特性禁用读成健康」从静默盲区转成可见信号。

**(4) mysql client 失败语义(补全 collector body 要点,mysql 无 `psql` 的 `ON_ERROR_STOP`)**:`mysql` client **默认不**像 `psql -v ON_ERROR_STOP=1` 那样 stop-on-error,但查询失败时进程**仍非零退出**——故 collector 用 `out=$(mysql --batch -N --connect-timeout=5 -h {{host|sh}} -P {{port}} -u {{user|sh}} -e "SQL") || { echo 'mysql slow_queries failed' >&2; exit 1; }`(`--batch -N` 出无表头 tab 分隔、`--connect-timeout=5` < `collect.timeout_seconds`),再 `case` 数值校验 `slow_query_count`,非数值/空→`exit 1`+空 stdout→`exception`。`MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}"` env remap、从不进 argv。**失败三态映射**:缺 `mysql`/缺 secret env→`requires_unmet`(preflight);服务不可达/认证失败/`slow_log` 表无权→非零退出+空 stdout→`exception`;表存在但本窗口无慢查询→`count=0`+`monitoring_enabled=true`→`ok`(真实空,与采集失败区分)。tasks 2.1 须落地完整 mysql collector body(对齐 proposal 的 postgres 样板 fail-loud 模式)。

### D-3:`postgres.long_queries` 必须排除 inspector 自身连接 + 冻结时长;semantic-abnormal 须真实持续长查询

`pg_stat_activity` 包含**本 inspector 发出的那条查询自身**(采样瞬间也 `state='active'`)。不排除则健康实例会因自身被计为长查询(边界态假阳性)。**裁定**:SQL 必须 `AND pid <> pg_backend_pid()` 排除自身后端。`max_duration_seconds = max(extract(epoch FROM now()-query_start))::int` 在目标 PG 内算出(D-1),标量冻结。

**semantic-abnormal 录制(契约硬条款,真造异常)**:录制器**必须**起一条**真实持续运行**的长查询(另开后台连接执行 `SELECT pg_sleep(big)`),待其 active 时长超过 manifest **默认** `threshold_seconds` 后**再采样**,使 `long_query_count >= warn_count`(默认 1)在默认阈值下触发。采样瞬间 `now()-query_start` 已是确定值、算成标量冻进 fixture。**禁止**用「健康态 + 把 `warn_count`/`threshold` 压到 0」凑。healthy fixture 须证「排除自身后 count=0」(防 inspector 自计的 vacuous 触发)。

### D-4:`nginx.error_rate` = 当前 access log 整体 5xx 率(日志轮转=窗口边界)+ 正确 awk 状态码字段 + `min_requests` 小样本门

**窗口语义裁定(不用 `sampling_window`)**:**不**在 awk 内做「日志时间戳 vs `now()` 滑动窗口」(nginx 日志时间格式 `[10/Oct/2000:13:55:36 -0700]` 在 awk 内解析成 epoch 比 `now` 既脆又重新引入回放端时钟依赖,与 D-1 冲突;`sampling_window` 注入的 UTC 边界同样要 awk 解析日志时间戳来比对,不解决脆性)。改用**当前 access log 文件整体**的 5xx/total 率:**日志轮转即窗口边界**(运维按时段轮转 access log,「自上次轮转以来错误率」是真实可解释信号)。聚合在采样时于目标机 awk 算成派生率标量(D-1),回放确定。

**awk 状态码字段必须正确取(关键修正)**:**禁**用 `$status`——awk 无命名字段,`$status` 是「以变量 `status`(未定义→0)为下标的字段」即 `$0`(整行),`$status ~ /^5/` 会错配任何含 5xx 样子子串的整行。combined/default 格式状态码是**第 9 字段**:`LC_ALL=C awk -v sf={{ status_field }} '{ total++; if ($sf ~ /^5/) e++ } END { ... }'`,参数 `status_field`(`type: integer, default: 9`)文档式声明假设 combined/default `log_format`(状态码在第 9 列);**非 combined 格式(状态码不在第 9 列)须显式覆盖 `status_field`**——这是参数化逃生舱,manifest `description` 须声明该假设(契约对非默认 `log_format` 沉默,本 inspector 不做格式自动探测)。

**`LC_ALL=C` 强制(关键修正)**:awk 的 `sprintf("%.2f",…)` 在逗号小数 locale(de/fr 等)下会出 `0,50` → **非法 JSON → parse 失败 → 健康 nginx 误落 `exception`**。故 awk 调用**必须** `LC_ALL=C` 前缀,逐字对齐已交付先例 `builtin/postgres/connection_usage.yaml:60`。(mysql/postgres 的 collector 只出整数/bool、无 locale 暴露,仅 nginx 这条派生浮点率受影响;tasks 5.x 加一条跨 inspector 静态断言「凡 awk 出 `%f` 必 `LC_ALL=C` 前缀」。)

**collector 结构 + 失败语义(静态日志路径,不参数化——关键修正)**:**本批次不引入 `access_log_path` 参数**,collector 与 `requires_files` 用**同一静态路径** `/var/log/nginx/access.log`。原因:`requires_files` 是 schema 的**静态字符串列表、不能引用 parameter**,而 runner preflight 在命令 render 前就探 `manifest.requires_files`——若日志路径参数化、`requires_files` 只能写默认路径,则「传非默认可读路径但默认路径缺失」会被误判 `requires_unmet`、「默认存在但传入路径缺失」会 preflight 通过再落 `exception`,**破坏契约失败分类**。静态路径使 preflight 探测路径 ≡ collector 读取路径,失败分类自洽:缺日志/**不可读**→`requires_unmet`(runner 的 `requires_files` preflight 是 `[ -r path ]`,存在但不可读同样非零→`requires_unmet`,**非** exception——已核验 runner 现行语义,此为契约一致的粗粒度归类)、真实空→`ok`、采集本身失败(awk 异常退出)→`exception`。**可配置日志路径留未来增强**(需先设计参数化 file-preflight,超出本批次「零 schema/infra 变更」)。`requires_binaries: [awk]`。**`END{}` 必须对空输入产合法零对象并 exit 0**:`END { if (total==0) {rate="0.00"} else {rate=sprintf("%.2f",(e/total)*100)}; printf "{\"total_requests\":%d,\"error_5xx_count\":%d,\"error_rate_pct\":%s}", total, e, rate }`——空日志(0 行)→`total_requests=0`/`error_rate_pct=0`(真值,除零防护)→finding 因 `total_requests >= min_requests` 不满足而不触发→`ok`(真实空,与采集失败区分)。finding `when: "error_rate_pct >= warn_pct and total_requests >= min_requests"`(**小样本门**防「2 请求 1 错=50%」假阳性)。

### D-5:以 ADDED 向 `service-inspector-suite` 追加 wave-2b cohort sibling 需求,永不 MODIFY wave-2a

`service-inspector-suite` spec 已冻结「追加式冻结 cohort」结构:后续 wave **必须**用 `新增需求`(ADDED)追加**仅约束自己 cohort** 的 sibling 覆盖需求,**禁止** MODIFY 已归档 wave、**禁止**改写已冻结的 wave-2a 清单;标题**必须** wave-prefixed 且全套件唯一(避免 archive rebuild 把 ADDED 误判为 MODIFY)。

**落地**:本变更 delta 在 `## 新增需求` 下加**一条**「需求:wave-2b 必须覆盖归档时冻结的累积/时间窗口服务单元格」,其 cohort 3 个 inspector 清单**留本 change proposal/tasks**、由 snapshot 验收、冻结于本 change 归档时(复刻 wave-2a D-1 altitude 约定)。公共质量门(守契约 / 双轨 fixture / 零新 infra / 干净注册 / 输出键区分)**已在 wave-2a 立的公共需求里**,本覆盖需求**引用**之、不重述。

### D-6:secret 全部用 `HOSTLENS_` 前缀 + collector remap 到 client 原生 env

沿用基底契约:`mysql.slow_queries` 用 `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD`、`postgres.long_queries` 用 `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD`;无鉴权实例导出空串过 preflight,collector 内 `export VAR="${HOSTLENS_...:-}"` 透传(空串即「无鉴权」)。`nginx.error_rate` 读日志文件、**无连接 secret**(对 secret 相关 MUST 属空集满足/不适用)。client 原生 env 通道限于 suite-wide 允许集 `{REDISCLI_AUTH, PGPASSWORD, MYSQL_PWD}`(本批次用后两者)。manifest 注释 + 运行文档声明 SSH 上需远端 `AcceptEnv HOSTLENS_*`。

### D-7:输出为纯聚合标量裸键;键名与参数/保留名均不碰撞;刻意不回吐 offending-query 文本明细

三者输出**均无 array 顶层字段**(纯聚合标量)→ 顶层用**裸标量键**,与 `redis.memory_usage` / `mysql.connection_usage` 先例及 suite「按 output_schema 是否含 array 字段区分」需求一致。**键名碰撞硬约束(runner 合并 `{**output, **parameters, **window_context}`,后者遮蔽前者)**:输出键**禁止**与任一 parameter 同名、**禁止**用保留窗口名 `window_start`/`window_end`/`window_seconds`。落地键:`mysql.slow_queries` = `slow_query_count`(int) + `slow_log_monitoring_enabled`(bool);`postgres.long_queries` = `long_query_count`(int) + `max_duration_seconds`(int);`nginx.error_rate` = `total_requests`(int) + `error_5xx_count`(int) + `error_rate_pct`(number)。参数名 `lookback_seconds`/`threshold_seconds`/`warn_count`/`warn_pct`/`min_requests`/`status_field`(nginx 日志路径**静态非参数**,见 D-4)与上述输出键**无交集**(已核验),故**无需**「实现期再核验改名」的 hedge——名已在本 design 钉死。

**刻意不回吐明细**:长查询/慢查询的 SQL 文本可能含表名、`WHERE` 字面值(潜在敏感),offending 列表属高基数明细(契约禁回吐)。故**只报聚合标量**,**不**输出 `results`/`items`/`records` 列表。带脱敏的 detail-listing 登记为未来增强,**非本批次**。此决策同时使三者全为「无 array 字段」纯聚合型、规避 results/items/records 键判据。

### D-8:`nginx.upstream` 与 `mysql.deadlocks` 本批次推后(经用户确认)

- **`nginx.upstream`**:upstream 健康/失败计数在**开源 nginx 无状态面**(stub_status 不含 upstream;`/upstream_conf`/API 仅 nginx Plus)。唯一开源路径是解析 access log 的 `$upstream_status`/`$upstream_addr`——但要求运维**特定 `log_format`**(非默认),前提强、跨环境脆,且与 error_rate 的日志解析风险叠加。
- **`mysql.deadlocks`**:须**并发事务竞态**主动造死锁(MySQL 回滚一方并写 `LATEST DETECTED DEADLOCK`),录制需多连接时序编排;`SHOW ENGINE INNODB STATUS` 是**自由文本**(无结构化死锁计数 status var),解析脆。wave-2 里**非确定性时序最难冻结**的一类。

二者**不强塞**进本批次(避免拖累 3 项闭环),推后到后续批次或单独 spike 裁定(类比 replication「先证后铺」)。**登记**:M6 覆盖矩阵的对应 cell 标注「wave-2b 推后,待后续批次/spike」,避免成为 M6 退出核验的永久空位歧义。

## 风险 / 权衡

- **[`mysql.slow_queries` 依赖 `slow_query_log=ON` + `log_output=TABLE` 非默认配置]** → **不**做静默健康盲区:collector 探采 `@@global.slow_query_log`/`@@global.log_output`,未启用 TABLE 日志→`slow_log_monitoring_enabled=false`→`warning`(诚实暴露,D-2(3));表无权→fail-loud→exception。录制 lane 起 compose mysql 时 `SET GLOBAL slow_query_log=ON; SET GLOBAL log_output='TABLE'`,并以**真实慢查询**(`long_query_time=1` + `SELECT SLEEP(2)` 等)产 TABLE 慢日志,**禁** `long_query_time=0` 噪声充数(见下条)。
- **[录制 lane 把「低阈值技巧」搬到服务端配置]** → 契约禁「健康态+低阈值」凑 semantic-abnormal;若录 mysql 慢日志用 `long_query_time=0` 则全部快查询都进 slow_log、"异常"是阈值=0 噪声而非真慢查询风暴——**结构等价于被禁的把戏、只是搬到服务端**。**裁定**:录制 lane 用**真实 `long_query_time`(如 1s)+ 真造慢查询**(`SELECT SLEEP(2)` / 大表无索引扫),使 logged 行是**真慢事件**(tasks 1.1/1.3/2.4)。
- **[窗口聚合的回放确定性]** → D-1 脊柱:聚合在录制机一侧坍缩成标量冻结,回放无时钟依赖;snapshot 断言冻结标量,**禁止**回放端按 `now()` 重聚合。跨 inspector 静态断言「output_schema 无带时间戳明细 array」佐证。
- **[`mysql.slow_queries` 用服务端 NOW() 而非 sampling_window]** → 有意裁定(D-2(2)):`sampling_window` 注入 UTC `window_start` 与 `mysql.slow_log.start_time`(server TZ)有时区错配;服务端 `NOW()` 同源同区无错配,标量冻结已满足回放确定性。acknowledge `sampling_window` 一等机制存在、本 inspector 经权衡不用,**非静默绕过**。
- **[`nginx.error_rate` awk 字段错位 / 小样本假阳性]** → awk 用正确状态码字段 `$status_field`(默认 9,非裸 `$status`,D-4),文档式声明 combined/default `log_format` 假设;`total_requests >= min_requests` 合取门防小样本;`END{}` 对空输入产合法零对象 exit 0(除零防护)。semantic-abnormal fixture 须**真实足量 5xx 流量**(总请求 >= `min_requests` 且 5xx 率超 `warn_pct`),snapshot 双向锁。
- **[`postgres.long_queries` 自身查询假阳性]** → D-3 `pid <> pg_backend_pid()` 排除自身;snapshot 含「健康实例→count=0」证排除生效。
- **[secret 不泄漏 + 反 vacuous]** → 仅对声明 `secrets` 非空的 manifest(mysql.slow_queries / postgres.long_queries)跑泄漏扫描;**须扩** `_RECORDED_SECRET_VALUES` 加入本批次每个 secret recorder 实际注入的密码常量,否则「不泄漏」断言扫 fixture 里不存在的值 → vacuous 恒真(复刻 wave-2a 6.4 教训)。`nginx.error_rate` 无 secret、不纳入。
- **[共享 cross-inspector 测试结构波及 wave-2a]** → `test_service_contract_crosscheck.py` 有**硬编码冻结结构**(`_ALL_SERVICE_MANIFESTS` 计数 8、`_SECRET_SERVICE_MANIFESTS` 计数 4、`_SECRET_CLIENT_RULES`/`_CLIENT_TIMEOUT_TOKEN`/`_NO_CONNECT_TIMEOUT`/`_INJECTABLE_PARAMS`/`_REQUIRES_USER`/`_RECORDED_SECRET_VALUES`),本批次新增 3 inspector 会破计数断言——tasks 5.8 列全须同步项(计数 8→11、4→6 等)。既有 `test_output_shape_by_array_field_not_for_each`(真实名,**非** `test_output_is_aggregate_scalar_object`)已是 array-field 驱动、对纯标量 cohort 通用,纳入新 manifest 后**自动覆盖、无需改写测试体**;改测试文件合法(是 wave-2a 测试代码、非冻结 spec),须确认不扰动 wave-2a 既有断言(回归核验)。
- **[阈值 hysteresis 未冻结]** → 沿用 wave-2a 判定:无状态即时快照(本批次聚合标量同理),`>=` 阈值无跨 run 状态;flapping 是下游告警层关注点,不为单批引入 hysteresis。判定:不适用、不阻塞。
- **[`mysql.deadlocks`/`nginx.upstream` 推后留 M6 矩阵空位]** → D-8 在覆盖矩阵显式标注「wave-2b 推后」,非永久空位歧义。

## 迁移计划

无运行时迁移:纯增 Inspector 文件 + suite spec 的 ADDED 覆盖需求。回滚 = 删新增 manifest/fixture/test + 撤 spec delta,无状态、无 schema 变更。feature branch `feat/add-log-and-fault-service-inspectors` → PR → CI 绿 → squash-merge → 归档(delta ADDED 合入 `openspec/specs/service-inspector-suite/`,**不**触 wave-2a 冻结需求)。

## 未决问题

- 已无阻塞性未决问题。采集手法(D-2 slow_log TABLE + 服务端 NOW() + 监控探采 / D-3 排除自身 / D-4 整文件率 + 正确 awk 字段 + min_requests)、窗口确定性与 sampling_window 裁定(D-1/D-2/D-4)、suite 追加方式(D-5)、键名碰撞(D-7)、推后项(D-8)均已裁定。
- 实现期细节(非契约、不阻塞 propose):`mysql.slow_queries` 的 `lookback_seconds`/`warn_count` 默认值、`postgres.long_queries` 的 `threshold_seconds`/`warn_count` 默认值、`nginx.error_rate` 的 `warn_pct`/`min_requests` 默认值,均随真实异常态量纲在实现时定——suite「附 ReplayTarget fixture」机械门硬约束:semantic-abnormal fixture 须在**最终**默认阈值下触发,录制与定值同 PR 闭环、snapshot 对 healthy(无 finding)/abnormal(默认阈值出 finding)双向锁。

> **altitude 约定**:suite spec 只持跨 wave 稳定通用规则 + 每 wave 一条冻结覆盖需求;D-1~D-8 等**单 inspector / 单批次专有决策**(确定性脊柱、各采集手法、sampling_window 裁定、键名碰撞、推后项)以本 design 为 SOT,不下沉进通用 suite spec(符合 wave-2a D-1「具体清单/手法留 design+tasks、spec 只持稳定规则」)。
