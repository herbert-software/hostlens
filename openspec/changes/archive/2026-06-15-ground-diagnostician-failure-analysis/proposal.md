> 经代码勘察 + SRE 拍板 + 一轮对抗 review 精化。范围**拓宽**为真机 ts.mac-mini 同一次巡检的**两类诊断质量误判**（systemd 历史失败被编成「连锁崩溃严重」+ 单核机 load 尖峰被误标「严重」）。**review 修正了关键 targeting**：病 2 的真凶是 `linux.system.load_avg`（已 per-core 但门控用 `load1` 单次采样），非 `system.uptime`。

## 为什么

真机 ts.mac-mini 6 台 fleet 巡检的根因分析**误判且误导**，两类病：

**病 1 — systemd 历史失败被编成「网络连锁崩溃 严重」**：报告定级「严重」、根因写「网络初始化故障导致 cloud-init 及网络服务连锁崩溃（置信度 高）」。实测：6 台全在线、稳定运行 36 天、负载≈0、SSH/Tailscale 全通。各 systemd failed 单元**彼此独立、多是开机一次性/历史残留**：`cloud-config`/`cloud-final`/`cloud-init`（aliyun-hk/vultr）是 `Type=oneshot`、只在首次开机跑一次、provisioning 出岔就**永久** `failed`，系统早跑了几十天；`zerotier-one`/`networking`/`hysteria-server` 各自独立。诊断师把独立陈旧信号硬编成统一「连锁崩溃」——**幻觉**，误导运维查根本不存在的网络故障。

**病 2 — 单核机 load 尖峰被误标「严重」**：tg-bot（1 核，Mastodon 全家桶 + 多 bot + redis + cloudflared + 容器）18:18 抓到 `load1=2.45` 标 critical。根因在 **`linux.system.load_avg`**：它已按 per-core 判定（`load1/ncpu >= 2.0 → critical`），但门控用的是 **`load1`（单次 1 分钟采样）**——tg-bot `2.45/1 = 2.45 >= 2.0` → critical。但首查 0.61/0.41/0.36、复查 0.17/0.31/0.33——`load15` 才 0.33，尖峰只持续约 1 分钟就过了；报告自标「置信度 低」却仍硬猜「磁盘 I/O 阻塞 / 内存压力 / CPU 密集」，实测 `vmstat wa=0` / 可用 5.6GB swap 0 / idle 91-95% 全排除。**单核机负载短暂过 2 是常态、非故障。**

## 根因拆解

| 层 | 病 1（systemd） | 病 2（load） |
|---|---|---|
| **inspector 判据缺口** | `linux.systemd.failed_units` 只报「哪些 failed」、不报「何时 failed / 单元类型」，且**任何** failed → `critical`（无时间锚、一刀切） | `linux.system.load_avg` 已 per-core（`load1/ncpu`，crit 2.0 / warn 1.0），但**门控用 `load1` 单次采样**——已采的 `load5`/`load15`（持续信号）**没用上**，瞬时尖峰即触发 |
| **诊断师过度归因** | 把独立、陈旧的 oneshot 残留编成「统一根因 + 连锁链」，不分「相关」与「因果」、置信度与证据不匹配 | 对自标「置信度 低」的瞬时单样本，硬编「持续性根因」（disk-I/O / mem / cpu）而无持续性证据 |

两病同根：**诊断师缺时间/持续性维度 + 过度归因，inspector 判据缺持续性/时间校准**。

## 变更内容

> 三线（load 持续门控 / systemd 时间锚+severity / 诊断师 prompt 抗幻觉）。

