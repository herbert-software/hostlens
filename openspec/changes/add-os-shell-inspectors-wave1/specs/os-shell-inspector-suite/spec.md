## 新增需求

### 需求:wave-1 必须按域覆盖指定的 OS/Linux 故障域

本套件**必须**在现有 builtin 基线之上、按 `TODO.md` §M6 覆盖矩阵新增覆盖以下 OS/Linux 故障域的纯 shell inspector：计算 CPU、内存、磁盘/FS、网络、进程、服务管理器与调度器、内核/系统、日志。每个域**必须**至少新增矩阵为该域列出的探针。**遵守 spike D-9**：本需求约束的是**套件层的域覆盖度**，**不**为任一具体 inspector 规定 input/output 行为契约——具体 inspector 清单（名称与采集手法）是**实现**，列在本变更的 `proposal.md` 与 `tasks.md`，由 snapshot 测试验收。

中间件 / 服务域（nginx / mysql / postgres / redis / docker / k8s）**禁止**纳入本套件（留 wave-2）；本套件**仅**含零外部服务依赖的 OS/Linux shell 探针。

#### 场景:清单中的 inspector 全部干净注册

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** `proposal.md`/`tasks.md` 列出的每个 wave-1 inspector（共 23 个，以本变更**归档时冻结**的清单为准；后续 wave 另立 change，不回溯改本 spec）**必须**以其声明 `name` 出现在 registry 中，且 registry `errors == []`

#### 场景:每个目标域都有新增探针且勾上矩阵

- **当** 评估 wave-1 是否达成域覆盖
- **那么** 上述 8 个故障域**必须**各自至少新增一个 inspector，且每个落地的 inspector **必须**勾上 `TODO.md` §M6 覆盖矩阵对应单元格；**禁止**以「域内已有一个探针」为由跳过矩阵为该域列出的新增项

#### 场景:中间件与服务域不在本套件范围

- **当** 评估某 inspector 是否属于 wave-1 套件
- **那么** 依赖外部服务（nginx / mysql / postgres / redis / docker / k8s）的 inspector **禁止**纳入本套件（留 wave-2）

### 需求:套件内每个 inspector 必须是遵守作者契约的纯 YAML

本套件内每个 inspector **必须**为纯 YAML manifest，并**遵守** `inspector-authoring-contract` 的全部规则（一切抽取与数值派生在 collector 内、finding 规则只做标量阈值/成员比较、`for_each` 单绑定、输出键防 parameter 遮蔽、命令注入安全三件套、运行前提文档式声明）。本需求**引用**该契约而非重述其细则，以免两份 spec 漂移。**禁止** enable `hook.py`、**禁止**新增 `sql_result` parse format、**禁止**在 finding 表达式里做解析或数值派生。

#### 场景:数值派生与跨行关联在 collector 内

- **当** 某 inspector 需要派生量（如磁盘 IO 利用率、内存 swap 使用率、僵尸进程计数）或需关联多行/多命令输出
- **那么** 该派生/关联**必须**由 collector 命令（shell 算术 / `jq` / 自行 read→sleep→read 双读算差）算出并写入输出 JSON，finding 规则只对已就绪标量做阈值比较；**禁止**在 finding 表达式内现算

#### 场景:输出键命名遵循契约的防遮蔽约定

- **当** 某 inspector 产出结果
- **那么** 若为列表型（配 `for_each`），其可迭代结果集的顶层键**必须**取自 `results` / `items` / `records` 之一；若为聚合型（无 `for_each`），其顶层标量键沿用裸命名（与既有 `system.uptime` / `linux.memory.pressure` 一致）但**必须**不与任一已声明 parameter 同名——两种形态都**禁止**输出键与 parameter 同名（finding 上下文中同名 parameter 会遮蔽 output 键）

#### 场景:参数安全进 shell

- **当** 某 inspector 把调用方参数（如关键进程名列表、日志路径、DNS 待查名）插入 `collect.command`
- **那么** 该参数**必须**经 `| sh`（或数组 `| map('sh')`）引用、且 `parameters` JSON Schema **必须**用 `pattern` 收紧取值域；**禁止**裸 `{{ param }}` 拼进可执行位置

### 需求:套件内每个 inspector 必须附 ReplayTarget fixture 与可证检出的 snapshot 测试

本套件内每个 inspector **必须**附带用 fixture 录制器（`inspector-fixture-recorder`）对真实 Linux host 录制的 `ReplayTarget` 兼容 fixture，以及 snapshot 测试，使其能离线确定性回放出 `InspectorResult`。**禁止**手写 fixture。CI **必须**全程经 `ReplayTarget` 回放，**禁止**在日常 CI 中依赖网络 / 真实主机 / 真实数据源。

为防止 no-op inspector 满足验收，每个 inspector **必须**至少附**一份触发预期 finding 的异常场景 fixture**，其 snapshot **必须**断言该场景产出预期的 finding（severity + message 语义），证明 inspector 真能**检出**目标故障——仅有「干净注册 + happy-path 无 finding」的 snapshot **不满足**验收。

#### 场景:异常场景 snapshot 证明检出能力

- **当** 对某套件 inspector 运行其 snapshot 测试
- **那么** 测试集**必须**含至少一份异常场景 fixture，其 snapshot 断言 inspector 在该场景下产出预期 severity 与 message 语义的 finding；**禁止**只有 happy-path（无 finding）snapshot 就判该 inspector 验收通过

#### 场景:离线回放确定性出结果

- **当** 在任意平台（含 macOS / CI）对某套件 inspector 运行其 snapshot 测试
- **那么** 它**必须**经 `ReplayTarget` 回放录制的 fixture、不触达任何真实主机或网络，并产出与快照一致的确定性 `InspectorResult`

#### 场景:缺少所需二进制时优雅 skip 而非崩溃

- **当** 目标主机缺少某 inspector `requires_binaries` 声明的二进制（如无 `smartctl` / 无 `chronyc`）
- **那么** runner preflight **必须**将该 inspector 标为 `status=requires_unmet` 并 skip、报告中标注，**禁止**报错中断同 run 其它 inspector

### 需求:本套件禁止引入新基础设施

本套件**必须**在现有 schema 字段集内完成，证明纯铺量无需新 infra：**禁止**改动 inspector manifest schema（不增删字段）、**禁止**新增 parse format（仅 raw/table/json/kv）、**禁止**扩 capability enum（现为 `{shell, file_read, ssh, systemd, docker_cli}`）、**禁止**新增 `min_binary_version` 等 schema 字段（窄 scope 版本门仍走文档式声明）、**禁止**新增 Python 运行时依赖。允许使用**现有** schema 字段（含已落地的 `collect.sampling_window`）。

#### 场景:零对外契约变更

- **当** 套件实现完成
- **那么** inspector manifest schema、Agent 可见工具数组（仍只有 `list_inspectors` / `run_inspector`）、parse format 集合、capability enum **必须**全部保持不变；**禁止**因本套件而改动任何对外契约

#### 场景:Linux-only 与版本门用文档式声明

- **当** 某 inspector 依赖 Linux 专有数据源（`/proc`、`/sys`、GNU `date -d`、`journalctl`）或特定工具版本
- **那么** 该前提**必须**在 `description` 与 `tags`（tag 正则 `^[a-z][a-z0-9_-]*$`，禁含 `+`）中文档式声明；**禁止**新增 manifest 字段做机器式版本门（会被 schema `extra="forbid"` 拒）
