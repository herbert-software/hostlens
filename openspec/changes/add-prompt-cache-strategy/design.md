## 上下文

`AgentLoop`（M2.2，`src/hostlens/agent/loop.py`）每个 turn 都调一次 `backend.messages_create(system, messages, tools, ...)`。Anthropic 的 prompt caching 把请求按固定顺序拼成前缀：

```
[ tools 数组 ]  →  [ system 块 ]  →  [ messages 块 ]
└──────── 跨 run 稳定 ────────┘     └─ run 内增长 ─┘
```

一个 `cache_control` 断点标记「一段可缓存前缀的终点」：API 缓存从请求开头到该断点（含）的所有内容。命中规则是**最长匹配前缀**，并允许在每个断点前回看约 20 个 block 做增量匹配。最多 4 个断点。ephemeral 缓存 TTL 5 分钟。

现状（M2.2 占位实现）：`_inject_cache_control` 仅在 `system` 为非空 list 时给末块打一个断点，gated on `backend.capabilities.prompt_caching`。tools 数组和 messages 都没动。M2.2 的 docstring 明确把「该缓存什么」留给 M2.5。

约束：
- CLAUDE.md §4.11 规则 #2：断点注入判定必须在 **loop 端**完成；backend 严禁静默丢弃 `cache_control`，不一致必须 raise `BackendCapabilityViolation`。
- CLAUDE.md §4.2 / M2.4：Planner 把 system 渲染成单元素 text block 列表且跨 run byte 稳定（断点 A 的前提）。
- 架构清晰度 > 功能广度：实现要让面试官读 `loop.py` 就懂缓存策略。

## 目标 / 非目标

**目标：**
- 用**两个断点**覆盖两段可缓存前缀：静态前缀（tools+system，跨 run）与滚动对话前缀（messages，run 内逐轮增长）。
- 断点数对任意 turn 恒 ≤2（首轮裸 str intent 跳过 B 故 = 1，后续轮 = 2），永不随对话增长，稳在 ≤4 预算内。
- capability gate 关闭时零注入，且有 CI 负例守住。
- 验证职责分离：CI 验「请求结构正确」，live opt-in 验「真实缓存命中」，杜绝 fixture 自证。

**非目标：**
- 不做 cache hit rate 可观测面板 / OTel 属性（见 proposal Non-Goals）。
- 不调 `ttl`（只用默认 ephemeral）。
- 不改 tools 数组内容/顺序/生成逻辑。
- 不碰 planner spec、backend 实现、`LoopResult` schema。

## 决策

### D-1：静态前缀只用 **1 个断点**（system 末块），**不**单独标 tools

因为前缀顺序是 `tools → system`，在 system 末块打断点已经把前面的 tools 一并纳入缓存前缀。Hostlens 的 system 渲染同样跨 run byte 稳定（M2.4），所以 `tools+system` 是**一段连续的稳定前缀**，一个断点即可全缓存。

- **替代方案：tools 末项 + system 末块各打一个断点（2 个静态断点）**。否决。这会创建两个缓存条目（`tools` 单独 + `tools+system`），多花一个断点预算和一次额外 cache-write，却没有任何收益 —— 只有当「tools 稳定但 system 会变」时分离才有意义，而 Hostlens 两者都稳定。把这个推理写进 spec + 文档，正是「我们理解缓存机制」的展示点。
- **结论**：断点 A = system 末块。tools 数组**不得**携带 `cache_control`（结构测试对此做正向断言）。

### D-2：滚动对话断点 B —— 每轮只标**最新** message 末块（依赖快照不变量，无需清除历史断点）

每次调 `messages_create` 前，只在当前 `messages` 最后一个 message 的最后一个 content block 上打断点 B —— 在 messages 浅拷贝上操作（只浅拷贝被标记的末 message 与其末块），不 mutate loop 存储的 messages。

为什么是「只标最新末块」而非「每轮新增一个不清除的断点」：
- 若每轮新增且保留历史断点，断点数随 turn 线性增长，第 4 轮后超过 API 上限 4 被拒、且缓存碎片化。
- 「只标最新」配合 API 的最长前缀匹配：B 在 turn2 首次 create（写入对话前缀缓存），**从 turn3 起**（B 首次创建后的下一轮）**典型短 run 下** turn N 的请求里断点 B 在 messages 末块，API 会命中 turn N-1 写入的更短对话前缀（增量回看），所以**不需要**保留历史断点也能逐轮命中。注意：turn2 是 B 的 create 而非 read，B 的对话前缀 read 收益从 turn3 才开始。**保守声明**：B 的 turn3 read 依赖 Anthropic「最长前缀匹配 + 断点前 ~20 block 回看」跨断点位置发现上一轮缓存——典型短 run 成立，极长对话（B 位置间隔超回看窗口）可能 B 只 create 无 read；无论如何静态前缀 A 的 read 不受影响，且 live 测试只断言聚合 cache_read（由 A 满足）、不单独证实 B，故对 B 多轮收益保持保守。
- 为什么**不需要**「清除历史 block 的 `cache_control`」这一步：注入只作用于请求快照、从不写回存储 messages（见 D-3），故存储 messages 恒不带 `cache_control`，历史断点根本不会出现；多写一步「全体清除」是对不可能分支的防御（CLAUDE.md §6），省略。该不变量由「断点数序列 `[1,2,2,…]`」结构测试守住。

