# Agent Loop 的 Prompt Caching 策略

> `AgentLoop`（`src/hostlens/agent/loop.py`）每个 turn 调一次 `backend.messages_create(system, messages, tools, ...)`。本文说明这些请求上的 `cache_control` 断点怎么打、缓存的是哪段前缀、命中时序如何。

## 1. 前缀顺序与两段可缓存前缀

Anthropic 把请求按固定顺序拼成可缓存前缀，命中规则是**最长匹配前缀**：

```
  ┌─────────────┐   ┌────────────┐   ┌──────────────┐
  │  tools 数组  │ → │  system 块  │ → │  messages 块  │
  └─────────────┘   └────────────┘   └──────────────┘
  └────────── 跨 run 稳定 ──────────┘   └─ run 内增长 ─┘
         （静态前缀）                      （滚动对话前缀）
```

- `tools + system`：跨 run byte 稳定（Planner 把 system 渲染成单元素 text block 列表且跨 run 稳定，tools 数组内容/顺序固定）。
- `messages`：run 内逐轮增长（每轮 append assistant 的 `tool_use` 与对应 `tool_result`）。

一个 `cache_control` 断点标记「一段可缓存前缀的终点」：API 缓存从请求开头到该断点（含）的所有内容。

## 2. 断点 A —— 静态前缀（缓存 tools + system）

断点 A 打在 **system 的最后一个 block** 上：

```python
system = [ {... text ...}, {... text, "cache_control": {"type": "ephemeral"} } ]
#                                       ^ 断点 A
```

因为前缀顺序是 `tools → system`，在 system 末块打断点**已经把前面的 tools 一并纳入缓存前缀**。所以：

- **tools 数组不单独打断点**。Hostlens 的 tools 与 system 都跨 run 稳定，是一段连续的稳定前缀，一个断点即可全缓存。单独给 tools 标断点只会多创建一个 `tools` 缓存条目、多花一个断点预算和一次额外 cache-write，却没有任何收益（分离只在「tools 稳定但 system 会变」时才有意义，Hostlens 两者都稳定）。

`system` 是裸字符串或空 list 时跳过断点 A（不报错）。Planner 路径下 system 恒为非空 list。

## 3. 断点 B —— 滚动对话前缀（缓存 messages）

每次调 `messages_create` 前，断点 B 只标**当前 `messages` 最后一个 message 的最后一个 content block**：

```python
messages = [ user, assistant, {... tool_result, "cache_control": {"type":"ephemeral"} } ]
#                                                  ^ 断点 B（只标最新末块）
```

- **滚动而非累积**：每轮只标最新 message，历史 message 不留断点。配合 API 的最长前缀匹配（允许在断点前增量回看），**典型短 run 下** turn N 的请求只需在末块打断点，就能命中 turn N-1 写入的更短对话前缀（B 命中的前提与边界见 §4 末「保守声明」）。
- **无需清除历史断点**：断点注入只作用于「即将发出的请求快照」——在 messages 浅拷贝上标记末块，**从不写回 loop 存储的 messages**。因此存储 messages 恒不带 `cache_control`，历史断点根本不会出现，无需也不该再做「全体清除」归一化（那是对不可能分支的防御）。
- **降级**：若末 message 的 `content` 是裸 `str`（无处挂断点）→ 跳过 B（不强转 list、不报错），断点 A 仍生效。首轮裸 str intent 即走此降级。

## 4. 生命周期与命中时序（Planner 典型 run）

turn1 的末 message 是 `{"role":"user","content":intent}`，content 是裸字符串，按降级规则跳过 B；turn2 起末 message 是 list 形态的 `tool_result`，B 生效。

| turn | 末 message | 断点 A | 断点 B | cache_read | 断点数 |
|---|---|---|---|---|---|
| turn1 | user(intent=裸 str) | cache_create | 跳过（裸 str） | 0（冷启） | **1** |
| turn2 | tool_result（list） | cache_**read** | cache_create | > 0 ✅ | **2** |
| turn3 | tool_result（list） | cache_**read** | cache_read †（典型短 run） | > 0 ✅ | **2** |
| … | … | read | read † | > 0 | 2 |

> † 断点 A 的 `cache_read` 是确定的（静态前缀复用）；断点 B 的 `cache_read` 标 † 表示**典型短 run 下成立**，超回看窗口的极长对话可能仅 create 无 read（见下「保守声明」）。表中 `cache_read > 0` 一列由 A 保证，不依赖 B。

- **断点数序列 `[1, 2, 2, …]`**：恒 ≤ 2，永不随 turn 增长。
- **断点 A** 从 turn2 起命中（`cache_read > 0`）——静态前缀复用，这是「第二次调用 cache_read > 0」的来源。
- **断点 B** 在 turn2 是 **create**（首次写入对话前缀缓存），其 read 收益**从 turn3 才开始**（turn2 非 read）。

> **关于断点 B 的 read 收益（如实声明，未被 live 测试单独证实）**：B 在 turn3 能否真正 cache_read，依赖 Anthropic 的「最长前缀匹配 + 断点前 ~20 block 回看」语义跨断点位置发现 turn2 写入的对话前缀缓存。典型短 run（B 位置间隔 < 20 block）成立；极长对话超出回看窗口时，B 可能只有 create 无 read。即便如此，**静态前缀 A 的 read 收益不受影响**。live 测试只断言聚合 `cache_read > 0`（由 A 满足即可），并不单独验证 B 的 read，故这里对 B 多轮省钱的预期保持保守表述。

## 5. 断点预算账本

| 项 | 值 |
|---|---|
| Anthropic 单请求断点上限 | 4 |
| Hostlens 任一请求断点数 | ≤ 2（断点 A + 断点 B） |
| 随 turn 增长？ | 否（恒定 ≤ 2） |

稳定地落在预算内，不会因多轮对话累积断点而被 API 拒绝或导致缓存碎片化。

## 6. TTL

只用默认 **ephemeral 缓存，TTL 5 分钟**，由 Anthropic 侧控制（不调 `ttl` 参数）。超过 TTL 的下一次 run 会重新 `cache_creation`（成本一次性，符合预期）。对话前缀因此在 Anthropic 侧驻留 ≤ 5 分钟——缓存内容本就是要发送的数据，不扩大数据出域范围。

## 7. 成本方向

静态前缀（tools + system）从「每轮全价」变为「首轮 cache_creation（约 1.25× 输入单价）、后续轮 cache_read（约 0.1× 单价）」——这部分收益确定。滚动断点 B 让对话前缀**在典型短 run 下也享受同等折扣**（B 的 read 边界见 §4 保守声明；极长对话可能 B 只 create 无 read，但 A 收益不变）。≥ 3 轮 run 即净省，turn 越多收益越大。

## capability gate

仅当 `backend.capabilities.prompt_caching == True` 时注入断点 A 与 B；为 `False` 时 `system` / `tools` / `messages` 三处零注入。注入判定在 loop 端完成，backend 严禁静默丢弃 `cache_control`。
