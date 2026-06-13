# Hostlens 开发路线（TODO）

> 项目分期路线图。每一期对应一个里程碑（M0–M10），完成后形成一个可演示的状态。
>
> **工作流约定**：每一期开始前必须先用 OpenSpec 起 proposal（用 `openspec-propose` skill），proposal 通过后再实施任务。**不允许跳过 spec 直接写代码。**
>
> **节奏感**：M0–M2 是骨架期，必须最快速度跑通"自然语言意图 → 出报告"的最小闭环；M3–M5 把"定时巡检 + 多通道通知"这个差异化卖点立起来；M6 之后是扩展期，按需推进。

---

## 进度总览

| 期 | 里程碑 | 核心交付 | 状态 |
|---|---|---|---|
| M0 | 项目脚手架 | 可跑 `hostlens doctor` 的空骨架 | ✅ |
| M1 | Core 抽象 + 最小管线 | `hostlens inspect localhost --inspector hello` 跑通 | ✅ |
| M2 | 手写 Agent loop | 自然语言意图 → Agent 自选 Inspector → 出 markdown 报告 | ✅ |
| M3 | Diagnostician + 报告体系 | 跨信号关联 + 根因假设 + regression diff | ✅ |
| M4 | Scheduler | cron 定时跑 + 历史 run 持久化 | ✅ |
| M5 | Notifier 抽象 + Telegram + 飞书 | 定时报告自动推送到 TG / 飞书 | ✅ |
| M6 | 内置 Inspector 库扩充 | 覆盖 Linux/Nginx/MySQL/Redis/Docker 真实场景 | ✅ |
| M7 | MCP Server | Claude Code / Cursor 能直接调用 Hostlens | ✅ |
| M8 | Docker + K8s ExecutionTarget | 容器与集群场景 | ✅ |
| M9 | 受控修复（Remediation） | plan → approve → execute → rollback 闭环 | ✅ |
| M10 | 通道扩展 + 文档发布 | 钉钉/企微/Slack/Email/Webhook + PyPI 1.0 | ⬜ |

---

## 已完成 milestone 详情

### M0 — 项目脚手架 ✅

**对应 OpenSpec change**：[`add-bootstrap-project-skeleton`](openspec/changes/archive/2026-05-22-bootstrap-project-skeleton/)（已 archive）

**验证**：`hostlens doctor --json | jq -e '.ready != null'` 输出合法 JSON；CI matrix [3.11, 3.12] 全绿。

### M1 — Core 抽象 + 最小可跑通管线 ✅

**对应 OpenSpec changes**（4 个 archived）：
- [`add-execution-target-abstraction`](openspec/changes/archive/2026-05-25-add-execution-target-abstraction/)（M1.1 + M1.2 — ExecutionTarget Protocol + LocalTarget + SSHTarget + `hostlens target add/list/remove`）
- [`add-tool-registry-capability-layer`](openspec/changes/archive/2026-05-25-add-tool-registry-capability-layer/)（M2 前置 — 双层 Capability Registry，M1 阶段先落地）
- [`add-inspector-plugin-system`](openspec/changes/archive/2026-05-26-add-inspector-plugin-system/)（M1.3 + M1.4 + M1.5 — InspectorManifest schema + loader + runner + Finding DSL + 内置 hello.echo / system.uptime + `hostlens inspectors list/show`）
- [`add-report-data-model`](openspec/changes/archive/2026-05-26-add-report-data-model/)（M1.6 + M1.7 — Report / Finding / Severity / Evidence 模型 + render_markdown / render_json + `hostlens inspect`）

**端到端 demo path**（5 分钟内 reproduce）：

```bash
pip install -e ".[dev]"
hostlens doctor --json | jq '.inspectors'                        # loaded: 2, errors: []
hostlens target add local-host --type local
hostlens inspect local-host --inspector hello.echo                # exit 0, md report on stdout
hostlens inspect local-host --inspector hello.echo --format json --output /tmp/r.json
hostlens inspect local-host --inspector nonexistent.foo           # exit 3, "inspector not found:"
HOSTLENS_INSPECTORS_SEARCH_PATHS=./examples/m1-report/inspectors \
  hostlens inspect local-host --inspector demo.sleep_timeout \
  --parameters '{"sleep_seconds": 30}' --timeout 1                 # exit 2, status=timeout
```

详见 [`examples/m1-report/README.md`](examples/m1-report/README.md) + [`docs/operations/inspect.md`](docs/operations/inspect.md)。

---

## M0 — 项目脚手架（Bootstrap）

**目标**：把空仓库变成"可装、可跑、可测、可 lint、可 doctor"的最小骨架。完成后不实现任何业务功能，但任何一个新增模块都有标准化的位置可放。

**对应 OpenSpec proposal**：`bootstrap-project-skeleton`

**退出条件**：`pip install -e .` 成功，`hostlens doctor --json` 能输出结构化结果，`pytest` 至少跑 1 个示例测试通过，CI 绿。

### 任务

- [x] **0.1 仓库布局**
  - [x] 创建 `src/hostlens/{agent,inspectors,targets,scheduler,notifiers,remediation,reporting,mcp_server,cli,core}/__init__.py`
  - [x] 创建顶层目录 `inspectors/`、`schedules/`、`tests/`、`docs/`
  - [x] 加 `.gitignore`（Python + macOS + venv + 本地配置）
  - [x] 加 `LICENSE`（Apache-2.0）
- [x] **0.2 打包与依赖**
  - [x] `pyproject.toml`：项目元数据、`[project.scripts] hostlens = "hostlens.cli:app"`
  - [x] 依赖分组：`core` / `mcp` / `dev`（lint+test）/ `docs`
  - [x] 锁定 Python `>=3.11`
  - [x] 验收：`pip install -e ".[dev]"` 成功，`hostlens --help` 输出
- [x] **0.3 代码质量工具链**
  - [x] 配置 `ruff`（lint + format，替代 black/isort/flake8）
  - [x] 配置 `mypy --strict`
  - [x] 配置 `pre-commit`：ruff、mypy、trailing-whitespace、yamllint
  - [x] 验收：`pre-commit run --all-files` 通过
- [x] **0.4 测试基线**
  - [x] 配置 `pytest`、`pytest-asyncio`、`pytest-cov`
  - [x] 写一个 dummy 测试验证 fixture/标记机制
  - [x] 验收：`pytest --cov` 跑通且有覆盖率报告
- [x] **0.5 CI（GitHub Actions）**
  - [x] workflow：lint + mypy + pytest（matrix: Python 3.11/3.12）
  - [x] 验收：本地 `act` 或 push 后 CI 绿
- [x] **0.6 配置与日志骨架**
  - [x] `core/config.py`：基于 `pydantic-settings`，支持 env + `~/.config/hostlens/*.yaml`
  - [x] `core/logging.py`：structlog 配置（dev=human、prod=json）
  - [x] `core/exceptions.py`：定义 `HostlensError` 基类与几个常用子类（`ConfigError` / `TargetError` / `InspectorError`）
- [x] **0.7 CLI 骨架 + doctor**
  - [x] `cli/__init__.py`：Typer app，注册子命令位
  - [x] `cli/doctor.py`：检查 Python 版本、`ANTHROPIC_API_KEY` 是否存在、配置目录可读、`--json` 输出
  - [x] 遵循全局 CLAUDE.md：写操作命令拒绝 root（EUID==0）的工具函数
  - [x] 验收：`hostlens doctor --json | jq .` 输出合法 JSON

---

## M1 — Core 抽象 + 最小可跑通管线

**目标**：跑通"在本地执行一个 Inspector → 拿到结构化结果 → 渲染成 markdown"的最小闭环。**不接 LLM**，纯机械管线，先验证抽象边界。

**对应 OpenSpec proposal**：
- `add-execution-target-abstraction`
- `add-inspector-plugin-system`
- `add-report-data-model`

**退出条件**：`hostlens inspect localhost --inspector hello.echo` 输出一份合法 JSON + 同样内容的 markdown 报告；新增一个 inspector = 只加 YAML 文件，零 Python 代码。

### 任务

- [x] **1.1 ExecutionTarget 抽象**（OpenSpec change `add-execution-target-abstraction` 落地）
  - [x] `targets/base.py`：`ExecutionTarget` Protocol、`ExecResult` dataclass、`Capability` 枚举
  - [x] `targets/local.py`：本地子进程实现，**使用 `asyncio.create_subprocess_shell`**（Inspector `collect.command` 含 pipe / redirect / 变量引用，必须走 shell 求值；安全边界由 manifest 渲染层负责，详见 ARCHITECTURE.md §4 命令渲染安全规则）
  - [x] `targets/registry.py`：target 注册与查找
  - [x] 验收：单测覆盖正常退出、非零退出、超时取消、stderr 捕获
- [x] **1.2 SSH Target**（OpenSpec change `add-execution-target-abstraction` 落地）
  - [x] `targets/ssh.py`：AsyncSSH 实现（key 认证为主，password 可选）
  - [x] 凭据加载：从 `~/.config/hostlens/targets.yaml` 读取
  - [x] CLI: `hostlens target add/list/remove`
  - [x] 集成测试：docker 起一个 sshd 容器，跑真实 SSH（不要 mock）
  - [x] 验收：非 root 用户能跑通 add → exec 流程
