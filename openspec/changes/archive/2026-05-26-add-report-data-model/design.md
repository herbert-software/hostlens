## 上下文

本提案 `add-report-data-model` 收尾 M1 范围（TODO.md M1.6 + M1.7），把 Inspector 的内部结果结构 `InspectorResult` 包装成面向人类与下游 Notifier 的统一 `Report` 容器 + 提供 markdown / json 双渲染 + 给出 `hostlens inspect` 命令作为 demo path 入口。proposal 已锁定字段集与对外契约。本设计文档解决以下**四个**关键决策（用户在提案输入阶段明确点出需要在此回答）：

1. `Finding` 双地址问题（`hostlens.inspectors.result.Finding` vs `hostlens.tools.schemas.run_inspector.FindingSummary`）如何统一
2. `Evidence` 字段集设计（单结构 vs 多 kind discriminated union vs 平面 dict）
3. CLI 退出码语义化（与 archived `hostlens doctor` 风格一致的 4 值方案）
4. spec delta 应该新增独立 `report-data-model` capability spec，还是合并到 `inspector-plugin-system` 现有 spec

当前状态（M1 已落地）：

- `add-execution-target-abstraction` 已交付 `ExecutionTarget` Protocol + `LocalTarget` / `SSHTarget` 实现 + `TargetRegistry`
- `add-inspector-plugin-system` 已交付 `InspectorManifest` / `InspectorRunner` / `InspectorRegistry` / Finding DSL 引擎 + 2 个内置 Inspector
- `add-tool-registry-capability-layer` 已交付 `ToolSpec` / `ToolContext` / `register_default_tools` + `list_inspectors` / `run_inspector` 两个 M2 stub-becoming-real handler
- `Finding` 当前**有两份近似定义**：`hostlens.inspectors.result.Finding`（severity/message/evidence: dict[str,str]）+ `hostlens.tools.schemas.run_inspector.FindingSummary`（同字段集）。后者的 docstring 明确说"M3 add-report-data-model proposal 才统一"——本提案是兑现承诺的窗口

利益相关者：

- M2 `add-agent-loop-skeleton` 提案：消费本提案的 `Report` 作为 Agent 输出的统一容器
- M3 `Diagnostician + 报告体系` 提案：在本提案的 `Finding` / `Report` 上 add-only 扩展（id / fingerprint / regression diff）
- M5 `Notifier 抽象` 提案：所有 Notifier 的 `render(report)` 消费本提案的 `Report` 对象
- 项目作者本人：CLI demo path 第一次能从"自然语言意图"（M2 接入后）跑到"看得见的 markdown 报告"

约束：

- 不引入新的 **runtime** Python 依赖（**特别是禁止 Jinja2**——模板系统留给 M5 Notifier）；测试可新增 `[dev]` 依赖 `syrupy` 用于 markdown 渲染 snapshot 对比
- 不破坏 M2 已落地的 `register_default_tools` 与 `run_inspector` handler
- M1 未发版，本提案是"最后一次自由调整 schema"的窗口（BREAKING 可以做，但 spec delta 必须文档化）
- 严格遵守 CLAUDE.md §4.2 / §6 / §7 / §4.10 / §6 / 反模式清单

## 目标 / 非目标

**目标：**

- 把 `Finding` 收归到唯一 SOT（`hostlens.reporting.models.Finding`），消除两份近似定义
- 定义最小但**可向 M3 add-only 扩展**的 `Finding` / `Report` schema（schema_version 锁定 1.0）
- 让 `Evidence` 既能表达"命令输出 + stdout/stderr/exit_code"也能表达"文件摘要"也能表达"指标数值"，但**不**陷入"对所有可能证据类型穷举"的设计陷阱
- 渲染器纯机械逻辑（< 200 行）；md / json 渲染共享同一 SOT `Report` 对象，二者输出不漂移
- CLI `hostlens inspect` 退出码语义化，CI / 监控脚本可直接消费（0 = healthy / 1 = critical finding / 2 = runner 内部失败 / 3 = 用户错）
- spec 组织方式让 M3 / M5 / M2 提案的扩展点可锚定到独立的 `report-data-model` capability spec 上，而不是污染 `inspector-plugin-system` spec

**非目标：**

