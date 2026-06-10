# 任务：migrate-redis-slowlog-service-contract

## 1. 分支与前置核查

- [x] 1.1 从最新 main 切 `feat/migrate-redis-slowlog-service-contract` 分支
- [x] 1.2 grep 全仓 `REDIS_PASSWORD`（排除 `HOSTLENS_REDIS_PASSWORD`）扇出点，列出全部位置（已知：`builtin/redis/slowlog.yaml`(secrets+collector×4+注释) / `test_redis_slowlog.py`(setenv+docstring) / `_record_redis_slowlog.py`(env+docstring) / `tests/cli/test_doctor.py`(注释 stale，secret 集动态 rglob 派生不会红但注释须更) / `fixtures/redis/slowlog_*.json`(内嵌命令串靠重录消除)）；确认无遗漏

## 2. manifest 迁移（`src/hostlens/inspectors/builtin/redis/slowlog.yaml`）

- [x] 2.1 `secrets: [REDIS_PASSWORD]` → `[HOSTLENS_REDIS_PASSWORD]`
- [x] 2.2 collector 4 处（SLOWLOG LEN + EVAL × auth 分支）`redis-cli -a "$REDIS_PASSWORD" --no-auth-warning` → `REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD" redis-cli --no-auth-warning`；分流判据改 `[ -n "${HOSTLENS_REDIS_PASSWORD:-}" ]`；**禁** `-a` 明文残留
- [x] 2.3 **collector 加 `-t 5` 连接超时**：4 处 redis-cli 调用（含无鉴权分支）加 `-t 5`（满足「连接超时 5 < 采集超时 15」MUST，对齐 memory_usage 模板）；description/注释更新为 HOSTLENS_ 契约措辞 + `-t 5` 说明
- [x] 2.4 `python3 -c "from pathlib import Path; from hostlens.inspectors.loader import load_manifest; load_manifest(Path('src/hostlens/inspectors/builtin/redis/slowlog.yaml'))"` 自检加载通过

## 3. recorder 与 fixture 重录

- [x] 3.1 `_record_redis_slowlog.py`：env `REDIS_PASSWORD` → `HOSTLENS_REDIS_PASSWORD`（含 docstring）；新增 **semantic-abnormal 场景**（`CONFIG SET slowlog-log-slower-than 0` + `DEBUG SLEEP 0.15` 造 ≥100ms 慢查询使 `max_micros ≥ slow_micros(100000)`，触发 max_micros rule —— **非**仅靠 count，因默认 warn_count=1 使 count rule 近恒真）；新增 **special-char-pw 场景**（密码 `"p w*d"`，复用 persistence 同值，走 recorder 脱敏路径）
- [x] 3.2 经 recorder **重录**（禁手编命令串）：`slowlog_empty` / `slowlog_nonempty`(finding-trigger) / `slowlog_conn_refused`（命令串现含 `REDISCLI_AUTH=` + `-t 5`）；**新增** `slowlog_semantic_abnormal` / `slowlog_special_char_pw`
- [x] 3.3 核对重录 fixture：命令串含 `REDISCLI_AUTH=` + `-t 5`、**不含** `-a "$`；stdout/stderr 无明文密码；`slowlog_semantic_abnormal` 的 max_micros ≥ 100000（触发 max_micros rule）

## 4. 测试更新（`tests/inspectors/test_redis_slowlog.py`）

- [x] 4.1 autouse fixture `setenv("REDIS_PASSWORD","")` → `"HOSTLENS_REDIS_PASSWORD"`；**更新** `test_nonempty_slowlog_derives_findings` 的 `output ==` 断言为重录后实际 count/max_micros 值（**不预设仍是 8** —— 重录改值）；empty/conn_refused 测试对新重录 fixture 仍绿（misses==[]）。**正确性锚说明**（D-7）：snapshot 的 count/max_micros 数值只锁「重录那次」，collector 正确性的真正锚是 finding severity 序列（`[warning]`）+ 7.2 真机重录 + 人工 review recorder 真造数据，PR 描述禁 claim「snapshot 锁了 collector 正确性」
- [x] 4.2 新增 `test_semantic_abnormal_at_default_thresholds`：默认阈值（不降阈值）断言 `slowlog_semantic_abnormal` 触发 **max_micros rule** warning + message（pattern 抄 `test_redis_persistence.py` semantic-abnormal）
- [x] 4.3 新增 `test_special_char_pw`：`"p w*d"` 密码经 REDISCLI_AUTH 回放，命令串不破 / 不 word-split（验命令安全；fixture 注释澄清 metrics-only 下非脱敏证据）
- [x] 4.4 新增 `requires_unmet` 测试（`_PROBE_TEST_SOURCES` failure-class meta-guard 要求 test 含 `status == "requires_unmet"`）：缺 redis-cli → requires_unmet
- [x] 4.5 新增 **BREAKING 回归测试** `test_legacy_redis_password_alone_yields_requires_unmet`：只设旧 `REDIS_PASSWORD`、不设 `HOSTLENS_REDIS_PASSWORD` → 断言 `status == "requires_unmet"`（锁旧 env 名静默失效但诚实 skip）

## 5. crosscheck 9 处冻结结构（`tests/inspectors/test_service_contract_crosscheck.py`）

