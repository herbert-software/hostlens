## 上下文

Hostlens 对 thinking 已做过「抑制」一半:`add-backend-disable-thinking`(2026-05-31 归档)给 `AnthropicAPIBackend` 加 `disable_thinking` 开关,经 `extra_body` 注入 `thinking:disabled` 关掉 DeepSeek 类端点默认开的 thinking,并把含 thinking 块的响应解析失败归一成 `BackendError(kind="unsupported_content_block")`。它**没有**建模 thinking 块,接 DeepSeek 只能靠「关得掉」这个假设。

本提案是 TODO M3.6 `support-extended-thinking` 的 **Path 1 切片**:从「抑制」转向「建模容忍」——provider 强制吐 thinking 时认得、能多轮按序回传、不崩,不再依赖抑制。动机是健壮性(`disable_thinking` 是脆路径:provider 改版 / 漏配 / 部分模型不认 disabled 都会让整链崩)。**不**主动请求 thinking、**不**消费 thinking 渲进报告(Path 2,未来独立提案)。

设计经两轮 Codex 对抗 review + 两次 live probe 全程落地,核心结论:容忍 inbound thinking 的实现是「无条件扩 `ContentBlock` 联合」,不需要任何新 capability 字段或 Protocol 签名变更。

## 验证数据(实施前已实测,2026-06-04)

`tests/manual/deepseek_thinking_probe.py`(单轮) + `deepseek_multiturn_probe.py`(多轮+工具),凭据从 cc-switch `DeepSeek` provider 取(base_url `https://api.deepseek.com/anthropic`),`deepseek-v4-pro` / `deepseek-v4-flash` 各一次(注意:这套 v4 model id 与仓内旧 `test_anthropic_api_deepseek_live.py` 的 `deepseek-chat`/`deepseek-reasoner` 不同——task 6.1 的 live 回归须用与本表一致的 v4 id)。**一次性手测;这张表不是可靠性来源——`@pytest.mark.live`(task 6.1)提供「接入/升级前可重复观测」,而「DeepSeek 永不验签」是外部不变量、CI 内无法证明,只能接入/升级前人工复核(probe 脚本是手测工具,未提交运行输出)。**

| 探针 | deepseek-v4-pro | deepseek-v4-flash |
|---|---|---|
| 单轮 thinking 块 schema | `{type, thinking, signature}` | `{type, thinking, signature}` |
| `signature` 存在? | 是,值 == message `id` | 是,值 == message `id` |
| 私有额外字段? | 无 | 无 |
| 多轮 turn1(带工具) | `[thinking, tool_use]` | `[thinking, tool_use]` |
| 回传 thinking 块后 turn2 | `[thinking, text]`,**不 400** | `[thinking, text]`,**不 400** |

结论(手测范围内):(1) DeepSeek thinking 块与 Anthropic 原生 `ThinkingBlock` 字段同形,`signature` required 安全;(2) `signature == id` 说明 DeepSeek 不做密码学验签,只回显——多轮回传旧 turn 的 id-signature **零 400**,relay 安全;(3) thinking 块每轮都出现(带工具轮也是),坐实 cassette 归一(支柱④)必需。

## 目标 / 非目标

**目标:**
- `MessageResponse.content` 容忍 `thinking` / `redacted_thinking` 块,成功解析不崩。
- Agent loop 多轮工具循环按序 verbatim 回传 thinking 块,不 400。
- cassette record→replay 对含 thinking 的多轮请求稳定命中。
- 接 DeepSeek 类端点即便**不设** `disable_thinking` 也能跑通(健壮性收益)。

**非目标:**
- 主动请求 thinking(`messages_create` 加 `thinking` 参数、`extended_thinking=True`)——Path 2。
- 消费 thinking:把推理 trace 渲进 Report / 持久化 / diff——独立提案。
- 废弃 `disable_thinking`——保留为可选 token 优化。
- interleaved thinking、新增 `backend.type=deepseek`、动 `BackendCapabilities` 字段集。

## 决策

### D-1:做 Path 1(tolerate-inbound),不做 Path 2(request-and-consume)

四支柱中 ①建模 ③多轮回传 ④cassette 归一是「容忍 + 多轮可用」的最小闭环;②给 Protocol 加 thinking 请求参数是「主动请求」,不在容忍 DeepSeek 的动机内。Path 1 直接解决「DeepSeek 默认吐 thinking 我们不认」,体量小、聚焦。符合 §8「架构清晰度 > 功能广度」。**否决**:一刀做全四支柱(Path 2)——主动请求 thinking 与本提案动机正交,且会牵出 Protocol 签名变更涟漪三个 backend。