- [x] **1.3 Inspector manifest schema（扩展版，覆盖真实运维需求；详见 docs/ARCHITECTURE.md §4）**（OpenSpec change `add-inspector-plugin-system` 落地）
  - [x] `inspectors/schema.py`：Pydantic 模型 `InspectorManifest`，必须包含：
    - 基础：`name` / `version` / `description` / `tags`
    - 目标兼容：`targets` (兼容的 ExecutionTarget 类型) / `requires_capabilities` (capability 集合)
    - 依赖：`requires_binaries` (远端需要的可执行文件，如 nginx/mysql/openssl) / `requires_files` (需要可读的路径)
    - 权限：`privilege` (none / sudo / root) —— Agent 拒绝运行需要 sudo 的 Inspector 除非用户显式 opt-in
    - 参数化：`parameters` (JSON Schema，由 schedule manifest 或 CLI 传入，如 `tls.cert_expiry` 需要 host list)
    - 密钥：`secrets` (引用 `${ENV_VAR}` 占位列表，如 PGPASSWORD)
    - 采集：`collect.command` 支持 Jinja2 模板（吃 parameters 与 secrets）/ `collect.timeout_seconds` / `collect.sampling_window` (适合"过去 N 分钟错误数"时窗采集)
    - 解析：`parse.format` (raw / table / json / kv) / `parse.columns`
    - 输出：`output_schema` (JSON Schema)
    - 判定：`findings` (`for_each` / `when` / `severity` / `message` 四字段 DSL；详见 docs/ARCHITECTURE.md §4 Finding DSL 求值语义)
    - 元数据：`artifacts` (列出额外产物，如"导出最近 50 行日志作为附件")
  - [x] `inspectors/loader.py`：YAML 解析 + JSON Schema 校验 + 密钥占位展开（`hook.py` 加载实为 M1-disabled，写了直接 raise）
  - [x] `inspectors/registry.py`：按 name 索引，启动时 walk `inspectors/builtin/` 与用户 `inspectors/`
  - [x] 验收：
    - [x] 加载失败时给出文件路径 + 字段级错误
    - [x] `requires_binaries` 不满足时 Inspector 自动 skip 并报告（而非报错）
    - [x] 需要 sudo 的 Inspector 在未 opt-in 时 dispatch 必须拒绝
    - [x] secrets 占位未注入（doctor 应能预先检测） → 加载时清晰报错且不泄露 env 变量名以外的信息
    - [x] **Shell 注入防御**（详见 ARCHITECTURE.md §4 命令渲染安全规则）：
      - parameters 中 string 类型字段未声明 `pattern` 或 `enum` → 加载时拒绝
      - command 模板中 string parameter 未走 `| sh` filter → 加载时拒绝（除非显式 `unsafe_raw: true`）
      - secrets 在 command 模板中被 Jinja 插值（不通过 env var 引用 `$VAR`）→ 加载时拒绝
      - 跑一组注入 payload 测试：用 `'; whoami; #` / `$(curl evil)` 等作为 parameter 值，验证渲染结果转义正确
- [x] **1.4 Inspector runner**（OpenSpec change `add-inspector-plugin-system` 落地）
  - [x] `inspectors/runner.py`：take(target, manifest) → 跑 collect → parse → 应用 findings → 返回 `InspectorResult`
  - [x] parse 内置 format：`raw` / `table` / `json` / `kv`
  - [x] findings 表达式引擎：基于 `simpleeval`（不要 `eval`），只允许只读表达式；按 ARCHITECTURE.md §4 Finding DSL 实现 `for_each`（遍历模式）与省略 `for_each`（聚合模式）；message 模板用 Python `.format(...)` 风格；loader 校验聚合模式 message 不引用 for_each 变量
- [x] **1.5 内置 hello inspector（管线验证用）**（OpenSpec change `add-inspector-plugin-system` 落地）
  - [x] `inspectors/builtin/hello/echo.yaml`：跑 `echo hello`，输出 `{"message": "hello"}`
  - [x] `inspectors/builtin/system/uptime.yaml`：跑 `uptime`，解析负载
- [x] **1.6 报告数据模型**（OpenSpec change `add-report-data-model` 落地）
  - [x] `reporting/models.py`：`Report` / `Finding` / `Severity` / `Evidence` Pydantic 模型（含 Finding.tags + Evidence kind-discriminated 字段集 + Report.from_inspector_results 工厂）
  - [x] `reporting/render_markdown.py`：纯 f-string 渲染（≤ 200 行；含控制字符 escape；env var 不展开；渲染边界过 redact_report_for_render）
  - [x] `reporting/render_json.py`：Pydantic `model_dump_json` 包装（同走 redact_report_for_render）
- [x] **1.7 CLI: inspect & inspectors**
  - [x] `hostlens inspect <target> --inspector <name> [--output file] [--format md|json] [--parameters JSON] [--allow-privileged] [--timeout SECONDS]`（4 值退出码 0/1/2/3 + Typer usage exit 改写 + stdout/stderr 分离）
  - [x] `hostlens inspectors list [--tag] [--target-type]`（archived `add-inspector-plugin-system` 已交付）
  - [x] `hostlens inspectors show <name>` 打印 manifest（archived `add-inspector-plugin-system` 已交付）
  - [x] 验收：本地 LocalTarget 端到端跑通 hello.echo + sleep_timeout demo Path 10 步（参见 `examples/m1-report/README.md`）；SSH 容器验证留给 M2 集成测试扩展

---

## M2 — 手写 Agent loop（核心展示点）

**目标**：实现 Hostlens 简历价值的核心 —— 一个**自己手写的、不依赖 LangChain 的** Anthropic tool-use loop。Planner Agent 接收自然语言意图，自动选择并调度 Inspector，输出结构化报告。

**对应 OpenSpec changes / proposal**：
- [`add-tool-registry-capability-layer`](openspec/changes/archive/2026-05-25-add-tool-registry-capability-layer/) ✓ archived（M2 前置；M1 阶段已落地）
- [`add-llm-backend-protocol`](openspec/changes/archive/2026-05-26-add-llm-backend-protocol/) ✓ archived（§2.1a + §2.1b + §2.1c — LLMBackend Protocol + AnthropicAPIBackend / FakeBackend / PlaybackBackend）
- [`add-agent-loop-skeleton`](openspec/changes/archive/2026-05-29-add-agent-loop-skeleton/) ✓ archived（§2.2 — 手写 tool-use loop + `LoopResult`；含 `agent-tool-adapter` delta：dispatch output-schema 失败改 raise `ToolError`）
- [`add-planner-agent`](openspec/changes/archive/2026-05-29-add-planner-agent/) ✓ archived（§2.4 — Planner Agent + `agent/prompts/planner.md` 提示词）
- [`add-prompt-cache-strategy`](openspec/changes/archive/2026-05-30-add-prompt-cache-strategy/) ✓ archived（§2.5 — 两层 prompt cache 策略 + `docs/agent-cache-strategy.md`）
- [`add-intent-cli`](openspec/changes/archive/2026-05-30-add-intent-cli/) ✓ archived（§2.7 — `hostlens inspect --intent` + RichLiveObserver 流式输出）
- [`add-llm-cassette-testing`](openspec/changes/archive/2026-05-30-add-llm-cassette-testing/) ✓ archived（§2.6 — cassette 工具链 + `HOSTLENS_LLM_MODE` 三态 + `llm_cassette` fixture）
- [`add-backend-disable-thinking`](openspec/changes/archive/2026-05-31-add-backend-disable-thinking/) ✓ archived（M3.6 兜底前移 — `disable_thinking` 开关，支持 thinking 默认开的 anthropic 兼容端点）
- [`add-incident-pack`](openspec/changes/archive/2026-05-31-add-incident-pack/) ✓ archived（§2.8 — 11 Inspector + `ReplayTarget` + 8 场景双回放 snapshot）
- [`add-demo-cli`](openspec/changes/archive/2026-05-31-add-demo-cli/) ✓ archived（§2.9 — `hostlens demo run/list` 离线回放）

**退出条件**：
1. `hostlens inspect prod-web-01 --intent "检查这台机器的健康状况"` 能让 Agent 自主决定调用哪些 Inspector，跑完后输出 markdown 报告；同样的意图在 cassette 回放下结果稳定
2. `hostlens demo run cpu_saturation` 能在本地 5 分钟内 reproduce 出一份带根因假设的报告（**无需 SSH、无需付费 API、无需真实生产访问**）
3. 「最小可用 incident pack」的 8 个真实场景每个都有 fixture 与 snapshot 测试

### 任务

- [x] **2.1a LLMBackend / BackendCapabilities / BackendDiagnostics Protocol 定义（≤2h；详见 ARCHITECTURE.md §9 模型层 / ADR-008）**
  - [x] `agent/backend.py`：`LLMBackend` Protocol（含 name / capabilities / messages_create）+ `BackendCapabilities` dataclass（7 字段：prompt_caching / tool_use / structured_output / parallel_tool_use / extended_thinking / vision / streaming）+ `BackendDiagnostics` Protocol（health_check / quota_check / ensure_safe_for_daemon）+ `MessageResponse` / `BackendHealth` / `QuotaStatus` 数据模型 + `BackendCapabilityViolation` 异常
  - [x] 验收：
    - [x] 三个 Protocol 完全独立，可分别 mock
    - [x] `BackendCapabilities` 配套测试：构造非法组合（如 `prompt_caching=True` 但 `tool_use=False`）应通过（capability 之间无依赖约束，仅声明）
    - [x] mypy --strict 通过

- [x] **2.1b AnthropicAPIBackend 默认实现（≤2h）**
  - [x] `agent/backends/anthropic_api.py`：包一层 `anthropic.AsyncAnthropic`，capabilities 全 true（**M2 范围 extended_thinking / streaming 必须 False**：Protocol 签名不含相应参数）
  - [x] ~~自动重试~~ —— 重试归 Agent loop 单一收口（ADR-005；backend 显式 `max_retries=0`，把 SDK 异常按域分类包装为 `BackendRateLimited` / `BackendUnavailable` / `BackendError(kind="auth_invalid")` raise，由 M2.2 Agent loop 按 ARCHITECTURE §9 Failure Semantics 表统一处理）
  - [x] 结构化日志：（M2.2 Agent loop 一侧消费 token usage 时统一记录；backend 层已通过 `BackendDiagnostics.health_check` 暴露 latency 指标）
  - [x] 基础 `BackendDiagnostics`：`health_check` 调 messages.create 一次 ping（用注入的 `health_check_model="claude-haiku-4-5"` 廉价探测）；`quota_check` 返回 None（M10.5 完善）；`ensure_safe_for_daemon` no-op
  - [x] 收到 `cache_control` block 但 capability 不应该有时 → raise `BackendCapabilityViolation`（不静默丢，Agent loop 的 bug 必须暴露）；扫描 system / messages / tools 三处（M2.2 Agent loop 端先检查 capability 不注入；backend 端是兜底）
  - [x] 401 + 403 统一映射 auth_invalid（AuthenticationError + PermissionDeniedError 同 catch）
  - [x] 验收：
    - [x] `mypy --strict` 通过
    - [x] cassette 录制模式跑通完整管线（M2 落地 PlaybackBackend 回放路径；录制工具未来再加）

