## 为什么

M6 wave-2 的「单实例只读」铺量批次(wave-2a)。基底 spike(`add-service-inspector-contract-spike`,已 apply + 归档)用 2 个真例(`redis.memory_usage` / `mysql.connection_usage`)逼出了 `service-inspector-contract`。本批次在该契约上**机械铺量**剩余的、与基底两探针**同质工程风险**的 service inspector:**单实例 + 即时只读快照 + 确定性一次性录制**——不依赖持续 workload、不依赖时间窗口累积、不依赖非确定性时序。

切片维度按 Codex 两轮对抗性讨论的收敛结论:**按「运行契约 + 采集风险」切,而非按服务域切**。判据收敛为——

> **确定性即时快照 → wave-2a;持续 workload / 时间窗口累积 / 非确定性时序 → wave-2b。**

关键澄清:「主动构造一个异常态实例并一次性录制」**不是** wave-2b 的判据——基底自己就这么做了(`mysql.connection_usage` 录 `access_denied` 时**故意设错密码**、录 `conn_refused` 时连不存在端口)。真正的分水岭是异常态是否需要**持续运行的 workload / 时间窗口**才能采到。据此 `nginx.health` / `nginx.config_test`(对静态坏配置/停服务一次性可录)属 wave-2a;`postgres.long_queries`(需查询持续运行于采样窗口)、`mysql.slow_queries`(累积慢日志)属 wave-2b。

## 变更内容

### 新增 6 个单实例只读 inspector(纯 YAML,无 hook.py)

| 域 | inspector(registry name) | 采集手法 | secret |
|---|---|---|---|
| redis | `redis.persistence` | `redis-cli INFO persistence`,collector 抽 `aof_enabled` / `rdb_changes_since_last_save` / `rdb_last_save_time` 聚合标量;finding **以 `aof_enabled==0` 为前提门** + `rdb_changes_since_last_save >= warn_changes`(防 AOF 开启假阳性,见 design D-8) | `HOSTLENS_REDIS_PASSWORD`→`REDISCLI_AUTH` |
| postgres | `postgres.connection_usage` | `psql` 即时查 `pg_stat_activity` 计数 + `max_connections`,SQL 内聚合;finding 参数 `warn/critical_used_pct` | `HOSTLENS_POSTGRES_PASSWORD`→`PGPASSWORD` |
| docker | `docker.images.disk_usage` | `docker system df --format '{% raw %}{{json .}}{% endraw %}'`(`{% raw %}` 避 Jinja 冲突,见 D-7)+ **`jq`** 选 Images 行,**直接解析 docker 自报 `Reclaimable` 的 `(NN%)`** 得 `reclaimable_pct`(规避字符串→字节转换);finding `reclaimable_pct >= warn_reclaimable_pct`(默认 80) | 无 |
| docker | `docker.networks` | `docker network ls/inspect` + `jq`,关联出**未被任何容器使用的非内置 user-defined network**:`dangling_networks`(total)+ `results`(top-N 截断,`max_results` 默认 50);finding `dangling_networks >= warn_count`(默认 1);禁纯计数/禁内置网络(见 design 冻结语义)。docker 调用均 `timeout <N>` 包裹 | 无 |
| nginx | `nginx.health` | **`curl -fsS` stub_status**(`systemctl` 仅生产备选、非录制路径,见 D-5;`host`/`stub_status_path` 经收紧 pattern + `| sh`),失败三态(up→ok / down→exception),**no-finding** | 无 |
| nginx | `nginx.config_test` | `nginx -t` 退出码 + stderr 摘要;**finding-route**(坏配置=成功采集出异常→finding,非 exception),静态坏配置一次性可录(见 design D-5) | 无 |

> `nginx.health` 是 **no-finding** inspector(`findings: []`):服务在→`ok`、不可达→`exception`(诚实,绝不伪造健康)。按 `service-inspector-contract` 双轨 fixture 机械门「仅 `findings` 非空者要求 semantic-abnormal」,它**不触发**该机械门,但仍须 up/down 两份 ReplayTarget snapshot 证明 up→`ok` / down→`exception`。采集走 `curl` stub_status 而非 `systemctl`——录制 lane 是无 systemd 的 compose 容器,`systemctl` 会恒非零退出致 up/down 无法区分(详见 design D-5)。
>
> `nginx.config_test` 反之是 **findings 非空** inspector(坏配置→finding):故**受**双轨机械门约束,semantic-abnormal fixture = 一份真实静态无效配置(确定性一次性可录,默认严重度下产出预期 finding)。

