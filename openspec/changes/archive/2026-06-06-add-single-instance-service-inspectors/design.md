## 上下文

基底 `add-service-inspector-contract-spike` 已 apply + 归档,产出 `service-inspector-contract`(8 条需求:连接注入安全 / secret 经 `HOSTLENS_` env remap / 失败三态分类 / 超时输出纪律 / 跨 target 无分叉 / 双轨 fixture / 边界止于单实例)。wave-1 已用 `os-shell-inspector-suite`(4 条需求)证明了「套件层覆盖契约 + 质量门」的铺量范式。

本变更(wave-2a)是 wave-2 的第一个铺量批次,需要:(1) 决定是否为 wave-2 service inspector 立**套件 capability**;(2) 确定 wave-2a 的 inspector 清单与切片判据。两个决策经 Codex 两轮对抗性辩论收敛,记录于「决策」节。

约束:零对外契约变更(沿用基底 schema 字段集 / capability enum / parse format / Agent 工具数组)、无新 Python 依赖、纯 YAML 无 hook.py、CI 全程 ReplayTarget 离线回放。

## 目标 / 非目标

**目标:**
- 立 `service-inspector-suite` capability,采用「稳定公共质量门 + 追加式冻结 cohort」结构,供 wave-2b/2c 复用且不回溯改 spec。
- 铺 6 个 wave-2a 单实例即时只读 inspector,每个守 `service-inspector-contract` + `inspector-authoring-contract`、附 ReplayTarget fixture + snapshot。
- 把 wave-2 切片判据钉死为可机械参照的形态(「即时快照 vs 持续 workload/时间窗口」)。

**非目标:**
- 不实现需持续 workload / 时间窗口的 inspector(wave-2b)、不实现多实例 replication(spike + wave-2c)。
- 不 MODIFY 已归档的 `inspector-authoring-contract` / `service-inspector-contract`。
- 不改 Agent / Target / 运行时 schema。

## 决策

### D-1:立 `service-inspector-suite`,用「追加式冻结 cohort」结构(Codex 第一轮 + 让步确认)

**选 (a) 现在立套件**,而非 (b) 逐 inspector 引用契约 / (c) 每 wave 各立套件。

- (b) 只能证单 manifest 合规,无法证「按域覆盖完整」,也无统一 wave 验收出口。
- (c) 三份 suite spec 必然漂移质量门(scaffold 明确要避免)。
- (a) 集中质量门,但**不能**写成会被后续 wave 持续扩写的「最终总清单」——否则 wave-2b 往里加单元格 = 对已归档 spec 的回溯 `MODIFY`。

**落地写法**:套件 spec 持有「稳定公共质量门」(对所有 wave-2 service inspector 普适)+ 每 wave 一条**归档时冻结**的 cohort 覆盖需求。后续 wave 用 `ADDED` 追加自己的 cohort 覆盖需求(sibling requirement),**永不 `MODIFY`** 已归档 wave 的需求。这复刻 `os-shell-inspector-suite` 的 D-9 手法:**把会随时间增长的具体清单降级到各 change 的 proposal/tasks,spec 层只持有稳定规则语义**;wave-2a cohort 的具体 6 个 inspector 名列在本 change 的 proposal/tasks,由 snapshot 验收,冻结于本 change 归档时。

> wave-2b 未来的 delta 形如(示意,非本 change 产出):
> ```
> ## 新增需求
> ### 需求:wave-2b 必须覆盖归档时冻结的累积/造故障单元格
> ...(其清单列在 wave-2b 的 proposal/tasks,由 snapshot 验收;不扩写、不解释 wave-2a 清单)
> ```

### D-2:`inspector-authoring-contract` 裸聚合键张力用就地澄清,不单起 MODIFY change(Codex 第二轮让步)

authoring-contract 第 22 行字面要求顶层键取自 `results/items/records`,与已交付 `redis.memory_usage` / `mysql.connection_usage` 的裸聚合标量键有字面张力。

**裁定:就地澄清,不改已归档 spec。** 三点论据:(1) **裸聚合键允许**(聚合型用不与 parameter 同名的裸标量键、而非 authoring-contract:22 字面要求的 `results/items/records`)是 `os-shell-inspector-suite` 已归档 spec 既定且被接受的解读,本套件**逐字沿用**;(2) 两个裸聚合键真例(`redis.memory_usage`/`mysql.connection_usage`)已过 CI/review/archive;(3) 该键命名是「作者纪律(prose + snapshot)」,**loader 不做机器门**(经 grep 核实 loader 无 top-key 校验),`validate`/`archive` 均无「先改被引用 spec」的机械门,故不改 authoring-contract 不会在任何流程步骤真出错。单起 MODIFY change 需多一轮 propose + RENAMED 工作流改已归档 spec,属范围蔓延。

