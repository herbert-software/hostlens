## 上下文

`add-cli-target-import`（#102 归档）已落地只读编排 `build_import_plan`（`targets/onboard.py:118`）与写半 `assemble_save_entries`（`onboard.py:215`）+ `save_targets_config`（`targets/config.py:698`）、四分类 `ImportPlan`（`targets/import_plan.py`，pure-Pydantic `model_dump_json`/`model_validate_json` 可 round-trip；`.save()` 经 `_atomic_write_yaml` 写 **YAML** 0600；**无 `.load()`**）。MCP 侧 `McpToolsAdapter`（`mcp_server/tools_adapter.py`）：`list_for_mcp` 投影只检 `sensitive_output is not None`；`dispatch` 检四道门——surface（`"mcp" in surfaces`）/ `sensitive_output is not None` / `side_effects ∉ {write,destructive}` / `requires_approval is not True`，全过后 `isinstance(result, spec.output_schema)` 校验并 `result.model_dump()` 序列化进 `TextContent`（**成功路径零 redaction**）。只读工具（`side_effects ∈ {none,read}` + 显式 `sensitive_output`）直接放行。MCP 工具注册在 `cli/mcp.py:serve_cmd`（`register_default_tools` + `register_mcp_management_tools(registry, deps=...)` 调用点，约 L206/L231），**不在** `mcp_server/server.py`（`build_server` 只接收已装配 registry）。`mcp-management-tools`（7 个只读工具）确立「`@tool` 纯 spec factory + `ManagementToolDeps` 闭包注入 + `_build_management_deps` 助手 + 显式 `register_*`」模式。

本变更是 Target Onboarding roadmap §5 的提案 B（次/可选），依赖 A。

## 目标 / 非目标

**目标：**
- MCP `propose_target_import` 工具：投影 `build_import_plan` 产 `ImportPlan`，propose-only 不落盘，`side_effects="read"` + `sensitive_output=True`，零 dispatch gate 改动。
- CLI `target import --from-plan <path>`：消费序列化 `ImportPlan` 直接落盘，复用 `--yes`/拒 root/原子幂等写门，使 propose→land 闭环成立。

**非目标：**
- MCP 永不写 `targets.yaml`（不做 `--from-plan` 的 MCP 等价物 / 两段式 token）。
- `remove_target` / `test_channel` / `notify_report` 不进本提案。
- 不新增 inventory source（自然语言来源留远期）。
- 不改 `build_import_plan` / `save_targets_config` 的对外契约（只新增调用方 + 给 `ImportPlan` 加 `version`/`load`）。

## 决策

**D1：MCP 工具输出 = 完整 round-trip `ImportPlan`（直接作 `output_schema`，不套 wrapper），不是脱敏摘要。**
`--from-plan` 要逐字落地探活结果（避免本地重跑探测漂移），必须拿到含主机地址的完整 plan。`output_schema=ImportPlan` 直接用——adapter 对 output schema **只**做 `isinstance(result, ImportPlan)` 校验（`tools_adapter.py:180`）+ `model_dump()` 序列化（`:187`），**不**把 output schema 投影成 MCP/agent 的 `inputSchema`（只有 `input_schema` 经 `model_json_schema()` 投影，`:76`），故 `LocalEntry|SSHEntry` 判别联合的 `anyOf` 投影问题对 output 是非问题。handler 直接返回 `ImportPlan`。
- 备选（弃）：套 `ProposeTargetImportOutput(plan: ImportPlan)` wrapper。否决——多一层无收益，且 handler 返回类型与 `output_schema` 不一致会在 `isinstance` 门运行期炸。
- 备选（弃）：只返 `to_json_obj()` 摘要。否决——`to_json_obj` 是**非 round-trip 的审计 JSON**（仍含 `to_add.host`，但丢 `failed_probe` fingerprint/细节、不可 `model_validate` 回原型），`--include-unreachable` 重建不了 `failed_probe`，闭环断裂。

