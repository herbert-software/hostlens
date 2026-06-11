## 上下文

M9 受控修复切成四片提案（P1a schema → P1b planner → P2 execution-workflow → P3 lark-approval），写代码风险单调递增。本提案是 P1a：把被下游三片共享的数据契约 `RemediationPlan` / `RemediationStep` 单独冻结。

当前状态：`src/hostlens/remediation/__init__.py` 是空占位；`docs/ARCHITECTURE.md` §8 已有 schema 草图，但**缺 `precheck_cmd` 字段**，且 §4.10 把 remediation 画成一个 `apply_remediation_step` ToolSpec（与 M9 探索阶段收敛的「Remediation 自成子系统、不进 Tool Registry」不变量矛盾——该文档遗留留给 P1b/P2 清理，不在本提案）。

约束：Pydantic v2 已锁定；`mypy --strict` 必须过；本提案零 IO、零 LLM、零写操作。

## 目标 / 非目标

**目标：**
- 定义 `RemediationStep` / `RemediationPlan` 两个 Pydantic v2 模型及其 model-level 校验不变量。
- 在 §8 草图基础上补 `precheck_cmd` 字段，闭合「执行前验证」缺口。
- 用纯单元测试钉死全部校验不变量与 JSON 往返，作为下游三片的契约 SOT。

**非目标：**
- 不生成、不执行、不渲染 Plan（分属 P1b / P2 / P3）。
- 不引入新 `Capability`、不投影到任何 surface、不碰 `ToolContext` / `ApprovalService`。
- 不做 risk_level 自动推断、不做命令脱敏（消费方职责）。

## 决策

### 决策 1：新增 `precheck_cmd: str | None`，补 verify 的前向对称缺口

§8 草图只有 `verify_cmd`（forward 后验证「做对了吗」），缺 forward 前的 precheck（「该做吗」）。这是个不对称缺口：Plan 在 T1 为某 finding 生成，人在 T3 审批（可能延迟数分钟到数小时），执行时远端可能已漂移——典型如 `kill -9 <pid>`，审批时该 PID 是失控进程，执行时已被复用为无辜进程。没有 precheck，Executor 会忠实地杀错对象**且 audit 报告「成功」**——静默做错事是最坏的一类失败。

`precheck_cmd` 让 Executor（P2）在每步 forward 前先验证语义前提仍成立，失败则中止。它检的是**语义前提**而非时间，比「Plan 加 TTL」精准（PID 复用可能秒级发生，TTL 无法覆盖）。

**替代方案**：
- (a) Plan 加 TTL 过期作废 —— 太粗，时间没过但世界可能已变；可作 P2 补充但不能替代 precheck。
- (b) 执行前让 Agent 重新诊断生成新 Plan —— 把 LLM 拉回执行路径（违反「执行路径无 LLM」不变量），且新 Plan 又要重新审批，无限循环。否决。
- (c) precheck_cmd（采纳）—— 确定性、无 LLM、检语义前提。

### 决策 2：`precheck_cmd` 值可空，但 high-risk 强制非 None

`precheck_cmd: _NonEmptyCmd | None`——**字段必填、无默认值**（见决策 3：构造/反序列化必须显式给值，可以是 `None`），但其**值**允许为 `None`。理由同 §4.10 `sensitive_output` 精神的反用：很多 step 确实幂等无害（如 `/var/log` 清理），强制每步都写 precheck 会逼出 `precheck_cmd="true"` 的噪音。注意「值可空」≠「字段可省」：省略键被拒（`missing`），传 `precheck_cmd=None` 才合法。

但加一条 model-level 不变量：`risk_level == "high"` ⟹ `precheck_cmd is not None`。high-risk = 破坏性 + 可能不可回滚，正是最不能容忍「前提漂移仍硬执行」的场景。这给 schema 一条清晰的硬不变量，而非「全可选」的软建议。

**替代方案**：所有 step 的 precheck 都必须非 None —— 噪音大、采用阻力高；否决。渐进采用（low/medium 值可为 None，high 强制非 None）更务实。

### 决策 3：`extra="forbid"` —— 契约漂移 fail-closed

两个模型都设 `model_config = ConfigDict(extra="forbid")`。P2 会把已审批 Plan 落盘、之后读回执行。`extra="forbid"` 让 JSON 里**多出**的未声明字段（如旧版已删字段）在反序列化时**立即抛 ValidationError**，而非静默吞掉去执行破坏性命令。这与整个 Remediation「fail-closed」基调一致：宁可拒绝执行，不可带着错误假设执行。

