# llm-cassette-testing 规范

## 目的
待定 - 由归档变更 add-llm-cassette-testing 创建。归档后请更新目的。
## 需求
### 需求:`HOSTLENS_LLM_MODE` 必须只在测试 fixture 内分派 backend，禁止接入生产 `create_backend`

测试侧的 cassette 行为由环境变量 `HOSTLENS_LLM_MODE` 控制，取值 `replay` / `record` / `live`，缺省（未设置或空）必须等同 `replay`。该 mode 的解析与 backend 构造**必须**只发生在 `llm_cassette()` pytest fixture 内部；生产工厂 `create_backend()`（按 `Settings.backend.type` 分派）**禁止**读取或感知 `HOSTLENS_LLM_MODE`，以保证只有一个生产 backend 来源、不出现 env mode 与 `backend.type` 冲突。

#### 场景:缺省 mode 等同 replay
- **当** `HOSTLENS_LLM_MODE` 未设置，调用 `llm_cassette("x")`
- **那么** 返回的 backend 必须是 `PlaybackBackend` 实例（replay 路径）

#### 场景:非法 mode 值 fail-fast
- **当** `HOSTLENS_LLM_MODE="bogus"`，调用 `llm_cassette("x")`
- **那么** 必须 `pytest.fail` / raise，错误信息含合法取值 `replay|record|live`，**禁止**静默回落到任一默认 backend

#### 场景:生产工厂不感知 mode
- **当** 设置 `HOSTLENS_LLM_MODE="record"` 后调用生产 `create_backend(settings)`（`settings.backend.type` 任意合法值）
- **那么** `create_backend` 的分派结果只取决于 `settings.backend.type`，与 `HOSTLENS_LLM_MODE` 无关（mode 不改变生产 backend 选择）

### 需求:`llm_cassette(name)` fixture 必须按显式名映射 cassette 且按 mode 选 backend

测试通过 `llm_cassette(name)` 取得一个已按当前 mode 选好的 `LLMBackend` 实例。`name` 是语义化稳定标识，映射到 `tests/fixtures/cassettes/<name>.jsonl`。**禁止**按 pytest nodeid / 测试函数名自动派生 cassette 路径（防止 rename / 拆分 / parametrize 造成 cassette 路径无意义漂移，并让 review 能从显式名看出测试绑定的语义场景）。

#### 场景:replay 模式返回 PlaybackBackend
- **当** mode=replay，`tests/fixtures/cassettes/foo.jsonl` 存在，调用 `llm_cassette("foo")`
- **那么** 必须返回 `PlaybackBackend(cassette_path=<.../foo.jsonl>)`

#### 场景:replay 模式 cassette 文件缺失清晰报错
- **当** mode=replay，`tests/fixtures/cassettes/missing.jsonl` 不存在，调用 `llm_cassette("missing")`
- **那么** 必须 raise / `pytest.fail`，错误信息含期望路径 `tests/fixtures/cassettes/missing.jsonl`

#### 场景:record 模式缺 API key fail-fast
- **当** mode=record 且环境无 `ANTHROPIC_API_KEY`，调用 `llm_cassette("foo")`
- **那么** 必须 `pytest.fail` / raise，错误信息指出 record 模式需要 `ANTHROPIC_API_KEY`，**禁止**返回一个会在首次调用时才 401 的 backend

#### 场景:live 模式返回真实 backend 且不写盘
- **当** mode=live 且有 `ANTHROPIC_API_KEY`，调用 `llm_cassette("foo")`
- **那么** 必须返回 `AnthropicAPIBackend`（或其等价不写盘包装），且该路径**禁止**写任何 cassette 文件

### 需求:`RecordingBackend` 必须内存收集整个 scenario 并原子 overwrite 写盘，禁止 append

record 模式的 `RecordingBackend` 包一层真实 `AnthropicAPIBackend`，拦截 `messages_create` 的入参与返回。一个 cassette 文件对应一个 scenario（可含多轮、多条 record）。`RecordingBackend` **必须**在内存中按调用顺序收集该 scenario 的全部 `(request, response)`，并在 scenario 结束时**原子 overwrite 整个 cassette 文件**（写临时文件后 rename）。**禁止** append 到已有文件（append 会让同 key 重复行堆积，导致 `PlaybackBackend` 静默取第一个、新录的 record 失效）。

