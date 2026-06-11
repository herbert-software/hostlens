## 为什么

M9 受控修复（Remediation）是 Hostlens 从「只读诊断」迈向「会改远端状态」的第一步，§4.5 的写操作硬约束分量最重，必须最后才碰、单独碰。整个 M9 由四片提案组成（P1a 纯 schema → P1b planner → P2 execution-workflow → P3 lark-approval，写代码风险单调递增），其中 **`RemediationPlan` / `RemediationStep` 是被 P1b/P2/P3 全部共享的契约 SOT**：planner 产出它、executor 执行它、飞书卡片渲染它。schema 错了下游全返工。

因此本提案（P1a）**先把这个契约单独冻结**：只定义 Pydantic 模型 + 校验规则 + 单元测试，**零写操作、零 LLM、零 IO**。它的评审维度是「字段不变量与约束关系」，与 P1b 的「LLM 行为」完全不同，值得独立成提案先过一次 review。

## 变更内容

- 新增 `src/hostlens/remediation/models.py`，定义两个 Pydantic v2 模型：
  - **`RemediationStep`**：一个 `precheck → forward → verify` + 可选 `rollback` 的执行单元，含 `risk_level`。
  - **`RemediationPlan`**：一个 finding 对应的有序 step 列表 + 元数据（`finding_id` / `target_name` / `rationale` / `estimated_duration_seconds`）。
- 相比 `docs/ARCHITECTURE.md` §8 的原始草图，**新增 `precheck_cmd: str | None` 字段**：在执行 `forward_cmd` 前验证假设仍成立，补上 `verify_cmd`（forward 后验证）的前向对称缺口，抵御审批延迟导致的世界漂移（TOCTOU，如 PID 复用）。
- 定义两条 model-level 校验不变量（用 Pydantic `model_validator(mode="after")`，违反消息含稳定 token `high_requires_precheck` / `rollback_none_requires_high` 供下游定位）：
  1. `risk_level == "high"` ⟹ `precheck_cmd` 不得为 `None`（最危险的动作必须有前提检查）。
  2. `rollback_cmd is None` ⟹ `risk_level == "high"`（不可回滚强制最高警觉；与 §8 原约束一致）。
- **全 fail-closed**：所有字段必填无默认值（缺键拒，杜绝静默 `None` 漂移）；命令字段拒空串与纯空白（不 strip 改值）；绑定字段拒「空白等效」含 `Cf`/`Zs`/`Cc` 不可见字符；`estimated_duration_seconds` 用 `StrictInt`（拒 bool/str/float）；两模型 `frozen=True`。
- **`RemediationPlan.load_json`** 类方法：拒绝重复 JSON 键（`object_pairs_hook`）——`model_validate_json` 对重复键静默取末值是篡改面，P2 必须经 `load_json` 加载落盘 plan。
- 单元测试覆盖全部上述契约（72 cases），作为冻结契约的可执行回归锚。

**实现已落地**：`src/hostlens/remediation/models.py`（~120 行纯 Pydantic）+ `tests/remediation/test_models.py`（72 passed）+ `remediation/__init__.py` 导出三个名字。`mypy --strict` 0 错误。不改任何现有模块、不接通任何执行路径、不导出执行能力。

## 功能 (Capabilities)

### 新增功能
- `remediation-plan-schema`: 受控修复方案的数据契约 —— `RemediationStep`（precheck/forward/verify/rollback 四元组 + risk_level）与 `RemediationPlan`（finding 绑定 + 有序 step + 元数据），及其 model-level 校验不变量。本能力**只定义数据形状与约束，不定义任何生成或执行行为**（那些分别属 P1b / P2）。

### 修改功能
<!-- 无。本提案不改任何现有 spec 的需求；ARCHITECTURE.md §8/§4.10 的文档遗留清理留给 P1b/P2 提案处理，不在本提案范围。 -->

## 影响

