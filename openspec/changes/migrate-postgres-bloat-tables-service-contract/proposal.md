## 为什么

`postgres.bloat_tables` 是 `service-inspector-contract` spike 之前就存在的 **pre-spike seed**，是该契约祖父化的**最后一个**在册 legacy。本提案把它迁移到**全合规**、受契约全部 MUST 管辖，使两个 pre-spike seed（`redis.slowlog` 已于独立 follow-up 迁移）**全部消解**、祖父条款**闭合**。

立项前对 `bloat_tables` 做了一次新审计，**实际 drift 与祖父条款登记的不一致**——既有**误标**也有**漏登**，本提案据实重列：

1. **secret 命名非 `HOSTLENS_`**（已登记）：声明 `secrets: [PGPASSWORD]`。`ssh-execution-target` 契约规定 SSH secret 投递走远端 `AcceptEnv HOSTLENS_*` —— 在按推荐配置 `AcceptEnv HOSTLENS_*` 的远端 sshd 上裸 `PGPASSWORD` env **被丢弃** → 有密码的 PostgreSQL 在 SSH 上巡检不了。同域 3 个合规 sibling（`postgres.{connection_usage,long_queries,replication_lag}`）已统一用 `HOSTLENS_POSTGRES_PASSWORD` + collector remap 到原生 `PGPASSWORD`。
2. **缺客户端连接超时**（**漏登** —— 祖父条款未列）：collector 无 `PGCONNECT_TIMEOUT`，`timeout_seconds: 30`。违反契约「客户端连接超时**必须**小于 `collect.timeout_seconds`」MUST（合规 sibling 均带 `PGCONNECT_TIMEOUT=5`）→ backend 不可达时会 hang 满整个 30s 而非快速 fail-loud。
3. **列表形态未截断**（**漏登** —— 祖父条款未列，是本次最大隐藏违规）：output 顶层 `results` 是 array（`ORDER BY n_dead_tup DESC`，**无 LIMIT、无 total 计数、无 `max_results` 参数**），整体回吐全部 user 表。违反契约「需返回列表时**必须**截断为 top-N（N 由 manifest 参数声明）并附 total 计数，**禁止**回吐高基数明细」MUST。迁入管辖后 `TestOutputShapeDiscipline` 的 list-shape 分支会要求 `max_results` 参数 + `[0:`/`LIMIT` 截断 + total 标量字段，缺一即红。
4. **双轨 fixture 不齐**（祖父条款**误标**为「缺 default-阈值 semantic-abnormal」）：审计实测 `bloated.json` 的 `orders` 行（`n_dead_tup=4000`、`dead_ratio=0.6667`）在 manifest **默认阈值**（`0.2`/`1000`）下即触发 finding，且录自 recorder 真起 postgres:16 + autovacuum-disabled `bloatdb` —— 它**已是** semantic-abnormal 轨、机械门 (a) **已满足**。真正**缺**的是契约第 (1) 条「健康态 + 降低阈值证 finding wiring」的 **finding-trigger 轨**。本提案据实补 finding-trigger 轨，使两轨齐全。

> **范围提示（实施前必读）**：本提案非「改个 env 名 + 补超时」的小迁移。与 `redis.slowlog`（metrics-only 扁平对象）不同，`bloat_tables` 是 **list-shaped** inspector，多一块 **collector + output_schema 重构**（截断 + total 计数）。爆炸半径含 2 处 collector 行为变更（`PGCONNECT_TIMEOUT=5` + top-N 截断）+ output_schema 加字段 + **多处 crosscheck 冻结结构** + 3 fixture 重录 + finding-trigger 轨补齐。详见「对外契约影响」与 design / tasks。

## 变更内容

### manifest（`src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml`）

