## 上下文

`hostlens doctor` 的 backend 连通性探测在 `src/hostlens/cli/doctor.py:_check_backend` 里用 `asyncio.wait_for(backend.health_check(), timeout=_BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS)` 包裹，常量硬编码 `5.0`（`doctor.py:572`）。M10.6 接入 OpenRouter 后实测：经 OpenRouter 路由的慢推理模型（DeepSeek / Qwen，含排队 + 首 token 延迟）一次 `max_tokens=10` 的 ping 常 >5s，doctor 把健康 backend 误报成 timeout。

关键约束（不可破坏）：
- `_is_ready`（`doctor.py:536`）**不**消费 backend 健康行 —— 所以这是误导性诊断，不是硬失败；本提案**不能**改成「health_check 超时翻转 exit 1」。
- 配置 schema 已有先例：`AgentSettings.health_check_model`（`config.py:172`）+ `create_backend` 处 `settings.agent.health_check_model if settings.agent else "claude-haiku-4-5"` 的回落模式（`backend.py:536`）。新字段必须对齐这套既有形态。
- `mypy --strict` 必过；不引新依赖。

## 目标 / 非目标

**目标：**
- 把 doctor 包裹 `health_check()` 的硬超时从硬编码 5s 改成可配置 `agent.health_check_timeout_seconds`。
- 默认值小幅上调 5.0 → 10.0，让多数「健康但略慢」backend（含 OpenRouter）开箱即过、无需配置。
- 数值有界 `[1, 120]`，保证 doctor 始终有界返回、不被挂死 backend 拖垮。
- env 覆盖 `HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS`，与既有 nested 字段一致。

**非目标：**
- 不做 per-model-id 超时映射表。
- 不做自动探测慢 backend 动态调超时。
- 不改 target-exec 探测的 5s（`doctor.py:254`，本地 shell，无关）。
- 不让 backend 健康行参与 `_is_ready` / 翻转 exit code。
- 不改 `health_check()` 自身的 `messages_create` 调用语义。

## 决策

### D-1：字段放 `AgentSettings`，不放 `BackendSettings`

- **选择**：`AgentSettings.health_check_timeout_seconds: float`，紧邻 `health_check_model`。
- **理由**：CLAUDE.md §4.11 rule #4 —— backend namespace 管「与谁通信 / 如何认证」，agent namespace 管「用哪个模型 / 行为参数」。健康探测的等待上限是 doctor 探测**行为参数**，且与之配对的 `health_check_model` 已在 agent 块；放一起语义内聚、回落代码可复用同一个 `settings.agent if ... else 默认` 分支。
- **替代**：放 `BackendSettings`（被否：与认证/transport 无关，且会和 `health_check_model` 拆散到两个 namespace）。

### D-2：默认值 5.0 → 10.0

- **选择**：`Field(default=10.0, ge=1, le=120)`。
- **理由**：实测 OpenRouter 慢模型 ping 常落在 5-10s 区间；10s 让这批「健康但慢」backend 开箱即过，同时 10s 仍在操作者「这是不是挂了？」阈值内。本项目刚在 M10.6 投入 OpenRouter 支持，默认值应匹配被支持的现实而非最快的 Haiku。
- **上界 120**：真慢推理 backend（reasoning 系经 OpenRouter 免费档）worst-case ping 可达数十秒；120s 留足头寸又封顶。对照 `DaemonSettings.shutdown_grace_seconds` 的 `le=600`——doctor 探测对响应性要求高于优雅停机，故取更紧的 120。
- **下界 1**：`0`/负数会让 `wait_for(timeout=0)` 立刻取消每次探测（等于禁用健康检查），`ge=1` 禁掉。
- **替代**：保持默认 5.0 仅加可配置（被否：OpenRouter 已是一类受支持场景，零配置仍误报体验差）。

### D-3：模块常量降级为「`settings.agent is None` 回落值」

- **选择**：`doctor.py` 保留一个常量（值 = 字段默认 10.0）作为 `settings.agent is None`（M0/M1 无 agent 块）时的回落；`_check_backend` 读 `settings.agent.health_check_timeout_seconds if settings.agent is not None else <常量>`。
- **理由**：完全对齐 `health_check_model` 在 `create_backend`（`backend.py:536`）的回落写法；不引入新的 None-safety 风险。常量值与字段默认必须一致，用相邻注释 + **防漂移测试断言**钉死（task 2.2：`常量 == AgentSettings().health_check_timeout_seconds`，二者不一致即测试红）。
- **回落值来源——明确取「字面常量 + 断言」，否决「引用 `model_fields[...].default`」**：`pydantic.fields.FieldInfo.default` 静态类型是 `Any`，在 `mypy --strict` 下把它当 `timeout: float` 传会触发 `Any` 泄漏（违反 CLAUDE.md §6「不允许 `Any`」），需额外 `float(...)` cast / `PydanticUndefined` 守门才干净——得不偿失。故回落用一个字面 `float` 常量（`= 10.0`），漂移风险交 task 2.2 的运行时断言兜（读 `AgentSettings()` 的真实默认比对），既 `--strict` 干净又零漂移。
- **替代**：`settings.agent is None` 时 raise（被否：破坏 M0/M1 配置可直接跑 doctor 的现状）。

### D-4：超时文案随配置变、语义不变

- 错误文案 `f"health_check timeout after {N}s"` 的 `N` 改为读出的实际配置值（旧代码已是 f-string 插常量，改成插变量即可）。仍为信息性输出，`_is_ready` 不动。

## 风险 / 权衡

- **[默认值变更是行为变更]** → 缓解：纯严格改善（更少误报），且 backend 健康不参与 exit code，无回归面；提案 What Changes / 本 design D-2 显式声明默认 5→10。
- **[常量与字段默认漂移]**（有人改字段默认忘了改回落常量）→ 缓解：加一条单测断言 `_BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS == AgentSettings().health_check_timeout_seconds`（task 2.2）。不走 `model_fields[...].default` 自动同步——见 D-3，那条会泄漏 `Any`。
- **[配 120s 仍超]** → 缓解：照旧输出信息性 timeout 文案，操作者据文案再调；exit code 不受影响，doctor 仍有界返回。
- **[误以为可借此让 doctor 在 backend 不健康时 fail]** → 缓解：spec 显式钉死「backend 健康是信息性、禁止翻转 ready」+ 对应场景，防止后续误改。

## Migration Plan

- 纯 add-only：旧配置不写 `health_check_timeout_seconds` → 取默认 10.0，行为只比旧 5.0 更宽松。无数据迁移、无 schedule run 记录影响、无 PyPI 兼容性破坏。
- 回滚：还原两文件改动即可（字段删除 + 常量复原 5.0）；无持久化状态。

## Open Questions

- （已在 D-3 收口）回落值来源已定为「字面常量 + 防漂移断言」；`model_fields[...].default` 路因 `FieldInfo.default: Any` 在 `--strict` 下泄漏 `Any`、违反 §6 而否决。无剩余待决项。
