## 上下文

#93（`extend-redact-flag-form-secrets`，已合并）把 `redact_text` 扩为引号感知，并为 `_KEYWORD_ASSIGN` 修了引号含空格 value 泄露。Security Engineer 复审残留后判：8 个 best-effort 残留无一必修、shell-parser follow-up ROI 为负，但点出两个零风险、与 #93 同源的可选收尾——本变更只做这两个。

## 目标 / 非目标

**目标：**
- #7：`_BEARER_HEADER` 对齐 #93 的 `_KEYWORD_ASSIGN` 引号感知修法，消掉 pre-existing 不一致。
- #4：补全 `_WRAPPER_VALUE_OPTS` 常用带值选项，缩小「未表内选项」残留。

**非目标：**
- 不引入 shell parser（ROI 负）；不动其它正则；不改 mask 强度分级；不追求清零残留。

## 决策

### 决策 1：#7 复用 `_SHELL_WORD` 同构片段，不新写正则

`_BEARER_HEADER` 的 value 从 `(\S+)` 换为与 `_SHELL_WORD` / `_KEYWORD_ASSIGN`（#93）**逐字节相同**的引号感知片段，sub 从 `_mask` 改 `_mask_glued_value`。**复用而非新写**的理由：该片段在 #93 经 8 轮 review + ReDoS 审计（可选闭合 alt 消重叠、无回溯）、且 `_mask_glued_value` 对无引号无空格值 == `_mask`（0/20000 fuzz）已验证。因此 #7 是「把已验证的修法套到第三个 value site」——零新风险面。

**替代（否决）**：给 Bearer 单独写一个简化正则——徒增不一致 + 新 ReDoS 审计负担。

### 决策 2：#4 是 data-only，不动 [B] 契约

`_WRAPPER_VALUE_OPTS` 是 best-effort 表（spec [B] 用「如…」举例、且显式列了「未表内选项」为 accepted 残留）。补项 = 加字典 entry，**不改 [B] 需求契约**，故 spec 只 MODIFY R1（为 #7），不动 [B]。补的选项限**已知带值**的（如 `sudo -C/-T`、`docker exec --workdir/--env-file`、`ssh -F/-J`、`env -u`），跳过方向安全侧（漏脱非 corruption），不引入误脱风险。

## 风险 / 权衡

- **#7 无空格 Bearer 输出变化** → `_mask_glued_value == _mask`（无引号无空格），逐字节不变；回归测试 + 既有 `test_redact.py` Bearer 用例锁定。
- **#7 与既有 4 类 R1「不动」语义** → MODIFY R1（RENAME 标题 + 把 Bearer 从「不动」移到「引号感知扩展」），JWT/sk 仍不动。归档前在 temp 副本实测 `openspec-cn archive`（RENAME + MODIFY 同标题易踩坑）。
- **#4 补项误吞** → 只补已知带值选项；负例测试锁定 `echo mysql -psec` head 不误判、`mysql -p mydatabase` 不动。

## 迁移计划

无数据迁移、无 API 破坏。spec 是 RENAME + MODIFY R1，归档前 temp 副本干跑 `openspec-cn archive` 确认 rebuild 过（中文标题、场景 4-井号、RENAME FROM/TO 标题与既有逐字匹配）。

## 待解决问题

无。