写出的每条 record 必须是 M2.1 已规约的 `{request, response, tools_schema_hash}` 格式，其中：
- `request` 是投影后的 canonical 子集 `{model, messages, tools_count}`，**必须**用与 `PlaybackBackend` 同源的 keying helper（见 §需求:request-key 算法必须单一来源）产出，保证与回放端字节一致。
- `tools_schema_hash` 在 recorder 产物中**必须存在**（不再是可选）：值为 `SHA256(json.dumps(tools, sort_keys=True))`（与 `llm-backend-protocol` spec §schema-drift 既有约定一致：默认 `ensure_ascii`，使 recorder 写入的 hash 与 CI `--current-tools-hash` 计算口径一致，否则非 ASCII tool schema 会误报 drift），让新录 cassette 自带 schema-drift 检测能力（M2.1 把它定为可选只为兼容手写旧 cassette；recorder 没有这个豁免）。

#### 场景:多轮 scenario 写出多条 record
- **当** 一个 record 模式测试在单 scenario 内触发 3 次 `messages_create`（多轮 Agent loop）
- **那么** 写出的 cassette 文件必须恰好含 3 条 record，每条 `request` 是 `{model, messages, tools_count}` canonical 子集，3 条的 request-key 互不相同（messages 逐轮增长）

#### 场景:recorder 产物必带 tools_schema_hash
- **当** record 模式写出任一 record
- **那么** 该 record 必须含非空 `tools_schema_hash` 字段，值等于对当次 `tools` 计算的 `SHA256(json.dumps(tools, sort_keys=True))`（与 `llm-backend-protocol` spec §schema-drift 既有约定一致：默认 `ensure_ascii`，使 recorder 写入的 hash 与 CI `--current-tools-hash` 计算口径一致，否则非 ASCII tool schema 会误报 drift）

#### 场景:重录覆盖整个文件
- **当** `tests/fixtures/cassettes/foo.jsonl` 已存在旧内容，对同 scenario 重新 record
- **那么** 写盘后该文件只含本次录制的 record，旧内容被整体覆盖（不是 append）

#### 场景:录制中断不留半写文件
- **当** record 写盘过程被中断（异常）
- **那么** 目标 cassette 文件必须保持原内容或不存在，**禁止**出现半写的损坏 JSONL（原子 rename 保证）

### 需求:任一检测门禁命中或调用异常后 recorder 必须进入 poisoned 状态，teardown 不得写出部分 cassette

scenario 多轮中，前几轮可能已累积干净 record，后一轮 request/response 检测门禁命中而 raise。此时 fixture teardown 仍会调用 `flush()`——若 `flush` 无条件写盘，会把「半个 scenario」（缺命中轮及其后续）写成 cassette，既违反「命中即**不写盘**」，又产出一个语义残缺、replay 必然 miss 的 cassette。本提案**必须**：(a) `RecordingBackend` 一旦某次 `messages_create` 因检测门禁命中或任何异常而 raise，即进入 **poisoned** 状态；(b) poisoned 状态下 `flush()` **必须**为 no-op（只从 active-path 注册表注销、不写盘）；(c) `flush()` / 资源释放**必须**幂等（teardown 重复调用安全）；(d) 若构造失败发生在注册之后，**必须**从注册表 rollback 不残留占用——当前实现**应**把注册作为 `__init__` 末步（注册后无可失败操作）以从结构上规避该路径。

#### 场景:门禁命中后 teardown 不写盘
- **当** 某 record scenario 第 2 轮检测门禁命中 raise，随后 fixture teardown 调用 `flush()`
- **那么** 目标 cassette 文件**不**被写入（保持原内容或不存在），且该 `cassette_path` 从 active-path 注册表释放

#### 场景:flush 幂等
- **当** 对同一 `RecordingBackend` 连续调用 `flush()` 两次
- **那么** 第二次为 no-op，不抛错、不重复写盘、不重复操作注册表

### 需求:`RecordingBackend` 写盘前必须对 request 与 response 都跑敏感检测门禁，命中即 fail 不落盘

合成 fixture 仍可能让 `tool_result` 带入 tmp 路径 / 当前用户名 / hostname / 时间戳 / 随机 id 等非预期信息；且 `tool_result` 会在下一轮进入 request `messages` 并被写入 cassette 的 request 字段——所以**只检测 response 不够**。`RecordingBackend` 在把每条 record 写盘前**必须**对 **canonical request 序列化文本**与 **response 序列化文本**都跑 `hostlens.core.redact.detect_sensitive_text`；命中任一 `CASSETTE_SENSITIVE_PATTERNS` 时**必须** raise 且**不写盘**，错误信息含命中的规则名但**禁止**回显命中的敏感原值。

