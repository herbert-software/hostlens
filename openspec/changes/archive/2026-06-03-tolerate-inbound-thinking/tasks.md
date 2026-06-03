## 1. ContentBlock 联合扩容(支柱①)

- [x] 1.1 在 `src/hostlens/agent/backend.py` 新增 `ThinkingBlock(type:Literal["thinking"], thinking:str, signature:str)`,`model_config = ConfigDict(extra="allow")`;`__all__` 导出
- [x] 1.2 新增 `RedactedThinkingBlock(type:Literal["redacted_thinking"], data:str)`,`extra="allow"`;`__all__` 导出
- [x] 1.3 `ContentBlock` 联合改为 `Annotated[TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock, Field(discriminator="type")]`
- [x] 1.4 更新 `BackendCapabilities.extended_thinking` 字段 docstring:说明「容忍 inbound thinking 无需新 capability 字段;extended_thinking 仍指主动请求,保持 False」(对齐 design D-2)——**显式写明**:`extended_thinking=False` 仅指「不主动请求 thinking」,**不**意味「响应不会含 thinking」(inbound thinking 被无条件容忍),防未来消费者据此误判响应 thinking-free
- [x] 1.5 单测:thinking 块解析为 `ThinkingBlock` 且 `signature` 取到、redacted 解析为 `RedactedThinkingBlock`、真正未知 type 仍 raise `ValidationError`、thinking 块带 provider 额外字段 `model_dump()` 保留(verbatim round-trip);验收 `pytest tests/agent/test_message_response.py -q`

## 2. 响应解析错误语义收窄(支柱①配套,Codex C)

- [x] 2.1 `src/hostlens/agent/backends/anthropic_api.py`:确认 `_is_content_discriminator_error` / `_classify_validation_error` 在 thinking 能成功 parse 后,`unsupported_content_block` 只对**真正未建模**的新 block type 触发;更新相关注释/capabilities 注释
- [x] 2.2 改既有依赖「thinking 触发 unsupported_content_block」的单测(`tests/agent/backends/test_anthropic_api.py` 相关用例):改用一个真正未知 type(如 `type="some_future_block"`)触发该 kind;新增「thinking 块成功解析、不触发 `unsupported_content_block`」用例;新增「thinking 缺 `signature` → `invalid_response`」用例
- [x] 2.3 验收 `pytest tests/agent/backends/test_anthropic_api.py -q`

## 3. cassette keying 归一化(支柱④,Codex D)

- [x] 3.1 `src/hostlens/agent/cassette_key.py`:抽取**共享投影纯函数** `project_messages_drop_thinking(messages) -> list[dict]`(无副作用,丢弃整个 `thinking`/`redacted_thinking` 块);`request_key_for_payload` 在 hash 前调用它投影(其自身签名不变);**禁止**在 recorder 处另写一份 drop 逻辑(单一来源,防投影规则漂移)
- [x] 3.2 `tests/support/cassette_recording.py`(test-support `RecordingBackend`,**非** `src/`):落盘 canonical request 调用 **3.1 的同一个** `project_messages_drop_thinking`(不自写第二份 drop 逻辑)(**卫生/安全,非 key 匹配所需**——key 匹配仅靠 3.1 的 helper,见 design D-6);**确认 response 仍存完整含 thinking 块**
- [x] 3.3 `tests/agent/test_cassette_key_golden.py`:断言既有 thinking-free golden hex **保持不变**(归一对 thinking-free 是恒等投影,**不要重置**——重置会掩盖对既有 keying 的误改);**新增**含 thinking payload 的 golden 用例,断言其 hash == 对应 thinking-stripped 等价 payload 的 hash
- [x] 3.4 单测:含 thinking 块的多轮 messages,两次 `thinking`/`signature` 不同 → request-key 相同(投影后稳定);thinking 块带额外字段不影响 key;`cassette_lint.py` 三处同源仍一致;**`_record_request_key` 对「落盘 request 仍含 thinking」的 record 重算 key == 归一后 live-key**(证明匹配不依赖落盘 strip)
- [x] 3.5 验收 `pytest tests/agent/ -k "cassette or key" -q` + `python scripts/cassette_lint.py`(若有 secret-scan 入口)

