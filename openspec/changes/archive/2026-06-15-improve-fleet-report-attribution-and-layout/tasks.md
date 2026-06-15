> 精化后的可执行任务（决策 1 已定为 `meta.target_type` 信号，不碰数据模型）。

## 1. fleet 信号（决策 1 已定：`meta.target_type == "fleet"`）

- [x] 1.1 确认 `Report.from_fleet_results` 置 `meta.target_type="fleet"`、`from_inspector_results` 不置 fleet（已勘察确认，全仓唯一来源 `models.py:586`）——**无代码改动**，仅作为实现前置事实。
- [x] ~~1.2 改 `reporting/models.py` 加 fleet 标记~~ **取消**：决策 1 选 `meta.target_type`，不碰 `report-data-model` 契约。

## 2. group_by_target fleet-aware

- [x] 2.1 `notifiers/_filters.py`：`group_by_target(findings, *, fleet: bool = False)`；**显式塌平分支**（早返回 `[(None, findings)]`）取 `distinct(non-None target_name) ≤ 1` **且 `not fleet`**；fleet 且 distinct≥1 走具名路径 → 分节；**all-None fleet**（distinct==0 ∧ fleet）经具名路径退化为单 `(None,…)` 节（渲染同为无主机平铺，由模板「节数>1 才渲未标注主机头」守卫抹平）。更新 docstring：**函数 docstring**（退化判据加 fleet 条件）**与模块级 docstring（现 20-27 行也复述了无 fleet 条件的塌平规则）**一并改,顺手把过期的 change-dir 引用（`improve-report-rendering-and-i18n`）改为指向 `openspec/specs/notifier-{telegram,lark}/spec.md`（指主规范而非 change-dir,免本 change 归档后引用再次失效;`test_report_layout.py:3` 同改）。
- [x] 2.2 telegram `report.md.j2` + lark `report.card.j2`：`group_by_target` 调用传 `fleet=(report.meta is not none and report.meta.target_type == "fleet")`。
- [x] 2.3 测试：
  - 新增「单台 finding 的 **fleet** 报告含主机节头」（telegram + lark，TZ 无关）。**断言注意转义不对称**：telegram 主机名经 `mdv2_escape`（`tg-bot` → `tg\-bot`，body 不含字面 `tg-bot`）——断言写转义形 / 用无特殊字符主机名（如 `hostA`，仿 `test_telegram_multi_target_sections`）/ 键于 `· 严重*` 节头；lark 经 `tojson` 不转义，断言可写字面 `tg-bot`。**lark 节头断言用子串 `in joined`**（节头内容带 sev_icon 前缀如 `🟡 **only-host · 警告**`，非整串相等；仿 `test_lark_multi_target_sections` 的 `... in joined`）。
  - **新增 all-None fleet 退化锚**（钉住 round-2 新增的「禁止孤立未标注主机节头」条款，否则该规范条款无测试守护）：`_fleet_report` 经 `model_copy` 把全部 finding `target_name` 改 `None`（`distinct(non-None)==0`、`fleet=True`）→ 断言 findings 仍渲染、**无**「未标注主机 /（未标注主机）」节头、lark 卡片 `json.loads` 合法；telegram + lark。
  - **翻转** `test_telegram_single_host_degrade_when_one_named_target`（fleet 单台现**必须**分节；断言实际节头形 `*{host} · {sev}*`——host=`only-host`、warning → `*only\-host · 警告*`，**禁止**裸 `*only-host*`，或改 fixture 用无保留字主机名）；**翻转并改名** `test_lark_single_host_no_sections`（实为 `_fleet_report`，改名如 `test_lark_single_host_fleet_sections`，断言 fleet 单台**必须**含节头 `**only-host · 警告**`，**禁止**裸 `**only-host**`）。
  - **新增 Lark agent-path 无分节锚**（补回因上条翻转而消失的锚）：`_single_report(...)`（非 fleet）卡片**禁止**含 `**<host>**` 主机节头。该锚经 `_lark_card`/`_lark_contents`（内含 `json.loads`）断言,故**同时透传性地守住卡片 JSON 合法性**;agent + findings + hypotheses 的调序后逗号形态另由翻转后的 `test_lark_root_cause_before_findings`（`_single_report` 带 findings+hypotheses,亦走 `_lark_card`）覆盖。
  - 回归锚 `test_telegram_single_host_no_sections`（agent 路径）**保持**塌平；多台 fleet 分节 / 去重×分节 / None-section 末尾序 回归不变。
  - **新增 fleet 信号 guard 测试**（钉住决策 1 的调用约定，零模型改动）：断言 agent 路径产物 `meta.target_type != "fleet"`——覆盖 `from_inspector_results` 各生产调用方 / `_single_report`，防止未来误传 `"fleet"` 让渲染层错判。