- [x] **2.1c FakeBackend + PlaybackBackend + cassette 工具链（≤2h）**
  - [x] `agent/backends/fake.py`：`FakeBackend` 单元测试用（构造时传 responses 列表，按顺序返回；耗尽 raise IndexError）
  - [x] `agent/backends/playback.py`：`PlaybackBackend` 集成测试用（cassette key = `SHA256({model, messages, tools_count})`；miss raise `CassetteMiss` 不回落真实 API；spec drift 由 `--current-tools-hash` lint 检测）
  - [x] cassette 格式定义（JSON Lines；每行 `{request, response, tools_schema_hash?}`）
  - [x] ~~pytest fixture `llm_cassette()`~~ —— M2 集成测试直接构造 PlaybackBackend；fixture 抽象暂未需要，待 M2.2 Agent loop 集成时再评估
  - [x] 配置 schema：`backend.type / api_key / base_url / cassette_path`；`agent.primary_model / fallback_model / max_turns / token_budget_input / token_budget_output / health_check_model`（参考 ARCHITECTURE §9）
  - [x] 验收：
    - [x] CI 默认 replay 模式，不消耗 API 额度（`@pytest.mark.live` + `addopts = "-m 'not live'"`）
    - [x] FakeBackend 与 PlaybackBackend 跑通 Agent loop 单测（M2.2 Agent loop 实施时直接消费）
    - [x] cassette 回放下 token usage 也能正确回放（不调真 API；`Usage` 字段 None→0 兼容 SDK 非缓存响应）
    - [x] 新增测试 case 时跑一次 record 即可（HOSTLENS_LLM_MODE=record）—— **已由 `add-llm-cassette-testing`（PR #37）落地**
    - [x] `BackendCapabilities.prompt_caching=False` 的 backend 上，Agent loop 不注入 `cache_control` block（**Agent loop 端检查 capability**，不是 backend 自己丢；backend 检测到不一致必须 raise `BackendCapabilityViolation`）
- [x] **2.2 Tool-use loop 核心（消费 LLMBackend，不直接 import anthropic）**（已交付，archived `add-agent-loop-skeleton`）
  - [x] `agent/loop.py`：`AgentLoop(backend, tool_adapter, settings, *, system=None)`，**backend 是私有依赖，不进 ToolContext**（ADR-008）
  - [x] 多轮 `while` 按 6 个 `stop_reason` 穷举推进；工具调用并行（同 turn 内多个 tool_use 并行执行，fail-loud 时取消 sibling）
  - [x] 单次 run 的 token 预算上限（per-run 硬上限，逐轮收缩 `max_tokens` 为剩余预算）
  - [x] 最大 turn 数兜底（默认 20）
  - [x] 在调 backend 前根据 `backend.capabilities.prompt_caching` 决定是否注入 `cache_control`
  - [x] **重要**：代码必须可读、注释 WHY，让面试官能快速读懂
- [x] **2.3 Tool Registry（双层 capability 模型；详见 CLAUDE.md §4.10）**（已交付，archived `add-tool-registry-capability-layer`）
  - [x] `tools/base.py`：`ToolSpec`（含 surfaces / side_effects / requires_approval / permissions / sensitive_output / target_constraints / tags 等 policy 字段）+ `ToolContext`（依赖注入容器）
  - [x] `tools/registry.py`：`register` / `list_for(surface)` / `dispatch`；dispatch 前强制校验 surfaces 与 policy gate（surface 不匹配 → `ToolPolicyViolation`）
  - [x] `@tool` 装饰器：声明式注册；Anthropic / MCP JSON Schema **不**进 ToolSpec，由 adapter 在投影时从 Pydantic 生成
  - [x] `agent/tools_adapter.py`：把 `surfaces ∋ "agent"` 的 ToolSpec 投成 Anthropic `tool_use` schema（M2 仅此一个 adapter；MCP adapter 留到 M7）
  - [x] 首批注册的能力：`run_inspector` / `list_inspectors` / `list_targets`（**不含 `read_finding_detail`** —— 已归档 `add-tool-registry-capability-layer` design §选择 明确否决：M2 由 `run_inspector` 一次返回完整 finding 列表，跨 turn 引用 finding 推到 M3 报告持久化后再加）
  - [x] 验收：
    - [x] `surfaces={"mcp"}` only 的 ToolSpec 在 agent 上下文 dispatch 必须报 `ToolPolicyViolation`
    - [x] CLAUDE.md §4.10 的 6 条硬规则每条对应至少 1 个单测
    - [x] handler 必须从 `ctx` 拿 registry —— 用 mypy + 静态检查防止 module-level singleton
- [x] **2.4 Planner Agent（消费 ToolRegistry）**（已交付，archived `add-planner-agent`）
  - [x] `agent/planner.py`：系统 prompt（含 ToolRegistry 概览）+ 通过 `agent/tools_adapter.py` 拿到工具列表
  - [x] **不**直接 import Inspector registry —— 所有能力通过 ToolRegistry dispatch
  - [x] **不暴露** `exec_arbitrary_command` —— 通过 `surfaces` + Inspector 限制能力面
  - [x] 提示词文件在 `agent/prompts/planner.md`（模板加载，不内联）
- [x] **2.5 Prompt caching 策略**（已交付，archived `add-prompt-cache-strategy`）
  - [x] 系统 prompt + Inspector registry 概览：`cache_control: ephemeral`
  - [x] 单测验证：第二次调用的 `cache_read_input_tokens > 0`
  - [x] 文档：`docs/agent-cache-strategy.md`（简短即可）
- [x] **2.6 LLM cassette 测试基础设施（与 2.1 的 PlaybackBackend 配套；构建 cassette 工具链与示例）** —— 已交付，archived `add-llm-cassette-testing`（PR #37）
  - [x] cassette 格式定义（请求 hash 算法 + 响应序列化 schema）—— `agent/cassette_key.py` 单一来源
  - [x] `tests/fixtures/cassettes/` 目录约定（按测试名分组）
  - [x] env 切换：`HOSTLENS_LLM_MODE=record|replay|live`（record 走 AnthropicAPIBackend + 写盘；replay 走 PlaybackBackend；live 走 AnthropicAPIBackend）
  - [x] pytest fixture：`llm_cassette()` 自动选 backend + cassette 文件（`tests/conftest.py`）
  - [x] 验收：CI 默认 replay 模式，不消耗 API 额度；新增测试 case 时跑一次 record 即可
- [x] **2.7 CLI: --intent 模式**（已交付，archived `add-intent-cli`）
  - [x] `hostlens inspect <target> --intent "<自然语言>"`
  - [x] 实时流式输出 Agent 思考与工具调用（Rich live display）
  - [x] 输出最终报告
- [x] **2.8 最小可用 Incident Pack（M2 收尾前必须能诊断这 8 个真实场景）** —— OpenSpec change `add-incident-pack`（11 Inspector + ReplayTarget + 双回放层 snapshot 测试）

  > **目标**：M2 结束时不仅能跑通"Agent 能调 Inspector"的管线，还能针对真实运维场景输出有用诊断。架构再漂亮，"你能诊断的第一个真实故障"才是用户和面试官判断这个项目的标尺。

  - [x] CPU 饱和：`linux.cpu.top_processes` + `linux.system.load_avg`
  - [x] 内存压力 / OOM：`linux.memory.pressure` + `linux.kernel.oom_killer`
  - [x] 磁盘满 / inode 耗尽：`linux.disk.usage` + `linux.fs.inode_pressure`
  - [x] systemd 失败单元：`linux.systemd.failed_units`
  - [x] 最近错误突增：`log.tail.error_burst`（通用 tail 探针）
  - [x] 文件描述符耗尽：`linux.process.fd_usage`
  - [x] 依赖服务连通性：`net.dependency.tcp_check`（按配置探测下游 host:port）
  - [x] TLS 证书过期：`net.tls.cert_expiry`（按 SNI 列表探测）
  - [x] 验收：每个场景有 ReplayTarget fixture + cassette + snapshot 测试，离线确定性回放（`tests/incidents/`）；面向人类的 `hostlens demo` CLI 留给 M2.9 `add-demo-cli`（2.9）

- [x] **2.9 Demo 路径（5 分钟内本地 reproduce 出报告）** —— 已交付，archived `add-demo-cli`（PR #44）。实现采 **scenario-registry + PlaybackBackend** 方案（`hostlens demo run/list`），替代原计划的 `examples/` 多目录散列：场景资产作 package-data 进 `src/hostlens/demo/scenarios/`，单一 SOT 避免第二份场景清单（已在 `demo-cli-command` spec 固化）
  - [x] `src/hostlens/demo/scenarios/<key>/`：8 套打包 incident replay 资产（`fixture.json` + `cassette.jsonl`），由 `demo run` 消费
  - [x] 覆盖 8 场景：`cpu_saturation` / `memory_oom` / `disk_inode` / `systemd_failed` / `error_burst` / `fd_exhaustion` / `dependency_unreachable` / `tls_expiry`
  - [x] CLI: `hostlens demo run <scenario>` / `hostlens demo list`（默认离线 replay，**无 `--replay` flag**；kebab→snake 归一化；**无需 SSH、无需付费 API、无需真实生产访问**）
  - [ ] `examples/README.md` 逐场景说明（装饰性 follow-up，非阻塞）
  - [ ] 录一段 GIF 放仓库 README 顶部（装饰性 follow-up，非阻塞）
  - [x] 验收：干净 macOS / Linux 上 `pip install -e ".[dev]" && hostlens demo run cpu_saturation` 秒级出带根因报告（实测 <0.1s）

---

## M3 — Diagnostician + 报告体系

**目标**：Planner 拿到一堆原始 finding 后，由 Diagnostician 做跨信号关联，产出**带根因假设的报告**，并支持与历史 run 做 regression diff。

> **状态：✅ 收口（核心交付完成）**。3.1/3.2/3.3/3.5 全部落地（Diagnostician + 根因假设 + 报告 schema/持久化 + regression diff）。两项**显式 deferred 至后续**（不在 M3 核心交付内、不阻塞里程碑）：**3.4 `render_html`**（markdown 渲染含根因章节已做，仅缺 HTML 渲染）与 **3.6 Path 2 `support-extended-thinking`**（推理 trace 主动请求+消费；Path 1 容忍已落地 #53）。

