# replay-execution-target 规范增量

## MODIFIED Requirements

### 需求:ReplayTarget 实现 ExecutionTarget 协议

The system SHALL 提供 `ReplayTarget`，一个实现完整 `ExecutionTarget` Protocol（`exec` / `read_file` / `capabilities`）的 shippable target（位于 `src/hostlens/targets/`），按预录 fixture 返回确定性结果，使 Inspector 无需真实主机即可走完整 `target → collect → parse → findings` 路径。

#### 场景:exec 命中返回预录结果

- **当** 对 `ReplayTarget` 调 `exec(cmd, timeout=..., env=...)` 且 `cmd` 命中 fixture
- **那么** 返回 fixture 中该命令预录的 `ExecResult`（stdout / stderr / exit_code / duration_seconds）

#### 场景:capabilities 由 fixture 声明

- **当** Inspector preflight 读取 `ReplayTarget.capabilities`
- **那么** 返回值等于 fixture 顶层 `capabilities` 字段投影出的 `set[Capability]`，使 `requires_capabilities`（如 `systemd`）的场景按声明通过或 skip

#### 场景:运行时 type 冒充既有 target 类型

- **当** 读取 `ReplayTarget.type`
- **那么** 返回 fixture 顶层 `impersonate` 声明的既有类型（`"local"` / `"ssh"` / `"docker"`，默认 `"local"`），使 runner preflight 的 `target.type in manifest.targets`（`Literal["local","ssh","docker"]`）透明通过，从而对 docker 派发路径做离线回放；`ExecutionTarget.type` 与 `InspectorManifest.targets` 的 Literal 枚举两侧均已含 `docker`，无需在本侧额外改动枚举即可冒充

#### 场景:impersonate 取值域限定为既有 target 类型

- **当** fixture 顶层 `impersonate` 声明为 `kubernetes` / `k8s` 或其他不在 `Literal["local","ssh","docker"]` 内的值
- **那么** 加载 fixture 时必须 raise（Pydantic 校验失败）——`impersonate` 只能冒充已实现的 target 类型，禁止冒充未实现的类型造成 preflight 假性通过
