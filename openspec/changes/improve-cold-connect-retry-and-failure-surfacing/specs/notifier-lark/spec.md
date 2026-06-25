## 修改需求

### 需求:飞书 Lark 报告卡片必须采用与 Telegram 同构的结构化布局

Lark 交互卡片**必须**以与 Telegram **同构**的信息结构渲染（卡片 JSON 形态）:

- **抬头**:severity 配色的标题区,`Hostlens 巡检 · {target_name} · {中文 severity}`,**禁止**用 `report.intent` 当标题。
- **覆盖行**:`{ok}/{total} 项检查 · {skipped} 项跳过 · {failed} 项失败` + 时间（计数规则与 Telegram 一致:`ok`→ok、`requires_unmet`→skipped、`timeout`/`target_unreachable`/`exception`→failed;不变量 `ok + skipped + failed == total`;`{failed}` 仅 `failed > 0` 时渲染）。
- **失败检查**(本提案新增,与 Telegram 同构):当 `failed > 0` 时**必须**渲染「失败检查」节,筛 `report.inspector_results` 里 `status ∈ _FAILED_STATUSES`(`{timeout, target_unreachable, exception}`,复用既有 frozenset,**不**用负谓词)的项,**按 `(target_name, 原因 label)` 分组**:每组主机名 + **中文原因 label** + 该组失败 inspector 名单。**原因 label 先 key on `status`**(5 值闭枚举):`timeout`→执行超时 / `target_unreachable`→不可达 / `exception`→采集异常;**仅 `status == "target_unreachable"`**(此时 `error == TargetError.kind`)以 `error` 细化:`ssh_connect_timeout`→连接超时 / `ssh_auth_failed`→认证失败 / `ssh_connect_failed`→连接失败 / `ssh_connection_lost`→连接中断,**未知 kind 回退桶级中文「不可达」**。**禁止以 `error` 为 `timeout`/`exception` 的键**(其 `error` 是自由文本、模型强制非空,以它为键会渲染生英文 + 把同主机多个 `exception` 拆成 N 组破坏「合一组」)。`failed == 0` 时**禁止**渲染该节。消费**已脱敏**报告(`_redact_inspector_result` 保留 `status`/`target_name`/`error`),零模型字段改动。**该节 meta-independent**：数据源 `Report.inspector_results`(`min_length=1`,恒在、与 `report.meta` 无关),故 `meta is None`(legacy schema-1.0)时覆盖行省略但失败检查节仍渲染。**`error` 脱敏稳定不变量**:`target_unreachable` 的 `error == TargetError.kind`(枚举串如 `ssh_connect_timeout`,`redact_text` 对其为 no-op,字节不变),故 `_FAIL_KIND_LABELS` 在已脱敏报告上查表命中——label 细化依赖此不变量(kind 串恒无 secret 模式)。飞书 `tojson` 序列化、**不做 MarkdownV2 转义**,主机名/label 按字面渲染。走与 Telegram **同一份** `_filters` 过滤器。
- **段顺序「失败检查 + 发现优先」**:卡片元素顺序**必须**为 抬头 → 覆盖行 → **失败检查** → **发现** → **根因分析**。即「失败检查」节(仅 `failed > 0`)**必须**排在「发现」之前,「发现」段元素**必须**排在「根因分析」段元素**之前**(与 Telegram 同序)。**逗号归属规则(具体化,防悬空)**:**抬头是 `card.header`、是 `card.elements` 数组的兄弟键、不在数组内**,故 `elements` 的首个数组元素**绝不能**带 leading 逗号。规则与既有「覆盖行」一致——**可选前置块各自 owns trailing 逗号、恒在的发现/健康态首元素无 leading 逗号**:① 覆盖行(`{% if report.meta %}`)渲染时末尾留 trailing 逗号(**不变**);② **「失败检查」节(`{% if failed_checks %}`)镜像覆盖行:无 leading 逗号 + 末尾自带 trailing 逗号**(它恒被发现/健康态接续,故 trailing 逗号必有后继元素);③ 发现/健康态首元素**无 leading 逗号**(依赖前一可选块的 trailing 逗号)。各排列即合法:`meta-present∧failed>0` = `[{覆盖,},{失败,},{发现}]`;`meta-None∧failed>0` = `[{失败,},{发现}]`(失败检查作首元素、无 leading);`failed==0` = `[{覆盖,},{发现}]`(失败检查整块省略,不留逗号);`meta-None∧failed==0` = `[{发现}]`。**禁止**给失败检查节加 leading 逗号(会在 meta-None 首元素位产生 `[,{` 、在 meta-present 与覆盖行 trailing 撞成 `},,{`)。**必须**用 `json.loads` 验证四种排列(见下方场景)皆合法。**健康态(`✅ 未发现异常`)是「发现」段的空态替代,占据「发现」位置**——故无 finding 但有失败检查时,顺序为 抬头 → 覆盖行 → 失败检查 → 健康态(→ 根因分析,若有 hypotheses)。
- **发现**:**去重**(去重键为 `(target_name, inspector_name, message, severity)` **四元组全字段相等才合并**,**禁止**仅 `(inspector_name, message)`——否则误并同 message 不同 severity / target 的独立发现)+ **按 severity 排序** + 每条**带来源** `inspector_name`。
- **根因分析**(渲染在「发现」之后):有 `hypotheses` 时放在「发现」之后,含每条 `description` 与其 `suggested_actions`。
- **健康态**:无 findings 时为「✅ 未发现异常」卡片(不渲空发现区)。
- **多 target / fleet 主机归因**:**按 `finding.target_name` 分组分节**(字段由 `report-data-model` 的 add-only `Finding.target_name` 提供,fleet 路径盖值)。**fleet vs agent 信号**与 Telegram 一致:`report.meta.target_type == "fleet"`(由 `from_fleet_results` 设置;`target_type` 模型层为无约束 `str`,故这是当前调用约定 + guard 测试守护,见 design「诚实边界」)。**退化判据(渲染层自持,与 Telegram 逐字同构)**:
  - **fleet 报告**(`meta.target_type == "fleet"`):只要存在 **≥1 个 non-None `target_name`**,**必须**按主机分节——**即使 distinct non-None target 只有 1 个**(单台出 finding 的 fleet 卡片也要标出主机)。**禁止**塌成无主机平铺。
  - **fleet 报告但 `distinct(non-None target_name) == 0`**(无任何 finding 带主机名,仅退化/测试构造才出现):维持无分节平铺,**禁止**渲染孤立的「未标注主机」节头。
  - **agent 单机报告**(非 fleet):`distinct(non-None target_name) ≤ 1` **必须无分节**(与既有单 target 一致)。
  - 即(**渲染无主机分节、无 per-host 节头**的充要条件,与 Telegram 同构):`distinct(non-None target_name) == 0`**或** `distinct(non-None target_name) == 1 且非 fleet`;其余——即 **`distinct ≥ 2`(无论 fleet 与否)或 `fleet 且 distinct ≥ 1`**——**必须**按主机分节。**禁**用「全相同或全 None」。(实现:`group_by_target` 显式塌平分支 `distinct ≤ 1 且非 fleet`;all-None fleet 经具名路径退化为单 `(None,…)` 节、由「节数 > 1 才渲未标注主机头」守卫抹平。)

