## 新增需求

### 需求:飞书 Lark 报告卡片必须采用与 Telegram 同构的结构化布局

Lark 交互卡片**必须**以与 Telegram **同构**的信息结构渲染（卡片 JSON 形态）:

- **抬头**:severity 配色的标题区,`Hostlens 巡检 · {target_name} · {中文 severity}`,**禁止**用 `report.intent` 当标题。
- **覆盖行**:`{ok}/{total} 项检查 · {skipped} 项跳过` + 时间。
- **根因分析置顶**:有 `hypotheses` 时放在「发现」之前,含每条 `description` 与其 `suggested_actions`。
- **发现**:**去重**(去重键为 `(target_name, inspector_name, message, severity)` **四元组全字段相等才合并**,**禁止**仅 `(inspector_name, message)`——否则误并同 message 不同 severity / target 的独立发现)+ **按 severity 排序** + 每条**带来源** `inspector_name`。
- **健康态**:无 findings 时为「✅ 未发现异常」卡片(不渲空发现区)。
- **多 target**:**按 `finding.target_name` 分组分节**(字段由提案 B 的 add-only `Finding.target_name` 提供,**多 target 分节显式依赖提案 B 落地**)。**退化**:单主机时(所有 finding `target_name` 相同或全 `None`)**必须无分节**(与既有单 target 行为一致)。

既有 HMAC-SHA256 时间戳签名、`validate_config`、发送需求**不变**。

#### 场景:卡片与 Telegram 同构
- **当** 渲染同一份报告到 Lark
- **那么** 卡片**必须**含抬头(非 intent)/ 覆盖行 / 根因(置顶)/ 去重排序带来源的发现,信息结构与 Telegram 一致;去重键与多 target 分组逻辑与 Telegram 一致(`(target_name, inspector_name, message, severity)` 四元组去重、按 `finding.target_name` 分节)

#### 场景:同 message 不同 severity 不去重
- **当** 卡片渲染含两条 `inspector_name` 与 `message` 相同、`severity` 不同的 finding
- **那么** 两条**必须各自保留**(去重键含 `severity`,不合并)

#### 场景:单主机退化为无分节
- **当** report 所有 finding 的 `target_name` 相同(或全为 `None`)
- **那么** 卡片**禁止**渲染主机分节,**必须**与既有单 target 行为一致

#### 场景:健康态卡片
- **当** report 无 findings
- **那么** **必须**渲染「✅ 未发现异常」健康态卡片,**禁止**渲染空的发现区
