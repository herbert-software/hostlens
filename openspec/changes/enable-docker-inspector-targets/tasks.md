# Tasks：放开 Inspector 的 Docker target 支持

## 1. Literal 取值域放开

- [x] 1.1 `src/hostlens/inspectors/schema.py`：`targets` 字段 Literal `["local","ssh"]` → `["local","ssh","docker"]`（保留 `Field(min_length=1)`）
- [x] 1.2 `src/hostlens/targets/replay.py`：**两处** Literal 都改 `["local","ssh"]` → `["local","ssh","docker"]`——fixture 模型的 `impersonate` 字段（:87，默认仍 `"local"`）**与**实例属性 `self.type: Literal[...] = data.impersonate`（:141）；漏改 :141 则 `impersonate="docker"` mypy 报错
- [x] 1.3 确认 `mypy --strict` 全绿（两处 Literal 改动不引入新 Any / 不破坏既有 type narrowing）

## 2. Schema / preflight 门单元测试

- [x] 2.1 测试 manifest 加载：`targets: [docker]` / `[local, docker]` / `[local, ssh, docker]` 成功（对应 plugin-system spec §场景:targets 接受 docker）
- [x] 2.2 测试 manifest 加载：`targets: []` / `[kubernetes]` / `[k8s]` 仍 raise `ValidationError`（对应 §场景:targets 必须非空且仅含允许值）
- [x] 2.3 测试 ReplayTarget fixture：`impersonate: docker` 加载成功、`.type == "docker"`；`impersonate: k8s` raise（对应 replay-execution-target spec §场景:impersonate 取值域限定）
- [x] 2.4 测试 runner preflight：docker-typed target × 声明 `docker` 的 inspector → step 1 通过；docker-typed target × 仅 `[local,ssh]` 的 inspector → `requires_unmet(["target_type"])`
- [x] 2.5 测试 capability 兜底：要求 `systemd` capability 的 inspector × 无 systemctl 的 docker target → `requires_unmet`（验证误声明被兜底）

## 3. cohort manifest 机械追加 docker（逐项依 design Decision 4 全表）

- [x] 3.1 nginx：config_test / error_rate / health 追加 `docker`
- [x] 3.2 mysql：connection_usage / replication_lag / slow_queries 追加 `docker`
- [x] 3.3 postgres：bloat_tables / connection_usage / long_queries / replication_lag 追加 `docker`
- [x] 3.4 redis：memory_usage / persistence / replication_lag / slowlog 追加 `docker`
- [x] 3.5 jvm：gc / heap / threads；go：goroutines / heap 追加 `docker`
- [x] 3.6 linux.process：**仅** zombies / critical_alive 追加 `docker`（**不含** fd_usage / total——读 `/proc/sys/*` host 全局 sysctl，见 design Decision 4 关键注 + authoring-contract §读 /proc/sys 场景）
- [x] 3.7 log：**仅** exception_burst 追加 `docker`（**不含** tail.error_burst——`journalctl` 读 host journal）
- [x] 3.8 net：connections / listening_ports / dns.resolve / dependency.tcp_check / tls.cert_expiry / tls.chain_validity 追加 `docker`
- [x] 3.9 冻结 INCLUDE/EXCLUDE 双名单 meta-guard：遍历全部 builtin manifest，断言 INCLUDE 集（28 个，design Decision 4 全表）`targets` **含** `docker`、EXCLUDE 集（37 个）**不含** `docker`；两集计数硬断言（28 / 37 / 合计 65），防手 tally 漂移
- [x] 3.10 **内容式** meta-guard（对应 authoring-contract §场景:内容式 meta-guard）：遍历 builtin manifest，断言凡 `collect.command` 含 `/proc/sys/` / `/proc/meminfo` / `journalctl` / `/proc/loadavg` / `/proc/uptime` 的 manifest，其 `targets` **不含** `docker`（机械拦截误归因类，独立于人工名单）
- [x] 3.11 `openspec-cn`/loader 重新加载全部 builtin：65 个 manifest 全部加载成功、无 schema 错误

## 4. docker 派发端到端回放测试（每类一代表，对应 authoring-contract §场景:docker 派发路径必须有代表性回放验证）

- [x] 4.1 服务类代表（如 `redis.memory_usage`）：构造/复用 fixture 设 `impersonate: docker` → runner.run → `InspectorResult.status == "ok"` + snapshot
- [x] 4.2 运行时类代表（如 `jvm.heap` 或 `go.heap`）：同上 docker 回放
- [x] 4.3 进程级代表（`linux.process.zombies` 或 `critical_alive`——**不用** fd_usage/total，二者已 EXCLUDE）：同上 docker 回放
- [x] 4.4 网络类代表（如 `net.listening_ports`）：同上 docker 回放
- [x] 4.5 测试文件 import 用 `from inspectors.x`（避免 `tests.inspectors.x` 在 console pytest 崩；见 memory `project_test_sibling_helper_import_ci`）；snapshot 断言 `.rstrip("\n")` 容忍尾换行

## 5. 文档与收尾

- [x] 5.1 `docs/operations/inspectors.md`：新增「Docker target 上可跑的 inspector / 容器视角注意事项」一节（net.* 是容器 netns 视角；服务类需容器内有对应 client 二进制；EXCLUDE 类为何保持 local/ssh）
- [x] 5.2 跑 `mypy --strict` + 全量 pytest（py3.11 / py3.12 心理预演，本地至少 py3.12）全绿
- [x] 5.3 `openspec-cn validate enable-docker-inspector-targets --strict` 通过
- [x] 5.4 对本次变更跑对抗性 review（`/review-loop-codex`），triage + 修复到放行（CLAUDE.md §5.3：含 src/ 运行时行为变更 + 改公开 schema 契约，必须 review）
- [ ] 5.5 feature branch `feat/enable-docker-inspector-targets` → PR → CI 绿 + Copilot/BugBot triage → squash merge
