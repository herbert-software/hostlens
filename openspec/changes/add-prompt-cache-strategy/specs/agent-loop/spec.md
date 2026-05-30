## 修改需求

### 需求:`cache_control` 注入由 backend capability 决定

调用 `messages_create` 前，`AgentLoop` 必须按**两层缓存策略**注入 `cache_control: ephemeral`，且仅当 `backend.capabilities.prompt_caching == True` 时注入；为 `False` 时**禁止**在 `system` / `tools` / `messages` 任一处注入任何 `cache_control`。注入判定必须在 loop 端完成，禁止依赖 backend 静默丢弃。

两层断点：

- **断点 A（静态前缀）**：当构造期注入的 `system` 为非空 `list[dict]` 时，在其最后一个 block 注入 `cache_control: ephemeral`。该断点缓存 `tools + system` 这段跨 run 稳定的最长前缀（Anthropic 前缀顺序为 `tools → system → messages`，system 断点天然吞掉前面的 tools）。`tools` 数组**禁止**单独携带 `cache_control` —— 它已被断点 A 的前缀覆盖，单独标记只会浪费断点预算。`system` 为裸字符串或空 list 时跳过断点 A（不报错）。
- **断点 B（滚动对话前缀）**：每次调用 `messages_create` 前，仅在当前 `messages` 最后一个 message 的最后一个 content block 上注入 `cache_control: ephemeral`，且在 messages 浅拷贝上操作（只浅拷贝被标记的末 message 及其末块），不得 mutate loop 持有的 `messages`。因注入只作用于请求快照、从不写回存储的 messages（见本需求末段），历史 message 天然不带 `cache_control`，故**无需也不得**额外做「清除历史断点」的归一化（那是对不可能分支的防御）。当末 message 的 `content` 不是非空 block 列表（如裸字符串）时跳过断点 B（不强转、不报错），断点 A 仍生效。

断点注入必须作用于「即将发出的请求快照」，不得 mutate loop 持有的 `self._system` 或累积的 `messages` 列表。任一请求携带的 `cache_control` 断点数必须恒 ≤ 2（A + B），不得随 turn 数增长。

#### 场景:prompt_caching=False 三处零注入

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == False`，`run()` 触发若干轮 `messages_create`
- **那么** 每次传给 `messages_create` 的 `system` / `tools` / `messages` 中任一 block 都不含 `cache_control` key（因此 backend 的 `check_capability_consistency` 不会 raise `BackendCapabilityViolation`）

#### 场景:prompt_caching=True 断点A在system末块且tools无断点

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == True`，`run()` 触发一次 `messages_create`
- **那么** 传给 `messages_create` 的 `system` 最后一个 block 含 `{"cache_control": {"type": "ephemeral"}}`
- **并且** 传给 `messages_create` 的 `tools` 数组中没有任何元素含 `cache_control` key

#### 场景:prompt_caching=True 滚动断点B只在最新message末块

- **当** backend `capabilities.prompt_caching == True`，loop 跑到第二轮（`messages` 已含 user / assistant / tool_result 多个 message）触发 `messages_create`
- **那么** 传给 `messages_create` 的 `messages` 中，仅最后一个 message 的最后一个 content block 含 `{"cache_control": {"type": "ephemeral"}}`，其余 message 的所有 block 都不含 `cache_control` key

#### 场景:断点数恒不超过2且不随turn增长

- **当** backend `capabilities.prompt_caching == True`、`system` 为非空 list，loop 从 `run(intent)` 起连续触发多轮 `messages_create`（首轮末 message 为裸字符串 `intent`，后续轮末 message 为 `tool_result` block 列表）
- **那么** 首轮请求 `system` + `tools` + `messages` 携带的 `cache_control` 断点总数为 1（仅断点 A；断点 B 因末 message 为裸字符串被跳过）
- **并且** 后续每一轮请求的断点总数为 2（断点 A 一个、断点 B 一个）
- **并且** 任一轮请求的断点总数都不超过 2，且不随 turn 数增加

#### 场景:末message为裸字符串时跳过断点B保留断点A

- **当** backend `capabilities.prompt_caching == True`、`system` 为非空 list，但当前 `messages` 最后一个 message 的 `content` 为裸字符串，触发 `messages_create`
- **那么** 该 message 不含 `cache_control`（断点 B 被跳过），且 `system` 最后一个 block 仍含 `{"cache_control": {"type": "ephemeral"}}`（断点 A 生效）

#### 场景:第二次调用真实命中静态前缀缓存

- **当**（`@pytest.mark.live`，opt-in 真实 Anthropic API）以非空 list 的 `system`、`prompt_caching == True`，且 `tools + system`（或 padded `system`）静态前缀 token 数已**超过所用 model 的最小可缓存阈值**（Sonnet/Opus ≈1024、Haiku ≈2048；前缀不足时由测试显式 pad 越过阈值），发起至少第二轮 `messages_create`
- **那么** 第二轮响应的 `cache_read_input_tokens > 0`（静态前缀在第二轮被复用）
- **测试方式（spec↔test 如实声明）** 该 live 验收驱动 `AgentLoop` 的真实注入函数（`_inject_cache_control` / `_roll_message_cache_breakpoint`）以「`run()` 发出的请求形态」连续发起 ≥3 次 `messages_create`，而**非**驱动 `AgentLoop.run()` 经真实模型多轮 tool-use —— 因真实模型未必每轮确定性 emit `tool_use`，经 `run()` 跑真模型会让 live 测试间歇假阴性。两种路径发出的请求前缀形态一致，故本场景命题（第 2/3 次调用复用静态前缀）被等价覆盖；loop 的多轮控制流由第 2 节 CI 结构测试经 `run()` 端到端验证
- **覆盖范围（必须如实声明，不得过度宣称）** 此 live 验收**只覆盖静态前缀断点 A 的真实命中**：`cache_read_input_tokens` 是单一聚合值，断点 B（对话前缀）在 turn2 才 create、turn3 才首次 read，且其 read 量无法从聚合值中干净地与 A 分离。因此断点 B 的正确性由 CI 结构断言（B 落在正确位置、不写回存储 messages、断点数序列 `[1,2,2,…]`）保证，**不**由本 live 场景宣称验证。前缀低于阈值时 Anthropic 不缓存属环境前提不满足、非实现缺陷，故前置条件必须由测试保证

#### 场景:多轮真实命中持续有效

- **当**（`@pytest.mark.live`，承前置条件与上一场景的测试方式）发起至少第三轮 `messages_create`
- **那么** 第三轮响应的 `cache_read_input_tokens > 0`（缓存命中在多轮中持续有效；聚合值不区分 A / B 的贡献，断点 B 的位置与断点数不变量由 CI 结构断言保证、不由本场景宣称）
- **说明** 此处只断言聚合命中持续 > 0，**不**对 A / B 各自的 read 量做拆分归因（聚合值无法区分），拆分归因属过度验证、本提案不做
