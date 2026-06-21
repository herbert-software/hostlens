## 1. CLI `--from-plan` 落地半（先做：闭环本地端 + 给 MCP 工具的 round-trip 验证锚）

- [x] 1.1 `targets/import_plan.py`：给 `ImportPlan` 加 `version: Literal["1"] = "1"` 字段（对齐 `TargetsConfig.version`，因 plan 现为 propose→land 持久化跨进程契约；缺 version 的旧 `.save` 产物经默认值加载为 v1，version 为非 "1" 值 → 报错）+ 加 `ImportPlan.load(path)`（`yaml.safe_load` → `model_validate`，因 JSON⊂YAML 故同容 YAML 与 JSON）。**信任边界校验**：`.load`（或 bucket validator）必须对**每个落盘向 entry**（`to_add` 恒含、`failed_probe` 当 `--include-unreachable` 含；`skipped`/`invalid_candidate` 不经 `assemble_save_entries` 投影、有意豁免）重申 promotion 不变量——`password`/`passphrase is None`、`*_env`（非 None）匹配**裸 env 名** `^[A-Z_][A-Z0-9_]*$`（**不是** `${VAR}` 形）、`host`/`user`/`key_path` 无控制/双向字符（**仅 `SSHEntry`**，`LocalEntry` 无此字段；复用导出的 `contains_unsafe_display_chars` 而**非** `_strip_control_chars`）、`key_path`（SSHEntry）无 `${` 占位（不在 `_PLACEHOLDER_ALLOWED_FIELDS`、落盘逐字写、`${VAR}` 毒化后续 expand-load 成持久 DoS；mirror `resolve_key_path`）、`to_add` 项 `enabled is True`（`failed_probe` 强制 `enabled=False` 故不要求）；任一违反 → `.load` 把 `pydantic.ValidationError`/`yaml.YAMLError`/`OSError` 统一裹成 `ConfigError`（项目 exit-2 载体），`--from-plan` 分支 `except (ConfigError, ValidationError)` 映射 exit 2、绝不裸 traceback。（`password is None` 是 DiD——`_entry_to_dict` 只从 `*_env` 取 password、不读 `entry.password`，明文非可达向量；真实向量是 disabled target / 畸形 `*_env` / 控制字符 host / `key_path: ${VAR}`）
- [x] 1.2 `cli/target.py` `import_cmd`：inventory 位置参数改为 `Argument(None)`（可选）+ 加 `--from-plan <path>` 选项；**`--concurrency` 默认改 sentinel `None`**（ref 模式再派生 10），否则无法区分显式传 `--concurrency 10` 与默认、exit-2 规则不可实现；裸 str 手动校验**恰好** inventory 与 `--from-plan` 二选一（皆缺/皆给 → `typer.Exit(2)`，不走 Click `UsageError`→exit 3）；`--source`（默认已 None）/`--concurrency`（sentinel None）非 None 时与 `--from-plan` 同传 → `typer.Exit(2)`（不静默忽略）；`--from-plan --dry-run` 合法（预览，**不**互斥）；`--json`/`--include-unreachable` 仍生效；既有 `--dry-run`+`--yes` 互斥不变
- [x] 1.3 `--from-plan` **独立写分支**（在到达 ref 模式共享尾 `target.py:~624-649` candidates_failed 块**之前**决定退出码与渲染，不穿过）：`ImportPlan.load` → `--dry-run` 或无 `--yes` 走 `render_diff` 预览 exit 0；`--yes` 走 `assemble_save_entries` + `save_targets_config`（复用拒 root / `${VAR}` 保全 / `--include-unreachable`）**且不显 diff**（文件来源信任）；`to_add` 为空时 exit 0（不复用 candidates-failed 启发式、不因 plan 自带 `failed_probe` 而 exit 1）
- [x] 1.4 `--from-plan` 文件错误（不可读/非法/schema 或 version 不符）映射为 exit 2 的结构化报错（无裸 traceback、不写盘）

## 2. MCP propose 工具（消费既有 `ImportPlan` round-trip;闭环验证 4.3 依赖 §1 的 `.load`）

