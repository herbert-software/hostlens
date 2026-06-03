## 修改需求

### 需求:`hostlens demo run <scenario>` 必须离线回放一个打包场景并渲染报告

`hostlens demo run <scenario>` 命令必须对一个打包的 incident 场景跑 **完整 Planner → Diagnostician 全链管线**（`ReplayTarget` + `PlaybackBackend`），组装一份忠实的一等 `Report`（复用 `agent-report-assembly` 的 `InspectorResultCollector` 两时点装配 + id 一致性 + status 合并），并把渲染后的报告输出到 stdout。渲染必须走 intent 风格 `Report` 渲染器（narrative + `## Findings` + `## 根因假设` + 一行遥测），**禁止**退回 Planner-only 的 `PlannerResult` 渲染。该命令禁止发起任何真实 Anthropic API 调用、禁止建立任何 SSH / 远程连接、禁止要求 Anthropic API key 或 target 凭据。Planner 与 Diagnostician 两段必须由**同一个** `PlaybackBackend` 实例服务（两段请求 key 不同、从同一份 cassette 按 key 匹配，不靠顺序）。该命令必须支持 `md`（默认）与 `json` 两种渲染格式（`-f` / `--format`，json 输出可 `Report.model_validate_json` 往返），并支持 `-o` / `--output FILE` 把报告写入文件而非 stdout。

#### 场景:对已知场景跑通离线回放并产出根因假设
- **当** 用户在已安装 Hostlens 的干净机器（无 `~/.config/hostlens/targets.yaml`、无 `ANTHROPIC_API_KEY`）上运行 `hostlens demo run cpu_saturation`
- **那么** 命令必须经 Planner→Diagnostician 回放管线产出该场景对应的 markdown `Report` 到 stdout，报告必须含与故障对应 severity 的 finding、narrative，以及 `## 根因假设` 章节下至少一条带证据链接的根因假设（注：该假设由 cassette authored 录死，本断言为 **liveness 守护**——抓"诊断段录成空关联/0 假设"，**不**评估诊断质量；见 design D-3.5）

#### 场景:不触达 API 的结构性保证（可断言，覆盖两段）
- **当** demo 装配完成时检查其 LLM backend，以及在缺失 `ANTHROPIC_API_KEY` 下运行
- **那么** Planner 与 Diagnostician 两段使用的 backend 必须都是同一个 `PlaybackBackend` 实例，且 demo 路径**绝不调用 `create_backend` 工厂**（可 monkeypatch `create_backend` 抛异常断言未被触达——比"实例 is PlaybackBackend"更强，防"先建真 backend 再丢弃"虚假满足），缺 key 不影响运行（仍正常出带根因假设的报告）——以此结构性事实断言"不触达 Anthropic API"，而非仅文档声明

#### 场景:json 与 md 输出同源 Report
- **当** 用户对同一场景分别以 `-f md` 与 `-f json` 运行
- **那么** json 必须输出忠实 `Report`（含 `meta` / `findings` / `hypotheses` / `metadata` 的 diagnosis_narrative），md 必须是该 `Report` 的 intent 风格渲染；两者来源同一组装结果

#### 场景:`--output` 写文件
- **当** 用户运行 `hostlens demo run cpu_saturation -o report.md`
- **那么** 渲染后的报告必须写入 `report.md`，stdout 不再输出报告正文

#### 场景:`--output` 写到不可写路径
- **当** 用户运行 `hostlens demo run cpu_saturation -o /不可写路径/report.md`
- **那么** 命令必须向 stderr 输出单行写失败错误并以退出码 3 结束，stdout 不输出报告正文，禁止输出 Python traceback

#### 场景:json 与 md 退出码一致
- **当** 用户对同一场景分别以 `-f md` 与 `-f json` 运行
- **那么** 两次运行的退出码必须一致（退出码由 `Report.meta.status` 与 finding severity 决定，与渲染格式无关）

### 需求:`demo run` 必须复用 inspect 的 4 值退出码契约

