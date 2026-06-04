## 1. 依赖与配置

- [x] 1.1 `pyproject.toml` 显式加 `httpx>=0.27,<1`（取与 anthropic 兼容的交集区间）；确认 `simpleeval` / `jinja2` 已在
- [x] 1.2 `core/config.py` `Settings` 加 `notifiers_config_path: Path = ~/.config/hostlens/notifiers.yaml`

## 2. Notifier 核心抽象 (notifier-protocol)

- [x] 2.1 `notifiers/base.py`：`Notifier` Protocol（`name` / `validate_config` / `render` / `send`）+ `NotifyPayload` / `NotifyResult` Pydantic v2 模型
- [x] 2.2 `notifiers/base.py`：`ChannelTypeRegistry` + `register_default_notifiers(registry)` 显式装配（无 import 时副作用）
- [x] 2.3 `notifiers/base.py`：共享发送辅助——有界指数退避（默认 3 次、429 尊重 Retry-After 且受 60s 封顶、4xx 非429 立即 failed 不重试）、单通道 60s 硬超时、secret 打码工具（**同时用于日志与 `NotifyResult.error` 字段**，防 token 经异常 str 泄漏进 runs.db）
- [x] 2.4 `NotifyResult.detail` 值统一 `str()` 化（int message_id 等）；截断辅助须按通道格式边界对齐（不劈开转义序列）、计长用通道 API 口径
- [x] 2.5 单测：registry 解析/未注册 raise、import 无副作用、NotifyResult 枚举校验、超长截断为合法产物 `truncated=True`、失败返回 `failed` 不抛、error 字段不含明文 secret、4xx 非429 不重试

## 3. 通道配置加载 + only_if 路由 (notify-routing)

- [x] 3.1 `notifiers/config.py`：解析 `notifiers.yaml`、`${ENV_VAR}` 注入（未设引用变量 fail-loud 指名、字面/畸形 `$` 按字面保留）、`type` 解析到 registry（未注册 fail-loud）、构造通道实例、调 `validate_config`（字段存在**且非空**）
- [x] 3.2 `notifiers/routing.py`：报告聚合 severity 派生（用 rank 比较取 max，空→info）；tags 并集
- [x] 3.3 `notifiers/routing.py`：`only_if` 求值复用 `inspectors.dsl.evaluate`（结果 `bool()` 归一）+ severity rank 上下文（info/warning/critical）；加载期 `validate_ast` 校验（空串 fail-loud）；**运行期求值任何异常（catch `Exception`，含但不限于 TypeError/NameNotDefined/TimeoutError/simpleeval.InvalidExpression）捕获归 `NotifyResult(failed)`、不冒泡**
- [x] 3.4 单测：env 缺失报错 / 注入正确且不入日志；type 未注册 raise；字段空值 fail-loud；severity 阈值 + tag 成员路由真假；非法/空串 only_if 加载期 raise；运行期异常记 failed 不冒泡（覆盖**不在例举内**的类型如 `simpleeval.InvalidExpression`，验证 catch-all）

## 4. Telegram 适配器 (notifier-telegram)

- [x] 4.1 `notifiers/templates/telegram/report.md.j2` + `mdv2_escape` / `sev_icon` Jinja2 filter
- [x] 4.2 `notifiers/telegram.py`：`render`（MarkdownV2 + 脱敏 Report + 截断）；`validate_config`（require bot_token/chat_id）
- [x] 4.3 `notifiers/telegram.py`：`send` 经 `httpx.AsyncClient` POST `sendMessage`，token 打码，2xx 记 message_id
- [x] 4.4 单测（`httpx.MockTransport`）：保留字转义、缺字段 fail-loud、成功记 message_id、日志无明文 token

## 5. 飞书 Lark 适配器 (notifier-lark)

- [x] 5.1 `notifiers/templates/lark/report.card.j2`：interactive 卡片（按 severity 区分 header 色）
- [x] 5.2 `notifiers/lark.py`：`render`（卡片 JSON + 脱敏 + 截断）；`validate_config`（require webhook_url，secret 可选）
- [x] 5.3 `notifiers/lark.py`：`send` HMAC-SHA256 时间戳签名（`hmac`/`hashlib`/`base64`，无新依赖）；无 secret 不附 sign；secret/url/sign 打码
- [x] 5.4 单测（`httpx.MockTransport` + 固定向量）：卡片可 json.loads、签名逐字节核对、无 secret 不附 sign

## 6. Scheduler↔Notifier 接线 (scheduler-engine / schedule-manifest)

- [x] 6.1 `scheduler/store.py`：`Run.notify_results` 由 `list[object]` 收紧为 `list[NotifyResult]`；反序列化兼容 M4 空数组
- [x] 6.2 `scheduler/loader.py`：**加载期**仅校验 `only_if` 语法（`validate_ast`，不读 notifiers.yaml，保 `schedule list` 不依赖通道配置）；channel 存在性校验移到装配期
- [x] 6.3 `scheduler/runner.py`：job 体 `ReportStore.save` 后、构造终态 Run 前派发 notify（仅 ok/partial）；`asyncio.gather`（单通道异常不取消其它）+ `Semaphore(4)`；失败隔离含 only_if 求值/渲染/发送三环节、不冒泡、不改 RunStatus、结果写 `notify_results`
- [x] 6.4 `scheduler/runner.py`：`channel_registry` 经构造器注入（与 TargetRegistry 同列，daemon/run/trigger 共用装配）；**装配期**校验 manifest notify.channel 解析到已注册通道，未知 channel fail-loud
- [x] 6.5 集成测：有 Report 触发产生 sent+skipped 记录；通道 send/only_if 异常记 failed 且 RunStatus 不变、不冒泡；无 Report 状态不派发且 notify_results 为空；未知 channel 装配期 raise；`schedule list` 在无 notifiers.yaml 下正常；M4 空数组反序列化兼容

## 7. CLI (notify-cli-command)

- [x] 7.1 `cli/notify.py`：`notify channels [--json]`（列出 + 校验状态，secret 不打印）
- [x] 7.2 `cli/notify.py`：`notify render --report <id> --channel <name>`（dry-run 渲染到 stdout，展示路由判定，不外发；report 不存在/orphan 非零退出 fail-loud）；`notify channels` 遇 yaml 缺失给可读提示不崩溃
- [x] 7.3 `cli/notify.py`：`notify test --channel <name> [--yes]`（固定 ping，无 TTY 缺 --yes exit 1；不做 EUID==0 拒绝——见 spec 豁免说明）
- [x] 7.4 `cli/__init__.py` 注册 notify 子命令组
- [x] 7.5 `cli/doctor.py`：`--check-channels` 探测（Telegram getMe / Lark 仅校验配置），结果进 `doctor --json` `checks.channels`
- [x] 7.6 CLI 测试：channels 不外发/yaml 缺失不崩、render 不外发/缺失 report 非零退出、test 非交互缺 --yes exit 1、doctor 标红无效通道

## 8. 文档与收尾

- [x] 8.1 `docs/operations/notify.md`：通道配置、`notifiers.yaml` 示例、`only_if` 表达式、Demo Path
- [x] 8.2 更新 `docs/OPERABILITY.md`（notify 并发/超时/重试限额）与 CLAUDE.md §9 当前阶段（M5 落地）
- [x] 8.3 `mypy --strict` + `ruff` + 全量 pytest 绿；`hostlens notify render` 跑通 Demo Path
