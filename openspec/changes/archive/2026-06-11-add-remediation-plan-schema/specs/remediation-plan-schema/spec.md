## 新增需求

### 需求:RemediationStep 定义一个 precheck-forward-verify 执行单元

系统必须提供 `RemediationStep` Pydantic v2 模型，表示受控修复中的一个原子执行单元。它必须包含以下字段，**全部必填、无默认值**（缺任何键在反序列化时抛 `ValidationError`，杜绝静默回填导致的语义漂移）：`description`（人类可读说明，`str`，允许空串但 `None`/非 `str` 被拒）、`precheck_cmd`（`str | None`，执行前验证假设的命令）、`forward_cmd`（执行命令，非空且非纯空白）、`rollback_cmd`（`str | None`，回滚命令）、`verify_cmd`（执行后验证命令，非空且非纯空白）、`risk_level`（`Literal["low", "medium", "high"]`，非法字面量被拒）。该模型禁止包含未声明字段（`extra="forbid"`）、且为 `frozen=True`（构造后不可变）。本模型只承载数据与校验，禁止包含任何执行逻辑或对 `ExecutionTarget` 的引用。

命令字段（`forward_cmd` / `verify_cmd`，以及非 `None` 的 `precheck_cmd` / `rollback_cmd`）必须既非空串（`min_length=1`）又有可见内容：无可见内容的命令——纯空白（`"   "` / `"\t"`）或纯不可见字符（仅 Unicode `Cf`/`Zs`/`Cc`，如 ZERO WIDTH SPACE `​` / BOM `﻿`）——是语义空命令、必须被拒（`_reject_blank_command` 用与绑定字段同一套 `_is_blank_equivalent` 判定）。但该校验**不得修改值**——只拒「零可见字符」，对有可见内容的命令原样保留，命令串内合法的首尾空白不受影响。`precheck_cmd` / `rollback_cmd` 的 `None` 表示「无此步」，与空串（「有此步但没写命令」，是 bug）严格区分。

#### 场景:构造一个完整的低风险 step
- **当** 以全部必填字段（含非空 `forward_cmd`、`verify_cmd`，`risk_level="low"`，`precheck_cmd` 与 `rollback_cmd` 均给出非空串）构造 `RemediationStep`
- **那么** 模型构造成功，各字段值与输入一致

#### 场景:命令字段拒绝空串与无可见内容
- **当** 把 `forward_cmd` / `verify_cmd` / 非 `None` 的 `precheck_cmd` / `rollback_cmd` 任一设为 `""`、纯空白 `"   "` / `"\t"`、或纯不可见字符 `"​"` / `"﻿"`（其余字段合法且 `risk_level` 满足不变量）
- **那么** 构造抛出 `ValidationError`（空串触发 `min_length=1`；纯空白/纯不可见触发 `_reject_blank_command` 校验器，消息含 `command_must_not_be_blank`）

#### 场景:命令字段保留合法的首尾空白
- **当** 以 `forward_cmd="  echo hi  "`（有可见内容、首尾带空白）构造 `RemediationStep`
- **那么** 构造成功，`forward_cmd` 原样为 `"  echo hi  "`（纯空白校验**不 strip 修改值**）

#### 场景:precheck 与 rollback 允许为 None
- **当** 构造 `RemediationStep` 时把 `precheck_cmd` 或 `rollback_cmd` 设为 `None`（且 `risk_level` 满足相应不变量）
- **那么** 构造成功，该字段值为 `None`（表示「无此步」）

#### 场景:拒绝未声明字段
- **当** 以一个不在 schema 中的额外字段构造 `RemediationStep`
- **那么** 构造抛出 `ValidationError`（`extra="forbid"`，契约漂移立即暴露）

#### 场景:拒绝非法 risk_level 字面量
- **当** 以 `risk_level` 为 `"low"` / `"medium"` / `"high"` 以外的值（如 `"HIGH"` / `"critical"` / 大小写变体）构造 `RemediationStep`
- **那么** 构造抛出 `ValidationError`（`Literal` 类型只接受三个精确字面量）