**诚实区分(列表/聚合的判据本套件做了收紧、非逐字重述)**:`os-shell-inspector-suite` 用 **`for_each`** 区分列表/聚合型(配 for_each=列表型 / 无 for_each=聚合型)。本套件**不照搬该判据**——因 `docker.networks` 是反例(输出 `results` 列表但 finding 是标量 `dangling_networks >= warn_count`、**无 for_each**,按 for_each 判据会误归聚合型→`results` 键判错)。故本套件**改用更精确的判据:按 output_schema 是否含 array 顶层字段**区分(与 finding 是否用 for_each 正交)。这是对 os-shell 解读的**收紧/修正**,**不是逐字重述**;但它**不**触发对 authoring-contract 的 MODIFY(judgment 仍是「作者纪律 + loader 无机器门」,与判据用 for_each 还是 array-field 无关)。**被重述的只是「裸聚合键允许」这一条**,判据机制是本套件的改进。

**落地**:`service-inspector-suite` spec 在「守 authoring-contract」需求里就地写明:裸聚合键允许(重述 os-shell)+ 列表/聚合按 array 字段判据区分(本套件收紧)。

### D-3:wave-2 切片判据 = 「即时快照 vs 持续 workload/时间窗口」(Codex 第二轮让步)

Codex 第一轮误用「是否需主动构造异常」作判据,被反驳:基底自己就主动构造异常录 fixture(`mysql.connection_usage` 录 `access_denied` 故意设错密码、录 `conn_refused` 连不存在端口——经核验 tasks.md task 3.3)。

**收敛判据:异常态能否经有界、确定性 setup 后即时采样 → wave-2a;必须依赖持续 workload / 时间窗口累积 / 非确定性时序 → wave-2b。**

**「采样时刻持有的资源」≠「持续 workload」**(辨析,防把 wave-2a 项误判为 2b):为造 semantic-abnormal 而在采样瞬间**确定性持有的外部资源**(如 `postgres.connection_usage` 为造高连接率而保持的固定连接数、`redis.persistence` 为造变更堆积而预置的固定写入)**不算**持续 workload——采样那一刻是确定快照,与 `mysql.connection_usage` 基底用「保持连接」录 semantic-abnormal 同款。「持续 workload」**特指**采样窗口内**必须持续运行**才能命中的查询/流量(`postgres.long_queries` 的长查询须采样时仍在跑、`nginx.error_rate` 须时窗累积真 5xx)。本判据是 reviewer 判定门(非机械门),已写入 suite spec「wave-2a 必须覆盖归档时冻结的单实例即时只读服务单元格」需求。

逐个裁定(wave-2b 行为方向性参照、非穷举):

| inspector | 归属 | 理由 |
|---|---|---|
| `redis.persistence` | wave-2a | 固定写入后即时读 `rdb_changes_since_last_save` 快照(持有资源 ≠ workload) |
| `postgres.connection_usage` | wave-2a | 即时聚合采样;保持固定连接数 = 采样时刻持有资源,非持续 workload(与 mysql.connection_usage 对称) |
| `docker.images.disk_usage` | wave-2a | `docker system df` 即时快照;固定大小镜像即时超阈 |
| `docker.networks` | wave-2a | `docker network ls/inspect` 即时快照 |
| `nginx.health` | wave-2a | 即时状态;no-finding(up→ok/down→exception),不触发双轨机械门 |
| `nginx.config_test` | wave-2a | 静态坏配置一次性可录(finding-route,见 D-5) |
| `postgres.long_queries` | wave-2b | 需查询**持续运行**于采样窗口 + 时序协调 |
| `mysql.slow_queries` | wave-2b | 慢日志/累积窗口 |
| `nginx.error_rate` / `nginx.upstream` | wave-2b | 需时窗内累积真 5xx / upstream 故障流量 |
| `mysql.deadlocks` | wave-2b | 须主动造死锁 + `SHOW ENGINE INNODB STATUS` 累积态 |

### D-4:不新增 `docker.containers.unhealthy`(经核验的职责重复)

