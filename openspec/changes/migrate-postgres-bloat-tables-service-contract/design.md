## 上下文

`postgres.bloat_tables` 是 `service-inspector-contract` spike 前的 pre-spike seed，祖父化的**最后一个**在册 legacy。`redis.slowlog` 已迁移（archive `2026-06-10-migrate-redis-slowlog-service-contract`），本提案对称迁移 `bloat_tables` 并**闭合**祖父条款。

与 slowlog（metrics-only 扁平对象）的关键差异：`bloat_tables` 的 output 顶层 `results` 是 **array** → 落入 `TestOutputShapeDiscipline` 的 **list-shape 分支**，必须满足 top-N 截断 + total 计数 + `max_results` 参数，这是 slowlog 没有的一块 collector + output_schema 重构。立项审计另查实：祖父条款登记的 drift 既**误标**（双轨：实际有 semantic-abnormal、缺 finding-trigger）又**漏登**（连接超时 + 列表截断两处 MUST 违规均未列）。

合规模板：同域 `postgres.{connection_usage,long_queries,replication_lag}`（`HOSTLENS_POSTGRES_PASSWORD` + 原生 `PGPASSWORD` remap + `PGCONNECT_TIMEOUT=5`）。

## 目标 / 非目标

**目标：**

- `bloat_tables` 满足 `service-inspector-contract` **全部 MUST**，从祖父条款移除，祖父化 seed 列表归零、条款闭合。
- secret `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD` + collector remap 回原生 `PGPASSWORD`（凭据仍走 env、不进 argv）。
- 补 `PGCONNECT_TIMEOUT=5`（< `timeout_seconds` 30）。
- 列表形态合规：`max_results` 参数（默认 20）+ SQL `LIMIT` 截断 top-N + `total_tables` total 计数标量。
- 双轨齐全：保留 `bloated.json`（semantic-abnormal，默认阈值触发）+ 补 finding-trigger 轨（降阈值触发）。
- crosscheck 全部冻结结构同步 + list-shape 两测适配。

**非目标：**

- **不改 findings 语义**：仍 `for_each: results as t` 逐表 `dead_ratio`/`n_dead_tup` 阈值比较；不改阈值参数默认值。
- **不引入新 secret 机制**（凭据文件等）：仍 env 注入 + 原生 env remap。
- **不改 `dbname` 注入安全三件套**（pattern + `| sh`）、**不改 targets**（仍 `[local, ssh, docker, k8s]`）。
- **不改 runner / loader / schema / recorder 框架**：纯 manifest + 测试 + recorder 数据迁移。
- **不动 docker.networks**（既有唯一 list-shaped inspector），仅让 list-shape 两测容纳第二个 list-shaped。

## 决策

### D1 — secret 名 `HOSTLENS_POSTGRES_PASSWORD`，remap 回原生 `PGPASSWORD`

对齐同域 sibling（非 `HOSTLENS_PGPASSWORD`）。collector：

```yaml
secrets: [HOSTLENS_POSTGRES_PASSWORD]   # was: [PGPASSWORD]
# PGPASSWORD="$PGPASSWORD" psql ...   →
PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}" PGCONNECT_TIMEOUT=5 psql -tAqX --dbname={{ dbname | sh }} -c "..."
```

`${HOSTLENS_POSTGRES_PASSWORD:-}` 空串安全（无密码实例显式导出空串通过 preflight）。psql 本无 argv 密码 flag（`-W` 是交互提示、`-p` 是 PORT），故 `_SECRET_CLIENT_RULES` 的 `forbidden_flags: ()` —— argv 面**本就干净**，迁移不引入 argv 明文。

### D2 — 列表截断重构（本提案最大工作量，slowlog 无此项）

SQL 改造（在既有 subquery 上加 `LIMIT` + 外层加 `total_tables`）：

```sql
SELECT json_build_object(
  'total_tables', (SELECT count(*) FROM pg_stat_user_tables),
  'results', coalesce(json_agg(t), '[]'::json)
) FROM (
  SELECT schemaname AS schema, relname AS table, n_live_tup, n_dead_tup,
         round(n_dead_tup::numeric / nullif(n_live_tup + n_dead_tup, 0), 4) AS dead_ratio
  FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT {{ max_results }}
) t
```