1. **load 改用持续信号门控**（`linux.system.load_avg`，病 2）：把 findings 的门控从 **`load1/ncpu`** 改为 **`load5/ncpu` 且 `load15/ncpu`**（持续过载，单次 `load1` 尖峰不再触发；`load5`/`load15` 已在 collect 内、无需改采集）；per-core 阈值 `crit=2.0` / `warn=1.0`（沿用现值但 **`parameters` 化**可按机型调）；`load1` 保留在 output 供展示、**不再决定 severity**。message 改 + version bump（保持英文、留 `_BACKLOG`，无 allowlist 变更）。**不动 `system.uptime`**（另一个固定阈值 load-avg inspector，非本次真凶，其 load1-only 是独立 latent 项、本提案非目标）。
2. **systemd inspector 时间锚 + severity 校准**（`linux.systemd.failed_units`，病 1）：collect 补每单元 `Type` + 失败时刻 `InactiveEnterTimestampMonotonic`（开机以来微秒）+ **系统 `uptime_seconds`**（从 `/proc/uptime` 首字段，**纯 POSIX、不用 gawk `systime()`**）；severity：**`oneshot` 且失败在开机窗口（默认 180s）内 且 `uptime_seconds >= min_uptime_seconds`（默认 3600，确保是「长跑机的开机历史残留」而非「刚重启后的当前失败」）→ `warning`**，其余 → `critical`。per-unit `for_each`（迭代键 `results`，os-shell 套件契约）。version bump。
3. **诊断师 prompt 抗幻觉**（`agent/prompts/diagnostician.md`，覆盖病 1+病 2）：新增「根因推理纪律」5 条 + 反面→正确 few-shot（进 system prompt 走 prompt cache）。病 2 的 inspector 层修复（零 finding）是**主**，prompt 层「瞬时单样本不编持续根因」是对其它仍会冒出 load-ish finding 的面的**兜底**。

## 非目标（Non-Goals）

- **只动 `linux.system.load_avg` + `linux.systemd.failed_units` 两个 inspector + 诊断师 prompt**，不碰其它 ~70 个 inspector 的诊断质量校准；**不动 `system.uptime`**（独立 latent 项）；不统一两个 load inspector。
- **不改报告渲染 / 主机归因 / 布局**（已落地 `improve-fleet-report-attribution-and-layout`）。
- **不引入联网时间服务**——时间锚走 `/proc/uptime` + systemd monotonic，纯本机、纯 POSIX。
- **不改 inspector `name`**（仅 bump version）；**不动 routing/聚合层**（报告级 severity = `max(finding.severity)`，inspector 侧降级即修报告头）。
- **不引入新基础设施**——守 os-shell 套件「现有 schema 字段集内完成」契约（kv/json parse / for_each / parameters 均已存在）。
- **不改 §4.2 inspector 不调 LLM / 诊断师只读架构**（ADR-008）。

## 影响

- **契约**：`os-shell-inspector-suite`（load 持续门控 + systemd Type/时间锚/uptime 字段 + severity 分级）、`diagnostician-agent`（prompt grounding）。
- **代码**：`inspectors/builtin/linux/system_load_avg.yaml`、`inspectors/builtin/linux/systemd_failed_units.yaml`、`agent/prompts/diagnostician.md`。
- **重跑（细化清单见 design）**：两个 inspector message 改 + version bump → **finding-id churn**；**load**：`cpu_saturation` demo cassette/snapshot、`incidents/_scenarios.py` load 条目、i18n backlog 复核、`health_default_set`；**systemd**：`test_systemd_failed_collector.py`（重写 collector + 重建 JSON-escape 回归）、`systemd_failed` incident（fixture stdout 改新 `results` 形 / snapshot 随 per-unit findings + authored Planner 叙述重生成 / `scenario.hypothesis` 字面 authored 成「两条独立 critical、不编连锁」；诊断师 `confidence`/`supporting_findings` 现可按场景 author（`_harness.py` `IncidentScenario.diag_confidence`/`diag_supporting`，默认 `high`/`("F1",)` 保其余 7 场景不变），`systemd_failed` 取 `medium`+引 `F1`/`F2` 合规纪律；但 snapshot 仍不渲 hypothesis/confidence，故仍不机器断言）、i18n allowlist（systemd 唯一成员，每条 FindingRule message 保留 CJK + 仅注入已声明字段）；**prompt**：诊断师 VCR cassette 重录。