这是**检测门禁**（detect-and-reject），**不是**静默脱敏：命中即 fail，**禁止**对 request 或 response 做任何 in-place scrub（scrub request 会改 keying、scrub response 会篡改真实 API 响应让 cassette 失真）。request 侧 cassette-safe 由合成输入治理 + 本门禁共同保证，keying 契约不变。

#### 场景:response 含敏感子串拒绝落盘
- **当** record 模式下某次 response 文本含 `/Users/alice/...`（命中 `user_home_path` 规则）
- **那么** `RecordingBackend` 必须 raise，错误信息含规则名 `user_home_path`，cassette 文件**不**被写入该 record，且错误信息不含 `alice` 子串

#### 场景:request 的 tool_result 含敏感子串拒绝落盘
- **当** record 模式下某轮 request `messages` 含上一轮 `tool_result` 嵌入的 `/Users/alice/...`（命中 `user_home_path`）
- **那么** `RecordingBackend` 必须 raise 且**不写盘**，错误信息含规则名但不含 `alice` 子串（证明 request 侧也过门禁）

#### 场景:干净 record 正常写盘
- **当** record 模式下 request 与 response 文本都不命中任何 `CASSETTE_SENSITIVE_PATTERNS`
- **那么** 该 record 正常写入 cassette 文件

### 需求:合成 fixture 必须字节稳定，record→replay 往返不得 miss

由于 request-key 把 `messages` 进 hash（含 `tool_result` 内容），合成输入若含非确定值（当前时间戳 / 随机 UUID / 当前用户名 / 临时路径），会让 record 端写入的 messages 与下次 replay 端活请求的 messages 字节不一致 → `CassetteMiss`。因此 cassette 测试用的合成 target / 合成 inspector 输出**必须**字节稳定：时钟、UUID、用户名、路径等**必须**冻结为固定值（不读真实环境）。

#### 场景:record 后立即 replay 同 scenario 不 miss
- **当** 用 record 模式录一个合成多轮 scenario 写盘，随后在 replay 模式用同一组合成输入跑同一 scenario
- **那么** 每轮 request-key 必须命中刚录的 record，**禁止**出现 `CassetteMiss`（证明合成输入字节稳定、keying 往返一致）

### 需求:record 模式必须防止同一 cassette 路径被并发/多实例覆盖

「一文件一 scenario + teardown overwrite」在同一 pytest run 内若有多个 recorder 指向同一 `cassette_path`（`parametrize` 未把参数并入 name、rerun、`pytest-xdist` 并发），各自内存收集后 teardown 各写一次，最后写入者会吞掉其他实例的数据，且静默无错。本提案**必须**：(a) record 模式维护一个进程内 active `cassette_path` 注册表，同一 path 出现第二个 active recorder 时**必须** fail-fast（不静默覆盖）；(b) 检测到 `pytest-xdist`（环境含 `PYTEST_XDIST_WORKER`）时 record 模式**必须** fail（跨进程无法共享该注册表，无法保证不互相覆盖）；(c) 文档**必须**要求参数化测试把参数并入显式 cassette name（每个参数一份 cassette）。

#### 场景:同 path 第二个 recorder fail-fast
- **当** record 模式下两个 recorder 实例在同一 run 内指向同一 `cassette_path`
- **那么** 第二个实例构造 / 启用时必须 raise，错误信息指出该 cassette 已被另一 recorder 占用

#### 场景:xdist 下禁止 record
- **当** record 模式运行且环境含 `PYTEST_XDIST_WORKER`
- **那么** 必须 `pytest.fail` / raise，错误信息说明 record 模式不支持 xdist 并发

### 需求:record 模式必须由 fixture 强制在装配层拒绝真实 target，禁止依赖测试作者自觉调用

`LLMBackend.messages_create` 的签名只有 `model/system/messages/tools/max_tokens/timeout`，**不含 target / ToolContext**——所以「拒绝真实 target」**不能**由 `RecordingBackend` 在 API 调用层完成（它根本看不到 target）。守门**必须**放在能看到 `target_registry` 的装配层，且**必须由 `llm_cassette` fixture 结构性强制**，**禁止**降级成「提供 helper、由测试作者自觉调用」（自觉调用会被遗忘，守门形同虚设）：

