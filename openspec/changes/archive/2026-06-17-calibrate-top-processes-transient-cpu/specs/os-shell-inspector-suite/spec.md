## 新增需求

### 需求:`linux.cpu.top_processes` 必须按进程存活时长（etimes）门控、不以单次 `ps %cpu` 快照触发严重度

`linux.cpu.top_processes` 当前用 `ps -eo pid,pcpu,... | head -n 10` 单次快照,并以 `float(p.cpu_pct) >= 90.0 → critical` 直接定级。但 procps-ng `ps(1)` 的 `%cpu` 定义为 **`cputime/realtime`**(累计 CPU 时间 ÷ 进程**自诞生**的存活时长),**非瞬时利用率**:一个刚 spawn、几乎全程吃满单核活约 1–2s 的短命进程(如 journalctl 扫大 journal——可能正是同轮巡检里别的 inspector 自起的)单次快照即读到 `~100%`,被误标「严重」(真机 `bandwagon` 上 `journalctl pid 33948` 100% CPU、进程随即消失、`load 0.00` 是已暴露的误报)。**必须**叠加「进程已存活足够久」的持续性门控,与 `linux.system.load_avg` 用 `load5/load15` 取代单样本 `load1` 同源:

- collect 命令**必须**加 `etimes` 字段(`ps -eo pid,pcpu,pmem,etimes,comm --sort=-pcpu --no-headers | head -n 10`);`etimes` 是 procps 的「进程存活秒数」(整数)。output_schema **必须**新增 `etimes: { type: string }`(`parse.format: table` 产出恒为 str,DSL 用 `int()` 转换)。`parse.columns` **必须**同步为 `[pid, cpu_pct, mem_pct, etimes, comm]`(列序与 `-eo` 字段序一致)。
- **必须**新增 parameter `min_etimes`(`type: number`,`exclusiveMinimum: 0`,默认 `10`,沿用 `load_avg` 的 numeric-阈值免 `pattern` + `exclusiveMinimum: 0` 校验范式)。`exclusiveMinimum: 0` 拒绝 schedule 把闸覆盖为 `0`/负(否则 `>= min_etimes` 对任意非负 etimes 恒真、门控退化为现状)。默认 `10`(非 60)是 SLO 取舍:既滤掉 `etimes` 0–2s 的一次性进程,又**不**让真正刚跑飞的进程隐身一整分钟伤本 inspector「诊断 CPU saturation」的使命。
- 两条 finding(`critical` ≥90 / `warning` 70–90)的 `when` **必须**前置 age 闸 `int(p.etimes) >= min_etimes`(与 `for_each: "rows as p"` 同作用域)。`message` 沿用下标语法 `{p[comm]}`/`{p[pid]}`/`{p[cpu_pct]}`(`str.format` 无属性式;属性式 `p.cpu_pct`/`p.etimes` 仅 `when` DSL 可用),保持英文(该 inspector 在 i18n `_BACKLOG`,无需迁移 allowlist)。`version` bump → finding-id churn(regression diff 旧 id resolved + 新 id added,可接受)。
- 本 inspector **procps-only**:`etimes`(及既有 `-eo` 自定义字段)在 busybox/Alpine ps 不被支持,**禁止**为兼容引入 `etime`(`[[DD-]hh:]mm:ss`)字符串解析 fallback(过度工程)。collect 注释**必须**写明 procps-only 假设。macOS/BSD `ps %cpu` 是「decaying average over a minute」语义不同,但本 inspector 仅 `targets:[local,ssh]` 的 Linux,机制对目标平台成立,不影响修法。
- **不支持平台必须 fail-loud,禁止静默假绿**:不支持 `etimes` 的 `ps` 报错后,`ps … | head` 的 pipeline 退出码取末段 `head`(恒 0)**掩盖** `ps` 的非零退出,且 runner **不**校验主 collect 命令的退出码(仅校验 timeout / parse / schema / 前置 probe)——故仅靠退出码**无法** fail-loud,会退化成 `status=ok` + 0 finding 的假「全部正常」。因此 `output_schema` 的 `rows` 数组**必须**声明 `minItems: 1`:正常 procps 主机恒有 ≥1 进程(init + 内核线程),`ps … | head` 必出 ≥1 行;**0 行 ⟹ 采集失败/平台不支持**,经 output_schema 校验失败 → runner `status=exception`(`output_schema_mismatch`)。这是 inspector 级 fail-loud 守卫(不改 runner、不改命令)。
- **不动 severity 分级语义**:多核机上「单进程占满 1 核 → critical」的过度归因(按 `ncpu` 归一 / `critical→warning`)是另一独立行为变更,不在本需求范围。本需求只加 age 闸、不改阈值与 severity 映射。

#### 场景:年轻进程的 `%cpu` 伪影不告警（确定性锚）

- **当** `ps` 快照里一个进程 `cpu_pct >= 90`(如 journalctl `100`,`%cpu = cputime/realtime` 对刚起的 CPU-bound 短命进程读数虚高)但 `etimes < min_etimes`(如 `etimes==1`,默认 `min_etimes==10`)
- **那么** `linux.cpu.top_processes` **禁止**为该进程产生任何 finding(瞬时/自起进程非故障),报告**不得**因此被标 `warning`/`critical`

#### 场景:持续占用 CPU 才告警（age 闸 + cpu 阈值双门）

- **当** 一个进程 `etimes >= min_etimes`(已存活足够久,确为持续占用)**且** `cpu_pct >= 90`
- **那么** **必须**产生 `critical` finding;若 `etimes >= min_etimes` 且 `cpu_pct` 在 `[70, 90)` 则产 `warning`;`etimes` 不足 `min_etimes` 的进程**无论 `cpu_pct` 多高**都**不**告警

#### 场景:collect 输出 `etimes` 列且命令串级锁

- **当** 本变更的 snapshot / 命令串级锁测试捕获 `linux.cpu.top_processes` 的主命令
- **那么** 捕获命令**必须**含 `ps -eo pid,pcpu,pmem,etimes,comm`(字段含 `etimes` 且列序固定),`output_schema`/`parse.columns` **必须**含 `etimes`,两条 finding 的 `when` **必须**含 `int(p.etimes) >= min_etimes`

#### 场景:不支持平台的空采集 fail-loud（确定性锚,禁止静默假绿）

- **当** `ps -eo …,etimes,… | head` 在不支持 `etimes` 的 `ps`(busybox/BSD)上报错、stdout 为空(`ps` 非零退出被 `| head` 掩盖,runner 不 gate 主命令退出码),解析得 `rows == []`
- **那么** `linux.cpu.top_processes` **必须** `status == "exception"`(`output_schema` 的 `rows` `minItems: 1` 校验失败 → `output_schema_mismatch`),**禁止** `status == "ok"` + 0 finding 的假「全部正常」;本变更**必须**含一份 offline 回归测试喂空 stdout 断言 `status == "exception"`
