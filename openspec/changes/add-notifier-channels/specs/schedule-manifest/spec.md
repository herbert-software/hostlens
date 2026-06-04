## 重命名需求

- FROM: ### 需求:`notify` 配置在 M4 为惰性占位
- TO: ### 需求:`notify` 配置在 M5 被消费用于路由发送

## 修改需求

### 需求:`notify` 配置在 M5 被消费用于路由发送

`ScheduleManifest.notify` 必须解析为类型化结构（每条含 `channel: str` 与可选 `only_if: str`），且 M5 的加载与调度路径**必须**消费它。**校验分两个时机，互不耦合**：

- **manifest 加载期**（`schedule list` / `run` / `daemon` / `trigger` 共同的纯加载路径，**不依赖 `notifiers.yaml`**）：每条 `only_if`（若提供）必须经 `hostlens.inspectors.dsl.validate_ast` 校验语法/AST，非法表达式 fail-loud；空串 `only_if` 同样 fail-loud。此阶段**不**校验 channel 是否存在（`schedule list` 不应被迫读 `notifiers.yaml`）。
- **调度装配期**（实际要派发 notify 的路径：`daemon` / `run` / `trigger` 注入 `channel_registry` + 加载 `notifiers.yaml` 时）：每个 `notify.channel` 必须能解析到一个已注册通道实例，否则 fail-loud（指出未知 channel，拼写错的 channel 名不得静默忽略）。runner 因此需注入通道配置依赖（`channel_registry` / 已加载通道集），与 `TargetRegistry` 注入同列。

调度触发产出 Report 后，runner 必须按 `only_if` 路由把（已脱敏的）报告发送到对应通道，并把每通道 `NotifyResult` 写入 `Run.notify_results`。secret 仍只经 `${ENV_VAR}` 注入、不入 manifest 明文。`NotifyConfig` 必须 `model_config = ConfigDict(extra="forbid")`（M5 收紧、替换 M4 的 `extra="allow"`）：notify 子字段出现未声明 key（如拼错 `only_iff`）必须 `ValidationError` fail-loud，与 manifest 其它模型（`ScheduleManifest` / `ReportConfig`）的 fail-loud 基调一致；M5 的合法字段恰为 `channel` + 可选 `only_if`，未来新增字段须经后续 OpenSpec 提案显式扩展。**已知可接受弱化**：M4 的 `NotifyConfig` 为 `extra="allow"`，故 M4 用户若在 `schedules/*.yaml` 的 `notify` 写过额外 key，M5 收紧后这些既有 manifest 会 `ValidationError`。此弱化可接受——M4 的 `notify` 是**显式声明「解析但不消费」的占位**（无任何行为依赖它），收紧成 `extra="forbid"` 正是要让这类多余/拼写 key fail-loud（错误信息会指出未知 key，用户删除即可）；不提供静默兼容（静默吞 key 与 fail-loud 基调矛盾），不写迁移脚本（占位字段无语义可迁移）。区别于 scheduler-engine 的 F15（那是 runs.db `notify_results` 反序列化，作用对象是 store；此处作用对象是用户手写 manifest）。

#### 场景:带 notify 的 manifest 触发后产生 notify_results

- **当** manifest 含 `notify: [{channel: ops-telegram, only_if: "severity >= warning"}]`，`ops-telegram` 已在 `notifiers.yaml` 配置，执行加载与一次产出 Report 的调度触发
- **那么** manifest 必须正常加载；`only_if` 求值为真时该通道实际发送且 `Run.notify_results` 含对应 `NotifyResult(status="sent")`；求值为假时记 `NotifyResult(status="skipped")`

#### 场景:引用未配置通道在装配期 fail-loud

- **当** manifest 的 `notify` 引用 `notifiers.yaml` 中不存在的 channel 名，执行 `daemon` / `run` / `trigger`（注入 channel_registry 的装配路径）
- **那么** 装配期必须 raise（指出未知 channel），禁止静默跳过该通道

#### 场景:schedule list 不因 notify 引用而要求 notifiers.yaml

- **当** manifest 含 `notify`、`notifiers.yaml` 不存在，执行 `hostlens schedule list`
- **那么** 列表必须正常加载（仅做 `only_if` 语法校验），**不**因 channel 未配置或缺 `notifiers.yaml` 而失败

#### 场景:非法 only_if 在加载期拒绝

- **当** manifest 的 `only_if` 含被 AST 闸门拒绝的构造，或为空串
- **那么** manifest 加载期必须 raise，禁止留到运行期