- **BREAKING — secret 重命名**：`secrets: [PGPASSWORD]` → `[HOSTLENS_POSTGRES_PASSWORD]`。用户须把 `PGPASSWORD` env 改设 `HOSTLENS_POSTGRES_PASSWORD`（与同域 postgres inspector 统一）。
- **env remap**：collector `PGPASSWORD="$PGPASSWORD" psql ...` → `PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}" psql ...`（HOSTLENS_ secret remap 回 psql 原生 `PGPASSWORD` env，凭据仍走 env 通道、从不进 argv）。
- **collector 行为变更 — 加 `PGCONNECT_TIMEOUT=5`**：< `timeout_seconds` 30，满足连接超时 MUST（对齐 `connection_usage` 模板）。
- **collector + output_schema 重构 — 列表截断**：新增 `max_results` 整数参数（默认 20）；SQL 内 `ORDER BY n_dead_tup DESC LIMIT {{ max_results }}` 截断为 top-N（最严重 N 张表）；`json_build_object` 增 `total_tables` 标量字段（`(SELECT count(*) FROM pg_stat_user_tables)`，截断前总数）；output_schema 增 `total_tables: integer` + `max_results` 参数声明。
- **不动** findings 语义（仍 `for_each: results as t` 逐表阈值比较）、阈值参数（`dead_ratio_threshold`/`dead_tuple_threshold`）、`dbname` 注入安全三件套、targets（仍 `[local, ssh, docker, k8s]`）。

### crosscheck 冻结结构（`tests/inspectors/test_service_contract_crosscheck.py`）

bloat_tables 须**同时**进 `_ALL` 与 `_SECRET`，并适配 list-shape 分支（详尽清单见 design.md「crosscheck 冻结结构清单」）：

- `_ALL_SERVICE_MANIFESTS` 加（**12→13**）+ `test_all_service_manifests_count_frozen` 断言改 13
- `_SECRET_SERVICE_MANIFESTS` 加（**7→8**）+ `test_secret_service_manifests_count_frozen` 断言改 8
- `_SECRET_CLIENT_RULES` 加 `{"native_env": "PGPASSWORD", "forbidden_flags": ()}`
- `_CLIENT_TIMEOUT_TOKEN` 加 `("PGCONNECT_TIMEOUT=5", 5)`
- `_INJECTABLE_PARAMS` 加 `dbname` host-pattern 注入正控 + `_ok_stdout` 加 bloat_tables 键（无 default 字面 dict，缺键 → KeyError 全量 error）
- `_PROBE_TEST_SOURCES` 加（failure-class meta-guard）+ `_ALL_FIXTURES` 加 `postgres_bloat_tables/*.json` glob
- **list-shape 两测适配**：`test_only_docker_networks_is_list_shaped` 断言 `["docker.networks"]` → `["docker.networks", "postgres.bloat_tables"]`（list-shaped 不再唯一）+ 该测 docstring「other 11」→「other 12」；`TestOutputShapeDiscipline` 参数化分支现对 bloat_tables 校验 (a) key∈{results}、(b) max_results+LIMIT、(c) total_tables 标量

### 测试（`tests/inspectors/test_postgres_bloat_tables.py`）

- autouse `setenv("PGPASSWORD", ...)` → `"HOSTLENS_POSTGRES_PASSWORD"`；`test_fixtures_inject_password_via_env_not_plaintext` 断言 `$PGPASSWORD` → `$HOSTLENS_POSTGRES_PASSWORD`（命令串现为 `PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}"`）。
- 既有 snapshot 断言更新：重录后命令串含 `PGCONNECT_TIMEOUT=5` + `LIMIT`、output 增 `total_tables`（bloated/healthy/empty 三测的 output 断言加 `total_tables`）。
- **新增 finding-trigger 轨测试**：复用 `bloated.json` 的 `sessions` 行（`100` dead / `0.0256` ratio，默认阈值下**不**触发），以**降低的阈值参数**回放断言其触发 finding（证 finding wiring；阈值是 DSL 参数、不在录制命令串里，故无须新 finding fixture；降阈值后 `orders` 仍触发 → 用 **presence** 断言非 `count == 1`）。
- **新增 `requires_unmet` 测试**（`_PROBE_TEST_SOURCES` failure-class meta-guard 要求）：缺 `psql` 二进制 → requires_unmet。
- **新增 BREAKING 回归测试**：只设旧 `PGPASSWORD`、不设 `HOSTLENS_POSTGRES_PASSWORD` → 断言 `requires_unmet`（锁旧 env 名静默失效但诚实 skip）。
- **新增 exception 测试**（failure-class meta-guard 强制；`bloat ∉ _NO_EXCEPTION_SNAPSHOT`）：回放新增的 `conn_refused` error fixture → 断言 `status == "exception"`（与 postgres sibling 同模板；缺此则 bloat 进 `_PROBE_TEST_SOURCES` 后 crosscheck 红）。

