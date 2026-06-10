# 任务：add-k8s-control-plane-inspectors

## 1. 分支与前置核查

- [x] 1.1 从最新 main 切 `feat/add-k8s-control-plane-inspectors` 分支
- [x] 1.2 **全仓**（含仓根 TODO.md / CLAUDE.md / README.md）grep 硬编码 cohort 计数扇出点（`28` / `37` / `65` 出现在断言或枚举处），列出本次须同步的全部位置清单（已知：`tests/inspectors/test_docker_target_cohort_guard.py` 计数断言与 docstring、TODO.md:432「65 个」叙述段、TODO.md M8 章节「INCLUDE 28 / EXCLUDE 37」、CLAUDE.md §9「65 个」；防漏其他上界断言 / docs 枚举）

## 2. 五个 inspector manifest（`src/hostlens/inspectors/builtin/k8s/`）

- [x] 2.1 `pods_oom_killed.yaml`：`lastState.terminated.reason=="OOMKilled"` 证据采集（含 `restart_count`），critical finding；`containerStatuses: null` 经 jq `//` 兜底；description 不宣称「历史」
- [x] 2.2 `pods_evicted.yaml`：`phase==Failed ∧ reason==Evicted` 滞留 pod，warning finding（含驱逐原因摘要；`status.message` 缺失经 jq `// ""` 兜底为空摘要）
- [x] 2.3 `pods_stuck_pending.yaml`：`--field-selector=status.phase=Pending` + jq `fromdateiso8601` 算 `age_seconds` + `unschedulable` 布尔 + `scheduled_reason` 缺失经 `// "none"` 兜底；`pending_age_seconds` 默认 600；二维 severity 矩阵三条 findings（critical / warning / warning）
- [x] 2.4 `nodes_conditions.yaml`：每 node 输出 Ready / MemoryPressure / DiskPressure / PIDPressure 标量（缺某 condition 类型兜底 `"Unknown"`）；`Ready != True` → critical（含 `"Unknown"`）、pressure `True` → warning；**无 namespace 参数**（node 无 namespace 概念，2.6 的 namespace 项对本 manifest 不适用）
- [x] 2.5 `events_warnings.yaml`：`type=Warning` 按 `(reason, involvedObject.kind, namespace)` 聚合计数（namespace 取 event 自身 `metadata.namespace`，禁取 `involvedObject.namespace`），`min_count` 默认 3 门控，仅 warning；聚合计数 `(.count // .series.count // 1)` 三级兜底；输出不含时间字段、不做时间计算；`timeout_seconds: 60`（其余 4 个 30）
- [x] 2.6 五个 manifest 统一项逐个核对：`targets: [local, ssh]`、`requires_binaries: [kubectl, jq]`、`privilege: none`（显式）、显式 `collect.timeout_seconds`、`parameters` 为 `type: object` 包裹（否则 pattern 静默失效）、`context`（`^[A-Za-z0-9_.:/@-]*$`）默认 `""` ×5、`namespace`（`^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$`，RFC 1123 label 实形、首尾必须字母数字）默认 `""` ×4（**nodes_conditions 除外**）、string 插值全走 `| sh`、POSIX `set --` 组装 args（空 context 不传 `--context`、空 namespace 给 `-A`、非空 namespace 先 `kubectl get namespace` 预检门控）、kubectl 与 jq 分段 fail-loud 门控（各自 `|| { echo … >&2; exit 1; }`）、输出 `{results: [...]}` 顶层对象、findings DSL 只做标量比较

## 3. 测试