既有 `docker.containers.restart_loop.yaml` description 即「Detect containers stuck in a restart loop **or reporting unhealthy**」,projection 含 `health: .State.Health.Status`。新增独立 `docker.containers.unhealthy` 与之职责重叠。此判定独立于切片(不是 wave-2a/2b 问题,是「已覆盖勿重复」),故直接从清单剔除。

### D-5:`nginx.health`(no-finding 失败三态)与 `nginx.config_test`(finding-route)的失败语义裁定

**`nginx.health`——no-finding 失败三态**:语义是「服务在不在」:在→`status=ok`、不可达→`status=exception`(client 非零退出 fail-loud,绝不伪造健康)。`findings: []`。按 `service-inspector-contract` 双轨机械门「仅 `findings` 非空者要求 semantic-abnormal」,它**不受**该机械门约束,但仍须 up/down 两份 snapshot 证明 up→ok / down→exception。

**采集路径必须用 `curl` stub_status,不用 `systemctl is-active`**(录制可行性裁定,消除原未决问题):录制 lane 是 docker-compose 容器,容器内通常**无 systemd**,`systemctl` 会因 D-Bus 不可达而**恒非零退出**——这会让 up 态也落 `exception`、无法与 down 态区分,使 no-finding 三态验收坍缩。故 `nginx.health` collector **必须**走 `curl -fsS --max-time <M> http://{{ host | sh }}:{{ port }}{{ stub_status_path | sh }}`(nginx 镜像配 stub_status location,compose 可行;**URL 模板里 `port` 后不再写字面 `/`**——`stub_status_path` 自带前导 `/`,否则渲染出 `//path` 在未开 `merge_slashes` 时 404)。

**collector 须做抽取、声明 parse + output_schema**(满足作者契约「抽取在 collector 内」):`curl -fsS` 拿到 stub_status 文本后,collector **必须**在命令内把它归一成小 JSON(如 `awk`/`sed` 抽 `Active connections:` 行得 `active_connections` 整数,再 `printf` 出 `{"healthy":true,"active_connections":N}`),`parse.format: json`、`output_schema` 声明 `healthy`(bool)+ `active_connections`(int);nginx 在 + stub_status 可解析→exit 0→`ok`;nginx 停 / endpoint 不可达 / 解析不出 stub_status 字段→非零退出 + 空 stdout→`exception`。`findings: []`。`requires_binaries: [curl]`(capability `shell`,在既有 enum 内)。snapshot(tasks 5.4)断言 up 态 parse 出 `active_connections` 字段——证 curl 真打到 stub_status 而非任意 200 路径。

**注入安全**:`host`/`port`/`stub_status_path` 均为参数,**必须**遵守契约注入安全三件套——`host` pattern `^[a-zA-Z0-9._-]+$`、`port` int、`stub_status_path` **必须**用收紧 pattern `^/[A-Za-z0-9/_-]+$`(自带前导 `/`、**禁** `../`/scheme/query)且经 `| sh` 引用;`curl --max-time <M>`(M < `collect.timeout_seconds`)满足超时纪律。

**`nginx.config_test`——finding-route(collector 须吞 `nginx -t` 配置无效退出、但只吞 exit 1)**:`nginx -t` 成功收集到「配置无效」是一次**成功采集出异常结果**,不是「采集失败」,故坏配置→**finding**,**非** exception。**关键 collector 结构**:`nginx -t` 坏配置 **exit 1 且 verdict 写 stderr、stdout 空**;若直接透传,runner 会因空 stdout 判 exception,finding-route 不成立。故 collector 须:

1. **捕获 `nginx -t` 退出码 `rc` 与 stderr**(`err=$(nginx -t 2>&1); rc=$?`)。
2. **按 rc 分流(防把「不能执行」误判为「配置无效」),用「白名单 finding + 其余全 exception」兜底**:`rc == 0` = 配置有效 → `config_valid:true` 无 finding;`rc == 1` = nginx 已执行、配置有误 → 走 finding-route(`config_valid:false`);**其余一切非零 rc(`126`/`127`/`>128` signal 以及 `2..125` 等任何非 {0,1} 值)= 不可执行/被杀/未预期错误 → collector 自身 `exit 1` + 空 stdout → `exception`**(不是配置问题,**禁止**伪报 `config_valid:false`)。即:只有 rc∈{0,1} 走「采集成功」路径,其余全部 fail-loud 到 exception——不留 rc 区间空白。注:`requires_binaries: [nginx]` 的 preflight 已挡住「nginx 不存在」(→requires_unmet),此兜底覆盖罕见的「存在但不可执行/异常码」。
3. **JSON-safe 序列化 stderr**:**禁用** `printf '{"detail":"%s"}' "$err"`(stderr 常含引号/冒号/反斜杠/换行 → 非法 JSON → parse 失败 → 误 exception)。**必须**用 `jq -n --arg d "$err" '{config_valid:false,detail:$d}'`(`jq` 入 `requires_binaries`)做转义。