### recorder + fixture（`_record_postgres_bloat.py` + 重录 3 + 新增 1）

- recorder env `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`（docstring 同步）；**加 1 个 error 场景**（连不可达端口 / 错密码）。
- 经 recorder **重录**（禁手编 —— 命令串变了 ReplayTarget 逐字节匹配）：`bloated.json` / `healthy.json` / `empty.json`（命令串现含 `PGCONNECT_TIMEOUT=5` + `LIMIT 20`、output 含 `total_tables`）。
- **新增 1 个 error fixture** `conn_refused.json`（exception 轨，连不可达端口、stderr 脱敏）—— failure-class meta-guard 要求 exception 断言（单个即足）。**finding-trigger 轨**仍零新 fixture（复用 bloated.json 降阈值）；**exception 轨**需此新 fixture，二者不矛盾。共 **4 fixture**。

### spec（`service-inspector-contract` + `inspector-plugin-system` MODIFY）

- `service-inspector-contract`：「本契约管辖范围与既有 seed 祖父化」需求里 bloat_tables 的「漂移/祖父化」点名收口为「**全部 pre-spike seed 已迁移、祖父条款闭合、无在册祖父化 inspector**」；新增场景「postgres.bloat_tables 迁移后受契约管辖」（对称 slowlog 那次，但本次是**闭合**而非收窄）。标题不变（无 RENAMED）。
- `inspector-plugin-system`：「CLI inspectors show 必须脱敏 secrets」需求拿 bloat + `PGPASSWORD` 当具例，迁移后改为 `HOSTLENS_POSTGRES_PASSWORD` + 修 inspector 名 typo（脱敏行为契约不变）。

## 功能 (Capabilities)

### 新增功能

无。

### 修改功能

- `service-inspector-contract`: 「本契约管辖范围与既有 seed 祖父化」需求 —— `postgres.bloat_tables` 由祖父化 legacy 转为已迁移合规、受契约全部 MUST 管辖，从祖父化 seed 列表移除；移除后祖父化 seed 列表**归零**、祖父条款闭合（无在册祖父化 inspector）。
- `inspector-plugin-system`: 「CLI `hostlens inspectors show <name>` 必须脱敏 secrets」需求 —— 该需求拿 `postgres.bloat_tables` + `PGPASSWORD` 当 secret-name 脱敏具例；迁移后真实 secret 为 `HOSTLENS_POSTGRES_PASSWORD`，示例 secret 名 / inspector 名 typo 同步更新（脱敏行为契约本身不变）。

## 影响

- **代码**：`builtin/postgres/bloat_tables.yaml`（secret + `PGCONNECT_TIMEOUT=5` + `max_results`/`LIMIT`/`total_tables` 重构）；`test_postgres_bloat_tables.py`（env + snapshot 加 total_tables + finding-trigger/requires_unmet/BREAKING/**exception** 四新测）；`_record_postgres_bloat.py`（env + 1 error 场景）；`test_service_contract_crosscheck.py`（多处冻结结构 + list-shape 两测适配）；`fixtures/postgres_bloat_tables/*.json`（**重录 3 + 新增 1 error fixture**）
- **spec**：MODIFY `service-inspector-contract`（祖父条款闭合 + 新增「迁移后受管辖」场景）+ MODIFY `inspector-plugin-system`（脱敏需求示例 secret 名同步）
- **文档**：逐文件点名（见 design D7）—— `docs/operations/inspectors.md`（worked example 改写）/ `targets.md`（祖父态）/ `inspector-authoring-contract.md`（活例 SQL 加截断）/ `ARCHITECTURE.md`（样例 secret 名）/ `test_doctor.py` 注释；CHANGELOG 载明 **BREAKING** env 名变更（`PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`）+ `PGCONNECT_TIMEOUT=5` + 列表截断（默认 top-20）
- **依赖**：零新依赖
- **对外契约**：output_schema 增 `total_tables`（additive，下游兼容）；`results` 由全量改 top-N（**行为变更** —— 极端 bloat 表多于 20 时仅报最严重 20 张 + total_tables 提示总数）