- [x] 5.1 `_ALL_SERVICE_MANIFESTS` 加 `"redis.slowlog": _ALL_SERVICE_MANIFESTS_PATH(...)`（**11→12**）；`test_all_service_manifests_count_frozen` 断言 `== 11` → `== 12`
- [x] 5.2 `_SECRET_SERVICE_MANIFESTS` 加 `"redis.slowlog": _ALL_SERVICE_MANIFESTS["redis.slowlog"]`（**6→7**）；`test_secret_service_manifests_count_frozen` 断言 `== 6` → `== 7`
- [x] 5.3 `_SECRET_CLIENT_RULES` 加 `"redis.slowlog": {"native_env": "REDISCLI_AUTH", "forbidden_flags": ("-a ",)}`
- [x] 5.4 `_CLIENT_TIMEOUT_TOKEN` 加 `"redis.slowlog": ("-t 5", 5)`
- [x] 5.5 `_INJECTABLE_PARAMS` 加 `("redis.slowlog", _ALL_SERVICE_MANIFESTS["redis.slowlog"], "host", "redis.internal")`（host pattern 注入正控）；**同步加 `_ok_stdout` 的 slowlog 键**（第 9 处冻结结构 —— `_ok_stdout(probe)` 是函数内 `return {...}[probe]` 无 default 字面 dict，`test_benign_value_rides_sh_filter` 经 `_INJECTABLE_PARAMS` 调它，缺 slowlog 键 → KeyError 全量 pytest error）：加 `"redis.slowlog": '{"count":1,"max_micros":1}'`（count≥warn_count=1 触发 warning 但 status=ok，正控用）。**注意**：`_ok_stdout` 是小写 helper-local dict，`grep '^_[A-Z]'` 抓不到，须查 memory `project_service_inspector_crosscheck_frozen_structures` 的结构清单核对
- [x] 5.6 `_PROBE_TEST_SOURCES` 加 `"redis.slowlog": Path(__file__).parent / "test_redis_slowlog.py"`（依赖 4.4 的 requires_unmet 测试）；`_ALL_FIXTURES` 加 `+ sorted((_FIXTURES / "redis").glob("slowlog_*.json"))`（leak 扫描覆盖，防 vacuous）；`_RECORDED_SECRET_VALUES` **复用** `"p w*d"` 不新增（若 5 个 slowlog fixture 的下界影响 `test_at_least_the_expected_fixtures_scanned >= 20` 的来源注释，同步更新该注释）
- [x] 5.7 **stale 注释同步**（断言机械鲁棒不红但与「冻结结构完整」卖点相左，须更）：(a) **模块顶部 docstring 计数注释**（`_ALL` 定义块附近的 `11 = 2 spike + 6 wave-2a + 3 wave-2b` 与 `Two spike probes + six wave-2a + three wave-2b inspectors` 两处）→ 改为 `12 = 2 spike + 6 wave-2a + 3 wave-2b + 1 migrated`（slowlog 是 migrated，既非 spike 也非 wave）；(b) `test_only_docker_networks_is_list_shaped` docstring「other **10** inspectors」→ 11；(c) `_ALL_FIXTURES` / `test_at_least_the_expected_fixtures_scanned` 的来源注释加 slowlog 5 个 fixture（下界 `>=20` 不动）。实现时 grep `2 spike`/`other 10`/`wave-2b` 全量定位，逐处核对
- [x] 5.8 跑 crosscheck 全门验证 slowlog 进 `_ALL`/`_SECRET` 后全部参数化断言通过（argv / HOSTLENS_ / REDISCLI_AUTH remap / timeout `-t 5` / output-shape / no-fork / injection-pattern + `_ok_stdout` 正控 / failure-class probe / 双轨 fixture / leak 扫描）；console 全量 pytest 无 KeyError/error

## 6. 文档与 spec 同步

- [x] 6.1 确认 spec delta（service-inspector-contract MODIFY 三需求，slowlog 4 处点名收窄为仅 postgres.bloat_tables + 新增「迁移后受管辖」场景）与实现一致；临时副本实测 `openspec-cn archive` rebuild 通过
- [x] 6.2 `tests/cli/test_doctor.py` 注释 stale 更新（slowlog 不再是「祖父化 seed」）
- [x] 6.3 迁移文档：CHANGELOG / docs 载明 **BREAKING** —— `redis.slowlog` env 名 `REDIS_PASSWORD` → `HOSTLENS_REDIS_PASSWORD`（用户须改设）+ collector 加 `-t 5` 连接超时

## 7. 验证与交付

- [x] 7.1 `pre-commit run --all-files` + `mypy --strict src/` + console `pytest` 全量绿（**不只跑 redis 子集** —— 防顶动 crosscheck 9 处冻结结构 / 既有并发）
- [x] 7.2 真机重录验证（docker compose 真 redis）：`python -m tests.inspectors._record_redis_slowlog` 跑通，5 fixture 重录、命令串含 REDISCLI_AUTH + `-t 5` 不含 `-a`、semantic_abnormal max_micros≥100000；记录进 PR 描述
- [ ] 7.3 commit（分支本地）→ 对抗性 review（含 secret 处理 / 命令串注入面 / 重录 fixture 假绿 / 9 处冻结结构完整性，属「应该跑」类）→ triage 修复 → APPROVE/CLEAR 后 push
- [ ] 7.4 `\gh pr create --base main`，PR 描述含 spec 引用 + 重录验证输出 + **BREAKING env 名 + `-t 5`** 声明 + 9 处 crosscheck 结构清单
- [ ] 7.5 CI 全绿后拉 Copilot / Cursor BugBot 评论逐条 triage，再 `\gh pr merge --squash --delete-branch`
- [ ] 7.6 归档：`openspec-cn archive`（delta 合入 `openspec/specs/`，change 目录 mv 到 archive）
