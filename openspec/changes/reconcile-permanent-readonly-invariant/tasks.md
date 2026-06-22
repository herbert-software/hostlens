## 1. 源码：reason 值重命名（`approval_flow_not_supported_in_m2` → `approval_flow_not_supported`）

- [x] 1.1 `src/hostlens/core/exceptions.py`：`ToolPolicyReason` Literal 成员 `"approval_flow_not_supported_in_m2"` → `"approval_flow_not_supported"`
- [x] 1.2 `src/hostlens/tools/base.py`：`NoopApprovalService.request_approval` 里 `reason="approval_flow_not_supported_in_m2"` → `reason="approval_flow_not_supported"`
- [x] 1.3 `src/hostlens/agent/tools_adapter.py`：dispatch approval gate `reason="approval_flow_not_supported_in_m2"` → `reason="approval_flow_not_supported"`
- [x] 1.4 `src/hostlens/mcp_server/tools_adapter.py`：dispatch approval 门 `reason="approval_flow_not_supported_in_m2"` → `reason="approval_flow_not_supported"`

## 2. 源码：临时性措辞升格为永久不变量（注释/docstring，无行为变更）

- [x] 2.1 `src/hostlens/tools/base.py`：`ApprovalService` docstring「Minimal approval gate contract (M2 stub; M9 ships the real flow).」→ 「永久 noop 的 agent-surface 审批契约；真审批属 Remediation 子系统的 `ApprovalGate`，不经 ToolContext」
- [x] 2.2 `src/hostlens/tools/base.py`：`NoopApprovalService` 类 docstring「M2 stub: ... Concrete implementations land with the M9 remediation flow.」→ 永久 noop 语义（agent surface 永不触发审批；M9 受控修复的真审批门是 `remediation/` 下独立的 `ApprovalGate`，**不**替换本类）
- [x] 2.3 `src/hostlens/tools/base.py`：`ToolContext` docstring 的 `approval_service` 条目「M2 uses NoopApprovalService to keep the ABI stable while the M9 flow is unfinished」→ 「永久注入 `NoopApprovalService`；agent-surface handler 永不触发审批，真审批属 Remediation 子系统」
- [x] 2.4 `src/hostlens/agent/tools_adapter.py`：去掉**全部** M2 临时性措辞（不止内联注释）——
      - dispatch docstring 第 156-157 行 `3. Side-effects gate: M2 forbids ...` / `4. Approval gate: M2 forbids ...` → `agent surface permanently forbids ...`
      - 内联注释第 182 行 `# 3. Side-effects gate (M2 read-only).` / 第 191 行 `# 4. Approval gate (M2 has no approval flow).` → permanent-invariant 措辞
      - 里程碑标签第 10 行 `runs the M2 policy gate` / 第 150 行 `Run the full M2 policy gate` → 去掉 `M2` 限定词（gate 是永久的）
- [x] 2.5 `src/hostlens/mcp_server/tools_adapter.py`：去掉 M2 临时性措辞——
      - dispatch docstring 第 94-95 行 `4. Side-effects gate: MCP forbids ...` / `5. Approval gate: MCP forbids ...`：把任何 M2 临时框架改成永久（`MCP forbids` 本身已是永久陈述，保留；如有 `in M2` 字样去掉）
      - 内联注释第 138 行 `# 5. Approval gate (MCP has no approval flow in M2).` → 去 `in M2`，措辞为永久不变量
