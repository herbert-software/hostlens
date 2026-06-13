## 修改需求

### 需求:`BackendDiagnostics` 必须是独立可选 Protocol（duck-type）

`hostlens.agent.backend.BackendDiagnostics` 必须定义为独立 `@runtime_checkable typing.Protocol`（**不**与 `LLMBackend` 继承/组合；`@runtime_checkable` 装饰器**必填**，否则 `isinstance(backend, BackendDiagnostics)` 会 raise `TypeError`），含以下成员：

- `async def health_check(self) -> BackendHealth`
- `async def quota_check(self) -> QuotaStatus | None`（返回 None 表示该 backend 不支持配额探测）
- `def ensure_safe_for_daemon(self) -> None`（不安全场景 raise `BackendDaemonUnsafe`；no-op 默认行为）

`BackendHealth` Pydantic 模型字段：

- `is_healthy: bool`
- `backend_name: str`
- `latency_ms: float | None`（最近一次 ping 延迟）
- `error: str | None`（不健康时的脱敏错误消息）

`QuotaStatus` Pydantic 模型字段：

- `remaining_input_tokens: int | None`
- `remaining_output_tokens: int | None`
- `reset_at: datetime | None`

`hostlens doctor` 命令必须 duck-type 检测 backend 是否实现 `BackendDiagnostics`，是则调 `health_check`；**禁止**强制所有 backend 实现 diagnostics。

doctor 对 `health_check()` 的调用**必须**用一个硬超时（`asyncio.wait_for`）包裹，超时秒数**必须**从 `settings.agent.health_check_timeout_seconds` 读取；`settings.agent is None`（M0/M1 配置无 agent 块）时**必须**回落到与该字段默认值一致的常量（10.0s），**禁止** `AttributeError`。超时触发时 doctor **必须**把 backend 健康行置为 `health_check_is_healthy=False` 并写入形如 `health_check timeout after {N}s` 的错误文案（`{N}` 为实际生效的配置秒数）。该超时结果**是信息性诊断**：**禁止**让 backend 健康（含 health_check 超时 / 失败）参与 `_is_ready` 计算或翻转 doctor 的 exit code —— backend 健康行只反映「连通性观测」，不是「本地就绪门」（与「构造失败也不翻转 ready」的现有立场一致）。

#### 场景:`AnthropicAPIBackend` 实现 BackendDiagnostics

- **当** 构造 `backend = AnthropicAPIBackend(...)` 后调 `isinstance(backend, BackendDiagnostics)`
- **那么** 必须返回 True

#### 场景:`PlaybackBackend` 不实现 BackendDiagnostics

- **当** 构造 `backend = PlaybackBackend(...)` 后调 `isinstance(backend, BackendDiagnostics)`
- **那么** 必须返回 False（cassette 模式无真实健康概念）

#### 场景:`ensure_safe_for_daemon` 默认 no-op

- **当** `backend = AnthropicAPIBackend(...)` 且 `is_daemon_mode(settings) == True`，调 `backend.ensure_safe_for_daemon()`
- **那么** 必须正常返回 None（API key 在 daemon 模式安全）

#### 场景:`isinstance` 不抛 TypeError

- **当** 调 `isinstance(some_backend, BackendDiagnostics)`
- **那么** 必须正常返回 bool（**禁止** raise `TypeError`）；此场景保证 `@runtime_checkable` 装饰器正确加载

#### 场景:doctor 用配置的超时包裹 health_check

- **当** `settings.agent.health_check_timeout_seconds = 10.0` 且一个实现 `BackendDiagnostics` 的桩 backend 的 `health_check()` 耗时约 7s 后返回 `is_healthy=True`，执行 `hostlens doctor`
- **那么** doctor 必须等满至该 backend 返回（不在 7s 处中断），backend 健康行 `health_check_is_healthy` 为 True（**禁止**误报 timeout）

#### 场景:health_check 超时是信息性、不翻转 exit code

- **当** 桩 backend 的 `health_check()` 耗时超过 `settings.agent.health_check_timeout_seconds`（如配 5.0s、ping 耗时 8s），其余 doctor 检查全 ok，执行 `hostlens doctor`
- **那么** backend 健康行 `health_check_is_healthy=False` 且错误文案含 `timeout after 5.0s`；但 doctor 整体**必须** exit 0（backend 健康不参与 `ready`）

#### 场景:`settings.agent is None` 时回落默认超时

- **当** 配置无 `agent:` 节（`settings.agent is None`）但有可探测的 `backend:`，执行 `hostlens doctor`
- **那么** doctor 必须用回落默认 10.0s 包裹 `health_check()`，**禁止** raise `AttributeError`
