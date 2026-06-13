## 为什么

`hostlens doctor` 对 backend 做连通性探测时，用一个**硬编码 5 秒**超时包裹 `BackendDiagnostics.health_check()`（`src/hostlens/cli/doctor.py:572` 的 `_BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS = 5.0`）。M10.6 接入 OpenRouter 后实测发现：经 OpenRouter 路由的慢模型（DeepSeek / Qwen 等推理系，含排队 + 首 token 延迟）一次 `max_tokens=10` 的 ping 经常 **>5s**，导致 doctor 把一个**健康但慢**的 backend 误报成 `health_check_is_healthy=false` + `health_check timeout after 5.0s`。

这是个**误导性诊断**而非硬失败（`_is_ready` 不消费 backend 健康行，exit code 不受影响），但操作者看到红色 "unhealthy" 会误判 backend 坏了。5s 这个常量对默认的 Haiku ping（典型 <1s）够用，对项目刚投入支持的 OpenRouter 慢模型场景偏紧 —— 应让它可配置。

## 变更内容

- **新增配置项** `agent.health_check_timeout_seconds`（`AgentSettings` 字段，紧邻已有的 `health_check_model`），`float`，默认 **10.0**，约束 `ge=1, le=120`；env 覆盖走 `HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS`。
- **doctor 改为读配置**：`_check_backend` 把 `asyncio.wait_for(...)` 的 `timeout=` 从模块常量改成 `settings.agent.health_check_timeout_seconds`；`settings.agent is None`（M0/M1 配置）时回落到默认常量（与 `health_check_model` 现有的 `settings.agent if ... else 默认` 回落模式对齐）。
- **默认值小幅上调** 5.0 → 10.0：让多数"健康但略慢"的 backend（含 OpenRouter）**开箱即过**而不必配置，同时仍有界、doctor 不会挂死；真正的慢推理 backend 可配到上限 120s。
- 超时错误文案保持 `health_check timeout after {N}s` 形态（`N` 改为从配置读出的实际值），仍为**信息性**输出，**不翻转** doctor `ready` / exit code。

## 功能 (Capabilities)

### 新增功能
（无）

### 修改功能
- `core-services`: `AgentSettings` 配置 schema 增加 `health_check_timeout_seconds` 字段（默认值 / 数值边界 / env 覆盖路由 / 回落语义）。
- `llm-backend-protocol`: `hostlens doctor` 包裹 `health_check` 的硬超时由「硬编码 5s」改为「从 `settings.agent.health_check_timeout_seconds` 读取，`agent` 缺省时回落默认」；并明确超时为信息性、不翻转 `ready`。

## 影响

- **代码**：
  - `src/hostlens/core/config.py` —— `AgentSettings` 加一个 `Field`。
  - `src/hostlens/cli/doctor.py` —— `_check_backend` 读配置；`_BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS` 降级为「`settings.agent is None` 时的回落默认值」常量（值同字段默认 10.0，单一真相靠断言钉死，见 design D-3）；该常量的 docstring（`doctor.py:573-577`，现描述「5 s ceiling for Haiku ping」）必须改写为「回落默认」角色。
  - `src/hostlens/cli/_doctor_schema.py` —— `BackendHealthRow.health_check_is_healthy` 字段上方 docstring（`:197-199`）硬编码 `"within the 5-second timeout"`，本变更后变成假陈述（且属 §6 禁止的「是什么」注释），必须改为「configured health-check timeout」之类不钉死秒数的措辞。`BackendHealthRow` 的 schema 字段集（6 个固定字段 + `extra="forbid"`）**不变**。
