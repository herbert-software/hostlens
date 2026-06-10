# 设计：迁移 redis.slowlog 至 service-inspector-contract 全合规

## Context

`redis.slowlog` 是 service-inspector-contract spike 前的 seed，祖父化登记三处漂移（secret 非 HOSTLENS_、argv 明文、缺 semantic-abnormal 轨）。**review 另查实第四处既存违规**：slowlog 的 redis-cli 调用无 `-t` 连接超时（违反「连接超时<采集超时」MUST），祖父化掩盖了它。全合规迁移须消解全部四处，使 slowlog 从祖父条款移除、**同时进 `_ALL_SERVICE_MANIFESTS`（11→12）与 `_SECRET_SERVICE_MANIFESTS`（6→7）**，真正受全部 `_ALL_ITEMS` 契约门管辖。同域 3 个 wave-2 redis inspector 是合规模板。

## Goals / Non-Goals

**Goals**：slowlog secret → HOSTLENS_ + REDISCLI_AUTH remap + **加 `-t 5`**；补 semantic-abnormal（max_micros 判别轨）+ special-char-pw + requires_unmet + BREAKING 回归测试；slowlog 进 9 处冻结 crosscheck 结构（权威清单见 D2，含 `_ALL`(12) + `_SECRET`(7) + 两 count_frozen + `_ok_stdout` 等）；祖父条款移除 slowlog。

**Non-Goals**（详见 proposal）：postgres.bloat_tables（sibling）/ metrics-only 设计 / output_schema / findings / 阈值 / targets / cohort guard（不动）/ 新 secret 机制。

## Decisions

### D1 全合规而非仅 secret 迁移（用户已定）

仅迁 secret 会留 slowlog 在「部分祖父化」尴尬态。全合规一次消解全部漂移 → 祖父条款彻底移除 slowlog。代价比初判大得多（见 D2/D3）。

### D2 全合规 = 进 `_ALL_SERVICE_MANIFESTS`，连带 `-t 5` collector 变更与 9 处冻结结构

**关键结构事实**（本次 review 机械查实，初版提案漏判）：
- `_SECRET_SERVICE_MANIFESTS` 的值**字面引用** `_ALL_SERVICE_MANIFESTS["redis.slowlog"]` —— 不先把 slowlog 加进 `_ALL` 就 KeyError。
- spec delta 新场景声称 slowlog「**必须满足全部 MUST**」。crosscheck 的 timeout / output-shape / no-fork / injection-pattern / failure-class 门都 over `_ALL_ITEMS`（非 `_SECRET_ITEMS`）。故 slowlog 必须进 `_ALL`(11→12) 才真正受契约管辖；只进 `_SECRET` = 名义合规、实际逃过半数门 = 与 delta「全部 MUST」矛盾。
- 进 `_ALL` 即受 `test_client_timeout_strictly_smaller_than_collect_timeout` 管辖，而 slowlog manifest **无 `-t`**（memory_usage 有 `-t 5`）。故**必须给 slowlog 加 `-t 5`**（collector 行为变更）+ `_CLIENT_TIMEOUT_TOKEN["redis.slowlog"]=("-t 5",5)`。