- `max_results` 整数参数（默认 **20**），渲染进 `LIMIT {{ max_results }}`。整数参数无需 `| sh`（loader 校验 type=integer，非 shell-evaluable），与既有阈值参数同口径。
- `total_tables` = 截断前 user 表总数（`count(*) FROM pg_stat_user_tables`），满足 list-shape (c)「total 计数标量」+ 给操作者「top-20 of M」语境。
- 排序 `ORDER BY n_dead_tup DESC` 保证 top-N 是**最严重的 N 张**——极端 bloat 表多于 20 时漏的是较轻的，符合「报最严重」诊断意图。
- output_schema 增 `total_tables: { type: integer }` 进 `required`；`max_results` 进 parameters。

> **取舍**：`total_tables` 取「全部 user 表数」而非「超阈值表数」，因阈值比较在 Finding DSL（非 SQL），SQL 无从知晓 DSL 阈值。全部表数是 SQL 可纯聚合的、稳定的 total，且与现有「返回含健康表的全集」行为一致（healthy.json 的 accounts n_dead_tup=0 仍在 results 内）。

### D3 — finding-trigger 轨：复用 bloated.json 降阈值回放，**不新增 finding fixture 文件**

契约双轨：(1) finding-trigger（健康/低值 + **降低阈值** → 触发，证 wiring）；(2) semantic-abnormal（真实异常 + **默认阈值** → 触发）。

- 轨 (2) 已由 `bloated.json` 满足：`orders` 行默认阈值（0.2/1000）触发，录自真起 postgres:16 + autovacuum-disabled `bloatdb`（recorder + 测试 docstring 证 provenance）。
- 轨 (1) 补法：`bloated.json` 的 `sessions` 行（`n_dead_tup=100`、`dead_ratio=0.0256`）默认阈值下**不**触发；新增测试以**降低的阈值参数**（如 `dead_ratio_threshold=0.01, dead_tuple_threshold=10`）回放同一 fixture，断言 `sessions` 触发 finding。**阈值是 Finding DSL 参数、不在录制命令串里**，故 ReplayTarget 仍逐字节匹配同一 `bloated.json`、无须新 finding fixture 文件。**注意**：降阈值后 `orders`（默认即触发）仍触发，故该测 findings ≥ 2 —— 断言用 **presence**（`any "sessions" in f.message`）、**禁** `len(findings) == 1`。

> 这是与 slowlog 的方法差异：slowlog 当年新增了 `slowlog_semantic_abnormal` fixture（因它真缺 semantic-abnormal 轨）；bloat 真缺的是 finding-trigger 轨，而该轨可由「已有 fixture + 降阈值」覆盖、零新 finding fixture。

### D3.1 — exception 轨：**必须新增 1 个 error fixture**（Round-2 查实的 blocker）

`_PROBE_TEST_SOURCES` 的 failure-class meta-guard（`test_each_failure_class_asserted_in_probe_suite`，crosscheck L944-954）对每个 probe 的 test 强制**三类断言齐全**：`status == "requires_unmet"` + `status == "ok"` + （除 `_NO_EXCEPTION_SNAPSHOT={"nginx.error_rate"}` 外）`status == "exception"`。**bloat 不在豁免集** → bloat 进 `_PROBE_TEST_SOURCES`（D5 #9 / tasks 5.6）后**必须**在 `test_postgres_bloat_tables.py` 含 exception 断言，否则 crosscheck **红**。

当前 bloat test 只有 `ok`×3（无 requires_unmet、无 exception）。两个 postgres sibling（`connection_usage` / `long_queries`）均有 `access_denied.json` + `conn_refused.json` error fixture + `status == "exception"` 断言为模板。

