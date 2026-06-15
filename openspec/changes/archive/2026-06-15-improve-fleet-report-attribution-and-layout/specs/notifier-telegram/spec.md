这两项改动（fleet 主机归因 + 段顺序）都落在既有「Telegram 报告渲染必须采用结构化布局」**同一条需求**内（其 `根因分析置顶` bullet 与 `多 target / 单主机退化` bullet）。因此用**重命名 + 修改**既有需求表达，避免新增需求与既有需求并存矛盾（archive 后规范不再自相冲突）。

## 重命名需求

- FROM: ### 需求:Telegram 报告渲染必须采用结构化布局（抬头 / 覆盖 / 根因优先 / 去重排序 / 来源 / 健康态）
- TO: ### 需求:Telegram 报告渲染必须采用结构化布局（抬头 / 覆盖 / 发现优先 / 去重排序 / 来源 / 健康态 / fleet 主机归因）

## 修改需求

### 需求:Telegram 报告渲染必须采用结构化布局（抬头 / 覆盖 / 发现优先 / 去重排序 / 来源 / 健康态 / fleet 主机归因）

Telegram 模板渲染的报告**必须**:

- **抬头**:`{severity 图标} *Hostlens 巡检 · {target_name} · {中文 severity}*`,**禁止**把 `report.intent`（整段巡检意图）当标题。
- **覆盖行**:含时间 + `{ok}/{total} 项检查 · {skipped} 项跳过 · {failed} 项失败`(从 `meta.inspectors_used` 算:`ok` 计入 `ok`、`requires_unmet` 计入 `skipped`、`timeout` / `target_unreachable` / `exception` 计入 **`failed`**)。**计数不变量** `ok + skipped + failed == total`——覆盖 `InspectorStatus` 五值闭集,**禁止**把 `failed` 折进 `skipped` 或漏计任何状态(否则运维误以为本次 schedule 完整完成、看不到真实失败)。`{failed}` 子句**仅在 `failed > 0` 时渲染**(为 0 省略,不引入「· 0 项失败」噪声)。
- **段顺序「发现优先」**(本提案改动):段顺序**必须**为 抬头 → 覆盖行 → **发现** → **根因分析**。即「发现」段**必须**渲染在「根因分析」段**之前**(运维先看客观事实、再看 LLM 推测)。抬头 / 覆盖行 的位置与内容**不变**。**健康态(`✅ 未发现异常`)是「发现」段的空态替代,占据「发现」段的位置,不是 `根因分析` 之后的独立尾段**——故无 finding 但有 `hypotheses` 时,顺序为 抬头 → 覆盖行 → 健康态 → 根因分析(健康态在根因之前)。
- **发现**:findings **必须去重**——去重键为 **`(target_name, inspector_name, message, severity)` 四元组,全字段相等才合并为一条**;**禁止**仅以 `(inspector_name, message)` 为键去重(否则会把同 message 不同 severity / 不同 target 的独立发现误并)。去重后 findings **按 severity 降序排**(critical → warning → info)、每条**带来源** `inspector_name`。
- **根因分析**(渲染在「发现」之后):有 `hypotheses` 时渲染 `根因分析` 段 —— 每条 `description`（带中文「置信度」）+ 其 `suggested_actions` 逐条以 `↳` 列出。
- **健康态**:无 findings 时渲染 `✅ 未发现异常`,**禁止**渲染空的「发现」段。
- **多 target / fleet 主机归因**(本提案改动):findings **必须按 `finding.target_name` 分组分节**渲染(每节主机名 + 该主机 severity + 该主机 findings)。该字段由 `report-data-model` 的 add-only `Finding.target_name` 给出(fleet 路径 `from_fleet_results` 为每条 finding 盖上其来源 `target_name`)。**fleet vs agent 信号**:用 `report.meta.target_type == "fleet"`——该值由 `from_fleet_results` 设置;`ReportMeta.target_type` 模型层是无约束 `str`(非 Literal),故「`== "fleet"` ⟺ fleet 报告」是**当前调用约定**(经勘察今日所有 `from_inspector_results` 调用方的 `target_type` 取自 target 运行时 `.type` ——`Literal["local","ssh","docker","k8s"]`;demo 的 `ReplayTarget` 冒充其中之一、运行时 `target_type` **绝不**为字面 `replay`——均不为 `fleet`),并由 guard 测试守护(见 design「诚实边界(信号强度)」)。该信号自描述、零模型改动、不耦合 id 编码。**退化判据(纯渲染层自持)**:
  - **fleet 报告**(`meta.target_type == "fleet"`):只要存在 **≥1 个 non-None `target_name`**,**必须**按主机分节——**即使去重后 distinct non-None target 只有 1 个**(单台出 finding 的 fleet 报告也要标出是哪台,这是本提案修复的核心缺陷)。**禁止**塌成无主机平铺。
  - **fleet 报告但 `distinct(non-None target_name) == 0`**(无任何 finding 带主机名——`from_fleet_results` 必盖值,仅退化/测试构造才出现):维持无分节平铺(无主机可标注),**禁止**渲染孤立的「未标注主机」节头。
  - **agent 单机报告**(非 fleet):维持既有退化——当 **`distinct(non-None target_name) ≤ 1`** 时**必须无分节渲染**(抬头已含机器名,per-host 标注冗余)。
  - 即(**渲染无主机分节、无 per-host 节头**的充要条件):`distinct(non-None target_name) == 0`(无主机可标注,fleet/agent 皆然)**或** `distinct(non-None target_name) == 1 且非 fleet`(agent 单机退化);其余情况——即 **`distinct ≥ 2`(无论 fleet 与否)或 `fleet 且 distinct ≥ 1`**——**必须**按主机分节。**禁止**用「全相同或全 None」这种判据。(实现层面:`group_by_target` 的显式塌平分支取 `distinct ≤ 1 且非 fleet`;all-None fleet 经具名路径返回退化的单 `(None, …)` 节、由模板的「节数 > 1 才渲未标注主机头」守卫抹平为无头平铺——见 design。)

