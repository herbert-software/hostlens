# 设计：K8s 控制面 inspector

## Context

M6 覆盖矩阵 K8s 域是最后一个整域空白。M8 已交付 KubernetesTarget（pod-exec 视角）与容器安全 cohort（INCLUDE 28 / EXCLUDE 37 / 总 65，`test_docker_target_cohort_guard.py` 冻结），`enable-k8s-inspector-targets` 归档时显式预约本提案。核心架构事实：OOMKilled / evicted / pending / node conditions / warning events 全部是**控制面状态**（API server / etcd），pod 内无 kubectl 也不该有集群读权限——本域 inspector 跑在配有 kubeconfig 的管理机上，同构先例是 docker.* 域（host 上跑 docker CLI）。约束：零新 infra（不动 manifest schema / target / capability / parse format，不 enable hook.py）；authoring contract 全部纪律适用（fail-loud、`{results:[...]}` 包裹、双防线注入防御、DSL 只做标量比较）。

## Goals / Non-Goals

**Goals**：
- 5 个 kubectl 视角 builtin inspector（oom_killed / evicted / stuck_pending / nodes.conditions / events.warnings），每个有 `_CaptureTarget` fixture + snapshot 测试
- cohort guard 计数 65→70 一次性更新；奇偶不变量与 INCLUDE 28 不动
- authoring contract EXCLUDE 枚举的 K8s 预言句转现在时

**Non-Goals**（详见 proposal）：pod 内 k8s-only 读取源类（escape hatch 独立提案）/ 改 KubernetesTarget / label_selector / crashloop 独立 inspector / Events 持久化 / 多 context fan-out / API 直连。

## Decisions

### D1 范围 = 既定 4 个 + `k8s.events.warnings`，共 5 个

`lastState.terminated` 只留最近一次终止，1h TTL 内的 Events 是唯一能看到 FailedMount / FailedScheduling / ImagePullBackOff / probe failure / **BackOff** 的数据源——BackOff 聚合还给「首批不含 crashloop inspector」提供降级覆盖。噪声控制在 collector 内：按 `(reason, involvedObject.kind, namespace)` 分组计数，`min_count` 参数（默认 3）门控，findings 只报 warning（Events 是间接证据，不报 critical）。聚合键的 namespace 维度**取 event 自身 `metadata.namespace`**（Event 是 namespaced 对象、该字段恒存在；cluster-scoped involvedObject 如 Node 的 event 落 default ns），不取 `involvedObject.namespace`（cluster-scoped 时为空，会在 output_schema string 校验上炸）。输出**不含时间字段**、聚合不做任何时间计算（`eventTime` 是 MicroTime，见 D5 坑 2）。对周期 ≤1h 的 schedule，TTL 1h 无窗口缺失。

**被否**：首批加 `k8s.pods.crashloop`（waiting.reason 同读取源、诱惑大）——5 个已对齐 wave-2a 体量上限（先例一次 6 个），且 events 的 BackOff 提供降级覆盖；登记 follow-up。

### D2 命名诚实性：oom_killed / evicted / stuck_pending / nodes.conditions / events.warnings

- `oom_killed` 而非矩阵登记的 `oom_history`：报告的是「当前 lastState 留有 OOMKilled 证据的容器」，现时快照非历史序列（与 `linux.kernel.oom_killer` 风格对齐）
- `stuck_pending` 而非 `pending`：判定的是「卡住」，存在 Pending pod 是正常态
- `nodes.conditions` 而非 `nodes.pressure`：同一条 `kubectl get nodes -o json` 读出 pressure 三兄弟**和 Ready**，只报 pressure 对 NotReady 视而不见才是言不符实；两段式对象名有 `docker.networks` 先例
- 覆盖矩阵（TODO.md）同步更名，本提案交付物之一

### D3 stuck_pending 判定：单参数 + 二维 severity 矩阵 + jq 算 age

