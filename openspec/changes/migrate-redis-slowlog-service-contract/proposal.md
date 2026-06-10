# 提案：迁移 redis.slowlog 至 service-inspector-contract 全合规（消解 seed 漂移）

## 为什么

`redis.slowlog` 是 `service-inspector-contract` spike 之前就存在的 **pre-spike seed**，被该契约显式祖父化。祖父条款登记了它**三处漂移**：

1. **secret 命名非 `HOSTLENS_`**：声明 `secrets: [REDIS_PASSWORD]`。`ssh-execution-target` 契约规定 SSH secret 投递走远端 `AcceptEnv HOSTLENS_*` —— 在按推荐配置 `AcceptEnv HOSTLENS_*` 的远端 sshd 上 `REDIS_PASSWORD` env **被丢弃** → 有密码的 Redis 在 SSH 上巡检不了。
2. **argv 明文密码**：collector 用 `redis-cli -a "$REDIS_PASSWORD"` —— 密码进 argv，`ps` 可见。
3. **缺 default-阈值 semantic-abnormal fixture 轨**：仅单轨 finding-trigger，缺契约要求的双轨。

> **本次 review 另查实一个既存契约违规**（祖父化掩盖了它）：`redis.slowlog` 的 redis-cli 调用**没有 `-t` 连接超时 flag**（同域合规模板 `memory_usage` 有 `-t 5`），违反 service-inspector-contract「客户端连接超时小于采集超时」MUST。**全合规迁移必须一并修复它**（collector 行为变更）。

同域 3 个 wave-2 inspector（`redis.{memory_usage,persistence,replication_lag}`）是合规模板。本提案把 `redis.slowlog` 迁移到**全合规**，消解上述漂移 + 补 `-t 5`，使其从祖父条款移除、**同时**加入 `_ALL_SERVICE_MANIFESTS`（11→12）与 `_SECRET_SERVICE_MANIFESTS`（6→7），真正受契约全部 `_ALL_ITEMS` 门管辖。

> **范围提示（实施前必读）**：本提案非「改个 env 名」的小迁移。爆炸半径含 1 处 collector 行为变更（`-t 5`）+ **9 处 crosscheck 冻结结构** + 5 个 fixture 重录/新增 + 多个新测试。详见「对外契约影响」与 tasks。

## 变更内容

### manifest（`src/hostlens/inspectors/builtin/redis/slowlog.yaml`）

- **BREAKING — secret 重命名**：`secrets: [REDIS_PASSWORD]` → `[HOSTLENS_REDIS_PASSWORD]`。用户须把 `REDIS_PASSWORD` env 改设 `HOSTLENS_REDIS_PASSWORD`（与同域 redis inspector 统一）。
- **argv → env remap**：4 处 `redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ...` → `REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD" redis-cli --no-auth-warning ...`，凭据不进 argv；有/无鉴权分流判据改 `[ -n "${HOSTLENS_REDIS_PASSWORD:-}" ]`。
- **collector 行为变更 — 加 `-t 5` 连接超时**：4 处 redis-cli 调用加 `-t 5`（与 `memory_usage` 模板一致，满足「连接超时 5 < 采集超时 15」MUST）。**这是采集行为变更**（此前无连接超时 → 网络挂起会拖到 collect timeout），全合规必须。
- 注释更新为 HOSTLENS_ 契约措辞。**不动** metrics-only 设计（仍不返回 SLOWLOG GET 命令文本）、output_schema、findings 语义、阈值参数、targets。

### crosscheck 冻结结构（`tests/inspectors/test_service_contract_crosscheck.py`，共 9 处）

slowlog 须**同时**进 `_ALL` 与 `_SECRET`（`_SECRET` 的值字面引用 `_ALL[...]`，不入 `_ALL` 会 KeyError；且「受契约管辖=满足全部 MUST」要求受全 `_ALL_ITEMS` 参数化门覆盖）：

