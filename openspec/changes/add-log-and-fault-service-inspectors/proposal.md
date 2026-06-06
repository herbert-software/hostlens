## 为什么

M6 wave-2 的「累积状态 / 时间窗口聚合」铺量批次(wave-2b)。基底 `add-service-inspector-contract-spike` 与 wave-2a `add-single-instance-service-inspectors` **均已 apply + 归档**,`service-inspector-contract`(8 条需求)与 `service-inspector-suite`(追加式冻结 cohort 结构)已冻结——本批次的服务契约前提已就绪,可从骨架提升为完整提案。

本批次 inspector 与 wave-2a「单实例即时只读」的工程风险**不同**:它们的 semantic-abnormal 异常态**不能**经有界、确定性 setup 后即时采到,而**必须依赖采样窗口内持续运行的 workload(长查询)或时间窗口内累积的真实事件/流量(慢日志、5xx 流量)**——这是 wave-2 切片判据(D-3,已冻结进 suite spec)划给 wave-2b 的那一类采集风险。单独成批使这类「持续/累积」录制风险不拖累单实例只读批次。

切片判据(reviewer 判定门,沿用 Codex 两轮对抗结论):**确定性即时快照(含采样时刻持有的固定资源)→ wave-2a;须持续运行的 workload / 时间窗口累积 / 非确定性时序 → wave-2b**。三个本批次 inspector 全部落在后者。

**本批次的核心新工程问题**:时间窗口 / 持续 workload 型 semantic-abnormal fixture 的**确定性录制**。窗口聚合若在采样时按「`now()` 相对窗口」计算、且把**原始带时间戳明细**回吐给回放端重新过滤计数,则回放时 `now` 已漂移、计数随之变化,破坏离线回放确定性。解法钉死为一条贯穿本批次的设计脊柱(design D-1):**窗口聚合在采样时刻于目标机内坍缩成最终标量、冻结进 collector 的 stdout;`ReplayTarget` 原样返回该冻结标量,collector 禁止回吐需在回放时按 `now()` 重聚合的原始带时间戳明细**。代码库已有的 `collect.sampling_window`(runner 用 frozen clock 注入 `window_start`/`window_end`/`window_seconds`、ReplayTarget 按渲染命令字节匹配)是「注入式冻结窗口」的一等机制;本批次各 inspector 显式裁定用它还是用服务端 `NOW()`+标量冻结(见 D-1/D-2/D-4,不静默绕过)。

## 变更内容

### 新增 3 个累积/时间窗口 service inspector(纯 YAML,无 hook.py)

| 域 | inspector(registry name) | 采集手法 | 2b 归属理由 | secret |
|---|---|---|---|---|
| mysql | `mysql.slow_queries` | `mysql` client 查 `mysql.slow_log` 表(需 `slow_query_log=ON` 且 `log_output` 含 `TABLE`,见 D-2):**先探采监控是否启用**(`@@global.slow_query_log`/`@@global.log_output`)→ `slow_log_monitoring_enabled`(bool),再服务端 SQL `WHERE start_time >= NOW() - INTERVAL {{ lookback_seconds }} SECOND` 聚合 `slow_query_count`(标量冻结,D-1);finding:监控未启用→`warning`(诚实暴露盲区,**不**报 ok+0)、启用且 `slow_query_count >= warn_count`→`warning` | 慢查询事件须在时间窗口内**真实累积**才能命中 | `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` |
| postgres | `postgres.long_queries` | `psql` 即时查 `pg_stat_activity`,SQL 内 `state='active' AND now()-query_start >= threshold` 且 `pid != pg_backend_pid()`(排除自身,见 D-3)聚合出 `long_query_count` + `max_duration_seconds`(标量冻结);finding `long_query_count >= warn_count` | 长查询须在**采样窗口内持续运行**才能采到(时序协调) | `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD` |
| nginx | `nginx.error_rate` | **`LC_ALL=C awk`**(防逗号小数 locale 破 JSON,对齐 connection_usage 先例)在目标机内单遍扫**静态路径** `/var/log/nginx/access.log`(`requires_files` 同一静态路径,缺失→`requires_unmet`;**不参数化路径**,见 D-4):**按 combined/default 格式取状态码字段 `$status_field`(默认 9,非裸 `$status`,见 D-4)**聚合 `total_requests` / `error_5xx_count` / 派生 `error_rate_pct`(标量冻结);finding `error_rate_pct >= warn_pct **and** total_requests >= min_requests`(小样本门防 1 请求 1 错=100% 假阳性) | 5xx 率须时窗内**累积真实流量**才有意义 | 无 |

