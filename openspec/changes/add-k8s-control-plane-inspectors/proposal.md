# 提案：K8s 控制面 inspector（kubectl 视角，M6 K8s 域收尾）

## 为什么

M6 覆盖矩阵的 K8s 域（pod OOM / evicted / pending / node pressure）是唯一整域空白。M8 交付 KubernetesTarget 后该域被「解锁」，但解锁方式与直觉相反：这些信号是**控制面状态**（住在 API server / etcd），pod-exec 视角根本看不到，pod 内也没有（也不该有）kubectl。`enable-k8s-inspector-targets`（已归档）的非目标已显式预约本提案：K8s 域 inspector 需 kubectl / API 视角，跑在**配有 kubeconfig 的管理机**上（`targets: [local, ssh]`），与 docker.* 域（host 上跑 docker CLI、不进容器）完全同构。现在做的理由：M8 四个 PR 收官、容器安全判据与 cohort guard 还热着，本提案是 M6 达到「每域 ≥3」退出条件的最后一块整域拼图。

## 变更内容

- **新增 5 个 builtin inspector**（`src/hostlens/inspectors/builtin/k8s/`，全部 `targets: [local, ssh]`、`requires_binaries: [kubectl, jq]`、`privilege: none`、只读 `kubectl get`）：
  - `k8s.pods.oom_killed` —— `containerStatuses[].lastState.terminated.reason == "OOMKilled"` 的容器证据（附 `restartCount`）。**命名诚实**：`lastState` 只保留最近一次终止，这是现时快照不是历史序列，故不叫覆盖矩阵登记的 `oom_history`（矩阵同步更名）
  - `k8s.pods.evicted` —— `status.reason == "Evicted"` 且 `phase == "Failed"` 滞留 API 的 pod
  - `k8s.pods.stuck_pending` —— 区分「正常调度中」与「卡住」：`pending_age_seconds` 阈值（默认 600）× `conditions[PodScheduled].reason == "Unschedulable"` 二维判定（severity 矩阵见 design）
  - `k8s.nodes.conditions` —— MemoryPressure / DiskPressure / PIDPressure **以及 Ready**（只报 pressure 而对 NotReady 视而不见才是命名撒谎）
  - `k8s.events.warnings` —— `type=Warning` 事件按 `(reason, involvedObject.kind, namespace)` 聚合计数、`min_count`（默认 3）门控；聚合键 namespace 取 event 自身 `metadata.namespace`（恒存在；cluster-scoped 对象如 Node 的 event 落 default ns）。计数兼容新老形态 `(.count // .series.count // 1)`；**输出不含时间字段**（`eventTime` 是 MicroTime 微秒小数，对其做时间计算的坑见 design）。入选理由：1h TTL 内的 Events 是唯一能看到 FailedMount / ImagePullBackOff / **BackOff**（给首批不含 crashloop inspector 提供降级覆盖）的数据源
- **参数面**（双防线：pattern 第二道墙 + `| sh` 第一道墙，与 `docker.containers.restart_loop` 的 `name_filter` 同构）：
  - `context`（string，`^[A-Za-z0-9_.:/@-]*$`，默认 `""` = 用 current-context；5 个 inspector 统一提供）—— pattern 放行 EKS ARN（含 `:/`）与 GKE（含 `_`）两大类主流 context 名，排除全部 shell 元字符
  - `namespace`（string，`^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$`，默认 `""` = `-A` 全集群；**仅 namespace 作用域的 4 个提供，`k8s.nodes.conditions` 除外**）—— pattern 取 RFC 1123 label 实形（首尾必须字母数字：禁 `-` 开头封死预检 positional 位的 `--help` 类 flag 注入、禁 `-` 结尾拒掉必非法的名字；零合法 namespace 被误拒）；默认全集群而非 `default`：控制面巡检的天然单位是集群，默认 `default` 会制造假阴性
  - `label_selector` **不纳入**（其文法含 `=,!()` 与空格，pattern 要么失守要么误拒合法 selector，defer）
- **cohort guard 计数更新**：`test_docker_target_cohort_guard.py` 冻结计数 65→70、`_EXCLUDE` roster +5（INCLUDE 28 不动；5 个新 inspector 均 `[local, ssh]`，docker⇔k8s 奇偶不变量对「双否」平凡成立）
- **覆盖矩阵更名**：TODO.md K8s 行三处更名 `k8s.pods.oom_history` → `k8s.pods.oom_killed`、`k8s.pods.pending` → `k8s.pods.stuck_pending`、`k8s.nodes.pressure` → `k8s.nodes.conditions`，并新增 `k8s.events.warnings` 第 5 格，5 格打 ✅
- **fixture + snapshot 测试**：走 `_CaptureTarget` 约定（作者编 kubectl JSON stdout，无真集群），必含五个 K8s 特有形态变体（Events 新老双形态 / 含 `eventTime` MicroTime 的行不影响聚合 / Pending pod 的 `containerStatuses: null` / evicted pod 缺 `status.message` / node 缺某 condition 类型，详见 design）