## 3. 布局调序「发现 → 根因」

- [x] 3.1 telegram `report.md.j2`：把「根因分析」块（现 13-22 行）移到「发现」块（现 23-42 行）之后；**同步更新该文件首部注释块的 `Structure:` 行**（现写 `根因分析(置顶) → 发现`）为「发现 → 根因分析（发现优先）」，避免注释与调序后行为矛盾。
- [x] 3.2 lark `report.card.j2`：同构调整 `elements` 顺序；妥善安排第 58 行无条件 `{"tag":"hr"}`（放发现与根因之间）；保持手工逗号拼接的 JSON 合法（无悬空/双逗号）；**同步更新该文件首部注释块**（现写 `根因分析(置顶)`）为「发现优先」。
- [x] ~~3.3 改 `render_markdown.py` 段顺序~~ **核对已一致**：`render()` 已 `_render_findings`(发现) → `_render_hypotheses`(根因假设)，**no-op，不改**。
- [x] 3.4 测试：
  - **翻转** `test_telegram_root_cause_before_findings` / `test_lark_root_cause_before_findings`（改断言为 发现 index < 根因 index，并相应改测试名）；根因段（含 `↳ suggested_actions`、置信度中文 label）仍正常渲染只是位置在后。
  - **新增 Lark JSON 合法性盲点锚**：`findings == [] 且 hypotheses != []`（健康态卡片仍带根因段，调序后根因块成 `elements` 末元素——逗号最易出错处）→ 断言 `json.loads` 可解析、含 `根因分析`、无 `发现` 段。
  - **收紧** `test_telegram_single_host_no_sections` 断言：现 `"*" not in body.split("*发现*",1)[1]` 因调序后尾段会接根因段而与 hypotheses 存在性耦合；改为只在「发现」节内断言无 per-host 节头（如无 `· 严重*` 类节头形态），与 hypotheses 解耦。
  - **更新测试文件头 docstring**：`tests/notifiers/test_report_layout.py:3-6` 仍引用旧 change-dir（`improve-report-rendering-and-i18n`）且枚举写「根因置顶」；本 change 既已编辑该文件,顺手把 change-dir 引用更到本 change、「根因置顶」措辞改为「发现优先」。

## 4. 收尾

- [x] 4.1 全量 `pytest` + `mypy --strict` + `ruff` 绿；notifier layout 测试是**断言式**（非 `.ambr` snapshot），无 snapshot 重生成；确认 lark JSON 合法性测试（`test_lark_renders_valid_json_for_every_scenario` + `_lark_card` 内 `json.loads`）守住调序后卡片。
- [x] 4.2 `openspec-cn validate --strict` + temp 副本 archive dry-run（核对重命名+修改不致 archive 中止、merged spec 无并存矛盾；Lark 用纯「修改需求」无重命名——其 MODIFY 标题与既有 spec 标题逐字相同 `飞书 Lark 报告卡片必须采用与 Telegram 同构的结构化布局`,不会被当孤立 rename）；真机 ts.mac-mini trigger 验证单台 finding 标主机 + 发现在上。