- 不实现 finding identity 字段（id / fingerprint / seen_at）——M3 add-only 扩展
- 不实现多 Inspector 并行调度（M2）
- 不实现 regression diff（M3）
- 不实现 HTML / PDF 渲染（M3+）
- 不引入 Jinja2 模板系统（M5 Notifier）
- 不实现 `Report.from_json()` 反向加载（M3 regression diff 才需要）
- 不实现 evidence 自动从 InspectorResult 推断（finding DSL 显式声明；当前 M1 finding DSL 不支持 evidence 构造，evidence 列表始终为空）
- 不实现 `hostlens inspect --intent` 自然语言入口（M2）
- 不实现 Notifier 集成（M5）

## 决策

### 决策 1：Finding 单 SOT，inspectors/result.py 与 tools/schemas/run_inspector.py 走 type alias re-export

**选择**：`hostlens.reporting.models.Finding` 是 SOT。两个旧地址（`hostlens.inspectors.result.Finding` 和 `hostlens.tools.schemas.run_inspector.FindingSummary`）改为 `from hostlens.reporting.models import Finding as Finding` / `FindingSummary = Finding` 的 type alias。

**理由（为什么选 type alias 而非"删除旧 import path"）**：

- M2 `register_default_tools` 与 `RunInspectorOutput.findings: list[FindingSummary]` schema 声明已经 `from hostlens.tools.schemas.run_inspector import FindingSummary`；type alias 让这些**声明**零修改；`_run_inspector_handler` 投影逻辑因 evidence dict comprehension 必须随 BREAKING 同步改一行（详见 §决策 3 → 影响 → "修改代码"）
- 旧 import path 的 docstring 已经预告"M3 add-report-data-model proposal 会替换"——type alias 兑现承诺且向用户文档一致
- M3 / M5 提案若需要扩展 Finding，**只**改 `hostlens.reporting.models.Finding`，两个旧 import path 自动跟进
- 替代方案 A：**强制删除两个旧 import path**——会让 M2 handler 重写 import，且违背"M1 已发版的 Finding 是这个 import path"的预期；M1 虽未发版给外部，但项目内部代码已经在用
- 替代方案 B：**让两个旧地址保留独立定义但加 deprecation warning**——会保留两份近似定义直到 M3 才删除，问题持续存在
- 替代方案 C：**`hostlens.reporting.models.Finding` 继承 `inspectors.result.Finding`**——继承关系反向（reporting 是上层抽象，inspectors 是下层数据源），违背依赖方向

**字段集**：本提案 `Finding` **恰好四字段**（`severity` / `message` / `evidence` / `tags`）。**两个 BREAKING**：(1) `evidence` 字段类型 `dict[str, str]` → `list[Evidence]`；(2) 新增 `tags: list[str] = []` 字段。

**为什么加 `tags` 现在而不是延后**：

- CLAUDE.md §4.4 已锁定 Notifier 路由契约：`only_if 表达式（基于报告 severity / finding tags）决定是否发送`——M5 Notifier 不加 finding tags 就没法做路由
- M5 提案修改 `Finding` 加 tags 是 BREAKING（frozen Pydantic schema 加字段对 model_validate 是兼容的，但对 model_dump_json 用了 snapshot 测试的下游是 BREAKING）；M1 现在加是 zero-cost（默认空列表，M1 finding DSL 不生产 tags，对所有现存场景透明）
- 反过来不加：M5 提案要么再做一次 Finding BREAKING（违背本提案"M3/M5 走 add-only 扩展"的承诺），要么 M5 改为把 tags 放在别处（如 `Report.metadata` 或 `Evidence.kind="metric" + metric_name="tag"`）——都比直接在 Finding 上加 tags 别扭

**为什么这两个 BREAKING 可接受**：

- M1 未对外发版（PyPI / GitHub Release 都尚无），没有外部用户
- 内部用户：`add-inspector-plugin-system` spec 中 finding DSL 的 evidence 构造场景在当前实现中**实际上没有触发**（finding DSL 当前只构造 message，evidence 留空 dict）；tags 是新字段 add-only 无既有引用——BREAKING 影响范围 = 测试代码 + spec 文本 + `default_tools.py:155` 一处 handler 投影逻辑
- M3 add-only 扩展窗口要求**现在**就把 evidence 字段类型与 tags 字段都定对，否则 M3 / M5 又要做一次 BREAKING

**循环导入处理（实现约束）**：

