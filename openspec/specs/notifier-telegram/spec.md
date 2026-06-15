# notifier-telegram 规范

## 目的

定义 Telegram 通知适配器契约——渲染 MarkdownV2 并正确转义、经 Bot API `sendMessage` 发送且 token 不入日志。
## 需求
### 需求:Telegram 适配器必须渲染 MarkdownV2 并正确转义

`TelegramNotifier`（`name == "telegram"`）必须从 `notifiers/templates/telegram/` 加载 Jinja2 模板，渲染 Telegram **MarkdownV2** 文本。模板环境必须注册 `mdv2_escape` filter，对 MarkdownV2 官方保留字符 `_ * [ ] ( ) ~ \` > # + - = | { } . !` 转义；并必须**同时转义内容中的字面反斜杠 `\`**（MarkdownV2 中 `\` 是转义引导符，内容里的字面 `\` 不转义会吞掉后一字符），使报告中任意内容（target 名、finding message、根因摘要）不会破坏 MarkdownV2 结构或被解释为格式标记。`validate_config` 必须要求 `bot_token` 与 `chat_id` 两个字段存在**且非空**（空串视为缺失）。

#### 场景:含保留字符的内容被转义

- **当** 渲染一个 `message` 含 `_`、`*`、`.` 等 MarkdownV2 保留字符的 finding
- **那么** 渲染产物中这些字符必须被反斜杠转义；产物作为 MarkdownV2 发送不会触发解析错误

#### 场景:缺失配置字段 fail-loud

- **当** `validate_config({"bot_token": "x"})`（缺 `chat_id`）
- **那么** 必须 raise（配置不完整），禁止静默通过

### 需求:Telegram 适配器必须经 Bot API `sendMessage` 发送且 token 不入日志

`send` 必须 POST 到 `https://api.telegram.org/bot<token>/sendMessage`，body 含 `chat_id` / `text` / `parse_mode="MarkdownV2"`。HTTP 必须用 `httpx.AsyncClient`。日志与 `NotifyResult.detail` 中 `bot_token` 必须打码（禁止明文出现）。**成功判定必须基于响应 body 的 `ok` 字段，不仅是 HTTP 状态**：Telegram 会以 HTTP 200 + `{"ok": false, "error_code": ..., "description": ...}` 表达业务失败（如 chat not found）——此情形必须记 `failed`（error 取脱敏后的 `description`），**禁止**误记 `sent`。仅当 HTTP 2xx **且** body `ok == true` **且** 含 `result.message_id` 时记 `sent` 并把 `str(message_id)` 存入 `detail`；HTTP 2xx 但 body 非合法 JSON / 缺 `result.message_id` 视为响应异常记 `failed`（不崩溃）。非 2xx HTTP 按 notifier-protocol 的有界重试/失败语义处理。

#### 场景:成功发送记录 message_id

- **当** Bot API 返回 200 + `{"ok": true, "result": {"message_id": 42}}`
- **那么** `NotifyResult.status == "sent"` 且 `detail` 含 `message_id`（字符串化）；日志中不出现明文 bot_token

#### 场景:HTTP 200 但业务失败记 failed 而非 sent

- **当** Bot API 返回 HTTP 200 + `{"ok": false, "error_code": 400, "description": "chat not found"}`
- **那么** `NotifyResult.status == "failed"`（**非** `sent`），`error` 含脱敏后的失败原因；不崩溃

### 需求:Telegram 报告渲染必须采用结构化布局（抬头 / 覆盖 / 根因优先 / 去重排序 / 来源 / 健康态）

Telegram 模板渲染的报告**必须**:

- **抬头**:`{severity 图标} *Hostlens 巡检 · {target_name} · {中文 severity}*`,**禁止**把 `report.intent`（整段巡检意图）当标题。
- **覆盖行**:含时间 + `{ok}/{total} 项检查 · {skipped} 项跳过 · {failed} 项失败`(从 `meta.inspectors_used` 算:`ok` 计入 `ok`、`requires_unmet` 计入 `skipped`、`timeout` / `target_unreachable` / `exception` 计入 **`failed`**)。**计数不变量** `ok + skipped + failed == total`——覆盖 `InspectorStatus` 五值闭集,**禁止**把 `failed` 折进 `skipped` 或漏计任何状态(否则运维误以为本次 schedule 完整完成、看不到真实失败)。`{failed}` 子句**仅在 `failed > 0` 时渲染**(为 0 省略,不引入「· 0 项失败」噪声)。
- **根因分析置顶**(在「发现」之前):有 `hypotheses` 时渲染 `根因分析` 段 —— 每条 `description`（带中文「置信度」）+ 其 `suggested_actions` 逐条以 `↳` 列出。
- **发现**:findings **必须去重**——去重键为 **`(target_name, inspector_name, message, severity)` 四元组,全字段相等才合并为一条**;**禁止**仅以 `(inspector_name, message)` 为键去重(否则会把同 message 不同 severity / 不同 target 的独立发现误并)。去重后 findings **按 severity 降序排**(critical → warning → info)、每条**带来源** `inspector_name`。
- **健康态**:无 findings 时渲染 `✅ 未发现异常`,**禁止**渲染空的「发现」段。
- **多 target**:**按 `finding.target_name` 分组分节**渲染(每节主机名 + 该主机 severity + 该主机 findings)。该字段由提案 B(`report-data-model` MODIFY)提供的 add-only `Finding.target_name` 给出——**本能力的多 target 分节渲染显式依赖提案 B 落地**。**退化判据(纯渲染层自持,对 B 的盖值策略零耦合)**:当 **`distinct(non-None target_name) ≤ 1`**（即去重后非 None 的来源 target 至多一个——把 `None` 与单一值视作同一主机)时**必须无分节渲染**(与既有单 target 行为一致、不引入分节噪声)。**禁止**用「全相同或全 None」这种判据——B 单 target 路径若对部分 finding 盖值、部分留 None,「全相同或全 None」两分支都不满足会误判多 target。

既有 MarkdownV2 转义、`validate_config`、`sendMessage` 发送需求**不变**。

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

#### 场景:多 target 按主机分节
- **当** report 的 findings 含两个不同 `target_name`(由提案 B 的 add-only `Finding.target_name` 提供)
- **那么** **必须**按 `target_name` 分主机节渲染,每节含主机名 + 该主机 severity + 该主机 findings

#### 场景:去重 × 分节组合（跨主机同 finding 不合并、主机内重复合并）
- **当** report 含三条 finding:hostA 与 hostB 各一条 `inspector_name` / `message` / `severity` **相同但 `target_name` 不同**的 finding(跨主机同问题),外加 hostA 内一条与其首条**四元组完全相同**(含 `target_name=hostA`)的重复
- **那么** 去重以 **`(target_name, inspector_name, message, severity)` 四元组**为键、**先于**分节执行:hostA 的两条重复**合并为一条**(四元组全等);hostA 与 hostB 的「同问题」**各自保留**(`target_name` 不同 → 四元组不等 → 不跨主机合并);最终 hostA 节 1 条、hostB 节 1 条,**禁止**因 message 相同把跨主机两条误并成一节一条(否则 fleet 报告会丢主机维度)

#### 场景:单主机退化为无分节（distinct non-None ≤ 1）
- **当** report 的 finding `target_name` 去重后非 None 值至多一个（含全 `None`、全同值、或部分盖值部分 None 的混合）
- **那么** **禁止**渲染主机分节,**必须**与既有单 target 行为一致(无分节噪声)

#### 场景:根因置顶
- **当** report 有 `hypotheses`
- **那么** `根因分析`（含 `suggested_actions`）**必须**渲染在「发现」之前

#### 场景:健康态不渲空发现段
- **当** report 无 findings
- **那么** **必须**渲染 `✅ 未发现异常` + 覆盖行,**禁止**渲染空的「发现」段

### 需求:Telegram 报告渲染的时间必须用宿主机本地时区

Telegram 报告抬头/覆盖行的时间（经共享 `fmt_time` 过滤器渲染 `report.meta.timestamp`）**必须**显示为运行 hostlens 的**宿主机系统本地时区**钟点,**禁止**直接打印 UTC 钟点。

- 报告时间戳**存储**仍是 UTC-aware（不变）;只在**渲染**时 `value.astimezone()` 转系统本地 TZ 后格式化。
- `fmt_time` 收到 **naive** datetime 时**必须先按 UTC 解释**（报告时间语义恒为 UTC）再转本地,避免被当本地时间不偏移。
- 既有覆盖行/抬头布局（计数、severity 图标、分节等）**不变**,只改时间钟点的时区。

#### 场景:报告时间显示宿主机本地时区而非 UTC
- **当** `report.meta.timestamp` 是 UTC `2026-06-15T08:55:00+00:00`,且宿主机/渲染进程本地 TZ 为 `Asia/Shanghai (+0800)`,渲染 Telegram 报告
- **那么** 抬头时间**必须**渲染为本地钟点 `2026-06-15 16:55`,**禁止**渲染 UTC 的 `2026-06-15 08:55`