1. `_ALL_SERVICE_MANIFESTS` 加 `redis.slowlog`（**11→12**）
2. `test_all_service_manifests_count_frozen` 断言 `== 11` → `== 12`
3. `_SECRET_SERVICE_MANIFESTS` 加 `redis.slowlog`（**6→7**）
4. `test_secret_service_manifests_count_frozen` 断言 `== 6` → `== 7`
5. `_SECRET_CLIENT_RULES` 加 `"redis.slowlog": {"native_env": "REDISCLI_AUTH", "forbidden_flags": ("-a ",)}`
6. `_CLIENT_TIMEOUT_TOKEN` 加 `"redis.slowlog": ("-t 5", 5)`（驱动 timeout 门，对应 collector 的 `-t 5`）
7. `_INJECTABLE_PARAMS` 加 `("redis.slowlog", ..., "host", "redis.internal")`（host pattern 注入正控）
8. `_PROBE_TEST_SOURCES` 加 `"redis.slowlog": .../test_redis_slowlog.py`（failure-class meta-guard 遍历）；`_RECORDED_SECRET_VALUES` **复用** `"p w*d"`（与 persistence 同值，清单零新增，避免反 vacuous 风险）；`_ALL_FIXTURES` 加 `+ sorted((_FIXTURES / "redis").glob("slowlog_*.json"))`（leak 扫描覆盖 slowlog fixture，否则 special-char-pw 脱敏门 vacuous）
9. **`_ok_stdout` 加 slowlog 键** `"redis.slowlog": '{"count":1,"max_micros":1}'` —— `_ok_stdout(probe)` 是函数内 `return {...}[probe]` **无 default** 字面 dict，注入正控 `test_benign_value_rides_sh_filter` 经 `_INJECTABLE_PARAMS` 调它，slowlog 进 `_INJECTABLE_PARAMS` 后缺键 → KeyError（全量 pytest **error**）。这是小写 helper-local dict、`grep '^_[A-Z]'` 抓不到（review 连漏两轮才查实，见 memory `project_service_inspector_crosscheck_frozen_structures`）

### 测试（`tests/inspectors/test_redis_slowlog.py`）

- autouse fixture `setenv("REDIS_PASSWORD","")` → `"HOSTLENS_REDIS_PASSWORD"`。
- **新增 `requires_unmet` 测试**（`_PROBE_TEST_SOURCES` 的 failure-class meta-guard 要求每 probe 的 test 含 `status == "requires_unmet"` 断言；slowlog test 现缺）：缺 redis-cli 二进制 → requires_unmet。
- **新增 semantic-abnormal 测试**：对真异常态录制，**默认阈值下触发 max_micros rule**（`max_micros >= slow_micros=100000`）—— 因默认 `warn_count=1` 极低使「非空即触发 count rule」近恒真，须用 `max_micros` rule 作判别轨（DEBUG SLEEP ≥100ms 造慢查询），与健康态 nonempty（仅 count rule、max_micros 小）机械区分；snapshot 断言默认阈值 severity+message。
- **新增 special-char-pw 测试**：特殊字符密码（复用 `"p w*d"`）经 REDISCLI_AUTH 不破命令串/不 word-split（对 metrics-only inspector，此轨验的是**命令安全**而非脱敏 —— slowlog 只输出整数，密码本不回显 stdout）。
- **新增 BREAKING 回归测试**：只设旧 `REDIS_PASSWORD`、不设 `HOSTLENS_REDIS_PASSWORD` → 断言 `requires_unmet`（锁住「旧 env 名静默失效但诚实 skip」，防伪验收）。
- **更新既有 snapshot**：重录后 `slowlog_nonempty` 的 count/max_micros 实际值会变（命令串变 + 录制环境差异），`test_nonempty_slowlog_derives_findings` 的 output 断言改为重录后实际值（不预设仍是 8）。

### recorder（`tests/inspectors/_record_redis_slowlog.py`）

- env `REDIS_PASSWORD` → `HOSTLENS_REDIS_PASSWORD`。
- 新增 semantic-abnormal 场景：`CONFIG SET slowlog-log-slower-than 0` + `DEBUG SLEEP 0.15`（造 ≥100ms 慢查询使 `max_micros ≥ 100000`，触发 max_micros rule）。
- 新增 special-char-pw 场景（密码 `"p w*d"`，recorder 脱敏路径）。

### fixture（重录 3 + 新增 2）

经 recorder **重录**（禁手编 —— 命令串变了 ReplayTarget 逐字节匹配）：`slowlog_empty` / `slowlog_nonempty`（finding-trigger）/ `slowlog_conn_refused`；**新增** `slowlog_semantic_abnormal` / `slowlog_special_char_pw`。

