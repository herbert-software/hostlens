## 1. Loop 缓存注入升级

- [x] 1.1 升级 `_inject_cache_control`（`src/hostlens/agent/loop.py`）：保持「`prompt_caching=False` 原样返回 / `system` 非空 list 时末块打 `cache_control: ephemeral`」，更新 docstring 说明该断点缓存的是 `tools+system` 静态前缀、且 tools 数组刻意不单独标记（design D-1）
- [x] 1.2 新增 `_roll_message_cache_breakpoint(messages, capabilities)`：`prompt_caching=False` 原样返回；否则返回 messages 浅拷贝并仅在最后一个 message 的最后一个 content block 注入 `ephemeral`（只浅拷贝被标记的末 message 与其末块，不 mutate 存储 messages）；**不**遍历清除历史块的 `cache_control`（快照式注入保证存储 messages 恒干净，清除是对不可能分支的防御，CLAUDE.md §6）；末 message `content` 非非空 block 列表时跳过（design D-2 / D-3）
- [x] 1.3 在 `run()` 的每次 `_call_with_retry` 调用前应用 `_roll_message_cache_breakpoint` 到当前 `messages`（与既有 `_inject_cache_control(self._system, ...)` 并列），确保两个注入都作用于请求快照、不 mutate `self._system` 与 `messages` 累积列表（design D-3）
- [x] 1.4 自查：确认任一请求断点数恒 ≤2，且 tools 数组全程不被注入 `cache_control`

## 2. CI 结构验证测试（FakeBackend，不烧 API）

- [x] 2.1 在 `tests/agent/test_cache_strategy.py` 加记录型 backend 包装（捕获每次 `messages_create` 的 `system`/`messages`/`tools` 入参副本），不污染生产 `FakeBackend`（design D-5）
- [x] 2.2 正例测试（按轮次分别断言，**不要在 turn1 断言 B**）：(a) **turn1** 请求（末 message 为裸 str intent）断言 system 末块有断点 A、tools 数组各项无 `cache_control`、且该 user message **无** B（裸 str 跳过）—— 对应 spec 场景「断点A在system末块且tools无断点」；(b) **turn2+** 请求（末 message 为 tool_result block 列表）断言断点 B 仅落在最新 message 末块、其余 message 无 `cache_control`、A 仍在 system 末块 —— 对应 spec 场景「滚动断点B只在最新message末块」
- [x] 2.3 断点预算测试：从 `run(intent)` 起连续 ≥5 轮 `messages_create`，断言每轮 `system+tools+messages` 的 `cache_control` 总数序列为 `[1, 2, 2, 2, 2, …]`（首轮末 message 为裸 str intent 跳过 B = 1，后续轮末 message 为 tool_result list = 2），且任一轮 ≤2、不随 turn 增长（对应 spec 场景「断点数恒不超过2且不随turn增长」）
- [x] 2.4 负例测试：`prompt_caching=False` 时断言 system/tools/messages 三处零 `cache_control`（对应 spec 场景「prompt_caching=False 三处零注入」）
- [x] 2.5 降级测试：末 message `content` 为裸字符串时断言 B 跳过、A 仍在（对应 spec 场景「末message为裸字符串时跳过断点B保留断点A」）

## 3. live 真实命中验证（opt-in）

- [x] 3.1 `@pytest.mark.live` 测试：真实 Anthropic API 跑 ≥3 轮，断言 turn2 与 turn3 `cache_read_input_tokens > 0`（对应 spec 场景「第二次调用真实命中静态前缀缓存」+「多轮真实命中持续有效」）；默认 `-m 'not live'` 跳过。**前置条件必须满足**：构造的 `tools+system` 静态前缀 token 数超过所用 model 最小可缓存阈值（避免短前缀假阴性）—— 显式 pad 一段稳定 system 越过阈值，二选一：用 `claude-haiku-4-5`（阈值 ≈2048，需 pad 到 ≥2048）或用 Sonnet（阈值 ≈1024）。不依赖真实 Planner 前缀恰好够大
- [x] 3.1b live 测试的断言**只覆盖静态前缀 A 与多轮聚合命中持续 > 0**，注释明确写出「不对 A/B 各自 read 量拆分归因（聚合值无法区分），B 正确性由第 2 节结构断言保证」—— 避免 live 过度宣称验证了 B（隐蔽自证）
- [x] 3.2 确认 cassette **不**被用作 cache_read 验收主证据（design D-5 反自证）；如已有相关 cassette 测试，仅保留其 API shape 回放兼容性用途

## 4. 文档

- [x] 4.1 新增 `docs/agent-cache-strategy.md`（简短）：`tools → system → messages` 前缀顺序图 + 断点 A/B 位置与 run 内/跨 run 生命周期 + 断点预算账本 + ephemeral 5 分钟 TTL 说明
- [x] 4.2 在 `docs/ARCHITECTURE.md §9` 加一行指向 `docs/agent-cache-strategy.md` 的 backlink（design 待解决问题）

## 5. 验收闭环

- [x] 5.1 `pytest -m 'not live'` 全绿（含第 2 节全部结构/负例/降级测试）
- [x] 5.2 `mypy --strict` 通过
- [x] 5.3 跑一次 proposal Demo Path 第 1–2 步确认可复现
- [x] 5.4 PR 前按 CLAUDE.md §5.3 对本变更跑对抗性 review（含运行时行为变更，应跑）