#### 场景:description 必填 str、拒 None/非 str
- **当** 以 `description` 为 `None` 或非 `str`（如 `123`）构造 `RemediationStep`（其余字段合法）
- **那么** 构造抛出 `ValidationError`（`string_type`）——「允许空串」≠「允许 None」；planner structured output 漏填该字段吐 `None` 会被拦下（`RemediationPlan.rationale` 同此契约）

#### 场景:缺任何字段被拒（无默认值）
- **当** 用缺少 `precheck_cmd` / `rollback_cmd` / `forward_cmd` 等任一键的输入构造 `RemediationStep`
- **那么** 构造抛出 `ValidationError`（`type` 为 `missing`）——所有字段必填、无默认回填，故缺 `rollback_cmd` 键不会静默漂移成 `None`

#### 场景:模型不可变（frozen）
- **当** 对已构造的 `RemediationStep` 实例赋值任一字段
- **那么** 抛出 `ValidationError`（`frozen=True`）

### 需求:高风险 step 必须声明 precheck

系统必须强制：当 `RemediationStep.risk_level == "high"` 时，`precheck_cmd` 禁止为 `None`。该不变量必须在模型构造时（Pydantic `model_validator(mode="after")`）校验，违反时抛出 `ValidationError`，且其消息**必须包含稳定标识子串 `high_requires_precheck`**（canonical token，作为契约的一部分：下游 P1b 调试 / 测试断言一律用该 substring 定位本不变量，不依赖随手写的自然语言文案；`ValidationError.errors()[i]["type"]` 恒为 `value_error`、不含名，故 token 只在 `str(exc)` 可见）。理由：高风险动作（破坏性、可能不可回滚）最不能容忍「前提已漂移仍硬执行」，必须有前置检查抵御审批延迟导致的世界漂移。`precheck_cmd` 仅在 `risk_level == "high"` 时强制非 `None`；当 `risk_level` 为 `"low"` 或 `"medium"` 时，`precheck_cmd` 为 `None`（省略前置检查）与非空串（提供前置检查）**均合法**——低/中风险动作多为幂等无害，强制每步写 precheck 只会逼出 `precheck_cmd="true"` 之类噪音。

#### 场景:high-risk 缺 precheck 被拒
- **当** 构造 `risk_level="high"` 且 `precheck_cmd=None` 的 `RemediationStep`
- **那么** 构造抛出 `ValidationError`

#### 场景:high-risk 带 precheck 通过
- **当** 构造 `risk_level="high"` 且 `precheck_cmd` 为非空串的 `RemediationStep`（其余字段合法，`rollback_cmd` 取 `None` 或非空串均可）
- **那么** 构造成功

#### 场景:非高风险 step 可省略 precheck
- **当** 构造 `risk_level` 为 `"low"` 或 `"medium"` 且 `precheck_cmd=None` 的 `RemediationStep`（`rollback_cmd` 为非空串以满足不变量，其余字段合法）
- **那么** 构造成功（非高风险 step 允许省略 precheck）

#### 场景:不变量违反消息含 canonical token
- **当** 单独违反某条不变量（其余字段合法）构造 step——分别构造「`high` 缺 precheck」与「`rollback=None` 且非 high」两例
- **那么** 前者 `str(ValidationError)` 含子串 `high_requires_precheck`、后者含 `rollback_none_requires_high`（token 是契约，下游用 `assert "<token>" in str(exc)` 定位，不用 `errors()[i]["type"]`——该 type 恒为 `value_error` 不含名）

#### 场景:字段级错误短路跨字段不变量
- **当** 构造一个同时有字段级错误（如 `forward_cmd=""`）与不变量违反（如 `high` 缺 precheck）的 step
- **那么** 只报字段级错误（`string_too_short`），不变量 token **不出现**——`model_validator(mode="after")` 在字段级校验失败时不运行；下游不可依赖单次 `ValidationError` 拿到全部违反原因

### 需求:不可回滚的 step 必须标记为高风险

