## 修改需求

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