> **三者均为 findings 非空 inspector** → 全部**受** `service-inspector-contract` 双轨 fixture 机械门约束:每个**必须**附一份在 manifest **默认阈值**下触发 finding 的 semantic-abnormal fixture,且该异常态**必须真造**(真实慢查询累积 / 真实持续长查询 / 真实 5xx 流量),**禁止**用「健康态 + 人为低阈值」凑(契约硬条款)——**含禁止把低阈值技巧搬到服务端配置**(如录 mysql 慢日志用 `long_query_time=0` 把全部快查询当慢日志录,见 D-2/tasks 1.3)。无 no-finding inspector。
>
> **输出形态**:三者均为**纯聚合标量**输出(无 array 顶层字段)→ 顶层用**裸标量键**(与 `redis.memory_usage` / `mysql.connection_usage` 先例一致),不取 `results/items/records`(suite spec「按输出形态区分」需求)。**刻意不**回吐 offending-query 明细列表:长查询/慢查询文本可能含表名与字面值(敏感),且属高基数明细(契约禁回吐),故只报聚合标量;detail-listing 留未来带脱敏的增强(非本批次)。

### 新增 capability 覆盖需求:`service-inspector-suite` 追加 wave-2b cohort(ADDED,**不** MODIFY wave-2a)

沿用 wave-2a 立的「稳定公共质量门 + 追加式冻结 cohort」结构(suite spec 已冻结该规则):本批次以 `新增需求`(ADDED)向 `service-inspector-suite` 追加**一条仅约束 wave-2b cohort** 的 sibling 覆盖需求(标题 wave-prefixed、全套件唯一),**禁止** `MODIFY` 已归档的 wave-2a 冻结清单、**禁止**改写或扩写 wave-2a。wave-2b 的具体 3 个 inspector 清单留本 change 的 proposal/tasks,由 snapshot 验收,冻结于本 change 归档时。公共质量门(守契约 / 双轨 fixture / 零新 infra / 干净注册)**引用**已冻结的 suite 公共需求,不重述。

## 功能 (Capabilities)

### 新增功能
- 无新 capability。复用 wave-2a 已立的 `service-inspector-suite`,仅以 ADDED 追加 wave-2b cohort 覆盖需求(sibling),不另立套件 spec(避免质量门漂移)。

### 修改功能
- 无。`service-inspector-contract` / `service-inspector-suite` 公共质量门 / `inspector-authoring-contract` 仅被**引用**,不 MODIFY。wave-2b cohort 覆盖需求是对 suite 的 ADDED,**不**触及 wave-2a 已冻结需求(suite spec 明令「禁止 MODIFY 已归档 wave」)。

## 影响

