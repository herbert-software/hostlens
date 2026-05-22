# Hostlens 项目说明（给 AI 协作者读）

> 这是 Hostlens 项目的本地约定。所有在本仓库工作的 AI（Claude Code / Codex / Cursor 等）都应该遵守。
> 全局 `~/.claude/CLAUDE.md` 的规则继续生效，本文件是**项目层增量**，冲突时以本文件为准。

---

## 1. 项目愿景（一段话）

Hostlens 是一个 **LLM 驱动的服务器巡检 Agent**：用户用自然语言描述意图，Agent 自动规划→并行采集→关联分析→输出带根因假设的报告，并按调度定时把报告推送到 Telegram / 飞书等多通道。区别于 Zabbix/Prometheus 的「规则匹配 + 告警」，定位是「**理解意图 + 推理诊断**」。双交付形态：**CLI + MCP Server**。

终极目标：可写在简历上的、面试官打开 `src/hostlens/agent/loop.py` 能直接看到「Agent 工作原理」的项目。

---

## 2. 技术栈（已锁定，不要拍脑袋替换）

| 类别 | 选型 | 红线 |
|---|---|---|
| 语言 | Python 3.11+，async-first | 同步 CPU 工作用 `asyncio.to_thread` |
| LLM | Anthropic SDK 原生 | **禁用 LangChain / LlamaIndex** —— 手写 Agent loop 是核心展示点 |
| 数据建模 | Pydantic v2 | 全程强类型；structured output 走 Pydantic |
| 远程执行 | AsyncSSH / docker-py / kubernetes | 必须异步 |
| CLI | Typer + Rich | 子命令式，`<verb> [args] [--json] [--yes]` |
| MCP | 官方 `mcp` SDK | 不要自己造 MCP 协议 |
| 调度 | APScheduler | 不要用 cron + shell 包脚本，进程内调度可控可测 |
| 通知模板 | Jinja2 | 飞书卡片、Telegram MarkdownV2 走模板 |
| 测试 | pytest + pytest-asyncio + VCR | LLM 调用走 cassette 回放 |
| 可观测 | structlog + OpenTelemetry | Agent 调用链可追踪 |

任何新增依赖前自问：**是否值得多一个依赖？标准库或现有依赖能否做到？**

---

## 3. 目录结构（计划）

```
hostlens/
├── src/hostlens/
│   ├── agent/              # Planner / Diagnostician / Remediation 三个 Agent
│   │   ├── loop.py         # 手写 tool-use loop（含 prompt caching、重试、token 预算）
│   │   ├── backend.py      # LLMBackend Protocol + AnthropicAPIBackend + FakeBackend (M2)
│   │   ├── backends/       # 其他 backend 实现 (Bedrock M10.5 / Vertex 1.0+ / Subscription M10.5 experimental)
│   │   ├── planner.py
│   │   ├── diagnostician.py
│   │   ├── tools_adapter.py  # ToolSpec → Anthropic tool_use schema 投影
│   │   └── prompts/        # 系统提示词 markdown
│   ├── inspectors/         # Inspector 加载器与注册中心
│   │   ├── registry.py
│   │   ├── loader.py       # YAML manifest 解析
│   │   └── builtin/        # 内置 Inspector（linux / nginx / mysql / redis / docker / k8s ...）
│   ├── targets/            # ExecutionTarget 接口与实现
│   │   ├── base.py
│   │   ├── ssh.py
│   │   ├── local.py
│   │   ├── docker.py
│   │   └── k8s.py
│   ├── scheduler/          # APScheduler 封装、Schedule manifest 加载
│   ├── notifiers/          # Notifier 抽象 + 各平台适配器
│   │   ├── base.py         # Notifier Protocol + Channel 注册表
│   │   ├── telegram.py
│   │   ├── lark.py
│   │   ├── templates/      # Jinja2 模板（按通道分子目录）
│   │   └── ...             # dingtalk.py / wecom.py / slack.py / email.py / webhook.py 未来扩展
│   ├── remediation/        # 受控修复：plan / approve / execute / rollback
│   ├── reporting/          # 报告渲染（md / html / json）+ regression diff
│   ├── mcp_server/         # MCP 服务端
│   ├── cli/                # Typer 命令（target / inspect / inspectors / schedule / notify / doctor / mcp）
│   └── core/               # 配置、日志、异常、密钥管理
├── inspectors/             # 用户/社区可放外部 inspector（不进 pip 包）
├── schedules/              # 定时任务 YAML
├── tests/
├── docs/
└── openspec/               # spec-driven 工作流
```

