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
| M3 | Diagnostician + 报告体系 | 跨信号关联 + 根因假设 + regression diff | ⬜ |
| M4 | Scheduler | cron 定时跑 + 历史 run 持久化 | ⬜ |
| M5 | Notifier 抽象 + Telegram + 飞书 | 定时报告自动推送到 TG / 飞书 | ⬜ |
| M6 | 内置 Inspector 库扩充 | 覆盖 Linux/Nginx/MySQL/Redis/Docker 真实场景 | ⬜ |
| M7 | MCP Server | Claude Code / Cursor 能直接调用 Hostlens | ⬜ |
| M8 | Docker + K8s ExecutionTarget | 容器与集群场景 | ⬜ |
| M9 | 受控修复（Remediation） | plan → approve → execute → rollback 闭环 | ⬜ |
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
    - 解析：`parse.format` (raw / table / json / kv / sql_result) / `parse.columns`
    - 输出：`output_schema` (JSON Schema)
    - 判定：`findings` (`for_each` / `when` / `severity` / `message` 四字段 DSL；详见 docs/ARCHITECTURE.md §4 Finding DSL 求值语义)
    - 元数据：`artifacts` (列出额外产物，如"导出最近 50 行日志作为附件")
  - [x] `inspectors/loader.py`：YAML 解析 + JSON Schema 校验 + 可选 `hook.py` 加载 + 密钥占位展开
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

**对应 OpenSpec proposal**：
- `add-diagnostician-agent`
- `add-report-persistence-and-diff`

**退出条件**：报告中能看到 "📌 根因假设" 章节，包含证据链接；对同一 target 跑两次能输出 "本次相对上次新增了 X、消失了 Y" 的 diff。

### 任务

- [ ] **3.1 Diagnostician Agent**
  - [ ] `agent/diagnostician.py`：输入 = findings 列表 + intent，输出 = 带根因假设的报告
  - [ ] 暴露的工具：`correlate_findings(ids)` / `request_more_inspection(inspector_name)`（允许补查）
  - [ ] 提示词在 `agent/prompts/diagnostician.md`
- [ ] **3.2 报告 schema 完善（与 docs/ARCHITECTURE.md §10 对齐 —— §9 failure semantics 与 §10 diff 基线选取依赖这些字段）**
  - [ ] `RootCauseHypothesis`：description / confidence / supporting_findings / suggested_actions
  - [ ] `ReportStatus` Enum：ok / partial / degraded_no_planner / degraded_rate_limited / degraded_token_budget / degraded_max_turns / empty_response / stored_as_orphan（注意：`failed_api_unavailable` 不在此 enum，归在 `RunStatus` 因为该场景无 Report，详见 ARCHITECTURE §7）
  - [ ] `InspectorRun`：name / version / status (ok/timeout/target_unreachable/requires_unmet/exception) / duration_seconds / finding_count
  - [ ] `BaselineRef`：run_id / timestamp / status / inspector_versions / report_schema_version
  - [ ] `ReportMeta`：run_id / report_schema_version / timestamp(tz-aware) / target_id / target_name / target_type / intent / schedule_name / status / inspectors_used (list[InspectorRun]) / token_usage / duration_seconds / baseline_ref / diff_skipped_reason
  - [ ] `Finding` 增加 `inspector_version` 字段（diff 指纹与基线对齐要用）
  - [ ] 验收：M2 已生成的 fixture 报告升级到新 schema；旧字段读取兼容；schema 变更日志归档
- [ ] **3.3 报告持久化**
  - [ ] `reporting/store.py`：SQLite（per-target 一个文件 or 单库），存 run 记录与报告 JSON
  - [ ] CLI: `hostlens reports list <target>` / `hostlens reports show <run_id>`
- [ ] **3.4 报告渲染扩展**
  - [ ] `reporting/render_html.py`：基于 Jinja2 模板，含交互式 finding 展开
  - [ ] markdown 渲染增加根因章节
- [ ] **3.5 Regression diff 引擎**
  - [ ] `reporting/diff.py`：两份报告对比，输出 `added` / `resolved` / `changed_severity`
  - [ ] CLI: `hostlens reports diff <run_id_a> <run_id_b>`
  - [ ] 也作为定时巡检报告里的一个 section（M5 用到）