**对应 OpenSpec proposal**：
- `add-diagnostician-agent`（3.1，后续 —— 消费已落地的 `Report.hypotheses` 字段 + `ReportStatus` degraded_* 值）
- [`add-report-persistence-and-diff`](openspec/changes/archive/2026-06-01-add-report-persistence-and-diff/) ✓ archived（3.2 schema + 3.3 持久化 + 3.5 diff 引擎 + `reports` CLI + `inspect --persist`；PR #46。提案 7 轮 + 代码 3 轮对抗 review APPROVE）
- `add-intent-report-persistence`（3.1 持久化半边 —— `--intent --persist` 现产忠实 `Report` 入库：从 per-run `InspectorResultCollector` 快照组装 + hypotheses/narrative 投影进 `Report`，`reports show`/`diff` 可消费。demo wiring 已交付（`wire-demo-to-report`：`demo run` 全链 Planner→Diagnostician→Report + `--persist`）。
- `add-hypothesis-level-diff`（hypothesis-level diff 已交付 —— `compute_diff` 在同套防污染门下按 `frozenset(supporting_findings)` 证据集键产 hypothesis 级 added/resolved/confidence_changed + unanchored/ambiguous_keys 计数，`reports diff` 渲染 hypothesis 段）

**退出条件**：报告中能看到 "📌 根因假设" 章节（占位已就位，内容待 3.1 Diagnostician 填充），包含证据链接；对同一 target 跑两次能输出 "本次相对上次新增了 X、消失了 Y" 的 diff（**已可**，见 `hostlens reports diff`）。持久化部分达成：`--intent --persist` 落库的 `Report` 含 hypotheses，`reports show`/`diff` 可取回并做 finding 级对比。

### 任务

- [x] **3.1 Diagnostician Agent** —— `add-diagnostician-agent`（消费 `Report.hypotheses` 形状 + `ReportStatus` degraded_* 值；`--intent` stdout 渲染「## 根因假设」章节含证据链接）
  - [x] **持久化半边** —— `add-intent-report-persistence`：`--intent --persist` 现产忠实 `Report` 入库（per-run `InspectorResultCollector` 快照组装 + hypotheses/narrative 投影；`--intent --format json` BREAKING 改输出 `Report`），`reports show` 取回含 hypotheses 的 `Report`，`reports diff` 对两次 `--intent` run 做 finding 级 added/resolved；no-result（collector 真空）不入库。demo run 持久化 wiring 已交付（`wire-demo-to-report`：`demo run` 走 `run_diagnosis_pipeline` 产忠实 `Report` + `--persist` 入库）。hypothesis-level diff 已交付（`add-hypothesis-level-diff`：diff 现按证据集键比 hypotheses，产 hypothesis 级 added/resolved/confidence_changed + unanchored/ambiguous_keys，`reports diff` 渲染 hypothesis 段）。
  - [x] `agent/diagnostician.py`：输入 = findings 列表 + intent，输出 = 带根因假设的 `DiagnosticianResult`（含 narrative / findings(canonical, 带 id) / hypotheses / reconcile 后的 status）
  - [x] 暴露的工具：`correlate_findings`（序号标签引用，纯结构化输出通道）/ `request_more_inspection(inspector_name)`（复用 `InspectorRunner` 补查，target 闭包固定）
  - [x] 提示词在 `agent/prompts/diagnostician.md`
- [x] **3.2 报告 schema 完善（与 docs/ARCHITECTURE.md §10 对齐 —— §9 failure semantics 与 §10 diff 基线选取依赖这些字段）** —— archived `add-report-persistence-and-diff`（add-only **路线 A**：保留 M1 扁平字段 + 叠加 `meta`/`hypotheses`）
  - [x] `RootCauseHypothesis`：description / confidence / supporting_findings / suggested_actions（**本提案只定义形状、不产内容**，内容由 3.1 Diagnostician 填）
  - [x] `ReportStatus` Enum：ok / partial / degraded_no_planner / degraded_rate_limited / degraded_token_budget / degraded_max_turns / empty_response / stored_as_orphan（`failed_api_unavailable` 归 `RunStatus`；本提案只产出 ok/partial/stored_as_orphan，degraded_* 由 3.1 产出）
  - [x] `InspectorRun`：name / version / status (ok/timeout/target_unreachable/requires_unmet/exception) / duration_seconds / finding_count
  - [x] `BaselineRef`：run_id / timestamp / status / inspector_versions / report_schema_version
  - [x] `ReportMeta`：run_id / report_schema_version / timestamp(tz-aware) / target_id / target_name / target_type / intent / schedule_name / status / inspectors_used (list[InspectorRun]) / token_usage / duration_seconds / baseline_ref / diff_skipped_reason
  - [x] `Finding` 增加 `inspector_version` 字段（实际加 `id`(severity-agnostic 指纹) / `inspector_name` / `inspector_version`，全 add-only 默认 None）
  - [x] 验收：M1/M2 fixture 经 render 的 sink 升级到 schema 1.1（旧 1.0 JSON 仍可加载）；run_inspector @field_serializer 保 agent 可见面与 cassette 不变
- [x] **3.3 报告持久化** —— archived `add-report-persistence-and-diff`
  - [x] `reporting/store.py`：SQLite 单库（WAL，rowid 总序 tie-break，索引从内存 meta 投影，存脱敏 JSON，orphan 降级改写 meta.status + UUID 防穿越），存 run 记录与报告 JSON
  - [x] CLI: `hostlens reports list <target>` / `hostlens reports show <run_id>`
- [ ] **3.4 报告渲染扩展**（render_html 未做 — **deferred**，M3 已收口为 ✅，本项留后续）
  - [ ] `reporting/render_html.py`：基于 Jinja2 模板，含交互式 finding 展开
  - [x] markdown 渲染增加根因章节（`add-report-persistence-and-diff` 已加 `## 根因假设` 章节占位，空时显示 `_暂无根因假设_`）
- [x] **3.5 Regression diff 引擎** —— archived `add-report-persistence-and-diff`
  - [x] `reporting/diff.py`：两份报告对比，输出 `added` / `resolved` / `changed_severity`（+ `inspector_upgraded`；compute_diff 规则 0-7：meta=None 前置 / None-id 跳过 / 版本对齐排除 / 指纹集合差，防自基线）
  - [x] CLI: `hostlens reports diff <run_id_a> <run_id_b>`（+ `diff --target <t>` 自动基线模式，rowid tie-break 排除 current）
  - [ ] 也作为定时巡检报告里的一个 section（M5 用到）
- [ ] **3.6 thinking 支持（拆成 Path 1 容忍 / Path 2 请求+消费两个独立提案）** — Path 1 ✅ 已落 #53；**Path 2 deferred**（M3 已收口为 ✅，Path 2 留作未来独立提案）

  > **触发背景**：M2.6 用 DeepSeek 做 live 测试时发现 `deepseek-v4-pro/flash` 经其 anthropic 兼容端点**强制返回 `type="thinking"` 块**，撞 `MessageResponse` 只建模 `text`/`tool_use` 的 scope → 解析崩。Diagnostician（3.1）若用推理模型也会受益。**reference memory**：`deepseek-v4-thinking-incompatible-live-test` / `deepseek-thinking-block-schema`（含实测 schema）。
  >
  > **「近期兜底」已落地**：`add-backend-disable-thinking`（2026-05-31 archived）已加 `disable_thinking` 开关（抑制路径）+ `BackendError(kind="unsupported_content_block")` 归一。原 §3.6 四支柱按动机拆成两刀。

  - [x] **Path 1 — `tolerate-inbound-thinking`（容忍，已实现 + archived #53）** 动机：DeepSeek 默认吐 thinking 我们不认 → 从「抑制」转向「建模容忍」，不依赖 `disable_thinking` 关得掉。范围 = 支柱①③④（不含②）。
    - [x] **支柱①** `ContentBlock` 联合新增 `ThinkingBlock{type,thinking,signature}` + `RedactedThinkingBlock{type,data}`，**两者 `extra="allow"`**（verbatim relay 保真）；`signature: str` required（实测 pro/flash 都有，值==message id，DeepSeek 不验签）
    - [x] **支柱③** Agent loop 多轮 verbatim 回传 thinking 块 —— **已天然成立**：断点 B 恒落末尾 user tool_result、永不落 thinking（loop 结构使然），只加结构回归测试钉死（design D-3）
    - [x] **支柱④** cassette keying 投影时**丢整个 thinking/redacted 块**再 hash（`extra="allow"` 残留字段会毁 hash 稳定）+ recorder 落盘 request 同源归一；response 存完整供回放
    - [x] **零新 capability**（design D-2）：容忍 = 无条件扩 union，没人 branch tolerate flag → 违反 §4.11，7 字段不变；`extended_thinking` 保持 False
    - [x] 收窄 `unsupported_content_block` 语义（thinking 现能 parse，该 kind 改指「真正未知新 block」）+ 改其测试
    - [x] `disable_thinking` 降级为「可选 token 优化」（非兼容必需）
    - [x] 验收：DeepSeek 不设 disable 也跑通 thinking-on 多轮（live 已预验证）+ cassette record→replay 命中
  - [ ] **Path 2 — `support-extended-thinking`（请求+消费，未来独立提案）** 范围 = 支柱② + 把推理 trace 渲进 Report。
    - [ ] **支柱②** `LLMBackend.messages_create` + Protocol 加 `thinking` 参数（`{type:enabled,budget_tokens}` / `disabled` / `adaptive`）+ 对应 backend `BackendCapabilities.extended_thinking=True`（此时才真正「主动请求」，loop 按 capability gate 决定注入）
    - [ ] **消费**：Diagnostician 推理 trace 渲进 Report（reporting/models + 渲染 + 持久化 + diff）—— 简历级「Agent 给你看它的思考」亮点
    - [ ] 验收：真 Anthropic key 开 thinking 跑通工具多轮 + cassette record→replay 命中

---

## M4 — Scheduler

**目标**：让 Hostlens 能按 cron / interval 自动跑巡检，运行结果持久化，daemon 模式稳定。

**对应 OpenSpec proposal**：`add-scheduler`

**退出条件**：`hostlens schedule daemon` 起来后能按 `schedules/*.yaml` 定时跑巡检，SIGTERM 优雅停机不丢任务。

### 任务

