## 新增需求

### 需求:service-inspector-suite 采用追加式冻结 cohort 结构

`service-inspector-suite` **必须**采用「稳定公共质量门 + 追加式冻结 cohort」结构,作为整个 M6 wave-2 service inspector 套件的覆盖契约与质量门(类比 `os-shell-inspector-suite`)。套件 spec **必须**只持有**稳定规则语义**(公共质量门 + 每 wave 一条冻结覆盖需求),**禁止**在 spec 层持有会随 wave 增长的「最终总清单」。每个 wave(2a / 2b / 2c …)在套件内拥有**独立、归档时冻结**的覆盖需求;**遵守 spike D-9**——各 wave 的具体 inspector 清单(名称与采集手法)是**实现**,列在该 wave 自己 change 的 `proposal.md` / `tasks.md`,由 snapshot 验收,冻结于该 change 归档时,**不**写进本 spec 的某条「总清单」。

后续 wave **必须**以 `新增需求`(ADDED)向套件追加**自己的** sibling 覆盖需求,**禁止** `MODIFY` 任一已归档 wave 的冻结覆盖需求,**禁止**扩写或解释已归档 wave 的清单——以此规避对已归档 spec 的回溯修改。每条 wave 覆盖需求的标题**必须** wave-prefixed 且全套件唯一(如「wave-2a 必须覆盖…」/「wave-2b 必须覆盖…」),使后续 wave 的 ADDED 需求标题不与任一已归档 wave 的需求标题相撞(撞名会让 archive 的 rebuild 把 ADDED 误判为 MODIFY)。

#### 场景:后续 wave 以 ADDED 追加覆盖需求而非改旧需求

- **当** wave-2b / wave-2c 要把自己的 inspector 纳入本套件
- **那么** 它**必须**在套件 delta 里用 `新增需求` 追加一条**仅约束自己 cohort** 的覆盖需求(其清单列在该 change 的 proposal/tasks、由 snapshot 验收),**禁止** `MODIFY` 已归档 wave 的覆盖需求、**禁止**改写已冻结的 wave-2a 清单

#### 场景:套件 spec 不在 spec 层冻结增长型总清单

- **当** 审阅本套件 spec 的覆盖需求
- **那么** 各 wave 的具体 inspector 名称**必须**留在对应 change 的 proposal/tasks 由 snapshot 验收;**禁止**把跨 wave 的增长型 inspector 总清单写进本 spec 的单一需求(否则后续 wave 增项即构成对本 spec 的回溯修改)

### 需求:wave-2a 必须覆盖归档时冻结的单实例即时只读服务单元格

wave-2a cohort **必须**覆盖「**单实例 + 即时只读快照 + 确定性一次性录制**」这一采集风险类的 service inspector——其异常态能经有界、确定性 setup 后**即时采样**得到,**不**依赖持续 workload、时间窗口累积或非确定性时序。本 cohort 的具体 inspector(以本变更**归档时冻结**的 `proposal.md` / `tasks.md` 清单为准)**必须**全部以其声明 `name` 干净注册且 registry `errors == []`。

切片判据(reviewer 判定门,非机械门):异常态需**持续运行的 workload / 时间窗口**才能采到者**禁止**纳入 wave-2a(留 wave-2b);「主动构造一个异常态实例并一次性录制」(如故意设错密码、停服务、放一份静态坏配置)**不**构成排除理由——基底已用此方式录 `access_denied` / `conn_refused` fixture。**关键区分**:「采样时刻确定性持有的外部资源」(如为造高连接率而**保持的固定数量连接**、为造内存压力而**预置的固定写入**)**不算**持续 workload——它在采样瞬间是确定快照;「持续 workload」**特指**采样窗口内**必须持续运行**才能命中的查询/流量(如长查询须在采样时仍在跑、错误率须时窗内累积真流量)。本判据是设计冻结的**人工 review 门**(reviewer 查该 inspector 的 semantic-abnormal 录制是否依赖采样窗口内持续运行的 workload),**非**可由 snapshot 机械验收的门(无法对「未纳入的 inspector」写断言)。

#### 场景:wave-2a 冻结清单全部干净注册