- 本提案**必须**提供 helper `guard_record_targets(target_registry, *, allow_real: bool) -> None`：对 registry 内每个 target 判定真实/合成——`type ∈ {ssh, docker, k8s}` **一律视为真实**；`type == local` 仅当其 `TargetEntry.tags` 含固定标记 `"cassette-synthetic"` 时视为合成，否则（裸 local 指向真实本机）**视为真实**。存在任一真实 target 且 `allow_real is False`（环境未设 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1`）时**必须** raise。**合成标记形态钉死为** `TargetEntry.tags` 含字符串 `"cassette-synthetic"`（复用既有 `TargetEntry.tags` 字段，不新增 target 属性），避免 apply 时临场选型导致 guard 规则漂移。
- record 模式下 `llm_cassette` fixture **必须**要求调用方提供其将使用的 `target_registry`（如 `llm_cassette(name, target_registry=...)`），并在**返回 `RecordingBackend` 之前**内部调用 `guard_record_targets`——即「拿到 record backend」这一步本身就强制过了守门，测试无法绕过。record 模式下未提供 `target_registry` 时 fixture **必须** fail（而非静默放行无守门的录制）。
- 该开关默认关闭，文档必须标注其风险（真实 hostname / IP / 路径可能入 cassette）。

#### 场景:默认拒绝真实 target
- **当** 在 record 模式下对含一个 SSH target 的 `target_registry` 调用 `guard_record_targets(registry, allow_real=False)`
- **那么** 必须 raise，错误信息指出需显式 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1`，且不回显 target 的 host / 凭据

#### 场景:带 cassette-synthetic 标记的 local 放行
- **当** 在 record 模式下对只含 local target、其 `TargetEntry.tags` 含 `"cassette-synthetic"` 的 `target_registry` 调用 `guard_record_targets(registry, allow_real=False)`
- **那么** 不 raise（放行录制）

#### 场景:裸 local（无标记）被拒
- **当** 在 record 模式下对含一个 local target、其 `TargetEntry.tags` **不含** `"cassette-synthetic"` 的 `target_registry` 调用 `guard_record_targets(registry, allow_real=False)`
- **那么** 必须 raise（裸 local 指向真实本机，视为真实 target）

#### 场景:fixture 强制守门，无法绕过
- **当** record 模式下经 `llm_cassette(name, target_registry=<含 SSH 的 registry>)` 取 backend（未设 `HOSTLENS_ALLOW_REAL_TARGET_RECORD`）
- **那么** fixture 必须在返回 backend 前 raise（守门由 fixture 触发，不依赖测试体内显式调用）

#### 场景:record 模式缺 target_registry 即 fail
- **当** record 模式下调用 `llm_cassette(name)` 未提供 `target_registry`
- **那么** fixture 必须 fail，**禁止**返回一个未经守门的 `RecordingBackend`

