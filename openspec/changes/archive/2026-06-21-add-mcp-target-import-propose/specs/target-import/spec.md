## 新增需求

### 需求:`hostlens target import --from-plan <path>` 必须从序列化 `ImportPlan` 直接落盘、跳过 source/probe、inventory 与 --from-plan 恰好二选一、复用 --yes/拒 root 门

`hostlens target import` **必须**支持 `--from-plan <path>` 模式：经 `ImportPlan.load(path)`（`yaml.safe_load` → `model_validate`，因 JSON⊂YAML 故同时容 YAML 与 JSON）加载一个序列化的 `ImportPlan`（`mcp-target-import-propose` 工具产出经 client 序列化的文件，或 dry-run 持久化产物 `ImportPlan.save` 写出的 YAML），**跳过 source 解析与 probe 探测**，直接 `assemble_save_entries` → `save_targets_config` 落盘。该模式存在的意义是：让远程经 MCP propose 的计划在本地**逐字、确定性**落地（不重跑探测以免 probe 结果漂移）。

为支持 `--from-plan`，inventory 位置参数从必填变为可选（`Argument(None)`），并加约束：**恰好** inventory（`ref`）与 `--from-plan` 二选一——皆缺 / 皆给均为参数错误 → exit 2（与既有 `--source` 一样走裸 str 手动校验 + `typer.Exit(2)`，**不**走 Click `UsageError`→exit 3，对齐既有 exit-2-not-3 纪律）。`--source` / `--concurrency` 是纯 parse/probe 期参数，`--from-plan` 既跳过 parse 又跳过 probe，故二者与 `--from-plan` 同传 = 静默无效 → 也 exit 2（不留沉默 no-op）；`--json`（渲染加载 plan 的 JSON）、`--include-unreachable`、`--yes`/`--dry-run` 仍生效。

`--from-plan` 沿用既有写门语义：
- `--dry-run`，或缺 `--yes`（且非 `--dry-run`）→ 渲染加载 plan 的 `render_diff` 预览、**不写盘**、exit 0（`--from-plan --dry-run` 是合法预览，与既有 dry-run 语义一致——**不**与 `--from-plan` 互斥）。
- `--yes` → 落盘。
- `--dry-run` 与 `--yes` 同传仍 exit 2（复用既有互斥）。
- 落盘前 **EUID==0 → exit 1**（拒 root，复用既有守卫）。
- `path` 不可读 / 内容非法 / 不符 `ImportPlan` schema（`version` 不符 → exit 2；**缺** `version` 键的 A 旧 `.save` 产物经 `version` 字段默认值加载为 v1，显式向后兼容）→ 否则参数错误 exit 2（结构化报错，**禁止**裸 traceback、**禁止**静默成功）。
- `--include-unreachable` 对 `--from-plan` **同样生效**：加载 plan 里 `failed_probe` 桶在该标志下以 `enabled=False` 登记（与 ref 模式逐字段同形）；不带该标志时 `failed_probe` 不落盘。
- 落盘仍经 `save_targets_config` 的原子 / 幂等 upsert / `${VAR}` 保全（`PendingAdd.password_env` / `passphrase_env` 透传），name 已存在者幂等跳过。