- [x] **4.1 Schedule manifest schema**
  - [x] `scheduler/schema.py`：`ScheduleManifest`（cron/interval、timezone、targets、**intent 必填、inspectors 可选作为优先 hint**、report 配置、notify 配置占位）
  - [x] `scheduler/loader.py`：扫 `schedules/*.yaml`
- [x] **4.2 APScheduler 封装**
  - [x] `scheduler/runner.py`：基于 `AsyncIOScheduler`，每个 manifest 注册为一个 job
  - [x] job 执行 = 调 M2 的 Agent loop（经 `orchestration.pipeline.run_diagnosis_pipeline`）
- [x] **4.3 Run 记录（与 ARCHITECTURE.md §7 RunStatus enum 对齐）**
  - [x] `scheduler/store.py`：每次触发记录 `Run`（run_id / schedule_name / triggered_at / started_at / finished_at / status / report_id / error / notify_results + 留痕 targets/inspectors/report_hash/report_storage）
  - [x] `RunStatus` Enum：ok / partial / budget_exhausted / missed / skipped_due_to_running / failed_api_unavailable / failed / daemon_stopped（详见 ARCHITECTURE.md §7）
  - [x] 验收：`Run.report_id is None` **当且仅当 `Run.status not in {ok, partial}`**（涵盖所有无 Report 的 RunStatus 值，与 ARCHITECTURE.md §7 边界表一致）；doctor 能输出最近 N 次 Run 状态分布
- [x] **4.4 Daemon 模式**
  - [x] `cli/schedule.py`：`run`（前台）/ `daemon`（后台）/ `list` / `trigger <name>` / `status`
  - [x] SIGTERM 信号：停止接受新任务，等当前 job 完成再退出（D-5 shield+drain；超 grace 落 daemon_stopped）
  - [x] daemon 模式日志写文件 + structlog json
- [x] **4.5 doctor 集成**
  - [x] `hostlens doctor` 增加 schedule 健康检查（next_fire_time 是否合理、上次 run 是否失败）落 `checks.schedules`

---

## M5 — Notifier 抽象 + Telegram + 飞书

**目标**：实现"业务通用化、可扩展"的核心证明 —— Notifier 适配器模式。内置 Telegram 和飞书两个通道，做好抽象让后续加通道只是"新增一个文件"。

**状态：✅ 已落地**。`notifiers/{base,config,routing,telegram,lark}.py` + Jinja2 模板 —— Notifier Protocol + Channel registry / Telegram（MarkdownV2）+ 飞书 Lark（HMAC 签名）/ `notifiers.yaml`（`${ENV_VAR}` 注入）+ `only_if` 路由 / Scheduler↔Notifier 接线（失败隔离不冒泡）/ `hostlens notify channels/render/test` + `doctor --check-channels`。已归档 `add-notifier-channels`。钉钉 / 企微 / Slack / Email / 通用 Webhook 仅靠 Protocol + registry 预留扩展点，本期未写适配器（留 M10）。

**对应 OpenSpec proposal**：
- `add-notifier-abstraction`
- `add-telegram-notifier`
- `add-lark-notifier`

**退出条件**：定时巡检报告能自动推送到 Telegram 群和飞书群；新增一个 dummy notifier 类不需要改任何现有代码（只在 channel registry 加一行）。

### 任务

- [ ] **5.1 Notifier 抽象**
  - [ ] `notifiers/base.py`：`Notifier` Protocol（`send` / `render` / `validate_config`）+ `NotifyPayload` / `NotifyResult`
  - [ ] `notifiers/registry.py`：按 `type` 字段查找适配器
  - [ ] `notifiers/config.py`：`~/.config/hostlens/notifiers.yaml` 加载，支持 `${ENV_VAR}` 展开
  - [ ] 抽象级单测：用 dummy notifier 验证注册与路由
- [ ] **5.2 Jinja2 模板系统**
  - [ ] `notifiers/templates/`：按通道分子目录（`telegram/`、`lark/`）
  - [ ] 模板上下文：`Report` Pydantic 模型字段全部可用
  - [ ] snapshot 测试：固定一份 fixture report，渲染结果对比快照
- [ ] **5.3 Telegram 适配器**
  - [ ] `notifiers/telegram.py`：bot API（aiohttp 直调，不引入 python-telegram-bot）
  - [ ] MarkdownV2 模板（注意转义）
  - [ ] 长报告分块发送
  - [ ] 验收：真实 bot 发送 + cassette 回放都通过
- [ ] **5.4 飞书 Lark 适配器**
  - [ ] `notifiers/lark.py`：webhook + 签名校验（HMAC-SHA256 + timestamp）
  - [ ] 富文本卡片模板（含交互按钮占位，预留 M9 用）
  - [ ] 验收：模板渲染出的 JSON 通过飞书卡片 schema 校验
- [ ] **5.5 路由表达式 only_if**
  - [ ] `notifiers/router.py`：基于 simpleeval 的安全表达式
  - [ ] 上下文：`severity` / `has_findings(...)` / `regression_count` / target / inspectors
  - [ ] 单测覆盖：表达式异常时不阻塞其它通道
- [ ] **5.6 Scheduler ↔ Notifier 接线**
  - [ ] Schedule manifest 的 `notify:` 字段触发 Notifier 调度
  - [ ] notify 结果回写 Run 记录
- [ ] **5.7 CLI**
  - [ ] `hostlens notify <channel> --report <file>` 单次发送（测试用）
  - [ ] `hostlens notify list-channels`
  - [ ] `hostlens doctor --check-channels` 探测每个通道连通性

---

## M6 — 内置 Inspector 库覆盖矩阵

**目标**：让 Hostlens 在真实运维场景下"能用"。按故障域组织 Inspector 矩阵，明确每个域的覆盖度，避免"看起来很多但盲区也很多"。

**对应 OpenSpec proposal**：按域分多个 sub-proposal（已落地 wave，见下）

**退出条件**：覆盖矩阵下每个域至少有 3 个 Inspector，总计 ≥40 个，每个 Inspector 有 manifest + snapshot 测试 + 在 `examples/` 里有可 replay 的 fixture。

