# 任务：migrate-postgres-bloat-tables-service-contract

## 1. 分支与前置核查

- [ ] 1.1 从最新 main 切 `feat/migrate-postgres-bloat-tables-service-contract` 分支
- [x] 1.2 grep 全仓 `PGPASSWORD` + `bloat_tables`（排除 `HOSTLENS_POSTGRES_PASSWORD` 与同域已合规 sibling）扇出点，确认 bloat_tables 相关位置全列（**review 查实易漏的 doc 扇出已并入**）：
  - 代码/测试：`builtin/postgres/bloat_tables.yaml`(secrets+collector+注释) / `test_postgres_bloat_tables.py`(setenv+`$PGPASSWORD` 断言+docstring) / `_record_postgres_bloat.py`(env+docstring) / `tests/cli/test_doctor.py`(L58 **与 L522** 注释 stale) / `fixtures/postgres_bloat_tables/*.json`(内嵌命令串靠重录消除) / `test_service_contract_crosscheck.py`(§5 冻结结构)
  - **文档（task 6.3 逐个改写，勿泛泛说 docs）**：`docs/operations/inspectors.md`(bloat_tables+PGPASSWORD canonical worked example,L405/445/453/462/468/482/174) / `docs/operations/targets.md`(L159-166 祖父态陈述) / `docs/operations/inspector-authoring-contract.md`(:178 活例展示截断前 SQL + :15/:77/:335 narrative) / `docs/ARCHITECTURE.md`(L475-498 具名 bloat worked example secret 名)
  - **第二 spec delta（task 6.1b）**：`openspec/specs/inspector-plugin-system/spec.md`(L909-933「inspectors show 脱敏」需求拿 bloat+PGPASSWORD 当具例，L914 prose + L919-922 场景)
  - **确认不受影响（无须改）**：`tests/cli/test_inspectors.py` 的 `db.pg`/`PGPASSWORD` 是独立测试自造 manifest、与 builtin bloat 无关；`tests/inspectors/test_docker_target_cohort_guard.py` 的 bloat ∈ `_INCLUDE` 因 targets 不变（仍 docker+k8s）而**不破**，无须改

## 2. manifest 迁移（`src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml`）

- [x] 2.1 `secrets: [PGPASSWORD]` → `[HOSTLENS_POSTGRES_PASSWORD]`
- [x] 2.2 collector remap：`PGPASSWORD="$PGPASSWORD" psql` → `PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}" psql`（凭据仍走 env、不进 argv）；description/注释更新为 HOSTLENS_ 契约措辞
- [x] 2.3 **加 `PGCONNECT_TIMEOUT=5`**：collector `PGPASSWORD="..." PGCONNECT_TIMEOUT=5 psql ...`（< `timeout_seconds` 30，满足连接超时 MUST，对齐 connection_usage 模板）；注释说明
- [x] 2.4 **列表截断重构（D2）**：(a) parameters 加 `max_results: { type: integer, default: 20 }`；(b) SQL subquery 加 `ORDER BY n_dead_tup DESC LIMIT {{ max_results }}`（整数参数无需 `| sh`）；(c) 外层 `json_build_object` 加 `'total_tables', (SELECT count(*) FROM pg_stat_user_tables)`；(d) output_schema 加 `total_tables: { type: integer }` 并入 `required`
- [x] 2.5 `python3 -c "from pathlib import Path; from hostlens.inspectors.loader import load_manifest; load_manifest(Path('src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml'))"` 自检加载通过

## 3. recorder 与 fixture 重录

- [x] 3.1 `_record_postgres_bloat.py`：env `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`（含 docstring 的示例命令）；**加 1 个 error 场景**（连不可达端口 / 错密码 → 录 `conn_refused.json`，stderr 脱敏）
- [x] 3.2 经 recorder **重录**（禁手编 —— 命令串变了 ReplayTarget 逐字节匹配）：`bloated.json` / `healthy.json` / `empty.json`（命令串现含 `PGCONNECT_TIMEOUT=5` + `LIMIT 20`、output 含 `total_tables`）；**新增** `conn_refused.json`（exception 轨）。**finding-trigger 轨**零新 fixture（复用 bloated.json 降阈值），**exception 轨**需此 error fixture —— 共 **4 fixture**
- [x] 3.3 核对 fixture：命令串含 `PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}"` + `PGCONNECT_TIMEOUT=5` + `LIMIT 20`、**不含**明文密码（仅 `$HOSTLENS_POSTGRES_PASSWORD` env-ref 形式）；三正常 fixture output 均含 `total_tables` 整数；`bloated.json` 的 `orders` 行默认阈值仍触发；`conn_refused.json` 为 psql 非零退出 + 空 stdout（回放出 `status=exception`）、stderr 无明文密码

