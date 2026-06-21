## 为什么

`add-cli-target-import`（#102，已归档）落地了「来源 → 探测 → 规划 → 事务写」四层 CLI 批量纳管管线，但 M7-ext 读期把写工具 `import_targets` 显式踢给了写期提案，至今 MCP surface 上无法触发纳管。Target Onboarding roadmap（§5）已裁决：`import_targets` 上 MCP **最多 propose-only**——产 `ImportPlan` 不落盘，对齐 M9「AI 担不了责的写只给方案不代执行」红线（一个 token 批量加 N 台能 SSH 进去的机器，没有低风险版本）。这是纳管故事的最后一块：让远程 LLM（经 MCP）能 propose 纳管计划，落盘仍由用户在本地显式 `--yes` 完成。

## 变更内容

- **新增 MCP 工具 `propose_target_import`**：复用 A 的只读编排 `build_import_plan`，输入 inventory `ref`（+ 可选 `source` 限定为 `{ssh_config, yaml}` / `concurrency` 限定 `1..100`，对齐 `TargetProbe` 本就有的内部 clamp，把静默 clamp 变诚实契约），`output_schema=ImportPlan`（直接用 `ImportPlan`，不套 wrapper——adapter 仅对 output schema 做 `isinstance(result, ImportPlan)` 校验 + `model_dump()` 序列化，**不**把 output schema 投影成 MCP/agent inputSchema，故无 `anyOf`/discriminator 投影问题），handler 返回 `ImportPlan`。`side_effects="read"` + `sensitive_output=True` + `surfaces={"agent","mcp"}`——**永不写 `targets.yaml`**，**零 MCP dispatch gate 改动**（adapter 对 `write/destructive` fail-closed，`read` 直接放行）。
- **propose→land 产物交接**：MCP dispatch 返回 `ImportPlan` 的 `model_dump()` 结构化结果（dict，非文件）。用户/MCP client 把它序列化成文件（YAML 或 JSON 皆可），交给下面的 `--from-plan` 落地。
- **新增 CLI 落盘入口 `hostlens target import --from-plan <path>`**：经 `ImportPlan.load`（`yaml.safe_load`，因 JSON⊂YAML 故同时容 YAML 与 JSON）加载一个序列化 `ImportPlan`（MCP 产出或 dry-run `.save` 产物），**跳过 source/probe**，直接 `assemble_save_entries` → `save_targets_config`，复用现有 `--yes` / 拒 root / 原子幂等写门。这是 propose→land 闭环的本地落地半。因引入 `--from-plan`，inventory 位置参数从必填变为可选：**恰好** `inventory` 与 `--from-plan` 二选一（皆缺 / 皆给 → exit 2）。`--from-plan --dry-run`（或无 `--yes`）= 预览加载的 plan 不写盘 exit 0（与既有 dry-run 语义一致）。
- **非目标**：MCP 永不写 `targets.yaml`（无 `--from-plan` 的 MCP 等价物）；不建两段式 approval token；`remove_target` 不进本提案；`test_channel` / `notify_report` 各自独立小提案；不新增自然语言来源（留远期）。

## 功能 (Capabilities)

### 新增功能
- `mcp-target-import-propose`: MCP `propose_target_import` 只读工具——投影 `build_import_plan` 产 `ImportPlan` 结构化输出，propose-only（不落盘）、`side_effects="read"`、显式 `sensitive_output=True`、经 `McpToolsAdapter` 的 fail-closed 门（`list_for_mcp` 投影检 `sensitive_output`；`dispatch` 检 surface / sensitive_output / side_effects / requires_approval 四道）。

### 修改功能
- `target-import`: 新增 `--from-plan <path>` 模式——从序列化 `ImportPlan` 直接落盘（跳过 source/probe），与 `ref` 位置参数**恰好二选一**（皆缺/皆给 exit 2）；`--from-plan --dry-run` 合法（预览，不互斥）；`--source` / `--concurrency`（纯 probe/parse 期参数；`--concurrency` 默认须改 sentinel `None` 方能检出显式传值）与 `--from-plan` 同传 exit 2；`--include-unreachable` / `--json` 仍生效；沿用 `--yes` / 拒 root 语义。`ImportPlan.load` 作为信任边界**必须**对每个落盘向 entry（`to_add` 恒含、`failed_probe` 当 `--include-unreachable` 含）重申 promotion 不变量（`password`/`passphrase` 为 None、`*_env` 匹配**裸 env 名** `^[A-Z_][A-Z0-9_]*$`、`host`/`user`/`key_path` 无控制字符、`to_add` 项 `enabled=True`），违反 → exit 2，防篡改/畸形 plan 把 disabled target / 不可展开 `${非法}` 占位 / 控制字符 host 写进 `targets.yaml`。

## 影响

- **代码**：新增 `src/hostlens/tools/import_propose_tool.py`（ToolSpec + handler + deps）+ 在 `cli/mcp.py` 的 `serve_cmd` 装配处注册（`register_mcp_management_tools` 调用点旁，**不是** `mcp_server/server.py`——后者只接收已装配好的 registry）；`src/hostlens/cli/target.py` 的 `import_cmd` 把 inventory 位置参数改为可选 + 加 `--from-plan` 分支；**必须**新增 `ImportPlan.load(path)`（对称于既有 `.save`）+ 给 `ImportPlan` 加 `version: Literal["1"]` 字段（对齐 `TargetsConfig` 既有版本字段，因 plan 现在是 propose→land 的持久化跨进程契约）。
- **MCP**：MCP 工具集 +1（`propose_target_import`），经 `build_server` 的 eager `list_for_mcp` 投影自检暴露（**doctor `checks.mcp` 仅检 `mcp` SDK 可导入，无工具名清单机制**——不增删 doctor）。
- **依赖**：无新增依赖（asyncssh / pydantic / mcp / pyyaml 均已在用）。
- **安全**：MCP 输出含 `to_add` 主机地址（横向移动图）→ `sensitive_output=True` 是**披露标签**（声明该工具输出敏感、供 MCP client / 审计判断），**非脱敏机制**（adapter 成功路径零 redaction，verbatim 序列化进 `TextContent`）；真实信任边界是用户的 MCP client。落盘门保持本地 `--yes`，MCP 不碰写门。