### D-2:不新增 `tolerates_inbound_thinking` capability 字段(关键)

CLAUDE.md §4.11 明文:`BackendCapabilities` 字段只放「Agent loop 真正会 branch 的能力」。容忍 inbound thinking 的实现是「无条件扩 `ContentBlock` 联合」——任何 backend 收到 thinking 都能解析,loop 的 relay 是无条件 `block.model_dump()`(`loop.py` ~407),**没有任何代码 branch 在「是否容忍 thinking」上**(loop 唯一 branch 的 capability 是 `prompt_caching`)。一个没人 branch 的 flag 是死元数据,违反 §4.11。⇒ **7 字段不变,零新 capability**。`extended_thinking` 保持 `False`(它的语义是「主动请求 thinking」,留给 Path 2)。**否决**:Codex round 1 曾建议新增 `tolerates_inbound_thinking`;round 2 用真实代码确认无 branch 点后撤销。

### D-3:多轮 cache_control 断点不落 thinking,只加结构测试

支柱③ 担心「cache_control 断点打在 thinking 块上」(Anthropic 通用约束)。但 Hostlens 的 `_roll_message_cache_breakpoint` 把断点 B 打在「最后一条 message 的最后一个 block」,而 loop 每个 assistant tool_use 轮后**必然 append 一条 user tool_result 消息**(`loop.py` ~278),最后一条永远是 user tool_result;断点 A 在 system 上;end_turn 轮 assistant 不回传。⇒ **断点永远落不到 thinking 块,relay 逻辑零改动**,只加结构回归测试钉死「断点 count 序列 `[1,2,2,…]` 且不落 thinking」。失效条件:未来引入「end_turn 后继续追加 assistant 并续轮」——测试会红,提醒重审。

### D-4:`ThinkingBlock` / `RedactedThinkingBlock` 用 `extra="allow"`

这两块是 verbatim relay 对象;`TextBlock`/`ToolUseBlock` 是「消费特定字段」对象。后者 `extra="ignore"` 丢额外字段无害,但 thinking 块若 `extra="ignore"`,`model_dump()` 会丢 provider 私有字段 → 回传不再逐字。`extra="allow"` 零成本保 verbatim(DeepSeek 当前无私有字段,是未来保险)。**否决**:统一 `extra="ignore"`——破坏 relay 保真。

### D-5:`signature: str` required(probe 落地)

实测 DeepSeek pro/flash 与 Anthropic 原生均带 `signature`,建 required 安全且让 relay `model_dump()` 不产生 `"signature": null` 破坏 verbatim。**否决**:`signature: str | None = None`——会让缺失字段 dump 成 `null` 改 wire 形状;且 Path 1 下 thinking 只来自强制吐的端点,缺 signature 的端点应走 `invalid_response` 清晰报错(已有归一路径),而非静默 optional。

### D-6:cassette keying 丢整块 + recorder 落盘同源归一

`request_key_for_payload` 在 hash 前丢弃整个 thinking/redacted 块(非只丢字段——`extra="allow"` 残留任何字段都让 hash 不稳,Codex round 2 D 点),helper 签名不变。**key 匹配正确性仅靠此 helper**:`PlaybackBackend._request_key`、`_record_request_key`(读盘重算;两者均在 `src/hostlens/agent/backends/playback.py`)、`cassette_lint`(`scripts/cassette_lint.py`)三处都委托它,且 `_record_request_key` 对落盘 request 在读取时**重新**归一——故 live-key 与 replay-key 恒相等,**与落盘 body 是否已 strip thinking 无关**。`RecordingBackend`(`tests/support/cassette_recording.py`)落盘 canonical request 同步投影属**卫生 + 安全**(不持久化 CoT、不让敏感门禁扫 thinking 文本),**非 key 匹配所需**;response 存完整(含 thinking 供回放)。golden 等价限定为 thinking-free payload 恒等(归一对其是恒等投影,既有 golden hex **不变**,**不重置**)。新增「`_record_request_key` 对落盘仍含 thinking 的 record 重算 key == 归一后 live-key」场景钉死「匹配不依赖落盘 strip」。**否决**:只丢 `thinking` 字段保留块壳——`signature` + extra 仍非确定,hash 照样漂。

### D-7:收窄 `unsupported_content_block` 语义 + 改其测试

union 建模 thinking 后,`type="thinking"` 成功 parse,不再触发 `_classify_validation_error` 的 `unsupported_content_block` 分支(Codex round 2 C)。该 kind 收窄成「真正未知/未建模的新 block type」(仍兜 SDK 未来新块);依赖它对 thinking 触发的单测(`test_anthropic_api.py` 相关用例)改成用一个真正未知 type 触发。thinking 缺 `signature` 归 `invalid_response`(字段缺失,非未知 block)。