- [ ] **3.6 extended-thinking 支持（独立提案 `support-extended-thinking`；M2 显式不支持，见 backend.py 注释「M3+ Diagnostician」）**

  > **触发背景**：M2.6 用 DeepSeek 做 live 测试时发现 `deepseek-v4-pro/flash` 经其 anthropic 兼容端点**强制返回 `type="thinking"` 块**，撞 M2 `MessageResponse` 只建模 `text`/`tool_use` 的 scope → 解析崩。Diagnostician（3.1）若用推理模型也会受益。**reference memory**：`deepseek-v4-thinking-incompatible-live-test`。

  - [ ] **支柱①** `MessageResponse.content` 的 `ContentBlock` 联合新增 `ThinkingBlock{type,thinking,signature}` + `RedactedThinkingBlock{type,data}`（按 `type=="thinking"` 过滤会丢 redacted_thinking → 破多轮协议，务必两者都建模）
  - [ ] **支柱②** `LLMBackend.messages_create` + Protocol 加 `thinking` 参数（`{type:enabled,budget_tokens}` / `disabled` / `adaptive`）+ `BackendCapabilities.extended_thinking=True`（对应 backend）
  - [ ] **支柱③** Agent loop 工具多轮**原样保留并按序回传 thinking 块**（signature 不变、顺序不变、不可省略——Anthropic/DeepSeek 带工具时省略→400）；cache_control 断点**不能打在 thinking 块上**（「pass unchanged」要求，断点挪到末尾 tool_use/text）
  - [ ] **支柱④（关键，与 M2.6 协同）** cassette keying 必须**归一化掉 thinking/redacted_thinking 块再 hash**（thinking 文本与 signature 都**非确定**，否则 record→replay 永不命中）；cassette 仍**存完整响应**（含 thinking 供回放回传），只在 `request_key_for_payload` 投影 messages 时 drop thinking 块——是加归一化前置步，**不**改 keying 算法形状
  - [ ] **近期兜底（可并入本提案）**：在 extended_thinking=False 时，backend adapter 对默认开 thinking 的 provider（如 DeepSeek）发 `thinking:{type:"disabled"}`（需实测生效）；意外收到 thinking 块时 `MessageResponse` 解析给清晰 `BackendError(kind="unsupported_content_block")` 而非裸 `ValidationError`
  - [ ] 验收：真 Anthropic key 开 thinking 跑通工具多轮 + cassette record→replay 命中；DeepSeek v4 经 anthropic 端点跑通

---

## M4 — Scheduler

**目标**：让 Hostlens 能按 cron / interval 自动跑巡检，运行结果持久化，daemon 模式稳定。

**对应 OpenSpec proposal**：`add-scheduler`

**退出条件**：`hostlens schedule daemon` 起来后能按 `schedules/*.yaml` 定时跑巡检，SIGTERM 优雅停机不丢任务。

### 任务

- [ ] **4.1 Schedule manifest schema**
  - [ ] `scheduler/schema.py`：`ScheduleManifest`（cron/interval、timezone、targets、**intent 必填、inspectors 可选作为优先 hint**、report 配置、notify 配置占位）
  - [ ] `scheduler/loader.py`：扫 `schedules/*.yaml`
- [ ] **4.2 APScheduler 封装**
  - [ ] `scheduler/runner.py`：基于 `AsyncIOScheduler`，每个 manifest 注册为一个 job
  - [ ] job 执行 = 调 M2 的 Agent loop
- [ ] **4.3 Run 记录（与 ARCHITECTURE.md §7 RunStatus enum 对齐）**
  - [ ] `scheduler/store.py`：每次触发记录 `Run`（run_id / schedule_name / triggered_at / started_at / finished_at / status / report_id / error / notify_results）
  - [ ] `RunStatus` Enum：ok / partial / budget_exhausted / missed / skipped_due_to_running / failed_api_unavailable / failed / daemon_stopped（详见 ARCHITECTURE.md §7）
  - [ ] 验收：`Run.report_id is None` **当且仅当 `Run.status not in {ok, partial}`**（涵盖所有无 Report 的 RunStatus 值，与 ARCHITECTURE.md §7 边界表一致）；doctor 能输出最近 N 次 Run 状态分布
- [ ] **4.4 Daemon 模式**
  - [ ] `cli/schedule.py`：`run`（前台）/ `daemon`（后台）/ `list` / `trigger <name>` / `status`
  - [ ] SIGTERM 信号：停止接受新任务，等当前 job 完成再退出
  - [ ] daemon 模式日志写文件 + structlog json
- [ ] **4.5 doctor 集成**
  - [ ] `hostlens doctor` 增加 schedule 健康检查（next_fire_time 是否合理、上次 run 是否失败）

---

## M5 — Notifier 抽象 + Telegram + 飞书

**目标**：实现"业务通用化、可扩展"的核心证明 —— Notifier 适配器模式。内置 Telegram 和飞书两个通道，做好抽象让后续加通道只是"新增一个文件"。

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

**对应 OpenSpec proposal**：`expand-builtin-inspectors`（按域分多个 sub-proposal）

**退出条件**：覆盖矩阵下每个域至少有 3 个 Inspector，总计 ≥40 个，每个 Inspector 有 manifest + snapshot 测试 + 在 `examples/` 里有可 replay 的 fixture。