- [x] 3.1 `tests/inspectors/test_k8s_inspectors.py`：5 个 inspector 各配 `_CaptureTarget` fixture（作者编 kubectl JSON stdout）+ snapshot 测试（snapshot 断言 `.rstrip("\n")` 容忍 pre-commit 尾换行）；兄弟 helper 导入用 `from inspectors._x` 形式（CI pythonpath 约定）
- [x] 3.2 五个 K8s 特有形态变体进 fixture 并被 snapshot 锁定：(a) Events 新老双形态各一条（`lastTimestamp: null` + `eventTime` + `series.count`）；(b) 含 MicroTime `eventTime` 的行断言不影响聚合结果；(c) Pending pod `containerStatuses: null` 行；(d) Evicted pod 缺 `status.message` 行；(e) node 缺某 condition 类型（如无 PIDPressure）行
- [x] 3.3 注入防御测试：context / namespace 传 `'; whoami; #` 类 payload 断言 pattern 拒载；namespace 传 `--help` / `-w` 类 leading-dash 值断言拒载（预检 positional 位 flag 注入旁路）、传 `a-` 尾 dash 值断言拒载（RFC 1123 实形）、传 `a` / `123` / `kube-system` 断言放行；EKS ARN 形态 context 断言放行（测试里写全量字面 ARN `arn:aws:eks:us-east-1:123456789:cluster/prod`，不要抄省略号）；GKE 形态 `gke_proj_zone_name` 断言放行
- [x] 3.4 fail-loud 测试：fixture 模拟 kubectl 非零退出（exit≠0 + 空 stdout）断言 `status=exception`（含 namespace 预检失败路径）；kubectl 成功且空集断言 `{"results":[]}` + `status=ok` 无 finding
- [x] 3.5 `test_docker_target_cohort_guard.py` 更新：`_EXCLUDE` roster +5 个 `k8s.*` 名字、计数断言 `len(_EXCLUDE)==42`、`len(all_names)==70`（INCLUDE 28 不动）；确认奇偶不变量与内容式 meta-guard 对新 manifest 通过（kubectl 命令不含 host 全局标记）
- [x] 3.6 manifest 声明形态契约测试（承接 k8s-inspector-suite 需求 1「manifest 声明形态」场景）：对 5 个 k8s manifest 以**原始 YAML 源**（`yaml.safe_load` 源文件，**非**加载后的 `InspectorManifest` 对象——schema 默认 `privilege="none"` 使对象级无法区分显式与默认）断言：`privilege` 键显式存在且为 `none`、`collect.timeout_seconds` 键显式存在（pods / nodes 类 30、events 类 60）、`targets` 恰为 `[local, ssh]`、`requires_binaries` 含 `kubectl` 与 `jq`、`collect.command` 中 kubectl 子命令仅为 `get`
- [x] 3.7 跑 1.2 列出的其余扇出点同步（如有）；console 形式 `pytest` 全量过（不要只跑子集）

## 4. 文档与 spec 同步

- [x] 4.1 TODO.md 覆盖矩阵 K8s 行：三处更名 `oom_history` → `oom_killed`、`pending` → `stuck_pending`、`pressure` → `conditions`，新增 `k8s.events.warnings` 第 5 格，行首注释去掉「待独立提案」、5 格打 ✅；TODO.md:432 叙述段「65 个」与 M6 剩余域清单同步（与 1.2 扇出清单对账）；6.x 任务清单加勾
- [x] 4.2 `docs/operations/inspectors.md` 新增「管理机」段：kubectl target 指向（local / 任意配 kubeconfig 的 ssh target）、RBAC 最小权限 `view` ClusterRole、events TTL 1h 对低频 schedule 的窗口盲区声明
- [x] 4.3 确认本 change 的两个 spec delta 与实现一致（k8s-inspector-suite 新增 / inspector-authoring-contract 修改）；在临时副本实测 `openspec-cn archive` rebuild 通过（MODIFIED 全文复制 + 中文标题已是既有踩坑点）

## 5. 验证与交付

- [x] 5.1 `pre-commit run --all-files` + `mypy --strict src/` + console `pytest` 全绿
- [x] 5.2 kind 真机 Demo Path 跑通（D-7：fixture 不验证 collector shell 正确性，真机兜底必须 **5/5 覆盖 + namespace 预检两路径**）：`kind create cluster` → 按 proposal Demo Path 用 `kubectl apply` heredoc 造真 OOMKilled pod（memory limit 16Mi + 超限分配；**exit 137 造不出 OOMKilled**）与 unschedulable pod（label key 显式加引号——防御性写法，避免 YAML 1.1 标量歧义，与 proposal Demo Path 注释一致）→ `sleep 90` 等事件积累 → 5 个 inspector 全部 `hostlens inspect` 跑一遍（oom_killed / stuck_pending（`--parameters` 调低阈值）/ events.warnings（`min_count: 2`）出预期 finding；evicted / nodes.conditions 在健康集群断言 `status=ok` 空 findings 作执行证据）→ namespace 预检两路径：`--parameters '{"namespace": "default"}'`（status=ok）与 `'{"namespace": "nosuchns"}'`（断言 status=exception 非空 ok）→ `kind delete cluster`；记录全部输出进 PR 描述
- [x] 5.3 commit（分支本地）→ 对抗性 review（Code Reviewer + Reality Checker 两方，Codex 用户指令跳过；APPROVE，RC 三项复核后全 verified-safe，每个在范围类目强对账锚）→ 无需修复
- [ ] 5.4 `\gh pr create --base main`，PR 描述含 spec 引用（`openspec/changes/add-k8s-control-plane-inspectors/`）+ Demo Path 全部输出 + 「fixture 不锁 collector 正确性、真机已 5/5 + namespace 预检两路径兜底」声明
- [ ] 5.5 CI 全绿后拉 Copilot / Cursor BugBot 评论逐条 triage，再 `\gh pr merge --squash --delete-branch`
- [ ] 5.6 归档：`openspec-cn archive`（delta 合入 `openspec/specs/`，change 目录 mv 到 archive；纯机械步骤经授权可 admin 直推）
