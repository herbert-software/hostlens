# Spec: Inspector 插件体系（sampling_window delta）

## ADDED Requirements

### Requirement: collect.sampling_window 时窗采集

The system SHALL 支持 manifest 可选字段 `collect.sampling_window.duration_seconds`；当声明时，runner MUST 基于可注入时钟计算 `window_end = now`、`window_start = now - duration_seconds`，并把 `window_start` / `window_end`（`YYYY-MM-DD HH:MM:SS` UTC 字符串，journalctl `--since/--until` 友好，非带 `T`/时区偏移的 ISO 形式）与 `window_seconds`（int）注入到 Jinja2 命令渲染上下文与 Finding DSL 求值上下文。省略该字段时行为与既有 Inspector 完全一致（向后兼容）。

#### Scenario: 注入窗口变量到命令渲染

- **WHEN** Inspector 声明 `collect.sampling_window.duration_seconds: 300` 且 `collect.command` 引用 `{{ window_start }}` / `{{ window_end }}`
- **THEN** runner 用 `[now-300s, now]` 的 `YYYY-MM-DD HH:MM:SS` UTC 字符串渲染命令，`window_start` 早于 `window_end` 恰好 300 秒

#### Scenario: 窗口变量可用于 Finding DSL

- **WHEN** 某 finding 的 `when` 表达式引用 `window_seconds`
- **THEN** DSL 求值上下文中 `window_seconds` 等于声明的 `duration_seconds`

#### Scenario: 省略 sampling_window 保持旧行为

- **WHEN** Inspector manifest 未声明 `collect.sampling_window`
- **THEN** 渲染与 DSL 上下文中不出现 `window_start` / `window_end` / `window_seconds`，加载与执行行为与本 delta 之前完全一致

### Requirement: 可注入时钟保证回放确定性

The system SHALL 允许向 runner 注入时钟（默认真实 UTC 时钟）；测试与回放场景 MUST 能注入固定时钟，使含窗口变量的渲染命令在重复运行间逐字节稳定（从而可被 `ReplayTarget` 精确匹配，并使 snapshot 稳定）。

#### Scenario: 冻结时钟产出稳定命令

- **WHEN** 注入固定时钟并对同一 `sampling_window` Inspector 渲染命令两次
- **THEN** 两次渲染出的命令字符串完全相同

### Requirement: 窗口注入变量名为保留名

The system SHALL 把 `window_start` / `window_end` / `window_seconds` 视为运行时注入的保留名；当 manifest `parameters` 声明了与之同名的字段时，loader MUST 拒绝加载并给出字段级错误，避免 parameter 覆盖注入变量造成求值歧义。

#### Scenario: parameter 撞保留名被拒

- **WHEN** 某 manifest 的 `parameters` 声明名为 `window_start` 的字段
- **THEN** loader 拒绝加载该 manifest 并指出该名为保留注入变量名

### Requirement: Agent 表面结构化参数的 JSON 解码 coercion

The system SHALL 在 runner 参数 coercion 阶段，对 manifest 声明类型为 `array` / `object` 的 string 参数值尝试 `json.loads`；当解码成功且结果与声明的容器类型一致时采用解码值，否则保留原字符串交由后续 `jsonschema.validate` 拒绝（与既有 `integer` / `number` / `boolean` coercion 同属 permissive-coerce-then-validate 不变式）。这使数组 / 对象参数可经 Agent 表面 `RunInspectorInput.parameters: dict[str, str]` 以 JSON 编码字符串传入；shell 注入防御仍由 manifest 的 `items.pattern` 与命令模板的 `| sh`（`shlex.quote`）保证，解码分支不会让未校验值到达 `target.exec`。`RunInspectorInput.parameters` 的 `dict[str, str]` tool schema 不变。

#### Scenario: JSON 数组字符串解码后通过校验

- **WHEN** Agent 对声明 `endpoints: {type: array}` 的 Inspector 传入 `parameters={"endpoints": "[\"database:5432\"]"}`
- **THEN** runner 将其解码为 `["database:5432"]`，`jsonschema.validate` 按 `items.pattern` 通过，命令渲染得到展开后的端点

#### Scenario: 非 JSON 或类型不符的字符串被拒

- **WHEN** array 类型参数收到非 JSON 字符串（如 `"database:5432"`）或解码结果不是数组
- **THEN** 保留原字符串，`jsonschema.validate` 以类型不符拒绝，Inspector status=exception，从不到达 `target.exec`

#### Scenario: 解码出的坏 item 被 items.pattern 拒

- **WHEN** array 参数解码为 `["database:5432;whoami"]`（含非法字符）
- **THEN** `items.pattern`（`^[a-zA-Z0-9.-]+:[0-9]+$`）拒绝该 item，命令不渲染、不执行

### Requirement: 默认工具装配支持注入时钟

The system SHALL 让默认工具装配函数 `register_default_tools` 接受可选时钟参数；当传入时，所注册的 `run_inspector` 工具 MUST 把该时钟透传给 `InspectorRunner`，使 Agent → `run_inspector` → runner 路径上的 `sampling_window` Inspector 渲染确定性命令。`ToolContext` 字段集保持不变（时钟经工具装配边界注入，不进 DI 容器，遵守 ADR-008）。

#### Scenario: 经 Agent 路径的窗口命令确定性

- **WHEN** 以 `register_default_tools(registry, clock=<固定时钟>)` 装配，并经 Agent 路径运行声明 `sampling_window` 的 Inspector
- **THEN** 渲染命令使用注入的固定时钟（与不传 clock 时的真实 UTC 默认行为隔离），使 `ReplayTarget` 可精确匹配