- [x] 2.6 `src/hostlens/targets/base.py`：`Capability` docstring 第 39-40 行「M9 ``add-remediation`` will add ``FILE_WRITE`` and write-class capabilities.」→ 「M9 受控修复经评审**不**新增写类 Capability（撤回 `FILE_WRITE` 承诺）；写操作走既有 `SHELL`（`target.exec`）+ 审批/audit/rollback」
- [x] 2.7 `src/hostlens/tools/schemas/list_targets.py` 第 53-55 行：`CAPABILITY_ALLOWLIST` docstring「M9 ``FILE_WRITE`` will expand both the enum and this allowlist together」→ 「M9 经评审不新增写类 Capability（撤回 FILE_WRITE 承诺）；该集合维持 M1 5 值，写操作走 SHELL + 审批/audit/rollback」（与 2.6 同体例；这是 `targets/base.py:39` 的派生兄弟，留着会与 2.6 自相矛盾）
- [x] 2.8 `tests/targets/test_capability.py` 第 30 行：docstring「M9 ``FILE_WRITE`` etc. would each go through its own proposal.」→ 「写类 Capability 经评审不新增（M9 撤回 FILE_WRITE 承诺）；未来如确需再各自走提案」（execution-target delta 已显式声明本提案订正此文件，必须落实）
- [x] 2.9 `src/hostlens/tools/base.py` 第 8 行模块 docstring：「`NoopApprovalService` — M2 stub that always refuses (M9 will replace).」→ 「`NoopApprovalService` — permanent noop that always refuses; 真审批属 Remediation 子系统的 `ApprovalGate`」（2.1/2.2 漏掉的模块级 docstring）

## 3. 测试：同步断言（reason 值）

- [x] 3.1 `tests/tools/test_context.py:128`：`assert err.reason == "approval_flow_not_supported_in_m2"` → `"approval_flow_not_supported"`
- [x] 3.2 `tests/core/test_exceptions.py:485`：reason 取值列表里的 `"approval_flow_not_supported_in_m2"` → `"approval_flow_not_supported"`
- [x] 3.3 `tests/agent/test_tools_adapter_policy.py`：第 12 行注释/docstring + 第 94 行断言里的旧值 → 新值
- [x] 3.4 `tests/mcp_server/test_tools_adapter_policy.py:118`：断言旧值 → 新值
- [x] 3.5 `tests/mcp_server/test_tools_adapter_cross_adapter.py:76`：`agent_err.reason == mcp_err.reason == "approval_flow_not_supported_in_m2"` → 新值
- [x] 3.6 **不要**改 `tests/agent/backends/test_anthropic_api.py:404` 的 `test_quota_check_returns_none_in_m2`（quota 独立事项，超范围）

## 4. docs/ARCHITECTURE.md：写类示例换成只读、与不变量一致

- [x] 4.1 §4.10 Layer 2 mermaid 图 `spec3`（`apply_remediation_step` / `side_effects=destructive` / `requires_approval=True`）→ 换成真实只读管控工具节点（如 `diff_reports`，`side_effects=read`）；强化「即便『做点事』的工具也永远只读」
- [x] 4.2 §「实战场景：Policy Gate 防止意外暴露」的 `docker_prune_images`（`surfaces={"agent","cli"}` / `side_effects="destructive"`）整段 → 重写为与本工程**真实 adapter 机制一致**的只读例子（Codex C-B：原拟例子把 surface gate 与 sensitive_output gate 混为一谈，会教错不变量）：
      - 例子工具：一个**只读但 verbose** 的诊断工具（如假想 `dump_internal_state`，`side_effects="read"` / `sensitive_output=True`），想给 `agent`/`cli` 用但**不**给远程 MCP 客户端。
      - **排除机制 = surface gate**：`surfaces={"agent","cli"}`（无 `"mcp"`）→ 根本不进 `registry.list_for("mcp")`，`mcp_server/tools_adapter.py:list_for_mcp()` 压根不会遍历到它。**不是** sensitive_output 把它挡在外面。
      - 教学点：要暴露给 MCP 必须**显式**把 `"mcp"` 加进 `surfaces`——这个显式动作随即逼你过 `sensitive_output`（fail-closed，`None` 即拒）+ side_effects + approval 三道门。
      - `project_to_mcp` 示例与真实 `list_for_mcp` 对齐：① `"mcp" not in spec.surfaces → return None`（surface gate，根本不投影）② `spec.sensitive_output is None → raise`（仅对已 opt-in mcp 的工具生效的 fail-closed 门）。并加一句说明：dispatch 路径另有 side_effects / approval 两道门（见 `mcp-tool-adapter` spec），与投影对称。**不要**用 `side_effects=="destructive"` 作为 MCP 排除示例。
      - **桥接说明（解决 §4.10 规则 5 的叙事邻接）**：在重写后的示例旁加一句——§4.10「6 条硬规则」之规则 5（危险操作必须 `write/destructive` + `requires_approval=True`，adapter 在 dispatch 前强制校验）是**跨表面声明规则**；在 agent / mcp 表面，该「强制校验」表现为**拒绝**（write/destructive 与 `requires_approval=True` 永久 raise，见 agent/mcp tool-adapter spec），写路径根本不以 agent/mcp ToolSpec 形式存在；本只读示例演示的是 surface gate + sensitive_output gate，与规则 5 不冲突。**不改规则 5 文本本身**（见 design §非目标）。

