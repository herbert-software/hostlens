## 新增需求

### 需求:飞书 Lark 报告卡片必须采用与 Telegram 同构的结构化布局

Lark 交互卡片**必须**以与 Telegram **同构**的信息结构渲染（卡片 JSON 形态）:

- **抬头**:severity 配色的标题区,`Hostlens 巡检 · {target_name} · {中文 severity}`,**禁止**用 `report.intent` 当标题。
- **覆盖行**:`{ok}/{total} 项检查 · {skipped} 项跳过` + 时间。
- **根因分析置顶**:有 `hypotheses` 时放在「发现」之前,含每条 `description` 与其 `suggested_actions`。
- **发现**:**去重** + **按 severity 排序** + 每条**带来源** `inspector_name`。
- **健康态**:无 findings 时为「✅ 未发现异常」卡片(不渲空发现区)。
- **多 target**:多主机 findings **按主机分节**。

既有 HMAC-SHA256 时间戳签名、`validate_config`、发送需求**不变**。

#### 场景:卡片与 Telegram 同构
- **当** 渲染同一份报告到 Lark
- **那么** 卡片**必须**含抬头(非 intent)/ 覆盖行 / 根因(置顶)/ 去重排序带来源的发现,信息结构与 Telegram 一致

#### 场景:健康态卡片
- **当** report 无 findings
- **那么** **必须**渲染「✅ 未发现异常」健康态卡片,**禁止**渲染空的发现区