---

## 4. 核心设计约定（必读）

### 4.1 Agent loop 必须自己实现

不要 `from langchain import ...`。Agent loop 通过 `LLMBackend` Protocol（见 §4.11）跑 tool-use 循环 —— Protocol 是 Anthropic-schema-first 的薄抽象，不是 vendor-agnostic 通用 LLM 包装层。理由：

- **简历可读性**：HR/面试官打开 `agent/loop.py` 能直接看到 Agent 工作原理
- **掌控力**：prompt caching、错误重试、token 预算、超时取消都要可控
- **依赖最小**：不被框架升级绑架

### 4.2 Inspector 是 SOT，Agent 是调度者

- 每个 Inspector 一个 YAML manifest（必填：`name` / `version` / `description` / `targets` / `collect` / `output_schema`）
- 复杂逻辑可选配 Python hook（同目录 `hook.py`）
- Agent 只决定「调用哪些 inspector、按什么顺序、怎么合并结果」，**不在 prompt 里写死巡检步骤**
- 新增简单检查项 = 加一个 YAML；复杂解析逻辑通过同目录可选 `hook.py` 接管（不是"永远零代码"，是"YAML 处理 80% 简单场景，hook.py 是逃生舱"）

### 4.3 ExecutionTarget 统一抽象

```python
class ExecutionTarget(Protocol):
    async def exec(self, cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> ExecResult: ...    # cmd 是 shell-evaluated; env 是 secrets 注入路径
    async def read_file(self, path: str) -> bytes: ...
    @property
    def capabilities(self) -> set[Capability]: ...
```

Inspector 通过 `target.capabilities` 决定能不能跑（例如 docker target 没有 `Capability.SSH`）。新 target = 实现一个 Protocol，不改 Inspector。

### 4.4 Notifier 必须是抽象 + 适配器

这是「业务通用化、可扩展」的核心证明点。**任何「加一个新通道」的需求**都必须只新增一个文件、不改主流程：

```python
class Notifier(Protocol):
    name: str                                                 # 注册到 channel registry 的 key
    def validate_config(self, cfg: dict) -> None: ...         # 启动时校验配置
    def render(self, report: Report) -> NotifyPayload: ...    # Jinja2 模板渲染产生 channel-native payload
    async def send(self, payload: NotifyPayload) -> NotifyResult: ...   # 接收已渲染 payload, 处理重试/限流/签名
```

- **初始实现**：Telegram、飞书 Lark（含签名校验）
- **预留扩展**：钉钉、企业微信、Slack、Email、通用 Webhook
- **Channel 配置**：`~/.config/hostlens/notifiers.yaml`，支持 `${ENV_VAR}` 占位
- **模板**：每个通道一套 Jinja2 模板（飞书卡片 / TG MarkdownV2 / DingTalk Markdown ...）
- **路由**：`only_if` 表达式（基于报告 severity / finding tags）决定是否发送

**反模式**：把 Telegram/飞书的发送代码直接写在 Reporter 或 CLI 里。这会让"加钉钉"变成大手术。

### 4.5 写操作的硬约束

任何会改变远端状态的操作（Remediation / Notifier 配置修改 / target 凭据写入）必须走：

```
plan (Agent 生成) → preview (人工看 diff) → approve (显式 --yes 或交互确认) → execute → verify → rollback-ready
```

- CLI 默认 `--dry-run`
- 非交互环境（无 TTY）缺 `--yes` **直接退出 1**，不要默默成功
- 继承全局 CLAUDE.md：**写操作必须拒绝 root（EUID==0）**

