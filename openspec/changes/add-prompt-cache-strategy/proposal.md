## 为什么

M2.2 的 `AgentLoop._inject_cache_control` 是一个**占位实现**：只在 `system` 末块打一个 `cache_control: ephemeral`，且当时刻意把「该缓存什么」推给 M2.5（见 `loop.py` docstring「choosing WHAT to cache is M2.5's job」）。现状的后果：

1. **多轮 loop 的对话前缀完全没缓存**。Planner 一次 run 通常 3–8 个 turn，每个 turn 都把「不断增长的 messages（含历次 tool_result）」原价重发。turn 越多，重复输入 token 越多，成本与延迟线性恶化 —— 而这恰恰是 Agent loop 最该用 prompt caching 的地方。
2. **缓存边界语义没有被显式规约**。当前 spec 只说「在 system 末块注入」，没有说明这个断点实际缓存的是 `tools + system` 这段跨 run 稳定前缀（因为 Anthropic 缓存前缀顺序是 `tools → system → messages`，system 断点天然吞掉前面的 tools）。这个机制不写进 spec，未来改 loop 的人很容易误加一个冗余的 tools 断点，浪费断点预算。
3. **没有任何测试守住「请求里到底有没有正确打上断点」**。M2.2 的两个场景只断言 system 末块有/无 `cache_control`，对 messages 滚动断点、对 `prompt_caching=False` 的负例覆盖都缺失。

CLAUDE.md §4.8 把 prompt caching 列为「必修」：「任何超过 5 次 LLM 调用的功能都要看 cache hit rate」。Planner 的单次 run 就跨越多次 LLM 调用，M2.5 是把这条红线在 Agent loop 里真正落地的节点。

## 变更内容

把 `AgentLoop` 的 `cache_control` 注入从「单断点占位」升级为**显式的两层缓存策略**，并配套验证与文档：

- **断点 A（静态前缀，跨 run 稳定）**：在 `system` 末块打 `cache_control: ephemeral`，缓存 `tools + system` 这段最长稳定前缀。明确规约：**不**在 tools 数组上单独打断点 —— system 断点已吞掉前面的 tools，单独标 tools 只会多一个冗余缓存条目、浪费断点预算（这是对现状语义的**澄清式收紧**，不是新增断点）。
- **断点 B（滚动对话前缀，run 内有效）**：每次调用 `messages_create` 前，在**当前** `messages` 列表最后一个 message 的最后一个 content block 上打 `cache_control: ephemeral`，让多轮 loop 内不断增长的对话前缀逐轮命中缓存。断点 B 是**滚动**的：每轮只标最新 message，历史 message 不留断点 —— 任一时刻请求里至多 2 个断点（A + B），稳定在 ≤4 预算内。
- **capability gate 不变且收紧负例**：`backend.capabilities.prompt_caching == False` 时**禁止**注入任何断点（A 和 B 都不注入），由 loop 端判定，禁止依赖 backend 静默丢弃（CLAUDE.md §4.11 规则 #2）。
- **验证（两层职责分离）**：
  - **CI 层（默认跑，不烧 API）**：用 `FakeBackend` 捕获 loop 实际发出的 request，断言「`prompt_caching=True` 时 system 末块有断点 A、tools 数组无断点；**仅当最新 message content 是非空 block 列表时（即 turn2+，turn1 裸 str intent 跳过 B）**最新 message 末块有断点 B；断点数序列 `[1,2,2,…]`」与「`prompt_caching=False` 时三处都没有任何 `cache_control`」。
  - **live 层（`@pytest.mark.live`，默认 skip，本地 opt-in）**：打真实 Anthropic API 跑 ≥3 轮，断言 turn2 与 turn3 的 `cache_read_input_tokens > 0` —— 这是唯一能证明「供应商侧真的命中缓存」的位置，但不进 CI 默认路径。**覆盖范围如实声明**：该断言**只覆盖静态前缀断点 A 的真实命中与多轮聚合命中持续有效**（`cache_read` 是单一聚合值，断点 B turn2 才 create、turn3 才首次 read，无法从聚合值干净分离 B 的贡献，故不做 A/B 拆分归因）；断点 B 的正确性由 CI 结构断言保证，不由 live 宣称。**前置条件**：测试构造的 `tools+system` 静态前缀必须超过所用 model 的最小可缓存阈值（Anthropic 对短 prompt 不缓存：Sonnet/Opus ≈1024、Haiku ≈2048 token），由测试显式 pad 保证，避免「实现正确但前缀过短」的假阴性。
  - **不**采用「录制 cassette + PlaybackBackend 断言 cache_read>0」作为验收主证据：cache_read 数值是录制时固化进 fixture 的，回放断言等于把要验证的结论预先写进 fixture（tautology）；cassette 仅作为 API shape 回放兼容性的辅助，不作 M2.5 验收主证据。