`parse.format: json`、`output_schema` 声明 `config_valid`(bool)+ `detail`(string);finding `when: "config_valid == false"`,semantic-abnormal fixture = **一份真实静态无效配置**(确定性一次性可录,默认严重度下产出预期 finding)。**注**:此 collector 与同批次 docker/redis/postgres 的「非零退出→exception」fail-loud 模式**方向相反**(config_test 是「采集成功、结果为坏配置」),故须在 manifest 注释显式说明。snapshot(tasks 5.5)**必须**覆盖含引号/反斜杠/换行的 stderr 场景(证 jq 转义正确)。这一裁定锁死 tasks 5.2/5.5 原「finding(或 exception)」的二义。

### D-6:secret 全部用 `HOSTLENS_` 前缀 + collector remap 到 client 原生 env

沿用基底契约:`redis.persistence` 用 `HOSTLENS_REDIS_PASSWORD`→`REDISCLI_AUTH`、`postgres.connection_usage` 用 `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD`;无鉴权实例导出空串过 preflight,collector 内 `[ -n "$VAR" ]` 分流。docker/nginx 三者(images_disk/networks/health/config_test)无连接 secret。manifest 注释 + 运行文档声明 SSH 上需远端 `AcceptEnv HOSTLENS_*`。

### D-7:docker 类 inspector 的契约应用(Go-template 大括号 / 有界输出 / 超时)

docker collector **必须**避开 Go-template `--format '{{...}}'`:`collect.command` 被 loader 用 **Jinja2** 渲染,`{{ }}` 是 Jinja 定界符,`docker system df --format '{{json .}}'` 会被 Jinja 解析失败致 manifest 注册不了(经核验既有 `docker.containers.restart_loop` 正是因此走 `docker inspect | jq` 投影、从不用 `--format '{{}}'`)。故 docker 两探针**必须**复刻该模式:用 `docker ... | jq` 在 collector 内投影/派生(`jq` 已是既有 docker 探针依赖);若个别命令必须用字面 `{{ }}`(如 `docker system df --format '{{json .}}'`),以 Jinja `{% raw %}...{% endraw %}` 包裹。

**pipe-exit 陷阱(必避)**:**禁止** `timeout docker ... | jq ...` 这种把 docker 直连 jq 的裸管道——POSIX `sh`(target 可能是 dash)默认只传播**最后一段(jq)**的退出码,docker 失败(daemon 不可达)时 jq 仍可能对空输入 `exit 0` → 整体 exit 0 → runner 误判 `ok`,违反契约「采集失败须非零退出 + 空 stdout」。`set -o pipefail` 非 POSIX 不可依赖。**必须**复刻 `containers_restart_loop` 的做法:docker 输出**先командой-substitution 捕获并 `|| exit 1` 单独 gate**(`out=$(timeout <N> docker system df --format '{% raw %}{{json .}}{% endraw %}') || { echo "docker df failed" >&2; exit 1; }`),**再**把 `$out` 喂给**另起一行**的 jq(jq 也 `|| exit 1` 单独 gate)。docker.networks 的多条 docker 调用同理逐条 command-sub + gate。

**有界输出**(契约 spec:79):`docker.networks` 的 `results` 列表**必须**在 collector 内截断为 top-N(参数 `max_results` **默认 50,冻结**)并保留 `dangling_networks` 作为 total 计数,**禁止**整表回吐高基数明细。

