> 经代码勘察 + SRE 拍板 + 一轮对抗 review。review 修正：①病 2 真凶是 `linux.system.load_avg`（非 `system.uptime`），fix 远小于原设计；②systemd boot 时刻不能用 gawk-only `systime()`，改 `/proc/uptime`；③severity 须加 uptime gate 区分「长跑机历史残留」与「刚重启后失败」。

## 勘察确认的 load-bearing 事实

- **Finding DSL `when`/`message` namespace = `{**output, **parameters, **window_context}`**（`runner.py`）。函数 `len/sum/min/max/any/all/now/float/int`（`dsl.py`）；**DSL 支持除法**（`linux.system.load_avg` 现就用 `float(load1)/int(ncpu)`）；`for_each: "<expr> as <var>"`，`when` 属性访问 `x.k`、`message` 下标 `{x[k]}`（`docker/containers_restart_loop.yaml`）。
- **finding-id = `sha256(name \x00 version \x00 message)[:16]`**（message + version 进指纹 → 改任一即 churn；severity 不进）。
- **报告头 severity = `max(finding.severity)`**（`routing.aggregate_severity`）→ inspector 侧降级直接修报告头。
- **`systime()` 是 gawk-only**（实测 macOS BSD awk `calling undefined function systime`；Debian 默认 mawk、Alpine busybox awk 均无）。systemd inspector `targets:[local,ssh]` 会命中这些 → **禁用 `systime()`/`strftime()`/`mktime()`**，boot/uptime 走 `/proc/uptime` + POSIX。
- **两个 load inspector**：`system.uptime`（`uptime` 命令、固定 4.0/8.0、load1-only、`_BACKLOG`）与 **`linux.system.load_avg`**（`/proc/loadavg`+`nproc`、per-core `load1/ncpu` crit2.0/warn1.0、`_BACKLOG`、被 `cpu_saturation` demo + `incidents/_scenarios.py` 使用）。**病 2 真凶 = `linux.system.load_avg`**（真机 `1-min load 2.45` 即其 `load1/ncpu>=2` 误触发）。`system.uptime` 不动。
- **os-shell 套件契约**：禁新基础设施（kv/json parse、for_each、parameters 均已存在，合规）；list 型 for_each 迭代键 ∈ `{results,items,records}`（systemd 用 `results`）。

## 决策 1（已定，review 修正 target）:load 改用持续信号门控（`linux.system.load_avg`）

现 findings 门控 `float(load1)/int(ncpu) >= 2.0`（critical）/ `>= 1.0`（warning）——**只看 load1 单次采样**，单核机一次进程突发即误标严重。**改为门控 `load5` 与 `load15` 的 per-core 比值**（持续信号），`load1` 不再决定 severity：

- `critical` 当 `float(load15)/int(ncpu) >= crit_per_core AND float(load5)/int(ncpu) >= crit_per_core`
- `warning` 当二者 `>= warn_per_core`（且未达 critical）
- 默认 `crit_per_core=2.0` / `warn_per_core=1.0`（沿用现值），**`parameters` 化**（numeric 免 pattern）。
- `load1` 保留 output（展示），**无任何 finding 读它**。

collect/output_schema 基本不变（已含 `load1/load5/load15/ncpu`，`parse: kv`）；仅 findings 的 `when`/`message` 变（message 改述 load5/15、英文不变 → 留 `_BACKLOG`、无 allowlist 改动）。version bump。→ tg-bot `load15/ncpu=0.33 < 1.0` → **零 finding**，病 2 修复。

> SRE 理由：`load1` 是 1 分钟 EMA，对单次进程风暴极敏感（cron/build/GC 都能顶过 2×核），单核机正常呼吸；要求 `load5 AND load15` 同时过阈 ≈「过载已持续 ≥5 分钟且仍在持续」，只有真卡住才达到。AND（非 OR）：load5 已回落但 load15 尾巴翘 = 正在恢复，不该 critical。

## 决策 2（已定）:severity 校准归属 = inspector 侧

报告头 = `max(finding.severity)` → inspector 侧把 cloud-init 残留 critical→warning 直接修报告头，不碰聚合层、不让诊断师改 finding（诊断师只读）。

## 决策 3（已定，review 修正 portability + fresh-reboot）:systemd 时间锚 + severity