### spec（`service-inspector-contract` MODIFY 3 需求）

「本契约管辖范围与既有 seed 祖父化」+「secret 必须经 env 注入」+「跨 local 与 SSH target 无分叉」三需求里 slowlog 的「漂移/祖父化」点名（共 4 处文字）收窄为仅 `postgres.bloat_tables`；祖父条款新增场景「redis.slowlog 迁移后受契约管辖」。

## 非目标（Non-Goals）

- **不迁移 `postgres.bloat_tables`**：另一 client（psql / `PGPASSWORD`）、另一域，独立 sibling follow-up（拆分 PR）。本提案后祖父条款仍留它一个。
- **不改 `redis.slowlog` 的 metrics-only 设计**：仍只报 count / max_micros，不引入 SLOWLOG GET 命令文本（`hook.py` 触发点，不在本提案）。
- **不改 output_schema / findings 语义 / 阈值参数 / targets**：targets 仍 `[local, ssh, docker, k8s]`，**cohort guard 计数（INCLUDE 28 / EXCLUDE 42 / 总 70）不变**（slowlog 本在 INCLUDE，迁移不动 targets）。
- ~~不改 collector 采集结构~~ —— **此项作废**：全合规**必须**加 `-t 5` 连接超时（既存契约违规），这是采集行为变更，已纳入范围。仅「不改 metrics-only 输出形态 / 不改 SLOWLOG LEN+EVAL 的两调用结构 / 不改 EVAL Lua」仍成立。
- **不引入新 secret 机制**（凭据文件等）：仍走 `secrets` env 注入 + 原生 env remap。
- **不改 runner / schema / fixture-recorder 框架**：纯 manifest + 测试 + recorder 数据迁移。

## 功能 (Capabilities)

### 新增功能

无。

### 修改功能

- `service-inspector-contract`: 「本契约管辖范围与既有 seed 祖父化」需求 —— `redis.slowlog` 由祖父化 legacy 转为已迁移合规、受契约管辖，从祖父化 seed 列表移除；祖父化 seed 仅剩 `postgres.bloat_tables`。

## 对外契约影响

| 契约面 | 影响 |
|---|---|
| **Inspector manifest secret 声明** | **BREAKING**：`REDIS_PASSWORD` → `HOSTLENS_REDIS_PASSWORD`（用户须改 env 名） |
| **collector 采集行为** | 加 `-t 5` 连接超时（此前无 → 网络挂起拖到 collect timeout；变更对正常路径透明，仅故障路径更快诚实失败） |
| `service-inspector-contract` spec | MODIFY 祖父条款（移除 redis.slowlog）；need 标题不变、无 RENAMED |
| 测试契约（crosscheck） | **9 处冻结结构**：`_ALL` 11→12、`_SECRET` 6→7、两个 count_frozen、`_SECRET_CLIENT_RULES`、`_CLIENT_TIMEOUT_TOKEN`、`_INJECTABLE_PARAMS`、`_PROBE_TEST_SOURCES`(+`_ALL_FIXTURES` glob+`_RECORDED_SECRET_VALUES`)、`_ok_stdout` |
| Inspector manifest schema | 无（secret pattern 接受两名，零字段变更） |
| Agent / MCP / CLI | 无 |

## 迁移后 collector 形态（核心 diff，SLOWLOG LEN 段）

```yaml
secrets: [HOSTLENS_REDIS_PASSWORD]   # was: [REDIS_PASSWORD]
collect:
  command: |
    # Secret as HOSTLENS_REDIS_PASSWORD (AcceptEnv HOSTLENS_* survives SSH);
    # remapped to redis-cli's REDISCLI_AUTH env channel — never argv, never {{ }}.
    # `-t 5` connect timeout (< collect timeout 15) per service contract.
    if [ -n "${HOSTLENS_REDIS_PASSWORD:-}" ]; then
      count=$(REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD" redis-cli --no-auth-warning -t 5 -h {{ host | sh }} -p {{ port }} --json SLOWLOG LEN) || { echo "redis SLOWLOG LEN failed" >&2; exit 1; }
    else
      count=$(redis-cli -t 5 -h {{ host | sh }} -p {{ port }} --json SLOWLOG LEN) || { echo "redis SLOWLOG LEN failed" >&2; exit 1; }
    fi
    # … EVAL max_micros 段同样 REDISCLI_AUTH remap + -t 5 …
```