**超时**(契约 spec:79 的 docker 适配):docker CLI 无 DB client 那种 `--connect-timeout` flag;故 docker collector **必须**用 coreutils `timeout <N> docker ...`(N < `collect.timeout_seconds`)包裹每个 docker 调用以满足「不可达快速失败」,`timeout` 计入 `requires_binaries`(coreutils 系统二进制、非新 Python 依赖,故不破「零新 infra」)。**注**:既有 docker 探针 `containers_restart_loop` 的 `requires_binaries` 是 `[docker, jq, xargs]`、**不含 `timeout`**——故 `timeout` 是本批次 docker 探针**新增**的系统二进制前提(缺失→`requires_unmet`);录制 lane(含 macOS)须确保 coreutils `timeout` 可用。本地 socket 不可达时 `docker` 立即 ENOENT 非零退出;远端 `DOCKER_HOST` TCP 不可达由 `timeout` 兜底。

**`docker.images.disk_usage` 的字节陷阱**:`docker system df` 的 `Size`/`Reclaimable` 是**人类可读字符串**(`"6.952GB"` / `"4.104GB (59%)"`),**不是**数值字节——故**不**声明 `images_size_bytes` 这类数值字段(避免引入 `numfmt`/awk 的字符串→字节转换风险)。改为:collector 用 `timeout N docker system df --format '{% raw %}{{json .}}{% endraw %}'`(`{% raw %}` 包裹 Go-template 大括号避 Jinja 冲突,见上)`| jq` 选 `Type=="Images"` 行,**直接解析 docker 自报的 `Reclaimable` 字段里的百分比 `(NN%)`** 得 `reclaimable_pct`(数值);`size`/`reclaimable` 作**信息性字符串**原样保留(不当数值用)。finding `when: reclaimable_pct >= warn_reclaimable_pct`(默认 80)。此法零字符串→字节转换、复用 docker 已算好的百分比。**解析须带缺百分比兜底**:某些 docker 版本/零可回收时 `Reclaimable` 形如 `"0B"` 无 `(NN%)`——正则 capture 不命中时 `reclaimable_pct` **必须** `// 0`(真值 0%,非 null),与契约「禁伪造健康零值」不冲突(此处 0% 是真实无可回收、非采集失败掩盖)。

### D-8:`redis.persistence` finding 必须以 `aof_enabled` 为前提门(防 AOF 假阳性)

`rdb_changes_since_last_save` 高**不等于**数据有持久化风险——若 AOF 开启,这些变更已经 AOF fsync 持久化,仅凭 RDB 变更计数报 finding 是**假阳性**。故 `redis.persistence` 的 finding **必须**以 `aof_enabled == 0`(AOF 关闭)为前提:`when: "aof_enabled == 0 and rdb_changes_since_last_save >= warn_changes"`;`aof_enabled` **必须**进 `output_schema` 作为判据前提字段。语义即「**仅在 AOF 关闭时**,RDB 快照债(自上次 save 的未落盘变更)超阈才告警」。

## 风险 / 权衡