- 问题：`reporting.models.Report.inspector_results: list[InspectorResult]` 引用 `inspectors.result.InspectorResult`；同时 `inspectors.result.Finding = re-export reporting.models.Finding` —— 直接导入形成循环
- 方案：
  1. `reporting/models.py` 顶部：`from __future__ import annotations` + `from typing import TYPE_CHECKING` + `if TYPE_CHECKING: from hostlens.inspectors.result import InspectorResult`
  2. `Report.inspector_results: list["InspectorResult"]`（注释字符串前向引用）
  3. `inspectors/result.py` 模块**末尾**（在 `__all__` 之后）追加：
     ```python
     # 兑现 reporting.models.Report 对 InspectorResult 的 forward-ref；
     # 必须显式传 _types_namespace，因为 reporting.models 顶部用 TYPE_CHECKING
     # 隔离了 InspectorResult，运行时全局 namespace 不可见。
     from hostlens.reporting.models import Report as _Report
     _Report.model_rebuild(_types_namespace={"InspectorResult": InspectorResult}, force=True)
     ```
     —— Pydantic v2 `model_rebuild(_types_namespace=...)` 是公开 API（见 pydantic v2 文档），用于显式提供 forward-ref 解析 namespace，**不**能省略；否则 `model_rebuild()` 在运行时 globals 找不到 `InspectorResult` 类名 → raise `PydanticUndefinedAnnotation`
  4. `hostlens.reporting.__init__` **不**触发 `model_rebuild()`（保持 import 零副作用，由 `inspectors.result` 在自身 import 时完成）
  5. 用户 / 测试代码使用顺序：先 `import hostlens.inspectors.result`（触发 model_rebuild）再 `Report(...)` / `Report.from_inspector_results(...)`；CLI / Agent loop 的 entrypoint `from hostlens.targets... import ...` 链路已自然 import `inspectors.result`，无需额外处理
- 测试验证：单独 `python -c "import hostlens.reporting; Report.from_inspector_results(...)"`（**不** import inspectors.result）会触发 `PydanticUndefinedAnnotation` —— 此时是用户错（spec 明确依赖顺序）；正确路径是 `python -c "import hostlens.inspectors.result; from hostlens.reporting.models import Report; Report.from_inspector_results(...)"` 必须成功

### 决策 2：Evidence 用 kind-discriminated 平面字段，不用 discriminated union 也不用 dict

**选择**：`Evidence` 是单一 Pydantic 模型，含 `kind: Literal["command_output", "file_excerpt", "metric", "structured"]` + 全部可选字段（`command` / `stdout` / `stderr` / `exit_code` / `path` / `excerpt` / `metric_name` / `metric_value` / `data` / `truncated`），通过 `model_validator` 强制 `kind ↔ 字段子集` 映射。

**理由（为什么不用 Pydantic discriminated union）**：

- 4 种 kind 共享大量字段（如 `command_output` 和 `file_excerpt` 都可能有 `truncated` 标记）
- Pydantic v2 的 discriminated union 把字段集分散到 4 个独立模型，序列化 JSON 时增加层次（`{"kind": "command_output", "command_output": {...}}`），下游 Markdown / JSON 渲染逻辑要分支匹配 4 种 schema
- M3 扩展新 kind（如 `kind="trace"` 关联分布式追踪）时，平面字段方案只需加 1 个 Literal value + 几个 optional 字段；discriminated union 方案需要新建一个独立 Pydantic 类
- 单一平面模型 + model_validator 在 Pydantic v2 下成本 < 50 行代码，**且** 让下游渲染逻辑可以直接 `getattr(evidence, "stdout", None)` 拿字段，不需要 isinstance 分支

**为什么不用 `dict[str, Any]`**：

- 字段类型完全丢失，下游渲染 / 序列化无法保证一致性
- 与项目"全程强类型 / Pydantic v2 / mypy --strict"约束直接冲突（CLAUDE.md §6）

**kind ↔ 字段映射规则**（model_validator 强制）：

| kind | 必填字段 | 可空字段 | 禁止字段 |
|---|---|---|---|
| `command_output` | `command`, `stdout` | `stderr`, `exit_code`, `truncated` | `path`, `excerpt`, `metric_name`, `metric_value`, `data` |
| `file_excerpt` | `path`, `excerpt` | `truncated` | `command`, `stdout`, `stderr`, `exit_code`, `metric_name`, `metric_value`, `data` |
| `metric` | `metric_name`, `metric_value` | `truncated` | `command`, `stdout`, `stderr`, `exit_code`, `path`, `excerpt`, `data` |
| `structured` | `data` | `truncated` | `command`, `stdout`, `stderr`, `exit_code`, `path`, `excerpt`, `metric_name`, `metric_value` |