既有 MarkdownV2 转义、`validate_config`、`sendMessage` 发送、宿主机本地时区渲染需求**不变**。

#### 场景:抬头不是 intent、且带覆盖行
- **当** 渲染一个 `intent` 为长句、severity=critical 的报告
- **那么** 第一行**必须**是 `🔴 *Hostlens 巡检 · <target> · 严重*` 类抬头,**禁止**出现整句 intent 当标题;**必须**有 `N/M 项检查` 覆盖行

#### 场景:覆盖行计入失败状态（不只 ok + skipped）
- **当** report 的 `meta.inspectors_used` 含 5 个 `ok` / 1 个 `requires_unmet` / 2 个 `timeout`（或 `exception` / `target_unreachable`）共 8 项
- **那么** 覆盖行**必须**渲染 `5/8 项检查 · 1 项跳过 · 2 项失败`,**禁止**渲染 `5/8 项检查 · 1 项跳过`（把 2 个失败漏计、误导运维以为 schedule 完整完成）;**必须**满足 `ok + skipped + failed == total`（5+1+2==8）

#### 场景:无失败时省略 failed 子句
- **当** report 的 `meta.inspectors_used` 全部 `ok` 与 `requires_unmet`、无任何 `timeout` / `target_unreachable` / `exception`
- **那么** 覆盖行**禁止**渲染 `· 0 项失败`（`failed > 0` 才渲染该子句）

#### 场景:findings 去重 + 排序 + 带来源
- **当** report 含 2 条 `(target_name, inspector_name, message, severity)` 四元组**完全相同**的 finding，及一条更低 severity 的 finding
- **那么** 四元组相同的两条**必须**只渲一次;critical **必须**排在 warning 之前;每条**必须**带 `inspector_name` 来源

#### 场景:同 message 不同 severity 不去重
- **当** report 含两条 `inspector_name` 与 `message` 相同、但 `severity` 不同的 finding(如同一检查项 critical 与 warning 各一)
- **那么** 两条**必须各自保留**(不合并)——去重键含 `severity`,严格全字段相等才合并

#### 场景:发现段在根因段之上（本提案改动）
- **当** 一个有 finding 且有根因假设(`hypotheses` 非空)的报告被渲染
- **那么** `发现` 段(`*发现*`)**必须**出现在 `根因分析` 段(`*根因分析*`)**之前**;`根因分析` 段(含每条 `description` 与 `↳ suggested_actions`)仍正常渲染,只是位置移到发现之后

#### 场景:单台 finding 的 fleet 报告仍按主机标注（本提案核心修复）
- **当** 一个 fleet 报告(`meta.target_type == "fleet"`,如覆盖 6 台的夜间巡检)去重后只有 1 台(如 `tg-bot`)带 finding(其余 target 无 finding)
- **那么** 渲染**必须**含该主机分节头(severity 图标 + 主机名 + 中文 severity,如 `🔴 *tg\-bot · 严重*`),**禁止**塌成无主机平铺的 `🔴 1-min load 2.45 …`(让运维看不出是哪台);即 fleet 报告即使 `distinct(non-None target_name) == 1` 也分节
  - **注意 MarkdownV2 转义**:主机名经 `mdv2_escape`,保留字符(如连字符 `-`)会被转义——`tg-bot` 渲染为 `tg\-bot`,故 Telegram body **不含**字面 `tg-bot`。测试断言**禁止**直接 `"tg-bot" in body`;应断言转义形 `tg\-bot`、或用无特殊字符的主机名(如 `hostA`,见既有 `test_telegram_multi_target_sections`)、或键于 `· 严重*` 节头形态。(飞书 Lark 经 `tojson` 不做 MarkdownV2 转义,卡片含字面 `tg-bot`——见 lark spec 同名场景。)

#### 场景:多 target fleet 报告按主机分节（回归）
- **当** report 的 findings 含两个不同 `target_name`(fleet 路径盖值)
- **那么** **必须**按 `target_name` 分主机节渲染,每节含主机名 + 该主机 severity + 该主机 findings

#### 场景:去重 × 分节组合（跨主机同 finding 不合并、主机内重复合并）
- **当** report 含三条 finding:hostA 与 hostB 各一条 `inspector_name` / `message` / `severity` **相同但 `target_name` 不同**的 finding(跨主机同问题),外加 hostA 内一条与其首条**四元组完全相同**(含 `target_name=hostA`)的重复
- **那么** 去重以 **`(target_name, inspector_name, message, severity)` 四元组**为键、**先于**分节执行:hostA 的两条重复**合并为一条**(四元组全等);hostA 与 hostB 的「同问题」**各自保留**(`target_name` 不同 → 四元组不等 → 不跨主机合并);最终 hostA 节 1 条、hostB 节 1 条,**禁止**因 message 相同把跨主机两条误并成一节一条(否则 fleet 报告会丢主机维度)

#### 场景:agent 单机报告退化为无分节（回归锚，非 fleet）
- **当** 一个 agent 单机报告(经 `from_inspector_results`,`meta.target_type != "fleet"`)的 finding `target_name` 去重后非 None 值至多一个（含全 `None`、全同值、或部分盖值部分 None 的混合）
- **那么** **禁止**渲染主机分节,**必须**与既有单 target 行为一致(无分节噪声、抬头已含机器名)

#### 场景:健康态不渲空发现段
- **当** report 无 findings
- **那么** **必须**渲染 `✅ 未发现异常` + 覆盖行,**禁止**渲染空的「发现」段