## 非目标（Non-Goals）

- **不做 `targets: [k8s]` 的 pod 内读取源 inspector**（serviceaccount token 到期类）——那是打破 docker⇔k8s 奇偶不变量的 escape hatch 场景，须独立提案同步修改 authoring-contract 判据 + guard 断言（归档 design 已登记）
- **不改 KubernetesTarget**（含「尊重 `kubectl.kubernetes.io/default-container` annotation」，仍是已登记的独立提案候选）
- **不做 `label_selector` 参数**（文法无法安全 pattern 化，follow-up）
- **不做 `k8s.pods.crashloop` 独立 inspector**（`events.warnings` 的 BackOff 聚合提供降级覆盖，登记下个 wave）
- **不做 Events 历史持久化或 TTL 1h 的任何 workaround**（event exporter 属监控系统职责，违反「专注诊断而非采集存储」红线）
- **不做多 context fan-out**（一次运行一个 context；多集群 = 多个 schedule / 多次调用）
- **不在 collector 内用 kubernetes-asyncio / API 直连**（collector 只允许 kubectl CLI，与 docker 域同构；API 直连需要 hook.py，M1-disabled）
- **零新 infra**：不动 manifest schema、不动 target、不加 capability、不 enable hook.py、不加 parse format

## 功能 (Capabilities)

### 新增功能

- `k8s-inspector-suite`: 5 个 kubectl 控制面视角 builtin inspector 套件的契约——逐 inspector 的采集源 / 判定语义 / 参数面 / fail-loud 门控 / fixture 形态变体要求

### 修改功能

- `inspector-authoring-contract`: 「容器适用性」需求 EXCLUDE 枚举中「同理未来 K8s 域 inspector……属独立提案」的预言句改为现在时点名 `k8s.*` 控制面管控类（targets 保持 `[local, ssh]`，与 `docker.*` 容器自身管控类并列）；需求标题不变，无 RENAMED

## 对外契约影响

| 契约面 | 影响 |
|---|---|
| Inspector manifest schema | **无**（零新字段、零新 parse format） |
| Agent tool schema / MCP tool schema | **无**（`run_inspector` 透出新 inspector 属数据面扩充，schema 不变） |
| Notifier Protocol / Schedule manifest | **无** |
| CLI 命令 | **无新命令**；`hostlens inspectors list` 多 5 行 |
| 测试契约 | `test_docker_target_cohort_guard.py` 冻结计数 65→70、EXCLUDE roster +5 |

## 完整 manifest 示例（`k8s/pods_stuck_pending.yaml`，判定语义最丰富的一个）