**`ImportPlan.load` 是信任边界，落盘前必须重申 promotion 不变量**：`--from-plan` 跳过 A 的 source→`promote_candidate`，直接 `model_validate` 外部文件，绕过 promotion 强制、而 `_entry_to_dict` 落盘时不再复核的保证。故 `.load`（或 bucket validator）**必须**对**每个会进入 `save_targets_config` 的 entry**（`to_add` 恒含；`failed_probe` 当 `--include-unreachable` 生效时含——它也经 `assemble_save_entries` 落盘）重申：
- `password` / `passphrase` 恒为 None（凭据只经 `*_env` 引用）；
- `password_env` / `passphrase_env`（非 None 时）匹配**裸 env 名** pattern `^[A-Z_][A-Z0-9_]*$`（`CandidateTarget` 用的同一 pattern；**不是** `${VAR}` 形——`${...}` 仅由 `_entry_to_dict` 落盘时合成）；
- `host` / `user` / `key_path` 不含控制 / 双向覆盖字符——**仅对 `SSHEntry`**（`LocalEntry` 无此三字段，条件应用、mirror `promote_candidate` 的 SSH 分支），复用导出的 `contains_unsafe_display_chars`（类别集 `{Cc,Cf,Cs,Zl,Zp}`，比 display-only 的 `_strip_control_chars` 多 `Cs` 代理）、**非** `_strip_control_chars`；
- `key_path`（`SSHEntry`）不含 `${` 占位——`key_path` **不在** `_PLACEHOLDER_ALLOWED_FIELDS`（仅 `password`/`passphrase`），落盘逐字写，含 `${VAR}` 会毒化后续每次 `load_targets_config(expand_env=True)`（`env_placeholder_not_allowed_here`）→ 持久化共享配置 DoS；source 层 `resolve_key_path` 拒此，`--from-plan` 跳过 promotion 故在此重申；
- `to_add` 项 `enabled is True`（`failed_probe` 经 `assemble_save_entries` 强制 `enabled=False`，故**不**对其要求 True）。

任一违反 → 加载失败 exit 2，不落盘。`skipped`（`list[str]`）/ `invalid_candidate` 桶**不**经 `assemble_save_entries` 投影、永不落盘，故**有意豁免**该校验（对其校验属过度防御，且会误拒合法 `invalid_candidate` plan）。**真实可达向量（非臆想）**：篡改/手改/跨版本畸形 plan 经 `_entry_to_dict` 可写出 ① `enabled=False` 的 disabled target（`_entry_to_dict` honor `enabled is False`）；② 畸形 `*_env`（`_entry_to_dict` 盲目包成 `${非法}` 占位，落盘不可展开 / 注入）；③ 控制字符 `host`/`user`（落盘未净化，spoof 后续审计）。注：`entry.password` 明文**不是**可达向量——`_entry_to_dict` 只从 `*_env` 参数取 password、从不读 `entry.password`；故 `password is None` 检查是契约完整性 / defense-in-depth，不堵明文泄露。这是 ponytail never-simplify 集里的「信任边界输入校验」，不可省。

**`--from-plan` 走独立写分支**：既有 ref 模式写尾含 candidates-failed 启发式（`failed_probe and not include_unreachable` 或 `invalid_candidate` → exit 1）+ 无条件 `render_diff`。`--from-plan` 退出码语义不同（`to_add` 空 → exit 0，不因 plan 自带 `failed_probe` 而 exit 1）、`--yes` happy path 不显 diff，故**必须**在到达共享尾之前用专属分支决定 exit 码与渲染，不穿过 candidates_failed 块。

**文件来源信任**：`--from-plan --yes` 的 happy path **不渲染** `render_diff`（预览只在 `--dry-run`/无 `--yes` 分支）。结合上面的 `.load` 不变量校验（拦明文密钥/disabled/坏占位），`--from-plan --yes` 定性为**信任文件作者身份**（如 `target add --yes` 信任命令行参数）；运维若需审计未预期主机应先 `--from-plan --dry-run` 预览（预览串经 `_strip_control_chars` 剥离、不可伪造）。

#### 场景:--from-plan + --yes 落盘
- **当** `target import --from-plan plan.yaml --yes`，`plan.yaml` 含探活成功的 `to_add` 候选
- **那么** 跳过 source/probe，经 `save_targets_config` 写出这些条目（`enabled=True`、凭据 `${VAR}` 占位保全），退出码与既有 import 落盘契约一致