### 新增 capability:`service-inspector-suite`(套件覆盖契约 + 质量门)

类比 wave-1 的 `os-shell-inspector-suite`,为整个 wave-2 service inspector 套件立**稳定公共质量门 + 追加式冻结 cohort** 结构(详见 design):

- **稳定公共质量门**(对所有 wave-2 service inspector 普适):守 `service-inspector-contract` + `inspector-authoring-contract`、双轨 fixture(no-finding 者豁免 semantic-abnormal)、干净注册 + 勾矩阵、零新 infra。
- **追加式冻结 cohort**:每个 wave(2a/2b/2c)在套件里拥有**独立、归档时冻结**的覆盖需求;后续 wave 用 `ADDED` 追加自己的覆盖需求,**永不 `MODIFY`** 已归档 wave 的清单——规避对已归档 spec 的回溯修改。本变更立套件 + 冻结 wave-2a cohort。

## 功能 (Capabilities)

### 新增功能
- `service-inspector-suite`:wave-2 service inspector 套件的覆盖契约与质量门(本变更立套件结构 + 冻结 wave-2a「单实例即时只读」cohort 覆盖)。

### 修改功能
- 无。`service-inspector-contract` 与 `inspector-authoring-contract` 仅被**引用**,不修改。针对 `inspector-authoring-contract` 第 22 行「顶层结果键取自 `results/items/records`」与聚合型裸标量键的字面张力:suite spec R4 中**「裸聚合键允许」逐字沿用** `os-shell-inspector-suite` 已归档先例;而**列表/聚合的分类判据本套件做了收紧**——os-shell 用 `for_each` 判,本套件改用更精确的「output_schema 是否含 array 字段」判(因 `docker.networks` 输出 array 但 finding 标量无 for_each、会被 for_each 判据误判),此为对先例的**修正非重述**(详见 design D-2)。两者都**不**触发对 authoring-contract 的 MODIFY:经核验 loader 无 top-key 机器门、temp-archive dry-run exit 0,**archive 不会失败**。**不**单起 MODIFY change(避免改已归档 spec + RENAMED 工作流的范围蔓延);收紧 authoring-contract:22 字面措辞登记为独立 follow-up(见 design 风险节),非本变更阻塞项。

## 影响

- 新增 `builtin/{redis,postgres,docker,nginx}/*.yaml`(新建 `builtin/nginx/` 目录)+ 对应 ReplayTarget fixture / snapshot 测试 / 录制入口。
- 复用基底的 docker-compose 录制 lane 模式(单实例 redis / postgres / nginx + docker daemon)。
- **对外契约影响:零**。沿用基底钉死的 manifest schema 字段集、capability enum(`{shell, file_read, ssh, systemd, docker_cli}`)、parse format(`raw/table/json/kv`);**Agent 可见工具数组不因本套件增减**(本变更不注册任何新 `ToolSpec`——现有 agent-surfaced 工具集含 `run_inspector` / `list_inspectors` / `list_targets` / `correlate_findings` / `request_more_inspection`,本变更一个都不动);无新 Python 依赖。新增 capability spec(`service-inspector-suite`)是文档契约,不改运行时 schema。
- **Agent 行为影响:零**。本批次只增 Inspector(SOT),不改 Agent loop / prompt / tool schema,故无 prompt caching 或 token 影响。

## 完整 YAML manifest 示例(`postgres.connection_usage`)

与已交付 `mysql.connection_usage` 同质(单实例连接率即时快照),证明 wave-2a 机械铺量无需新 infra:

```yaml
name: postgres.connection_usage
version: 1.0.0
description: >-
  PostgreSQL 连接使用率巡检(单实例)。需 psql client;secret 声明
  HOSTLENS_POSTGRES_PASSWORD(HOSTLENS_ 前缀对齐 ssh-execution-target 契约),
  collector 内 remap 到 client 原生 PGPASSWORD(从不内联)。只报聚合标量
  (已用连接数 / max_connections / 派生率)。服务不可达/认证失败 → status=exception;
  缺 psql → requires_unmet。SSH 上需远端 sshd 配 AcceptEnv HOSTLENS_*。
tags: [postgres, service, connections]
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
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"
    dbname:
      type: string
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"
      default: "postgres"
    warn_used_pct:     { type: number, default: 80.0 }
    critical_used_pct: { type: number, default: 95.0 }
  additionalProperties: false

collect:
  # secret HOSTLENS_POSTGRES_PASSWORD remapped to client-native PGPASSWORD on the
  # psql invocation (never -W/inline). PGCONNECT_TIMEOUT=5 < timeout_seconds 15 so
  # an unreachable backend fails fast (orthogonal transport-layer status=timeout
  # only past 15s). -tA = tuples-only, unaligned; one round-trip returns
  # "used|max" via the | field sep, awk'd on a SEPARATE line (command-sub, not a
  # pipe — piping psql|awk would inspect awk's exit and mask a psql failure). The
  # value is the GLOBAL backend count (pg_stat_activity), not a per-db slice.
  # fail-loud: non-numeric or psql error → exit 1 + empty stdout → status=exception
  # (a down backend never fabricates used_pct=0).
  command: |
    raw=$(PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}" PGCONNECT_TIMEOUT=5 \
      psql -tA -F'|' -h {{ host | sh }} -p {{ port }} -U {{ user | sh }} -d {{ dbname | sh }} \
      -c "SELECT (SELECT count(*) FROM pg_stat_activity), current_setting('max_connections')::int") \
      || { echo "psql connection_usage failed" >&2; exit 1; }
    used=$(printf '%s' "$raw" | awk -F'|' 'NR==1{print $1}')
    maxc=$(printf '%s' "$raw" | awk -F'|' 'NR==1{print $2}')
    case "$used" in ''|*[!0-9]*) echo "used non-numeric: $used" >&2; exit 1;; esac
    case "$maxc" in ''|*[!0-9]*) echo "maxc non-numeric: $maxc" >&2; exit 1;; esac
    if [ "$maxc" -gt 0 ]; then
      pct=$(LC_ALL=C awk -v u="$used" -v m="$maxc" 'BEGIN{printf "%.2f", (u/m)*100}')
      printf '{"used_connections":%d,"max_connections":%d,"used_pct":%s}' "$used" "$maxc" "$pct"
    else
      printf '{"used_connections":%d,"max_connections":%d,"used_pct":null}' "$used" "$maxc"
    fi
  timeout_seconds: 15

parse:
  format: json

output_schema:
  type: object
  properties:
    used_connections: { type: integer }
    max_connections:  { type: integer }
    used_pct:         { type: [number, "null"] }
  required: [used_connections, max_connections, used_pct]
  additionalProperties: false

findings:
  - when: "used_pct != None and used_pct >= critical_used_pct"
    severity: critical
    message: "PostgreSQL connections at {used_pct}% ({used_connections}/{max_connections})"
  - when: "used_pct != None and used_pct >= warn_used_pct and used_pct < critical_used_pct"
    severity: warning
    message: "PostgreSQL connections at {used_pct}% ({used_connections}/{max_connections})"
```

## 非目标 (Non-Goals)

- ❌ 不含需**持续 workload / 时间窗口累积**才能录异常的 inspector(留 wave-2b:`postgres.long_queries`、`mysql.slow_queries`、`nginx.error_rate`、`nginx.upstream`、`mysql.deadlocks`)。
- ❌ 不含多实例 / replication inspector(留 replication spike + wave-2c)。
- ❌ 不新增 `docker.containers.unhealthy`——既有 `docker.containers.restart_loop` 已读取 `State.Health.Status` 并对 unhealthy 出 finding,新增即重复(经核验)。
- ❌ 不重复基底已交付的 `redis.memory_usage` / `mysql.connection_usage`。
- ❌ 不引入新 parse format / 新 secret 机制 / DockerTarget / K8s / JVM·Go。
- ❌ **不**单起 change 去 MODIFY 已归档的 `inspector-authoring-contract`(裸聚合键张力用套件 spec 就地澄清,沿用 os-shell 先例)。
- ❌ 不改 Agent loop / prompt / tool schema(本批次只增 Inspector SOT)。

## Demo Path

```
hostlens inspectors list --tag postgres   # 看 postgres.connection_usage 注册
hostlens inspectors list --tag nginx      # 看 nginx.health / nginx.config_test 注册
hostlens inspectors show nginx.config_test
pytest -k "persistence or postgres_connection or images_disk or docker_networks or nginx_health or nginx_config" -q
```
