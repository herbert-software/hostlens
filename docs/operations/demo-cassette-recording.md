# Demo 场景 cassette 重录约定

面向 contributor 的指南：**什么时候必须重录 demo 场景 cassette、怎么重录、为什么**。

`hostlens demo run` 是完全离线的全链演示（Planner → Diagnostician → Report）。
8 套场景的回放资产在
`src/hostlens/demo/scenarios/<key>/{fixture.json, cassette.jsonl}`：

- `fixture.json` —— `ReplayTarget` 的命令录制（target 侧）
- `cassette.jsonl` —— `PlaybackBackend` 的 LLM 录制（**单份文件同时含 Planner 段与
  Diagnostician 段的 record**）

这两段 record 在同一份 cassette 里靠 **request key** 区分（key 对 `{model, messages,
tools_count}` 取规范化指纹，含完整 `messages` 数组）。**诊断段的 request key 隐式依赖
Planner 段的输出**——这是下面所有约定的根因。

录制不需要任何真实 Anthropic API key：录制机制是
`RecordingBackend(cassette_path, inner=FakeBackend(responses=build_authored_responses(scenario)))`，
`RecordingBackend` 只负责**捕获活体 pipeline 组装的真实 request**，model 响应由
手写 authored 脚本（`build_authored_responses`）提供。**禁止手工编写 request
record**——request key 必须由活体 pipeline 自动捕获，否则任何字节漂移都会在回放期
变成 `CassetteMiss`。

## 涉及的关键文件

| 文件 | 作用 |
|---|---|
| `tests/incidents/_harness.py` 的 `build_authored_responses` | 每套场景的 authored 响应脚本（Planner 段 + 诊断段 `MessageResponse`）；SOT |
| `tests/incidents/_generate.py` | 重录入口（跑全链、`RecordingBackend` 捕 request、整文件覆写 cassette） |
| `src/hostlens/demo/scenarios/<key>/cassette.jsonl` | 重录产物（Planner + 诊断段 record） |

## 约定 1：改 Planner authored 响应 / fixture 必须整体重录该场景 cassette

改某场景的 **Planner authored 响应或 `fixture.json`** 时（即便是看起来"无关"的小改，
只要导致某条 finding 的 message 文本或顺序变化），诊断段 record 的 request key
就会漂移 → 回放期 `CassetteMiss`。

原因：诊断段首条 user message 由 Planner 段产出的 seeded findings 渲染而成，这些
findings 的文本 / 顺序 / 序列化全部参与诊断段 request key 的指纹。

**做法**：用扩展后的 `build_authored_responses` **整体重录该场景 cassette**
（Planner 段 + 诊断段一起，经 `tests/incidents/_generate.py` 的全链录制路径）。
不要只改 Planner 段而留下旧的诊断段 record。

确定性重录会让 Planner record 与历史版本 **byte-identical**（authored Planner
响应不变 + 冻结时钟 + 录制侧 Settings 一致），所以"重录"对 Planner record 是无害的；
诊断段 record 则按新的 seeded findings 重新捕获。重录后用 `git diff` 核对 Planner
record 行确实零变化。

> 注：当前 `wire-demo-to-report` 变更的 Non-Goal「本次不改 Planner record 内容」
> 只约束那次变更，**挡不住未来的改动**——所以这条约定对后续 contributor 长期有效。

## 约定 2：改 D-7 排序键 / `_render_findings_block` 渲染字段必须连 authored label 一并重写

诊断段 authored 响应里 `correlate_findings.supporting_findings` 引用的是
**ordinal label（如 `["F1", "F3"]`），不是 finding id**。F1 / F2 这些位置标签由
seeded findings 的**排序后顺序**位置式分配——这个排序由共享核心的稳定排序键
（design D-7）确定，使 F1 / F2 跨 run 稳定、authored `[F#]` 引用指向固定 finding。

排序键是 `_render_findings_block` 渲染投影的**超集**。因此当你改动：

- **D-7 排序键**，或
- **`_render_findings_block` 的 per-finding 渲染字段**（例如 M3 evidence DSL 改成渲
  evidence body 并同步进键）

时，sort 顺序可能重排 → 同一条证据 finding 从 F1 挪到 F2 → **所有 authored
`[F#]` label 错位**。

**危险点**：录制期用的是 `FakeBackend`，它**没有自纠错**（只会吐出脚本里的下一条
响应）。authored label 悬空时，该 `correlate_findings` 调用被静默 bounce →
`harvest_hypotheses` 跳过它 → **录成 0 假设的 cassette**（渲染成
`_暂无根因假设_`）。这不会在录制期 loud-fail，是个静默错误。

**做法**：改这类排序键 / 渲染字段时，**必须对照新排序重写各场景的
`supporting_findings` label，并整体重录全部 8 套 cassette**。重录脚本会打印该场景
排序后的 seeded findings 列表，照着写新的 `[F#]` 引用。

## 这两条耦合的常驻守护：8 套全链 e2e 回放

`tests/incidents/` 的 **8 套场景全链离线回放 e2e** 是上面两条约定的常驻回归网，每套断言：

- `replay_target.misses == []`（无 cassette / fixture 漂移）
- Report 含 **≥1 hypothesis**（**liveness 守护**：抓"诊断段录成 0 假设"，**不**评估
  假设质量——假设内容由 cassette 录死，录什么有什么）

未来谁改了某场景的 Planner 资产却忘了重录诊断 record，或改了排序键 / 渲染字段却忘了
重写 authored label，这套 e2e 会立刻红。**它必须在 CI 常驻。** 但它只是兜底——上面
两条约定仍须由 contributor 主动遵守，别指望 e2e 替你记住重录。
