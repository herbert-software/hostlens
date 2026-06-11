## 为什么

刚合并的 #93（`extend-redact-flag-form-secrets`）把 `redact_text` 扩到引号感知，但 Security Engineer 复审后指出两个**零风险、可选**的收尾点（判为「非高危、可选便宜收尾」，非必修）：

1. **#7 既有 `_BEARER_HEADER` 仍是裸 `\bBearer\s+(\S+)`** —— 引号含空格值 `Bearer "a b"` 在引号内空格截断、只脱 `"a` 漏 `b"`。这与 #93 已为 `_KEYWORD_ASSIGN` 修过的引号含空格值泄露**完全同类**；#93 当时明确把 Bearer 列为出范围（spec R1「Bearer/JWT/sk- 三类不动」）。本变更把 Bearer 也对齐——一行把 value 换成同构的引号感知 shell-word 片段 + `_mask_glued_value`，消掉这个 pre-existing 不一致。
2. **#4 `_WRAPPER_VALUE_OPTS` 取值选项表不全** —— `sudo -n`/`docker exec --user` 等已覆盖，但某些常用带值选项缺漏时，其值 token 会被当作命令头致该段不脱（spec 已列为「未表内取值选项」best-effort 残留）。补全常用工具的带值选项**缩小**该残留——纯加数据、零架构、零 ReDoS 风险。

威胁模型说明（沿用 #93）：redact 是 best-effort 非安全边界，root 拒绝门 + env 注入是纵深；这两个残留**现实危害低**（真实 Bearer token 无空格、未表选项罕见），本变更只为一致性 + 顺手缩残留，**不引入 shell parser**。

## 变更内容

- **#7 `_BEARER_HEADER` 引号感知**：value 从 `(\S+)` 换为与 `_SHELL_WORD`/`_KEYWORD_ASSIGN` 逐字节同构的引号感知片段 `((?:[^\s"']+|"(?:\\.|[^"\\])*"?|'[^']*'?)+)`，sub 从 `_mask` 改 `_mask_glued_value`。无空格值 mask 输出逐字节不变（与 `_KEYWORD_ASSIGN` 同理）；`Bearer "a b"` 现整体脱敏。
- **#4 补全 `_WRAPPER_VALUE_OPTS`**：给 `sudo`/`env`/`docker`/`ssh`/`nice`/`time` 补常用带值选项（如 `sudo -C/-T`、`docker exec --workdir/--env-file`（已有部分）、`ssh -F/-J`（已有）、`env -u`（已有）等缺漏项），缩小「未表内取值选项」残留。表是 best-effort、不求穷举。

## 功能 (Capabilities)

### 新增功能
（无）

### 修改功能
- `text-secret-redaction`: MODIFY R1（既有 4 类规则的引号感知扩展）—— 把 Bearer 从「不动」改为「与 key=value 同构的引号感知 value 匹配 + `_mask_glued_value`」（需求标题 RENAME：「仅 key=value」→「key=value 与 Bearer」）；JWT/sk- 仍不动；mask 强度仍不变。#4 wrapper 取值选项表扩充是 [B] 需求契约内的实现细节（spec 已列「未表内选项 best-effort」、表是举例非穷举），**不需** MODIFY [B] 契约，仅 code + 测试。

## 影响

- **代码**：`src/hostlens/core/redact.py`（`_BEARER_HEADER` value 引号感知 + sub 改 `_mask_glued_value`；`_WRAPPER_VALUE_OPTS` 补项）；`tests/core/test_redact.py`（补 Bearer 引号值 + 补充 wrapper 选项用例）。
- **spec delta**：`text-secret-redaction`（RENAME + MODIFY R1）。
- **文档**：`docs/OPERABILITY.md §7.2` 微调（Bearer 现覆盖引号值）。
- **依赖**：零新增。
- **下游契约**：无破坏。既有 Bearer 无空格 token 输出逐字节不变（如 `Authorization: Bearer eyJ...` 仍 `Bearer eyJh...`）；行为只增（引号含空格值现脱敏）。

## 非目标 (Non-Goals)

- **不**引入 shell parser（Security Engineer 评估 ROI 为负：新依赖 + 新 DoS 面 + 违反「值得多一个依赖」红线）。
- **不**动其它正则（`_KEYWORD_ASSIGN`/`_JWT`/`_SK_KEY`/URL/ENV/SHELL_WORD 均不改）。
- **不**改 mask 强度分级（凭据类全 `****` 仍留独立 follow-up）。
- **不**追求清零所有残留——其余 best-effort 残留（嵌套 sh -c / 散文位置 / 转义空格 / 前导未闭合引号吞后续 / exotic 引用）经评估接受，不在本变更。

## 对外契约影响

- **`text-secret-redaction`**：见修改功能（R1 RENAME + MODIFY）。
- **CLI / Inspector / Agent tool / MCP / Notifier / Schedule manifest**：均无变更。
- **`hostlens.core.redact` 公共 API**：`redact_text` 签名 / `__all__` 不变。

## Failure Modes

1. **Bearer 引号感知正则若引入回溯** → 用与 `_SHELL_WORD` **逐字节同构**的已验证片段（#93 经 8 轮 review + ReDoS 审计、可选闭合 alt 无重叠），不新写；单测含长转义引号 run < 2s。
2. **无空格 Bearer 值 mask 变化** → `_mask_glued_value` 对无引号无空格值 == `_mask`（#93 已 0/20000 fuzz 验证），输出逐字节不变；回归测试锁定。
3. **wrapper 选项补项误吞命令** → 补的是**已知带值选项**（跳选项 + 其值），跳过方向是安全侧（漏脱非 corruption）；负例测试锁定 `echo mysql -psec` 等 head 不误判。

## Operational Limits

- 纯本地正则 / 字典查找，无 IO、O(len(s))、无新 ReDoS（同构片段已硬化）。

## Security & Secrets

- 不引入新密钥、不扩攻击面。核心即安全收益：消掉 Bearer 引号值泄露 + 缩小 wrapper 选项残留。redact 仍 best-effort 非安全边界（root 门 + env 注入纵深不变）。

## Cost / Quota Impact

- 零 token / 零 API —— 纯本地工具函数。

## Demo Path

```bash
python -c "
from hostlens.core.redact import redact_text as r
print(r('Authorization: Bearer \"my token val\"'))   # #7 引号含空格 Bearer 整体脱敏
print(r('Authorization: Bearer eyJabc.def.ghi'))      # 无空格 Bearer 不变
print(r('sudo -C 3 mysql -psup3rsecret'))             # #4 sudo -C 带值选项穿透
"
```
预期：第 1 行 `Bearer ****`（不漏 `val`）、第 2 行与改前一致、第 3 行 `mysql -psup3...` 脱敏。`pytest tests/core/test_redact.py -q` 全绿。