### 4.6 Scheduler 不是黑盒

- 调度任务 YAML 是 SOT，存在 `schedules/` 目录
- 每次触发必须留下：`run_id`、触发时间、目标、Inspector 集合、报告 hash、通知发送结果
- `hostlens schedule list` 必须能看到 next_fire_time，方便排错
- daemon 模式必须支持优雅停机（SIGTERM → 等当前任务跑完再退）

### 4.7 结构化输出

所有 LLM 输出走 Pydantic schema（Anthropic 的 tool_use 模拟 structured output），不要 prompt 工程让模型「返回 JSON」再用 `json.loads` 解析。

### 4.8 Prompt caching 是必修

- 系统提示词、Inspector schema 列表、固定 few-shot 必须缓存（`cache_control: ephemeral`）
- 任何超过 5 次 LLM 调用的功能都要看 cache hit rate
- 写完任何调 LLM 的代码，自查「是否启用 prompt cache」
- **若 `backend.capabilities.prompt_caching=False`（如未来接的非 Anthropic backend），Agent loop 必须不注入 `cache_control` block** —— 由 loop 端检查 capability，不允许 backend 端静默丢弃后假装成功（会让 cache hit rate 指标失真）

### 4.9 doctor 子命令

继承全局 CLAUDE.md 的范式。`hostlens doctor` 必须检查：

- Python 版本
- 必需环境变量（`ANTHROPIC_API_KEY` 等）
- 各 target 连通性（按需）
- 各通道连通性（`--check-channels`）
- Inspector 加载错误
- 配置文件可读性
- 提供 `--json` 输出，方便 Agent ping

### 4.10 Tool Registry — Agent ↔ 能力的唯一入口（双层 capability 模型）

> **M2 实施前必须按此设计立 OpenSpec proposal**。

Hostlens 里"Agent 能主动调用的能力"统一通过 **双层 Capability Registry** 暴露：

**Layer 1 —— Capability Spec（host-agnostic）**：

```python
class ToolSpec(BaseModel):
    name: str
    version: str
    input_schema: type[BaseModel]      # Pydantic；JSON Schema 由 adapter 在投影时生成
    output_schema: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[BaseModel]]

    # 三个 surface 文案分开（人因诉求不同）
    agent_description: str             # 给 Anthropic tool_use
    mcp_description: str               # 给远程 LLM
    cli_help: str | None               # 给人类（None = 不暴露 CLI）

    # 策略元数据（policy gate，不是 hint）
    surfaces: set[Literal["agent", "mcp", "cli"]]
    side_effects: Literal["none", "read", "write", "destructive"]
    requires_approval: bool = False
    permissions: set[str] = set()
    sensitive_output: bool | None = None      # 必须显式声明; adapter 在 MCP 投影时拒绝 None
    target_constraints: set[str] | None = None
    timeout: float | None = None
    tags: set[str] = set()
```

> `sensitive_output` 故意默认 `None` 而不是 `False`：`False` 会让"忘记声明"和"显式声明无敏感输出"无法区分，破坏"不显式即拒绝"语义。

**Layer 2 —— Surface Adapter**：

- `agent/tools_adapter.py` → `surfaces ∋ "agent"` → Anthropic `tool_use` schema
- `mcp_server/tools_adapter.py` → `surfaces ∋ "mcp"` → MCP tool definition
- `cli/tools_adapter.py` →（可选）`surfaces ∋ "cli"` → Typer command

**依赖注入（强制）**：handler 通过 `ToolContext` 拿依赖，禁止从 module-level singleton 取：

```python
@dataclass
class ToolContext:
    target_registry: TargetRegistry
    inspector_registry: InspectorRegistry
    config: Settings
    logger: structlog.BoundLogger
    approval_service: ApprovalService | None
    cancel: asyncio.Event
```

**6 条硬规则**：