- **文档**：新增 `docs/agent-cache-strategy.md`（简短），画出 `tools → system → messages` 前缀顺序、两个断点的位置与 run 内/跨 run 生命周期、断点预算账本。

## 功能 (Capabilities)

### 新增功能

无新增 capability。本变更是对既有 `agent-loop` 缓存行为的规约升级，不引入新组件。

### 修改功能

- `agent-loop`: `cache_control` 注入需求从「只在 system 末块打一个断点」升级为「静态前缀断点 A（system 末块，缓存 tools+system）+ 滚动对话前缀断点 B（每轮最新 message 末块）」两层策略；显式规约 tools 数组不单独打断点、断点预算 ≤2/请求；capability gate 关闭时禁止注入任一断点的负例纳入需求。

## 影响

- **对外契约影响**：
  - **Agent tool schema**：无变更（tools 数组内容/顺序不动，只是不在其上加 `cache_control`）。
  - **Inspector / MCP / Notifier / Schedule / CLI 契约**：均无变更。
  - **`LoopResult` / `LoopUsage` schema**：无变更（`cache_creation_input_tokens` / `cache_read_input_tokens` 字段 M2.2 已存在，本变更只是让它们在多轮场景下真正非零）。
- **代码**：
  - 改：`src/hostlens/agent/loop.py` —— `_inject_cache_control` 升级 + 新增滚动 message 断点注入（在每次 `_call_with_retry` 调用前应用到当前 `messages`）。
  - 改：`tests/agent/` —— 新增 `FakeBackend` 结构断言测试（正例 + `prompt_caching=False` 负例）+ `@pytest.mark.live` 真实命中测试。
  - 新增：`docs/agent-cache-strategy.md`。
- **依赖**：无新增依赖。
- **不触碰**：`PlannerAgent`（其「system 必须是单元素 text block 列表」需求 M2.4 已规约，断点 A 继续依赖该前提，本变更不改 planner spec）；`backend.py` / 各 backend 实现（capability gate 与 `check_capability_consistency` 兜底逻辑不变）。

## 非目标（Non-Goals）

- **不**实现 cache hit rate 的运行时指标采集/上报（structlog 字段、OTel span 属性）。M2.5 只保证断点被正确注入 + 字段被正确累加；把 cache_read/creation 做成可观测指标面板留给后续可观测性专项。
- **不**做跨 run 的持久化缓存或自建缓存层。只用 Anthropic 原生 ephemeral（5 分钟 TTL）prompt caching，不引入任何本地缓存存储。
- **不**引入 `cache_control` 的 `ttl` 调参（1h 缓存等）。M2 范围只用默认 ephemeral。
- **不**改 tools 数组的内容、顺序或生成逻辑（byte 稳定性是 M2.3 既有前提，本变更假设其成立，不重新规约）。
- **不**给 Diagnostician（M3）或 MCP server（M7）设计缓存策略 —— 那些 Agent/surface 引入时各自 propose。
- **不**实现 cassette 录制工具链（M2.6 / 未来独立 proposal）。

## Failure Modes

1. **`messages` 末 message 的 content 为裸字符串而非 block 列表**：Anthropic 允许 `content` 是 `str`，此时无处挂 `cache_control`。降级行为：断点 B 注入时若发现末 message content 不是非空 list，则**跳过 B**（不强行把 str 转 list，避免改变 wire 语义），断点 A 仍生效，run 正常继续。结构测试覆盖此分支。
2. **`prompt_caching=True` 但 `system` 是裸字符串/空 list**：断点 A 无处可打（沿用 M2.2 行为，跳过 A）。若同时 messages 可标，则只有 B 生效。不报错、不降级整个 run。Planner 路径下 system 恒为非空 list（M2.4 已规约），此分支只在直接构造 `AgentLoop` 的测试/未来调用方出现。
3. **滚动断点实现误把 mark 写回存储 messages**：会导致历史断点累积、请求断点数随 turn 增长、超过 4 个被 API 拒绝（或缓存碎片化）。防御：注入只在 messages 浅拷贝的末块上标记、从不 mutate 存储 messages（快照式注入），故存储恒不带 `cache_control`、无需「清除历史断点」步骤；用「断点数序列为 `[1,2,2,…]`（首轮裸 str intent 跳 B）、恒 ≤2 且不随 turn 增长」的测试守住该不变量。
4. **backend capability 与注入不一致**：loop 端 gate 判错（如 `prompt_caching=False` 仍注入）。兜底：backend 的 `check_capability_consistency` 会 raise `BackendCapabilityViolation`（M2.1 既有），fail-loud 暴露 bug，不静默。
5. **真实 API 未命中缓存（cache_read 恒 0）**：可能因前缀 < 最小可缓存 token 数（Anthropic 对短 prompt 不缓存）、或 tools/system 渲染非 byte 稳定。live 测试会捕获；CI 结构测试不受影响（结构正确 ≠ 一定命中，二者职责分离，这正是不靠 cassette 自证的原因）。