## 4. 测试更新（`tests/inspectors/test_postgres_bloat_tables.py`）

- [x] 4.1 autouse `setenv("PGPASSWORD", ...)` → `"HOSTLENS_POSTGRES_PASSWORD"`；`test_fixtures_inject_password_via_env_not_plaintext` 的 `$PGPASSWORD` 断言 → `$HOSTLENS_POSTGRES_PASSWORD`
- [x] 4.2 三 snapshot 测试（bloated/healthy/empty）output 断言加 `total_tables`（重录后实际值，**不预设**）；`test_empty_db_parses_object_not_array` 的 `result.output == {"results": []}` → `{"total_tables": <实际>, "results": []}`
- [x] 4.3 新增 **finding-trigger 轨测试** `test_finding_trigger_with_lowered_thresholds`：回放 `bloated.json`、传**降低的阈值参数**（如 `dead_ratio_threshold=0.01, dead_tuple_threshold=10`），断言 `sessions`（默认阈值下不触发）现触发 warning（证 finding wiring；阈值是 DSL 参数不在命令串、复用同 fixture 零新 finding 文件）。**注意**：降阈值后 `orders`（默认即触发）仍触发 → findings ≥ 2，断言用 **presence**（`any("sessions" in f.message for f in findings)`）、**禁** `len(findings) == 1`
- [x] 4.4 新增 `requires_unmet` 测试（`_PROBE_TEST_SOURCES` failure-class meta-guard 要求 test 含 `status == "requires_unmet"`）：缺 `psql` 二进制 → requires_unmet
- [x] 4.5 新增 **BREAKING 回归测试** `test_legacy_pgpassword_alone_yields_requires_unmet`：只设旧 `PGPASSWORD`、不设 `HOSTLENS_POSTGRES_PASSWORD` → 断言 `status == "requires_unmet"`（锁旧 env 名静默失效但诚实 skip）
- [x] 4.6 新增 **exception 轨测试**（`_PROBE_TEST_SOURCES` failure-class meta-guard 强制；`bloat ∉ _NO_EXCEPTION_SNAPSHOT` → test **必须**含 `status == "exception"`，缺则 5.6 落地后 crosscheck 红）：回放 3.x 新增的 `conn_refused.json` error fixture → 断言 `status == "exception"`（pattern 抄 sibling `test_postgres_long_queries.py` 的 `test_conn_refused_fails_loud`）

## 5. crosscheck 冻结结构（`tests/inspectors/test_service_contract_crosscheck.py`，按 design D5 清单 12 项逐项对账）

