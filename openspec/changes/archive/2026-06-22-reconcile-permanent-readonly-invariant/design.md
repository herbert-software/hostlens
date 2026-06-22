## 上下文

M9 已落地三条永久不变量（见 proposal §为什么），但 M2 时期写下的「临时性」措辞从未被回收，散落在 4 个 active spec、6 处源码、6 个测试、`docs/ARCHITECTURE.md`。归档的 `add-remediation-plan-schema` 提案 §非目标 显式把这批债 defer 到「随 P1b/P2 提案走 spec 流程」，但 P1b/P2 未做，成了孤儿。本变更是收口提案。

约束：`reason` 值 `approval_flow_not_supported_in_m2` 被 3 个 active spec 用精确字符串匹配钉死（agent / mcp tool-adapter 场景 + tool-registry 的 `ToolPolicyReason` Literal），所以这是 **spec-pinned 契约改动**（必须走 spec 流程同步那 3 个 spec，而非直接改代码）。**注意区分两层「契约」**：spec-pinned（需同步 spec）≠ 外部 API BREAKING——见 §风险，该值在 MCP 错误文本里理论可观测但无真实客户端触发路径，故**非外部 API 破坏**。

## 目标 / 非目标

**目标：**
- 把「M2 临时墙 / M9 会替换 / M9 加 FILE_WRITE」措辞升格为「永久只读 / 永久 noop / M9 不新增写类 Capability」。
- `ToolPolicyReason` 值去 `_in_m2` 后缀，spec / 源码 / 测试三方同步。
- `docs/ARCHITECTURE.md` 的写类 ToolSpec 示例换成与「Agent 表面永久只读」一致的只读示例。

**非目标：**
- 不放开 Agent / MCP 写表面（这正是要钉死的不变量）。
- 不动 archived 提案/spec 里的历史 `_in_m2`（历史快照）。
- 不重命名 `test_quota_check_returns_none_in_m2`（quota 确实 M2 返回 None、M10.5 完善，是独立事项）。
- 不新增 Capability / ToolSpec / surface，无运行时行为变更。
- **不改 §4.10「6 条硬规则」之规则 5**（「危险操作必须 `side_effects ∈ {write, destructive}` 且 `requires_approval=True`，adapter 在 dispatch 前强制校验」）及其在 `CLAUDE.md` §4.10 规则 5 / `tool-registry-capability-layer` spec hard rule 5 的镜像。理由：它是 **timeless 跨表面声明规则**（非 M2/M9 临时措辞），与永久只读不变量**一致**——agent/mcp adapter 以「拒绝」履行该「强制校验」、CLI 以「审批」履行；rewrite 它需同步项目 bible（`CLAUDE.md`）+ spec hard rule 5，属独立、更大的契约变更。本提案仅在 task 4.2 重写示例旁加一句桥接说明（澄清规则 5 在只读表面表现为拒绝），不动规则 5 文本。

## 决策

- **D-1：重命名值，而非只改散文。** 备选「保留 `..._in_m2` 值、只改注释说它是永久」被否——值名与文档自相矛盾，正是 3am 困惑源。重命名是一个精确字符串的全量 find-replace，spec 场景散文本就要改，重命名的增量成本只多 1 个 Literal 成员 + 5 处测试断言。
- **D-2：archived 不动。** `_in_m2` 在多个归档 change 里出现（含 `add-tool-registry-capability-layer` 的 tasks/design/spec 快照）；归档是历史，改它反而失真。只动 `openspec/specs/`（active）+ 源码 + 测试 + docs。
- **D-3：ARCHITECTURE docker_prune_images 示例换成一致的真实例子，不留 counterfactual。** 备选「标注为 counterfactual + 指向 M9 不变量」被否——读者仍会先读到一个写类 ToolSpec 再被告知「其实不存在」，绕。policy-gate 的教学点用「只读但 `sensitive_output=True` 的工具，想暴露给 agent/cli 但**不**暴露 mcp」同样成立（走 surface gate + sensitive_output gate），且与本工程设计一致。
- **D-4：mermaid spec3 换成真实只读管控工具。** `apply_remediation_step (destructive)` → 一个真实存在的 `side_effects=read` 工具（如 `diff_reports`），强化「即便是『做点事』的工具也永远只读」。
- **D-5：MODIFIED 必须整块复制。** 4 个 spec 的 5 个 MODIFIED 需求各只改 1-2 行，但 openspec 归档要求 MODIFIED 携带完整需求块（含全部场景），否则归档丢细节。对大块（如 tool-registry 的 84 行 `TargetSummary` scrub 需求）用**程序化 extract-substitute**（从 live spec 抽取整块 + 只替换目标子句）而非手抄，再用 `difflib` diff-anchor 确认仅目标行变化，消除抄写漂移。`openspec-cn archive` **无** `--dry-run` flag——可归档性验证须把仓库 copy 到 temp dir、在副本里跑 `openspec-cn archive -y <change>`（见 tasks 5.8）。

## 风险 / 权衡

- [reason 值是否被持久化/外泄] → `ToolPolicyViolation` 不入 report store / audit.log / cassette（audit.log 属 Remediation 子系统，与 tool dispatch 无关）。tasks 含一步 grep 确认值未被序列化到任何持久化面，避免重命名漏改造成 replay/snapshot drift。
- [reason 值并非纯内部：MCP 错误文本可观测] → 更正「纯内部异常细节」的早期表述。`ToolPolicyViolation` 经 `__all__` 导出、有公开 `.reason` 属性、`str(err)` 含 `reason=...`，且 `mcp_server/server.py:71` 对其调 `_error_result(scrub_exception_message(str(exc)))`——故 reason 值**技术上可达** MCP 客户端错误文本（scrub 去的是密钥/路径，不是枚举值）。**但**：approval 门只在 `requires_approval=True` 时触发，而当前**所有 mcp-exposed 工具（11 个：M7 只读三件套 + M7-ext 7 管控 + `propose_target_import`）全部只读、`requires_approval=False`**（源码层 `grep requires_approval=True src/hostlens/tools/` 零命中，`tests/mcp_server/test_serve_assembly.py` 钉死 11），该门对任何真实 mcp 工具永不触发——故今天没有真实客户端路径会 emit 这个字符串。结论：本次重命名**无控制流/功能变更**；reason 字符串是非契约诊断串、理论可观测但当前无真实触发路径，**非外部 API BREAKING**。
- [MODIFIED 整块复制抄写漂移] → 小块逐块从 active spec 原文复制只做 targeted 编辑；大块（如 84 行 `TargetSummary` 需求）用 extract-substitute + difflib diff-anchor（D-5），仅目标子句变化时才接受。`openspec validate --strict` + temp-copy archive（5.8）+ 全量 pytest（断言已同步）兜底。
- [测试断言遗漏] → 已枚举 5 个测试文件的精确行；改完跑 `grep -rn approval_flow_not_supported_in_m2 src tests openspec/specs docs` 必须零命中。
