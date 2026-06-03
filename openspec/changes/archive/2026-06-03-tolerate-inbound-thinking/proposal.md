## 为什么

DeepSeek v4（经 anthropic 兼容端点 `https://api.deepseek.com/anthropic`，`deepseek-v4-pro` / `deepseek-v4-flash`）**默认强制返回 `type="thinking"` 内容块**。Hostlens 的 `MessageResponse.content` 判别联合只建模 `text` / `tool_use`（`src/hostlens/agent/backend.py`），于是任何含 thinking 块的响应在 `MessageResponse.model_validate(...)` 抛 `union_tag_invalid` → 当前由 `add-backend-disable-thinking` 归一成 `BackendError(kind="unsupported_content_block")`。这意味着接此类端点**只能靠 `disable_thinking=True` 抑制**——一条「靠 provider 守约关闭」的脆路径：provider 任何版本改动、漏配开关、或部分模型不认 disabled，都会让整链崩在响应解析。

本提案换一条**健壮**路径:**建模并容忍 inbound thinking 块**，使 thinking 强制开的端点即便不抑制也能正常跑（含多轮工具循环），不再依赖「关得掉」这个假设。这是 TODO M3.6 `support-extended-thinking` 的 **Path 1（tolerate-inbound）切片**:只「认得吐回来的 thinking 并按序回传」，**不**主动请求 thinking、**不**消费 thinking 渲进报告（那是 Path 2，留作未来独立提案）。

实测已全程验证（2026-06-04，`tests/manual/deepseek_thinking_probe.py` + `deepseek_multiturn_probe.py`，pro/flash 各一次）:thinking 块 schema 精确为 `{type, thinking, signature}`（与 Anthropic 原生 ThinkingBlock 同形，`signature` 两模型都有，值恰为 message `id`）；多轮 + 工具下 turn1 `[thinking, tool_use]` → 原样回传 thinking 块 → turn2 `[thinking, text]` **零 400**，证实 DeepSeek 不验签、relay 安全。详见 design.md「验证数据」。

## 变更内容

- `MessageResponse.content` 的 `ContentBlock` 判别联合**新增** `ThinkingBlock{type, thinking, signature}` + `RedactedThinkingBlock{type, data}`，二者 `model_config = ConfigDict(extra="allow")`（区别于 `TextBlock` / `ToolUseBlock` 的 `extra="ignore"`）——因为 thinking 块是 verbatim relay，`extra="allow"` 保住未来 provider 私有字段不被 `model_dump()` 丢弃。
- **不新增** `BackendCapabilities` 字段。容忍 inbound thinking 的实现是「无条件扩 union」——任何 backend 收到 thinking 都能解析、Agent loop 无条件 verbatim 回传 assistant content，**没有任何代码 branch 在「是否容忍 thinking」上**。按 CLAUDE.md §4.11「字段只放 Agent loop 真正会 branch 的能力」，新增 flag 是死元数据，故 7 字段维持不变。
- `AnthropicAPIBackend._classify_validation_error` 的 `unsupported_content_block` 路径:thinking 块现在能成功 parse，不再触发该分支 → 把它的语义**收窄成「真正未知/未建模的 block type」**（仍兜 SDK 未来新增的其它 block），并更新依赖它对 thinking 触发的单元测试。
- cassette keying（`cassette_key.py` `request_key_for_payload`）:投影 `messages` 时**丢弃整个 thinking / redacted_thinking 块**再 hash（thinking 文本 + signature 非确定，不归一则 record→replay 永不命中）。**key 匹配的正确性仅由该 helper 保证**——`PlaybackBackend` 查找、`_record_request_key`（读盘重算）、`cassette_lint` 三处都委托同一 helper，且 `_record_request_key` 在读取落盘 request 时**重新**归一，故即便落盘 body 仍含 thinking，live-key 与 replay-key 也恒相等。`RecordingBackend`（`tests/support/cassette_recording.py`）落盘 canonical request 应用同一投影属**卫生 + 安全**考虑（不把 chain-of-thought 持久化进 cassette、不让敏感门禁扫描 thinking 文本），**非** key 匹配所需；cassette **仍存完整响应**（含 thinking 供回放回传）。golden hash 对 thinking-free payload **保持不变**（归一化对其是恒等投影，钉死「未误改既有 keying」），另加「含 thinking payload 的 hash == 其 thinking-stripped 等价物」的新 golden 用例。
- Agent loop 多轮回传:**已天然成立**（`loop.py` verbatim `model_dump` assistant content；cache_control 断点恒落在末尾 user tool_result、永不落 thinking 块——见 design.md D-3），本提案只**加结构回归测试**钉死该不变量，不改 relay 逻辑。
- `disable_thinking` **定位降级**:从「兼容必需」改述为「**可选的 token 节省优化**」（开启抑制 thinking 输出省 token；关闭也不再崩，由本提案兜住）。Fake / Playback backend **不**实现 thinking 注入。

