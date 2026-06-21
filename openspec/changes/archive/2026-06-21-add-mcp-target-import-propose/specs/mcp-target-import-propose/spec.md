## 新增需求

### 需求:`propose_target_import` 必须注册为只读 ToolSpec、propose-only 不落盘、经 fail-closed 投影

MCP 工具 `propose_target_import` **必须**声明为 `ToolSpec` 且策略元数据为 `surfaces={"agent","mcp"}`、`side_effects="read"`、`sensitive_output=True`、`requires_approval=False`。handler **必须**复用 `target-import` capability 的只读编排 `build_import_plan`（source → promote → probe → classify），返回四分类 `ImportPlan`；**禁止**调用 `save_targets_config`、**禁止**写 `targets.yaml`、**禁止**触发任何落盘——纳管的落盘半永远只在本地 CLI `--yes` 完成（对齐 roadmap §5：MCP 最多 propose-only，对齐 M9「给方案不代执行」红线）。

工具必须能经 `McpToolsAdapter` 的 fail-closed 门：`list_for_mcp` 投影检 `sensitive_output is not None`；`dispatch` 检四道门——surface（`"mcp" in surfaces`）/ `sensitive_output is not None` / `side_effects ∉ {write,destructive}` / `requires_approval is not True`。因 `surfaces ∋ "mcp"` 过 surface 门、`side_effects="read"` 过 write/destructive 门，本工具**不触碰** adapter 对 write/destructive 的拒绝路径——**零 dispatch gate 改动**即可暴露。`mcp_description` 与 `agent_description` **禁止**共用同一字符串（人因诉求不同：远程 LLM vs 本地 LLM）。

#### 场景:注册即显式声明只读策略元数据
- **当** 装配 MCP server 后枚举其工具
- **那么** `propose_target_import` 在工具集中，且其 `ToolSpec` 的 `side_effects=="read"`、`sensitive_output is True`、`requires_approval is False`、`surfaces ⊇ {"mcp"}`

#### 场景:经现有 fail-closed 投影自检通过
- **当** 以 `McpToolsAdapter.list_for_mcp` 投影该工具、并对其 `dispatch`
- **那么** `list_for_mcp` 的 `sensitive_output` 门与 `dispatch` 的四道门（surface / sensitive_output / side_effects / requires_approval）全部放行，无 `ToolPolicyViolation`

#### 场景:dispatch 产 plan 但绝不落盘
- **当** dispatch `propose_target_import` 指向一个解析出可达候选的 inventory `ref`
- **那么** 返回的 `ImportPlan` 的 `to_add` 非空，且 `targets.yaml` 在 dispatch 前后逐字节不变（handler 全程不调 `save_targets_config`）

#### 场景:mcp_description 与 agent_description 不共用
- **当** 读取该 `ToolSpec` 的 `mcp_description` 与 `agent_description`
- **那么** 二者为不同字符串

### 需求:handler 依赖必须经注册期闭包注入，禁止扩 ToolContext

`propose_target_import` 的 handler 依赖（`Settings` / inventory source registry / 既有 target 名集合的读取路径）**必须**经注册期闭包注入（与 `mcp-management-tools` 的 `ManagementToolDeps` 同构，经 `cli/mcp.py:serve_cmd` 的 `_build_import_propose_deps` 助手装配），**禁止**扩 `ToolContext`（冻结于 ADR-008 六字段集）、**禁止**从 module-level singleton 取。读取既有 target 名集合用于把已存在的候选归入 `skipped` 桶——该读取是只读的，不改变工具的 `side_effects="read"` 定性。

#### 场景:ToolContext 字段集不变
- **当** 引入本工具后检查 `ToolContext` 的字段
- **那么** 字段集与 ADR-008 六字段集逐字段一致，未为本工具新增任何字段

#### 场景:handler 依赖来自注入而非全局
- **当** 审查 handler 实现
- **那么** 其依赖来自注册期闭包（注入），无 `from ... import` module-level singleton 直取

### 需求:输出必须是直接作 output_schema 的 round-trip `ImportPlan`，供 CLI `--from-plan` 落地