## 5. 验证

- [x] 5.1 实现期：`grep -rn "approval_flow_not_supported_in_m2" src/ tests/ docs/` **零命中**（实现只改代码/测试/文档）。`openspec/specs/` 的旧值由 archive 合入 delta 后才清零——见 5.8（归档后 rebuild grep 零命中）
- [x] 5.2 stale 措辞清零（分两段，避免 OOS 误报）：
      - (a) 实现期 src/tests/docs：`grep -rnE "M9.*FILE_WRITE.*will|FILE_WRITE.*expand|M9 will replace|M2 forbids|M9 ships|到 M8/M9 才回填" src/ tests/ docs/` 须**零命中**。注：这是 **backstop（抓子集）不是穷举**——任务清单 §2 才是覆盖 SOT；本 grep 只命中各 bucket 里恰好含这些 token 的 stale 行，其余 stale 行靠 §2 逐条任务覆盖。`openspec/specs/` 的 stale 措辞（如 spec.md:376「到 M8/M9 才回填」）由 MODIFIED delta 承载、archive 时整块改写——归档后 rebuild 零命中见 5.8
      - (b) 仅 `src/hostlens/tools/base.py`：`grep -nE "M2 stub|M9 remediation flow" src/hostlens/tools/base.py` 须零命中（base.py 第 8/66/72 行的 M2-stub docstring 全部升格）
      - **注**：`M2 stub` 故意**不**进 (a) 的全树 grep——它在以下 live 位置**按设计保留**（OOS 历史/迁移叙事，本提案不改）：`tests/tools/test_list_targets_redaction.py:10`（M1 迁移叙事）、`openspec/specs/tool-registry-capability-layer/spec.md:244/249`（list_inspectors/run_inspector handler M2→real 迁移叙事，所属需求本提案不 MODIFY）、`spec.md:376`（描述 M2-stub 期 placeholder 历史，本提案仅改其回填子句、保留 "M2 stub 阶段" 前缀）、`openspec/specs/llm-backend-protocol/spec.md:433`（`is_daemon_mode` 真·M2 stub，归 M5，与本提案无关）
- [x] 5.3 确认 reason 值未被持久化：`grep -rn "approval_flow_not_supported" src/hostlens/reporting src/hostlens/scheduler src/hostlens/remediation` 应零命中（reason 是异常细节，不入 store/audit/cassette；如有命中说明重命名漏改了序列化面，必须处理）
- [x] 5.4 `mypy --strict` 0 错误（Literal 值改动需类型一致）
- [x] 5.5 `ruff check` + `ruff format --check` 通过
- [x] 5.6 全量 `pytest`（**整目录非子集**）绿
- [x] 5.7 `openspec-cn validate reconcile-permanent-readonly-invariant --strict` 通过
- [x] 5.8 archive 可归档实测（`openspec-cn archive` **无** `--dry-run` flag）：把仓库 copy 到 temp dir，在副本里跑 `openspec-cn archive -y reconcile-permanent-readonly-invariant`，确认 exit 0、4 个 spec MODIFIED 正确合入、rebuild 后 `openspec/specs/` 旧值/旧措辞零命中——验证 MODIFIED 整块复制无标题漂移、scenario 标题改动不破坏 archive（遵循 memory `openspec-modified-rename-archive` SOP）