**collect 一条 pipeline 取**：每失败单元 `Type` + 失败时刻 `InactiveEnterTimestampMonotonic`（开机以来微秒，免解析 wall-clock）+ **系统 `uptime_seconds`**（`read -r uptime_seconds _ < /proc/uptime`——**shell 内建、不引入 `cut` 等未声明 binary**，`requires_binaries` 维持 `[systemctl, awk]`；**纯 POSIX，不用 gawk `systime()`**）。每失败单元一次 `systemctl show -p Type -p InactiveEnterTimestampMonotonic`，**per-unit 迭代用 `while IFS= read -r unit`（非 `for ... in $(...)` 空白分词，保含空格/转义的单元名）**，保留现 awk JSON-escape（unit 名可含 `\`/`"`）+ **空集仍 emit 合法顶层对象** `{"uptime_seconds":N,"results":[]}`（`parse_json` 拒顶层数组）。fail-loud：`/proc/uptime` 读不到 → exit 1（`status=exception`，非假 ok）。message 模板用下标 `{u[unit]}`/`{u[type]}`（`str.format` 不支持属性式 `{u.unit}`；`u.type` 只在 `when` 用）。

**output_schema**：`required: [uptime_seconds, results]`；`uptime_seconds: number`；`results: [{unit:str, type:str, inactive_monotonic_us:int}]`（迭代键 `results`，套件契约）。

**severity 规则（`for_each: "results as u"`）**：
- 规则 1（**warning**）:`u.type == 'oneshot' AND u.inactive_monotonic_us > 0 AND u.inactive_monotonic_us <= boot_window_seconds*1000000 AND uptime_seconds >= min_uptime_seconds` → **长跑机的开机一次性历史残留**。
- 规则 2（**critical**）:上式取反 → 近期/常驻/刚重启后失败。
- **`uptime_seconds >= min_uptime_seconds` 门是 review 修正的关键**（fixes fresh-reboot）：`InactiveEnterTimestampMonotonic` 是 boot-relative，光「oneshot 在开机窗口内失败」不足以判历史——**刚重启 2 分钟的机器**上一个本次开机失败的 oneshot 也满足，那是「刚崩」该 critical。加 `uptime_seconds >= min_uptime_seconds`（默认 3600s=1h，病 1 机器 up 36 天远超）才把「长跑机的开机历史残留」与「刚重启后失败」分开。
- `inactive_monotonic_us > 0` 守卫：systemd 对从未 inactive 的单元报 0，排除退化、落 critical（保守不假阴）。
- 默认 `boot_window_seconds=180` / `min_uptime_seconds=3600`，`parameters` 化。降级目标 **warning 不是 info**（历史残留仍值得运维知道）。常驻服务（`zerotier-one` 等 `Type=notify/simple`）落规则 2 → critical（正确；「独立 vs 连锁」是诊断师叙事职责、非 severity）。

## 决策 4（已定）:诊断师 prompt 抗幻觉（覆盖病 1+病 2）

`diagnostician.md` 新增「## 根因推理纪律（抗过度归因）」5 条（**新 H2 小节、节内从 1 编号**，与既有「调度纪律」并列）：① 失败默认独立、勿无证据编连锁 ② 历史 vs 近期（结合 `Type`/失败时刻/`uptime_seconds`：高 uptime + oneshot + 开机窗口失败 = provisioning 残留、非刚崩）③ 相关≠因果 ④ 瞬时单样本（load1 高、load5/15 正常）不得编持续根因 ⑤ 置信度匹配证据。+ 反面→正确 few-shot（进 system prompt、走 prompt cache）。

**病 2 两层分工（review 澄清）**：inspector 层修复是**主**——tg-bot 病 2 下 `linux.system.load_avg` 产**零 finding**，诊断师根本拿不到 load 信号去乱猜（这才是正解）。prompt 层「瞬时单样本不编持续根因」是**兜底**——针对仍会冒出 load-ish finding 的其它面（多核机踩 warn 阈、或别的 inspector 的瞬时信号）。故病 2 的**确定性强锚在 inspector**（零 finding），prompt 否定锚是弱兜底、不绑 tg-bot 那个「无 finding」case。

## 决策 5（severity 校准归属确认 + 架构红线）

inspector 侧（决策 2）；§4.2 inspector 不调 LLM（A/B 仍纯确定性 DSL）；诊断师只读（C 仅加 prompt 文本）；DSL 能力边界内（除法/算术/for_each/属性访问全有先例）；无需 hook.py。

## 测试方向（确定性强锚 + LLM 弱锚 + 人工抽检）

