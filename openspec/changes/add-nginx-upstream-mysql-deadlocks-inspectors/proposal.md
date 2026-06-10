## 为什么

M6 内置 Inspector 库已达退出门槛(每域 ≥3、总数 70、各有 snapshot + replay fixture),但 wave-2b 在归档时显式推后了两个累积/时间窗口单元格:**nginx.upstream**(upstream 故障)与 **mysql.deadlocks**(InnoDB 死锁)。这两格是 Web/DB 域真实运维高频故障——upstream 全挂(`no live upstreams`)直接 502、InnoDB 死锁回滚事务丢写——却是当前覆盖矩阵仅剩的可选缺口。补齐它们把 M6 从「达退出门槛」推到「Web/DB 域全满」,且不引入任何新基础设施(纯追加 service inspector,复用既有 `service-inspector-contract` + `os-shell` 同族 collector 形态)。

## 变更内容

- 新增 **nginx.upstream** inspector(YAML manifest + snapshot 测试 + ReplayTarget fixture):单遍 `LC_ALL=C` awk 扫静态路径 `/var/log/nginx/error.log`,按窗口(整文件 / 日志轮转即窗口边界,沿用 nginx.error_rate 的 D-4 设计)统计 upstream 故障事件并在 collector 内坍缩成标量计数。
- 新增 **mysql.deadlocks** inspector(YAML manifest + snapshot 测试 + ReplayTarget fixture):`SHOW ENGINE INNODB STATUS` 取「LATEST DETECTED DEADLOCK」段,collector 内 parse 死锁时间戳并在采样时刻算成 `deadlock_age_seconds` 标量,与 `lookback_seconds` 窗口比较。secret 走 `HOSTLENS_MYSQL_PWD`(remap 到 `MYSQL_PWD`,从不内联 argv),与 mysql.slow_queries 完全对齐。
- 两个 inspector 都进容器安全 cohort 的 **INCLUDE**(读 service 本地日志 / 服务端,非 host 全局源),`targets: [local, ssh, docker, k8s]`,cohort INCLUDE 计数 28 → 30。
- 同步更新 `tests/inspectors/test_service_contract_crosscheck.py` 硬编码结构与容器 cohort guard 测试。

## 功能 (Capabilities)

### 新增功能
<!-- 无新 capability;两个 inspector 追加进既有 service-inspector-suite -->

### 修改功能
- `service-inspector-suite`: 追加(ADDED)一个 sibling 覆盖需求,覆盖本批两个 wave-2b 尾批 inspector(nginx.upstream / mysql.deadlocks)。**引用**套件已冻结的公共质量门(守 `service-inspector-contract` / 守作者契约且输出键区分 / 附 ReplayTarget fixture 与可证检出 snapshot / 禁引入新基础设施),**不**重述细则;**不** MODIFY 已归档的 wave-2a/wave-2b 旧覆盖需求与其冻结清单。

## 影响

**对外契约影响**:
- **Inspector schema**:新增两个 manifest,复用既有 `InspectorManifest` schema,无 schema 字段变更。
- **Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令**:**零变更**。两个 inspector 经既有 registry 自动加载,无需改 Agent / MCP / CLI 代码。
- **容器 cohort 不变量**:INCLUDE 28 → 30;docker⇔k8s 奇偶不变量与内容式 meta-guard 须随之更新断言上界。

**受影响代码**:
- `src/hostlens/inspectors/builtin/nginx/upstream.yaml`(新增)
- `src/hostlens/inspectors/builtin/mysql/deadlocks.yaml`(新增)
- `tests/inspectors/`:两个 inspector 各一份 snapshot 测试 + fixture;`test_service_contract_crosscheck.py` 计数/meta-guard 更新;容器 cohort guard 测试 INCLUDE 上界更新
- `examples/`:两份可 replay fixture
- `openspec/specs/service-inspector-suite/spec.md`(归档时合入 ADDED delta)

### nginx.upstream manifest 示例(完整)