`hostlens demo run` 必须遵循 4 值退出码契约（0/1/2 复用 `_compute_intent_report_exit_code`，对组装出的 `Report` 映射；exit 3 为 demo 自写的 caller 边界）：0 表示健康（`meta.status` 为 ok 且无 critical finding）、1 表示 ok 且存在至少一个 critical severity finding、2 表示**任何降级 `meta.status`**（`degraded_*` / `empty_response` / `partial`——`partial` 是相对旧 `PlannerResult` 退出码的新增行为，由 `_derive_report_status` 在 inspector 非 ok 时推出）或 collector 真空 no-result 或装配/运行期失败，3 表示用法 / 配置错误。

为可靠区分 exit 3（资产缺失 / 未知场景）与 exit 2（装配/运行期失败），命令必须在装配**之前**执行一次 **pre-flight 资产解析检查**（确认场景归一化后在 registry 中、且资产经 `importlib.resources.files(...).joinpath(name).is_file()` 存在——必须用此 `Traversable` API，禁止 `os.path.exists`/`Path.exists`，后者对 zip-safe wheel 资源误判 False）；pre-flight 失败一律 exit 3。退出码按异常阶段映射：未知场景 / `importlib.resources` 资源缺失（pre-flight）→ 3；cassette JSON 格式坏 / fixture schema 坏（装配期 `ValueError` / `ConfigError`）→ 2；Planner **或 Diagnostician** 段行为漂移导致运行期 `CassetteMiss` / `ReplayMiss`-degraded → 2；`--output` 写失败 → 3。命令在任何分支均禁止向用户输出 Python traceback；意外异常必须包装为单行 `internal: <kind>: <msg>` 写入 stderr。

#### 场景:critical finding 退出码 1
- **当** 用户运行的场景回放后 `Report` 含至少一个 critical severity finding 且 `meta.status` 为 ok
- **那么** 命令必须以退出码 1 结束，报告仍完整输出到 stdout

#### 场景:partial 状态映射退出码 2（由退出码映射单测覆盖，非 8 套打包场景 e2e）
- **当** 一个 `Report.meta.status` 为 `partial` 的 Report 输入 `_compute_intent_report_exit_code`
- **那么** 必须返回退出码 2（区别于 critical 的 exit 1）；报告仍可完整渲染输出
- **测试方式说明（必须如实标注）**：现有 8 套打包场景的 inspector 全部回放为 `ok`（fixture 命令 `exit_code` 均为 0），`_derive_report_status` 只在某 inspector 非 ok（`target_unreachable`/`exception`/`requires_unmet` 或全 timeout）时才推 `partial`，故 **8 套打包 demo 的 e2e 路径永不产 partial**（注意 `dependency_unreachable` 场景的 "unreachable" 是应用级 finding，**不是** inspector 级 `target_unreachable` 状态，其 inspector 仍回放 ok）。本变更不改 fixture、**不**要求新增畸形场景；`partial→2` 行为由对 `_compute_intent_report_exit_code` 的**单元测试**（构造 `Report(meta.status=partial)`）保证，并复用 `--intent` 既有的 partial 退出码回归。本 spec 不得把 partial 列为某个打包 demo 场景的 e2e 可复现行为。

#### 场景:运行期 cassette miss 退出码 2
- **当** Planner 或 Diagnostician 段行为与录制漂移，运行期 `messages_create` 找不到匹配 record 而抛 `CassetteMiss`（注：这是运行期失败，不同于"资产被破坏"——后者是装配期 `ValueError`）
- **那么** 命令必须把异常包装为单行 `internal: <kind>: <msg>` 写入 stderr 并以退出码 2 结束，禁止输出 Python traceback

#### 场景:装配期资产损坏退出码 2
- **当** 某场景 cassette 的 JSON 格式被破坏，`PlaybackBackend` 构造期抛 `ValueError`
- **那么** 命令必须以退出码 2 结束（区别于资产**缺失**的 exit 3），单行 stderr，禁止输出 Python traceback

## 新增需求

### 需求:`hostlens demo run --persist` 必须把组装出的忠实 Report 落盘

`hostlens demo run` 必须新增 `--persist` flag（默认 **关**）。仅当显式传 `--persist` 且本次产出了 `Report`（非 no-result）时，命令必须把该 `Report` 落盘到标准 `ReportStore`（与 `hostlens reports show / diff` 读取的同一个 store），使 demo 产出可被 `reports` 子命令离线消费，复现 M3.1 持久化闭环。no-result（collector 真空）时必须显式跳过落盘（不静默假成功）。落盘失败必须把退出码升到 2（复用 `--intent` 的 orphan/persist-fail 升级语义），分两分支：`ReportStore.save` **抛异常** → 单行 `internal:`；主库不可写但**降级 orphan** → 单行 `warning:`（非 `internal:`），报告仍渲染。