**缺字段：所有字段必填、无默认值**。包括 `precheck_cmd` / `rollback_cmd`——它们类型是 `str | None` 但**不给 `= None` 默认值**，故缺键在反序列化时抛 `ValidationError`（`missing`），不会静默回填 `None`。这关掉了「一个原本带 `rollback_cmd` 的 step 若 JSON 缺该键、静默漂移成无回滚高危 step」的洞——P1a 在 schema 层堵死，不下推给 P2。代价：构造 step 必须显式传 `precheck_cmd=None` / `rollback_cmd=None`（而非省略），这对结构化产出的 planner 无负担、且更 fail-closed。

**重复键：`load_json` 显式拒绝**。`model_validate_json` 依赖 pydantic-core 解析器，对重复 JSON 键静默取末值（`{"forward_cmd":"f","forward_cmd":"g"}` → `"g"`），是篡改/损坏 plan 的注入面，发生在 Pydantic 校验之前、`extra="forbid"` 无法拦（重复键不是「多出字段」而是同名覆盖）。P1a 提供 `RemediationPlan.load_json(data)` 类方法：先用 `json.loads(..., object_pairs_hook=_reject_duplicate_keys)` 解析（命中重复键即 `raise ValueError("duplicate_json_key: ...")`）、再 `model_validate`。**P2 必须经 `load_json` 加载落盘 plan**，不得直接用 `model_validate_json`。这把原本要下推给 P2 的「防篡改解析」收进 P1a 的契约，使 schema 的 JSON 入口自带防御。

**替代方案**：`extra="ignore"`（Pydantic 默认）—— 静默丢弃未知字段，会让契约漂移潜伏到执行期才爆。否决。把缺键/重复键留给 P2 —— 会让 P1a 的契约不自洽（schema 声称 fail-closed 却有静默漂移入口），且 review 上限只能到 APPROVE-DEGRADED；既然 P1a 就是要冻结一个可信契约，这两个洞就该在 P1a 堵死。

### 决策 4：模型不引用 `ExecutionTarget`、不进 Tool Registry

`RemediationStep` 只存命令字符串，**不持 target 引用、无执行方法**。模型是纯数据，执行由 P2 的 Executor（拿 `ExecutionTarget` + 已审批 Plan）完成。这落实「Remediation 自成子系统」不变量：schema 不被任何 adapter 投影成 ToolSpec / MCP tool / CLI 命令。与「Notifier 不进 Tool Registry」是同一类决策。

### 决策 5：字段级约束 —— Pydantic 原生 + 两个非 mutating 校验器，全 fail-closed

- 所有字段**必填、无默认值**（含 `precheck_cmd` / `rollback_cmd`，见决策 3）。
- **共享 `_is_blank_equivalent(value)`**：剔除所有不可见字符（Unicode 类别 `Cf`/`Zs`/`Cc`）后 `strip()` 为空即真。命令字段与绑定字段都用它——「零可见字符」在任一角色都是垃圾。它比 `strip_whitespace=True` 严（后者漏 `Cf` 零宽字符，`"​"` 会假阳性通过），且**不修改值**（只判定、不返回 strip 后结果），故有可见内容的串（含 `"  echo hi  "`）原样保留。
- 命令字段（`forward_cmd` / `verify_cmd` / 非 None 的 `precheck_cmd` / `rollback_cmd`）：`min_length=1` 挡空串 + `_reject_blank_command` 校验器经 `_is_blank_equivalent` 挡纯空白/纯不可见（消息 `command_must_not_be_blank`）。
- `precheck_cmd` / `rollback_cmd`：`Annotated[str, StringConstraints(min_length=1)] | None`（None 或非空串），实测空串落 `str` 分支触发 `min_length`、不回退 None 分支；校验器对 None 短路（`value is not None and ...`）。
- `finding_id` / `target_name`：`_reject_blank_binding` 校验器经同一 `_is_blank_equivalent` 判定（消息 `binding_field_must_not_be_blank`）。空绑定 = 正确性 bug（绑空发现 / 无法解析 target）。
- `rationale` / `description`：`str`（允许空串），**有意不约束非空**（planner 文案质量，不混入契约正确性）；但 `str` 类型仍拒 `None`/非 str。
- `steps`：`Field(min_length=1)`。
- 跨字段不变量（high⟹precheck、rollback=None⟹high）：`@model_validator(mode="after")`。**时机注意**：`mode="after"` 仅在全部字段级校验通过后运行，故字段级错误（如 `forward_cmd=""`）与不变量违反并存时只报字段级错误、token 不出现——这是 Pydantic 既定行为，tasks 钉成契约事实，避免 P1b 误以为单次 `ValidationError` 含全部违反原因。

### 决策 6：全 fail-closed 类型契约 —— `StrictInt` + 严格命令/绑定校验，不留 lax 静默强制