- [x] 5.1 `_ALL_SERVICE_MANIFESTS` 加 `"postgres.bloat_tables"`（**12→13**）；`test_all_service_manifests_count_frozen` 断言 `== 12` → `== 13`
- [x] 5.2 `_SECRET_SERVICE_MANIFESTS` 加 `"postgres.bloat_tables": _ALL_SERVICE_MANIFESTS["postgres.bloat_tables"]`（**7→8**）；`test_secret_service_manifests_count_frozen` 断言 `== 7` → `== 8`
- [x] 5.3 `_SECRET_CLIENT_RULES` 加 `"postgres.bloat_tables": {"native_env": "PGPASSWORD", "forbidden_flags": ()}`
- [x] 5.4 `_CLIENT_TIMEOUT_TOKEN` 加 `"postgres.bloat_tables": ("PGCONNECT_TIMEOUT=5", 5)`
- [x] 5.5 `_INJECTABLE_PARAMS` 加 `("postgres.bloat_tables", _ALL_SERVICE_MANIFESTS["postgres.bloat_tables"], "dbname", "appdb")`（正控值 `"appdb"` 须满足 bloat **更严** pattern `^[A-Za-z_][A-Za-z0-9_]*$`，无 `.`/`-`）；**同步加 `_ok_stdout` 的 bloat 键** `"postgres.bloat_tables": '{"total_tables":0,"results":[]}'`（无 default 字面 dict，缺键 → KeyError 全量 error；小写 helper-local，grep `^_[A-Z]` 抓不到，须查 memory `project_service_inspector_crosscheck_frozen_structures` 核对）
- [x] 5.6 `_PROBE_TEST_SOURCES` 加 `"postgres.bloat_tables": Path(__file__).parent / "test_postgres_bloat_tables.py"`（**触发 failure-class meta-guard，强制 test 含 `requires_unmet`(4.4) + `ok`(既有) + `exception`(4.6) 三类断言**；bloat ∉ `_NO_EXCEPTION_SNAPSHOT`，缺 exception 即红）；`_ALL_FIXTURES` 加 `+ sorted((_FIXTURES / "postgres_bloat_tables").glob("*.json"))`（覆盖 replay-key/env 不变量门；bloat peer-auth 无注入 secret 值，leak 扫描对它本就 vacuous，价值是 replay-key 完整性）
- [x] 5.7 **list-shape 两测适配**：`test_only_docker_networks_is_list_shaped` 现断言 `== ["docker.networks"]`。**该测不 sort、按 `_ALL_SERVICE_MANIFESTS.items()` 插入序**——`postgres.connection_usage`(L104) 在 `docker.networks`(L106) **之前**，若把 bloat 插进 postgres 组它会**先于** docker.networks → 直接写 `["docker.networks","postgres.bloat_tables"]` 会**顺序反转真红**。**改法择一（推荐 a）**：(a) 断言改 `sorted(list_shaped) == ["docker.networks", "postgres.bloat_tables"]`（顺序无关、鲁棒）；(b) 显式把 bloat 插在 `docker.networks` **之后**再按插入序写。+ docstring「other 11」→「other 12」。确认 `TestOutputShapeDiscipline.test_output_shape_by_array_field_not_for_each` 对 bloat 自动校验 (a)/(b)/(c) 通过（无需改测试码，靠 D2 重构）
- [x] 5.8 **stale 注释/narrative 同步**：(a) `_ALL` 计数注释 `12 = 2 spike + 6 wave-2a + 3 wave-2b + 1 migrated` → `13 = ... + 2 migrated`；(b) **`_SECRET_SERVICE_MANIFESTS` 的 docstring narrative 当前字面是 `(6)` 且只列 6 名、缺 `redis.slowlog`**（slowlog 迁移时遗留的**既存 stale** —— 真实 dict 已 7 名含 slowlog；本提案 5.2 把 dict 计数 7→8）：须把 narrative 重写为 `(8)` + **完整 8 名清单（补 `redis.slowlog` 与 `postgres.bloat_tables` 两名）**，否则按字面「只加 bloat」改完 narrative 仍缺 slowlog；(c) `_at_least_the_expected_fixtures_scanned` 的 `>= 20` 是下界、加 bloat fixture（+4）只增不减、**仍成立无需改断言**，仅来源注释可加 bloat fixture；(d) **`_ALL` 上方 L92 注释 `Two spike probes + ... + one migrated (redis.slowlog, ...)`** 也 stale（「one migrated」只列 slowlog）→ 改「two migrated (`redis.slowlog`, `postgres.bloat_tables`)」。grep 用 **`migrated`**（非 `1 migrated`，否则漏 L92 的 `one migrated`）+ `other 11`/`(6)`/`>= 20` 全量定位逐处核对
- [x] 5.9 跑 crosscheck 全门验证 bloat 进 `_ALL`/`_SECRET` 后全部参数化断言通过（HOSTLENS_ 前缀 / PGPASSWORD remap / 无 argv 明文 / `PGCONNECT_TIMEOUT=5` timeout 门 / list-shape (a)(b)(c) / injection-pattern + `_ok_stdout` 正控 / failure-class probe / leak 扫描）；console 全量 pytest 无 KeyError/error

## 6. 文档与 spec 同步

