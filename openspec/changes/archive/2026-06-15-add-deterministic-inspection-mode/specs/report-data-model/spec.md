## 修改需求

### 需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT

`hostlens.reporting.models.Finding` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段——四个 M1 核心字段（不变）+ 三个 M3 add-only 身份字段（全部带默认值）+ 一个本提案新增 add-only 来源字段（带默认值，旧构造方与旧 JSON 零改动可加载）：

核心字段（M1，不变）：

- `severity: Severity`
- `message: str`（min_length=1）
- `evidence: list[Evidence] = []`
- `tags: list[Tag] = []`（M1 finding DSL 不生产 tags；用于 M5 Notifier `only_if` 路由；每个 tag 约束 `^[a-z][a-z0-9_-]*$`）

M3 add-only 身份字段（用于 diff 指纹与根因假设引用；见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹）：

- `id: str | None = None`（确定性内容指纹；`from_inspector_results` 自动计算；直接构造或 legacy JSON 缺省为 None）
- `inspector_name: str | None = None`（产出该 finding 的 Inspector name；工厂从所属 `InspectorResult.name` 填充）
- `inspector_version: str | None = None`（Inspector version；工厂从 `InspectorResult.version` 填充；diff 版本对齐用）

本提案 add-only 来源字段（用于多 target / fleet Report 标注每条 finding 的来源 target；见 §需求:多 target Report 必须由确定性 fleet 组装路径产出）：

- `target_name: str | None = None`（产出该 finding 的来源 target 名；默认 `None` → 旧构造方 / 旧 JSON 零改动可加载；多 target 组装路径给每条 flatten 出的 finding 盖来源 `InspectorResult.target_name`；单 target 路径可留 `None` 或盖单值。**禁止**纳入 `compute_finding_id` 指纹——保单 target finding id 跨 run 稳定，见下「不纳入指纹」约束）

`extra="forbid"` 仍生效。`hostlens.reporting.models.Finding` 是 **唯一 SOT**；以下 import path 必须是 type alias re-export，**禁止**独立定义：

- `hostlens.inspectors.result.Finding` = `from hostlens.reporting.models import Finding as Finding`
- `hostlens.tools.schemas.run_inspector.FindingSummary` = `FindingSummary = Finding`

**`target_name` 不纳入 `compute_finding_id`**：指纹仍恒为 `sha256(f"{inspector_name}\x00{inspector_version}\x00{message}")[:16]`（见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹，本提案**不改**该指纹定义）；`target_name` 是 add-only 标注字段，**禁止**进入指纹，否则同一检查项跨 target 会得到不同 id、破坏 per-target regression diff 的同 id 锚点。

#### 场景:Finding 字段集严格（核心四字段 + M3 身份字段 + 来源字段，拒绝未声明字段）

- **当** 试图 `Finding(severity="info", message="x", evidence=[], tags=[], extra="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:Finding 默认 evidence 与 tags 为空 list

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.evidence == []` 且 `finding.tags == []` 必须均为 True

#### 场景:Finding 仅核心字段时身份字段与来源字段默认 None

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.id is None` 且 `finding.inspector_name is None` 且 `finding.inspector_version is None` 且 `finding.target_name is None` 必须均为 True

#### 场景:单 target 路径构造 finding 不带来源 target_name

- **当** 在单 target 路径构造 `Finding(severity="info", message="ok")`（不传 `target_name`）
- **那么** `finding.target_name is None` 必须为 True（向后兼容,单 target 不强制盖来源标注）

#### 场景:Finding 接受显式来源 target_name

- **当** 构造 `Finding(severity="warning", message="cpu high", target_name="aliyun-bj")`
- **那么** 必须成功且 `finding.target_name == "aliyun-bj"`

#### 场景:Finding 接受显式身份字段

- **当** 构造 `Finding(severity="warning", message="cpu high", id="abc123", inspector_name="linux.cpu.top_processes", inspector_version="1.0.0")`
- **那么** 必须成功且三个身份字段按传入值保存

#### 场景:Finding 接受 Evidence 实例列表

- **当** 构造 `Finding(severity="critical", message="db down", evidence=[Evidence(kind="command_output", command="ping db", stdout="", stderr="timeout", exit_code=1)])`
- **那么** 必须成功且 `finding.evidence[0].kind == "command_output"`

#### 场景:Finding 接受 tags 列表

- **当** 构造 `Finding(severity="warning", message="cpu high", tags=["cpu", "perf"])`
- **那么** 必须成功且 `finding.tags == ["cpu", "perf"]`

#### 场景:Finding 拒绝 dict 形式 evidence

- **当** 试图 `Finding(severity="info", message="x", evidence={"key": "value"})`（dict 而非 list）
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝 list 中混入非 Evidence 元素

- **当** 试图 `Finding(severity="info", message="x", evidence=["not an evidence"])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝非字符串 tags