**D2：propose→land 产物交接 + `ImportPlan.load` 兼容双格式。**
MCP `dispatch` 返回的是 `model_dump()` **dict**（非文件）。交接定义为：MCP client / 用户把该结构化结果序列化成文件（任意 YAML 或 JSON），`--from-plan` 经新增 `ImportPlan.load(path)` 加载。`.load` 用 `yaml.safe_load` → `model_validate`——因 **JSON 是 YAML 的子集**，单一 `yaml.safe_load` 同时容 `.save` 写出的 YAML 与 client 写出的 JSON，消除 `.json`/YAML 格式分裂。加载失败（文件不存在 / 非法 / 不符 schema / `version` 不符）→ CLI 映射 exit 2；缺 `version` 键（A 旧 `.save` 产物）经 `version: Literal["1"] = "1"` 默认值加载为 v1（显式向后兼容，spec 须声明）。

**`.load` 是信任边界，必须重申 promotion 不变量（完整契约见 `target-import` spec）。** `--from-plan` 跳过 A 的 promotion 直接 `model_validate` 外部文件，绕过 `promote_candidate` 强制、而 `_entry_to_dict` 落盘时不复核的保证。`.load` 必须对**每个落盘向 entry**（`to_add` 恒含、`failed_probe` 当 `--include-unreachable` 含；`skipped`/`invalid_candidate` 不经 `assemble_save_entries` 投影、永不落盘故有意豁免）校验：① `password`/`passphrase is None`；② `*_env`（非 None）匹配**裸 env 名** `^[A-Z_][A-Z0-9_]*$`（**不是** `${VAR}` 形——`${...}` 仅由 `_entry_to_dict` 落盘合成；`PendingAdd.password_env` 本身是裸名如 `MY_PASS`，对其做 `${VAR}` 校验会拒掉 propose 自己产的合法 plan）；③ `host`/`user`/`key_path` 无控制/双向字符——**仅 `SSHEntry`**（`LocalEntry` 无此字段），复用导出的 `contains_unsafe_display_chars`（**非** display-only 的 `_strip_control_chars`，前者类别集多 `Cs` 代理）；④ `to_add` 项 `enabled is True`（`failed_probe` 经 `assemble_save_entries` 强制 `enabled=False`，不要求 True）；⑤ `key_path`（SSHEntry）无 `${` 占位（不在 `_PLACEHOLDER_ALLOWED_FIELDS`、落盘逐字写，`${VAR}` 会毒化后续 `load_targets_config(expand_env=True)` → 持久 DoS；mirror source 层 `resolve_key_path`）。违反 → exit 2（`.load` 把 `ValidationError`/`yaml.YAMLError`/`OSError` 统一裹成项目 exit-2 载体 `ConfigError`，`--from-plan` 分支 `except (ConfigError, ValidationError)` 映射、绝不裸 traceback）。**真实可达向量**：`enabled=False` disabled target（`_entry_to_dict:603` honor）、畸形 `*_env`（盲包成 `${非法}`）、控制字符 host（落盘未净化）、`key_path: ${VAR}`（落盘逐字、后续 expand-load 毒化为持久 DoS）；`entry.password` 明文**非**可达向量（`_entry_to_dict:619-622` 只从 `*_env` 取 password、不读 `entry.password`），故 `password is None` 检查仅契约完整性/DiD。这是 ponytail never-simplify 集里的「信任边界输入校验」，不可省。
- 备选（弃）：强制只认 JSON 或只认 YAML。否决——`.save` 已是 YAML、client `model_dump_json` 是 JSON，双来源天然存在，`yaml.safe_load` 一行兼容最省。

**D3：`--from-plan` 走「load → assemble → save」三步，inventory 位置参数变可选。**
`import_cmd` 把 inventory 位置参数从必填改为 `Argument(None)`，加校验：**恰好** `inventory` 与 `--from-plan` 二选一（皆缺/皆给 → exit 2，与既有 `--source` 一样走裸 str 手动校验 + `typer.Exit(2)`，**不**走 Click `UsageError`→exit 3，对齐 `target-import` spec 的 exit-2-not-3 纪律）。`--source` / `--concurrency` 是纯 parse/probe 期参数，`--from-plan` 既跳过 parse 又跳过 probe，故同传 = 静默无效 → 也 exit 2（对齐严格度，不留沉默 no-op）；`--include-unreachable`（控制 plan 的 `failed_probe` 是否落盘 enabled=False）与 `--json`（渲染加载 plan）仍生效。`--from-plan` 沿用 `--yes`/`--dry-run`/拒 root 门：`--dry-run` 或无 `--yes` → 预览加载 plan exit 0；`--yes` → 落盘；`--dry-run`+`--yes` 仍 exit 2。