demo 的「完全自包含、不读取用户配置」约束针对的是**读**（targets.yaml / API key / 用户 `HOSTLENS_*` 环境）；`--persist` 写 `Report` 到 store 是用户显式动作、不违反读自包含。落盘的 demo `Report` 必须可与真实 run 区分：其 `target_name` 必须带 demo 来源标记 `demo:<scenario>`。区分**仅**靠 `target_name` 的 `demo:` 前缀（`target_type` 取 ReplayTarget 的 impersonate 值 `local`/`ssh`，不参与区分）。

**与现有 `reports` CLI 契约的相容性（不扩范围）**：本变更**不改** `reports` CLI / `RunIndexRow` schema（见 Non-Goals）。现有 `reports list <target>` 是**按 target 过滤**的 per-target 列表、其行不渲染 `target_name`；现有 `reports show <run_id>` 按**全局 run_id** 解析（无 target 参数）。故 demo 标记的「可区分」语义落在现有契约内：(1) `reports show <run_id>` 取回的 `Report.meta.target_name` 带 `demo:` 前缀可见；(2) `demo:<scenario>` 作为 per-target 查询键可 `reports list demo:<scenario>` 定向列出该 demo 的 run。**不要求** demo 标记出现在 `reports list <某真实 target>` 的行输出里（那需要改 `RunIndexRow`/`_format_row`，属 Non-Goal 之外）。默认不传 `--persist` 时，命令行为与落盘前一致（只渲染、不触碰 store）。

#### 场景:`--persist` 落盘并可经 reports 取回
- **当** 用户运行 `hostlens demo run cpu_saturation --persist`，随后运行 `hostlens reports show <run_id>`（`reports show` 按全局 run_id 解析、无需 target 参数，故 `demo:` 前缀的 target_name 不阻碍取回）
- **那么** demo 组装的 `Report`（含 hypotheses）必须已入库且能被 `reports show <run_id>` 取回；取回的 `Report.meta.target_name` 必须为 `demo:cpu_saturation`，并可作为 `reports list demo:cpu_saturation` 的查询键定向列出该 demo run（不要求出现在其他真实 target 的 `reports list` 行里）

#### 场景:不传 --persist 不落盘
- **当** 用户运行 `hostlens demo run cpu_saturation`（无 `--persist`）
- **那么** 命令只渲染报告到 stdout，禁止写入 `ReportStore`（`reports list` 不因此次运行新增条目）

#### 场景:两次 --persist run 可经 reports diff 离线比对（同场景两次 → 空 delta）
- **当** 用户对同一场景两次 `hostlens demo run <scenario> --persist`（两次终态 status=ok），随后 `hostlens reports diff <a> <b>`
- **那么** `reports diff` 必须成功跑通并输出 finding 级比对结果——**因 demo 确定性回放，同场景两次 run 的 findings（含 id）完全相同，故 finding 级 delta 为空（无 added/resolved/changed）**；本场景证明的是「diff 管线可离线消费 demo 产出」，**不是**「demo 能造出有变化的 diff」（要 added/resolved 需两个不同 baseline，超出 demo 确定性回放能力，亦非本需求目标）。hypothesis 级对比是后续提案，不在本需求内

#### 场景:落盘失败升退出码 2（raise 与 orphan 两分支均升 2）
- **当** `--persist` 下 `ReportStore.save` 抛异常（如 store 路径不可写）
- **那么** 命令必须向 stderr 输出单行 `internal:` 错误并以退出码 2 结束，禁止输出 Python traceback

#### 场景:落盘降级 orphan 也升退出码 2
- **当** `--persist` 下主 store 不可写但 `_persist_report` 降级写出 orphan 文件（返回 `True`，不抛异常）
- **那么** 命令必须按 `--intent` 既有语义（caller 的 `(orphaned or persist_failed) and exit_code in (0,1) → 2`）把退出码升到 2，stderr 输出单行 `warning:`（orphan 路径，**非** `internal:`），报告仍渲染输出