- **当** 试图 `Finding(severity="info", message="x", tags=[123, None])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 是 frozen 不可变

- **当** 构造 `f = Finding(severity="info", message="x")` 后试图 `f.severity = "critical"`
- **那么** 必须 raise `pydantic.ValidationError` 或 `TypeError`（Pydantic v2 frozen 行为）

#### 场景:Finding type alias 路径必须可 import

- **当** 执行 `from hostlens.inspectors.result import Finding as F1; from hostlens.tools.schemas.run_inspector import FindingSummary as F2; from hostlens.reporting.models import Finding as F3`
- **那么** `F1 is F3` 与 `F2 is F3` 必须均为 True（type alias，不是子类）

#### 场景:legacy 缺身份字段与来源字段的 dict 可加载

- **当** 执行 `Finding.model_validate({"severity": "info", "message": "x"})`（旧 schema 产出的 finding，无 id/inspector_name/inspector_version/target_name）
- **那么** 必须成功且四个 add-only 字段均为 None（add-only 向后兼容）

#### 场景:target_name 不改变 finding id

- **当** 两次以同 `inspector_name`/`inspector_version`/`message` 但不同 `target_name`（一个 `"a"` 一个 `"b"`）经 `compute_finding_id` 计算 id（指纹 helper 入参不含 target_name）
- **那么** 两次 `id` 必须**相同**（`target_name` 不参与指纹）

### 需求:渲染/落盘边界必须脱敏 `meta`/`hypotheses` 字符串并透传 Finding 身份字段

`redact_report_for_render` 在产生脱敏拷贝时，**必须**：

- 透传 `Finding.id` / `Finding.inspector_name` / `Finding.inspector_version`（**不**脱敏——id 是 hash、inspector name/version 非敏感；但必须在 `_redact_finding` 重构调用里带上，否则脱敏拷贝丢字段）
- **透传 `Finding.target_name`（本提案 add-only 来源字段）**：在 `_redact_finding` 重构 `Finding(...)` 调用里**必须**带上 `target_name`，与既有 `meta` / `report` 的 `target_name` 同样过 `redact_text`（`None` 透传 `None`），否则脱敏拷贝把它丢成 `None`。**理由（硬约束，非可选）**：所有 notifier 渲染入口（`telegram.py` / `lark.py` 的 `render`）**先** `redact_report_for_render`、**再**喂模板（见 notifier-telegram / notifier-lark spec），故提案 C 的多 target 分节与 `(target_name, inspector_name, message, severity)` 四元组去重消费的是**脱敏拷贝**的 finding；若 `_redact_finding` 不透传 `target_name`，渲染时 `target_name` 全 `None` → C 的退化判据 `distinct(non-None target_name) ≤ 1` 命中 → 多 target 分节**永不触发**、跨主机同 finding 因 `target_name` 全 `None` 被四元组误并 → fleet 报告**静默丢失主机维度**。`redact_text` 对正常 target 名是 no-op 但确定且幂等，不破坏去重 / 分节的同值比较。**既有回归门**：`tests/reporting/test_redact_m3_fields.py` 的 add-only 字段透传守门测试**必须**同步加 `target_name` 存活断言。
- 透传 `Report.meta`（`meta is None` 时保持 None）并对其内字符串字段（`target_name` / `intent` / `target_id` / `schedule_name`）过 `redact_text`；透传 `Report.hypotheses` 并对 `description` / `suggested_actions` 过 `redact_text`
- `meta.token_usage` / `inspectors_used` / `status` 等数值与枚举字段不脱敏，原样透传
- **不变量**：脱敏拷贝**必须**保留 `meta`（除非源就是 None）——否则 `render_json` 输出缺 meta，`ReportStore` 落盘后 round-trip 取回的报告 meta 丢失（store 的索引列另从内存 `report.meta` 投影，不依赖脱敏 JSON，见 report-persistence spec）

#### 场景:脱敏拷贝保留 Finding 身份字段

- **当** 对含 `Finding(id="abc", inspector_name="insp", inspector_version="1.0", ...)` 的 report 调 `redact_report_for_render`
- **那么** 返回报告对应 finding 的 `id == "abc"`、`inspector_name == "insp"`、`inspector_version == "1.0"`（未丢失）

#### 场景:脱敏拷贝保留 Finding 来源 target_name（fleet 分节 / 去重前提）

- **当** 对含 `Finding(message="cpu high", target_name="bandwagon", ...)` 的 fleet report 调 `redact_report_for_render`
- **那么** 返回报告对应 finding 的 `target_name == "bandwagon"`（**未丢成 `None`**）——使下游提案 C 渲染层的多 target 分节与四元组去重在脱敏拷贝上仍有真实来源值

#### 场景:脱敏拷贝保留并脱敏 meta

- **当** `report.meta is not None` 且 `report.meta.intent` 含敏感字符串 `"token=sk-ABCDEFGHIJKLMNOPQRSTUVWX1234"`
- **那么** `redact_report_for_render(report).meta is not None` 且其 `intent` 不含完整 `sk-ABCDEFGHIJKLMNOPQRSTUVWX1234` 字面量

#### 场景:legacy 无 meta 报告脱敏不崩

- **当** 对 `report.meta is None` 的 legacy 报告调 `redact_report_for_render`
- **那么** 必须成功且返回报告 `meta is None`

## 新增需求

### 需求:多 target Report 必须由确定性 fleet 组装路径产出

除既有单 target `Report.from_inspector_results`（target 单值）外,**必须**提供一条多 target（fleet）Report 组装路径,接受**跨多个 target** 的 `InspectorResult` 列表并组装成**一份** Report,供确定性巡检模式（见 `deterministic-inspection-mode` 能力）的逐 target 采集结果聚合使用。该路径**必须**:

- 接受多 target 的 `inspector_results`（每个 `InspectorResult` 携带自己的 `target_name`）。
- 把 `Report.target_name` 设为**确定性 fleet 标签**——由参与的 target 名派生，派生前**必须对 target 名集合先排序取规范序**（**不依赖调用方传入的 target 顺序**），再按确定性规则 join，满足 `Report.target_name` 的 `min_length=1` 约束;同一组 target（无论传入顺序）**必须**派生同一标签（确定性、可复现）。
- 把 `meta.target_id` 设为**确定性 fleet id**——由 target_id 集合（**先排序取规范序**）+ `schedule_name` 派生,使**不同 fleet**（不同 target 集合或不同 schedule）得到**不同** `target_id`,避免在 `ReportStore` 中撞 store key（per-target store key 复用既有 target_id-keyed 语义）；确定性**不得**依赖调用方传入 target 的顺序（未来扇出若重排 target，fleet id 不得漂移、否则 store key churn 误判 baseline miss）。该派生**必须**落在**与裸 `target_name` 不相交的命名空间**——fleet id **必须**带一个不可能等于任何裸 target_name 的限定（如强制 `fleet:` 前缀）。理由:per-target report 的 `target_id` 缺省 == `target_name`(见 report-persistence);**单成员** deterministic fleet（`targets:[x]`,schedule-manifest 允许）若朴素派生出 `target_id == "x"`,会与该机 agent 模式 per-target report **撞 store key**,使 `compute_diff` 的「`target_id` 不等才 raise」防线**失效**、fleet report 被误与 per-target report 互 diff——正是本能力「fleet 无 per-target diff」非目标要禁止的污染。前缀限定把这条软约束变硬。
- flatten findings 时给**每条** finding 盖**来源** `target_name`（取自该 finding 所属 `InspectorResult.target_name`）,使一份 fleet Report 内可按来源 target 区分 findings。
- flatten findings 时**必须**与既有 `from_inspector_results` **一致地填充 M3 身份字段** `id` / `inspector_name` / `inspector_version`（取自该 finding 所属 `InspectorResult`,`id` 经 `compute_finding_id` 计算）——**仅** `target_name` 取来源 target,身份字段**不得留 None**。**下游依赖**:提案 C 的多 target 渲染按 `(target_name, inspector_name, message, severity)` 四元组去重,fleet finding 的 `inspector_name` 若为 None 会让不同 inspector 的同 message/severity finding 被误并;故 fleet 路径填身份字段是 C 去重正确性的硬前提。最省力实现 = fleet 路径**复用** `from_inspector_results` 的 per-finding 加工逻辑(身份字段 + 来源 target 一起盖)。
- 组装 `meta.inspectors_used` 时**必须**为**每个**参与的 `(target, inspector)` 留一条 `InspectorRun`、其 `status` **逐项保真**（含 `requires_unmet` / `timeout` / `target_unreachable` / `exception`，不折叠不删除）。**下游依赖**:提案 C 的覆盖行 `{ok}/{total} 项检查 · {skipped} 项跳过` 从 `meta.inspectors_used[].status == requires_unmet` 计 `{skipped}`;deterministic 模式的「`requires_unmet` 不降级」override（见 `deterministic-inspection-mode` / `scheduler-engine`）**仅**作用于 `meta.status` 报告级派生,**禁止**借此从 `inspectors_used` 删除或改写任何逐项记录,否则 C 的覆盖行 `{skipped}` 静默归零、`{total}` 缩水而无人报错。

既有单 target `from_inspector_results` 行为**不变**（target 单值、不强制盖 finding 来源 target_name）。

#### 场景:多 target 组装产出一份 Report

- **当** 以 `targets=[a, b]` 的混合 `InspectorResult`（`a` 与 `b` 各自的结果各带其 `target_name`）经 fleet 组装路径组装
- **那么** **必须**产出**一份** `Report`,其 `inspector_results` 含 a 与 b 的全部结果,`findings` 是跨 a/b 的扁平视图

#### 场景:fleet Report 的 findings 带来源 target_name

- **当** fleet 组装路径 flatten `targets=[a, b]` 的 findings
- **那么** 来自 `a` 的 `InspectorResult` 的每条 finding `target_name == "a"`,来自 `b` 的每条 finding `target_name == "b"`

#### 场景:fleet Report 的 findings 身份字段非 None（C 去重前提）
- **当** fleet 组装路径 flatten 一条来自某 inspector 的 finding
- **那么** 该 finding 的 `inspector_name` / `inspector_version` / `id` **必须**非 None（与既有 `from_inspector_results` 一致填充）,**仅** `target_name` 取来源 target;**禁止**只盖 `target_name` 而留身份字段 None

#### 场景:fleet 的 inspectors_used 逐项保真 requires_unmet（C 覆盖行前提）
- **当** fleet 组装一组结果含一个 `status == requires_unmet` 的 inspector,且 deterministic 模式对 `meta.status` 应用「`requires_unmet` 不降级」override
- **那么** `meta.inspectors_used` **必须**仍含该 inspector 的 `InspectorRun` 且其 `status == "requires_unmet"`（逐项记录不被 override 删除 / 改写）;`meta.status` 不因它降级 partial，但 `inspectors_used` 保真——使提案 C 覆盖行的 `{skipped}` 计数能从中数出 ≥ 1

#### 场景:fleet target_id 由有序 target 集合与 schedule 确定性派生

- **当** 对同一组 `targets`（同序）+ 同一 `schedule_name` 两次组装 fleet Report
- **那么** 两次的 `meta.target_id` **必须相同**;而对**不同** target 集合或不同 `schedule_name` 组装时 `meta.target_id` **必须不同**（避免不同 fleet 撞 store key）

#### 场景:单成员 fleet 的 target_id 不撞该成员的 per-target target_id
- **当** 以 `targets=[x]` 组装 deterministic fleet Report,且该机另有 agent 模式 per-target report（`meta.target_id == "x"`）
- **那么** fleet Report 的 `meta.target_id` **必须 ≠ `"x"`**（带 `fleet:` 类限定前缀）,使二者在 `ReportStore` 不撞 key、`compute_diff` 不会跨 fleet/per-target 误取基线

#### 场景:fleet target_name 标签确定性

- **当** 对同一组 `targets`（同序）两次组装 fleet Report
- **那么** 两次的 `Report.target_name` **必须相同**且满足 `min_length=1`

### 需求:fleet（多 target）Report 的 per-target regression diff 是非目标

多 target（fleet）Report 是 **notify 导向**的聚合产物;**per-target regression diff 仍只在 per-target（agent 模式）report 上做**。fleet Report 持有**单一** `meta.target_id`（fleet id),**无法**为其内含的每个 target 取 per-target baseline,故**禁止**期望对 fleet Report 做 per-target regression diff。`report-regression-diff` 的 target_id-keyed baseline 语义对 fleet Report **不适用**:fleet Report 的 baseline（若做）只能是「同 fleet id 的上一份 fleet Report」整体比对,**不**拆分到每个 target。本提案**不**为 fleet Report 实现任何 diff;regression diff 的既有 per-target 契约不变。**反向依赖提示**:提案 C 的 finding-id message-churn 免责（inspector-authoring-contract）依赖**本条「不为 fleet Report 实现任何 diff」**——若未来给 fleet Report 加**任何** diff（per-target **或** fleet-level「同 `meta.target_id`(fleet id) 整体比对」;`compute_finding_id` 恒 hash `message`、与 diff 粒度无关,故两种粒度都会让 message 改写产生一次性 `resolved`/`added` churn），须同步评估并撤销 C 的 churn 免责叙述。

#### 场景:fleet Report 不期望 per-target baseline

- **当** 一份 fleet（多 target）Report 落盘后
- **那么** **禁止**对其执行 per-target regression diff（按各内含 target 分别取 baseline）;per-target diff 仅适用于 agent 模式的单 target report