- **必须新增 1 个 error fixture**（如 `conn_refused.json`：连不可达端口 → psql 非零退出 + 空 stdout → parse 异常 → `status=exception`）。bloat collector 无显式 `|| exit 1`，但 psql 连接失败本就非零退出 + 空 stdout，runner 仅解析 stdout → `status=exception`（与 slowlog/sibling 同机制）。
- recorder 须加 1 个 error 场景（连错端口 / 错密码）。
- 这**修正** D3「不新增 fixture」的过宽表述：**finding-trigger 轨**零新 fixture（复用 bloated.json 降阈值）成立，但 **exception 轨需新增 1 个 error fixture** —— 二者不矛盾。

### D4 — fixture 重录 + 新增范围

- **重录 3**（命令串变化 `HOSTLENS_POSTGRES_PASSWORD` + `PGCONNECT_TIMEOUT=5` + `LIMIT` + `total_tables` SQL → ReplayTarget 逐字节匹配）：`bloated.json` / `healthy.json` / `empty.json`（经 recorder 真起 postgres:16）。重录后三 fixture output 均含 `total_tables`；命令串含 `PGCONNECT_TIMEOUT=5` + `LIMIT 20`、不含明文密码。
- **新增 1**（D3.1 exception 轨）：`conn_refused.json`，录自连接失败态（连不可达端口）、stderr 脱敏。单个 exception fixture 即满足 meta-guard（sibling 有 conn_refused + access_denied 两个是富余、非要求）。
- 共 **4 个 fixture**（重录 3 + 新增 1）。**禁手编**。

### D5 — crosscheck 冻结结构清单（实现按此逐项对账，缺一即 KeyError 全量 error）

bloat_tables 须**同时**进 `_ALL` 与 `_SECRET`（`_SECRET` 值字面引用 `_ALL[...]`，不入 `_ALL` 会 KeyError）：

| # | 结构 | 改动 |
|---|---|---|
| 1 | `_ALL_SERVICE_MANIFESTS` | 加 `"postgres.bloat_tables": _builtin_root()/"postgres"/"bloat_tables.yaml"`（**12→13**） |
| 2 | `test_all_service_manifests_count_frozen` | `== 12` → `== 13` |
| 3 | `_SECRET_SERVICE_MANIFESTS` | 加 `"postgres.bloat_tables": _ALL_SERVICE_MANIFESTS["postgres.bloat_tables"]`（**7→8**） |
| 4 | `test_secret_service_manifests_count_frozen` | `== 7` → `== 8` |
| 5 | `_SECRET_CLIENT_RULES` | 加 `{"native_env": "PGPASSWORD", "forbidden_flags": ()}`（psql 无 argv 密码 flag，`-p` 是 PORT） |
| 6 | `_CLIENT_TIMEOUT_TOKEN` | 加 `("PGCONNECT_TIMEOUT=5", 5)` |
| 7 | `_INJECTABLE_PARAMS` | 加 `("postgres.bloat_tables", _ALL[...], "dbname", "appdb")`（dbname pattern 注入正控；正控值须满足 bloat **更严** pattern `^[A-Za-z_][A-Za-z0-9_]*$`，无 `.`/`-`，`"appdb"` 合规） |
| 8 | `_ok_stdout` | 加 `"postgres.bloat_tables": '{"total_tables":0,"results":[]}'`（函数内无 default 字面 dict；`test_benign_value_rides_sh_filter` 经 `_INJECTABLE_PARAMS` 调它，缺键 → KeyError 全量 error。小写 helper-local，`grep '^_[A-Z]'` 抓不到） |
| 9 | `_PROBE_TEST_SOURCES` | 加 `"postgres.bloat_tables": .../test_postgres_bloat_tables.py`（**触发 failure-class meta-guard，强制 test 含 `requires_unmet` + `ok` + `exception` 三类断言 —— 依赖新增 requires_unmet 测试 + D3.1 的 exception 测试**） |
| 10 | `_ALL_FIXTURES` | 加 `+ sorted((_FIXTURES/"postgres_bloat_tables").glob("*.json"))`，使 `_ALL_FIXTURES` 的 replay-key/env 不变量门覆盖 bloat fixture（**非** secret-leak 扫描:bloat 走 peer auth、recorder `-u postgres`,fixture 内**无注入的 secret 值**,leak 扫描对它本就 vacuous;真实价值是 replay-key 完整性覆盖） |
| 11 | `test_only_docker_networks_is_list_shaped` | 断言 `["docker.networks"]` → 现两个 list-shaped。**该测不 sort、按 `_ALL_SERVICE_MANIFESTS.items()` 插入序**:`postgres.connection_usage`(L104) 在 `docker.networks`(L106) **之前**,故若把 bloat 插进 postgres 组(connection_usage 邻位)它会**先于** docker.networks → 写成 `["docker.networks","postgres.bloat_tables"]` 会**顺序反转真红**。**改法择一(推荐 a)**:(a) 断言改 `sorted(list_shaped) == ["docker.networks", "postgres.bloat_tables"]`(顺序无关、鲁棒);(b) 显式把 bloat 插在 docker.networks **之后**并按插入序写。+ docstring「other 11」→「other 12」 |
| 12 | docstring narrative + 计数注释 | (a) `_ALL` 注释 `12 = 2 spike + 6 wave-2a + 3 wave-2b + 1 migrated` → `13 = ... + 2 migrated`;(b) **`_SECRET_SERVICE_MANIFESTS` 的 docstring narrative 当前字面 `(6)` 且只列 6 名、缺 `redis.slowlog`(slowlog 迁移遗留的既存 stale —— 真实 dict 已 7 名含 slowlog)**:须重写为 `(8)` + **完整 8 名清单(补 `redis.slowlog` 与 `postgres.bloat_tables` 两名)**,否则按字面「6→8 只加 bloat」改完仍缺 slowlog;(c) `_at_least_the_expected_fixtures_scanned` 下界 `>=20` 实盘 35→38、**仍成立无需改**,仅来源注释可加 bloat 3 fixture |