- [x] 6.1 确认 spec delta 与实现一致 + 临时副本实测 `openspec-cn archive` rebuild 通过：(a) `service-inspector-contract` MODIFY 三需求，祖父条款**闭合** + 新增「postgres.bloat_tables 迁移后受契约管辖」场景（祖父化 seed 列表归零、无在册豁免）；(b) **`inspector-plugin-system` MODIFY「CLI inspectors show 必须脱敏 secrets」需求**：示例 secret 名 `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`、inspector 名 typo `postgres.bloat.tables` → `postgres.bloat_tables`、env 占位同步（脱敏行为契约不变，完整复制需求块全部场景）
- [x] 6.2 `tests/cli/test_doctor.py` 注释 stale 更新（**两处**）：(a) L58「legacy PGPASSWORD-derived name」；(b) **L522** 把 bloat_tables 与「on HOSTLENS_」探针并列、隐含 bloat 非 HOSTLENS_ —— 均改为 bloat 归入 HOSTLENS_ 命名一类（secret 集 rglob 动态派生、不会红，仅注释）
- [x] 6.3 迁移文档（**逐文件点名，勿泛泛说 docs**，见 design D7）：
  - `docs/operations/inspectors.md`：bloat_tables+`PGPASSWORD` 贯穿全章 worked example（L405/445/453/462/468/482/174）逐处改写为 `HOSTLENS_POSTGRES_PASSWORD`（**否则用户照抄会命中本提案新加的 BREAKING requires_unmet 回归测试、文档教错**）；顺手修 L405/462 的 `postgres.bloat.tables` 点号 typo
  - `docs/operations/targets.md` L159-166：两处祖父态陈述（「still declares non-HOSTLENS_ ... pending migration」/「last grandfathered ... besides postgres.bloat_tables」）改为「祖父条款闭合、无在册祖父化 inspector」
  - **`docs/operations/inspector-authoring-contract.md`（canonical 活例）**：`:178` 活例 `postgres.bloat_tables` 展示**截断前** SQL（`json_build_object('results', json_agg(...))`，无 `total_tables`/`LIMIT`）→ 迁移后 canonical 形态加 `total_tables` + `LIMIT {{ max_results }}`，须同步该活例或注明「截断后形态见 `bloat_tables.yaml`」（否则用户照写出的 list inspector 缺截断、违反 list-shape MUST）；`:15/:77/:335` bloat narrative 一并核对
  - `docs/ARCHITECTURE.md` L475-498：**具名 bloat 专属** worked example（标题「复杂示例 2…（PostgreSQL 表 bloat）」+ `name: postgres.bloat.tables`，非「通用示例」）——L497-498 `secrets: - PGPASSWORD` 对齐为 `HOSTLENS_POSTGRES_PASSWORD`、修 `.bloat.tables` typo（非 test-enforced、不阻断 archive，与 inspectors.md 同批改）
  - CHANGELOG / docs：**BREAKING** —— env 名 `PGPASSWORD` → `HOSTLENS_POSTGRES_PASSWORD`（用户须改设）+ `PGCONNECT_TIMEOUT=5` 连接超时 + 列表 top-20 截断（`max_results` 可调）+ output 加 `total_tables`

## 7. 验证与交付

- [x] 7.1 `pre-commit run --all-files` + `mypy --strict src/` + console `pytest` 全量绿（**不只跑 postgres 子集** —— 防顶动 crosscheck 12 处冻结结构 / list-shape 两测 / 既有并发）。实测:pre-commit 全 Passed(ruff-format 重排 1 测试文件已修)、mypy --strict 97 files 干净、全量 pytest **3134 passed / 27 skipped / 0 failed**(装 mcp+kubernetes_asyncio 可选依赖后)
- [x] 7.2 真机重录验证（docker compose / docker run 真 postgres:16，seed bloatdb 关 autovacuum / healthydb / emptydb + 1 error 场景如错端口）：`HOSTLENS_POSTGRES_PASSWORD=<pw> python tests/inspectors/_record_postgres_bloat.py` 跑通，**4 fixture**（重录 3 + 新增 `conn_refused.json`）、命令串含 `PGCONNECT_TIMEOUT=5` + `LIMIT 20` 不含明文、3 正常 output 含 `total_tables`、bloated.json orders 默认阈值触发、`conn_refused.json` 回放出 `status=exception`；记录进 PR 描述。实测:hl-pg postgres:16 真录,bloated total_tables=2/orders 2000:4000:0.6667/sessions 3800:100:0.0256、healthy total_tables=1、empty {total_tables:0,results:[]}、conn_refused exit2 空stdout status=exception,全部回放 misses=[]
- [ ] 7.3 commit（分支本地）→ 对抗性 review（含 secret 处理 / list-shape 截断正确性 / exception 轨 fixture / 重录 fixture 假绿 / crosscheck 12 处冻结结构 + list-shape 两测 + failure-class meta-guard 完整性 / 祖父条款闭合 / 两 spec delta，属「应该跑」类）→ triage 修复 → APPROVE/CLEAR 后 push
- [ ] 7.4 `\gh pr create --base main`，PR 描述含**两** spec 引用 + 重录验证输出 + **BREAKING env 名 + `PGCONNECT_TIMEOUT=5` + 列表截断** 声明 + crosscheck 结构清单 + 祖父条款闭合说明
- [ ] 7.5 CI 全绿后拉 Copilot / Cursor BugBot 评论逐条 triage，再 `\gh pr merge --squash --delete-branch`
- [ ] 7.6 归档：`openspec-cn archive`（delta 合入 `openspec/specs/`，change 目录 mv 到 archive）；确认归档后主 spec 祖父条款已闭合（seed 列表归零）
