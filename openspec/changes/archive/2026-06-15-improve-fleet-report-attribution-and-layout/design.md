> 经代码勘察（`reporting/models.py`、`notifiers/_filters.py`、两套模板、`render_markdown.py`、`tests/notifiers/test_report_layout.py`）后精化：决策 1 收敛、决策 2/3 落到具体改动点与回归面。

## 决策 1（已定）:「是否 fleet 报告」的信号 —— `report.meta.target_type == "fleet"`

`group_by_target` 现在只看 findings 自身、无 report 上下文,判不了「fleet 还是 agent 单机」。需要把信号传进来。

**勘察发现一个比骨架三候选都好的现成信号**:`Report.from_fleet_results` 已经把 `meta.target_type` 硬置为 `"fleet"`（`reporting/models.py:586`),而 agent 单机路径 `from_inspector_results` 的 `target_type` 取自 target 的**运行时 `.type`**——`Literal["local","ssh","docker","k8s"]`(demo 的 `ReplayTarget.type` 由 fixture `impersonate` 字段**冒充**其中之一、默认 `local`,见 `targets/replay.py:142` + `demo/assembly.py:121`;config 层判别值 `replay` **绝不**写入运行时 `meta.target_type`)。即**无任何 `from_inspector_results` 调用方产出 `target_type == "fleet"`**(也不产 `"replay"`)。模板已能访问 `report.meta`（现用 `report.meta.timestamp` / `report.meta.inspectors_used`）,且 `redact_report_for_render` 保留 `meta`、`_redact_meta` 不动 `target_type`,渲染期可读。

> 注:`ReportMeta` 的 docstring(`models.py:318-321`)把 `replay` 列为 canonical 值,是**过时/误导**(运行时 ReplayTarget 冒充实型、`target_type` 永不为字面 `replay`);该 docstring 是预存代码、修正它属本提案范围外(非目标:不碰数据模型),本提案文档以运行时实际为准。

> **诚实边界(信号强度)**:`ReportMeta.target_type` 是无约束 `str`(非 Literal),模型层**不硬约束** `"fleet"` 为工厂专属——「`target_type == "fleet"` ⟺ fleet 报告」是**当前调用约定**(经勘察今日所有调用方满足),不是模型不变量。本提案选 (d) 是为不碰数据模型(非目标);代价是若未来有 agent 路径调用方误传 `target_type="fleet"` 会被渲染层当 fleet。**缓解(在范围内)**:加一条 guard 测试——断言 agent 路径(`from_inspector_results` 各生产调用方 / `_single_report`)产出的 `meta.target_type != "fleet"`,把这条约定钉成回归。**不在范围**:把 `target_type` 收成 Literal 或加 validator(碰 `report-data-model`,非目标)。

候选对比:

| 候选 | 结论 |
|---|---|
| (a) `meta.target_id.startswith("fleet:")` | 可行,但耦合到 id 的 `fleet:` 哈希编码约定(易随存储键策略漂移)。**否** |
| (b) `meta.inspectors_used` distinct target > 1 | **误判单成员 fleet**(`targets:[x]` distinct==1 会被当 agent),恰好漏掉本提案要修的「单台」场景。**否** |
| (c) `reporting/models.py` 加 `is_fleet`/`target_count` flag | 最清晰但**碰 `report-data-model` 契约**(要补 spec delta + 改模型),非必要。**否** |
| **(d) `meta.target_type == "fleet"`** | 自描述、**零模型改动**、不耦合 id 编码、已被 `from_fleet_results` 设好、渲染期可读。**采用** |

> 选 (d) 后,骨架任务 1.2（改 `models.py` + 补 `report-data-model` spec delta）**取消**——本提案不碰 report 数据模型。

定了信号后,`group_by_target` 改为 fleet-aware（最小手术）:

```python
def group_by_target(findings, *, fleet: bool = False):
    distinct_named = {f.target_name for f in findings if f.target_name is not None}
    if len(distinct_named) <= 1 and not fleet:   # 仅多加 "and not fleet"
        return [(None, list(findings))]          # agent 单机:塌平(回归不变)
    # 既有具名分节路径(distinct>=1 都走这里:fleet 单台 也分节)
    ...
```

`group_by_target` 的**显式塌平分支**(`if len(distinct_named) <= 1 and not fleet`)返回 `[(None, findings)]`;此外 **all-None fleet**(distinct==0 ∧ fleet)经具名路径也退化返回单 `(None, findings)` 节——两者**渲染同为无主机平铺**(故「渲染无 per-host 节头」的充要条件是 `distinct==0` 或 `distinct==1 且非 fleet`,见 spec)。模板按信号传参:

```jinja
{% set sections = report.findings | dedup
   | group_by_target(fleet=(report.meta is not none and report.meta.target_type == "fleet")) %}
```