既有 HMAC-SHA256 时间戳签名、`validate_config`、发送、宿主机本地时区渲染需求**不变**。

#### 场景:卡片与 Telegram 同构（发现优先）
- **当** 渲染同一份报告到 Lark
- **那么** 卡片**必须**含抬头(非 intent)/ 覆盖行 / 去重排序带来源的发现 / 根因分析,信息结构与 Telegram 一致;且**段顺序「发现」在「根因分析」之前**(与 Telegram 同序);去重键与多 target 分组逻辑与 Telegram 一致(`(target_name, inspector_name, message, severity)` 四元组去重、按 `finding.target_name` 分节、fleet 信号取 `meta.target_type`)

#### 场景:有失败检查时卡片按目标与原因分组列出（reason 先 key on status，本提案新增）
- **当** 一个 fleet 卡片的 `inspector_results` 含:`cloudcone` 的 3 个 inspector `status=target_unreachable` / `error="ssh_connect_timeout"`;`vultr` 的 2 个 `status=exception`、`error` 各为 `"parse_failed: …"` 与 `"output_schema_mismatch: …"`(自由文本互不同)
- **那么** 卡片**必须**含「失败检查」节:`cloudcone · 连接超时`(经 kind 细化)+ 其 3 个 inspector,`vultr · 采集异常`(**key on status**,不读自由文本 `error`)+ 其 **2 个** inspector **合一组**(尽管 error 文本不同);主机名/label 按字面(飞书不转义);卡片仍 `json.loads` 可解析。**禁止** `vultr · parse_failed: …` 生英文、**禁止**把 vultr 拆两组