## 4. 多轮 relay 结构回归测试(支柱③,无生产代码改动)

- [x] 4.1 结构测试钉死:含 thinking 块的多轮循环里,`_inject_cache_control` + `_roll_message_cache_breakpoint` 产生的 cache_control 断点 **count 序列 `[1,2,2,…]` 且从不落在 thinking 块上**(design D-3 不变量)
- [x] 4.2 测试:Agent loop 把含 thinking 的 assistant content **verbatim `model_dump`** 回传(thinking 块原样进下一轮 assistant 消息、顺序不变)——断言回传 dict 与输入 thinking dict 逐字相等,且用 loop 实际调用形式 `model_dump()`(**无 `exclude_unset`/`exclude_none`**),钉死未来给 relay 加 exclude 参数会破坏 verbatim 的回归
- [x] 4.3 验收 `pytest tests/agent/test_loop.py -k "thinking or cache" -q`

## 5. disable_thinking 定位降级(Codex E2)

- [x] 5.1 `anthropic_api.py` 的 `disable_thinking` docstring + capabilities 注释:从「兼容必需」改述为「可选 token 节省优化,默认 False;关闭也不再崩(由本变更容忍)」
- [x] 5.2 更新 `reference_deepseek_thinking_incompatible_live_test` 相关用户文档/注释口径(若 README/docs 提到「接 DeepSeek 必须 disable_thinking」),改为「推荐设以省 token,非必需」
- [x] 5.3 确认 Fake/Playback backend 不引入 thinking 注入逻辑(无改动验收:grep 无新增 extra_body)

## 6. live 回归测试(实测可靠性兜底)

- [x] 6.1 新增 `@pytest.mark.live` 测试:对真实 DeepSeek 端点(凭据从环境读,model id 用 `deepseek-v4-pro`/`deepseek-v4-flash`——与 probe 一致,**非**旧 live 测试的 `deepseek-chat`/`deepseek-reasoner`)跑 thinking-on 多轮工具循环,断言 turn1 `[thinking, tool_use]`、回传 thinking 块后 turn2 不 400、`ThinkingBlock.signature` 取到;**marker 与 env 两轴对齐**:除 `@pytest.mark.live` 外须 `HOSTLENS_LLM_MODE=live`(否则 fixture 静默给 PlaybackBackend → 假绿),live 测试内断言 mode==live 否则 skip;CI 默认 `-m 'not live'` 跳过,凭据缺失 skip。**此 live 测试是接入 DeepSeek / 升级 anthropic SDK / DeepSeek 模型前的人工必跑门禁,非 CI 强制**
- [x] 6.2 **手写合成** thinking cassette(含 thinking 多轮,thinking 块为 pattern-free 合成文本——避开 `core/redact.py` 的 hostname/FQDN/ipv4/`/Users`|`/home`/`.ssh`/email/`sk-`/Bearer/JWT 等规则族,否则 `cassette_lint` 的敏感扫描会拦;**不**从真实 DeepSeek 录制,因敏感门禁会扫 response 内 CoT 致 poison,见 design 待解决问题决策);各轮 request 须在**非 thinking** 内容上彼此不同(多轮天然增长即满足,防 thinking-strip 后 `cassette_lint` 误报 duplicate-key);replay 锁定「解析 + relay + keying 归一」不回归。live 测试(6.1)**不**落盘 cassette
- [x] 6.3 断言本变更**不影响**既有 backend 重试/降级语义:thinking 解析不改 429 honor-retry-after / API 宕机 degraded 路径(沿用既有覆盖,加一条 smoke 确认 thinking 响应仍正常累计 usage)

## 7. 收尾

- [x] 7.1 `mypy --strict src/hostlens/agent/` 通过(新 block model 无 `Any` 泄漏)
- [x] 7.2 全量 `pytest tests/ -m 'not live'` 绿
- [x] 7.3 `openspec-cn validate tolerate-inbound-thinking` 通过
- [x] 7.4 PR 前对抗性 review(CLAUDE.md §5.3;含运行时行为变更,应跑)