- [x] 2.1 新增 `tools/schemas/propose_target_import.py`：`ProposeTargetImportInput`（`ref` + `source: Literal["ssh_config","yaml"] | None` + `concurrency: conint(ge=1, le=100) | None` 可选，上界 **100 对齐 `TargetProbe` 内部 clamp**（并发已被限到 ≤100，schema `le=100` 把静默 clamp 变诚实契约——**不**写「镜像 MAX_STATUS_LIMIT」，那是 handler 内 `min()` 静默 clamp、机制不同）；**output_schema 直接用 `ImportPlan`**（不套 wrapper——adapter 仅对 output 做 `isinstance(result, ImportPlan)` + `model_dump()`，不投影成 inputSchema）
- [x] 2.2 新增 `tools/import_propose_tool.py`：`ImportProposeToolDeps`（`Settings` / source registry / 既有 target 名 **fresh-read 回调**——每次调用 `load_targets_config(settings.targets_config_path, expand_env=False)` 取 name 集合，**非** `target_registry.names()`，后者读 serve 启动期冻结的 registry 仍漏运行期本地新加的 target）闭包注入 + `@tool` spec factory（`surfaces={"agent","mcp"}`、`side_effects="read"`、`sensitive_output=True`、`requires_approval=False`、`output_schema=ImportPlan`、`mcp_description ≠ agent_description`）+ handler 调 `build_import_plan` 返回 `ImportPlan`，绝不调 `save_targets_config`
- [x] 2.3 错误隔离（**两路 fail-closed，分清**）：① 非法 `source`/越界 `concurrency` 在 adapter input 校验步 `raise TypeError`、早于 handler，由 `server.py` `TypeError`→error-result 作 MCP `isError` 返回；② handler 期失败（坏 `ref` 的 `ConfigError`/`OSError`）走 dispatch 通用 `except` 信封。空 inventory → 空 `ImportPlan`；MCP 宿主机缺凭据 env 时 cred-ful 候选诚实归 `failed_probe`（非崩溃）
- [x] 2.4 在 `cli/mcp.py:serve_cmd` 装配处（`register_mcp_management_tools(registry, deps=...)` 调用点旁，约 L231）注册 `register_import_propose_tool(registry, deps=...)`；deps 经新增 `_build_import_propose_deps(settings, target_registry)` 助手构造（镜像 `_build_management_deps`，含 fresh-read 既有 target 名 callable）——**不**在 `mcp_server/server.py`（它只接收已装配 registry）

## 3. 接线与可观测

- [x] 3.1 经 `build_server` 的 eager `list_for_mcp` 投影自检确认 `propose_target_import` 通过四道 fail-closed 门且在工具集中（**不**改 doctor——`checks.mcp` 仅检 `mcp` SDK 可导入，无工具名清单机制）

## 4. 测试（VCR/真 fixture，不 mock）

- [x] 4.1 工具单测：注册即声明只读策略元数据（side_effects=read / sensitive_output=True / requires_approval=False / surfaces⊇mcp / output_schema is ImportPlan）；`mcp_description ≠ agent_description`
- [x] 4.2 dispatch 测：产非空 `to_add` 且 `targets.yaml` dispatch 前后逐字节不变（不落盘断言）；非法 source / 越界 concurrency（`<1` 与 `>100`）→ 经 input 校验作 MCP `isError`（非 dispatch 信封）；坏 ref → dispatch 信封；空 inventory → 空 plan
- [x] 4.3 round-trip 测：工具产出 `model_dump()` dict → `model_validate` 逐字段等价；序列化为 YAML 与 JSON 两文件 → `target import --from-plan` 加载均逐字段等价；缺 version 旧 plan 加载为 v1
- [x] 4.4 cred 视角测：MCP 宿主机缺 `password_env` 时 cred-ful 候选归 `failed_probe`（非崩溃、非误判 reachable）；既有 target 名 fresh-read（serve 后本地新加 target、不重建 registry，下次 propose 仍把它归 `skipped` 而非 `to_add`——证明读的是 config 非冻结 registry）
- [x] 4.5 CLI `--from-plan` 测：`--yes` 落盘（to_add enabled=True / `${VAR}` 保全）、`--dry-run` 与无 `--yes` 预览 exit 0、inventory 与 `--from-plan` 皆给/皆缺 exit 2、`--from-plan`+显式 `--source`/`--concurrency` exit 2、文件非法/version 非 "1" exit 2、**畸形 plan（`to_add` enabled=False / `*_env` 非裸 env 名 / `host` 含控制字符 / `failed_probe` 同类畸形在 `--include-unreachable` 下）经 `.load` 不变量校验 exit 2 且不写盘**、EUID==0 exit 1、`--include-unreachable` 登记 failed_probe(enabled=False)、`to_add` 为空 exit 0
- [x] 4.6 测试隔离仓根 dev `.env`（chdir tmp + delenv `HOSTLENS_BACKEND__*` + 跑全量目录非子集，避免 CI 红）

## 5. 文档与归档

- [x] 5.1 `docs/` 补 propose→land 闭环用法（MCP propose 产 `ImportPlan` → client 序列化为 YAML/JSON → 本地 `target import --from-plan --yes`）+ `sensitive_output` 是披露标签非脱敏 + cred-ful 主机建议本地 `target import <ref>`（propose 探测视角=MCP 宿主机视角）+ `--from-plan --yes` 文件来源信任说明
- [x] 5.2 `docs/roadmap/target-onboarding.md` §5 标记提案 B 落地；CLAUDE.md 下一步候选更新
- [ ] 5.3 实现完成跑对抗性 review（§5.3）→ APPROVE 后开 PR → CI 绿 squash merge → 归档 change