非破坏性:真 Anthropic 默认不吐 thinking，本提案对其零行为变更;`disable_thinking` 行为不变;union 扩容只是把「以前非法的 thinking 响应」变合法，不影响既有 text/tool_use 解析。

## 功能 (Capabilities)

### 新增功能

（无 —— 本提案只修改既有 capability 的需求，不引入新 capability）

### 修改功能

- `llm-backend-protocol`: 四个需求 MODIFIED——(1) `MessageResponse` 的 `ContentBlock` 判别联合扩容 `ThinkingBlock` + `RedactedThinkingBlock`（`extra="allow"`），使含 thinking 的响应可成功解析并 verbatim 回传;(2) 响应解析错误分类中 `unsupported_content_block` 的触发语义收窄成「真正未知 block type」(不再对 thinking 触发);(3) `BackendCapabilities` 需求:字段集**显式不变**(7 字段，无 tolerate flag),但其 `extended_thinking` 字段说明中「`ContentBlock` union 不含 `ThinkingBlock`」的旧理由**必须**更新(union 现已含 thinking;容忍≠主动请求,故字段仍 False);(4) `disable_thinking` 需求:从「兼容必需」重述为「可选 token 优化」(含一条「不抑制时 inbound thinking 被容忍」新场景),避免归档后与扩容后的 union 需求自相矛盾。
- `llm-cassette-testing`: cassette 请求 keying 在投影 `messages` 时归一化丢弃整个 thinking / redacted_thinking 块再 hash（key 匹配正确性**仅**由此共享 helper 保证）;`RecordingBackend`（`tests/support/cassette_recording.py`）落盘 canonical request 同步归一属**卫生/安全**（非 key 匹配所需）;cassette 仍存完整响应。

## 影响

- Affected specs: `llm-backend-protocol`（MODIFIED）、`llm-cassette-testing`（MODIFIED）
- Affected code:
  - `src/hostlens/agent/backend.py`（`ThinkingBlock` / `RedactedThinkingBlock` + 并入 `ContentBlock` 联合;`BackendCapabilities` 注释更新说明「容忍 thinking 不需新字段」）
  - `src/hostlens/agent/backends/anthropic_api.py`（`_classify_validation_error` / `_is_content_discriminator_error` 收窄 `unsupported_content_block` 语义;capabilities 注释更新;`disable_thinking` docstring 重述）
  - `src/hostlens/agent/cassette_key.py`（`request_key_for_payload` 加 thinking 块归一化投影）
  - `tests/support/cassette_recording.py`（test-support 的 `RecordingBackend`；落盘 canonical request 同步归一——卫生/安全，非 key 匹配所需）
  - `tests/`（`ThinkingBlock` 解析 / relay verbatim 结构测试 / cassette key 归一 / golden:thinking-free 不变 + 新增 thinking-bearing 用例 / `unsupported_content_block` 测试改语义;手写合成 thinking cassette 供 CI replay;新增 `@pytest.mark.live` DeepSeek thinking-on 多轮 relay 回归测试）
- 对外契约影响:`MessageResponse` 的 `ContentBlock` 判别联合（Agent loop 消费侧）扩容;cassette 请求 key 算法语义变更（影响既有 cassette 命中，需重录或确认无 thinking 的 cassette 不受影响）。**不**改 `LLMBackend.messages_create` 签名、**不**改 `BackendCapabilities` 字段集、**不**改 CLI / MCP / Notifier / Scheduler 契约。
- Migration: 无配置迁移（无新字段）。既有 cassette:不含 thinking 块的保持命中（归一化对无 thinking 的 messages 是恒等投影）;若已有含 thinking 的 cassette（理论上不存在，旧 union 解析不了）需重录。

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| provider 吐的 thinking 块缺 `signature` 字段（未来某端点/模型） | `ThinkingBlock` required `signature` 校验失败 | `model_validate` → `BackendError(kind="invalid_response")`（不再误标 thinking 专属）;loop fail-loud 上抛、CLI 边界兜成一行错误。**实测 DeepSeek pro/flash 均带 signature，当前无此风险** |
| provider 吐未建模的**新** block type（非 thinking/redacted） | `content[*]` union discriminator 失败 | 走收窄后的 `unsupported_content_block`（语义现为「真正未知 block」）→ `BackendError`，清晰提示而非裸 pydantic |
| 多轮 relay 回传 thinking 块被 provider 拒（未来某端点验签且不认旧 id-signature） | 续轮 400 | **实测 DeepSeek 不验签、零 400**;真 Anthropic Path 1 不请求 thinking 故不产生 thinking 块、不触发;若未来端点验签失败 → `BackendUnavailable` 走既有重试/降级 |
| cassette 归一化遗漏某条 thinking 块 | record 的 key 含非确定 thinking → replay miss | 单测断言「含 thinking 的多轮 messages 投影后 key 稳定」+ golden 钉死;归一是「drop 整块」非「drop 字段」，`extra="allow"` 的额外字段一并丢弃，无残留 |
| `disable_thinking=True` 仍漏抑制（provider 不认 disabled） | 仍返回 thinking 块 | **本提案使其无害**:thinking 块现在能 parse + relay，不再崩（这正是从「抑制」到「容忍」的健壮性收益） |