- 新增 `builtin/{mysql,postgres,nginx}/*.yaml`(`builtin/nginx/` 目录已由 wave-2a 建)+ 对应 ReplayTarget fixture / snapshot 测试 / 录制入口。
- 复用 wave-2a 的 docker-compose 录制 lane(单实例 mysql / postgres / nginx);**新增录制协调**:**真实**慢查询累积(`long_query_time` 设为真实值如 1s + 跑 `SELECT SLEEP(2)` 等真慢查询,**禁** `long_query_time=0` 噪声充数,见 tasks 1.3)、持续长查询的后台连接 + 采样时序、真实 5xx 流量生成(见 tasks)。
- **新增系统二进制前提**:`nginx.error_rate` 需 `awk`(coreutils/系统二进制,非新 Python 依赖,不破「零新 infra」);`requires_files` 声明 access log 路径(缺失→`requires_unmet`)。
- **对外契约影响:零**。沿用 manifest schema 字段集、capability enum(`{shell, file_read, ssh, systemd, docker_cli}`)、parse format(`raw/table/json/kv`)、`collect.sampling_window` 既有字段;**Agent 可见工具数组不增减**(不注册任何新 `ToolSpec`);无新 Python 依赖;不 enable `hook.py`;不新增 parse format。
- **Agent 行为影响:零**。只增 Inspector(SOT),不改 Agent loop / prompt / tool schema,无 prompt caching / token 影响。

## 非目标 (Non-Goals)

- ❌ **不含** `nginx.upstream`(开源 nginx 无 upstream 状态面,只能走特定 `log_format $upstream_status` 解析)与 `mysql.deadlocks`(须并发事务竞态造死锁 + 解析自由文本 `SHOW ENGINE INNODB STATUS`,非确定性时序最难冻结)——二者录制风险与设计复杂度显著高于本批次三项,推后到后续批次/单独 spike 裁定(类比 replication 先证后铺),不强塞进本批次拖累闭环。
- ❌ **不含**单实例即时只读 inspector(已属 wave-2a)、不含多实例 / replication(留 replication spike + wave-2c)。
- ❌ semantic-abnormal fixture **禁止**用「健康态 + 低阈值」凑——本批次价值依赖异常专属信号(慢查询累积数 / 持续长查询数与时长 / 5xx 率),**必须真造**对应异常态录制(契约双轨 fixture 硬条款);**且禁把低阈值技巧搬到服务端配置**(如 `long_query_time=0`)伪造累积。
- ❌ **不**回吐 offending-query 文本明细列表(敏感 + 高基数),只报聚合标量;带脱敏的 detail-listing 留未来增强。
- ❌ **不** MODIFY 已归档的 `service-inspector-contract` / `service-inspector-suite` 公共需求 / wave-2a 冻结 cohort / `inspector-authoring-contract`。
- ❌ **不**引入新 parse format / 新 secret 机制 / 新 capability / DockerTarget / `hook.py`。

## Demo Path

```bash
# 三个新 inspector 干净注册(registry errors == [])
hostlens inspectors list | grep -E 'mysql.slow_queries|postgres.long_queries|nginx.error_rate'

# 缺 client/日志/secret → requires_unmet(不中断同 run 其它 inspector)
hostlens inspect --inspector postgres.long_queries --target local --json   # 无 psql/secret → requires_unmet

# 离线 ReplayTarget 回放 snapshot(不触真实服务,CI 默认路径)
pytest tests/inspectors/test_mysql_slow_queries.py \
       tests/inspectors/test_postgres_long_queries.py \
       tests/inspectors/test_nginx_error_rate.py -q
```

## 完整 YAML manifest 示例(`postgres.long_queries`)

证明「持续 workload 型」inspector 在现有 schema 内即可实现、无需新 infra(SQL 内聚合 + 自身排除 + 标量冻结)。**结构严格对齐已交付 `builtin/postgres/connection_usage.yaml`**:`collect` 仅 `command`/`timeout_seconds`、`parse` 为**独立顶层块**、`findings` 仅 `when`/`severity`/`message`(`FindingRule` 是 `extra=forbid`,**无** `id`/`title` 字段)、`message` 用 **Python `.format()` 风格单大括号** `{field}`(**非** Jinja `{{ }}`):

