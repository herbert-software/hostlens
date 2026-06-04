## 新增需求

### 需求:`hostlens notify channels` 必须列出已配置通道且不外发

`hostlens notify channels [--json]` 必须读取 `notifiers.yaml`，列出每个通道实例的名称、类型与配置校验状态（`validate_config` 是否通过、引用的环境变量是否齐备），**禁止**发送任何消息。`--json` 必须输出机读结构供 Agent ping。secret 值禁止打印（仅显示是否已配置/已解析）。

#### 场景:列出通道不触发发送

- **当** 执行 `hostlens notify channels`
- **那么** 必须列出所有通道及其校验状态；不得有任何出站消息发送；secret 不以明文显示

#### 场景:notifiers.yaml 缺失或畸形时给出明确提示而非崩溃

- **当** `notifiers.yaml` 不存在 / 不可读 / 内容为非法 YAML（parse 失败），执行 `notify channels`
- **那么** 必须给出可读提示（无配置文件 / 路径不可读 / YAML 语法错并指明）并以非崩溃方式结束（空列表或明确错误），禁止抛未捕获异常栈

### 需求:`hostlens notify render` 必须 dry-run 渲染且默认不发送

`hostlens notify render --report <id> --channel <name>` 必须取 `reports/` 下已持久化的 Report，按目标通道渲染并把 payload 打印到 stdout，**默认且唯一行为是不发送**（dry-run）。这是无 token / 无网络的本地 Demo Path 主路径。可用选项展示 `only_if` 路由判定结果（发送/跳过及原因）。`--report` 指向不存在的 `report_id`、或指向 `report_storage="orphan"` 的报告（`get_run`/正常 store 取不到）时，必须以非零退出码 fail-loud 并给出可读原因（report 不存在 / 为 orphan 需走 orphan 路径），**禁止**渲染空壳或崩溃。`--channel` 指向 `notifiers.yaml` 未配置或类型未注册的通道时，同样必须以非零退出码 fail-loud 并指出未知 channel（与 report 侧失败对称），**禁止**静默无输出或崩溃。

#### 场景:渲染既有报告到 stdout 不外发

- **当** 对一个已存在的 `report_id` 与已配置通道执行 `notify render`
- **那么** 必须把渲染后的 channel-native payload 打印到 stdout；不得有任何出站请求

#### 场景:render 目标报告不存在则 fail-loud

- **当** 对一个不存在的 `report_id`（或 orphan-stored、正常 store 取不到）执行 `notify render`
- **那么** 必须以非零退出码终止并给出可读原因；禁止渲染空内容或抛未捕获异常

### 需求:`hostlens notify test` 为外发操作，非交互缺 `--yes` 必须退出 1

`hostlens notify test --channel <name>` 会向通道真实外发一条固定 ping 消息（不依赖任何已有 Report）。作为外发操作，无 TTY 的非交互环境缺 `--yes` 必须直接 `exit 1`（不得默默成功），有 TTY 时必须交互确认。

**EUID==0 拒绝豁免（有意决策，可审计）**：`notify test` **不触发** CLAUDE.md §4.5 / 全局 write-op 的 EUID==0 拒绝。依据：(1) 全局 EUID 规则的立意是「防 root 运行产生 root-owned 文件」，而 `notify test` 是纯出站 HTTPS 请求、**不在任何主机创建/修改文件**；(2) §4.5 列举的写操作是「Remediation / Notifier **配置修改** / target 凭据写入」——均改变受管基础设施状态，`notify test` 仅发一条消息、既不改远端被巡检主机状态也不改本地 notifier 配置，不属该集合。故按 root 运行 `notify test` 无 root-owned-file 风险、无安全降级，豁免成立。（与之相对：未来若新增「写 `notifiers.yaml` 配置」的 CLI 子命令，那才落入 §4.5、必须拒绝 root。）

#### 场景:非交互缺 --yes 退出 1

- **当** 无 TTY 环境执行 `hostlens notify test --channel x`（未带 `--yes`）
- **那么** 必须以退出码 1 终止，且不发送任何消息

### 需求:`doctor --check-channels` 必须探测通道连通性并进 `--json`

`hostlens doctor --check-channels` 必须对每个已配置通道做轻量连通性/配置探测（如 Telegram `getMe`、Lark 仅校验配置完整性不外发业务消息），结果纳入 `doctor --json` 的 `checks.channels`。探测失败（token 无效 / 变量缺失）必须标红但不影响其它 doctor 检查项。

#### 场景:无效通道配置被 doctor 标红

- **当** 某通道引用的环境变量缺失或 token 无效，执行 `doctor --check-channels`
- **那么** 该通道在 `checks.channels` 中标记为失败并附原因；其它检查项不受影响