### D-8:`disable_thinking` 并存,重述为可选 token 优化

抑制(disable)与容忍(本提案)互补:抑制省 token(provider 不生成 thinking),容忍保健壮(关不掉也不崩)。`disable_thinking=True` → 无 thinking 块 → 容忍不触发;`False` → thinking 块来 → 容忍接住。无冲突。文档/注释把 `disable_thinking` 从「兼容必需」改述为「可选优化,默认 False」(Codex round 2 E2),避免读者误以为不设就会崩。Fake/Playback 不实现注入。

## 风险 / 权衡

- [provider 未来吐缺 `signature` 的 thinking 块] → `invalid_response` 清晰报错(非裸 pydantic),已有归一路径兜;实测当前两模型都带,无即时风险。
- [未来某端点验签且不认回传的旧 id-signature → 续轮 400] → 实测 DeepSeek 不验签;真 Anthropic Path 1 不请求故不产 thinking 不触发;若发生走 `BackendUnavailable` 既有重试。归 live 回归测试覆盖。
- [cassette 归一遗漏某块 → replay miss] → 单测断言含 thinking 多轮 messages 投影后 key 稳定 + golden;归一是「drop 整块」杜绝残留。
- [既有 cassette key 算法语义变更] → 归一对 thinking-free 是恒等投影,既有(全是 thinking-free)cassette 命中不变;thinking-free golden hex **保持不变**(不重置,作为「未误改既有 keying」的锚),仅**新增** thinking-bearing 等价用例。
- [thinking 文本进 cassette response → repo 含 CoT] → response 必须留 thinking 供回放,无法消除;request 侧已归一去重。诚实残留:`detect_sensitive_text` 门禁是正则/关键词式,只挡 ip/hostname/路径/email 等成型 secret,**挡不住「换了措辞但运维敏感」的推理**(如「prod 库负载高」);故采用「**CI cassette 手写合成、不录真实 DeepSeek**」(见待解决问题决策)从源头避开把真实 CoT 提交进仓。
- [thinking-on 端点返回 `pause_turn`(server 端工具长任务的暂停信号,与纯 thinking 不相关)] → loop 现有 `raise UnexpectedStopReason`(fail-loud,D-8「Hostlens solicits neither」)。probe 仅见 `tool_use`/`text` 未见 `pause_turn`;Path 1 不请求 server 工具故不预期触发,若触发是明确报错而非静默错乱——非本提案引入(既有行为),不在 relay「零改动」claim 的反例内(pause_turn 在 relay 前就 raise)。
- [`redacted_thinking` 形状未经 DeepSeek 实测(`{type,data}` 取自 Anthropic 原生)] → `extra="allow"` + 缺 required 字段走 `invalid_response` 兜底,风险有界;probe 只见 `thinking` 未见 redacted,建模 redacted 是「按 Anthropic 原生 spec 防丢块」而非实测确认。

## 迁移计划

无配置迁移(无新字段、无 schema 变更需用户动作)。代码侧:先扩 `ContentBlock` 联合(否则含 thinking 的 cassette 连 lint 都过不了),再改 keying 归一 + recorder,再改 `unsupported_content_block` 语义与测试,最后 golden 校验(thinking-free hex 不变 + 加 thinking-bearing 用例) + 加 live 回归。回滚:删两个 block model + union 还原 + keying 投影还原即可,无持久化状态。

## 待解决问题(已决,记录决策)

- **CI replay cassette 不走真实 DeepSeek 录制**。`RecordingBackend` 的敏感门禁是 detect-and-reject(不脱敏),而 spec 要求 response 必须留**完整** thinking 供回放——门禁会扫到 thinking CoT。真实 DeepSeek 服务器巡检会话的 CoT 极可能命中 `core/redact.py` 的 hostname/ipv4/路径/email 规则,录制即 poison、cassette 写不出,使 CI 友好的 replay 路径(Demo Path 路径一)失去 cassette。⇒ **决策**:CI 用的 replay cassette **手写合成**(thinking 块为 pattern-free 合成文本),确定性锁死「解析 + relay + keying 归一」三件事,**不依赖**录制门禁;真实 DeepSeek thinking 行为由 `@pytest.mark.live`(task 6.1)作**人工门禁**覆盖,该 live 测试**无需**落盘 cassette。CI 保证与录制门禁问题就此解耦。
- 残留:手写合成 cassette 的 thinking schema 若与真实 DeepSeek 漂移,CI 仍绿而 live 会红——这正是 live 门禁职责,接入/升级前必跑。