#### 场景:--from-plan 缺 --yes 或 --dry-run 预览不写盘 exit 0
- **当** `target import --from-plan plan.yaml`（无 `--yes`、无 `--dry-run`），或 `target import --from-plan plan.yaml --dry-run`
- **那么** 渲染加载 plan 的 `render_diff` 预览、不写 `targets.yaml`、exit 0

#### 场景:加载兼容 YAML 与 JSON 两种格式
- **当** `--from-plan` 分别指向一个 YAML 文件（`.save` 产物）与一个 JSON 文件（client `model_dump_json` 产物），内容等价
- **那么** 两者经 `ImportPlan.load` 均加载成功且还原为逐字段等价的 `ImportPlan`

#### 场景:inventory 与 --from-plan 皆给或皆缺 exit 2
- **当** `target import some-inventory.yml --from-plan plan.yaml`（皆给），或 `target import`（皆缺）
- **那么** 作为参数错误 exit 2，不解析任何来源、不写盘

#### 场景:--from-plan 文件不可读或非法 exit 2
- **当** `--from-plan` 指向不存在 / 非法 / 不符 `ImportPlan` schema（`version` 为非 `"1"` 值）的文件
- **那么** 结构化报错 exit 2，不裸传 traceback、不写盘

#### 场景:缺 version 的旧 plan 加载为 v1
- **当** `--from-plan` 指向一个 A 旧 `.save` 写出的、不含 `version` 键的 plan 文件
- **那么** 经 `version` 字段默认值加载为 v1、正常处理（向后兼容，非报错）

#### 场景:--from-plan 与 --source/--concurrency 同传 exit 2
- **当** `target import --from-plan plan.yaml --source yaml`，或 `... --concurrency 50`
- **那么** 作为参数错误 exit 2（纯 probe/parse 期参数在 `--from-plan` 下无意义，不静默忽略），不写盘
- **注** `--concurrency` 默认值须改为 sentinel `None`（ref 模式再派生 10），否则无法区分「显式传 `--concurrency 10`」与默认值，该 exit-2 规则不可实现

#### 场景:--from-plan 加载畸形 plan（disabled/坏 env 名/控制字符 host/key_path ${占位}/内联明文凭据）被拒 exit 2
- **当** `--from-plan` 指向一个落盘向 entry（`to_add`，或 `--include-unreachable` 下的 `failed_probe`）含 `enabled=False`（to_add）、或 `password_env` 非裸 env 名 `^[A-Z_][A-Z0-9_]*$`、或 `host`/`user`/`key_path` 含控制 / 双向覆盖字符、或 `key_path` 含 `${` 占位、或内联明文 `password`/`passphrase` 的 plan 文件
- **那么** `ImportPlan.load` 重申 promotion 不变量后加载失败 exit 2，**不**把 disabled `to_add` / 不可展开 `${非法}` 占位 / 控制字符 host / `${VAR}` key_path / 明文凭据写进 `targets.yaml`

#### 场景:--from-plan EUID==0 落盘前 exit 1
- **当** 以 EUID==0 运行 `target import --from-plan plan.yaml --yes`
- **那么** 落盘前 exit 1（拒 root），不写 `targets.yaml`

#### 场景:--from-plan + --include-unreachable 登记 failed_probe
- **当** `target import --from-plan plan.yaml --yes --include-unreachable`，plan 含 `failed_probe` 候选
- **那么** 这些候选以 `enabled=False` 登记，与 ref 模式 `--include-unreachable` 输出逐字段同形

#### 场景:--from-plan 的 to_add 为空时落盘语义明确
- **当** `target import --from-plan plan.yaml --yes`，plan 的 `to_add` 为空（如全 `skipped`，或全 `failed_probe` 且未带 `--include-unreachable`）
- **那么** 无条目写出、`targets.yaml` 不变、exit 0（「无可落盘项」不是失败；区别于 ref 模式「探活/提升全失败」的 exit 1——`--from-plan` 不重跑探测，不复用 candidates-failed 启发式）