- `pending_age_seconds` 默认 **600**：盖过两类最长「正常 Pending」——cluster-autoscaler 节点供给（云上 2–5min）与大镜像拉取 / volume attach（phase==Pending 涵盖已调度但 ContainerCreating 的 pod）；Hostlens 是周期巡检非实时告警，宁低噪
- severity 矩阵：`unschedulable ∧ age>阈值` → critical；`unschedulable ∧ age≤阈值` → warning（autoscaler 可能正在救）；`¬unschedulable ∧ age>阈值` → warning；其余无 finding。`unschedulable` = `conditions[type==PodScheduled].reason == "Unschedulable"`（scheduler 显式判决）
- age 全部在 jq 内算：`(now - (.metadata.creationTimestamp | fromdateiso8601)) | floor`——jq 本就在 `requires_binaries`，`fromdateiso8601` 是 jq ≥1.5 builtin，免去 GNU `date -d` 的 Linux-only 依赖；`creationTimestamp` 保证秒精度 `Z` 结尾 RFC3339，恰是 `fromdateiso8601` 的严格格式
- collector 输出每 pod `{name, namespace, age_seconds, unschedulable, scheduled_reason}` 全标量，DSL 只做比较（authoring contract「墙 1」）

**被否**：双阈值（warning 阈 + critical 阈）——参数面翻倍换不来判定质量，unschedulable 布尔已是第二维。

### D4 参数面：context + namespace，双防线，默认全集群

| 参数 | 提供范围 | pattern | 默认 | 语义 |
|---|---|---|---|---|
| `context` | 5 个统一 | `^[A-Za-z0-9_.:/@-]*$` | `""` | 空 = 不传 `--context`（current-context） |
| `namespace` | ×4（`nodes.conditions` 除外——node 无 namespace 概念） | `^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$` | `""` | 空 = `-A`；非空 = `-n <ns>` |
| `pending_age_seconds` / `min_count` 等数值 | 按 inspector | 无需 | 600 / 3 | integer 免 `\| sh` |

- context pattern 难点是 EKS ARN（`arn:aws:eks:…:cluster/foo`，含 `:/`）与 GKE（`gke_proj_zone_name`，含 `_`）——该 charset 全放行，同时排除空白 / 引号 / `;|&$` 等全部 shell 元字符；pattern 是第二道墙，第一道仍是 `| sh`（shlex.quote），与 `docker.containers.restart_loop` 的 `name_filter` 同构；`*` 量词容纳空默认值（schema-default 注入不违约，见 manifest parameters 既有教训：parameters 必须 `type: object` 包裹，pattern 才生效）
- namespace pattern 取 RFC 1123 label **实形**（首尾必须字母数字）而非裸字符集 `^[a-z0-9-]*$`：namespace 在预检命令中处于 **positional 位**，裸字符集会放行 `--help` 一类 leading-dash 值——`kubectl get namespace --help` 打印帮助且 exit 0，预检被空过、随后的 LIST 对不存在 ns 返回空集，恰好绕回预检要杀的假阴性类；`| sh`（quoting）对 argv 语义级的 flag 注入不设防，必须由 pattern 这道墙封。禁尾 `-` 顺带拒掉必非法的名字（即使放行也只会落进预检 fail-loud，收紧使「实形」声称为真）。实形收紧零合法 namespace 被误拒。context 无此暴露（恒处 `--context` 的 flag 值位被消费）
- 默认 `""` → `-A` 而非 `"default"`：控制面巡检天然单位是集群，真实集群 default namespace 几乎不承载业务，默认 `default` 会让 inspector 默认报「一切正常」——假阴性比噪声更糟
- **namespace 非空必须预检**：kubectl 对不存在 namespace 的 LIST 返回空集且退出 0——typo 的 namespace 会被祝福为 `status=ok`「无发现」，与上一条同一类假阴性。collector 在 `-n` 分支前先 `kubectl get namespace <ns> >/dev/null || exit 1`（一次廉价 GET，`view` ClusterRole 覆盖），不存在 / 不可读 → fail-loud → `status=exception`
- 空参数分支用 POSIX `set --` 组装 args（`[ -n {{ context | sh }} ] && set -- "$@" --context {{ context | sh }}`），**不依赖** `kubectl --context=''` 的未文档化行为

**被否**：`label_selector`——文法含 `=` `,` `!` `()` 与空格（`env in (a,b)`），pattern 要么宽到失守要么拒掉合法 selector；namespace 已覆盖主要裁剪需求，YAGNI。

### D5 一个提案装 5 个；fixture 走 `_CaptureTarget`；五个 K8s 特有坑显式登记

切分判据看共享面：同域目录、同 `requires_binaries`、同 context/namespace 参数模式、cohort guard 冻结计数**改一次**（65→70、EXCLUDE +5）而非五个 PR 各 rebase 一次；wave-2a 一次 6 个先例背书体量。奇偶不变量无忧：5 个全 `targets: [local, ssh]`，`("docker" in targets)==("k8s" in targets)` 对双否平凡成立。