### Operational Limits

无新增运维约束。union 扩容是纯解析层、cassette 归一化是投影时的列表过滤（O(blocks)，零额外 IO）、relay 沿用既有 verbatim 路径。**正向**:thinking 块进 messages 后单轮 input token 略增（thinking 文本随多轮累积回传），但本提案不主动请求 thinking、仅容忍 provider 强制吐的部分;接 DeepSeek 时可继续用 `disable_thinking=True` 把这部分 token 省掉。不改 `messages_create` 的 timeout / 重试 / 并发模型（沿用 `add-llm-backend-protocol`）。

### Security & Secrets

无新增密钥、无新增凭据暴露面。**反而收紧一处**:cassette 落盘的 canonical request 归一化丢弃 thinking 块，避免把模型 chain-of-thought 推理串持久化进 cassette 文件（潜在敏感内容 + 噪声）。`signature`（DeepSeek 即 message id）非密钥，但作为 thinking 块一部分在 cassette key 投影时一并丢弃、不进 hash。`ThinkingBlock.thinking` 文本可能含模型对采集数据的推理，但 cassette 完整响应仅本地存储、CI 默认 `-m 'not live'` 不录;live 测试凭据从环境/cc-switch 读取不入仓。

### Cost / Quota Impact

近中性偏正向。本提案**不主动请求** thinking（不开 budget_tokens），仅容忍 provider 强制吐的 thinking;对真 Anthropic（默认不吐 thinking）零 token 影响。接 DeepSeek 类端点时，多轮回传的 thinking 文本会随轮次累积进 input（可由 `disable_thinking=True` 省掉，本提案让「不省」也不再崩）。CI 全程 mock / cassette replay / `-m 'not live'`，零真实 API 调用;live 回归测试单次 ~2 调用，成本极低。

### Demo Path

```bash
# 路径一(CI 友好，无付费 API):cassette replay 验证 thinking 解析 + relay + key 归一
# 注意:此 replay cassette 是**手写合成**的(thinking 块为 pattern-free 合成文本)，
#       不从真实 DeepSeek 录制——因 RecordingBackend 敏感门禁会扫 response 内 thinking CoT
#       (server 巡检 CoT 含 hostname/ip/路径概率高)致 poison，见 design「待解决问题」决策。
pytest tests/ -m 'not live' -k "thinking or cassette" -q
# 期望:含 thinking 块的多轮响应被 ThinkingBlock/RedactedThinkingBlock 成功解析、
#       assistant content verbatim 回传、cassette key 对含 thinking 的 messages 稳定命中

# 路径二(真实端点，手动)：DeepSeek thinking-on 多轮不再崩
export HOSTLENS_BACKEND__TYPE=anthropic_api
export HOSTLENS_BACKEND__API_KEY=<deepseek-token>
export HOSTLENS_BACKEND__BASE_URL=https://api.deepseek.com/anthropic
export HOSTLENS_AGENT__PRIMARY_MODEL=deepseek-v4-pro
# 注意:不设 DISABLE_THINKING(或设 false)——本提案前必崩在 thinking 解析，本提案后正常出报告
hostlens inspect local-host --intent "检查这台机器的健康状况"
# 期望:多轮工具循环全程不报 ValidationError / BackendError(unsupported_content_block)，正常产出
```

CI 无成本回归:`pytest tests/ -m 'not live'` 全绿(单测 mock SDK + 手写合成 cassette replay 锁定 thinking 解析/relay/key 归一);**「provider 是否恒守不验签 / thinking schema 不漂移」是外部不变量、CI 内无法证明**。该 live 验证是**接入 DeepSeek 或升级 `anthropic` SDK / DeepSeek 模型前的人工必跑门禁**(非 CI 强制):实施者在 PR 前用 `HOSTLENS_LLM_MODE=live` + 真实 DeepSeek 凭据跑一次 `pytest -m live`，绿了再合。「接 DeepSeek thinking-on 跑通」因此是**人工门禁保证**、非 merge pipeline 自动 enforce——这是诚实边界。
