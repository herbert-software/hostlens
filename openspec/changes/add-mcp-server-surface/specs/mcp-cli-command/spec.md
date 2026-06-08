## 新增需求

### 需求:`hostlens mcp serve` 必须以 stdio 启动 MCP Server 且对缺失依赖优雅退出

CLI 必须新增 `mcp` 子命令组，含 `hostlens mcp serve`，以 stdio transport 前台启动 MCP Server（装配真实 `ToolRegistry`（`register_default_tools`）+ `ToolContext` 工厂）。`mcp` 为 optional-dependency：当官方 `mcp` SDK 未安装时，`hostlens mcp serve` 必须捕获 `ImportError`，向 stderr 打印清晰提示（含 `pip install "hostlens[mcp]"`）并以**退出码 1** 退出，**禁止**抛裸 traceback、**禁止**以退出码 0 静默成功。

#### 场景:mcp SDK 已安装时 serve 启动 stdio server

- **当** 官方 mcp SDK 已安装，运行 `hostlens mcp serve`
- **那么** 进程以 stdio transport 启动 MCP Server（前台），可被 MCP host 拉起并响应 list_tools / call_tool

#### 场景:mcp SDK 未安装时 serve 退出码 1 且提示安装

- **当** 官方 mcp SDK 未安装，运行 `hostlens mcp serve`
- **那么** 进程以退出码 1 退出，stderr 含安装提示 `pip install "hostlens[mcp]"`
- **且** **不**打印裸 Python traceback、**不**以退出码 0 退出

### 需求:`hostlens doctor` 必须报告 mcp SDK 可用性

`hostlens doctor`（`--json` 与人类渲染两种输出）必须新增 `checks.mcp` 项，反映官方 `mcp` SDK 是否可 import。status 取值必须复用既有 `CheckResult` 枚举：可 import → `status="ok"`；不可 import → `status="missing"`（与现有 doctor check 的 status 语义一致，保证 `--json` schema 稳定、Agent 解析逻辑统一）。**人类渲染**（非 `--json`）同样展示 `checks.mcp` 行，`status=="missing"` 时附 `pip install "hostlens[mcp]"` 提示。该检查为**非致命**：`checks.mcp` **禁止**加入 doctor readiness 聚合白名单（`_is_ready`），故 mcp SDK 缺失时 `checks.mcp.status=="missing"` 但 doctor 整体判定**不**失败。

#### 场景:doctor --json 含 checks.mcp 状态且非致命

- **当** mcp SDK 已安装，运行 `hostlens doctor --json`
- **那么** 输出 JSON 必须含 `checks.mcp`，`status == "ok"`
- **当** mcp SDK 未安装，运行 `hostlens doctor --json`
- **那么** 输出 JSON 必须含 `checks.mcp`，`status == "missing"`
- **且** doctor 整体 readiness 判定**不**因 `checks.mcp.status=="missing"` 而失败（`checks.mcp` 不在 `_is_ready` 白名单内）
