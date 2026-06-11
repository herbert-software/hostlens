## 重命名需求

- FROM: ### 需求:`redact_text` 既有 4 类规则的 mask 强度不变，仅 key=value 值匹配扩展为引号感知
- TO: ### 需求:`redact_text` 既有 4 类规则的 mask 强度不变，key=value 与 Bearer 值匹配扩展为引号感知

## 修改需求

### 需求:`redact_text` 既有 4 类规则的 mask 强度不变，key=value 与 Bearer 值匹配扩展为引号感知

本提案在 `hostlens.core.redact.redact_text(s: str) -> str` 上**叠加**新规则。既有 4 类（`key=value`/`key:value` 赋值、`Bearer <tok>`、JWT `eyJ...`、`sk-...`）的 mask 强度/输出格式**必须**不变：长于 8 字符的值 `<前 4>...<后 4>`，≤8 字符 `****`；mask **强度分级**（凭据类改全 `****`）**禁止**纳入本提案范围（会破坏 `report-data-model` 既有 scenario，留 follow-up）。

**引号感知 value 匹配扩展**适用于 `key=value`（`_KEYWORD_ASSIGN`）**与 `Bearer`（`_BEARER_HEADER`）两条规则**：value 从裸 `(\S+)` 扩展为与 [B] 同构的引号感知 shell-word 片段 `((?:[^\s"']+|"(?:\\.|[^"\\])*"?|'[^']*'?)+)`，并改用 `_mask_glued_value` 包装。理由：裸 `(\S+)` 对引号含空格值 `password="a b"` / `Bearer "a b"` 在引号内空格截断、只脱 `"a` 漏 `b"`（与 flag 形同类泄露）。**无空格值 mask 输出逐字节不变**（`_mask_glued_value` 对无引号无空格值 == `_mask`，故 `report-data-model` scenario、既有 test、真实 `Bearer eyJ...` token 全保持）；引号含空格值现整体脱敏。JWT（`_JWT`）与 sk-（`_SK_KEY`）**禁止**改动（无 value-截断结构、不适用）。

#### 场景:既有 key=value 无空格值输出格式不变
- **当** 输入 `password=verylongsecretvalue`
- **那么** 输出含 `password=` 与 `...`（前 4 后 4 形式，与本提案前一致）

#### 场景:key=value 引号含空格值整体脱敏不漏尾
- **当** 输入 `password="abc def"`
- **那么** 输出不含漏脱的 ` def"` 尾段（不发生裸 `(\S+)` 在引号内空格截断致 `password=**** def"`）；值整体脱敏（`password=****`）

#### 场景:既有 Bearer 无空格 token 输出不变
- **当** 输入 `Authorization: Bearer eyJabcdefghijklmnopqrstuvwxyz`
- **那么** 输出保留字面 `Bearer ` 与前 4 后 4 形式（与本提案前一致，真实 base64url token 无空格、不受影响）

#### 场景:Bearer 引号含空格值整体脱敏不漏中段
- **当** 输入 `Authorization: Bearer "a b c"`
- **那么** 值作**单 token** 脱敏——不发生裸 `(\S+)` 只脱 `"a`、漏 ` b c"` 全段的截断；短值（≤8）输出 `Bearer ****`（长值则前 4 后 4，中段被遮），字面 `Bearer ` 保留

#### 场景:既有 sk- 输出格式不变
- **当** 输入 `sk-abcdefghijklmnopqrstuvwxyz1234567890`
- **那么** 输出保留前缀 `sk-a` 与后缀 `7890`、含 `...`