- **当** wave-2a 实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 proposal/tasks 列出的每个 wave-2a inspector(以**归档时冻结**清单为准;后续 wave 另立 change 不回溯改本 spec)**必须**以其声明 `name` 出现在 registry 中,且 registry `errors == []`

#### 场景:需持续 workload 或时间窗口的 inspector 由 reviewer 门挡在 wave-2a 外

- **当** reviewer 评估某 service inspector 是否属 wave-2a cohort
- **那么** 若其 semantic-abnormal 录制**必须**依赖采样窗口内**持续运行**的 workload(如长查询持续占用)、时间窗口累积(如慢日志/错误率)或非确定性时序,则**禁止**纳入 wave-2a;仅「确定性即时快照(含采样时刻持有的固定资源)可录」者纳入。本场景是 reviewer 判定准则、非机械断言

### 需求:套件内每个 inspector 必须遵守 service-inspector-contract

本套件内每个 inspector **必须**遵守 `service-inspector-contract` 的全部需求(连接参数注入安全 / secret 用 `HOSTLENS_` 前缀声明并 remap 到 client 原生 env 不进 argv / service 层失败三态分类 / 超时与输出纪律 / 跨 local 与 SSH target 无分叉 / 边界止于单实例)。本需求**引用**该契约而非重述其细则,以免两份 spec 漂移。

#### 场景:套件 inspector 守基底服务契约

- **当** 检视某套件 inspector 的 manifest 与 collector 命令
- **那么** 其连接参数注入、secret 声明与 remap、失败分类、超时输出、无 target 分叉**必须**全部满足 `service-inspector-contract` 对应需求;**禁止**以「属于套件」为由豁免该契约任一 MUST。**无连接 secret 的 inspector**(如 docker / nginx 探针)对 secret 相关 MUST 属**空集满足**(无 secret 即无可违反的 secret-remap 义务,这是「不适用」非「豁免」),其余 MUST(注入安全 / 失败分类 / 超时输出 / 无分叉)照常适用

### 需求:套件内每个 inspector 必须遵守作者契约且输出键区分聚合与列表型

本套件内每个 inspector **必须**为纯 YAML manifest 并遵守 `inspector-authoring-contract`(一切抽取与数值派生在 collector 内、finding 规则只做标量阈值/成员比较、`for_each` 单绑定、命令注入安全三件套、运行前提文档式声明)。**禁止** enable `hook.py`、**禁止**新增 parse format、**禁止**在 finding 表达式里做解析或数值派生。

关于输出顶层键,**裸聚合键允许**(纯标量聚合输出用不与 parameter 同名的裸标量键、而非 `inspector-authoring-contract` 第 22 行字面要求的 `results`/`items`/`records`)是 `os-shell-inspector-suite` 已归档 spec 既定且被接受的解读,本套件**逐字沿用**(故**不** MODIFY `inspector-authoring-contract`)。

但**列表型 vs 聚合型的分类判据,本套件对 os-shell 做了收紧、非逐字重述**:os-shell 用 **`for_each`** 区分(配 for_each=列表型 / 无 for_each=聚合型);本套件**改用更精确的判据——按 output_schema 是否含 array 顶层字段**区分,**与 finding 是否用 `for_each` 正交**。原因:一个 inspector 可有 array 输出而 finding 仍是标量(如 `docker.networks` 输出 `results` 列表但 finding 为标量 `dangling_networks >= warn_count`、**无 `for_each`**),按 os-shell 的 for_each 判据会把它误归聚合型→`results` 键判错。规则:**含 array 顶层字段的输出(列表型)**,该 array 顶层键**必须**取自 `results`/`items`/`records` 之一;**纯标量聚合输出(无 array 字段)**沿用裸标量键(与既有 `redis.memory_usage` / `mysql.connection_usage` 两个 collector 派生 JSON 聚合真例一致)。两种形态的顶层键都**禁止**与任一已声明 parameter 同名(否则 finding 上下文中 parameter 会静默遮蔽 output 键)。

