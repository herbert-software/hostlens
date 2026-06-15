> 真机报告反馈②③：fleet 报告主机归因 + 报告段顺序。经代码勘察精化（决策 1 已定为 `meta.target_type` 信号、零模型改动；spec delta 已落为对既有「结构化布局」需求的重命名 + 修改）。

## 为什么

真机 ts.mac-mini 全队巡检暴露两个报告**渲染**问题（与诊断质量无关，那是 `ground-diagnostician-failure-analysis`）：

**② fleet 报告丢主机归因**：6 台 fleet 报告里只有 1 台（tg-bot）出 finding 时，Telegram/飞书报告显示 `🔴 1-min load 2.45 ...` 却**不说是哪台**。抬头写的是全部 6 台（`aliyun-bj,…,vultr`），运维看不出 tg-bot 才是问题机。根因在 `notifiers/_filters.py:group_by_target`：当 `distinct(non-None target_name) <= 1` 时**故意塌成无主机标签的平铺**（`[(None, findings)]`）——这是为 **agent 单机报告**设计的（抬头已有机器名、不必再标），但对**多 target fleet 报告**就错了：单台出问题反而丢了归因。数据本身没问题（finding 带 `target_name='tg-bot'`，由 `from_fleet_results` 盖值），是分组渲染逻辑的设计缺陷。

**③ 布局顺序**：报告现在「根因分析 → 发现」。运维更想先看**发现**（客观事实）、再看**根因分析**（LLM 推测）。希望调成「发现 → 根因」。

## 变更内容

1. **fleet 报告永远按主机标注**：fleet 报告（`report.meta.target_type == "fleet"`，由 `from_fleet_results` 设置——见 design 决策 1）即便去重后只 1 台有 finding，也按主机分节标注；**agent 单机报告保持平铺**（`from_inspector_results`，抬头已含机器名）。改 `group_by_target` 加 `fleet: bool` 参数（**显式塌平分支** ⟺ `distinct(non-None) ≤ 1` **且非 fleet**；fleet 报告 distinct≥1 分节、all-None fleet 经具名路径退化为无主机平铺——精确充要条件见 design/spec），telegram/lark 模板按 `meta.target_type` 传参。**不碰 report 数据模型**（决策 1 选 `meta.target_type` 而非新 flag）。

2. **布局调序**：telegram `report.md.j2` + lark `report.card.j2` 把「根因分析」与「发现」对调成「发现 → 根因」。`render_markdown.py` 经核对**已经是**「发现 → 根因假设」顺序，**无需改动**。

## 非目标（Non-Goals）

- **不改 agent 单机报告的平铺渲染**（抬头已有机器名，per-host 标注冗余）。
- **不改 `report-data-model` / 报告数据模型**（决策 1 复用既有 `meta.target_type`，不加新字段；finding 的 `target_name` 已存在，本提案只改渲染消费）。
- **不改诊断质量**（误判/幻觉/severity 是 `ground-diagnostician-failure-analysis` 的范围）。
- **不改 `render_markdown.py`（`reports show`）的 fleet 主机归因**：该面 `_render_findings` 仅按 severity 分组、从不渲 per-finding `target_name`，是预先存在的独立缺口；本提案聚焦真机反馈直接命中的**通知推送通道**，render_markdown 的 fleet 归因留独立 follow-up。
- **不动飞书 HMAC 签名 / 时间渲染**等无关渲染面。

## 影响

- **契约**：`notifier-telegram` / `notifier-lark` 渲染 spec —— 对既有「结构化布局」需求做**重命名 + 修改**（段顺序「发现优先」+ fleet 主机归因退化判据），**不新增并存矛盾的需求**。
- **代码**：`notifiers/_filters.py`（`group_by_target` 加 `fleet` 参数）、`notifiers/templates/{telegram/report.md.j2, lark/report.card.j2}`（段顺序 + 分组传参）。**不碰** `reporting/models.py` / `render_markdown.py`。
- **测试**：`tests/notifiers/test_report_layout.py` —— 翻转 3 个既有断言（根因/发现序 ×2、单台 fleet 分节 ×2 见 design 表），新增「单台 finding 的 fleet 报告仍标主机」「发现在根因之上」锚点（TZ 无关）；agent 单机 + 多台 fleet 回归锚不变。