- **文档**（与本变更的 OpenRouter 动机直接相关，须同步否则新旋钮无法被发现）——用**自完整判据**而非脆弱的文件清单（防漏列）：
  - **判据 A（用户面配置文档，scope 内自完整）**：凡**逐字段列举 sibling agent 旋钮的用户面配置文档**都须补 `health_check_timeout_seconds`（保持各自语法形态）。scope = `docs/ + .env.example`（用户照抄配置的来源），集合 = `grep -rli "token_budget_input" docs/ .env.example`（**`-i`**：`.env.example` 用大写 env 形），当前 = `{docs/ARCHITECTURE.md（配置 schema 的 agent YAML 块，:1114-1120）, docs/MIGRATION.md（:14-29 agent 块）, .env.example（:48-53 通用参数块）}`。`.env.example` 用 env 形 `# HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS=10`，两个 YAML 块用 `health_check_timeout_seconds: 10`。**自完整仅限该 scope（非全仓）**：未来任何**新增到 `docs/`/`.env.example` 的**同类逐字段文档会被同一 grep 兜住。**显式排除 `TODO.md:221`**——它虽逐字段列 agent 字段（含 `token_budget_input`）但是一条**已勾选 `- [x]` 的 M2(`add-llm-backend-protocol`) 交付台账**；`health_check_timeout_seconds` 是 M10.6 新增字段，塞进勾掉的 M2 交付行属**历史失真**（它不是供用户复制的配置样例，是「M2 交付了哪些字段」的历史快照），故刻意不补。
  - **判据 B（OPERABILITY SOT）**：`docs/OPERABILITY.md:6` 自声明为「健康检查」等主题的**单一事实来源**、且「任何 OpenSpec proposal 涉及对应主题**必须引用本文档**」。本变更主题正是 doctor 健康检查探测超时，故须在 OPERABILITY 相关配额/健康检查表登记该旋钮（默认 / 配置键 `agent.health_check_timeout_seconds` / 用途）。注意 OPERABILITY 用配置键命名（`agent.token_budget`），不被判据 A 的 `token_budget_input` grep 兜住，故单列。
  - **判据 C（doctor-unhealthy 排障处方）**：`docs/ARCHITECTURE.md:1138` 现把「第三方端点 doctor 报 backend 不健康」**唯一**归因到 `health_check_model` model id 不被认；须补**第二个**致因（健康但慢的 backend ping >timeout → 误报 timeout）+ 处方（调高 `health_check_timeout_seconds`），否则按现有排障文档操作的 OpenRouter 用户改对 model id 后仍见 unhealthy、学不到新开关。
- **对外契约影响**：
  - **CLI 命令**：`hostlens doctor` / `hostlens doctor --json` 行为不变（输出字段不增不减，仅超时阈值与错误文案里的秒数随配置变化）。
  - **Config schema**：`agent` namespace 新增一个可选字段（add-only，向后兼容；旧配置不写该字段 → 用默认 10.0）。
  - Inspector / Agent tool / MCP tool / Notifier / Schedule manifest schema **均不受影响**。
- **依赖**：无新增依赖。

## 非目标（Non-Goals）

- **不**做 per-model-id 的超时映射表（如 `{deepseek-r1: 40s, haiku: 5s}`）—— 一个 agent 块通常对一个 backend，单值配置已够；映射表是过度工程。
- **不**做"自动探测慢 backend 并动态调超时" —— 显式配置优于隐式魔法。
- **不**改 doctor 里 target-exec 探测的 5s 超时（`doctor.py:254` 的 `echo hostlens-doctor-probe`）—— 那是本地 shell 探测，与 backend HTTP ping 是两回事。
- **不**让 backend 健康行参与 `_is_ready` / 翻转 exit code —— 维持现状（backend 健康是信息性诊断，不是本地就绪门）。
- **不**改 `BackendDiagnostics.health_check()` 自身的 `messages_create` 调用语义（仍 `messages=[{"role":"user","content":"ping"}], max_tokens=10`）。

## Failure Modes

