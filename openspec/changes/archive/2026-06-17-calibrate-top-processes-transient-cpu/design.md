## 上下文

`linux.cpu.top_processes`(`src/hostlens/inspectors/builtin/linux/cpu_top_processes.yaml`)用单次 `ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -n 10` 快照,`cpu_pct >= 90 → critical` / `[70,90) → warning`。procps `ps %cpu = cputime/realtime`(自进程诞生),对刚 spawn 的短命 CPU-bound 进程读数虚高至 ~100% —— 真机 `bandwagon` 上 `journalctl pid 33948` 100% CPU 后随即消失、`load 0.00` 即此误报。同源问题已在 `linux.system.load_avg`(`os-shell-inspector-suite` spec line 175-189)用「持续信号 load5/load15 取代单样本 load1」修过,本变更复用同一门控哲学。经 SRE 对抗性核验:机制承重点(`%cpu = cputime/realtime`)逐字成立,方向(age 闸)正确,3 处取舍需固化(见决策)。

## 目标 / 非目标

**目标：**
- 用「进程存活时长 `etimes`」做「持续占用」的代理闸,消除年轻/自起进程的 `%cpu` 伪影误报
- 与 `load_avg` 校准对齐:参数化阈值 + `exclusiveMinimum: 0`,可按机型 schedule 覆盖
- 改动最小:仅 collect 加一字段 + output_schema/parse 加一列 + 一个 parameter + 两条 finding 各前置一个 conjunct

**非目标：**
- severity 多核重标定(单进程占满 1 核在多核机上 critical 是另一过度归因 → 另起提案)
- self-PID 排除自身进程树 / 两次采样取间隔 CPU(均否决,见决策)
- busybox/BSD 兼容(本 inspector 仅 Linux procps)

## 决策

**D1:用 `etimes`(存活秒数)整数字段,不用 `etime` 字符串。** procps `-eo etimes` 直接给整数秒,DSL `int(p.etimes)` 即用;`etime`(`[[DD-]hh:]mm:ss`)需 collector/DSL 端解析,徒增复杂。
- 替代(否决):`etime` 字符串解析 fallback 以兼容 busybox —— 过度工程;现有 `pcpu` 已依赖 procps `-eo`,本变更只放大同一既有假设,故坚持 `etimes` + 明文声明 procps-only / busybox known-unsupported(fail-loud)。

**D2:`min_etimes` 默认 `10`(不是 60)。** 关键 SLO 取舍。60s 抄 load5 的时间常数,但单进程存活时长 ≠ load EMA 物理意义:CPU saturation 真凶常是**刚起**的东西(fork 炸弹/启动即失控服务),60s 静默窗口让真持续跑飞的进程隐身一整分钟、伤本 inspector 使命、拉差真阳 MTTD。`10` 仍能滤掉 `etimes` 0–2s 的一次性 journalctl(本次误报),盲窗压到最小。
- 替代(考虑过):`5`(更激进,盲窗更小但对采集抖动更敏感)、`60`(抄 load5,被否)。`10` 是平衡点,且 parameter 化可按机型覆盖。

**D3:age 闸做 finding `when` 的 conjunct,不改 collect 的排序/截断。** `--sort=-pcpu | head -n 10` 仍按 CPU 排序取前 10 行展示(年轻进程仍出现在输出供人看),只是**不产 finding**。保留诊断上下文,门控只作用于「是否告警」。

**D4:否决 self-PID 排除。** 采集是 SSH 远端短命 shell,PID 链不稳;age 闸已天然覆盖「观察者抓到自己」(自起 journalctl `etimes≈0`)。`etimes` 闸比 self-exclusion 更简单且更通用(也挡住非自起的一次性进程)。

**D5:否决两次采样取间隔 CPU(像 top)。** 需 inspector 内两次 `ps` + sleep + 配对计算,collect 端无干净的 sleep/状态表达,破坏单次快照简洁性。age 闸是恰好的最小修法。

## 风险 / 权衡

- [头 `min_etimes` 秒的真持续跑飞进程不报] → default 取 10s 压缩盲窗;下一轮巡检 `etimes` 已过闸照常报;parameter 可按机型下调。
- [busybox/Alpine target collect 失败] → 经 `output_schema.rows` 的 `minItems: 1` fail-loud `status=exception`(非静默假阳)。**注意 fail-loud 不能靠退出码**:`ps … | head` 退出码取末段 `head` 掩盖 `ps` 非零,且 runner 不 gate 主 collect 命令退出码——故用 minItems:1(正常主机恒 ≥1 进程,0 行=采集失败)做 inspector 级守卫,不改 runner、不改命令;现状 `pcpu` 已有 procps 依赖脆弱性,本变更补齐此前缺失的 fail-loud。明文声明 procps-only。
- [`version` bump 致 finding-id churn] → regression diff 旧 id resolved + 新 id added,与 load_avg / failed_units 校准一致,可接受、列入重跑清单。
- [offline 无法验证 collector shell 执行] → 遵 D-7 `_CaptureTarget` 约定:offline 只命令串级锁(捕获命令含 `etimes` 字段 + finding `when` 含 age 闸),collector 执行正确性靠真机 Demo Path(构造 `etimes<10` 与 `>10` 的高 CPU 进程各验一次)。