```yaml
name: nginx.upstream
version: 1.0.0
# Service-inspector-suite wave-2b probe: upstream failure events from the current
# /var/log/nginx/error.log. Nginx OSS exposes NO per-upstream stats without
# nginx-plus/stub_status, so upstream health is inferred from error-log events
# (log rotation = window boundary, design D-4). Static path so requires_files
# preflight matches the path the collector reads.
description: >-
  Nginx upstream 故障巡检(单实例)。单遍 LC_ALL=C awk 扫静态路径
  /var/log/nginx/error.log,按整文件窗口(日志轮转即窗口边界)统计 upstream 故障事件
  (upstream timed out / no live upstreams / connect() failed ... upstream /
  upstream prematurely closed)。Nginx OSS 无 per-upstream 统计,故经 error.log 推断。
  缺 awk 或日志不可读 → requires_unmet;空日志 → ok 零对象。无连接 secret。
tags: [nginx, web, service, upstream]
targets: [local, ssh, docker, k8s]
requires_capabilities: [shell]
requires_binaries: [awk]
requires_files: [/var/log/nginx/error.log]
secrets: []
privilege: none

parameters:
  type: object
  properties:
    warn_count:
      type: integer
      minimum: 1
      default: 1
  additionalProperties: false

collect:
  # 单遍 awk 在 collector 内坍缩成标量(Authoring Contract rule 1):upstream_error_count
  # 与分类计数全在此算;Finding DSL 仅阈值比较 ready 标量。LC_ALL=C 防 locale 破 JSON。
  # END{} 对空输入吐合法零对象。
  command: |
    LC_ALL=C awk '
      /no live upstreams/                                    { noLive++ }
      /upstream timed out/                                   { timeout++ }
      /connect\(\) failed/                                   { connect++ }
      /upstream prematurely closed connection/               { premature++ }
      /upstream (timed out|prematurely|sent)|no live upstreams|while connecting to upstream/ { total++ }
      END {
        printf "{\"upstream_error_count\":%d,\"timed_out\":%d,\"no_live_upstreams\":%d,\"connect_failed\":%d,\"prematurely_closed\":%d}", \
          total+0, timeout+0, noLive+0, connect+0, premature+0
      }
    ' /var/log/nginx/error.log
  timeout_seconds: 15

parse:
  format: json

output_schema:
  type: object
  properties:
    upstream_error_count: { type: integer }
    timed_out:            { type: integer }
    no_live_upstreams:    { type: integer }
    connect_failed:       { type: integer }
    prematurely_closed:   { type: integer }
  required: [upstream_error_count, timed_out, no_live_upstreams, connect_failed, prematurely_closed]
  additionalProperties: false

findings:
  - when: "upstream_error_count >= warn_count"
    severity: warning
    message: "Nginx upstream 故障 {upstream_error_count} 次(timeout={timed_out} no-live={no_live_upstreams} connect={connect_failed})"
```

### mysql.deadlocks manifest 示例(完整)