工具 `output_schema` **必须**直接为 `ImportPlan`（不套 wrapper），handler 返回 `ImportPlan` 实例；`dispatch` 经 `isinstance(result, ImportPlan)` 校验后 `model_dump()` 序列化返回。该 dict 是可 round-trip 形态（`model_validate` 可还原），用户/MCP client 将其序列化成文件（YAML 或 JSON）后逐字交给 `hostlens target import --from-plan` 在本地落地（propose→land 闭环）。因输出携带 `to_add` 的连接地址（横向移动图），故 `sensitive_output` **必须**显式声明为 `True`——这是**披露标签**（声明输出敏感、供 MCP client/审计判断），**非脱敏机制**（adapter 成功路径零 redaction，`to_add.host` verbatim 进 `TextContent`，真实信任边界是用户的 MCP client）。

#### 场景:输出 dict 可 model_validate round-trip
- **当** 取本工具 `dispatch` 返回的 `model_dump()` dict，喂给 `ImportPlan.model_validate`
- **那么** 还原出的 `ImportPlan` 与 handler 产出逐字段等价（round-trip 无损）

#### 场景:输出经序列化文件可被 --from-plan 加载
- **当** 把工具产出的 `ImportPlan` 以 YAML 或 JSON 任一格式写入文件，交给 `target import --from-plan` 加载
- **那么** 两种格式都加载成功且与产出逐字段等价（`ImportPlan.load` 经 `yaml.safe_load` 同容 YAML 与 JSON）

### 需求:空 inventory 产空 plan，非法输入与解析失败 fail-closed（机制分两路），凭据视角为 MCP 宿主机视角

空 inventory 必须产空 `ImportPlan`（四桶皆空）而非报错。所有失败都 fail-closed（脱敏、不裸传、不静默成功），但**机制分两路，spec/test 不可笼统说「dispatch 信封」**：

- **input-schema 违反**（非法 `source` 超出 `{ssh_config,yaml}`、`concurrency` 越界）在 adapter 的 input 校验步即 `raise TypeError`、**早于** handler，由 `server.py` 的 `TypeError`→error-result 捕获，作 MCP `isError` 文本结果返回（**不**走 dispatch 的通用 `except` 信封）。
- **handler 期失败**（坏 `ref` 的 `ConfigError`/`OSError`）走 `dispatch` 通用 `except` 包装为结构化错误信封。

input schema **必须**把 `source` 约束为 `Literal["ssh_config","yaml"] | None`、`concurrency` 约束为 `conint(ge=1, le=100)`——上界 100 对齐 `TargetProbe` 本就有的内部 clamp（并发 SSH 扇出已被限到 ≤100，非「无界」），schema `le=100` 把静默 clamp 变诚实契约（超限 reject）。

探测的凭据从**运行进程（`hostlens mcp serve`）的 `os.environ`** 解析（`build_import_plan` → `_resolve_probe_entry`）。当 MCP 宿主机缺少候选所需的 `password_env`/`passphrase_env` 时，cred-ful 候选探活失败归 `failed_probe`（非崩溃），plan 诚实反映「MCP 宿主机探测视角」；**禁止**假装可达。`mcp_description` / 文档须指明：cred-ful 主机的纳管建议在能解析凭据的本地用 `target import <ref>`，而非远程 MCP propose（首批 cred-less Tailscale SSH 不受影响）。

#### 场景:空 inventory 产空 plan 不报错
- **当** dispatch 指向一个解析出零候选的 inventory
- **那么** 返回 `ImportPlan` 四桶皆空（`is_empty` 为真），非错误信封

#### 场景:坏 ref 的 handler 失败走 dispatch 信封
- **当** dispatch 指向语法非法/无法解析的 inventory `ref`
- **那么** 经 `dispatch` 通用 `except` 包装为结构化错误信封（脱敏），不裸传异常、不触发探测、不写盘

#### 场景:非法 source / 越界 concurrency 经 input 校验 fail-closed
- **当** 传入超出 `{ssh_config,yaml}` 的 `source`，或 `<1` / `>100` 的 `concurrency`
- **那么** adapter input 校验步 `raise TypeError`、早于 handler，由 server 捕获作 MCP `isError` 结果返回（脱敏），不触发探测、不写盘

#### 场景:MCP 宿主机缺凭据 env 时 cred-ful 候选诚实归 failed_probe
- **当** dispatch 指向含 cred-ful 候选的 inventory，但其 `password_env` 未在 `hostlens mcp serve` 进程的 `os.environ` 中
- **那么** 该候选探活失败归 `failed_probe`（非崩溃、非误判 reachable），plan 诚实反映 MCP 宿主机探测视角
