## 新增需求

### 需求:通道配置必须从 `notifiers.yaml` 加载并解析 `${ENV_VAR}`

通道实例配置必须从 `~/.config/hostlens/notifiers.yaml` 加载（路径经 `Settings` 暴露，如 `notifiers_config_path`）。每个 `channels.<name>` 条目必须含 `type`（通道类型 key）及该类型所需字段；`type` 必须能在 `ChannelTypeRegistry` 解析到已注册类型，否则**加载期** fail-loud（指出未知 type）。字段值中的 `${ENV_VAR}` 占位必须在加载期从环境变量解析；引用了未设置（`os.environ` 无该 key）的环境变量必须 fail-loud（报错并指明缺失变量名），**禁止**解析为空串静默继续。仅含字面 `$`（非 `${...}` 占位）的值按字面保留、不解析（畸形如 `${X`（缺右括号）按字面保留，不当占位）；`${}`（空变量名占位）必须 fail-loud（指出空变量名非法），不静默按字面保留也不查 `os.environ[""]`。占位解析为**单层、不递归**：`${${A}}` 不做嵌套展开（按单层匹配最内/最外的确定规则处理，注入结果不再二次扫描 `${...}`），避免递归展开带来的不确定性。`validate_config` 必须校验该类型所需字段不仅**存在**且**非空**（空串/None 视为缺失 fail-loud），避免空 token 等无效配置通过。secret（token / webhook / sign secret）**禁止**明文写入 yaml，必须经 `${ENV_VAR}` 注入，且不得落入任何持久化记录（Run / 日志）。

#### 场景:未设置引用的环境变量则报错

- **当** `notifiers.yaml` 含 `bot_token: ${TELEGRAM_BOT_TOKEN}` 而该环境变量未设置，执行加载
- **那么** 加载必须 raise 并指出缺失变量 `TELEGRAM_BOT_TOKEN`，禁止解析为空串

#### 场景:已设置则正确注入

- **当** `TELEGRAM_BOT_TOKEN` 已设置，加载含该占位的通道
- **那么** 通道实例的 `bot_token` 必须等于环境变量值；该值不得出现在任何日志或 Run 记录中

#### 场景:type 引用未注册类型加载期 fail-loud

- **当** `notifiers.yaml` 某通道 `type: slack`（未注册），执行加载
- **那么** 加载必须 raise（指出未知 type `slack`），禁止静默跳过该通道

#### 场景:必需字段存在但为空视为缺失

- **当** 加载 `{type: telegram, bot_token: "", chat_id: "123"}`（`bot_token` 已设但空串）
- **那么** `validate_config` 必须 fail-loud（空值视为缺失），禁止空 token 通过

### 需求:`only_if` 路由必须复用硬化 DSL 求值器并对 severity 做有序比较

`only_if` 表达式求值必须复用 `hostlens.inspectors.dsl` 的硬化求值器（`validate_ast` 静态 AST 闸门 + `evaluate(expr, context, *, timeout_seconds=1.0)` 带超时），**禁止**自造未审计求值器或用裸 `eval`。求值结果必须经 `bool(...)` 归一（真→发送、假→`skipped`）。求值上下文必须把报告聚合 severity 映射为有序 rank（`info=0 < warning=1 < critical=2`）并绑定 `info`/`warning`/`critical` 名，使 `severity >= warning` 成立；必须暴露 `tags`（全报告 finding tags 并集）使 `'x' in tags` 成立。报告聚合 severity 必须派生为所有 finding severity 的最大值（用 rank 比较、**非**字符串字典序），无 finding 时为 `info`。`only_if` 缺省（未提供、`None`）等价恒发送；空串 `""` **不是**合法表达式，必须在加载期 fail-loud（要恒发送须省略该字段而非置空串）。

`validate_ast` 的拒绝面必须被**准确**描述（避免实现者误解）：它拒绝 `Lambda` / 各类 comprehension（`ListComp`/`SetComp`/`DictComp`/`GeneratorExp`）/ `Import`·`ImportFrom` / `__dunder__` 形式的属性访问 / 禁用 builtin 名；它**不**拒绝白名单函数调用（如 `len`/`sum`/`min`/`max`/`any`/`all`），故「`only_if` 一律禁函数调用」是**错误**预期。`validate_ast` 是**语法/AST 层**闸门、在加载期跑，**不**解析名是否存在——引用未定义名（如拼错 `severty`）能过加载、到运行期才 `NameNotDefined`，由下条「运行期求值异常」需求兜底。

#### 场景:severity 阈值路由

- **当** 报告聚合 severity 为 `warning`，`only_if == "severity >= warning"`
- **那么** 求值为真，该通道应发送；若聚合 severity 为 `info` 则求值为假，结果记 `skipped`

#### 场景:tag 成员路由

- **当** 报告 finding tags 并集含 `disk_full`，`only_if == "'disk_full' in tags"`
- **那么** 求值为真该通道应发送；不含时求值为假记 `skipped`

#### 场景:非法 only_if 在加载期 fail-loud

- **当** manifest 的 `only_if` 含被 AST 闸门拒绝的构造（`lambda` / comprehension / `__import__` 等 dunder 属性 / import 语句），或为空串 `""`
- **那么** 必须在 manifest 加载/校验期 raise（`validate_ast` 拒绝 / 空串解析失败），禁止留到运行期静默跳过

#### 场景:白名单函数调用不被加载期拒绝

- **当** `only_if` 含白名单函数调用（如 `len(tags) > 0`）
- **那么** `validate_ast` 必须放行（不在加载期 raise）；该表达式在运行期正常求值

### 需求:`only_if` 运行期求值异常必须归类为通道失败且隔离

`only_if` 求值在路由阶段（渲染/发送**之前**）执行。其求值的**任何运行期异常**——**含但不限于**类型不匹配（如 `severity >= 'warning'` 引发 `TypeError`）、引用未定义名（`simpleeval.NameNotDefined`，如拼错 `severty`）、求值超时（>1s，`TimeoutError`）、以及 `inspectors.dsl.evaluate` 复用的 simpleeval 抛出的其它运行期异常（`simpleeval.InvalidExpression` / `FeatureNotAvailable` / `NumberTooHigh` / `FunctionNotDefined` 等）——必须被捕获并归类为该通道的 `NotifyResult(status="failed", error=...)`（error 经 secret 打码），**禁止**冒泡出 job 体、**禁止**改变已裁定的 `RunStatus`，与渲染/发送异常同属失败隔离面（对齐 scheduler-engine 的 notify 派发隔离需求）。捕获必须按「任何异常」全称实现（如 catch `Exception`），**禁止**只 catch 上述例举子集而漏放其它运行期异常类。单通道求值异常不得影响其它通道。

#### 场景:类型不匹配的 only_if 在运行期记 failed 不冒泡

- **当** `only_if == "severity >= 'warning'"`（字符串字面量与 rank 比较，运行期 `TypeError`），触发一次产出 Report 的调度
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`，其它通道照常路由

#### 场景:引用未定义名的 only_if 运行期记 failed

- **当** `only_if == "severty >= warning"`（拼错名，过 `validate_ast` 但运行期 `NameNotDefined`）
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，不冒泡、不改 `RunStatus`
