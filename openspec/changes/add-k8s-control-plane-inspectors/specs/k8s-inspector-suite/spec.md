# k8s-inspector-suite 规范（delta）

> 目的：定义 5 个 kubectl 控制面视角 builtin inspector（`k8s.pods.{oom_killed,evicted,stuck_pending}` / `k8s.nodes.conditions` / `k8s.events.warnings`）的采集源、判定语义、参数面与测试契约。

## 新增需求

### 需求:控制面 inspector 必须以 kubectl 视角跑在管理机上

K8s 控制面类 inspector（`k8s.*`）的采集信号是 API server 状态，**必须**声明 `targets: [local, ssh]`（管理机视角）、`requires_binaries: [kubectl, jq]`，**必须**显式声明 `privilege: none`，**禁止**声明容器类 target（`docker` / `k8s`）——pod 内无 kubectl 也不得引入集群读权限。每个 manifest **必须**显式声明 `collect.timeout_seconds`（pods / nodes 类 30、events 类 60）。collector **必须**只使用只读的 `kubectl get`，**禁止**任何写动词与 API 直连（kubernetes-asyncio / curl API server）。认证**必须**完全复用管理机既有 kubeconfig，**禁止**经 manifest `secrets:` 或参数传递任何凭据内容。

#### 场景:manifest 声明形态

- **当** 加载 `k8s.pods.oom_killed` 等 5 个 builtin manifest
- **那么** 每个 manifest 的 `targets` 必须恰为 `[local, ssh]`，`requires_binaries` 必须含 `kubectl` 与 `jq`，`privilege` 必须显式声明为 `none`（manifest 源文件内可见，不依赖 schema 默认），`collect.timeout_seconds` 必须显式声明（pods / nodes 类 30、events 类 60），`collect.command` 中 kubectl 子命令必须仅为 `get`

#### 场景:管理机缺 kubectl 时诚实 skip

- **当** 目标机 PATH 中无 `kubectl` 二进制
- **那么** runner preflight 必须以 `status=requires_unmet` skip（而非报错），与既有 `requires_binaries` 语义一致

### 需求:k8s.pods.oom_killed 必须报告 lastState 中的 OOMKilled 证据且命名诚实

inspector **必须**经 `kubectl get pods -o json` 读取 `status.containerStatuses[].lastState.terminated.reason == "OOMKilled"` 的容器，输出 `{results: [...]}`（每行含 pod 名、namespace、容器名、`restart_count` 标量）。**必须**对每个 OOMKilled 证据产生 `severity=critical` finding（message 含 namespace/pod/容器与 restartCount）。语义上这是**现时快照**（`lastState` 只保留最近一次终止）而非历史序列——描述文案**禁止**宣称「历史」覆盖。

#### 场景:容器留有 OOMKilled 证据

- **当** 某 pod 的 `containerStatuses[].lastState.terminated.reason` 为 `OOMKilled`
- **那么** 必须产生 critical finding，message 含该 pod 的 namespace、名称、容器名与 `restart_count`

#### 场景:containerStatuses 为 null 时不崩溃

- **当** 某 pod（如 Pending 中）的 `status.containerStatuses` 为 null 或缺失
- **那么** collector 的 jq 必须以 `//` 兜底跳过该 pod，不产生解析异常

### 需求:k8s.pods.evicted 必须报告滞留 API 的 Evicted pod

inspector **必须**报告 `status.phase == "Failed"` 且 `status.reason == "Evicted"` 的 pod（每行含 pod 名、namespace、`status.message` 的驱逐原因摘要；`status.message` 缺失时**必须**经 jq `// ""` 兜底为空摘要，**禁止**让真 Evicted pod 因缺 message 把整次运行打成 schema 失败），并对每个产生 `severity=warning` finding（驱逐已发生、pod 为滞留残骸，指示节点压力史而非进行中故障）。

#### 场景:Evicted pod 缺 status.message

- **当** 某 Evicted pod 的 `status.message` 缺失
- **那么** 该行摘要必须兜底为空字符串，正常产生 warning finding，不产生解析或 schema 异常

#### 场景:存在 Evicted pod

- **当** 集群中存在 `phase=Failed, reason=Evicted` 的 pod
- **那么** 必须产生 warning finding，message 含 namespace/pod 与驱逐原因摘要

### 需求:k8s.pods.stuck_pending 必须区分正常调度与卡住

inspector **必须**仅采集 `status.phase == "Pending"` 的 pod（`--field-selector`），对每 pod 输出 `{name, namespace, age_seconds, unschedulable, scheduled_reason}` 全标量行：`age_seconds` **必须**在 jq 内经 `now - (creationTimestamp | fromdateiso8601)` 计算（**禁止**依赖 GNU `date -d`）；`unschedulable` 定义为 `conditions[type==PodScheduled].reason == "Unschedulable"`；`scheduled_reason` 在缺 PodScheduled condition 或 reason 缺失时**必须**经 jq `// "none"` 兜底（与 `k8s.pods.evicted` 的 message 兜底对称，**禁止**让极新 Pending pod 以 null 撞 output_schema 校验；k8s reason 值恒为 CamelCase，小写 `none` 无撞名歧义）。findings **必须**实现二维 severity 矩阵（`pending_age_seconds` 参数默认 600）：