- **新增代码**：`src/hostlens/remediation/models.py`（纯 Pydantic 模型）+ `tests/remediation/test_models.py`。
- **对外契约影响**：引入一个**新的内部数据契约** `RemediationPlan` / `RemediationStep`。它**不是** Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令中的任何一个——它是 Remediation 子系统的私有数据模型，按 M9 架构不变量「Remediation 自成子系统，不进 Tool Registry」，**不投影到任何 surface**（无 ToolSpec、无 MCP tool、无 CLI 命令）。本提案不改动 `agent/tools_adapter.py` 的 dispatch gate、不改 `ToolContext`、不引入新 `Capability`。
- **依赖**：无新增第三方依赖（Pydantic v2 已锁定）。
- **下游解锁**：P1b（`add-remediation-planner`，structured output target = 本 schema）、P2（`add-remediation-execution-workflow`，executor 消费本 schema）、P3（飞书卡片渲染本 schema）。

## 架构不变量对齐

本提案遵守 M9 探索阶段收敛的四条不变量（见 `TODO.md` M9 段）：

1. **Agent 表面永久只读** —— 本 schema 不产生任何挂 `"agent"` surface 的 ToolSpec。
2. **Remediation 自成子系统，不进 Tool Registry** —— 模型不被任何 adapter 投影。
3. **审批门与 ToolContext 分离** —— 本提案不碰 `ApprovalService` / `ToolContext`。
4. **不引入受限写 API / 不加新 Capability** —— 本提案纯数据，连 `target.exec` 都不调用。

## Non-Goals（非目标）

- ❌ **不生成 Plan** —— Planner Agent 属 P1b，本提案的模型只是它的 structured output target。
- ❌ **不执行任何 step** —— Executor / `target.exec` / dry-run / approval / rollback / audit 全属 P2。
- ❌ **不接通飞书卡片** —— 渲染属 P3。
- ❌ **不引入受限写 API、不新增 `Capability.FILE_WRITE`** —— 安全边界（审批 + audit + rollback）由 P2 承载；本提案不预设。
- ❌ **不清理 `docs/ARCHITECTURE.md` §4.10/§8 的文档遗留**（`apply_remediation_step` ToolSpec、`docker_prune_images` 示例、`_in_m2` 后缀、`FILE_WRITE` 注释）—— 这些是 design 层语义变更，随 P1b/P2 提案走 spec 流程，不在 P1a 范围。
- ❌ **不定义 audit log 格式 / 落盘路径** —— 属 P2（§9.5）。
- ❌ **不做 risk_level 的自动推断**（如「命中 `rm -rf` 模式自动标 high」）—— 那是 P1b 的 planner prompt 职责；本 schema 只校验「high ⟹ precheck 非空」这类结构不变量，不做内容启发式。

## Failure Modes

本提案是纯数据模型，故障面极小：

1. **非法 Plan 构造**（如 high-risk step precheck=None）→ Pydantic `ValidationError` 构造时立即抛出，**fail-closed**，绝不产生半合法 plan。单元测试钉死。
2. **缺字段的落盘 plan**（P2 读到损坏/旧版 JSON）→ 所有字段必填无默认值，缺任何键抛 `ValidationError`（`missing`），不静默回填 `None`（杜绝「缺 rollback_cmd 键→静默无回滚高危 step」漂移）。
3. **JSON 未知字段 / 重复键**（篡改/拼接损坏的落盘 plan）→ 未知字段由 `extra="forbid"` 拒；重复键由 `RemediationPlan.load_json`（`object_pairs_hook`）拒，不静默取末值（`model_validate_json` 本身会取末值，故 P2 必须经 `load_json`）。
4. **空 `steps` / 空或空白等效绑定字段 / 纯空白命令** → 分别由 `Field(min_length=1)`、`_reject_blank_binding`（剔除 `Cf`/`Zs`/`Cc` 后判空）、`_reject_blank_command` 拒。
5. **`estimated_duration_seconds` 类型混入**（bool/str/float）→ `StrictInt` 拒一切非 int 强制（`True→1` 之类静默不会发生）；负数由 `ge=0` 拒；无上界（离谱大值属 planner 质量、P1b 处理，不致 schema 崩溃）。