1. `surfaces` 是 policy gate 不是 hint —— 多注册一个 surface = 一次显式的安全决定
2. `ToolSpec` 不存 host 专有 JSON Schema —— 一律由 adapter 在投影时从 Pydantic 生成
3. 新增 Agent 可调用能力必须走 `@tool` 注册，不允许 prompt 里写死或绕过 registry 直调函数
4. **Notifier 不进 Tool Registry** —— 它是 Scheduler / Reporter 触发的输出通道，不是 Agent 主动调用的能力
5. 危险操作必须 `side_effects ∈ {write, destructive}` 且 `requires_approval=True`，adapter 在 dispatch 前强制校验
6. MCP 暴露的工具必须显式声明 `sensitive_output`，缺省禁止暴露

**反模式**：

- ❌ 用单个 `visibility: set[str]` 替代上面的策略元数据集 —— 软分类一定失控
- ❌ 三个 surface 共享一份 description —— 人因诉求不同（CLI 给人 / Agent 给本地 LLM / MCP 给远程 LLM），必须分开
- ❌ Handler 里 `from hostlens.targets.registry import TARGET_REGISTRY` —— 必须从 `ctx` 拿，否则 registry 退化成 service locator
- ❌ 把 Inspector / Notifier / Target 塞进 ToolSpec —— 它们是业务插件不是 Agent capability
- ❌ M2 上来就实现三个 surface adapter —— M2 只做 Layer 1 + Agent adapter，MCP adapter 到 M7

### 4.11 LLMBackend — 模型层抽象 (Agent Loop 私有依赖)