## Failure Modes

| # | 场景 | 行为 |
|---|---|---|
| 1 | 用户仍设旧 `REDIS_PASSWORD`、未设 `HOSTLENS_REDIS_PASSWORD` | preflight 要求 `HOSTLENS_REDIS_PASSWORD` 存在 → 缺失 → `requires_unmet`（诚实 skip，**新增回归测试锁住**） |
| 2 | SSH 远端配 `AcceptEnv HOSTLENS_*` | 迁移后 secret 经 `HOSTLENS_REDIS_PASSWORD` 到达远端（迁移前 `REDIS_PASSWORD` 被丢） |
| 3 | 无密码 Redis | 显式导出 `HOSTLENS_REDIS_PASSWORD=` 空串 → 走无 REDISCLI_AUTH 分支 |
| 4 | redis 不可达 / NOAUTH | redis-cli 非零退出 / 非数字回显 → exit 1 + 空 stdout → `status=exception`（不变）；`-t 5` 使网络挂起 5s 内诚实失败而非拖到 15s |
| 5 | fixture 重录漏 / 命令串不一致 | ReplayTarget `misses != []` → 测试红（机械暴露） |

## Operational Limits

- 无新并发 / 内存面。`-t 5` 使单次连接最长 5s（此前无界至 collect timeout 15s），`timeout_seconds: 15` 不变。

## Security & Secrets

**核心收益**：

- **凭据不进 argv**：`REDISCLI_AUTH` env remap 替代 `-a "$pwd"`，`ps` 不可见密码。crosscheck `forbidden_flags: ("-a ",)` 机械断言命令串无 `-a `。
- **secret 跨 SSH 可达**：`HOSTLENS_` 前缀对齐 `AcceptEnv HOSTLENS_*`。
- **special-char-pw 轨对 slowlog 验的是命令安全（非脱敏）**：slowlog metrics-only 只输出整数 count/max_micros，密码**本不回显 stdout**，脱敏门对它 trivially 通过；special-char-pw（`"p w*d"`）真实价值是验证含空格/glob 的密码经 REDISCLI_AUTH env 不破命令串/不 word-split。fixture 注释与 PR 须澄清此点，避免误读为「脱敏证据」。`_ALL_FIXTURES` 加 slowlog glob 使 leak 扫描真覆盖（防 vacuous）。
- 无新密钥、不扩攻击面。

## Cost / Quota Impact

- 零 LLM token / 零 Anthropic API 影响（Inspector 不调 LLM）。

## Demo Path

```bash
pip install -e ".[dev]"
# 1. 离线（无 redis、无 API key）：重录 fixture 的 snapshot 回放 + crosscheck 全门
pytest tests/inspectors/test_redis_slowlog.py tests/inspectors/test_service_contract_crosscheck.py -q
#    含 semantic-abnormal（max_micros rule 默认阈值触发）+ requires_unmet + special-char-pw + crosscheck 12/7 全门
# 2. 真机重录（docker compose 真 redis）：
python -m tests.inspectors._record_redis_slowlog
#    compose up redis → CONFIG SET slowlog-log-slower-than 0 + DEBUG SLEEP 0.15 造真慢查询 → 录 5 fixture → compose down
#    断言重录 fixture 命令串含 REDISCLI_AUTH + -t 5、不含 -a "$；stdout 无明文密码；semantic_abnormal max_micros≥100000
```

## 影响

- **代码**：`builtin/redis/slowlog.yaml`（secret + collector + `-t 5`）；`test_redis_slowlog.py`（env + 4 个新测试 + count snapshot 更新）；`_record_redis_slowlog.py`（env + 2 场景）；`test_service_contract_crosscheck.py`（**9 处结构**）；`fixtures/redis/slowlog_*.json`（重录 3 + 新增 2）
- **spec**：MODIFY `service-inspector-contract`（3 需求，祖父条款移除 redis.slowlog）
- **文档**：CHANGELOG / docs 载明 **BREAKING** env 名变更
- **依赖**：零新依赖