- **[docker socket 是高权限边界,非「无 secret 即无风险」]** → `docker.images.disk_usage` / `docker.networks` 与 DB-secret inspector 分作两个 implementation batch 验收;daemon/socket 不可达由 `docker` 命令 fail-loud → `exception`,**不**硬编码 `requires_files: [/var/run/docker.sock]`(`DOCKER_HOST` 可能指向他处)。docker 命令的大括号/有界输出/超时处理见 D-7。
- **[`docker.networks` 异常语义易失准]** → **冻结异常语义**:finding = 「存在**未被任何容器使用的非内置 user-defined network**」,输出 `dangling_networks`(total 计数)+ `results`(top-N 截断列表,带 network 名,见 D-7 有界输出);finding `when: dangling_networks >= warn_count`,**默认 `warn_count: 1`(本节冻结,实现不得改默认值)**。**禁止**把「网络总数多」或内置网络计入。**内置(非 user-defined)排除集**除默认网络 `bridge`/`host`/`none` 外,**还包含 Docker Swarm 自建系统网** `ingress`(按 inspect `.Ingress == true` 标识)与 `docker_gwbridge`(按名)——二者均由 Docker 引擎自建、非用户定义,空 `Containers` 时不应计为 dangling。这是对「非内置 user-defined」意图的完整实现(本风险节原仅列举 `{bridge, host, none}`,此处补全该意图,**非新增需求**)。
- **[`docker.images.disk_usage` 异常语义未冻结 + 字节转换陷阱]** → **冻结输出 schema 与 finding**:collector 经 `timeout N docker system df --format '{% raw %}{{json .}}{% endraw %}'` + `jq` 选 Images 行,**直接解析 docker 自报 `Reclaimable` 字段的百分比 `(NN%)`** 得 `reclaimable_pct`(数值);`size`/`reclaimable` 作信息性字符串保留(**不**声明 `_bytes` 数值字段——`docker system df` 输出是人类可读串如 "6.952GB",数值化需 numfmt/awk,故规避,见 D-7)。finding `when: reclaimable_pct >= warn_reclaimable_pct`,**默认 `warn_reclaimable_pct: 80.0`(本节冻结)**——语义即「可回收镜像磁盘占比过高 = 镜像层堆积浪费」。
- **[`redis.persistence` AOF 假阳性 + 失败态误纳]** → finding **必须**以 `aof_enabled == 0` 为前提门(见 D-8,防 AOF 开启时 RDB 变更计数假阳性);`aof_enabled` 进 output_schema。本批次只收「**AOF 关闭下** RDB 快照债超阈」这类即时快照异常;`warn_changes` 默认值实现期随真实 `INFO persistence` 量纲定(proposal 表已注),`rdb_last_bgsave_status=err` / AOF rewrite failure 等真实失败态需主动诱发,留 wave-2b。
- **[就地澄清裸聚合键被未来 reviewer 质疑]** → suite spec「遵守作者契约且输出键区分聚合与列表型」需求已改为**重述 os-shell 已归档先例的既定解读**(非本套件新授豁免)+ design D-2;经核验 loader 无 top-key 机器门、temp-archive dry-run exit 0、os-shell 双 spec 已在 main 共存,**archive 不会失败**。**FU 登记**:收紧 `inspector-authoring-contract` 第 22 行字面措辞(显式写入「列表型 results/items/records、聚合型裸标量键」区分),作独立 follow-up 根治 prose 张力,**非本变更阻塞项**。
- **[postgres 录制需 dbname 才能连]** → manifest 声明 `dbname` 参数(默认 `postgres`)、经 `| sh` + pattern;录制 lane 起单实例 postgres + 固定低 `max_connections` 录 semantic-abnormal。
- **[阈值 hysteresis 未冻结(已评估,判不适用)]** → 本批次 inspector 均为**无状态即时快照**,每次 run 报当前态,`>=` 阈值无跨 run 状态;边界抖动(flapping)是**下游告警层**(Notifier/调度)的关注点,不属无状态 inspector 快照语义。既有 `mysql.connection_usage` / `redis.memory_usage` 同样用裸 `>=` 无 hysteresis 且已交付——本批次沿用同口径,不为单批引入 hysteresis(否则与全契约不一致)。判定:**不适用、不阻塞**。

## 迁移计划

无运行时迁移:纯增 Inspector 文件 + capability spec。回滚 = 删新增 manifest/fixture/spec 目录,无状态、无 schema 变更。feature branch `feat/add-single-instance-service-inspectors` → PR → CI 绿 → squash-merge → 归档(delta 合入 `openspec/specs/service-inspector-suite/`)。

## 未决问题

- 已无阻塞性未决问题。`docker.networks` 异常语义、`nginx.health` 采集路径、`nginx.config_test` 失败语义均已在 D-3/D-5/风险节裁定冻结。
- 实现期细节(非契约、不阻塞 propose):`docker.networks` 的 `warn_count`(默认 1,已冻结)、`docker.images.disk_usage` 的 `warn_reclaimable_pct`(默认 80,已冻结)、`docker.networks` 的 `max_results`(top-N 截断)的具体值已在 D-7/风险节冻结;仅 `redis.persistence` 的 `warn_changes` 默认值随真实 `INFO persistence` 量纲在实现时定(suite spec「附 ReplayTarget fixture」需求的机械门仍硬约束:semantic-abnormal fixture 须在**最终**默认阈值下触发,且该默认值须落在 `healthy.rdb_changes < warn_changes ≤ abnormal.rdb_changes` 区间、由真实异常态写入量确定,见 tasks 2.1/2.3);`nginx.health` 所用 nginx 录制镜像的 stub_status location 配置在录制 lane 落地时写入。

> **altitude 约定**:suite spec 只持有跨 wave 稳定的**通用规则**;D-3~D-8 等**单 inspector 专有决策**(切片归属、docker 命令手法、redis AOF 门、nginx 采集路径)以本 design 为 SOT,不下沉进通用 suite spec(符合 D-1「具体清单/手法留 design+tasks、spec 只持稳定规则」)。spec 与 design 此处的详略差异是有意分层,非不一致。
