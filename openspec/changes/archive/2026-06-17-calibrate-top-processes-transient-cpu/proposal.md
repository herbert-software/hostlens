## 为什么

今日巡检 `bandwagon` 报 🔴 严重:`Process journalctl (pid 33948) is using 100% CPU (linux.cpu.top_processes)`。真机核实为**误报**:pid 33948 已消失、load `0.00/0.00/0.00`、`journalctl --verify` 全 PASS、无 crontab/systemd timer/常驻监控 agent;pid 落在巡检窗口(服务器本地 ~17:00 PDT)同窗口 SSH 会话进程序列里,是一次性短命进程。建议的 `kill 33948` / `--vacuum-time` 全打空(进程已死、journal 健康)。

根因(已查实):`linux.cpu.top_processes` 用 `ps -eo pid,pcpu,... | head -n 10` 单次快照。procps-ng `ps(1)` 对 `%cpu` 的定义是 **"the CPU time used divided by the time the process has been running (cputime/realtime ratio)"** —— 即「累计 CPU 时间 ÷ 进程自诞生的存活时长」,**不是瞬时利用率**。一个刚 spawn、几乎全程吃满单核活约 1–2s 的进程(如 journalctl 扫 968MB journal)单次快照即读到 `~100%`,触发 finding 的 `float(p.cpu_pct) >= 90.0 → severity: critical`,缺任何持续性门控。更糟的是:同一轮巡检里别的 inspector(systemd 失败 / log 域)本就会对 journal 跑 journalctl,这些**自起**的短命进程被本采样器抓到(「观察者抓到自己」),使该误报**结构性必然复发**。

这与已落地的 `linux.system.load_avg`「单样本 load1 误报」**同源**——单维瞬时尖峰 → 升级 `critical`,缺持续性门控。本提案沿用同一解药。

## 变更内容

给 `linux.cpu.top_processes` 叠加「进程存活时长」门控,作为「持续占用」的代理信号(类比 load_avg 用持续信号 load5/load15 取代单样本 load1):

- collect 命令加 `etimes`(进程存活秒数,procps 整数)字段
- output_schema 加 `etimes` 列
- 新增 parameter `min_etimes`(`number`,`exclusiveMinimum: 0`,默认 `10`)
- 两条 finding 的 `when` 前置 `int(p.etimes) >= min_etimes`

`min_etimes` 默认 `10` 的取舍:既滤掉 `etimes` 0–2s 的一次性 journalctl,又不像 60s 那样让真正刚跑飞的进程(fork 炸弹 / 启动即失控服务)隐身一整分钟,伤本 inspector「诊断 CPU saturation」的使命。

## 功能 (Capabilities)

### 新增功能
（无）

### 修改功能
- `os-shell-inspector-suite`: `linux.cpu.top_processes` 的 finding 门控从「单次 `ps %cpu` 快照」改为「叠加进程存活时长(`etimes`)闸」,消除年轻进程的 `%cpu = cputime/realtime` 伪影误报

## 影响

- 代码:`src/hostlens/inspectors/builtin/linux/cpu_top_processes.yaml`(collect / output_schema / parameters / findings;`version` bump → finding-id churn,regression diff 旧 id resolved + 新 id added)
- 测试:对应 snapshot / 命令串级锁测试新增 `etimes` 列断言
- 无新依赖、无 API 变更;非 breaking(消费方拿到的 finding 只会变少不会变形)

## 非目标 (Non-Goals)

- **severity 多核重标定**:多核机上「单进程占满 1 核 → 🔴 严重」是另一种过度归因(应按 `ncpu` 归一或 `critical→warning`),与本 age 闸同源但属独立行为变更(影响所有多核 target 的定级),**另起提案**,本提案**不碰 severity**
- **self-PID 排除自身进程树**:SSH 远端 shell PID 链不稳,age 闸已天然覆盖「观察者抓到自己」(自起进程 `etimes≈0`),否决
- **两次采样取间隔 CPU(像 top)**:需 inspector 内两次 `ps` + sleep + 配对,破坏单次快照简洁性,否决
- **busybox/BSD 兼容**:本 inspector 仅 `targets:[local,ssh]` Linux procps,不为 busybox/Alpine 引入 `etime`(`[[DD-]hh:]mm:ss`)字符串解析 fallback