- `report.meta is None`（legacy schema-1.0）→ 非 fleet → 维持塌平,安全。
- 纯 `from_fleet_results` 路径:findings 逐条被盖上来源 `target_name`（`ir.target_name`,必非 None）,故 `distinct_named ≥ 1`,具名分节路径正常、`none_group` 为空、不触发「（未标注主机）」。**若测试经 `model_copy(update={"findings":[...]})` 注入 `target_name=None` 的 finding(既有 fleet 用例就这么构造)**,fleet=True 下仍走具名路径,该 None finding 落入既有「未标注主机」末尾节(回归不变)。
- agent 报告:`from_inspector_results` **不主动盖 `target_name`**——常规单 target 输入 finding 的 `target_name` 为 None(`distinct_named == 0`);若上游已预盖同一主机值则 `distinct_named == 1`(既有 `test_redact_m3_fields` 即如此)。两种都满足 `distinct ≤ 1` 且 `fleet=False` → 塌平。**逐字回归**。
- `fleet=False` 默认值保证:非 fleet 但 distinct≥2 的直接构造报告仍按既有「distinct>1 分节」走（`len<=1` 为假,与 fleet 标志无关）。

## 决策 2:布局调序「发现 → 根因」

**telegram `report.md.j2`**:现序 抬头 → 覆盖行 → **根因分析(13-22 行)** → **发现(23-42 行)**。调成 抬头 → 覆盖行 → **发现** → **根因分析**。纯模板块移动。注意「健康态(✅ 未发现异常)」是「发现」段的 `{% else %}` 空态(占据「发现」槽位),不是独立尾段——故 findings 为空且有 hypotheses 时,渲染序为 抬头 → 覆盖行 → 健康态 → 根因分析(健康态在根因之前)。

**lark `report.card.j2`**:同构调整 `elements` 顺序。**注意两个脆弱点**:① 第 58 行有一个**无条件 `{"tag":"hr"}`** 现夹在根因与发现之间,调序时其位置要重新安排(放在发现与根因之间仍合理);② 卡片用**手工前导逗号 `,{...}`** 拼 JSON,块移动易引入悬空/双逗号——naive 块互换会在 4 种 findings×hypotheses 组合下都坏(findings/健康态块与移后的 hr 之间缺逗号、且原用纯尾随逗号的 hypotheses 块变成末元素后多一个 `]` 前的尾逗号)。**正确拼法**:把根因块改成与发现段同样的「前导逗号 `,{...}`」风格(不再用尾随逗号),hr 留在发现与根因之间;由 `test_lark_renders_valid_json_for_every_scenario` + 新增「findings=[] ∧ hypotheses≠[]」锚 + 各用例经 `_lark_card`(内含 `json.loads`)守住合法性。

**`render_markdown.py`（`reports show` 的 markdown）**:勘察发现其顺序**已经是** `_render_summary`/`_render_findings`(219 行) → `_render_hypotheses`(220 行),即「发现」**已在**「根因假设」之上。**故布局调序对 `render_markdown` 是 no-op,无需改动**(骨架任务 3.3 收敛为「核对已一致、不改」)。**反向说明**:正因已是 发现→根因,把 `render_markdown` 纳入调序任务集反而是**范围外 churn**(去动一个已正确的文件),故有意不列入 3.1/3.2 的模板调序集。

**待确认 → 已确认**:覆盖行(时间+计数)留在抬头下不动;「✅ 未发现异常」健康态是「发现」段的空态替代,findings 为空时渲染在覆盖行之后;若同时 hypotheses 非空,则健康态之后继续渲染根因分析(健康态在根因之前),调序后位置仍合理。

## 决策 3（范围边界）

`section_severity` / `dedup` / `sort_sev` / `coverage_line` / `fmt_time` 等既有过滤器**不动**;只改 `group_by_target`(加 `fleet` 参数)与两套模板的「段顺序 + 分组传参」。**agent 单机报告的分节维度逐字不变**(仍平铺、无 per-host 节头;回归锚 `test_telegram_single_host_no_sections` 等)——但段顺序「发现优先」是对**所有**报告(含 agent)统一调整,故 agent 报告的发现/根因段序也随之翻转(`test_*_root_cause_before_findings` 即 agent 路径、随本提案翻转)。即「逐字不变」仅限**分节**维度,不含段顺序。

**`render_markdown` 的 fleet 主机归因不在本提案范围**:`reports show` 的 `_render_findings` 仅按 severity 分组、**从不**渲染 per-finding `target_name`,fleet 报告在该面也丢主机维度——但这是预先存在的独立缺口,本提案聚焦真机反馈直接命中的**通知推送通道(Telegram/飞书)**。列入非目标,留独立 follow-up。

