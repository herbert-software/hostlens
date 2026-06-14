## 新增需求

### 需求:Telegram 报告渲染必须采用结构化布局（抬头 / 覆盖 / 根因优先 / 去重排序 / 来源 / 健康态）

Telegram 模板渲染的报告**必须**:

- **抬头**:`{severity 图标} *Hostlens 巡检 · {target_name} · {中文 severity}*`,**禁止**把 `report.intent`（整段巡检意图）当标题。
- **覆盖行**:含时间 + `{ok}/{total} 项检查 · {skipped} 项跳过`(从 `meta.inspectors_used` 算,`requires_unmet` 状态计入 skipped)。
- **根因分析置顶**(在「发现」之前):有 `hypotheses` 时渲染 `根因分析` 段 —— 每条 `description`（带中文「置信度」）+ 其 `suggested_actions` 逐条以 `↳` 列出。
- **发现**:findings **必须去重**——去重键为 **`(target_name, inspector_name, message, severity)` 四元组,全字段相等才合并为一条**;**禁止**仅以 `(inspector_name, message)` 为键去重(否则会把同 message 不同 severity / 不同 target 的独立发现误并)。去重后 findings **按 severity 降序排**(critical → warning → info)、每条**带来源** `inspector_name`。
- **健康态**:无 findings 时渲染 `✅ 未发现异常`,**禁止**渲染空的「发现」段。
- **多 target**:**按 `finding.target_name` 分组分节**渲染(每节主机名 + 该主机 severity + 该主机 findings)。该字段由提案 B(`report-data-model` MODIFY)提供的 add-only `Finding.target_name` 给出——**本能力的多 target 分节渲染显式依赖提案 B 落地**。**退化**:report 为单主机时(所有 finding 的 `target_name` 相同、或全为 `None`)**必须无分节渲染**(与既有单 target 行为一致、不引入分节噪声)。

既有 MarkdownV2 转义、`validate_config`、`sendMessage` 发送需求**不变**。

#### 场景:抬头不是 intent、且带覆盖行
- **当** 渲染一个 `intent` 为长句、severity=critical 的报告
- **那么** 第一行**必须**是 `🔴 *Hostlens 巡检 · <target> · 严重*` 类抬头,**禁止**出现整句 intent 当标题;**必须**有 `N/M 项检查` 覆盖行

#### 场景:findings 去重 + 排序 + 带来源
- **当** report 含 2 条 `(target_name, inspector_name, message, severity)` 四元组**完全相同**的 finding，及一条更低 severity 的 finding
- **那么** 四元组相同的两条**必须**只渲一次;critical **必须**排在 warning 之前;每条**必须**带 `inspector_name` 来源

#### 场景:同 message 不同 severity 不去重
- **当** report 含两条 `inspector_name` 与 `message` 相同、但 `severity` 不同的 finding(如同一检查项 critical 与 warning 各一)
- **那么** 两条**必须各自保留**(不合并)——去重键含 `severity`,严格全字段相等才合并

#### 场景:多 target 按主机分节
- **当** report 的 findings 含两个不同 `target_name`(由提案 B 的 add-only `Finding.target_name` 提供)
- **那么** **必须**按 `target_name` 分主机节渲染,每节含主机名 + 该主机 severity + 该主机 findings

#### 场景:单主机退化为无分节
- **当** report 所有 finding 的 `target_name` 相同(或全为 `None`)
- **那么** **禁止**渲染主机分节,**必须**与既有单 target 行为一致(无分节噪声)

#### 场景:根因置顶
- **当** report 有 `hypotheses`
- **那么** `根因分析`（含 `suggested_actions`）**必须**渲染在「发现」之前

#### 场景:健康态不渲空发现段
- **当** report 无 findings
- **那么** **必须**渲染 `✅ 未发现异常` + 覆盖行,**禁止**渲染空的「发现」段