```yaml
name: k8s.pods.stuck_pending
version: 1.0.0
description: >-
  Detect pods stuck in Pending beyond an age threshold, distinguishing
  "normally scheduling" from "stuck" via the PodScheduled=Unschedulable
  condition. Runs kubectl against the cluster control plane from a
  management host (local/ssh) — NOT inside a pod.
tags: [k8s, pods, pending, scheduling]
targets: [local, ssh]
requires_binaries: [kubectl, jq]
privilege: none

parameters:
  type: object
  properties:
    # kubeconfig context; empty = current-context. Charset admits EKS ARN
    # (":/" chars) and GKE ("_" chars) context names while excluding every
    # shell metacharacter. Second wall behind `| sh`.
    context:
      type: string
      pattern: "^[A-Za-z0-9_.:/@-]*$"
      default: ""
    # Namespace scope; empty = -A (whole cluster). RFC 1123 label shape
    # (alphanumeric at both ends) — forbidding a leading dash closes the
    # positional flag-injection bypass of the namespace pre-check
    # (`kubectl get namespace --help` would exit 0 without checking anything).
    namespace:
      type: string
      pattern: "^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$"
      default: ""
    # A pod Pending longer than this is "stuck". 600s covers the two longest
    # benign Pending causes: cluster-autoscaler node provisioning and large
    # image pulls / volume attach.
    pending_age_seconds:
      type: integer
      default: 600
  additionalProperties: false

collect:
  # Args assembled via POSIX `set --` (no reliance on undocumented
  # `--context=''` behavior). FAIL-LOUD: kubectl and jq gated separately —
  # an unreachable API server exits non-zero with empty stdout →
  # status=exception (honest), never fabricated `{"results":[]}`.
  # Non-empty namespace is PRE-CHECKED via `kubectl get namespace`: a LIST
  # against a nonexistent namespace exits 0 with empty items — without the
  # pre-check a namespace typo would be blessed as status=ok "no findings"
  # (silent false negative). The pre-check GET is covered by the `view`
  # ClusterRole.
  # Age computed entirely in jq (`fromdateiso8601`; creationTimestamp is
  # guaranteed second-precision RFC3339) — no GNU `date -d` dependency.
  command: |
    set --
    [ -n {{ context | sh }} ] && set -- "$@" --context {{ context | sh }}
    if [ -n {{ namespace | sh }} ]; then
      kubectl get namespace {{ namespace | sh }} "$@" >/dev/null || { echo "namespace not found or not readable" >&2; exit 1; }
      set -- "$@" -n {{ namespace | sh }}
    else
      set -- "$@" -A
    fi
    pods=$(kubectl get pods "$@" --field-selector=status.phase=Pending -o json) || { echo "kubectl get pods failed" >&2; exit 1; }
    printf '%s' "$pods" | jq -c '{
      results: [.items[] | {
        name: .metadata.name,
        namespace: .metadata.namespace,
        age_seconds: ((now - (.metadata.creationTimestamp | fromdateiso8601)) | floor),
        unschedulable: (((.status.conditions // []) | map(select(.type == "PodScheduled" and .reason == "Unschedulable")) | length) > 0),
        scheduled_reason: (((.status.conditions // []) | map(select(.type == "PodScheduled")) | first | .reason) // "none")
      }]
    }' || { echo "jq failed" >&2; exit 1; }
  timeout_seconds: 30

parse:
  format: json

output_schema:
  type: object
  properties:
    results:
      type: array
      items:
        type: object
        properties:
          name:             { type: string }
          namespace:        { type: string }
          age_seconds:      { type: integer }
          unschedulable:    { type: boolean }
          scheduled_reason: { type: string }
        required: [name, namespace, age_seconds, unschedulable, scheduled_reason]
        additionalProperties: false
  required: [results]
  additionalProperties: false

findings:
  - for_each: "results as p"
    when: "p.unschedulable and p.age_seconds > pending_age_seconds"
    severity: critical
    message: "pod {p[namespace]}/{p[name]} unschedulable for {p[age_seconds]}s (scheduler verdict: {p[scheduled_reason]})"
  - for_each: "results as p"
    when: "p.unschedulable and p.age_seconds <= pending_age_seconds"
    severity: warning
    message: "pod {p[namespace]}/{p[name]} unschedulable (age {p[age_seconds]}s, autoscaler may still recover)"
  - for_each: "results as p"
    when: "not p.unschedulable and p.age_seconds > pending_age_seconds"
    severity: warning
    message: "pod {p[namespace]}/{p[name]} Pending {p[age_seconds]}s without Unschedulable verdict (PodScheduled reason: {p[scheduled_reason]})"
```

## Failure Modes

| # | 场景 | 行为 |
|---|---|---|
| 1 | API server 不可达 / context 不存在 / kubeconfig 缺失 | kubectl 非零退出 → collector exit 1 + 空 stdout → `status=exception`（诚实），**绝不**伪装成 `{"results":[]}` 的「无发现」 |
| 2 | RBAC 不足（能连上但 get 被 403） | kubectl 非零退出 → 同上 `status=exception`；错误文本进 stderr 证据 |
| 3 | 管理机没装 kubectl / jq | `requires_binaries` preflight → `status=requires_unmet`（skip 不报错） |
| 4 | 大集群响应慢 | `timeout_seconds: 30`（events 给 60）超时 → `status=timeout`；namespace 参数可裁剪范围 |
| 5 | Events 新老形态混存（`lastTimestamp: null` 的 events.k8s.io 写入） | 聚合计数用 `(.count // .series.count // 1)` 归一；fixture 双形态变体钉死该分支 |
| 6 | `namespace` 参数 typo（namespace 不存在） | kubectl 对不存在 ns 的 LIST 返回空集且退出 0，会被伪装成「无发现」——collector 在 namespace 非空时**必须**先 `kubectl get namespace` 预检（`view` ClusterRole 覆盖该 GET），不存在 / 不可读 → exit 1 → `status=exception`（诚实） |

## Operational Limits

- **并发**：无新并发面——inspector 由既有 runner 并行框架调度，每 inspector 单条 kubectl 进程
- **内存**：`kubectl get pods -A -o json` 在数千 pod 集群可达数十 MB，经命令替换全量缓冲后管道喂 jq（fail-loud 分段门控所需，**非流式**，峰值内存为响应体的数倍——shell 变量 1× + jq 解析树 2-3×）；`timeout_seconds` 30s（pods/nodes）/ 60s（events）是上界护栏，namespace 参数可裁剪范围
- **超时**：全部显式声明 `collect.timeout_seconds`，不依赖默认值