系统必须强制：当 `RemediationStep.rollback_cmd is None` 时，`risk_level` 必须为 `"high"`。该不变量必须在模型构造时校验，违反时抛出 `ValidationError`，且其消息**必须包含稳定标识子串 `rollback_none_requires_high`**（canonical token，同上：下游用该 substring 定位本不变量）。理由：无回滚路径的操作必须获得最高警觉级别（与 ARCHITECTURE §8 原约束一致）。

#### 场景:无 rollback 却非 high 被拒
- **当** 构造 `rollback_cmd=None` 且 `risk_level` 为 `"low"` 或 `"medium"` 的 `RemediationStep`
- **那么** 构造抛出 `ValidationError`

#### 场景:无 rollback 且 high 通过
- **当** 构造 `rollback_cmd=None` 且 `risk_level="high"` 且 `precheck_cmd` 非空的 `RemediationStep`
- **那么** 构造成功

### 需求:RemediationPlan 绑定 finding 并聚合有序 step

系统必须提供 `RemediationPlan` Pydantic v2 模型，包含字段（**全部必填、无默认值**）：`finding_id`（绑定的发现 ID）、`target_name`（执行目标名）、`rationale`（修复理由，`str`，允许空串但拒 `None`/非 `str`）、`steps`（`list[RemediationStep]`，至少 1 个元素）、`estimated_duration_seconds`（**严格** `int`，非负）。该模型禁止包含未声明字段（`extra="forbid"`）、且 `frozen=True`。`steps` 的顺序即执行顺序，由消费方（P2 Executor）解释，本模型不附加额外语义。

`finding_id` 与 `target_name` 是绑定标识字段，必须**非空且非「空白等效」**：空 `finding_id` 会让落盘 plan 绑到空发现、空 `target_name` 会让 P2 无法解析执行目标，二者皆为正确性 bug，必须在 schema 层拦截。「空白等效」由 `_reject_blank_binding` 校验器经共享的 `_is_blank_equivalent` 判定——剔除所有不可见字符（Unicode 类别 `Cf` 格式符 / `Zs` 空格 / `Cc` 控制符，如 ZERO WIDTH SPACE `​`、BOM `﻿`）后 `strip()` 为空即拒；这比 `strip_whitespace=True`（只处理常规空白、漏掉 `Cf` 零宽字符）更严，关闭「纯不可见字符绑定值」这一篡改面。命令字段（见上）用同一套 `_is_blank_equivalent`——「零可见字符」在命令或绑定任一角色都是垃圾；两者都只拒「无可见内容」、不修改有内容的值。

`estimated_duration_seconds` 是**严格** `int`（`StrictInt`）：拒绝 `bool`（`True`/`False`）、数字串（`"5"`）、浮点（`5.0`/`5.5`）等一切非 `int` 输入——该字段虽是装饰性预估，但严格类型与整体 fail-closed 基调一致，杜绝 `True→1` 之类静默强制。

#### 场景:构造一个含单 step 的合法 plan
- **当** 以非空 `finding_id` / `target_name`、合法 `rationale`、含至少一个合法 `RemediationStep` 的 `steps`、非负 `int` 类型 `estimated_duration_seconds` 构造 `RemediationPlan`
- **那么** 构造成功，`steps` 顺序与输入一致

#### 场景:拒绝空或空白等效的绑定标识字段
- **当** 构造 `finding_id` 或 `target_name` 为 `""`、`"   "`、`"\t"`、或纯不可见字符（`"​"` / `"﻿"`）的 `RemediationPlan`（其余字段合法）
- **那么** 构造抛出 `ValidationError`（`_reject_blank_binding`：剔除不可见字符后为空即拒，消息含 `binding_field_must_not_be_blank`）

#### 场景:绑定字段含可见内容时保留原值
- **当** 以 `finding_id=" disk-full "`（有可见内容、首尾带空白）构造 `RemediationPlan`
- **那么** 构造成功，`finding_id` 原样保留（校验只拒「空白等效」，不 strip 有内容的值）