**`--from-plan` 走独立写分支，不复用 ref 模式的共享尾。** 既有 `import_cmd` 写尾（`target.py:~624-649`）含 candidates-failed 启发式（`plan.failed_probe and not include_unreachable` 或 `invalid_candidate` → exit 1）+ 无条件 `render_diff`（`:~608-610`）。`--from-plan` 的退出码语义不同（`to_add` 为空 → exit 0，**不**因 plan 自带 `failed_probe` 而 exit 1，因它不重跑探测）、且 `--yes` happy path 不显 diff（文件来源信任）。故 `--from-plan` 必须在到达共享尾**之前**用专属分支决定 exit 0/1 与是否渲染，**不**穿过 `:637` 的 candidates_failed 块。落盘仍 `assemble_save_entries` + `save_targets_config`，幂等 upsert 自动处理 name 冲突。
- 备选（弃）：让用户重跑 `target import <ref> --yes`。否决——重新探测可能与 propose 时结果不一致，违背「确定性落地」。

**D4：工具依赖经 `_build_import_propose_deps` 闭包注入，注册在 `cli/mcp.py:serve_cmd`。**
新增 `tools/import_propose_tool.py`（`ImportProposeToolDeps` + `@tool` factory + `register_import_propose_tool`）。deps 需 `Settings`（喂 `build_import_plan` + `TargetProbe`）、source registry（`default_source_registry()`）、既有 target 名读取（归 `skipped`，只读不改 `side_effects`）。在 `cli/mcp.py:serve_cmd` 的 `register_mcp_management_tools` 调用点旁，加 `_build_import_propose_deps(settings, target_registry)` 助手（镜像 `_build_management_deps`）+ `register_import_propose_tool(registry, deps=...)`。对齐 §4.10 / ADR-008（不扩 `ToolContext`）。

「既有 target 名读取」**必须**每次 propose 调用 fresh-read `load_targets_config(settings.targets_config_path, expand_env=False)` 并取其 name 集合，**不是** `target_registry.names()`——后者读的是 serve 启动期一次性构建的 `target_registry`（`mcp.py:202`，`names()` 只返内存 keys），serve 运行期本地 CLI 新加的 target 仍漏。land 期 `save_targets_config` 的 fresh-load 幂等 upsert 是第二道兜底（重复 name 落盘时跳过），但 propose 阶段就 fresh-read config 让 plan 预览更准。

**D5：`side_effects="read"` 是定性决策。**
handler 只读既有 target 名 + 跑探测（探测是对远端的只读 exec），不写本地任何配置。故 `read` 而非 `write`，天然不碰 adapter 的 write 拒绝门——「零 dispatch gate 改动」由此成立。

**D6：MCP `source`/`concurrency` 输入受约束（含上界），非法输入与 handler 失败走不同 fail-closed 路径。**
input schema 把 `source` 约束为 `Literal["ssh_config","yaml"] | None`、`concurrency` 约束为 `conint(ge=1, le=100)`。上界取 **100** 对齐 `TargetProbe` 本就有的内部 clamp（`probe.py` 把 concurrency clamp 到 100）——即并发 SSH 扇出**已**被 TargetProbe 限到 ≤100（RC R2 担心的「无界扇出」实际不可达）；schema 显式 `le=100` 的价值是**诚实契约**（超限 reject 而非 TargetProbe 的静默 clamp，对齐「不留沉默 no-op」）。**不**写「镜像 MAX_STATUS_LIMIT」——那是 handler 内 `min()` 静默 clamp（值 100）、机制不同。

CLI 侧 `import_cmd` 的 `--concurrency` 当前是 `Option(10)` 有默认值，无法区分「显式传 10」与默认；为支持「`--from-plan` + `--concurrency` → exit 2」须把默认改为 sentinel `None`（ref 模式再派生 10）。