### 覆盖矩阵

| 故障域 | 必须覆盖 | M2 计划基线（M2.8） | M6 新增 |
|---|---|---|---|
| 计算（CPU） | top processes / load avg / throttling / cpufreq | top_processes, load_avg | cpu.throttling, cpu.cpufreq |
| 内存 | pressure / swap / OOM history / hugepages | memory.pressure, kernel.oom_killer | memory.swap, memory.hugepages |
| 磁盘 / FS | usage / inode / IO / SMART / mount health / logrotate | disk.usage, fs.inode_pressure | disk.io, disk.smart, fs.mount_health, fs.logrotate |
| 网络 | connections / listening ports / dependency probes / DNS / NTP | dependency.tcp_check | network.connections, network.listening_ports, dns.resolve, ntp.drift |
| 进程 | zombies / FD 耗尽 / 总数 / 关键进程存活 | process.fd_usage | process.zombies, process.total, process.critical_alive |
| 服务管理器 | systemd failed units / timers / masked units | systemd.failed_units | systemd.timer_status, systemd.masked |
| 调度器 | cron 历史 / anacron / 失败 cron | — | cron.last_runs, cron.failures |
| TLS | cert 过期 / cert chain 有效性 | tls.cert_expiry | tls.chain_validity |
| 内核 / 系统 | dmesg errors / kernel taint / reboot-required / uptime | — | system.kernel_messages, system.uptime, system.reboot_required, system.kernel_taint |
| 安全基线 | failed logins / sudo history / 异常监听端口 | — | security.failed_logins, security.sudo_history, security.unexpected_listen |
| 包管理 | 待升级包 / 安全补丁 | — | pkg.pending_updates, pkg.security_patches |
| Web / Nginx | health / config test / 5xx rate / upstream health | — | nginx.health, nginx.config_test, nginx.error_rate, nginx.upstream |
| MySQL | conn usage / slow queries / replication lag / deadlocks | — | mysql.connection_usage, mysql.slow_queries, mysql.replication_lag, mysql.deadlocks |
| PostgreSQL | conn usage / replication lag / bloat（真实 SQL）/ long queries | — | postgres.connection_usage, postgres.replication_lag, postgres.bloat_tables, postgres.long_queries |
| Redis | memory / persistence / replication / slowlog | — | redis.memory_usage, redis.persistence, redis.replication_lag, redis.slowlog |
| Docker（SSH 跨） | unhealthy / restart loop / image disk / network | — | docker.containers.unhealthy, docker.containers.restart_loop, docker.images.disk_usage, docker.networks |
| K8s（M8 target 就位后） | pod OOM history / evicted / pending / node pressure | — | k8s.pods.oom_history, k8s.pods.evicted, k8s.pods.pending, k8s.nodes.pressure |
| 运行时（JVM） | heap usage / GC pressure / thread count | — | jvm.heap, jvm.gc, jvm.threads |
| 运行时（Go） | goroutine count / heap from pprof | — | go.goroutines, go.heap |
| 日志 | error burst / exception 突增 | log.tail.error_burst | log.exception_burst |

> 说明：「M2 计划基线」列列出 §M2.8 **已交付**的 Inspector（见 `src/hostlens/inspectors/builtin/`，PR #38/#42）；M6 在此基础上按域扩充。每个 Inspector 落地时必须勾上覆盖矩阵对应单元格。

### 实施任务（按域）

- [ ] **6.1 计算 / 内存 / 磁盘扩充**（cpu.throttling, memory.swap, disk.io, disk.smart, fs.mount_health, fs.logrotate, memory.hugepages, cpu.cpufreq）
- [ ] **6.2 网络 + DNS + NTP**（network.connections, network.listening_ports, dns.resolve, ntp.drift）
- [ ] **6.3 systemd + cron**（systemd.timer_status, systemd.masked, cron.last_runs, cron.failures）
- [ ] **6.4 TLS + 安全基线 + 包管理**（tls.chain_validity, security.failed_logins, security.sudo_history, security.unexpected_listen, pkg.pending_updates, pkg.security_patches）
- [ ] **6.5 Nginx**（health, config_test, error_rate, upstream）
- [ ] **6.6 MySQL / PostgreSQL**（含真实 SQL 模板，如 postgres bloat 用 `pg_stat_user_tables` 的具体查询，参考 docs/ARCHITECTURE.md §4 复杂示例 2）
- [ ] **6.7 Redis**（memory_usage, persistence, replication_lag, slowlog）
- [ ] **6.8 Docker（SSH 跨）**（containers.unhealthy, containers.restart_loop, images.disk_usage, networks）
- [ ] **6.9 JVM / Go 运行时**（jvm.heap, jvm.gc, jvm.threads, go.goroutines, go.heap）
- [ ] **6.10 进程 + 内核 + 日志**（process.zombies, process.total, process.critical_alive, system.reboot_required, system.kernel_taint, log.exception_burst）
- [ ] 验收：每个 Inspector 必须有 fixture + snapshot 测试 + 覆盖矩阵里的位置勾上 + 在 `examples/` 里给出至少一个 demo 场景