> 详细 Protocol / 实现矩阵 / ToS 风险表见 [docs/ARCHITECTURE.md §9 模型层](docs/ARCHITECTURE.md#9-agent-loop)。本节是约束总结。

**Protocol（Anthropic-schema-first，不是 vendor-agnostic 通用包装）**：

```python
class LLMBackend(Protocol):
    name: str
    capabilities: BackendCapabilities          # prompt_caching / tool_use / structured_output / parallel_tool_use / extended_thinking / vision / streaming (7 字段; 按需扩展)
    async def messages_create(*, model, system, messages, tools, max_tokens, timeout) -> MessageResponse: ...
```

**实现矩阵（按场景选）**：

| 场景 | Backend | 何时用 |
|---|---|---|
| 默认 / 个人开发 | `AnthropicAPIBackend` | `ANTHROPIC_API_KEY`，最简单 |
| 企业生产 (推荐) | `BedrockBackend` (M10.5) | AWS IAM，ToS 干净，audit 完整 |
| GCP 企业 (1.0 后) | `VertexBackend` | GCP Service Account |
| 测试 (单元) | `FakeBackend` | 固定响应 mock |
| 测试 (集成 / replay) | `PlaybackBackend` | VCR cassette 回放，CI 必备 |
| 实验 / demo (**禁生产**) | `ClaudeSubscriptionBackend` | OAuth 订阅；daemon 模式强制 raise |

**4 条硬规则**：

1. **Backend 注入 `AgentLoop.__init__`，不进 `ToolContext`** —— 防止 tool handler 拿 backend 后违反「Inspector 不能调 LLM」 (ADR-008)
2. **`cache_control` 由 Agent loop 在调用前根据 capability 决定是否注入** —— backend 严格透传不做静默丢弃；不一致时 backend 必须 raise `BackendCapabilityViolation` 暴露 bug（不假装成功，否则 cache hit rate 指标失真）
3. **`ClaudeSubscriptionBackend` 在 daemon 模式必须强制 raise** —— 不只是 warn；通过 `BackendDiagnostics.ensure_safe_for_daemon()` 实现
4. **配置 `backend:` 与 `agent:` 分两个 namespace** —— backend 管「与谁通信 / 如何认证」，agent 管「用哪个模型 / 行为参数」

**反模式见 §7**。

---

## 5. OpenSpec 工作流

本项目使用 [OpenSpec](openspec/) 做 spec-driven 开发：

- **新增能力**先提案：用 `openspec` skill 写 proposal / design / spec / tasks，再实现
- **改动已有契约**（Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest schema / CLI 命令）必须更新对应 spec
- **实现完成后归档**：把 `openspec/changes/` 下的提案推进到 `openspec/specs/`
- **proposal 必须有「非目标」**，防止范围蔓延

### 5.1 Git 分支与 PR 工作流（强制）

`main` 分支已设 **branch protection**，**禁止直接 push**。任何代码 / 文档 / 配置改动必须走：

1. **从最新 main 切 feature branch**：
   ```bash
   git checkout main && git pull origin main
   git checkout -b <type>/<short-kebab-name>
   ```
   分支命名约定（与 OpenSpec change name 对齐）：
   - `feat/<change-name>` —— 新功能 / 新提案实施（如 `feat/add-tool-registry-capability-layer`）
   - `fix/<change-name>` —— bug 修复
   - `docs/<short-name>` —— 仅文档
   - `chore/<short-name>` —— 工具配置 / dependabot 等
   - `refactor/<short-name>` —— 重构无行为变更

2. **commit + push branch**：
   ```bash
   git add <explicit files>     # 禁用 git add -A 避免误带本地配置/secrets
   git commit -m "<conventional commit msg>"
   git push -u origin <branch>
   ```

3. **开 PR 到 main**：用 `\gh pr create --base main --title "..." --body "..."`；PR 描述含 spec 引用（`openspec/changes/<change-name>/`）与 Demo Path

4. **等 CI 全绿 + review**（如适用）后 **squash merge 到 main**：
   ```bash
   \gh pr merge <num> --squash --delete-branch
   ```

5. **每个 OpenSpec change 一个 feature branch**：M0 之后所有 OpenSpec 提案（add-tool-registry-capability-layer / add-llm-backend-protocol / add-agent-loop-skeleton 等）都按此走；**不允许**多个提案合到同一 branch 混淆 PR scope

**反模式**：

- ❌ **直接 push 到 main**（branch protection 在 push 阶段拒绝，触发 fatal error；注意 git 分布式，本地 commit 永远不会被拒，但 `git push origin main` 必失败 —— 必须走 feature branch + PR）
- ❌ 一个 branch 同时改两个不相关提案（拆分 PR）
- ❌ commit message 与 branch 名 / OpenSpec change name 不对齐（让 PR 历史可追溯）
- ❌ PR 不写 spec 引用 / Demo Path（reviewer 无法快速验证）
- ❌ merge commit 而非 squash（保持 main history 线性，每个 PR 一个 commit 易于回滚 / cherry-pick）
- ❌ **对 dependabot PR 用 `@dependabot squash and merge` / `@dependabot merge`** —— 这两个指令把合并权交给 dependabot，绕开人类 review；只允许 `@dependabot rebase`（让 dependabot 把 PR rebase 到最新 main 触发 CI 重跑），CI 绿后**人类**用 `\gh pr merge <num> --squash --delete-branch` 手动合并

### 5.2 Dependabot PR 处理流程（dependabot 也走 PR，不是例外）

GitHub dependabot 自动开 PR 升级依赖，但 **dependabot PR 与人类 PR 走相同流程，没有自动合并特权**：

1. **CI 红时**：发 `@dependabot rebase` 指令（让 dependabot 把 PR rebase 到最新绿 main，CI 重跑）
2. **CI 绿后**：人类 review PR diff（dependabot 通常只改 1-3 行 yaml / pyproject 但仍需快速 scan）
3. **合并**：人类用 `\gh pr merge <num> --squash --delete-branch`（**不要**用 `@dependabot squash and merge`，那会让 dependabot 在 CI 绿后自动 merge 绕开人类 review）

风险分级：
- **低风险**（patch / minor 升级、GH Actions、stdlib hooks）：CI 绿即可合
- **中风险**（major bump 但只影响 dev / test 环境如 mypy `additional_dependencies`）：CI 绿 + spot check 改了什么
- **高风险**（major bump 影响 runtime 如 pydantic / typer / structlog 的 pyproject dependencies）：CI 绿 + 手动跑一次 demo path

---

## 6. 代码风格

- **async-first**：所有 IO 用 async；同步 CPU 工作用 `asyncio.to_thread`
- **No global state**：依赖通过构造器注入，方便测试
- **类型完整**：`mypy --strict` 必须过；不允许 `Any`（除非有清晰注释说明为什么）
- **错误处理只在边界做**：内部函数信任调用方，不要每个函数都 try/except 吞异常
- **不写无意义注释**：注释只写「为什么」，不写「是什么」
- **不写防御性 fallback**：不要给「不可能发生的分支」加兜底
- **测试用真实 fixture**：LLM 用 VCR cassette；SSH 用 docker 容器跑真 sshd（不要 mock paramiko）

---

## 7. 反模式清单（看到就纠正）

- ❌ 把「巡检步骤」写死在 system prompt 里 —— 应该让 Agent 从 Inspector registry 里选
- ❌ Inspector 里直接调 LLM —— Inspector 只采集 + 结构化，推理留给 Agent
- ❌ 给 Agent 暴露危险工具（`exec_arbitrary_command`）—— 只暴露受限的 Inspector + 受审批的 Remediation Plan
- ❌ 用 LangChain / 框架代替手写 loop —— 这是项目核心展示点
- ❌ 把通知发送代码直接写在 Reporter / CLI 里 —— 必须走 Notifier 抽象
- ❌ 在 Notifier 实现里硬编码模板字符串 —— 用 Jinja2 模板文件
- ❌ 把 webhook URL / bot token 写进代码或 commit —— 走 `${ENV_VAR}` 或本地密钥文件
- ❌ 给简单功能堆 MCP server —— MCP 服务端只暴露经过设计的「对 Agent 友好」的工具集
- ❌ 在代码注释里写本次任务背景（"P1 修复"/"review 反馈加的"）—— 这些放 commit message / PR description
- ❌ 把 markdown / 提案 / 决策文档写进 `src/` —— 那些放 `docs/` 或 `openspec/`
- ❌ **把 LLMBackend 放进 `ToolContext` 或 Tool Registry** —— Backend 是 AgentLoop 私有依赖（ADR-008）；放进 ToolContext 等于让任何 Inspector handler 拿到 backend 后自己调 LLM，破坏 §4.2「Inspector 不能调 LLM」红线
- ❌ Tool handler 通过 `ctx.llm_backend` 调 LLM —— 同上，handler 该用的是 Inspector / Remediation 等已有抽象
- ❌ 在生产 daemon 模式使用 `ClaudeSubscriptionBackend` —— 订阅是 dev/demo only，daemon 启动时 `BackendDiagnostics.ensure_safe_for_daemon()` 必须强制 raise
- ❌ 把 `LLMBackend` 包装成「provider-agnostic 通用 LLM 抽象」—— 那是 LangChain / LiteLLM 的事；Hostlens 的 backend 明确 Anthropic-schema-first

---

## 8. 沟通约定

- **中文优先**（用户母语），技术术语保留英文
- 任何「业务通用化」改动先在 OpenSpec 起 proposal，避免范围蔓延
- 简历项目的优先级：**架构清晰度 > 功能广度 > 性能极致**

---

## 9. 当前阶段

项目刚启动，仓库里已有这些文档：

- `README.md` —— 项目简介与快速开始
- `CLAUDE.md` —— 本文件（AI 协作者约定）
- `TODO.md` —— 10 期开发路线（M0-M10）
- `docs/ARCHITECTURE.md` —— 完整架构 + ADR
- `docs/OPERABILITY.md` —— 生产部署与运维约束（并发预算 / SSH 复用 / API 配额 / 存储保留 / 密钥脱敏 / 降级路径 / 已知限制）
- `openspec/` —— spec-driven 工作流配置

**没有任何 `src/` 代码**。下一步推荐顺序：

1. M0「项目脚手架」前先用 OpenSpec 起 `bootstrap-project-skeleton` proposal
2. 完成 M0 后、M1 之前先起 `add-tool-registry-capability-layer`（M2 会消费它）
3. 然后按 TODO.md 的 M1-M10 顺序推进，每期开始前都先 propose

**不要跳过 spec 直接写代码** —— 这是项目唯一的工程纪律红线。