## Operational Limits

- **断点预算**：任一 `messages_create` 请求至多 2 个 `cache_control` 断点（A + B），恒定 ≤ Anthropic 上限 4，不随 turn 数增长。
- **内存预算**：滚动断点注入只做 messages list 的浅拷贝（O(n) 引用拷贝）+ 末 message 与末块字典浅拷贝（O(1) 新结构），**不遍历内容块、不深拷贝 message 正文**；与 loop 每轮已有的 messages 组装同阶，不引入额外正文复制。
- **并发预算**：无变更 —— 缓存注入是单次请求组装的纯函数步骤，不引入新并发。
- **超时设置**：无变更，沿用 `agent.*` 既有 timeout/budget。
- **TTL**：ephemeral 缓存 5 分钟 TTL，由 Anthropic 侧控制；超过 TTL 的下一次 run 重新 cache_creation（成本一次性，符合预期）。

## Security & Secrets

- **不引入新密钥**：纯请求组装变更。
- **不扩大攻击面**：`cache_control` 是发往 Anthropic 的元数据 key，不携带任何用户/凭据数据；缓存内容就是本来就要发送的 tools+system+messages，缓存不改变数据出域范围。
- **脱敏**：无新增脱敏需求 —— 缓存的是已组装好的 prompt，脱敏边界在更上游（report redaction / secrets 注入层），本变更不触碰。
- **注意点**：滚动断点会让「对话前缀」被 Anthropic 缓存 5 分钟。若 messages 中含敏感 tool_result，这些内容本来就已发往 API；缓存不增加新的暴露面，但文档需说明「ephemeral 缓存驻留 ≤5 分钟」这一事实供合规评估。

## Cost / Quota Impact

- **方向：显著降本**。Planner 单 run 多轮场景下，tools+system 静态前缀从「每轮全价」变为「首轮 cache_creation（1.25× 单价）、后续轮 cache_read（0.1× 单价）」；滚动断点让对话前缀也享受同等折扣。turn 越多收益越大。
- **API 调用频次**：无变化（缓存不增减调用次数，只降低每次调用的有效输入计费）。
- **首轮成本**：cache_creation 比无缓存略贵（写缓存约 1.25× 输入单价），但单次一次性，被后续 cache_read 折扣迅速摊平（典型 ≥3 轮即净赚）。
- **配额影响**：降低有效输入 token 计费，对 Anthropic input token 配额是净正面。
- **可见性**：`LoopUsage.cache_read_input_tokens` 在多轮 run 后应 > 0，可在 `LoopResult` 中观察（M2.5 不做面板，但数据已可读）。

## Demo Path

无需 SSH、无需付费 API（cassette/Fake 路径优先）：

```bash
pip install -e ".[dev]"

# 1) CI 结构验证（不烧 API）：断言 loop 正确注入 A+B 断点、负例不注入
pytest tests/agent/test_cache_strategy.py -v          # 全绿，含 prompt_caching=False 负例

# 2) 全套 CI 默认路径仍绿（不含 live）
pytest -m 'not live'

# 3)（可选，本地 opt-in，需 ANTHROPIC_API_KEY）真实命中验证（≥3 轮，前缀已 pad 越过 model 阈值）
ANTHROPIC_API_KEY=sk-... pytest -m live tests/agent/test_cache_strategy.py -v
#    → 断言 turn2 与 turn3 的 cache_read_input_tokens > 0（覆盖静态前缀 A + 多轮聚合命中持续；不拆分 A/B 归因）

# 4) 读文档理解策略（面试现场可直接展示）
cat docs/agent-cache-strategy.md                      # tools→system→messages 前缀图 + 两断点生命周期
```