## 对外契约影响

- **Inspector schema**:`linux.cpu.top_processes` manifest 变更(collect 命令、output_schema 增 `etimes`、新增 `parameters.min_etimes`、findings `when`)→ 更新 `os-shell-inspector-suite` spec
- 其余契约(Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令)**无影响**

## Failure Modes

1. **目标是 busybox/Alpine ps**:`-eo etimes` 不被支持,`ps` 报错 stdout 空。**注意**:`ps … | head` 的退出码取末段 `head`(恒 0)掩盖 `ps` 非零退出,且 runner **不**校验主 collect 命令退出码——故 fail-loud **不能**靠退出码,而由 `output_schema.rows` 的 `minItems: 1` 兜底:0 行(正常 procps 主机恒有 ≥1 进程)→ schema 校验失败 → `status=exception`(非静默 `status=ok` 假阳)。现状 `pcpu` 已依赖 procps `-eo`,本变更放大同一**既有**假设并补上此前缺失的 fail-loud 守卫。
2. **`etimes` 非数字 / 空**:此分支**不可达**——procps `etimes` 恒为非空整数,且 `parse_table` 对列数不足的行**跳过**(不补空),故每条存活行的 `etimes` 必为整数,`int(p.etimes)` 必成功。如强行构造空值,`int("")` 抛 `ValueError`,而 `ValueError` **不在** `_DSL_EXCEPTIONS`(`runner.py`)→ 会传出 runner(非干净 `status=exception`);但这与同行既有未守卫的 `float(p.cpu_pct)` 是**完全相同**的假设与风险,非本变更引入。故**不加**真值守卫(遵 CLAUDE.md「不为不可能分支写防御 fallback」+ 与同行 `cpu_pct` 对称;只给 `etimes` 加守卫而 `cpu_pct` 不加是不对称的死代码)。
3. **真持续跑飞但 `etimes < min_etimes` 的进程**:头 10s 不报(可接受的 MTTD 取舍;下一轮巡检 `etimes` 已过闸照常报)。default 取 10s 而非 60s 即为压缩此盲窗。
4. **schedule 把 `min_etimes` 覆盖为 0 或负**:`exclusiveMinimum: 0` 在 schema 层拒绝(沿用 load_avg 阈值校验范式),防止「闸恒开」退化为现状。

## Operational Limits

- 并发 / 内存 / 超时:不变(仍单条 `ps ... | head -n 10`,`timeout_seconds: 10`)。`etimes` 仅多一列输出,开销可忽略。

## Security & Secrets

- 无新密钥、无脱敏需求、不扩大攻击面。`etimes`/`pcpu`/`comm` 均非敏感;`min_etimes` 是 numeric parameter,在 DSL 内消费,**不**进 shell 命令字符串(无注入面)。

## Cost / Quota Impact

- 零 LLM / API 影响:纯 inspector collect/finding 逻辑,不经 Agent loop,不消耗 token / Anthropic 配额。副作用是**降低** Diagnostician 被喂误报 finding 的频次(间接省 token)。

## Demo Path

- 无需 SSH / 付费 API:在一台 procps Linux(或 local target)跑 `hostlens inspect linux.cpu.top_processes --json`,核对输出含 `etimes` 列;构造一个刚起的高 CPU 短命进程(`yes > /dev/null &` 后立即采集)验证 `etimes < 10` 不产 finding、存活 >10s 后产 finding。
- offline:跑新增 snapshot / 命令串级锁测试,核对捕获命令含 `pid,pcpu,pmem,etimes,comm` 且 finding `when` 含 `int(p.etimes) >= min_etimes`。