---

## M7 — MCP Server

**目标**：把 Hostlens 暴露成 MCP server，让 Claude Code / Cursor 把它当作工具调用。

**对应 OpenSpec proposal**：`add-mcp-server`

**退出条件**：Claude Code 配置 Hostlens 后，能在对话里直接说"帮我巡检 prod-web-01"并看到完整工具调用链。

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

---

## M8 — Docker + Kubernetes ExecutionTarget

**目标**：补全容器与集群场景，验证 `ExecutionTarget` 抽象的真正扩展性。

**对应 OpenSpec proposal**：
- `add-docker-execution-target`
- `add-kubernetes-execution-target`

**退出条件**：Inspector 不修改代码，仅 manifest 的 `targets:` 字段加上 `docker` / `k8s` 就能在容器内/Pod 内跑。

### 任务

- [ ] **8.1 DockerTarget**
  - [ ] `targets/docker.py`：基于 docker-py，`exec_run` 异步包装
  - [ ] capability 声明（无 systemd / 无 SSH）
  - [ ] 容器选择：name / id / label selector
- [ ] **8.2 KubernetesTarget**
  - [ ] `targets/k8s.py`：基于 kubernetes-asyncio，`pod exec`
  - [ ] selector：namespace + label / pod name
  - [ ] 多容器 pod：必须显式指定 container
- [ ] **8.3 Capability 系统打磨**
  - [ ] Inspector 加载时根据 manifest `targets:` 与实际 target capabilities 做兼容性匹配
  - [ ] 不兼容时 CLI 给出清晰错误：哪个 capability 缺失
- [ ] **8.4 CLI: target add 支持 docker/k8s**
  - [ ] `hostlens target add my-pod --type k8s --namespace prod --label app=api`

---

## M9 — 受控修复（Remediation）

**目标**：闭环故事 —— Agent 不光能看出问题，还能提出修复建议，经过人工审批后执行，并预备好回滚路径。

**对应 OpenSpec proposal**：
- `add-remediation-plan-schema`
- `add-remediation-execution-workflow`

**退出条件**：对一个典型问题（如 `/var/log` 占满磁盘），Agent 能生成 plan、CLI 展示 diff、`--yes` 后执行、记录 audit log、保留回滚命令。

### 任务

- [ ] **9.1 Remediation Plan schema**
  - [ ] `remediation/models.py`：`RemediationPlan` / `RemediationStep`（含 `forward_cmd` / `rollback_cmd` / `verify_cmd` / `risk_level`）
- [ ] **9.2 Plan 生成 Agent**
  - [ ] `agent/remediation_planner.py`：输入 = finding + target，输出 = RemediationPlan
  - [ ] 高风险动作（`rm -rf` / `kill -9` / 修改 systemd unit）必须显式标 `risk_level=high` 并要求双重确认
- [ ] **9.3 预览与审批**
  - [ ] CLI: `hostlens fix <run_id>` → 展示 plan diff → 等待 `--yes` 或交互 y/N
  - [ ] 非交互无 `--yes` → 退出 1（绝不默默执行）
  - [ ] 拒绝以 root 身份运行（EUID==0）
- [ ] **9.4 执行与回滚**
  - [ ] `remediation/executor.py`：顺序执行 steps，每步跑 verify_cmd
  - [ ] 任一步失败 → 倒序跑前面已执行 step 的 rollback_cmd
- [ ] **9.5 Audit log**
  - [ ] 每次 fix 写 `~/.local/share/hostlens/audit.log`：who / when / target / plan / outcomes
- [ ] **9.6 飞书卡片交互按钮**
  - [ ] M5 预留的卡片按钮接通：飞书群里点"批准修复"→ 触发执行（带 token 校验）
  - [ ] 标记为 experimental，开关默认 off

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

---

## 横向工作（贯穿所有期，不归属单一里程碑）

> 这些是"持续做"的工程纪律，不在某一期里被打勾。每期收尾时回看一遍。

- [ ] **类型完整性**：`mypy --strict` 0 错误
- [ ] **测试覆盖率**：core 模块 ≥ 80%
- [ ] **依赖审计**：定期 `pip-audit`，警告及时升级
- [ ] **Prompt cache hit rate**：每次新增 LLM 调用点都看一遍指标
- [ ] **OpenSpec 卫生**：每完成一期把 `openspec/changes/` 下的提案归档到 `openspec/specs/`
- [ ] **README / CLAUDE.md / config.yaml 同步**：架构演进后及时更新

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