判据收紧**不**触发对 `inspector-authoring-contract` 的 MODIFY(键命名是作者纪律、loader 无机器门,与判据用 for_each 还是 array-field 无关);第 22 行字面措辞的收紧(显式写入聚合/列表区分)登记为独立 follow-up(见 design 风险节),不阻塞本套件。

#### 场景:按输出形态(非 for_each)区分裸标量键与 results/items/records

- **当** 某套件 inspector 产出结果
- **那么** 若其 output_schema **无 array 顶层字段**(纯标量聚合),顶层标量键**允许**裸命名;若**含 array 顶层字段**(列表型,**无论 finding 是否用 `for_each`**),该 array 顶层键**必须**取自 `results`/`items`/`records` 之一且该列表**必须**在 collector 内截断为 top-N + total 计数;两者都**禁止**输出键与任一已声明 parameter 同名

#### 场景:数值派生在 collector 内

- **当** 某套件 inspector 需要派生量(如连接使用率、镜像磁盘占用、未挂载网络计数)
- **那么** 该派生**必须**由 collector 命令算出并写入输出 JSON,finding 规则只对已就绪标量做阈值/成员比较;**禁止**在 finding 表达式内现算

### 需求:套件内每个 inspector 必须附 ReplayTarget fixture 与可证检出的 snapshot

本套件内每个 inspector **必须**附带用 fixture 录制器(`inspector-fixture-recorder`)对真实服务录制的 `ReplayTarget` 兼容 fixture 与 snapshot 测试,经离线回放确定性出 `InspectorResult`。**禁止**手写 fixture、**禁止**日常 CI 依赖真实服务/网络。

双轨 fixture 要求**沿用** `service-inspector-contract`:凡 manifest 的 `findings` 列表**非空**,该 inspector **必须**附一份在 manifest **默认阈值**下触发 finding 的 semantic-abnormal fixture(对真实异常态录制),snapshot 断言其在默认阈值下产出预期 severity + message。**no-finding** inspector(`findings: []`,如健康探活型)**豁免** semantic-abnormal fixture(机械门「仅 findings 非空者要求」),但**必须**附至少两份 snapshot 证明其失败三态映射正确(如服务在→`ok`、不可达→`exception`)。

#### 场景:findings 非空者必须有默认阈值触发的 semantic-abnormal fixture

- **当** 某套件 inspector 的 `findings` 列表非空
- **那么** 其测试集**必须**含一份对真实异常态录制的 semantic-abnormal fixture,snapshot 断言在 manifest **默认阈值**下产出预期 severity + message;**禁止**仅以「健康态 + 人为低阈值」的 finding-trigger fixture 判其验收通过

#### 场景:no-finding inspector 证失败三态而非 semantic-abnormal

- **当** 某套件 inspector 是 no-finding 失败三态型(`findings: []`)
- **那么** 它**豁免** semantic-abnormal finding fixture,但**必须**附 snapshot 证明服务可达→`status=ok`、服务不可达→`status=exception`(fail-loud,**禁止**伪造健康 ok)

#### 场景:离线回放确定性出结果

- **当** 在任意平台(含 macOS / CI)对某套件 inspector 运行其 snapshot 测试
- **那么** 它**必须**经 `ReplayTarget` 回放录制的 fixture、不触达真实服务/网络,并产出与快照一致的确定性 `InspectorResult`;非确定输出(随机值 / 时间戳)**必须**在录制时冻结

### 需求:本套件禁止引入新基础设施

本套件**必须**在现有 schema 字段集内完成,证明纯铺量无需新 infra:**禁止**改动 inspector manifest schema(不增删字段)、**禁止**新增 parse format(仅 `raw/table/json/kv`)、**禁止**扩 capability enum(现为 `{shell, file_read, ssh, systemd, docker_cli}`)、**禁止**新增 Python 运行时依赖、**禁止** enable `hook.py`。Agent 可见工具数组**必须不因本套件增减**(本套件不注册任何新 `ToolSpec`、不暴露新 Agent 工具)。

#### 场景:零对外契约变更

- **当** 套件实现完成
- **那么** inspector manifest schema、Agent 可见工具数组、parse format 集合、capability enum **必须**全部保持不变;**禁止**因本套件而改动任何对外运行时契约