kubectl 版本漂移担忧基本 moot——`-o json` 输出是 versioned API object（pods/nodes/events 全 core/v1，字段稳定多年），非 kubectl 自家渲染。**真正的坑有五，fixture 必含对应变体**：

1. **Events 双形态**：老式 event 有 `lastTimestamp` + `count`；新式（events.k8s.io 写入）`lastTimestamp: null`、用 `eventTime` + `series.count`——聚合计数必须 `(.count // .series.count // 1)` 三级兜底，fixture 两形态各编一条
2. **`eventTime` 是 MicroTime**（微秒小数），`fromdateiso8601` 对小数秒失败——本套件输出不含时间字段、聚合不做时间计算，故仅为「不影响聚合」验证项：fixture 含 MicroTime 行、断言聚合结果不受其影响（未来若做时间计算须先 `sub("\\.[0-9]+Z$"; "Z")` 归一；`creationTimestamp` 无此问题）
3. **null 形态（pods）**：Pending pod 的 `containerStatuses` 可为 null、`lastState` 可为 `{}`——jq 全程 `//` 兜底，fixture 必含 null 变体行，否则 snapshot 锁不住分支
4. **evicted 缺 `status.message`**：并非所有 Evicted pod 都带 message——jq `// ""` 兜底为空摘要，否则真 Evicted pod 反把整次运行打成 schema 失败
5. **node 缺某 condition 类型**：老集群可无 PIDPressure、异常 node 可缺 Ready 上报——缺失类型兜底为 `"Unknown"`；`Ready` 兜底后仍按 `!= True` → critical（缺 Ready 本身就是告警信号）

fail-loud 与 docker 域逐位对应：kubectl 连不上 / context 不存在 / RBAC 403 → 非零退出 → exit 1 + 空 stdout → `status=exception`（诚实）；集群真没事 → kubectl 退出 0 且 `items: []` → `{"results":[]}`（诚实空）。kubectl 与 jq **分段门控**（各自 `|| { echo … >&2; exit 1; }`），不合并管道（`||` 只看末段退出码，`set -o pipefail` 非 POSIX）。

**被否**：拆 pods 提案 + nodes/events 提案——共享面 >90%，拆分只制造两次 guard 计数 rebase 冲突。

### D6 spec delta 策略：MODIFIED 不 RENAMED

`inspector-authoring-contract` 的「容器适用性」需求标题不变，只改 EXCLUDE 枚举一处措辞（预言句→现在时点名 `k8s.*` 控制面管控类）。MODIFIED delta 必须复制完整需求块（含全部场景）——部分复制会在归档时丢内容（既有踩坑）。delta 用中文标题（`### 需求:` / `#### 场景:` / `**当**` / `**那么**`），英文标题过 validate 但过不了 archive rebuild。

## Risks / Trade-offs

- **[Events TTL 1h，低频 schedule 有窗口盲区]** → docs 载明「events.warnings 对周期 >1h 的 schedule 只看到最近 1h」；不做持久化 workaround（非目标）
- **[`-A` 默认在多租户大集群可能慢 / 输出大]** → `timeout_seconds`（pods/nodes 30s、events 60s）+ `namespace` 参数裁剪；超时 → `status=timeout` 诚实降级
- **[jq `now` 与节点时钟偏差影响 age]** → age 用于 600s 量级阈值判定，秒级偏差无害；NTP 漂移本身有 `net.ntp.drift` 专门 inspector
- **[管理机概念对用户新]** → `docs/operations/inspectors.md` 新增「管理机」段：kubectl target 指到哪、RBAC 最小权限 `view` ClusterRole
- **[fixture 锁不住 collector shell 正确性（D-7 固有）]** → 命令串级锁 + kind 真机 Demo Path 兜底；PR 描述禁 claim「fixture 验证了 collector 正确性」

## Migration Plan

纯增量（5 个新 YAML + 测试 + 一处 spec 措辞），无数据迁移、无契约破坏。回滚 = revert 单个 squash commit。部署顺序无要求。

## Open Questions

- `k8s.pods.crashloop`（waiting.reason==CrashLoopBackOff）登记为下个 wave 候选——读取源与 oom_killed 同（containerStatuses），届时评估是否与 token 到期类（escape hatch 提案）合批
- 多集群体验（context 枚举 / fan-out）若有真实需求，属 Schedule manifest 层（多 schedule 各配 context 参数），不动 inspector