**两条 fail-closed 路径要分清（spec/test 不能笼统说「dispatch 信封」）**：① input-schema 违反（非法 `source`/越界 `concurrency`）在 `McpToolsAdapter.dispatch` 的 **input 校验步**（`model_validate` 前置）即 `raise TypeError`，**早于** handler，**不**走 dispatch 的通用 `except` 信封——它向上由 server 的 `handle_call_tool` `except (..., TypeError)` → `_error_result` 捕获，作 MCP `isError` 文本结果返回（仍 fail-closed + 脱敏）。② handler 期失败（坏 `ref` 的 `ConfigError`/`OSError`）才走 dispatch 通用 `except` 信封。两者都不裸传、不静默成功，但机制不同。（实现时按符号定位，勿信本文行号——接线后行号会移。）

**D7：`ImportPlan` 加 `version: Literal["1"] = "1"`。**
plan 从 A 的「同进程 dry-run 产物」升级为 B 的「propose（可能远程/异机）→ land（本地，可能跨 hostlens 升级）」持久化跨进程契约，需版本字段防跨版本 opaque 失败（对齐 `TargetsConfig.version`）。一行字段，`.load` 校验 version。

## 风险 / 权衡

- **主机地址外泄给远程 LLM** → `sensitive_output=True` 是**披露标签非脱敏机制**（adapter 成功路径零 redaction，`to_add.host` verbatim 进 `TextContent`）；信任边界是用户的 MCP client。落盘仍须本地 `--yes`，MCP 不碰写门。
- **探测凭据视角 = MCP 宿主机视角**（最隐蔽，3/3 reviewer 命中）→ `build_import_plan` 从**运行进程的 `os.environ`**（`onboard.py:87-96`）解析 `password_env`/`passphrase_env`。MCP server 进程若缺这些 env，cred-ful 候选探活失败被误归 `failed_probe`，远程 propose 出的 plan 让可达主机看似不可达，用户落地一个降级 plan（静默错误非崩溃）。首批 tizi 为 cred-less Tailscale SSH 故不触发。缓解：design + spec 显式声明「propose 探测视角 = `hostlens mcp serve` 进程视角」，文档建议 cred-ful 纳管在能解析凭据的本地用 `target import <ref>` 而非 MCP propose；handler 诚实分类不假装成功。
- **propose 与 land 之间 inventory 漂移** → `--from-plan` 落地的是 plan 快照里的探活结果；若两次之间机器状态变了，落地的 `enabled` 可能过时。缓解：plan 短时效，文档建议 propose 后尽快 land；`save_targets_config` 幂等可纠偏。
- **plan 文件篡改 / `--from-plan --yes` 不显示 diff** → `--from-plan --yes` happy path **不渲染 `render_diff`**（预览只在无 `--yes`/`--dry-run` 分支，镜像既有 `import_cmd`）；且 `_strip_control_chars` 只净化**预览显示串**、不在 `.load`→`save` 数据路径，落盘的是未净化的 `entry.host`。故 `--from-plan` 跳过 promotion，篡改文件不经 promotion 的控制字符拒绝。定性为**文件来源信任**：`--from-plan --yes` 信任文件作者身份（如 `target add --yes` 信任命令行参数），运维若需审计先 `--from-plan --dry-run` 预览（预览串经控制字符剥离、不可伪造）；`.save` 0600 限制本地写。
- **跨版本 plan handoff** → D7 的 `version` 字段防 opaque 失败。
- **探测放大** → 远程一个 propose 调用触发对整批候选的并发 SSH 探测。并发度**已**被 `TargetProbe` 内部 clamp 到 ≤100（`probe.py`），故远程传 `concurrency: 100000` 也打不出 >100 并发——「无界扇出」实际不可达。input schema 的 `le=100`（D6）只是把这层 clamp 变成诚实契约（超限 reject 而非静默 clamp）。inventory 候选总数由 source 文件大小自然受限（非攻击者任意放大）。

## Open Questions

- `inventory.yml` 富元数据（role/provider/region/runtime=podman）是否驱动 inspector 选择——独立 spec 决策，不在本提案。
- propose 后是否需要一个「server 端把 plan 写到用户可取的临时位置」便利层——当前定为 client 自序列化（最省），有需求再起。