断点 B 的命中时序（Planner 典型 run）。注意 turn1 的末 message 是 `{"role":"user","content":intent}`，content 是**裸字符串**，按 D-2 降级规则**跳过断点 B** —— 所以 turn1 只有 1 个断点（A），断点数序列是 `[1, 2, 2, …]` 而非恒 2：
```
turn1: messages=[user(intent=裸str)]                  A:cache_create  B:跳过(裸str)     read=0 (冷启)   断点数=1
       → tool_use, append assistant+tool_result(list 形态)
turn2: messages=[user, assistant, tool_result]        A:cache_READ    B:cache_create   read>0 ✅        断点数=2
       → ...
turn3: messages=[..., tool_result]                    A:cache_READ    B:cache_READ(turn2前缀)†+create  断点数=2
  († B 的 read 为典型短 run 下成立；超回看窗口的极长对话可能仅 create，A 的 read 不受影响)
```
turn2 起断点 A 必然 cache_read>0（静态前缀复用，前提见 D-5 最小可缓存阈值），这正是验收「第二次调用 cache_read>0」的来源；断点 B 是额外的对话前缀折扣。

- **替代方案：不做 B，只保留静态断点 A**。否决（用户已拍板要两层）。仅 A 也能满足「turn2 cache_read>0」的字面验收，但放弃了多轮对话前缀这块省钱点（典型短 run 下兑现，边界见 D-2 保守声明）—— 而 Agent loop 多轮恰恰是 prompt caching 最该发力处（CLAUDE.md §4.8）。
- **降级**：若末 message 的 `content` 是裸 `str`（Anthropic 合法形态），无处挂断点 → **跳过 B**，不强转 list（避免改 wire 语义），A 仍生效，run 正常。

### D-3：注入点放在 `_call_with_retry` 调用前，作用于「本次请求快照」，不可变 self 状态

断点注入是**纯函数式**的请求组装步骤：`_inject_cache_control(system, caps)` 返回 system 的浅拷贝；新增 `_roll_message_cache_breakpoint(messages, caps)` 返回 messages 的浅拷贝（只浅拷贝被标记的末 message 与其末块）。loop 持有的 `self._system` 与累积的 `messages` 列表**不被 mutate** —— 注入只发生在「即将发出的那一份请求」上。

- 理由：messages 在 loop 里被 append 复用；注入若写回存储 dict，会让存储状态在轮间变脏。快照式注入（只在浅拷贝上标记末块）保证存储 messages 恒干净，每次请求的断点布局可独立推理、可测。
- 由这条不变量推出一个**简化**：既然存储 messages 恒不带 `cache_control`，`_roll_message_cache_breakpoint` **无需**遍历清除历史断点，只标记末块即可 —— 多写一步「全体清除」是对不可能分支的防御（CLAUDE.md §6），省略；该不变量由「断点数序列 `[1,2,2,…]`」结构测试守住。

### D-4：capability gate 单点判定，A 与 B 同源 gate

`prompt_caching == False` 时，A 与 B **都不注入**。两个注入函数各自首行检查 `capabilities.prompt_caching`，False 直接原样返回。CI 负例对「system / tools / 任一 message block 都不含 `cache_control` key」做三处断言，确保 gate 关闭时 backend 的 `check_capability_consistency` 不会 raise。

### D-5：验证 = FakeBackend 结构断言（CI）+ `@pytest.mark.live` 真实命中（opt-in）

与 Codex 设计咨询结论一致（见 proposal「变更内容」）。

- **CI 结构测试**（`FakeBackend`，捕获 loop 发出的 request 副本）：
  - 正例（`prompt_caching=True`）：system 末块有 `{"cache_control":{"type":"ephemeral"}}`；最新 message（block 列表形态）末块有断点 B；tools 数组各项**无** `cache_control`；断点数序列为 `[1, 2, 2, …]`（首轮裸 str intent 跳 B = 1，后续 = 2），恒不超过 2 且不随 turn 增长。
  - 负例（`prompt_caching=False`）：system / tools / messages 三处零 `cache_control`。
  - 降级例：末 message content 为裸 str → B 跳过、A 仍在。