## Operational Limits

- **并发预算**：N/A —— 纯数据模型，无 IO、无并发。
- **内存预算**：单个 `RemediationPlan` 是小对象（数个 step，每个几条命令字符串），内存可忽略。
- **超时设置**：N/A —— 无任何阻塞操作。

## Security & Secrets

- **新密钥**：无。
- **脱敏**：`forward_cmd` 等字段可能在 P2 的 audit / P3 的飞书卡片里包含敏感命令（如含 token 的 URL），但**脱敏是消费方（P2/P3）的职责**，本 schema 不强制脱敏、也不假定。本提案仅在文档中标注「这些字段是 untrusted 命令串，消费方渲染前需自行评估脱敏」。
- **攻击面**：本提案**不扩大攻击面** —— 模型不执行任何东西，`forward_cmd` 在 P1a 阶段只是一个被存储和校验的字符串，没有任何代码路径会 eval 它。真正的攻击面（任意 shell 执行）由 P2 引入并以审批 + 非 root + audit 防护。

## Cost / Quota Impact

- **Token 消耗**：0 —— 本提案不调用任何 LLM。
- **API 调用频次**：0。
- **对 Anthropic 配额影响**：无。

## Demo Path

5 分钟内本地 reproduce（无 SSH、无付费 API、无 docker）：

```bash
pip install -e ".[dev]"
pytest tests/remediation/test_models.py -v   # 全绿：合法构造 + 两条不变量正反例 + JSON 往返
python -c "
from hostlens.remediation.models import RemediationPlan, RemediationStep
# 合法 high-risk step：有 precheck
step = RemediationStep(
    description='清理 /var/log 下 7 天前的轮转日志',
    precheck_cmd='test \$(df --output=pcent /var | tail -1 | tr -dc 0-9) -ge 90',
    forward_cmd='find /var/log -name \"*.gz\" -mtime +7 -delete',
    rollback_cmd=None,            # 删除不可回滚
    verify_cmd='test \$(df --output=pcent /var | tail -1 | tr -dc 0-9) -lt 90',
    risk_level='high',
)
plan = RemediationPlan(
    finding_id='disk-var-log-full',
    target_name='prod-web-01',
    rationale='/var 分区使用率 94%，轮转日志占大头',
    steps=[step],
    estimated_duration_seconds=5,
)
print(plan.model_dump_json(indent=2))
# 反例 1：high-risk 但 precheck=None → 触发 high⟹precheck 不变量
try:
    RemediationStep(description='x', precheck_cmd=None, forward_cmd='rm -rf /tmp/x',
                    rollback_cmd=None, verify_cmd='true', risk_level='high')
except Exception as e:
    print('正确拒绝 1:', type(e).__name__)
# 反例 2：rollback=None 却非 high → 触发 rollback=None⟹high 不变量
try:
    RemediationStep(description='x', precheck_cmd='true', forward_cmd='true',
                    rollback_cmd=None, verify_cmd='true', risk_level='low')
except Exception as e:
    print('正确拒绝 2:', type(e).__name__)
# 反例 3：load_json 拒重复 JSON 键（防篡改）
import json
try:
    RemediationPlan.load_json('{\"finding_id\":\"a\",\"finding_id\":\"b\"}')
except ValueError as e:
    print('正确拒绝 3:', str(e)[:18])
"
```

预期：打印合法 plan 的 JSON，并对三个独立反例（① high-risk precheck=None ② rollback=None 却 risk_level=low ③ load_json 重复键）各打印一次 `正确拒绝 N: ...`。注：所有字段必填，构造 step 须显式传 `precheck_cmd=None`/`rollback_cmd=None`（非省略）。