`TestOutputShapeDiscipline.test_output_shape_by_array_field_not_for_each` 无需改代码：bloat 进 `_ALL` 后参数化分支自动校验 (a) `results` ∈ {results,items,records} ✓、(b) `max_results` 参数 + `LIMIT` ✓、(c) `total_tables` 标量 ✓ —— D2 重构正是为过这三条。

### D6 — 测试与 recorder 改动

- `test_postgres_bloat_tables.py`：`setenv("PGPASSWORD",...)` → `"HOSTLENS_POSTGRES_PASSWORD"`；`test_fixtures_inject_password_via_env_not_plaintext` 的 `$PGPASSWORD` → `$HOSTLENS_POSTGRES_PASSWORD`；三 snapshot 测试 output 断言加 `total_tables`；新增 **四**测：finding-trigger（降阈值，presence 断言非 count==1）/ requires_unmet（缺 psql）/ BREAKING 回归（只设旧 PGPASSWORD → requires_unmet）/ **exception（回放 D3.1 的 conn_refused error fixture → `status == "exception"`，满足 failure-class meta-guard）**。
- `_record_postgres_bloat.py`：env `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`（docstring 同步）；**加 1 个 error 场景**（连不可达端口 / 错密码 → 录 `conn_refused.json`，stderr 脱敏）。

### D7 — 文档与 stale 注释（review 查实多处漏登，逐个点名）

迁移会让多处把 bloat_tables/`PGPASSWORD` 当祖父态或 canonical 示例的文档变 stale，**禁泛泛说「docs」**，逐文件点名：