| 锚 | 断言 | 强度 |
|---|---|---|
| inspector severity（主力） | 病 2:`load15/ncpu < warn` → `linux.system.load_avg` **零 finding**；持续过载双门 → critical。病 1:oneshot+开机窗口+高 uptime → `warning`（非 critical），仅历史残留时 `aggregate_severity`==warning；fresh-reboot（低 uptime）oneshot → critical | 强、逐字 |
| 诊断师叙述（**非机器逐字断言**） | `systemd_failed` 的 **authored Planner 叙述**（`_scenarios.py` `scenario.narrative`，**进 snapshot**）写「两条独立 critical、不编连锁」、不含「连锁/级联/雪崩」→ snapshot 锚;诊断师 `hypothesis` 字面（**不进 snapshot**、只供 cassette）authored 后靠 **grep/源码 review** 锚 + few-shot 进 prompt + 真机 Demo Path 人工抽检。诊断师 `confidence`/`supporting_findings` 现可按场景 author（`IncidentScenario.diag_confidence`/`diag_supporting`，默认 `high`/`("F1",)`）——`systemd_failed` 取 `medium` + 引 `F1`/`F2` 合规录入 cassette;但快照投影**不渲** hypotheses/confidence/supporting，故仍**不**机器断言其计数/置信度，靠 cassette 行为示范 + 源码 review + 人工抽检 | 弱、authored+人工 |
- inspector fixture 走 **D-7 os-shell 约定**（`_CaptureTarget` 编 `/proc/loadavg`+`nproc` / `systemctl list-units`+per-unit `show`+`/proc/uptime` stdout、frozen monotonic、命令串级锁；不 claim「fixture 锁正确性」、正确性靠命令串锁 + 真机 Demo Path）。

## 重跑影响点清单（review 细化到具体文件）

| 影响 | load（`linux.system.load_avg`） | systemd（`linux.systemd.failed_units`） | prompt |
|---|---|---|---|
| finding-id churn | 是（message 改 load1→load5/15+version） | 是（聚合→per-unit+version） | 无 |
| i18n crosscheck | `_BACKLOG` 成员、英文 message → **复核仍通过**（无 allowlist 改） | `_MIGRATED_ALLOWLIST` 唯一成员（静态断言、非 regen）：新 per-unit message **必须**保留 ≥1 CJK + 仅注入已声明字段（`u.*` 循环变量豁免）；旧 `{failed_names}` 注入随 `failed_names` 删除而消除 | 否 |
| 具体 pin 测试（**须手改断言、非 regen**） | `cpu_saturation` demo `cassette.jsonl`/`fixture.json` + **两处 snapshot**（`tests/demo/snapshots/cpu_saturation.md` 与 `tests/incidents/snapshots/cpu_saturation.md` 都 pin 该 message）;`tests/incidents/_scenarios.py` load 条目。（`test_health_default_set` 仅断言集合成员、不受影响。） | `test_systemd_failed_collector.py`（重写 collector 断言 `results`/Type/monotonic + **重建 JSON-escape 回归**）;`systemd_failed` incident 三处:① `demo/scenarios/systemd_failed/fixture.json` 的 **collector 命令串 + stdout** 须重录（replay 按命令串键控、命令变了就 miss）② `tests/incidents/snapshots/systemd_failed.md` 随 **per-unit findings 块**（新 message + severity）+ **authored Planner 叙述**重生成（snapshot 只渲 Planner 叙述+findings+tokens，**不渲 hypothesis/confidence/supporting**——故 snapshot 不受 confidence 改动影响、保持不变）;`cassette.jsonl` 因 finding 内容变 + confidence/supporting 改动重录（`confidence:high→medium`、`supporting_findings:["F1"]→["F1","F2"]`，见 ③）③ `_scenarios.py` systemd 条目：`main_stdout` 改新 `{"uptime_seconds":N,"results":[…]}` 形（authored stdout 须满足 oneshot+开机窗口+高 uptime 以真触发病 1 修复）+ `scenario.narrative`/`scenario.hypothesis` 字面改「两条独立 critical、不编连锁」+ `diag_confidence="medium"`/`diag_supporting=("F1","F2")`（`IncidentScenario` 新增这两个 author 字段、默认 `high`/`("F1",)` 保其余 7 场景不变;独立信号无机制证据按纪律 5 不得 `high`、hypothesis 讨论 nginx+mysql 须引 `F1`/`F2`） | **诊断师单测 cassette key 不含 system prompt**（`test_diagnostician_agent.py`），故**纯 prompt 改动不需重录诊断师单测 cassette**;需重录的是 **incident cassette**——因 finding 内容变（per-unit message 取代 `failed_names`、在 cassette 的 `messages` 键内），非 prompt |
| 不受影响（核对） | `system.uptime` 的 `TestSystemUptime` **不动**（未 retarget）；cohort_guard 计数 72、doctor builtin-count（version-only） | 同左 | — |

**实现顺序**：先 A+B（inspector，确定性可单测、确认 churn 范围）→ 再 C（prompt few-shot 引用「inspector 降级后的 warning」、须 A/B 先落地）。**incident cassette 因 finding 内容变化而重录（与 prompt 改动无关）须在 A/B 落地后做**；诊断师单测 cassette 不受 prompt 改动影响（key 不含 system prompt）。