```yaml
name: mysql.deadlocks
version: 1.0.0
# Service-inspector-suite wave-2b-tail probe: latest InnoDB deadlock from
# SHOW ENGINE INNODB STATUS. MySQL exposes NO clean cumulative deadlock counter
# by default, so this is a point-in-time "did a deadlock occur within lookback"
# check. The deadlock age is collapsed to a scalar AT SAMPLE TIME on the target
# (wave-2b determinism: ReplayTarget returns the frozen scalar, never re-aggregates
# by now()). secret HOSTLENS_MYSQL_PWD remapped to MYSQL_PWD, never inlined in argv.
description: >-
  MySQL InnoDB 死锁巡检(单实例,point-in-time 最近死锁)。SHOW ENGINE INNODB STATUS
  取「LATEST DETECTED DEADLOCK」段,collector 内 parse ISO 形时间戳并在采样时刻算
  deadlock_age_seconds 标量、冻结进输出(无死锁恒发哨兵 -1)。MySQL 默认无累积死锁
  计数器,故只判「最近 lookback 窗口内是否发生死锁」。secret 声明 HOSTLENS_MYSQL_PWD
  (remap 到 MYSQL_PWD,从不内联 argv)。服务不可达/认证失败/时间戳 parse 失败 →
  status=exception;无死锁 → ok。SSH 上需远端 sshd 配 AcceptEnv HOSTLENS_*。
tags: [mysql, service, deadlocks, innodb]
targets: [local, ssh, docker, k8s]
requires_capabilities: [shell]
requires_binaries: [mysql]
secrets: [HOSTLENS_MYSQL_PWD]
privilege: none

parameters:
  type: object
  required: [user]
  properties:
    host:
      type: string
      pattern: "^[a-zA-Z0-9._-]+$"
      default: "127.0.0.1"
    port:
      type: integer
      minimum: 1
      maximum: 65535
      default: 3306
    user:
      type: string
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"
    lookback_seconds:
      type: integer
      minimum: 1
      default: 3600
  additionalProperties: false

collect:
  # secret remapped to client-native MYSQL_PWD (never argv). --connect-timeout=5
  # < timeout 15 → unreachable fails fast. Parse the LATEST DETECTED DEADLOCK
  # section's ISO timestamp; compute age AT SAMPLE TIME, freeze. NOTE the section
  # layout: the "LATEST DETECTED DEADLOCK" marker is sandwiched between "------"
  # separator lines and the timestamp is the FIRST ISO-dated line AFTER the marker
  # (NOT the immediate next line — that is the closing separator). So flag on the
  # marker, then print the first line matching ^YYYY- (skips the separator). Both
  # keys ALWAYS emitted (no-deadlock → deadlock_age_seconds:-1 sentinel) so
  # output_schema.required never fails. fail-loud: mysql error / non-ISO / non-numeric → exit 1.
  command: |
    set -e
    export MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}"
    status=$(mysql --batch -N --connect-timeout=5 \
      -h {{ host | sh }} -P {{ port }} -u {{ user | sh }} \
      -e "SHOW ENGINE INNODB STATUS\G") \
      || { echo 'mysql innodb status failed' >&2; exit 1; }
    ts=$(printf '%s' "$status" | awk '/LATEST DETECTED DEADLOCK/{f=1; next} f && /^[0-9][0-9][0-9][0-9]-/{print $1" "$2; exit}')
    if [ -z "$ts" ]; then
      printf '{"deadlock_detected":false,"deadlock_age_seconds":-1}'
    else
      epoch=$(date -d "$ts" +%s) || { echo 'deadlock timestamp parse failed' >&2; exit 1; }
      now=$(date +%s)
      age=$((now - epoch))
      printf '{"deadlock_detected":true,"deadlock_age_seconds":%d}' "$age"
    fi
  timeout_seconds: 15

parse:
  format: json

output_schema:
  type: object
  properties:
    deadlock_detected:     { type: boolean }
    deadlock_age_seconds:  { type: integer }
  required: [deadlock_detected, deadlock_age_seconds]
  additionalProperties: false

findings:
  - when: "deadlock_detected == True and deadlock_age_seconds <= lookback_seconds"
    severity: warning
    message: "MySQL InnoDB 最近 {deadlock_age_seconds}s 前检测到死锁(在 {lookback_seconds}s 窗口内)"
```

## Non-Goals(非目标)

- **不做 nginx-plus API / upstream_conf 集成** —— OSS error.log 推断已覆盖目标人群;nginx-plus 是付费特性,留 follow-up。
- **不做 mysql 累积死锁计数(`Innodb_deadlocks`-style)** —— 需 Performance Schema / 第三方 patch 配置,默认 MySQL 拿不到干净累积计数;本期只做「最近窗口内是否发生死锁」point-in-time 判定,留 follow-up。
- **不做死锁双方 SQL / 事务全文解析** —— 只报「最近 lookback 窗口内检测到死锁」+ age,不还原死锁现场。
- **不碰其他 service inspector** —— 不 MODIFY 已归档 wave 的 manifest / spec / fixture。
- **不改 `service-inspector-contract` spec 本身** —— 只追加 inspector,复用既有契约。
- **不引入新依赖 / 新基础设施** —— awk 与 mysql client 在既有 requires 体系内。

## Failure Modes

| 场景 | 行为 | 降级 |
|---|---|---|
| `/var/log/nginx/error.log` 缺失/不可读 | `requires_files` 预检 fail | status=requires_unmet(非 exception),报告标注前提未满足 |
| nginx error_log 非默认路径(自定义 `error_log` 指令) | 扫到的是空/旧文件 | upstream_error_count=0 → ok;真机 Demo Path 文档提示路径覆盖为 follow-up(本期静态路径) |
| mysql 服务不可达 / 认证失败 | `--connect-timeout=5` 快速失败,collector exit 1 + 空 stdout | status=exception,fail-loud 不静默报健康 |
| InnoDB 从未发生死锁(STATUS 无 LATEST DETECTED DEADLOCK 段) | collector parse 命中空段,END 恒发哨兵 | deadlock_detected=false + deadlock_age_seconds=-1 → ok 零 finding(两键恒出,不省略键,避免 output_schema required 校验失败→exception) |
| SHOW ENGINE INNODB STATUS 时间戳 locale/格式异常 | parse 失败 | collector exit 1 + stderr → status=exception(不吐半成品标量) |
| 远程 mysql 与 collector 主机 TZ 不同(INNODB STATUS 时间戳是 server 本地时区,`date -d` 按 collector 主机时区解析) | age 偏移 offset(可能把近期死锁推出 lookback 或得负 age) | **文档化限制**:默认 `host=127.0.0.1`(collector 与 mysql 同机共享时钟)无偏移、为常见态;跨 TZ 远程检查留 follow-up(需 `@@global.time_zone` 归一)。manifest collector 注释已显式声明 |