```yaml
name: postgres.long_queries
version: 1.0.0
description: >-
  PostgreSQL 长查询巡检(单实例)。需 psql client;secret 声明
  HOSTLENS_POSTGRES_PASSWORD(HOSTLENS_ 前缀对齐 ssh-execution-target 契约),
  collector 内 remap 到 PGPASSWORD(从不内联进 argv)。SQL 内聚合 active 且
  运行时长超阈的后端为标量(long_query_count / max_duration_seconds),排除
  inspector 自身连接(pid != pg_backend_pid())。聚合值在采样时算成标量、冻结进
  输出;服务不可达/认证失败 → status=exception;缺 psql → requires_unmet。
  SSH 上需远端 sshd 配 AcceptEnv HOSTLENS_*。
tags: [postgres, service, long-queries, workload]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [psql]
secrets: [HOSTLENS_POSTGRES_PASSWORD]
privilege: none

parameters:
  type: object
  required: [user]
  properties:
    host:
      type: string
      pattern: "^[a-zA-Z0-9._-]+$"          # blocks injection in -h value
      default: "127.0.0.1"
    port:
      type: integer
      minimum: 1
      maximum: 65535
      default: 5432
    user:
      type: string
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"   # anchored first char, aligned with connection_usage.yaml
    dbname:
      type: string
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"
      default: "postgres"
    threshold_seconds:
      type: number
      minimum: 1
      default: 60           # 一条 active 查询运行超过此秒数即计入 long query
    warn_count:
      type: integer
      minimum: 1
      default: 1            # 存在 >=1 条长查询即 warning(默认阈值,semantic-abnormal 须在此值下触发)
  additionalProperties: false

collect:
  # secret HOSTLENS_POSTGRES_PASSWORD remapped to client-native PGPASSWORD (never -W/inline).
  # PGCONNECT_TIMEOUT=5 < timeout_seconds 15 → unreachable backend fails fast.
  # pid <> pg_backend_pid() excludes this inspector's own backend (else a healthy
  # instance self-counts). now()-query_start collapsed to a frozen scalar at sample
  # time (D-1): ReplayTarget returns the recorded stdout verbatim, no replay-time clock.
  # fail-loud: non-numeric or psql error → exit 1 + empty stdout → status=exception.
  command: |
    set -e
    out=$(PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}" PGCONNECT_TIMEOUT=5 \
      psql -tA -F'|' -h {{ host | sh }} -p {{ port }} -U {{ user | sh }} -d {{ dbname | sh }} \
      -v ON_ERROR_STOP=1 \
      -c "SELECT count(*),
                 COALESCE(max(extract(epoch FROM now()-query_start)),0)::int
          FROM pg_stat_activity
          WHERE state='active'
            AND pid <> pg_backend_pid()
            AND now()-query_start >= interval '{{ threshold_seconds }} seconds'") \
      || { echo 'psql long_queries failed' >&2; exit 1; }
    cnt=$(printf '%s' "$out" | cut -d'|' -f1)
    dur=$(printf '%s' "$out" | cut -d'|' -f2)
    case "$cnt" in ''|*[!0-9]*) echo 'non-numeric count' >&2; exit 1;; esac
    case "$dur" in ''|*[!0-9]*) echo 'non-numeric duration' >&2; exit 1;; esac
    printf '{"long_query_count":%s,"max_duration_seconds":%s}' "$cnt" "$dur"
  timeout_seconds: 15

parse:
  format: json

output_schema:
  type: object
  properties:
    long_query_count:     { type: integer }
    max_duration_seconds: { type: integer }
  required: [long_query_count, max_duration_seconds]
  additionalProperties: false

findings:
  - when: "long_query_count >= warn_count"
    severity: warning
    message: "存在 {long_query_count} 条运行超过 {threshold_seconds}s 的活动查询, 最长 {max_duration_seconds}s"
```

> 注:`message` 用单大括号 `{long_query_count}`(`str.format` 渲染);用 `{{ }}` 会被当转义字面量、不插值。`postgres.long_queries` 不依赖时间窗口(按查询**已运行时长**阈值判,非 `[now-N, now]` 窗口),故**不**用 `collect.sampling_window`——窗口机制的裁定见 mysql.slow_queries(D-2)与 nginx.error_rate(D-4)。