**字段语义**：

- `command`: 命令模板（manifest 原始字符串，**不**展开 env var）
- `stdout` / `stderr`: 上游 ExecutionTarget 的输出（已按 1MB 上限截断，`truncated=True` 时表示被截断）
- `exit_code`: 子进程退出码；`None` 表示无法获取（如 timeout）
- `path`: 文件绝对路径（manifest `requires_files` 中已校验过的字符集）
- `excerpt`: 文件内容片段（同样可能被 `truncated`）
- `metric_name`: 指标名（如 `load_1min`）
- `metric_value`: 数值（float）或 字符串（如 `"unavailable"`）
- `data`: 任意结构化数据（M1 finding DSL 不构造，M6+ hook.py 可用）
- `truncated`: 标记被上游截断（统一字段，4 种 kind 共用）

### 决策 3：CLI 退出码 4 值语义化（0/1/2/3），与 doctor 已有风格对齐

**选择**：

- `0` = 所有 finding ≤ warning **且** inspector status == "ok"（用户视角：巡检通过）
- `1` = 至少一个 critical finding（用户视角：业务问题）
- `2` = inspector status != "ok"（用户视角：runner / 远端故障，区别于业务问题）
- `3` = 参数 / 配置错误（用户视角：用户自己错了）

**理由**：

- 与 `hostlens doctor` 已有 exit code 风格（`0` 健康 / `1` 配置问题 / `2` 自身错误 / `3` 用户参数错）严格对齐——见 `cli/doctor.py` 与 `cli/_doctor_schema.py`
- 区分"业务发现严重问题（exit 1）"与"runner 内部失败（exit 2）"是 CI / 监控脚本的核心诉求：前者要 page on-call，后者要 retry
- 区分"用户参数错（exit 3）"与"runner 内部失败（exit 2）"是为了 CI script 区别处理（exit 3 永远不该 retry）
- POSIX 约定 exit 2 = misuse of shell command（如 `grep` 的"找不到"），exit 3 是用户自定义；本方案借用 exit 2 给"runner 内部失败"是有意为之——`hostlens` 把"runner 内部失败"视为环境异常而非用户错
- 替代方案 A：**只用 0/1**——丢失"业务严重 vs 环境异常"区分，CI 无法 retry 决策
- 替代方案 B：**0/1/2/3/4/5 细分**（如 timeout / target_unreachable / requires_unmet 各占一码）——增加 CI script 复杂度；status 信息已经在 stdout 的 Report 里，exit code 只做粗粒度分类

**critical 优先级**：当 inspector status != "ok" **且** 同时有 critical finding 时，应返回 exit 2 还是 exit 1？

- **选择**：exit 2（runner 失败优先）
- **理由**：findings 是 runner 在 status="ok" 路径上生成的；status != "ok" 时 findings 列表通常为空（除非 inspector 的 finding DSL 在 preflight 阶段就触发了——但当前 M1 finding DSL 在 collect 之后才求值），所以这个冲突场景实际上极少；选 exit 2 是因为"环境异常"对 CI 而言更需要立即介入

### 决策 4：新增独立 `report-data-model` capability spec + 新增 `inspect-cli-command` capability spec

**选择**：

- 新增 `openspec/specs/report-data-model/spec.md`：覆盖 `Severity` / `Evidence` / `Finding` / `Report` 数据模型 + `render_markdown` / `render_json` 渲染契约
- 新增 `openspec/specs/inspect-cli-command/spec.md`：覆盖 `hostlens inspect` CLI 命令的参数 / 退出码 / 错误处理
- 修改 `openspec/specs/inspector-plugin-system/spec.md`：仅 §需求:Finding 数据模型字段集（M1 与 M2 严格对齐）—— Finding 的 SOT 指向 `report-data-model`，evidence 类型从 `dict[str, str]` 改为 `list[Evidence]`；其他场景**不动**

**理由（为什么 3 个独立 spec 而非合并）**：