本提案对易被 lax 静默强制的字段一律收紧到 fail-closed，与整体「宁拒不带错执行」基调一致：

- **`estimated_duration_seconds: StrictInt`**：拒绝 `bool`（`True`/`False`）、数字串（`"5"`）、浮点（`5.0`/`5.5`）等一切非 `int` 输入（`Field(ge=0)` 另拒负）。理由：虽是装饰性预估，但 `True→1` 之类静默强制与 fail-closed 基调冲突；planner 产 JSON number 是真 int，`StrictInt` 无负担。**这取代了早期「lax 接受 bool/str 是 accepted-degraded」的取舍**——既然要 clean 契约，就不留这个 cosmetic 例外。
- **`steps` 的 dict 强制路径**：`model_validate_json` / `model_validate({"steps":[{...}]})` 必经「dict→RemediationStep」强制——P2 从磁盘加载 plan 的确切路径。`extra="forbid"` 与跨字段不变量**传播进嵌套 step**（dict 内多字段被拒、dict 内 `high` 缺 precheck 被 `high_requires_precheck` 拒）。此路径与「用实例构造」并行但独立，必须单独测，否则 P1b 改 steps 注解时嵌套校验静默回归无网可接。
- **`risk_level: Literal[...]`**：非法字面量（`"HIGH"` / `"critical"` / 大小写变体）由 `Literal` 拒绝；以显式场景在 spec 层显形。

**替代方案**：duration 用 lax `int`（接受 `5.0`/`"5"`/`bool`）—— 更宽松但留静默强制面，且让 review 上限卡在 APPROVE-DEGRADED。否决：clean 契约值得 `StrictInt` 这点严格。

## 风险 / 权衡

- [precheck 给「假阴性安全感」：precheck 通过不代表 forward 一定安全，precheck 与 forward 之间仍有毫秒级 TOCTOU 窗口] → 接受并在 docstring 写明「precheck 缩小而非消除 TOCTOU 窗口」；把审批延迟的分钟级窗口压到执行间隔的毫秒级已是数量级改善。
- [`Annotated[str, min_length=1] | None` 的类型表达可能让 mypy --strict 或 Pydantic 校验顺序产生意外] → 已用真实现 + 测试套件在 Pydantic 2.13.4 钉死：空串落 `str` 分支触发 `min_length`、不回退 None；纯空白触发 `_reject_blank_command`；三态行为正确、`mypy --strict` 0 错误、72 测试全绿。
- [全字段必填（含 `precheck_cmd`/`rollback_cmd` 无默认值）增加构造负担：每个 step 必须显式传 `precheck_cmd=None`] → 接受：换来「缺键即拒、无静默 None 漂移」的 fail-closed；planner 结构化产出本就给全字段，负担只落在手写测试/示例上。
- [`StrictInt` 拒 `5.0`/`"5"`：若某上游误传字符串数字会被拒] → 接受：这正是 fail-closed 想要的——上游该产真 int；宽松强制的代价（`True→1` 静默）更不可取。
- [`load_json` 是 P1a 新增的 API 面，需 P2 记得用它而非 `model_validate_json`] → 已在 spec JSON 需求 + models docstring 显式声明「P2 必须经 `load_json`」；这是把防篡改解析收进 schema 契约的代价，值得（否则重复键洞下推 P2、契约不自洽）。
- [schema 现在冻结，P1b/P2 实现时可能发现缺字段（如 step 需要 `timeout` / `id`）] → 接受：本提案有意只冻结「已确证必需」的最小字段集；下游若确需新增，走「修改 remediation-plan-schema」的增量 spec。
- [`extra="forbid"` + 全必填让未来字段新增成为「破坏性」反序列化变更] → 接受：在写子系统里，显式破坏 > 静默漂移。新增字段走增量 spec；若需向后兼容旧 plan 再单独设计迁移。

## Migration Plan

无迁移。纯新增文件（`remediation/models.py` + `tests/remediation/test_models.py`），不改任何现有模块、不接通任何路径。回滚 = 删除这两个文件，对其余系统零影响（下游 P1b 尚未实现，无消费方）。

## Open Questions

- `RemediationStep` 是否需要稳定 `id`（供 P2 audit 按 step 引用 / P3 卡片按 step 渲染按钮）？倾向**留给 P2/P3**：audit 可用「plan 内序号」引用，真需要稳定 id 时走增量 spec。本提案不预加，避免 YAGNI。
- `estimated_duration_seconds` 是否该上移到 step 级（每步一个预估）而非 plan 级？倾向保持 §8 的 plan 级（粗粒度足够给审批人一个量级感）；P2 若需 per-step 超时再议。