| 条件 | severity |
|---|---|
| `unschedulable` 且 `age_seconds > 阈值` | critical |
| `unschedulable` 且 `age_seconds ≤ 阈值` | warning |
| 非 `unschedulable` 且 `age_seconds > 阈值` | warning |
| 其余 | 无 finding |

#### 场景:超龄且 scheduler 判决不可调度

- **当** 某 Pending pod 的 `PodScheduled` condition reason 为 `Unschedulable` 且 age 超过 `pending_age_seconds`
- **那么** 必须产生 critical finding，message 含 scheduler 判决原因

#### 场景:年轻的 Pending pod 不告警

- **当** 某 Pending pod age 未超阈值且无 `Unschedulable` 判决
- **那么** 必须不产生任何 finding（正常调度中）

### 需求:k8s.nodes.conditions 必须同时覆盖 pressure 与 Ready

inspector **必须**经 `kubectl get nodes -o json` 对每个 node 输出 `Ready` / `MemoryPressure` / `DiskPressure` / `PIDPressure` 四类 condition 的标量状态；node 缺某 condition 类型时（老集群无 PIDPressure、异常 node 缺 Ready 上报）该列**必须**兜底为 `"Unknown"`，**禁止**因缺失类型产生解析或 schema 异常；兜底值与 k8s 原生 `status: Unknown`（kubelet 失联）合流为**已知折衷**——两者对 Ready 同为 critical，语义合流不构成误归因。findings：`Ready != True` **必须** `severity=critical`（含兜底后的 `"Unknown"`——缺 Ready 上报本身就是告警信号）；任一 pressure condition 为 `True` **必须** `severity=warning`。**禁止**只报 pressure 而忽略 NotReady。

#### 场景:节点 NotReady

- **当** 某 node 的 `Ready` condition status 非 `True`
- **那么** 必须产生 critical finding

#### 场景:节点有内存压力

- **当** 某 node 的 `MemoryPressure` condition status 为 `True`
- **那么** 必须产生 warning finding

### 需求:k8s.events.warnings 必须聚合降噪且只报 warning

inspector **必须**采集 `type=Warning` 的 events，在 collector 内按 `(reason, involvedObject.kind, namespace)` 聚合计数——namespace 维度**必须**取 event 自身 `metadata.namespace`（Event 是 namespaced 对象、该字段恒存在；**禁止**取 `involvedObject.namespace`，cluster-scoped involvedObject 如 Node 时该字段为空会在 schema 校验上炸）。仅对计数 ≥ `min_count`（参数，默认 3）的聚合行产生 finding，severity **必须**为 `warning`（Events 是间接证据，**禁止** critical）。聚合计数 jq **必须**以 `(.count // .series.count // 1)` 三级兜底兼容新老两种 event 形态。输出**禁止**含时间字段、聚合**禁止**做任何时间计算（`eventTime` 是 MicroTime 含微秒小数，`fromdateiso8601` 对其失败；未来若需时间计算必须先归一化小数秒）。

#### 场景:BackOff 事件聚合达到阈值

- **当** 某 namespace 中同 reason=BackOff、kind=Pod 的 Warning event 聚合计数 ≥ `min_count`
- **那么** 必须产生 warning finding，message 含 reason / kind / namespace / 计数

#### 场景:新式 event 无 lastTimestamp

- **当** event 由 events.k8s.io 写入（`lastTimestamp: null`、有 `eventTime` 与 `series.count`）
- **那么** 聚合计数必须取 `series.count`，不产生解析异常；含 MicroTime `eventTime` 的行不影响聚合结果

#### 场景:cluster-scoped 对象的 event 正常聚合

- **当** 某 Warning event 的 `involvedObject` 是 cluster-scoped 对象（如 Node，`involvedObject.namespace` 为空）
- **那么** 聚合键 namespace 维度取 event 自身 `metadata.namespace`，正常计入聚合，不产生 schema 异常

### 需求:kubectl 失败禁止伪装为空结果