- `report-data-model` 是 M2 / M3 / M5 三个后续提案的共同消费契约——独立 spec 让扩展点清晰锚定（M3 给 Finding 加 id 字段时 modify 这个 spec；M5 给 Report 加 metadata 路由字段时 modify 这个 spec）
- `inspect-cli-command` 是 CLI 行为契约，与数据模型契约关注点分离——M2 提案不会改 `inspect-cli-command`（M2 加 `--intent` 是 modify 这个 spec），但会重度消费 `report-data-model`
- `inspector-plugin-system` 已经 978 行，再塞 Finding 模型扩展 + Report 容器 + CLI 命令会让单 spec 失焦
- 替代方案 A：**全部合并到 `inspector-plugin-system`**——违反 OpenSpec capability 单一关注点原则
- 替代方案 B：**只新增 `report-data-model`，CLI 合并到 `cli-foundation`**——`cli-foundation` 当前只覆盖 `doctor` 命令的"CLI 框架行为"（Typer 设置 / multi-command mode / EUID 工具函数等），把业务命令塞进去会让 spec 失焦；`inspect` 命令未来 M2 加 `--intent` 时也需要独立扩展点
- 替代方案 C：**`inspect` 命令合并到 `inspector-plugin-system`**——`inspectors list/show` 当前在 `inspector-plugin-system` spec 是因为它们直接暴露 Inspector registry；但 `inspect` 命令是"用 Report 容器跑一次端到端巡检"，关注点是 Report 渲染，不是 Inspector registry——合并会混淆边界

## 风险 / 权衡

| 风险 | 缓解措施 |
|---|---|
| BREAKING：`Finding.evidence` 由 `dict[str, str]` 改为 `list[Evidence]`，可能漏改 archived `inspector-plugin-system` spec 的测试 / `default_tools.py:155` 的 handler 投影逻辑 | spec delta 在 `inspector-plugin-system` 显式 MODIFIED Requirements（含 hello.echo 场景的 `evidence={...}` → `evidence=[]` 修正）；tasks.md §5.3 显式列出"修改 default_tools.py `_run_inspector_handler` 把 dict comprehension 改为 `findings=list(result.findings)` 直接复用"；CI 通过 mypy --strict 兜底（旧 dict 形式无法 validate 进 `list[Evidence]`） |
| BREAKING：新增 `Finding.tags: list[str] = []` 字段（兑现 CLAUDE.md §4.4 M5 Notifier 路由契约） | 字段有默认值（空 list），M1 finding DSL 不生产 tags 对所有现存测试透明；M5 提案在不改 Finding schema 的前提下消费 tags 做路由 |
| **循环导入风险**：`reporting.models.Report` ↔ `inspectors.result.Finding` 形成依赖回环 | §决策 1 末尾详述实现约束（TYPE_CHECKING + 字符串前向引用 + `inspectors/result.py` 末尾 `Report.model_rebuild()`）；tasks.md §2.5 加测试 `pytest tests/reporting/test_no_circular_import.py`：clean import `hostlens.reporting` 不报错，clean import `hostlens.inspectors.result` 也不报错；CI 矩阵跑 `python -c "import hostlens.inspectors.result"` 与 `python -c "import hostlens.reporting; import hostlens.inspectors.result; ..."` 两种顺序均成功 |
| `Evidence` 单平面字段模型 + model_validator 的 kind ↔ 字段映射，初次 review 可能觉得"为什么不用 discriminated union" | 在本文件 §决策 2 显式列出替代方案 + 理由；测试用例覆盖 4 种 kind × （合法字段集 / 非法字段集）矩阵；docstring 给每个 kind 写一个最小构造示例 |
| 渲染器纯 Python f-string 拼接，未来若需要 i18n / 主题切换会重写 | M1 范围明确不做 i18n / 主题；M3 引入 Diagnostician 时 `## Findings` 章节才会有"根因假设"等富语义，那时再评估是否升级渲染器；当前 ≤ 200 行的实现成本远低于引入模板引擎 |
| CLI exit code 4 值语义在 critical finding + status="ok" 边界（同时有 critical finding 与 runner 失败时哪个优先）容易引发误解 | §决策 3 明确选 exit 2 优先；测试用例覆盖该边界；docs/operations/inspect.md 给出 CI scriptlet 示例（`case "$exit_code" in 0) ...; 1) page;; 2) retry;; 3) error;; esac`） |
| `--output` 文件静默覆盖可能导致用户误删 | proposal Failure Modes 已记入"已知接受风险"；docs/operations/inspect.md 文档化"建议用 `--output` 指向 `/tmp` 或带时间戳的路径"；与 POSIX 工具行为一致 |
| `Evidence.command` 模板字符串渲染时若误展开 env var 会泄露 secret | 渲染器**禁止**展开 env var；测试用例覆盖"命令含 `$PGPASSWORD` 时 md/json 都输出 `$PGPASSWORD` 字面量"的断言 |
| markdown 渲染时 stdout/stderr 含 ANSI escape 可能污染 terminal | 渲染器对所有 evidence 文本字段做 control char escape（保留 `\n` / `\t`）；测试用例覆盖 ANSI escape payload |
| 新增独立 `report-data-model` spec 会让 OpenSpec specs 目录数量增加（当前 8 个 → 10 个） | OpenSpec 工作流支持的最大 capability 数量没有硬限制；增长是项目复杂度的合理映射；M3 / M5 提案进一步增加 spec 数量是预期路径 |
| `Report.from_inspector_results` 工厂方法是唯一推荐构造路径，但 Pydantic 默认允许 `Report(**dict)` 直接构造 | 不阻塞——`Report` 是 frozen + extra=forbid 的 Pydantic 模型，直接构造是合法且符合 Pydantic 习惯；`from_inspector_results` 只是为了"自动 flatten findings + 自动生成 report_id + 自动锁定 schema_version"的便利封装；docstring 推荐使用 |
| 平面 Evidence 模型的 model_validator 在字段集变更（M3 加新 kind）时容易遗漏新增的 ↔ 字段映射 | spec 中把映射表作为正文，model_validator 实现作为映射表的代码翻译；测试矩阵 4 种 kind × 字段集断言覆盖；CI 测试矩阵参数化 |

