> 诊断师 system prompt 新增「根因推理纪律（抗过度归因）」约束 + few-shot 范例，覆盖真机暴露的两类幻觉（独立历史失败被编连锁、瞬时单样本被编持续根因）。在既有「中文叙述」等约束之上叠加，不改诊断师只读架构。

## 新增需求

### 需求:诊断师根因推理必须 grounded、抗过度归因（勿无证据编因果连锁 / 勿对瞬时单样本编持续根因）

诊断师（`agent/prompts/diagnostician.md` system prompt）产根因假设时**必须**遵守以下 grounding 纪律（在既有中文根因叙述、序号标签引用等约束之上）。约束**必须**写入 **system prompt**（固定文本、走 prompt cache，§4.8），**禁止**仅在测试场景注入——否则生产环境无反面教材保护、真机已暴露的幻觉易复发。

- **失败默认独立**:多个 failed/异常信号**默认彼此独立**;只有在有**具体可观测的共享证据**（同一时间窗先后发生 / 同一依赖链 / 同一错误信息 / 明确的 systemd `Requires=`/`After=`）时才可提因果。**禁止**无共享证据把独立信号编成「连锁崩溃 / 级联故障 / 雪崩」——宁可输出多条独立假设。
- **历史 vs 近期**:**必须**结合 finding 携带的时间锚（systemd 单元 `Type` 与失败时刻 `inactive_monotonic_us`、系统 `uptime_seconds`）判断信号新鲜度;高 uptime 主机上 `oneshot` 类（cloud-init/cloud-config/cloud-final/networking）在开机窗口内失败**应识别为 provisioning 历史残留**、紧迫度低,**禁止**叙述为「刚崩」或据此推断当前网络故障。
- **相关 ≠ 因果**:措辞**必须**区分「同时存在/同为 failed」与「X 导致 Y」;主张因果**必须**给机制证据（依赖关系 / 时间先后 / 错误传播），否则只陈述共现。
- **瞬时单样本不得编持续性根因**:负载类信号仅 1 分钟均值（`load1`）偏高而 5/15 分钟均值正常时，是**瞬时尖峰**,**禁止**据此推断「磁盘 I/O 阻塞 / 内存压力 / CPU 密集」等**持续性**根因（这些需持续性证据：持续高 `load15`、非零 iowait、swap 活动、低 idle）;已被证据排除的根因**禁止**列出。
- **置信度与证据匹配**:`confidence` **必须**与证据强度对应——`high` 须有直接机制证据;缺时间/依赖证据、信号互相独立时**不得**为 `high`;证据不足以支撑任何假设时**必须**如实说明「未发现需处置异常」,**禁止**为产出而编造。

prompt **必须**含一个 few-shot 范例（进 system prompt）示教正确形态:对「多台主机各有独立历史 systemd 失败 + 一台单核机 load 瞬时尖峰」，正确输出是 **N 条独立、识别为历史/瞬时、低紧迫的假设**，而非 1 条高置信「连锁崩溃」统一根因;范例**必须**含反面（标注为何错）与正确对照。

#### 场景:多台独立历史失败不得编成连锁（病 1，诊断师层=prompt 行为要求，**LLM 输出质量、非机器断言**）
> 这是对 LLM 输出的**行为要求**，本质不可逐字机器断言。**强确定性锚在 inspector 层**（cloud-init oneshot+开机窗口+高 uptime → `warning`，见 os-shell delta）——那才是防回归主力。本诊断师场景的验收靠:① few-shot 范例进 system prompt（示教正确形态、走 cache）② 重写 `systemd_failed` 单主机 incident 场景：其 **authored Planner 叙述**（`_scenarios.py` `scenario.narrative`，**会进 snapshot**——`project_planner_result` 渲染 Planner 叙述+findings+tokens）写成「两条独立 critical、不编连锁」、不含「连锁/级联/雪崩」,由 snapshot 锚;诊断师 **`hypothesis` 字面**（`_scenarios.py` `scenario.hypothesis`，**不进 snapshot**、只供 cassette 录制）同样 authored 成独立叙事，靠**源码 review/grep** 锚 ③ 真机 ts.mac-mini Demo Path 人工抽检。诊断师 **`confidence` 与 `supporting_findings`** 现可**按场景 author**（`IncidentScenario.diag_confidence` / `diag_supporting`，默认 `high` / `("F1",)` 保其余 7 场景不变）——`systemd_failed` 取 `medium` + 引 `F1`/`F2`（hypothesis 同时讨论 nginx 与 mysql 须引两标签、独立信号无机制证据按纪律 5 不得 `high`），合规录入 cassette。但 incident 快照投影**仍不含** hypotheses/confidence/supporting，故诊断师置信度/标签**仍不做机器断言**：cassette 录入的是正确行为示范，靠**源码 review + few-shot + 真机 Demo Path** 锚，而非 snapshot 逐字断言。
- **当** 主机有彼此无关的 `oneshot` 历史/开机一次性 systemd 失败（finding 已带 `Type`/失败时刻、severity 已被 inspector 降为 `warning`）、系统 uptime 高、无共享因果证据
- **那么** 诊断师**应**把它们视为各自独立的历史残留、紧迫度低（few-shot 示教「多条独立、低紧迫假设」而非单条高置信「连锁崩溃」统一根因）;authored 场景的根因叙述**不得**含「连锁/级联/雪崩」式无证据因果编织

#### 场景:瞬时 load 尖峰不编持续性根因（病 2 的诊断师层兜底）
> 病 2 的**主**修复在 inspector 层（`linux.system.load_avg` 改用 load5/15 持续门控 → tg-bot 那种瞬时尖峰**直接零 finding**，诊断师拿不到信号去乱猜）。本场景是**兜底**——针对仍会冒出 load-ish finding 的其它面（多核机踩 warn 阈、或别的 inspector 的瞬时信号 + 证据已排除持续性原因）。
- **当** 某 finding/证据显示负载/资源类信号仅瞬时偏高（如 `load1` 高而 `load5`/`load15` 正常）、且持续性证据已被排除（iowait/swap/idle 正常）
- **那么** 诊断师**禁止**输出「磁盘 I/O 阻塞 / 内存压力 / CPU 密集」等持续性根因假设;**应**说明其为瞬时尖峰、不构成根因（或不产该假设）