- **`docs/operations/inspectors.md`（worked example，最重要）**：该文件把 `postgres.bloat_tables` + `export PGPASSWORD=...` 当**贯穿全章的唯一 secret 配置 worked example**（L405/445/453/462/468 + L482 + L174 表）。迁移后用户照抄 `export PGPASSWORD=` 会命中**本提案新加的 BREAKING 回归测试**所锁的 `requires_unmet` —— 文档直接教错。须逐处改写为 `HOSTLENS_POSTGRES_PASSWORD`。（注:L405/462 的 `postgres.bloat.tables` 点号是既存文档 typo,正确名 `postgres.bloat_tables`,顺手修。）
- **`docs/operations/targets.md` L159-166（祖父态陈述）**：两处迁移后为假 —— (1)「`postgres.bloat_tables` still declares a non-`HOSTLENS_` secret name pending migration」;(2)「`redis.slowlog` was the last grandfathered pre-spike seed besides `postgres.bloat_tables`」。须改为「祖父条款闭合、无在册祖父化 inspector」措辞(对齐 spec 闭合)。
- **`tests/cli/test_doctor.py`**:(a) L58 注释「legacy PGPASSWORD-derived name」;(b) **L522** 注释把 bloat_tables 与「on HOSTLENS_」探针并列、隐含 bloat 非 HOSTLENS_ —— 两处均须更新（secret 集 rglob 动态派生、不会红,仅注释）。
- **`docs/operations/inspector-authoring-contract.md`（canonical 活例，Round-2 查实）**:`:178` 活例 `postgres.bloat_tables` 展示 `json_build_object('results', json_agg(...))` 是**截断前**形态（无 `total_tables`、无 `LIMIT`）。迁移后 canonical SQL 加 `total_tables` + `LIMIT {{ max_results }}`,该活例仍教旧形态 → 用户照写出的 list inspector 缺 top-N 截断/total、恰**违反本提案要补的 list-shape MUST**。须把 `:178` 活例 SQL 同步加 `total_tables`/`LIMIT`,或注明「截断后形态见 `bloat_tables.yaml`」。（`:15/:77/:335` 的 bloat narrative 一并核对。）
- **`docs/ARCHITECTURE.md` L475-498（Round-2 修正定性）**:L475-476 标题「复杂示例 2…（PostgreSQL 表 bloat）」+ `name: postgres.bloat.tables` —— 这是**具名 bloat 专属** worked example（参数 host/port/database 与真 manifest 发散的说明性副本），**非**「通用示例」。L497-498 `secrets: - PGPASSWORD` 迁移后 stale,须对齐为 `HOSTLENS_POSTGRES_PASSWORD`,与 inspectors.md 同批改;非 test-enforced、不阻断 archive。
- **`openspec/specs/inspector-plugin-system/spec.md`（第二 spec delta，见下「修改功能」）**:需求「CLI `hostlens inspectors show <name>` 必须脱敏 secrets」的 prose（L914 `如 [PGPASSWORD]`）与场景「secrets 字段只显示名字」（L919-922,`manifest.secrets=[PGPASSWORD]` + `postgres.bloat.tables`）拿 bloat 当 secret-name 脱敏具例。迁移后 bloat 真实 secret = `HOSTLENS_POSTGRES_PASSWORD`,该具例反事实。本提案**新增第二 spec delta** MODIFY 此需求:示例 secret 名 `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`、inspector 名 typo `postgres.bloat.tables` → `postgres.bloat_tables`、env 占位同步。非 test-enforced,但闭合祖父条款须不留关于该 inspector 的反事实陈述。
- **CHANGELOG / docs**:**BREAKING** env 名 + `PGCONNECT_TIMEOUT=5` + 列表 top-20 截断 + output 加 `total_tables`。

## 风险 / 权衡

- **list-shape 行为变更（top-N 截断）**：bloat 表超 20 时仅报最严重 20 张 + `total_tables` 提示总数。缓解：默认 20 对真实库足够；`max_results` 可调；排序保证报的是最严重的。**这是把既有「回吐全集」违规改为合规的必要变更**，非退化。
- **output_schema 加 `total_tables`（additive）**：下游 diagnostician / report 兼容（新增字段，array items 不变）。
- **欠范围风险已被前置审计消解**：list-shape 截断 + finding-trigger 轨缺失这两处祖父条款漏登项已纳入范围（立项前对抗性 review 查实）；实现时按 D5 清单 12 项逐条对账，防 crosscheck KeyError。
- **重录依赖真 postgres**：CI 不依赖（离线回放）；重录是一次性 dev 操作，须人工核对命令串无明文密码 + output 含 total_tables（D4 + tasks 验证步）。
- **spec 祖父条款闭合后的回归门**：日后若新发现 pre-spike 漂移 inspector，须独立 migrate、禁新立豁免（spec 已明文）。本提案不预设还有未发现的 pre-spike seed（仅这两个被登记）。