#### 场景:无失败时不渲染失败检查节且卡片合法（本提案新增）
- **当** report 的 `inspector_results` 全部 `ok` 与 `requires_unmet`(`failed == 0`)
- **那么** 卡片**禁止**含「失败检查」节(无节头、无空节);条件省略**禁止**引入悬空/双逗号,卡片仍 `json.loads` 可解析

#### 场景:失败检查节的逗号排列组合均合法 JSON（本提案新增）
- **当** 分别渲染四种形态:① `failed>0` ∧ 无 finding(健康态);② `failed>0` ∧ **有 finding**(失败检查节与发现节相邻);③ `failed>0` ∧ `meta is None`(覆盖行省略、失败检查节紧随抬头);④ `failed==0`(节省略)
- **那么** 四种产物**必须**均 `json.loads` 可解析的合法 interactive 卡片,**无**悬空/双逗号(失败检查节**无 leading 逗号、自带 trailing 逗号**镜像覆盖行,发现/健康态首元素无 leading 逗号,各排列拼接合法)

#### 场景:同 message 不同 severity 不去重
- **当** 卡片渲染含两条 `inspector_name` 与 `message` 相同、`severity` 不同的 finding
- **那么** 两条**必须各自保留**(去重键含 `severity`,不合并)

#### 场景:发现段在根因段之上
- **当** 一个有 finding 且有根因假设(`hypotheses` 非空)的报告渲染飞书卡片
- **那么** `发现` 段元素(`**发现**`)**必须**排在 `根因分析` 段元素(`**根因分析**`)**之前**;卡片整体仍是 `json.loads` 可解析的合法 interactive 卡片

#### 场景:单台 finding 的 fleet 卡片仍按主机标注
- **当** 一个 fleet 卡片(`meta.target_type == "fleet"`,覆盖 ≥2 target)去重后只有 1 台有 finding
- **那么** 卡片**必须**含该主机分节头(`**tg-bot · 严重**` 类),**禁止**渲染成无主机平铺;卡片仍是合法 JSON。(飞书 Lark 经 `tojson` 序列化、**不做 MarkdownV2 转义**,故主机名按字面渲染——`tg-bot` 即字面 `tg-bot`,与 Telegram 的 `tg\-bot` 转义形不同;测试断言据此分通道写。)

#### 场景:去重 × 分节组合（跨主机同 finding 不合并、主机内重复合并）
- **当** 卡片渲染一份含三条 finding 的 report:hostA 与 hostB 各一条 `inspector_name` / `message` / `severity` **相同但 `target_name` 不同**的 finding,外加 hostA 内一条与其首条**四元组完全相同**的重复
- **那么** 去重以 **`(target_name, inspector_name, message, severity)` 四元组**为键、**先于**分节执行:hostA 两条重复**合并为一条**;hostA 与 hostB 的同问题**各自保留**(`target_name` 不同 → 不跨主机合并);hostA 节 1 条、hostB 节 1 条,**禁止**因 message 相同把跨主机两条误并(与 Telegram 同序)

#### 场景:agent 单机卡片退化为无分节（回归锚，非 fleet）
- **当** 一个 agent 单机报告(`meta.target_type != "fleet"`)的 finding `target_name` 去重后非 None 值至多一个（含全 None、全同值、或混合盖值/None）
- **那么** 卡片**禁止**渲染主机分节,**必须**与既有单 target 行为一致

#### 场景:健康态卡片
- **当** report 无 findings
- **那么** **必须**渲染「✅ 未发现异常」健康态卡片,**禁止**渲染空的发现区

#### 场景:无 finding 但有根因假设时卡片仍是合法 JSON（调序逗号盲点）
- **当** report `findings == []` 但 `hypotheses != []`（健康态卡片仍带根因段;两字段独立、无 cross-validator）渲染飞书卡片——此形态下「发现优先」调序后 `根因分析` 块成为 `elements` 末元素
- **那么** 卡片**必须**是 `json.loads` 可解析的合法 interactive 卡片（手工前导/尾随逗号拼接**禁止**出现悬空/双逗号）,且含 `根因分析` 段、渲染「✅ 未发现异常」、**无**`发现` 段