1. **配置值越界**（`0` / `200` / 负数命中 `ge=1`/`le=120`；非数字如 `"abc"` 命中 Pydantic「valid number」校验）：均在 `load_settings()` 阶段 raise `ConfigError`，doctor 不会带病运行 —— fail-loud。脱敏方面：`_format_validation_error` 的 `_is_sensitive` 匹配的是**字段名**（非值），`health_check_timeout_seconds` 名不含 `key/token/secret/password/credential` 故不触发脱敏、input 值原样保留在 ConfigError 中（该值本身无敏感信息）。
2. **配置仍偏小、真实 backend 更慢**：超时照旧触发，doctor 输出 `health_check timeout after 10.0s`（信息性），exit code 不受影响；操作者据文案把值再调高。降级行为与今天一致，只是阈值可控。
3. **`settings.agent is None`（M0/M1 无 agent 块）**：回落到默认 10.0，行为确定，不 `AttributeError`。
4. **backend HTTP 端点真的挂死**：`asyncio.wait_for` 在配置上限（≤120s）强制取消，doctor 不会无限阻塞 —— `le=120` 上界保证 doctor 始终有界返回。

## Operational Limits

- **并发预算**：无变化 —— `_check_backend` 仍是 doctor 进程内单次 `asyncio.run(asyncio.wait_for(...))`，不引入并发。
- **内存预算**：无变化（一次 `max_tokens=10` 的 ping）。
- **超时设置**：唯一被本提案触及的超时；默认 10.0s，可配 `[1, 120]`s。上界 120s 是「doctor 仍可接受的最长阻塞」与「慢推理 backend ping 真实耗时」的折中（对照 `DaemonSettings.shutdown_grace_seconds` 的 `le=600`，doctor 探测比优雅停机对响应性要求更高，故取更紧的 120）。

## Security & Secrets

- **不**引入新密钥、不扩大攻击面。
- 超时错误文案 `health_check timeout after {N}s` 只含一个数字，无敏感信息；现有 `BackendHealth.error` 的 `redact_text` 脱敏链路不受影响（本提案不改 health_check 返回路径）。
- 新字段 `health_check_timeout_seconds` 名不含 `key/token/secret/password/credential`，`load_settings()` 的字段名脱敏正则不会（也无需）命中它。

## Cost / Quota Impact

- **零额外 API 调用**：doctor 仍是每次 1 次 ping。
- **token 消耗不变**：`max_tokens=10` 的廉价探测，走 `health_check_model`（默认 Haiku）。
- 默认 5→10s 上调**不增加** API 调用频次或 token 用量，只改客户端等待上限。

## Demo Path

无需付费 API / 无需真 backend，3 步本地复现（≤5 分钟）：

1. **默认值**：构造 `Settings()`（无 agent 块）或带 agent 块，断言 `settings.agent.health_check_timeout_seconds == 10.0`（或 `settings.agent is None` 时 doctor 回落常量为 10.0）。
2. **可配置 + env 覆盖**：`HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS=40 hostlens doctor --json`，断言无报错且探测用 40.0s 上限（env 字符串 coerce 成 float `40.0`；超时文案 f-string 渲染为 `...after 40.0s`，非 `40s`）；越界值 `=0` / `=200` / 非数字 `=abc` 断言 `load_settings()` raise `ConfigError`。
3. **慢 backend 误报修复**：monkeypatch 一个**实现 `BackendDiagnostics`** 的 backend（如 `AnthropicAPIBackend.health_check`，复用 `tests/cli/test_doctor_backend.py:147-168` 脚手架）使其 sleep —— **注意 `FakeBackend`/`PlaybackBackend` 不实现 `BackendDiagnostics`，doctor 在 isinstance 处早退、根本不进 `wait_for`，故不能用它们做这条 Demo**。让 `health_check()` sleep > timeout：旧 5s 常量下 doctor 报 timeout，配 `health_check_timeout_seconds` 调高后报 healthy；用单测固化（不需真网络，见 tasks 3.1 的合法时长约束）。