## Security & Secrets

- **零新密钥**：认证完全复用管理机既有 kubeconfig（`~/.kube/config` 或 `KUBECONFIG`），Hostlens 不读、不存、不传输 kubeconfig 内容；无 `secrets:` 声明
- **只读**：全部 `kubectl get`，无任何写动词；建议（docs 载明）管理机 ServiceAccount 绑 `view` ClusterRole 即可
- **注入面**：`context` / `namespace` 两个 string 参数，pattern + `| sh` 双防线（manifest 加载期机械校验，缺 pattern 直接拒载）；数值参数免疫
- **不扩大攻击面**：不监听端口、不引入新依赖（kubectl 是用户管理机自备二进制）

## Cost / Quota Impact

- **零 LLM token 影响**：inspector 是纯采集单元（§4.2 红线：Inspector 不调 LLM）；Agent 侧仅 registry 概览多 5 行 inspector 元数据，进既有 prompt cache（`cache_control: ephemeral`），增量 <500 token 且命中缓存
- **零 Anthropic API 调用增量**；kubectl 对 API server 的压力 = 每次巡检 5 个 GET（events 那个 LIST 较重，靠 namespace 参数与 timeout 兜底）

## Demo Path

```bash
pip install -e ".[dev]"
# 1. 离线（无集群、无 API key）：snapshot 测试回放全部 5 个 inspector 的固定 kubectl JSON
pytest tests/inspectors/test_k8s_inspectors.py -q        # 全绿，含五个形态变体
# 2. 真机（kind 单节点集群，~3 分钟）：D-7 约定下 fixture 不验证 collector shell/jq 正确性，
#    真机兜底必须 5/5 覆盖——每个 collector 至少真机执行一次（健康集群上 status=ok 空 findings 也算执行证据），
#    且 namespace 预检分支必须以「存在 / 不存在」两路径各真机执行一次
kind create cluster
# 真 OOMKilled：memory limit 16Mi + 容器内超限分配（exit 137 ≠ OOMKilled，必须真触发 cgroup OOM）
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: oom-demo}
spec:
  restartPolicy: Always
  containers:
  - name: c
    image: python:3.11-alpine
    command: ["python", "-c", "x = bytearray(64 * 1024 * 1024)"]
    resources: {limits: {memory: "16Mi"}}
EOF
# 不可调度 pod（label key 显式加引号——防御性写法，避免任何 YAML 1.1 标量歧义）
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: pending-demo}
spec:
  nodeSelector: {"hostlens.demo/no-such": "node"}
  containers: [{name: c, image: busybox, command: ["sleep", "1d"]}]
EOF
sleep 90   # 等 OOMKill + ≥2 个 BackOff 周期积累（60s 在慢网络下是时序赌博）
hostlens target add mgmt --type local
hostlens inspect mgmt --inspector k8s.pods.oom_killed      # 报 oom-demo OOMKilled 证据（critical）
hostlens inspect mgmt --inspector k8s.pods.stuck_pending --parameters '{"pending_age_seconds": 30}'
                                                           # 报 pending-demo unschedulable（critical）
hostlens inspect mgmt --inspector k8s.pods.evicted         # 健康 kind 无 evicted → status=ok 空 findings（执行证据）
hostlens inspect mgmt --inspector k8s.pods.evicted --parameters '{"namespace": "default"}'
                                                           # namespace 预检·存在路径（status=ok）
hostlens inspect mgmt --inspector k8s.pods.evicted --parameters '{"namespace": "nosuchns"}' ; echo "exit=$?"
                                                           # namespace 预检·typo 路径（status=exception，非空 ok）
hostlens inspect mgmt --inspector k8s.nodes.conditions     # Ready=True → status=ok（执行证据）
hostlens inspect mgmt --inspector k8s.events.warnings --parameters '{"min_count": 2}'
                                                           # 报 BackOff 聚合（warning）
kind delete cluster
```

## 影响

- **代码**：新增 `src/hostlens/inspectors/builtin/k8s/` 5 个 YAML + `tests/inspectors/test_k8s_inspectors.py`（含 `_CaptureTarget` fixture）；修改 `tests/inspectors/test_docker_target_cohort_guard.py`（计数 + roster）
- **spec**：ADD `k8s-inspector-suite`；MODIFIED `inspector-authoring-contract`（EXCLUDE 枚举一处措辞，需求标题不变）
- **文档**：TODO.md 覆盖矩阵 K8s 行更名 + 打 ✅；`docs/operations/inspectors.md` 新增「管理机」概念段（kubectl target 指到哪、RBAC 最小权限 `view`）
- **依赖**：零新 Python 依赖（kubectl / jq 是目标机二进制，走 `requires_binaries` preflight）