连带 9 处冻结结构（proposal「变更内容」逐条列）：`_ALL`(12)/`_ALL` count(12)/`_SECRET`(7)/`_SECRET` count(7)/`_SECRET_CLIENT_RULES`/`_CLIENT_TIMEOUT_TOKEN`/`_INJECTABLE_PARAMS`(host 正控)/`_PROBE_TEST_SOURCES`+`_ALL_FIXTURES` glob+`_RECORDED_SECRET_VALUES`/**`_ok_stdout`**（注入正控的 benign stdout，函数内无 default 字面 dict）。**枚举陷阱**：`_ok_stdout` 是小写 helper-local dict、`grep '^_[A-Z]'` 抓不到，前两轮 review 漏判（与 round-1 `_SECRET`→`_ALL` KeyError 同根因）—— 枚举 crosscheck 冻结结构**必须**对照 memory `project_service_inspector_crosscheck_frozen_structures` 的清单，不能只靠 grep 大写常量。

**`-t 5` 是行为变更非纯合规装饰**：此前 slowlog 无连接超时 → redis 网络挂起会拖到 collect timeout 15s；加 `-t 5` 后 5s 内诚实失败。对正常路径透明，故纳入范围合理，但须改 proposal「不改采集结构」非目标。

### D3 fixture 必须重录、禁手编；重录改既有 snapshot 值

迁移改了命令串（env 名 + remap + `-t 5`），ReplayTarget 逐字节匹配命令，3 个既有 fixture（empty/nonempty/conn_refused）**必须**重录、不能手改命令串。**重录后 `slowlog_nonempty` 的 count/max_micros 实际值会变**（命令串变 + 录制环境 SLOWLOG 自计数效应：threshold=0 时 SLOWLOG LEN/EVAL 自身进 slowlog），故 `test_nonempty_slowlog_derives_findings` 的 `output == {"count":8,...}` 断言改为重录后实际值（不预设 8）。semantic-abnormal 与 special-char-pw 同样真录。

### D4 semantic-abnormal 必须触发 max_micros rule（非仅 count rule）

slowlog 默认 `warn_count=1` **极低** —— 任何非空 slowlog（count≥1）即触发 count rule warning。故契约「semantic-abnormal 默认阈值触发」机械门对 slowlog **近恒真**（健康态 nonempty 在默认阈值下已触发），无法机械区分 finding-trigger 与 semantic-abnormal。**判别轨 = max_micros rule**（默认 `slow_micros=100000`=100ms）：semantic-abnormal 须造 `max_micros≥100000` 的真慢查询（`CONFIG SET slowlog-log-slower-than 0` + `DEBUG SLEEP 0.15`），snapshot 断言触发 max_micros rule 的 warning（健康态 nonempty 的 max_micros 远小于 100ms、不触发该 rule）。机械验收强化为「semantic-abnormal fixture 在默认阈值下触发 max_micros rule」+ 人工 review recorder 真造慢查询。

### D5 special-char-pw 对 metrics-only 验命令安全（非脱敏）+ leak 扫描须真覆盖

slowlog metrics-only 只输出整数，密码**本不回显 stdout**，脱敏门 trivially 通过 —— special-char-pw（`"p w*d"`）真实价值是验证含空格/glob 的密码经 REDISCLI_AUTH env 不破命令串/不 word-split（C2 类）。但 `_ALL_FIXTURES` 现**不 glob** `slowlog_*.json`，slowlog fixture 逃过 leak 扫描 → 须加 `+ sorted((_FIXTURES/"redis").glob("slowlog_*.json"))` 否则该门对 slowlog vacuous。`_RECORDED_SECRET_VALUES` **复用** `"p w*d"`（与 persistence 同值），避免新值未被扫描的反 vacuous 风险。

### D6 spec MODIFY：祖父条款全文复制 + 三需求 4 处点名收窄

MODIFY 三需求（标题不变、无 RENAMED）：祖父条款正文+场景、secret 需求 :33 注、无分叉需求 :97 注里 slowlog 点名收窄为仅 postgres.bloat_tables；祖父条款新增场景「redis.slowlog 迁移后受契约管辖」。MODIFY delta 全文复制（中文标题，archive rebuild 前临时副本预演）。**本次 review 已机械 diff 确认 delta 与 baseline 仅差 slowlog 点名收窄 + 新增场景，其余规范正文字节相同。**

## Risks / Trade-offs

- **[用户既有 `REDIS_PASSWORD` 失效]** → BREAKING；新增回归测试锁「旧 env→requires_unmet 诚实 skip」；迁移文档提示改名
- **[重录漏 fixture / 命令串不匹配]** → ReplayTarget `misses != []` 机械红
- **[semantic-abnormal 用健康态低阈值冒充]** → max_micros rule 判别轨（D4）+ recorder 脚本人工 review 双防
- **[`-t 5` 改变挂起超时语义]** → 对正常路径透明，仅故障路径更快诚实失败；与同域模板一致
- **[9 处冻结结构漏改某处]** → 各有对应断言（count_frozen / glob / probe meta-guard），漏改即测试红，非静默；tasks 逐条钉死

## Migration Plan

manifest secret/collector(+`-t 5`) + 重录 5 fixture + 9 处 crosscheck 结构 + 4 个新测试 + spec 注记，无 schema 迁移。回滚 = revert 单 squash commit。**面向用户 BREAKING env 名变更**，CHANGELOG / 迁移文档须载明 `REDIS_PASSWORD` → `HOSTLENS_REDIS_PASSWORD`。

## Open Questions

- ~~crosscheck 是否对「单 client 调用」有硬断言~~ —— **已查实结论**：crosscheck **无**单调用断言（secret-not-in-argv 用子串存在性、不计调用次数；output-shape 查 schema 字段；timeout 查 token 存在）。slowlog 的 SLOWLOG LEN + EVAL 两调用结构**不触发**任何 crosscheck 红。真正会咬的是 D2 的缺 `-t` 连接超时（已纳入修复），不是「双调用」。
- `postgres.bloat_tables` 迁移作为 sibling follow-up，本提案后祖父条款留它。