## 迁移计划

本提案是**纯增量 + 1 个有界 BREAKING**，无需多阶段迁移：

1. **Phase 1（实现）**：feature branch `feat/add-report-data-model` 上：
   - 写 `hostlens.reporting.models` 与渲染器 + 单测
   - 修改 `hostlens.inspectors.result.Finding` 与 `hostlens.tools.schemas.run_inspector.FindingSummary` 为 type alias
   - 写 `hostlens.cli.inspect` 与单测 + 集成测试
   - 写 syrupy snapshot 测试覆盖 md 渲染
2. **Phase 2（spec 同步）**：
   - 新增 `openspec/specs/report-data-model/spec.md`（从本提案 spec delta 派生）
   - 新增 `openspec/specs/inspect-cli-command/spec.md`
   - MODIFY `openspec/specs/inspector-plugin-system/spec.md`：Finding 字段集场景
3. **Phase 3（PR + review）**：feature branch 完成后跑对抗性 review（CLAUDE.md §5.3），review APPROVE 后开 PR squash 到 main
4. **回滚策略**：若 PR merge 后发现 BREAKING 影响超出预期，可 revert 整个 squash commit；`hostlens.reporting` 是新模块，回滚不影响 M0-M1 已落地代码（除了 Finding type alias 的 revert——但既然 M2 / M3 都要消费新 Finding，回滚价值低）
5. **OpenSpec archive**：实施完成后用 `/opsx:archive` 把变更目录归档到 `openspec/changes/archive/2026-xx-xx-add-report-data-model/`

## Open Questions

- **Q1**：`Evidence.metric_value` 类型 `float | str | None`——是否应该把 `int` 也加入？**当前选择**：不加；JSON Schema 中 `int` 是 `float` 的子集，Pydantic v2 会自动 coerce `int` → `float`（除非显式 `strict=True`）。如果 M3+ 出现需要保留 int 精度的指标，再调整。
- **Q2**：`Report.report_id` 用 `uuid4()` 还是某种确定性 hash（如 `sha256(target_name + inspector_name + started_at)`）？**当前选择**：`uuid4()`；理由：M3 regression diff 才需要确定性 ID，且那时应该用 `(target, inspector, finding fingerprint)` 三元组而不是 report 级 ID。
- **Q3**：`render_markdown` 是否需要支持"无 unicode 表格"模式（用 ASCII 字符）？**当前选择**：不需要；GFM markdown 在所有终端 / GitHub / 飞书富文本中都能正确渲染表格。
- **Q4**：CLI `--format` 是否应该支持 `--format both`（同时输出 md + json）？**当前选择**：不需要；CLI 用 `&&` 跑两次更简洁，且单一格式输出更符合 POSIX 工具习惯。
- **Q5**：是否需要在 `Report` 中记录 `hostlens` CLI version（如 `hostlens_version: str = "1.0.0a1"`）？**当前选择**：不需要 M1；M3 regression diff 需要时再加（add-only 扩展兼容）。