#### 场景:显式开关放行真实 target
- **当** `guard_record_targets(registry_with_ssh, allow_real=True)`（对应 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1`）
- **那么** 不 raise，但 `RecordingBackend` 写盘前仍必须过 request+response 敏感检测门禁（不豁免）

### 需求:`hostlens.core.redact` 必须暴露 cassette 共享敏感规则，且不改 `redact_text` runtime 语义

cassette 提交门禁的敏感标准比 runtime 日志脱敏更宽。`hostlens.core.redact` **必须**导出 `CASSETTE_SENSITIVE_PATTERNS`（含 sk- key / Bearer / JWT / credential 赋值 / `/Users|/home` 路径 / `.ssh` / IPv4 / email / hostname-FQDN 等规则）与 `detect_sensitive_text(text: str) -> str | None`（命中返回首个匹配的规则名，否则 `None`）。`cassette_lint.py` 与 `RecordingBackend` **必须**同源 import 这套规则，保证「录完即过 lint」。本需求**禁止**扩大 `redact_text()` 的既有 runtime masking 语义（runtime 允许保留 HOME / 路径等非 secret 信息）。

#### 场景:detect_sensitive_text 命中返回规则名
- **当** 调用 `detect_sensitive_text("token=Bearer xyz123")`
- **那么** 返回非 None 的规则名字符串（如 `bearer_token` 或 `credential_assignment`）

#### 场景:detect_sensitive_text 干净文本返回 None
- **当** 调用 `detect_sensitive_text("hello world, connection refused")`
- **那么** 返回 `None`

#### 场景:redact_text runtime 语义不变
- **当** 对一个仅含 `/Users/alice` 路径（runtime 视为非 secret 可保留）的字符串调用既有 `redact_text`
- **那么** 其行为与本提案前完全一致（本提案不改 `redact_text`），路径处理不被 cassette 规则收紧

#### 场景:lint 与 recorder 同源
- **当** `cassette_lint.py` 与 `RecordingBackend` 各自判定某文本是否敏感
- **那么** 两者必须基于同一份 `CASSETTE_SENSITIVE_PATTERNS`，对同一输入给出一致判定（录完的 cassette 必过 lint secret-scan）

### 需求:request-key 算法必须单一来源，禁止在多处复制

request-key 算法抽取在单一无副作用 helper `hostlens.agent.cassette_key.request_key_for_payload(model, messages, tools_count) -> str`，由 `PlaybackBackend` / `RecordingBackend` / `cassette_lint.py` **共用**。recorder（写 canonical request）、lint（重复 key 检测）、playback（回放匹配）三处若各自复制算法，任一处对序列化参数 / 投影细节漂移就会造成「lint 认为无重复但 playback 实际冲突」或反向误报、或「recorder 写的 key 与 playback 读的不一致」，故必须共用同一 helper（helper 签名不变）。

thinking 归一化投影本身**必须**抽取成 `cassette_key.py` 内一个独立可复用的纯函数（如 `project_messages_drop_thinking(messages) -> list[dict]`，签名与命名由实现定，但**必须**是「输入 messages、输出丢弃整个 thinking/redacted 块后的新 messages 列表」的无副作用投影），**禁止**在 `request_key_for_payload` 与 `RecordingBackend` 两处各写一份 drop 逻辑（否则两处投影规则漂移会让落盘 body 与 keying 投影不一致）。`request_key_for_payload` 在 hash 前**必须**调用该投影函数：**丢弃整个 `type="thinking"` 与 `type="redacted_thinking"` 内容块**（不是只丢 `thinking` / `signature` 字段——这两块 `extra="allow"`，残留任何字段都会让 hash 不稳）再序列化求 hash。理由:thinking 块的 `thinking` 文本与 `signature` 都是 provider 每次非确定生成的，多轮回传后会进 `messages`，若不归一则同一逻辑请求每次 record 的 key 都不同 → record→replay 永不命中。归一只发生在 **keying 投影**这一步,**不**改变 Agent loop 实际发往 provider 的 `messages`(loop 仍 verbatim 回传完整 thinking 块)。

golden 等价契约相应限定为:对**不含 thinking/redacted 块**的 payload，helper 的 hash 必须与重构前 `PlaybackBackend._request_key` 既有算法**逐字节相等**（归一对 thinking-free messages 是恒等投影，既有 golden 不变）;对**含 thinking 块**的 payload，hash 必须等于「先 drop thinking/redacted 块后的等价 thinking-free payload」的 hash。

**key 匹配的正确性仅由该 helper 保证**:`PlaybackBackend._request_key`（回放查找）、`PlaybackBackend._record_request_key`（读取落盘 request 重算索引 key）、`cassette_lint`（重复检测）三处都委托 `request_key_for_payload`，而 `_record_request_key` 在读取落盘 request 时**重新**应用归一投影——因此**即便落盘 request body 仍保留 thinking 块，live-key 与 replay-key 也恒相等**，匹配不依赖落盘 body 是否已 strip。

`RecordingBackend` 落盘 canonical request 时**应**调用**同一个** `project_messages_drop_thinking` 投影函数（不得自写第二份 drop 逻辑），但此举属**卫生 + 安全**目的（不把非确定 chain-of-thought 持久化进 cassette 文件造成噪声/排障困惑、不让落盘前的敏感检测门禁扫描 thinking 文本），**非 key 匹配所需**（匹配已由上一段的共享 helper 保证）——`RecordingBackend` 位于 `tests/support/cassette_recording.py`（test-support，非生产 `src/` backend）。cassette 存储的 **response 必须保留完整原始内容（含 thinking / redacted_thinking 块）**，供 replay 时 Agent loop 按序 verbatim 回传——keying 与 request 持久化归一 thinking，response 不归一，二者**禁止**混淆。

#### 场景:三处 keying 同源

- **当** 对同一 `(model, messages, tools_count)` payload 分别经 `PlaybackBackend` 查找路径、`RecordingBackend` 写入路径、`cassette_lint` 重复检测路径计算 request-key
- **那么** 三者必须得到完全相同的 hex（均出自 `request_key_for_payload`）

#### 场景:thinking-free payload 重构不改 playback hash（golden）

- **当** 对一组**不含 thinking 块**的固定 payload 用 `request_key_for_payload` 计算 hash
- **那么** 结果必须等于重构前 `PlaybackBackend` 既有算法对同一 payload 的 hash（golden 值），证明归一化对 thinking-free 输入是恒等、行为等价

#### 场景:含 thinking 块的多轮 messages keying 稳定

- **当** 同一逻辑请求的 `messages` 含被回传的 thinking 块，其 `thinking` 文本 / `signature` 在两次 record 间不同（provider 非确定生成），分别计算 request-key
- **那么** 两次 hash 必须相同（thinking/redacted 块在投影时被整块丢弃后才 hash），record→replay 不因 thinking 非确定而 miss

#### 场景:keying 投影丢整块而非丢字段

- **当** thinking 块携带 `extra="allow"` 的 provider 私有额外字段，计算 request-key
- **那么** 投影必须丢弃整个 thinking 块（含所有额外字段），hash 不受任何 thinking 块字段影响

#### 场景:response 保留 thinking 供回放

- **当** record 一段含 thinking 块的多轮 scenario
- **那么** cassette 落盘的 response 必须含完整 thinking / redacted_thinking 块（replay 时供 Agent loop verbatim 回传）；落盘的 request 则已 thinking-归一

#### 场景:key 匹配不依赖落盘 request 是否 strip thinking

- **当** 一条落盘 cassette record 的 `request.messages` **仍保留** thinking 块（即落盘归一被跳过或回滚），对其调用 `_record_request_key`（读取重算）；并对等价的、含相同 thinking 的 live 请求调用 `request_key_for_payload`
- **那么** 两者必须得到相同 hex（`_record_request_key` 读取时重新归一），证明 record→replay 匹配由共享 helper 保证、**与落盘 body 是否 strip 无关**；落盘 strip 仅服务卫生/安全

### 需求:`cassette_lint.py` secret-scan 必须检测同文件内重复 request-key

`RecordingBackend` 因 overwrite 不产生重复 key，但手写 / 误编辑的 cassette 可能在同文件内出现重复 request-key，导致 `PlaybackBackend` 静默取第一个、其余轮被吞。`cassette_lint.py` 的 secret-scan 模式**必须**附带检测：同一 cassette 文件内若出现重复 request-key（用 §需求:request-key 算法必须单一来源 抽出的 `request_key_for_payload` 计算）则 exit 1 并在 stderr 指出冲突文件与 key 前缀。

#### 场景:重复 key 被 lint 拒绝
- **当** 某 cassette 文件含两条 request-key 相同的 record，运行 `python scripts/cassette_lint.py`
- **那么** 必须 exit 1，stderr 指出该文件存在重复 request-key

#### 场景:多轮不同 key 正常通过
- **当** 某 cassette 文件含多条 request-key 互不相同的 record（正常多轮 scenario）
- **那么** lint 不因重复 key 报错（仍受 secret-scan / schema 校验约束）

### 需求:CI 默认必须以 replay 模式运行且零 API 消耗

CI 跑测试套件时 `HOSTLENS_LLM_MODE` **必须**缺省为 replay：所有经 `llm_cassette()` 的 LLM 测试走 `PlaybackBackend`，miss → `CassetteMiss` fail，**禁止**回落真实 API；CI **禁止**依赖 `ANTHROPIC_API_KEY` 跑默认测试路径。CI 另需在 lint 阶段对全部 cassette 跑 `cassette_lint.py` secret-scan。

#### 场景:CI 默认路径不读 API key
- **当** CI 在无 `ANTHROPIC_API_KEY` 环境下运行默认测试套件
- **那么** 经 `llm_cassette()` 的测试全部走 replay 并通过（cassette 齐备时），无任何真实 API 调用

#### 场景:cassette 缺失在 CI 暴露
- **当** 某测试的 cassette 在 replay 下 key miss
- **那么** 必须 `CassetteMiss` fail（红），**禁止**因此回落真实 API 静默通过
