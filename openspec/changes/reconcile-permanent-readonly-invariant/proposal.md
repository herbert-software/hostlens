## 为什么

M9 探索阶段确立了三条**永久不变量**：(1) Agent / MCP 表面永久只读——写类 ToolSpec 在 dispatch 永久被拒，不是「M2 临时墙」；(2) `ToolContext.approval_service` 永久是 `NoopApprovalService`，真审批属 Remediation 子系统独立的 `ApprovalGate`，不是「M9 会替换的 stub」；(3) M9 受控修复经评审**不**新增写类 Capability，撤回 `FILE_WRITE` 承诺，写走既有 `SHELL`/`target.exec` + 审批/audit/rollback。

但代码、测试、4 个 active spec、`docs/ARCHITECTURE.md` 里仍残留 M2/M9 临时性措辞（`_in_m2` 后缀、`FILE_WRITE` 过时承诺、`apply_remediation_step`/`docker_prune_images` 写类示例），与已落地的不变量矛盾。这批债在归档的 `add-remediation-plan-schema` 提案里被显式 defer 到「随 P1b/P2 提案走 spec 流程」，但一直没做，成了孤儿 tech-debt（见 TODO.md「Follow-up：文档遗留清理」三项 + M9 banner ⚠️）。对一个以「架构清晰度」为第一优先的简历项目，一个值名叫 `..._in_m2` 却被文档描述成「永久」是典型的 3am 困惑源。

## 变更内容

纯措辞 / 命名升格，**无控制流 / 功能变更**（dispatch 拒绝行为完全不变）。`reason` 值经 `ToolPolicyViolation` 导出并在 MCP 错误文本里**理论可观测**（`server.py` 对其 `scrub` 后 `str()`），但 approval 门仅在 `requires_approval=True` 时触发，而当前所有 mcp-exposed 工具（11 个）全只读（`requires_approval=False`），该门对真实 mcp 工具永不触发——故无真实客户端会 emit 此串。结论：这是 **spec-pinned 内部值重命名**（值被 3 个 active spec 钉死，故必走 spec 流程），但**非外部 API BREAKING**（非契约诊断串、无真实客户端触发路径，风险可忽略，见 design §风险）：

- spec-pinned 值重命名（非外部 API BREAKING）：`ToolPolicyReason` Literal 值 `approval_flow_not_supported_in_m2` → `approval_flow_not_supported`，同步 3 个 active spec、4 处源码、5 个测试文件的断言。
- 把 agent / mcp tool-adapter spec 与源码内联注释里「M2 阶段 / 在 M2 raise / no approval flow in M2」的临时性措辞升格为「Agent / MCP 表面**永久**只读、写类 ToolSpec 与 approval 永久不支持」的不变量措辞。
- `tools/base.py` 的 `ApprovalService` / `NoopApprovalService` / `ToolContext` docstring 从「M2 stub，M9 会替换」改为「永久 noop，真审批属 Remediation 子系统的 `ApprovalGate`」。
- `execution-target` spec 与 `targets/base.py` Capability docstring 里「M9 加 `FILE_WRITE` 等」改为「M9 经评审**不**新增写类 Capability，写走既有 `SHELL`（`target.exec`）+ 审批/audit/rollback」（与 `add-kubernetes-target` 已订正的 `K8S_EXEC` stale 措辞同体例）。
- `docs/ARCHITECTURE.md`：§4.10 mermaid 图的 `apply_remediation_step`（destructive）spec 节点与「实战场景：Policy Gate」里的 `docker_prune_images`（destructive）示例——替换为与「Agent 表面永久只读 / Remediation 不进 Tool Registry」一致的只读示例。

## 功能 (Capabilities)

### 新增功能

（无）

### 修改功能

- `agent-tool-adapter`: dispatch 的 approval gate `reason` 值重命名 + 把 side_effects / approval gate 的「M2 阶段」措辞升格为永久不变量（拒绝行为不变）。
- `mcp-tool-adapter`: 同上 `reason` 值重命名 + approval 门「in M2」措辞升格为永久。
- `tool-registry-capability-layer`: `ToolPolicyReason` Literal 成员值重命名 + 去掉「M2 阶段合法 reason 码集合」的临时性措辞。
- `execution-target`: 撤回「M9 加 `FILE_WRITE` 等」过时承诺，改为「M9 不新增写类 Capability」。

## 影响

- **Spec**：`openspec/specs/{agent-tool-adapter,mcp-tool-adapter,execution-target}/spec.md` 各 1 个 MODIFIED 需求；`tool-registry-capability-layer/spec.md` 2 个 MODIFIED 需求（异常层级 + `TargetSummary`/`CAPABILITY_ALLOWLIST` scrub 需求的 `file_write` 回填措辞）
- **源码**：`core/exceptions.py`、`tools/base.py`（含模块 docstring 第 8 行）、`agent/tools_adapter.py`（含 dispatch docstring 156-157 + 里程碑标签 10/150）、`mcp_server/tools_adapter.py`、`targets/base.py`、`tools/schemas/list_targets.py`（`CAPABILITY_ALLOWLIST` docstring 的 FILE_WRITE 承诺）
- **测试**：`tests/tools/test_context.py`、`tests/core/test_exceptions.py`、`tests/agent/test_tools_adapter_policy.py`、`tests/mcp_server/test_tools_adapter_policy.py`、`tests/mcp_server/test_tools_adapter_cross_adapter.py`、`tests/targets/test_capability.py`（FILE_WRITE-as-future docstring）
- **文档**：`docs/ARCHITECTURE.md` §4.10（mermaid spec3 + policy-gate 实战示例）
- **无依赖变更、无控制流变更**；归档目录里的历史 `_in_m2` / 旧措辞不动（历史快照）。