## Operational Limits

- **并发预算**:两个 inspector 单命令单轮,各 `timeout_seconds: 15`(< 既有 service inspector 默认上界);无额外并发占用。
- **内存预算**:awk 单遍流式扫 error.log,O(1) 内存不缓冲全文件;mysql 单次 round-trip,STATUS 输出 KB 级,collector parse 后即丢。
- **超时**:nginx 15s(本地日志扫描);mysql 15s(含 `--connect-timeout=5` 快速失败)。

## Security & Secrets

- **nginx.upstream**:无 secret(只读本地日志),不扩大攻击面;读 service 本地日志非 host 全局 → 容器 cohort INCLUDE。
- **mysql.deadlocks**:secret 声明 `HOSTLENS_MYSQL_PWD`,collector 内 remap 到 client 原生 `MYSQL_PWD`(**从不内联进 argv**,防 `ps` 泄露),与 mysql.slow_queries 完全对齐;SSH 上需远端 sshd 配 `AcceptEnv HOSTLENS_*`。无新密钥类型,不扩大攻击面。
- **脱敏**:两个 inspector 输出均为计数/布尔/时长标量,无敏感字符串(不回吐日志原文 / SQL 文本)。

## Cost / Quota Impact

- **零 LLM token 影响**:Inspector 是纯采集,不调 LLM;Agent 侧 token 成本仅在选用该 inspector 时多一条 tool result(计数标量,~50 token 量级),无 prompt cache 影响(Inspector schema 列表已缓存,新增两条 manifest 进入既有缓存段)。
- **无新 API 调用 / 配额**:不触 Anthropic API 之外的付费服务。

## Demo Path(5 分钟本地 reproduce,优先 cassette/offline)

```bash
pip install -e ".[dev]"

# 0) collector-shell 可执行强锚(无需 nginx/mysql,直接跑 collector 的 awk/date 片段):
#    对真实形态 SHOW ENGINE INNODB STATUS\G 死锁段样本跑 deadlocks awk —— 确认跳过 ------ 分隔线取到 ISO 时间戳:
printf '%s\n' '------------------------' 'LATEST DETECTED DEADLOCK' '------------------------' '2024-01-02 03:04:05 0x7f' '*** (1) TRANSACTION:' \
  | awk '/LATEST DETECTED DEADLOCK/{f=1;next} f&&/^[0-9][0-9][0-9][0-9]-/{print $1" "$2;exit}'   # → 2024-01-02 03:04:05(非 ------)
#    age 算术(GNU date -d，目标 Linux；本机 macOS 用 gdate):date -d "2024-01-02 03:04:05" +%s 与 now 相减
#    nginx.upstream 合并正则 total 唯一行计数(排除无关行)+ 空日志 END{} 零对象，同法直跑 awk 验证

# 1) 离线回放(无需 nginx / mysql,确定性):
pytest tests/inspectors/test_nginx_upstream.py tests/inspectors/test_mysql_deadlocks.py -q
#    → snapshot 断言:semantic-abnormal fixture 在默认阈值下产出 warning + 预期 message

# 2) registry 干净注册:
hostlens doctor --json | jq '.inspectors'                 # loaded 计数含两个新 inspector, errors: []
hostlens inspectors show nginx.upstream                   # 看 manifest 与参数表
hostlens inspectors show mysql.deadlocks

# 3)(可选,需真机)真 nginx 上跑:
hostlens inspect local-host --inspector nginx.upstream     # 读 /var/log/nginx/error.log
```

真机 Demo 配方(可选,文档化在 examples/):用 `ab`/`wrk` 打一个把 upstream 指向死端口的 nginx 触发 `no live upstreams`;用两个并发事务交叉锁行触发 InnoDB 死锁后跑 `SHOW ENGINE INNODB STATUS` 验证 LATEST DETECTED DEADLOCK 段。