**状态：✅ 域全满**。已通过多个 inspector wave 增量交付，当前 `src/hostlens/inspectors/builtin/` 下 **72 个** inspector（总数已过 ≥40 门槛，每域 ≥3），核心域（计算/内存/磁盘/网络/进程/systemd/cron/nginx/mysql/postgres/redis/docker/**K8s 控制面**/log/系统/**security/包管理**/**语言运行时 JVM·Go**/**TLS chain**）已覆盖。已归档 change：`add-inspector-authoring-contract`、`add-os-shell-inspectors-wave1`、`add-service-inspector-contract-spike`、`add-single-instance-service-inspectors`、`add-log-and-fault-service-inspectors`、`add-replication-inspector-spike`、`add-replication-lag-inspectors`、`add-postgres-replication-lag-inspector`、`add-security-baseline-and-package-inspectors`、`add-runtime-inspectors`、`add-tls-chain-validity-inspector`、`add-k8s-control-plane-inspectors`、`migrate-redis-slowlog-service-contract`、`migrate-postgres-bloat-tables-service-contract`、`add-nginx-upstream-mysql-deadlocks-inspectors`。seed 漂移迁移已收口（slowlog/bloat_tables 全量合规 service-inspector-contract）。K8s 控制面域（pod OOMKilled / evicted / stuck-pending / node conditions / warning events）经 `add-k8s-control-plane-inspectors` 以 kubectl 管理机视角交付（5 个 `k8s.*` inspector，`targets: [local, ssh]`）。wave-2b 尾批 nginx.upstream（upstream 故障）、mysql.deadlocks（InnoDB 死锁）经 `add-nginx-upstream-mysql-deadlocks-inspectors` 交付，Web/DB 域全满。

### 覆盖矩阵

| 故障域 | 必须覆盖 | M2 计划基线（M2.8） | M6 新增 |
|---|---|---|---|
| 计算（CPU） | top processes / load avg / throttling / cpufreq | top_processes, load_avg | cpu.throttling ✅, cpu.cpufreq ✅ |
| 内存 | pressure / swap / OOM history / hugepages | memory.pressure, kernel.oom_killer | memory.swap ✅, memory.hugepages ✅ |
| 磁盘 / FS | usage / inode / IO / SMART / mount health / logrotate | disk.usage, fs.inode_pressure | disk.io ✅, disk.smart ✅, fs.mount_health ✅, fs.logrotate ✅ |
| 网络 | connections / listening ports / dependency probes / DNS / NTP | dependency.tcp_check | net.connections ✅, net.listening_ports ✅, net.dns.resolve ✅, net.ntp.drift ✅ |
| 进程 | zombies / FD 耗尽 / 总数 / 关键进程存活 | process.fd_usage | process.zombies ✅, process.total ✅, process.critical_alive ✅ |
| 服务管理器 | systemd failed units / timers / masked units | systemd.failed_units | systemd.timer_status ✅, systemd.masked ✅ |
| 调度器 | cron 历史 / anacron / 失败 cron | — | cron.last_runs ✅, cron.failures ✅ |
| TLS | cert 过期 / cert chain 有效性 | tls.cert_expiry | tls.chain_validity ✅ |
| 内核 / 系统 | dmesg errors / kernel taint / reboot-required / uptime | — | system.kernel_messages ✅, system.uptime, system.reboot_required ✅, system.kernel_taint ✅ |
| 安全基线 | failed logins / sudo history / 提权向量 | — | security.failed_logins ✅, security.sudo_history ✅, security.world_writable_dirs ✅（异常监听端口由既有 net.listening_ports 覆盖，不重复） |
| 包管理 | 待升级包 / 安全补丁 / held-back | — | pkg.pending_updates ✅, pkg.security_patches ✅, pkg.held_back ✅ |
| Web / Nginx | health / config test / 5xx rate / upstream health | — | nginx.health ✅, nginx.config_test ✅, nginx.error_rate ✅, nginx.upstream ✅ |
| MySQL | conn usage / slow queries / replication lag / deadlocks | — | mysql.connection_usage ✅, mysql.slow_queries ✅, mysql.replication_lag ✅, mysql.deadlocks ✅ |
| PostgreSQL | conn usage / replication lag / bloat（真实 SQL）/ long queries | — | postgres.connection_usage ✅, postgres.replication_lag, postgres.bloat_tables, postgres.long_queries ✅ |
| Redis | memory / persistence / replication / slowlog | — | redis.memory_usage ✅, redis.persistence ✅, redis.replication_lag ✅, redis.slowlog ✅ |
| Docker（SSH 跨） | unhealthy / restart loop / image disk / network | — | docker.containers.unhealthy（由 docker.containers.restart_loop 覆盖、不单列）, docker.containers.restart_loop ✅, docker.images.disk_usage ✅, docker.networks ✅ |
| K8s（kubectl 控制面视角，跑在配 kubeconfig 的管理机上） | pod OOMKilled / evicted / stuck-pending / node conditions / warning events | — | k8s.pods.oom_killed ✅, k8s.pods.evicted ✅, k8s.pods.stuck_pending ✅, k8s.nodes.conditions ✅, k8s.events.warnings ✅ |
| 运行时（JVM） | heap usage / GC pressure / thread count | — | jvm.heap ✅, jvm.gc ✅, jvm.threads ✅ |
| 运行时（Go） | goroutine count / heap from pprof | — | go.goroutines ✅, go.heap ✅ |
| 日志 | error burst / exception 突增 | log.tail.error_burst | log.exception_burst ✅ |

> 说明：「M2 计划基线」列列出 §M2.8 **已交付**的 Inspector（见 `src/hostlens/inspectors/builtin/`，PR #38/#42）；M6 在此基础上按域扩充。每个 Inspector 落地时必须勾上覆盖矩阵对应单元格。

### 实施任务（按域）

- [ ] **6.1 计算 / 内存 / 磁盘扩充**（cpu.throttling, memory.swap, disk.io, disk.smart, fs.mount_health, fs.logrotate, memory.hugepages, cpu.cpufreq）
- [ ] **6.2 网络 + DNS + NTP**（network.connections, network.listening_ports, dns.resolve, ntp.drift）
- [ ] **6.3 systemd + cron**（systemd.timer_status, systemd.masked, cron.last_runs, cron.failures）
- [x] **6.4a 安全基线 + 包管理**（security.failed_logins, security.sudo_history, security.world_writable_dirs, pkg.pending_updates, pkg.security_patches, pkg.held_back）—— 已交付，归档 `add-security-baseline-and-package-inspectors`
- [x] **6.4b TLS chain validity**（tls.chain_validity）—— 已交付，归档 `add-tls-chain-validity-inspector`
- [x] **6.5 Nginx**（health ✅, config_test ✅, error_rate ✅, upstream ✅）
- [x] **6.6 MySQL / PostgreSQL**（mysql.connection_usage/slow_queries/replication_lag/deadlocks ✅、postgres.connection_usage/replication_lag/bloat_tables/long_queries ✅，含真实 SQL 模板）
- [x] **6.7 Redis**（memory_usage, persistence, replication_lag, slowlog）—— 全交付，slowlog 已迁移至 service-inspector-contract 全量合规
- [x] **6.8 Docker（SSH 跨）**（containers.restart_loop, images.disk_usage, networks；unhealthy 由 restart_loop 覆盖不单列）
- [x] **6.9 JVM / Go 运行时**（jvm.heap, jvm.gc, jvm.threads, go.goroutines, go.heap）—— 已交付，归档 `add-runtime-inspectors`
- [x] **6.10 进程 + 内核 + 日志**（process.zombies, process.total, process.critical_alive, system.reboot_required, system.kernel_taint, log.exception_burst）
- [ ] 验收：每个 Inspector 必须有 fixture + snapshot 测试 + 覆盖矩阵里的位置勾上 + 在 `examples/` 里给出至少一个 demo 场景

---

## M7 — MCP Server

**目标**：把 Hostlens 暴露成 MCP server，让 Claude Code / Cursor 把它当作工具调用。

**对应 OpenSpec proposal**：`add-mcp-server-surface`（已归档 `2026-06-08-add-mcp-server-surface`）

**退出条件**：Claude Code 配置 Hostlens 后，能在对话里直接说"帮我巡检 prod-web-01"并看到完整工具调用链。

**状态：✅ 已落地（M7）**。`src/hostlens/mcp_server/{tools_adapter.py, server.py}` + `cli/mcp.py`：`McpToolsAdapter`（`list_for_mcp` 投影 + 九步 `dispatch`）、`build_server`（eager fail-closed 自检）+ `run_stdio` stdio server、`hostlens mcp serve`、`doctor checks.mcp`（非致命）。只读三件套（`list_inspectors` / `list_targets` / `run_inspector`）显式 opt-in `"mcp"` surface；fail-closed `sensitive_output` 三处对称；`mcp` 为 optional-dep（`pip install "hostlens[mcp]"`）。**实际交付与下方原计划的差异**：①**stdio-only**，HTTP transport（原 7.4）+ 远程鉴权列为 Non-Goal；②**未做 Resources 暴露**（原 7.3 `hostlens://reports|inspectors` 留待有需求时另提案）；③只读三件套，未新增 `run_inspection`/`get_report`/`list_recent_runs`（原 7.2 备选）。下方任务清单是**原始规划**，保留作历史；实际落地以归档 spec 为准。

### 任务

- [ ] **7.1 MCP server 骨架**
  - [ ] `mcp_server/server.py`：基于官方 `mcp` SDK
  - [ ] **不**重写工具集 —— 通过新增的 `mcp_server/tools_adapter.py` 把 `registry.list_for("mcp")` 投成 MCP tool definition（参考 CLAUDE.md §4.10）
  - [ ] adapter 必须强制要求 ToolSpec 显式声明 `sensitive_output`，缺省禁止暴露
- [ ] **7.2 在 ToolRegistry 增量注册 MCP-only 能力**
  - [ ] 把已有 ToolSpec 的 `surfaces` 加 `"mcp"`：M2 首批 `list_targets` / `list_inspectors` / `run_inspector`，以及 M3 落地的 `read_finding_detail`（若届时已注册）
  - [ ] 新增 MCP 专用 ToolSpec（如有必要）：`run_inspection(target, intent?)` / `get_report(run_id)` / `list_recent_runs(target?)`
  - [ ] 每个 MCP 暴露的 ToolSpec 必须撰写 `mcp_description`（面向远程 LLM）—— 不能复用 `agent_description`
- [ ] **7.3 资源（Resources）暴露**
  - [ ] `hostlens://reports/<run_id>` 让 LLM 拉取报告原文
  - [ ] `hostlens://inspectors/<name>` 暴露 inspector manifest
- [ ] **7.4 传输**
  - [ ] stdio 模式（`hostlens mcp serve --stdio`）
  - [ ] HTTP / SSE 模式（`hostlens mcp serve --http --port 8765`）
- [ ] **7.5 集成文档**
  - [ ] `docs/integrations/claude-code.md`
  - [ ] `docs/integrations/cursor.md`
  - [ ] 在 README 加录屏 / GIF

### M7 后续扩展 — MCP 管控工具集（按 side_effects 读写分期）

> M7 落地只读三件套（`list_targets` / `list_inspectors` / `run_inspector`）。本扩展让 AI 助手通过 MCP 完成全套管控操作，无需切回 CLI 或手编 YAML。

> **2026-06-13 探索结论（取代下方原 A/B/C 分组）**：按 `side_effects` 切**读 / 写两期**，而非按域切三组。硬约束在 `McpToolsAdapter.dispatch`：当前 **rule ③ 硬拒** `side_effects ∈ {write,destructive}`、**rule ④ 硬拒** `requires_approval=True`（reason `approval_flow_not_supported_in_m2`），且 MCP `context_factory` 注入的是永远 refuse 的 `NoopApprovalService`。原 A/B/C 分组里每个写工具（`add_target`/`remove_target`/`trigger_schedule`/`test_channel`）都标 `requires_approval=True`——**按当前 gate 根本到不了 handler**，是不可实施的规划。故重构为：**读半零闸门改动、可立刻落**；**写半必须先建 MCP approval 机制**。四点收敛决策：①读写分期 ②补 reports 读三件套（原规划漏） ③`add_target`→批量 `import_targets`（驱动场景=已有 target 列表迁移，一条条敲 CLI 太烦） ④trigger 的「发通知」从只读触发里拆出独立写工具。

#### 提案① `add-mcp-readonly-management-tools`（读期，零闸门改动，先落）

七个只读工具，**仅给已有 handler 声明 `surfaces ∋ "mcp"` + `sensitive_output` + 投影适配**，复用既有 store/loader/runner，不重写业务逻辑：

- [x] `list_schedules()` — schedule manifest + next_fire_time + enabled 状态；`sensitive_output=True`（暴露调度结构）
- [x] `get_schedule_status(name?, limit?)` — 最近 N 次 Run 留痕（run_id / 触发时间 / 目标 / inspector 集合 / report_hash / notify 结果）；`sensitive_output=True`
- [x] `run_schedule_now(name)` — **`notify` 固定为 false**：跑 schedule 绑的只读 inspector（含 intent pipeline）+ 持久化 Report，**不发通知**。本质 = 批量版 `run_inspector`，故 `side_effects=read`（M7 已接受「远程 LLM 在主机上跑只读巡检」这条边界，批量不跨新边界）。发通知拆到提案② `notify_report`，以保 `side_effects` 在 ToolSpec 上**静态**（不靠 `notify=true` 参数在 read/write 间横跳）
- [x] `list_channels()` — `notifiers.yaml` 通道（name / type / enabled / only_if 路由）；**绝不返回 token / secret**；脱敏后仍暴露渠道结构 → `sensitive_output=True`
- [x] `list_reports(target?)` — 历史报告列表；`sensitive_output=True`
- [x] `show_report(run_id)` — 取回单份 Report（含 findings / hypotheses）；`sensitive_output=True`
- [x] `diff_reports(run_id_a, run_id_b)` — regression diff；`sensitive_output=True`
- [x] 共同：每个新 ToolSpec 分别撰 `mcp_description`（远程 LLM）/ `agent_description`（本地 loop），不复用

#### 提案② `add-mcp-write-approval-flow`（写期，核心是两段式 approval 机制）

**approval 模型（探索定稿）：两段式 token，Hostlens 强制，commit 必须带外。** 关键正确性约束：propose 把 token 返给 LLM，但 commit **只能**经人类在自己终端 `hostlens mcp approve <token>` 完成——LLM 物理上驱动不了写盘。**反模式**：用第二个 MCP `confirm_action(token)` 工具提交，则 LLM 自己就拿到 token、可自问自答，两段式沦为花架子。与 §4.5 `plan→preview→approve→execute`、M9「medium/high 不代执行」(`feedback_ai_no_auto_exec_elevated_risk`) 同形。

**意外干净属性**：MCP server 进程永远只读（propose 不写盘）；唯一写盘点在 CLI `approve` 进程 → 现成的 `_refuse_root_for_write(EUID==0)` 守卫天然覆盖，写拒 root 红线不用在 MCP 层重造。

建议再切两片（各独立提案，PR scope 干净，符合「每组独立提案」）：

**②a `add-mcp-write-approval-mechanism`（纯机制 + 最小验证）**

- [ ] pending-action store：token / action 类型 / payload（待写 diff）/ created_at / 过期 / 单次使用 / 状态（pending→approved→executed / expired）；design.md 决断**复用 M9 `remediation/approval.py` 的 ApprovalGate + append-only audit 形态**还是另起
- [ ] CLI `hostlens mcp approve <token>` / `hostlens mcp pending`：读 store、重校验（含 EUID≠0）、执行、写 audit、标记 executed
- [ ] dispatch 放开 rule ④ 走 **propose 旁路**（rule ③ 仍拒**直接**写——写只能经 propose→人类 approve）
- [ ] 最小验证写工具 `test_channel(channel)`：发一条测试消息，最小爆炸半径验证两段式端到端跑通

**②b `add-mcp-write-management-tools`（招牌写工具，叠在已验证机制上）**

- [ ] `import_targets(entries[])` — **招牌：批量迁移**。input_schema **只收 `*_env`（环境变量名），绝无 `password` 明文字段**；propose 返 `targets.yaml` 的 before/after diff 预览；`mcp_description` 写明行为约定：源数据遇明文密钥 → 映射成 env 名 + 提示人类 `export`，**绝不把明文写进 yaml**
- [ ] `remove_target(name)` — destructive，走同一两段式 gate
- [ ] `notify_report(report_id, channels)` — trigger 的「发通知」半，外部副作用（真实消息）走 gate

---

## M8 — Docker + Kubernetes ExecutionTarget

**目标**：补全容器与集群场景，验证 `ExecutionTarget` 抽象的真正扩展性。

**对应 OpenSpec change**（4 个 archived）：
- [`add-docker-target`](openspec/changes/archive/2026-06-09-add-docker-target/)（8.1 — DockerTarget，PR #81）
- [`enable-docker-inspector-targets`](openspec/changes/archive/2026-06-09-enable-docker-inspector-targets/)（8.3 docker 半边 — inspector 侧放开，PR #82）
- [`add-kubernetes-target`](openspec/changes/archive/2026-06-10-add-kubernetes-target/)（8.2 — KubernetesTarget，PR #83）
- [`enable-k8s-inspector-targets`](openspec/changes/archive/2026-06-10-enable-k8s-inspector-targets/)（8.3 k8s 半边 — inspector 侧放开，PR #84）

**退出条件**：Inspector 不修改代码，仅 manifest 的 `targets:` 字段加上 `docker` / `k8s` 就能在容器内/Pod 内跑。✅ 已达成 —— 容器安全 cohort（INCLUDE 28 / EXCLUDE 42，按 collector 实际读取源逐项判定）的 28 个 manifest 仅追加 `targets:` 值、collector 命令零改动；docker⇔k8s 奇偶不变量 + 内容式 meta-guard 钉死 cohort。

**状态：✅ 已落地**。

### 任务

- [x] **8.1 DockerTarget**
  - [x] `targets/docker.py`：基于 docker-py，exec 异步包装（只读：`exec` + `read_file`）
  - [x] capability 声明（`{SHELL, FILE_READ}` + 懒探测 SYSTEMD/DOCKER_CLI；无 SSH）
  - [x] 容器选择：name / id（经 `targets.yaml` 配置）
- [x] **8.2 KubernetesTarget**
  - [x] `targets/kubernetes.py`：基于 kubernetes-asyncio，pod exec 走 `WsApiClient`，`read_file` 走 tar-over-ws
  - [x] selector：namespace + pod name
  - [x] 多容器 pod：建议显式指定 `container:`（默认取 `spec.containers[0]`；尊重 `default-container` annotation 登记为未来独立提案）
- [x] **8.3 Capability 系统打磨**
  - [x] `InspectorManifest.targets` Literal 收口为 `local/ssh/docker/k8s` 全集；runner preflight 按 manifest `targets:` + capability 匹配（不满足报 `requires_unmet`）
  - [x] 派发路径 flip-impersonate 回放测试（ReplayTarget `impersonate: docker/k8s`）
- [ ] **8.4 CLI: target add 支持 docker/k8s**（留 follow-up —— 与 docker/k8s 一致经 `targets.yaml` 配置即可用，CLI 写入非阻塞）

---

## M9 — 受控修复（Remediation）

**目标**：闭环故事 —— Agent 不光能看出问题，还能提出修复建议，经过人工审批后执行，并预备好回滚路径。

**退出条件**：对一个典型问题（如 `/var/log` 占满磁盘），Agent 能生成 plan、CLI 展示 diff、`--yes` 后执行、记录 audit log、保留回滚命令。✅ 已达成。

**状态：✅ 已落地**（P1a #89 / P1b #90 / P2 #91 / P3 #92 已 merged + archived）。

> **P3 实际形态变更**：原提案 `add-remediation-lark-approval`（飞书远程审批）在实施时按红线「AI 不代做中高风险操作」（见 memory `feedback_ai_no_auto_exec_elevated_risk`）**翻转**为 `add-risk-tiered-remediation-execution`——仅 **low** 风险走自动执行闭环，**medium/high 只产 runbook 不代执行**；飞书远程审批 / high-risk 远程触发被否。下面 P3 任务（9.6）按此 reconcile。
>
> **⚠️ 未完成的 follow-up**：本段末「Follow-up：文档遗留清理」四项**未随 M9 提案落地**（`_in_m2` 后缀仍在 4 处、ARCHITECTURE.md 仍留 `apply_remediation_step`/`docker_prune_images` 早期反例、`targets/base.py:39` 仍写「M9 会加 FILE_WRITE」而 M9 实际撤回了该承诺）——是孤儿 tech-debt，含代码注释改动（需走 PR），建议单独起一个小 `chore` 收口。

### 架构不变量（贯穿所有 M9 提案，先于切分确立）

> 这几条是 M9 探索阶段收敛的决定，约束下面每一个提案。改动它们要先回 OpenSpec 起 proposal。

1. **Agent 表面永久只读** —— M2 在 `agent/tools_adapter.py dispatch()` 写死的「`side_effects ∈ {write,destructive}` / `requires_approval=True` → raise」两道 gate **不是临时墙，是永久不变量**。M9 不放开 agent surface；写路径**根本不以 ToolSpec 形式存在**（避免 §7「❌ 给 Agent 暴露危险工具」）。
2. **Remediation 自成子系统，不进 Tool Registry** —— 类比 Notifier（§4.4）：Executor 是 CLI/Reporter 触发的写通道，不是 Agent 主动调用的能力。Executor **不进 loop、不持 `LLMBackend`、不进 `ToolContext`**。Planner Agent 复用只读 Registry（`run_inspector` 等）核实状态，用 structured output 产出 `RemediationPlan` 数据，**不在 loop 里执行任何 step**。
3. **审批门与 ToolContext 分离** —— `NoopApprovalService` 在 `ToolContext` 里**永久保留拒绝语义**（agent-surface handler 永不触发审批）；真审批门是 `remediation/` 下独立的 `ApprovalGate`，给 Executor/CLI 用。**绝不把真 ApprovalService 塞进 ToolContext**（否则等于给所有 handler 开写后门）。
4. **不引入受限写 API / 不加新 Capability** —— 安全边界是「审批 + audit + rollback 的 shell 串」，写走既有 `Capability.SHELL`（`target.exec`）；撤回 `targets/base.py` 里 `FILE_WRITE` 的 M9 承诺（残缺的受限 API 必然逼出 `exec_raw` 逃生舱，反降低审批人警觉）。

### 提案切分（写代码风险单调递增；每片可独立 demo）

> TODO 原本的两个提案（`add-remediation-plan-schema` / `add-remediation-execution-workflow`）切成四片 + 一道客观门控。schema 必须先冻结（P2/P3 都依赖它）；写路径最后才碰、单独碰（§4.5）。

| 片 | 提案 | 写风险 | 可 demo | 门控 |
|---|---|---|---|---|
| **P1a** | `add-remediation-plan-schema` | 零（纯 Pydantic 契约） | schema 校验单测 | 无（M9 起点） |
| **P1b** | `add-remediation-planner` | 零（只读 + LLM） | 喂 finding → 打印 Plan（不执行） | 依赖 P1a 冻结 |
| 🚪 | — 客观门控 — | — | — | **Planner 对 `/var/log` 占满场景产出人判「可执行」的 Plan（forward/verify/rollback 三元组都对、high-risk step 的 precheck 齐备），录像为证** |
| **P2** | `add-remediation-execution-workflow` | 写（dry-run→真实） | `hostlens fix` 全闭环 | **真实 `exec` 是 P2 最后一个 task**，前面编排全在 dry-run 下验证 |
| **P3** | ~~`add-remediation-lark-approval`~~ → 实交付 `add-risk-tiered-remediation-execution` | 写（仅 low 自动执行） | low 自动闭环 / medium·high 出 runbook | 红线翻转：中高风险不代执行 |

### 任务

#### P1a — `add-remediation-plan-schema`（纯契约，零写零 LLM）

- [x] **9.1 Remediation Plan schema**
  - [x] `remediation/models.py`：`RemediationPlan` / `RemediationStep`（含 `precheck_cmd` / `forward_cmd` / `rollback_cmd` / `verify_cmd` / `risk_level`）
  - [x] **`precheck_cmd: str | None`**（默认 `None`）：执行 `forward_cmd` 前验证假设仍成立（补 `verify_cmd` 的前向对称缺口，挡 TOCTOU / 审批延迟导致的世界漂移，如 PID 复用）
  - [x] 校验规则：`risk_level=="high"` ⟹ `precheck_cmd` 不得为 `None`；`rollback_cmd is None` ⟹ `risk_level=="high"`
  - [x] 纯单元测试覆盖校验规则；**此提案冻结整个 M9 的契约 SOT**

#### P1b — `add-remediation-planner`（Agent 产 Plan，不执行）

- [x] **9.2 Plan 生成 Agent**
  - [x] `agent/remediation_planner.py`：输入 = finding + target，输出 = `RemediationPlan`（structured output，§4.7）；复用只读 Tool Registry 核实状态，**不执行任何 step**
  - [x] 高风险动作（`rm -rf` / `kill -9` / 修改 systemd unit）必须显式标 `risk_level=high`（触发 P2 的双重确认 + 强制 precheck）
  - [x] LLM 调用走 VCR cassette；demo = 喂 finding → 打印 Plan

#### P2 — `add-remediation-execution-workflow`（写路径；dry-run 先行，真实 exec 最后）

- [x] **9.3 预览与审批**
  - [x] `remediation/approval.py`：独立 `ApprovalGate`（**不复用 ToolContext 的 ApprovalService**）
  - [x] CLI: `hostlens fix <run_id>` → 展示 plan diff → 等待 `--yes` 或交互 y/N；`risk_level=high` 走双重确认
  - [x] 非交互无 `--yes` → 退出 1（绝不默默执行）；默认 `--dry-run`
  - [x] 拒绝以 root 身份运行（EUID==0）
- [x] **9.4 执行与回滚**（先全链路 dry-run 验证编排，真实 `target.exec` 接通为本片**最后一个 task**）
  - [x] `remediation/executor.py`：顺序执行 steps；每步先跑 `precheck_cmd`（失败 = 世界已漂移 → 中止）、再 `forward_cmd`、再 `verify_cmd`
  - [x] 任一步未能成功推进（precheck 拒绝 / forward 报错 / verify 失败）→ 倒序跑已成功 step 的 `rollback_cmd`，走统一收尾路径
- [x] **9.5 Audit log**
  - [x] 每次 fix 写 `~/.local/share/hostlens/audit.log`（append-only，永不轮转）：who / when / target / plan / outcomes
  - [x] 失败区分三态：`precheck-blocked`（前提漂移，没碰）/ `forward-failed`（执行报错）/ `verify-failed`（执行了但结果不对）

#### P3 — ~~`add-remediation-lark-approval`~~ → 实交付 `add-risk-tiered-remediation-execution`

> 原飞书远程审批方案按红线翻转为风险分级执行（见本段顶部 banner）。

- [x] **9.6 风险分级执行**
  - [x] **low** 风险：走 9.3–9.5 的自动执行闭环（precheck → forward → verify → rollback-ready + audit）
  - [x] **medium / high** 风险：不代执行，渲染 `remediation/runbook.py` + Jinja2 模板（`templates/runbook.md.j2`）产出人工 runbook，交还操作者在自己终端执行
  - [x] 飞书远程审批 / high-risk 远程触发**被否**（AI 担不了中高风险责任）

### Follow-up：文档遗留清理（在对应 M9 提案里改，不预先动）

- [ ] P1a/P2 提案需清理 `docs/ARCHITECTURE.md` 两处与「Agent 表面永久只读」矛盾的早期示例：§4.10 图里的 `apply_remediation_step` ToolSpec（spec3）、`docker_prune_images(surfaces={"agent","cli"}, side_effects="destructive")` 实战例子
- [ ] `agent/tools_adapter.py` dispatch gate 的 reason 字符串去 `_in_m2` 后缀（误示临时），语义升格为不变量
- [ ] `targets/base.py` Capability 注释撤回 `FILE_WRITE` 的 M9 承诺；`tools/base.py` `NoopApprovalService` / `ToolContext` 注释从「M9 will replace」改为「永久 noop，审批属 Remediation 子系统」

---

## M10 — 通道扩展 + 文档发布

**目标**：把 Notifier 扩到主流通道，完善文档，发布 1.0 到 PyPI。

**对应 OpenSpec proposal**：
- `add-more-notifiers`
- `publish-1.0`

**退出条件**：`pip install hostlens` 就能用；文档站可读；README 有演示视频或 GIF。

### 任务

- [ ] **10.1 通道适配器扩展（每个都遵循 M5 抽象，新增一个文件 + 一个模板目录）**
  - [ ] 钉钉（DingTalk）webhook + 签名
  - [ ] 企业微信（WeCom）webhook
  - [ ] Slack incoming webhook + Block Kit 模板
  - [ ] Email（SMTP，纯文本 + HTML 两版）
  - [ ] 通用 Webhook（POST JSON，模板可自定义 body）
- [ ] **10.2 文档站**
  - [ ] 选型：mkdocs-material
  - [ ] 章节：入门 / 概念（Inspector/Target/Notifier/Agent） / CLI 参考 / Inspector 开发指南 / Notifier 开发指南 / 部署运维 / 架构
- [ ] **10.3 示例 & 演示**
  - [ ] `examples/`：3 套完整示例（Web 服务巡检、DB 主从巡检、K8s 集群日常巡检）
  - [ ] 录一个 2 分钟演示视频或 GIF 放 README 顶部
- [ ] **10.4 发布流程**
  - [ ] GitHub Release workflow：tag → build → PyPI publish（trusted publishing）
  - [ ] CHANGELOG.md
  - [ ] 1.0 发版前跑一遍全量集成测试
- [ ] **10.5 LLM Backend 扩展（按需推进；详见 ARCHITECTURE.md §9 模型层）**
  - [ ] `BedrockBackend`：AWS IAM 认证，企业生产推荐（ToS 干净 + audit 完整）；含完整 `BackendDiagnostics`（quota_check 读 CloudWatch metrics）
  - [ ] `ClaudeSubscriptionBackend`（experimental，标注 dev/demo only）：通过 `claude-agent-sdk` 的 OAuth；`ensure_safe_for_daemon()` 检测 daemon 模式必须 raise；配置必须 `accept_subscription_risks: true`
  - [ ] doctor 子命令：`hostlens doctor --check-backend` 用 duck-type 检测 `BackendDiagnostics` 协议，输出当前 backend 认证状态 + capabilities + 已用配额
  - [ ] 验收：
    - [ ] Bedrock backend 跑通同一套 cassette（除 prompt caching 相关测试外）
    - [ ] Subscription backend 在 daemon 进程中启动必须立刻 exit 1
    - [ ] 切换 backend 不需要修改任何业务代码（只改配置文件 `backend.type`）
- [x] **10.6 OpenRouter backend 配置优化**（`add-openrouter-backend-config` 提案 #97 merged + archived；真端点 2026-06-13 经 `qwen/qwen3.7-plus` 实测通过）
  - [x] `BackendSettings` 加 `extra_headers: dict[str, str] | None` 字段，`AnthropicAPIBackend` 透传给 SDK `default_headers`——支持 OpenRouter 推荐的 `HTTP-Referer` / `X-OpenRouter-Title` 统计 header（`create_backend` 接线层剥离大小写不敏感的 `x-api-key`/`authorization` 防认证覆盖；`__repr__` 对值无条件全遮蔽 `***`）
  - [x] `AnthropicAPIBackend.capabilities` 从 ClassVar 改为构造时实例注入（新构造参数 `prompt_caching: bool=True` + `BackendSettings.prompt_caching: bool|None=None`，定向覆盖单项）——非 Claude 模型置 `False` 使 loop 不注入 `cache_control`，解决 `cache_creation_input_tokens` 恒 0 导致指标失真。注：仅开放 `prompt_caching` 单项覆盖（其余 6 capability 无按模型配置需求，详见提案 D-1）
  - [x] 验收：`HOSTLENS_BACKEND__EXTRA_HEADERS` 透传 + `prompt_caching=False` 行为有单测覆盖（已过）；**真端点验证 2026-06-13**：`create_backend` 读真 `.env`（`base_url=https://openrouter.ai/api` + `prompt_caching=false` + `extra_headers`）经 `qwen/qwen3.7-plus` 发真请求往返成功（返回 `OPENROUTER_OK`），实例 `capabilities.prompt_caching=False`、repr 中 extra_headers 值全遮蔽 `***`、`cache_creation_input_tokens=0`（指标失真已修）。**注**：原 task 5.2 写的 `hostlens demo` 验证不了 OpenRouter（demo 离线写死 PlaybackBackend、不走 create_backend）；真端点验证须走 `create_backend`/`hostlens inspect` agent 路径。**运营发现**（非缺陷）：deepseek/qwen 经 OpenRouter 延迟常 >5s，超 `doctor` health_check 的 5s 硬超时 → doctor 多报 backend timeout；候选 follow-up：health_check timeout 可配置或按 backend 调高

---

## 横向工作（贯穿所有期，不归属单一里程碑）

> 这些是"持续做"的工程纪律，不在某一期里被打勾。每期收尾时回看一遍。

- [ ] **类型完整性**：`mypy --strict` 0 错误
- [ ] **测试覆盖率**：core 模块 ≥ 80%
- [ ] **依赖审计**：定期 `pip-audit`，警告及时升级
- [ ] **Prompt cache hit rate**：每次新增 LLM 调用点都看一遍指标
- [ ] **OpenSpec 卫生**：每完成一期把 `openspec/changes/` 下的提案归档到 `openspec/specs/`
- [ ] **README / CLAUDE.md / config.yaml 同步**：架构演进后及时更新
- [ ] **MCP 资产登记**：当前 MCP server 只有只读三件套；通过 AI 对话完成资产登记的能力已重规划为 M7-ext 写期提案②b 的 `import_targets`（批量迁移，两段式 approval），见上方「M7 后续扩展 — MCP 管控工具集」。在该提案落地前，资产登记须手动编辑 `~/.config/hostlens/targets.yaml` 或用 `hostlens target add` CLI

---

## 不在本路线内的事（Anti-Roadmap）

明确**不做**或**1.0 之后才考虑**的事，避免范围蔓延：

- ❌ Web Dashboard（1.0 之后）
- ❌ 多租户 / 团队权限系统（1.0 之后）
- ❌ Inspector 市场（hosted）（1.0 之后）
- ❌ 自训练 / 微调模型（永远不做，定位是"调用最强模型"）
- ❌ 替代 Prometheus 做指标存储（永远不做，专注"诊断"而非"采集存储"）
- ❌ 在 Inspector 里调 LLM（违反 §4.2，永远不做）
- ❌ 引入 LangChain / LlamaIndex（违反 §4.1，永远不做）