## 测试方向（含既有测试的「翻转」清单）

改动会让既有 `test_report_layout.py` 中部分断言**从绿变红**,这些是**行为变更、必须随提案翻转**(不是回归):

| 既有测试 | 现断言 | 改后 |
|---|---|---|
| `test_telegram_root_cause_before_findings` / `test_lark_root_cause_before_findings` | 根因 index < 发现 index | **翻转**为 发现 index < 根因 index |
| `test_telegram_single_host_degrade_when_one_named_target`（单 target **fleet**,549 行） | `*only-host*` 不在 body | **翻转**为 fleet 单台**必须**含分节头。**断言写实际节头形 `*{host} · {sev}*`(连字符经 `mdv2_escape`)**:host=`only-host`、severity=warning → 断言 `*only\-host · 警告*` in body;**禁止** `*only-host*`(节头格式是 `*host · sev*`、且 `-` 被转义,该裸串永不出现)。或改 fixture 用无保留字主机名(如 `onlyhost`/`hostA`)再断言 `*hostA · 警告*`。 |
| `test_lark_single_host_no_sections`（单 target **fleet**,564 行,经 `_fleet_report`） | `**only-host**` 不在卡片 | **翻转**为 fleet 单台**必须**含分节头(并改测试名,如 `test_lark_single_host_fleet_sections`,标明 fleet 路径)。**断言写实际节头形 `**{host} · {sev}**`**:Lark 经 `tojson` 不转义 → 断言 `**only-host · 警告**`(字面);**禁止** `**only-host**`(节头格式是 `**host · sev**`,该裸串永不出现)。 |

> 注意:`test_lark_single_host_no_sections` 名虽含 "no_sections" 但**用的是 `_fleet_report`**(fleet 路径),翻转后它不再是「agent 无分节」锚——Lark 的 agent-path 无分节锚因此**消失**,必须靠下方新增锚补回(Lark 模板的 JSON/逗号/节头逻辑与 Telegram 不同,不能只靠 Telegram 的 agent 锚)。

**保持回归锚不变**(agent 路径,绝不翻):

- `test_telegram_single_host_no_sections`（538 行,经 `from_inspector_results`,无 hypotheses）→ 仍塌平、无分节。**实现时收紧其断言**:现 `"*" not in body.split("*发现*",1)[1]` 只因 fixture 无 hypotheses 才在调序后仍绿(调序后 发现 段之后会接 根因 段;若该 fixture 将来加 hypotheses,`*根因分析*` 会落入尾段误伤断言)——改为只在「发现」节内断言无 per-host 节头(如断言无 `· 严重*` 类节头形态),与 hypotheses 是否存在解耦。
- 多台 fleet 分节 / 去重×分节组合 / None-section 末尾序 等既有 fleet 多台用例 → 不变。

**新增锚**:

- 单台 finding 的 **fleet** 报告（覆盖 ≥2 target、仅 1 台有 finding）→ 渲染**含**该主机分节头;telegram + lark 两通道;TZ 无关。**断言注意 转义不对称**:telegram 经 `mdv2_escape` 转义主机名保留字(`tg-bot` → `tg\-bot`,body 不含字面 `tg-bot`),lark 经 `tojson` 不转义(字面 `tg-bot`)——telegram 断言写转义形 / 无特殊字符主机名 / 键于 `· 严重*` 节头,lark 断言可写字面。
- **all-None fleet 退化锚(新增,钉住 round-2 新增的禁止孤立节头条款)**:`_fleet_report` 经 `model_copy` 把所有 finding 的 `target_name` 改为 `None`(`distinct(non-None)==0`、`fleet=True`)→ 断言:findings 仍渲染、**无**「未标注主机 / （未标注主机）」节头、lark 卡片 `json.loads` 合法;telegram + lark。
- **Lark agent-path 无分节锚(新增,补回消失的锚)**:用 `_single_report(...)`（`from_inspector_results`,非 fleet）的卡片**禁止**含主机分节头(`**<host>**`)——与翻转后的 fleet 单台锚配对,守住 Lark 的 fleet/agent 二分。
- **Lark JSON 合法性盲点(新增)**:`findings == [] 且 hypotheses != []`（健康态卡片仍带根因段）的形态——`findings`/`hypotheses` 是独立字段(无 cross-validator),可经 `model_copy` 构造;调序后 根因 块变成 `elements` 末元素,是悬空/缺失逗号最易出现处,而现有 `json.loads` 守卫(`test_lark_renders_valid_json_for_every_scenario` 等)**不覆盖**此形态。新增断言:该形态卡片 `json.loads` 可解析、含 `根因分析`、无 `发现` 段。
- 布局:发现段在根因段**之上**(telegram body index + lark contents index;两通道)。