每个 inspector 的 collector **必须**对 kubectl 与 jq 分段门控（各自 `|| { echo … >&2; exit 1; }`，**禁止**合并管道后只看末段退出码）：API server 不可达、context 不存在、RBAC 拒绝时 **必须** exit 非零 + 空 stdout（→ `status=exception`）；**禁止**在 kubectl 失败时输出 `{"results":[]}`（会被祝福为 `status=ok` 的「无发现」假阴性）。集群真无异常时（kubectl 退出 0 且匹配集为空）**必须**输出 `{"results":[]}`。**namespace 参数非空时 collector 必须先经 `kubectl get namespace <ns>` 预检**（kubectl 对不存在 namespace 的 LIST 返回空集且退出 0——不预检则 namespace typo 被祝福为「无发现」假阴性；该 GET 由 `view` ClusterRole 覆盖），namespace 不存在 / 不可读 **必须** exit 非零 → `status=exception`。

#### 场景:API server 不可达

- **当** kubectl 因连不上 API server 退出非零
- **那么** collector 必须 exit 1 且 stdout 为空，InspectorResult 必须为 `status=exception`

#### 场景:集群无异常

- **当** kubectl 成功返回且匹配项为空（如无 Pending pod）
- **那么** collector 必须输出 `{"results":[]}`，`status=ok` 且无 finding

#### 场景:namespace 参数 typo 不得伪装为无发现

- **当** `namespace` 参数指向不存在的 namespace
- **那么** 预检 `kubectl get namespace` 非零退出 → collector exit 1 + 空 stdout → `status=exception`（诚实），禁止输出 `{"results":[]}` 报 `status=ok`

### 需求:context 与 namespace 参数必须走双防线且默认全集群

`context` 参数（pattern `^[A-Za-z0-9_.:/@-]*$`，默认 `""`）为 5 个 inspector 统一**必须**；`namespace` 参数（pattern `^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$`——RFC 1123 label 实形、首尾**必须**为字母数字（禁 `-` 开头封死预检 positional 位的 `--help` 类 flag 注入旁路、禁 `-` 结尾拒掉必非法的名字）；默认 `""`）为 namespace 作用域的 4 个（`k8s.pods.{oom_killed,evicted,stuck_pending}` + `k8s.events.warnings`）**必须**，`k8s.nodes.conditions` **禁止**提供（node 无 namespace 概念）。两参数均为 string（`parameters` **必须** `type: object` 包裹否则 pattern 静默失效），模板插值**必须**走 `| sh`。空 `context` **必须**不传 `--context`、空 `namespace` **必须**展开为 `-A`，参数组装**必须**用 POSIX `set --`（**禁止**依赖 `--context=''` 的未文档化行为）。**禁止**提供 `label_selector` 参数（文法无法安全 pattern 化）。

#### 场景:注入 payload 被 pattern 拒载

- **当** manifest 参数传入 `'; kubectl delete pod x; #` 一类含 shell 元字符的 context 值
- **那么** 参数校验必须在渲染前拒绝（pattern 不匹配），不进入命令模板

#### 场景:EKS ARN 形态的 context 名被放行

- **当** context 值为 `arn:aws:eks:us-east-1:123456789:cluster/prod`
- **那么** pattern 必须放行，渲染后经 `| sh` 引用为单参数

#### 场景:namespace 为空时查全集群

- **当** `namespace` 参数为默认空串
- **那么** kubectl 调用必须带 `-A`（全 namespace），而非限定 `default`

### 需求:fixture 必须覆盖 K8s 特有形态变体且真机路径兜底

5 个 inspector **必须**各有 `_CaptureTarget` fixture（作者编 kubectl JSON stdout）+ snapshot 测试，且 fixture 集合**必须**含五类形态变体：Events 新老双形态、含 MicroTime `eventTime` 的行不影响聚合、Pending pod 的 `containerStatuses: null`、Evicted pod 缺 `status.message`、node 缺某 condition 类型。fixture 测试**不验证** collector shell 逻辑的真实正确性（离线不执行 kubectl/jq）——交付**必须**附 kind 集群真机 Demo Path 作为 collector 正确性兜底，且**必须** 5 个 collector 各至少真机执行一次（健康集群上 `status=ok` 空 findings 也算执行证据），namespace 预检分支**必须**以「namespace 存在」与「namespace 不存在」两路径各真机执行一次（offline fixture 不执行 shell，预检分支两侧均无覆盖时兜底声明即为伪验收），PR 描述**禁止**宣称「fixture 锁定了 collector 正确性」、**禁止**宣称未真机执行过的 collector 或分支已被兜底。

#### 场景:snapshot 锁定 null 变体分支

- **当** fixture 含 `containerStatuses: null` 的 pod 行
- **那么** snapshot 测试必须覆盖该行经 jq `//` 兜底后的输出形态

#### 场景:cohort guard 计数同步

- **当** 5 个 `k8s.*` manifest 加入 builtin
- **那么** `test_docker_target_cohort_guard.py` 的冻结计数必须更新为 EXCLUDE 42 / 总数 70（INCLUDE 28 不动），5 个新名字进 `_EXCLUDE` roster，奇偶不变量 `("docker" in targets)==("k8s" in targets)` 对其平凡成立