- **live 测试**（`@pytest.mark.live`，默认 `-m 'not live'` 跳过）：真 API 跑 ≥3 轮，断言 turn2 与 turn3 的 `cache_read_input_tokens > 0`。**覆盖范围（必须如实声明，避免过度宣称这一隐蔽的自证）**：live 只验证**静态前缀断点 A 的真实命中**与「多轮命中持续有效」。`cache_read_input_tokens` 是单一聚合值，断点 B 在 turn2 才 create、turn3 才首次 read，其 read 量无法从聚合值干净分离 —— 所以 live **不**宣称单独验证了 B；B 的正确性归 CI 结构断言（位置 / 不写回存储 messages / `[1,2,2,…]` 序列）。**前置条件（必须满足，否则是假阴性而非真信号）**：测试构造的 `tools + system` 静态前缀 token 数必须**超过所用 model 的最小可缓存阈值**（Anthropic 对短 prompt 不缓存：Sonnet/Opus ≈1024、Haiku ≈2048 input token）。测试不依赖真实 Planner 前缀恰好够大，而是显式 pad 一段稳定 system（或塞入足量 tool schema）保证越过阈值 —— 这样「pass」可信、「fail」是真实信号（注入位置错或前缀非 byte 稳定），不会因前缀过短误判。
- **拒绝**「cassette + PlaybackBackend 断言 cache_read>0」作主验收证据：cache_read 数值录制时已固化进 fixture，回放只证明「回放链路能读出当年的数值」，不证明当前代码仍能让真实 API 命中 —— 自证陷阱。cassette 仅留作 API shape 回放兼容性辅助。

FakeBackend 需要能把每次收到的 `(system, messages, tools)` 存下来供断言。若现状 `FakeBackend` 只按序返回不记录入参，则在测试侧用一个记录型子类/包装（捕获 `messages_create` 入参），不污染生产 `FakeBackend`。

## 风险 / 权衡

- **滚动断点实现误把 mark 写回存储 messages → 历史断点累积、断点数超 4 被 API 拒** → 缓解：D-3 快照式注入（只在浅拷贝末块标记、不 mutate 存储），存储 messages 恒干净；「断点数序列 `[1,2,2,…]`、恒 ≤2 不随 turn 增长」结构测试守住该不变量。
- **真实 API 短前缀不缓存（cache_read 恒 0）导致 live 测试假阴性** → 缓解：① 结构测试与命中测试职责分离，CI 不依赖真实命中；② live 测试**显式 pad 静态前缀越过 model 最小可缓存阈值**（见 D-5 前置条件），使其在「实现正确」时必过、「实现错误」时才挂 —— 排除「前缀过短」这一与实现无关的失败源。真实 Planner 的 tools+system（含 inspector schema 概览）通常已远超阈值，但 live 测试不赌这一点。
- **末 message 裸 str 导致 B 静默跳过，使用者以为 B 生效** → 缓解：降级路径有专门结构测试断言「裸 str 时 B 不在、A 在」，且文档说明 Planner 路径下 messages 末块恒为 block 列表（tool_result 是 list 形态），裸 str 只在直接构造 loop 的边缘场景出现。
- **权衡：首轮 cache_creation 比无缓存略贵（~1.25× 输入单价）** → 接受：单次一次性，典型 ≥3 轮 run 即净省（proposal Cost 小节）。
- **权衡：ephemeral 缓存让对话前缀在 Anthropic 侧驻留 ≤5 分钟** → 接受：缓存内容本就是要发送的数据，不扩大出域范围；文档明示驻留时长供合规评估（proposal Security 小节）。

## 迁移计划

无数据/契约迁移：`cache_creation_input_tokens` / `cache_read_input_tokens` 字段 M2.2 已在 `MessageResponse` / `LoopUsage` 存在，本变更只让它们在多轮下非零。`LoopResult` schema 不变，既有报告/测试不受影响。回滚 = 还原 `loop.py` 两个注入函数到 M2.2 单断点版本即可，无残留状态。

## 待解决问题

- live 测试用哪个 model 跑真实命中验证？成本最低是 `claude-haiku-4-5`（与 `health_check_model` 同款），但 Haiku 的最小可缓存阈值更高（≈2048 token），与「压低成本」存在张力：要么用 Haiku 并把静态前缀 pad 到 ≥2048，要么改用阈值更低（≈1024）的 Sonnet。两者皆可 —— tasks 阶段二选一，关键是无论选谁都要满足 D-5 前置条件（前缀越过该 model 阈值），不靠真实 Planner 前缀大小。
- 文档 `docs/agent-cache-strategy.md` 是否同时塞进 `docs/ARCHITECTURE.md §9` 的交叉链接？倾向加一行 backlink，正文独立成文（保持简短）。