#### 场景:rationale 必填 str、拒 None/非 str
- **当** 以 `rationale` 为 `None` 或非 `str` 构造 `RemediationPlan`
- **那么** 构造抛出 `ValidationError`（`string_type`；与 `RemediationStep.description` 同契约）

#### 场景:拒绝空 step 列表
- **当** 构造 `steps=[]` 的 `RemediationPlan`
- **那么** 构造抛出 `ValidationError`（无 step 的 plan 无意义）

#### 场景:estimated_duration_seconds 严格 int
- **当** 以 `estimated_duration_seconds` 为 `5.0` / `"5"` / `True` / `False` / 负数 `-1` 构造 `RemediationPlan`
- **那么** 构造抛出 `ValidationError`（`StrictInt` 拒非 int 强制 + `ge=0` 拒负）
- **当** 以 `estimated_duration_seconds` 为非负 `int`（如 `0` / `5`）构造
- **那么** 构造成功

### 需求:RemediationPlan 支持 JSON 往返且拒绝重复键

系统必须支持 `RemediationPlan` 与其 JSON 表示之间的无损往返（`model_dump_json` → `model_validate_json`），以便 P2 将已审批的 plan 落盘、P3 将其渲染到飞书卡片。往返后所有字段值必须以 `RemediationPlan.__eq__`（Pydantic 值相等）判定相等。`extra="forbid"` 拦截多出的未声明字段；因所有字段必填无默认值，缺任何键一律抛 `ValidationError`（`missing`），不存在「缺可选键静默漂移」。

此外，系统必须提供 `RemediationPlan.load_json(data)` 类方法作为 P2 加载落盘 plan 的入口：它先用拒绝**重复 JSON 键**的解析钩子解析，再 `model_validate`。理由：`model_validate_json` 依赖 pydantic-core 解析器，对重复键静默取末值（`{"forward_cmd":"f","forward_cmd":"g"}` → `"g"`），是篡改/损坏 plan 的注入面，发生在 Pydantic 校验之前、`extra="forbid"` 无法拦。P2 必须经 `load_json` 加载，不得直接用 `model_validate_json`。

#### 场景:plan 经 JSON 往返保持相等
- **当** 对一个合法 `RemediationPlan`（其 `steps` 中**至少含一个 `rollback_cmd=None` 的 high-risk step**，以覆盖 `null` 序列化路径）调用 `model_dump_json()` 再用 `model_validate_json()` 重建
- **那么** 重建对象与原对象以 `RemediationPlan.__eq__`（Pydantic 值相等，**非** `model_dump()` 字典比较）相等，且 `rollback_cmd` 在 JSON 中序列化为 `null`（字段保留，非消失）

#### 场景:嵌套 step 经 dict 强制路径反序列化（P2 加载落盘 plan 的确切路径）
- **当** 用 `RemediationPlan.model_validate({...})` / `model_validate_json(...)` 传入 `steps` 为 **dict 列表**（而非 `RemediationStep` 实例）
- **那么** 每个 dict 被强制成 `RemediationStep`，且 `extra="forbid"` 与跨字段不变量**传播进嵌套**：dict 内含未声明字段 → `ValidationError`；dict 内 `risk_level="high"` 缺 `precheck_cmd` → 被 `high_requires_precheck` 不变量拒；合法 dict → 构造成功

#### 场景:反序列化含未知字段的 JSON 被拒
- **当** 用一段含 schema 未声明字段的 JSON 调用 `model_validate_json()`
- **那么** 抛出 `ValidationError`（`extra="forbid"`）

#### 场景:load_json 拒绝重复键
- **当** 用一段含重复 JSON 键（如 `finding_id` 出现两次）的 payload 调用 `RemediationPlan.load_json()`
- **那么** 抛出 `ValueError`（消息含 `duplicate_json_key`），不静默取末值

#### 场景:load_json 接受干净 payload 并等价于直接构造
- **当** 用一个合法 plan 的 `model_dump_json()` 输出调用 `RemediationPlan.load_json()`
- **那么** 返回的对象与原 plan 相等
