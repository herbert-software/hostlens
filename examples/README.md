# Demo 场景 — 离线看 Agent 跑起来

`hostlens demo` 把 8 套**已验证的真实故障场景**包进产品包，让你在干净机器上
`pip install` 后**一条命令、无需 SSH / 付费 API key / 用户配置**就能离线 reproduce
一份带根因假设的报告，并在终端实时看到 Planner Agent 调度 Inspector → 收集
finding → 生成 narrative 的过程。

这是把「这是个 Agent 而不是脚本」直接展示出来的最低成本路径：底层走 M2.8 的
**双回放层**（`ReplayTarget` 回放主机命令输出 + `PlaybackBackend` 回放模型回合），
**零 token 消耗、零 API 调用、亚秒级完成**。

## 准备

```bash
pip install -e ".[dev]"
hostlens demo list        # 列出全部 8 个可选场景
```

## 命令形态

```
hostlens demo run <scenario> [-f md|json] [-o FILE] [--quiet|--no-progress]
hostlens demo list
```

- `<scenario>`：场景 key（snake_case 为准），也接受 kebab-case 纯归一化
  （`cpu-saturation` → `cpu_saturation`，仅 `-`→`_`，**无别名表**，故 `cpu-spike`
  这类不同词不解析）。
- `-f / --format`：`md`（默认）或 `json`。
- `-o / --output`：把报告写进文件（不带则输出到 stdout）。
- `--quiet` / `--no-progress`：同一开关两拼写，关掉 stderr 上的实时进度流。

**输出分离**：进度流在 **stderr**，报告在 **stdout**，可重定向 / 可测试。

**退出码契约**：`0` 健康 / `1` 完成且含 critical finding（8 个回放场景都有 critical
finding，故都是 exit 1）/ `2` 降级·装配损坏·运行漂移 / `3` 未知场景·资产缺失·`-o`
写失败。

---

## 首推场景（有明确根因链）

这三个场景的 narrative 给出**量化的根因假设**，最能体现「跨信号关联 + 推理诊断」
相对传统规则匹配的差异，适合做门面 demo。

### cpu_saturation — CPU 饱和

定位占用 CPU 最高的进程与系统负载，把「CPU 高」推进到「哪个进程、负载相对核数多高」。

```bash
hostlens demo run cpu_saturation
```

期望输出（节选）：

```
这台主机 CPU 处于饱和状态: mysqld (pid 4242) 正占用 97.5% CPU, 且 1 分钟负载 16.40 已达 4 核的约 4 倍. 建议排查 mysqld 的慢查询或失控线程.

## Findings
- critical: Process mysqld (pid 4242) is using 97.5% CPU
- critical: 1-min load 16.40 is >= 2x the 4 available cores
```

### memory_oom — 内存压力 / OOM

检查可用内存与内核 OOM-killer 记录，把「内存紧张」关联到「谁被 OOM 杀掉了」。

```bash
hostlens demo run memory_oom
```

期望输出（节选）：

```
内存严重不足: 当前可用内存仅 2.0%, 且内核 OOM-killer 最近触发过一次, 杀掉了 mysqld (pid 4242). 建议立刻扩容或排查内存泄漏进程.

## Findings
- critical: Only 2.0% memory available
- critical: Kernel OOM-killer fired recently (see oom_events for details)
```

### dependency_unreachable — 下游依赖探测

探测关键依赖端口的 TCP 连通性，区分「哪个依赖挂了、哪个正常」。

```bash
hostlens demo run dependency_unreachable
```

期望输出（节选）：

```
下游依赖探测: database:5432 已不可达, cache:6379 正常. 建议优先排查 database 的网络连通与进程存活.

## Findings
- critical: Dependency endpoint database:5432 is unreachable
```

---

## 其余场景

这些场景同样可跑、同样出带根因假设的报告。其中 `error_burst` 是计数器型
（统计错误日志量），形态上最接近传统监控的阈值规则匹配，故保留但不作首推。

### disk_inode — 磁盘容量与 inode

```bash
hostlens demo run disk_inode
```

期望输出（节选）：

```
根分区 / (/dev/sda1) 已用 98%, 接近写满; /var (/dev/sdb1) 也到了 88%. 同时根分区 inode 使用率达 96%. 建议清理日志与临时文件.

## Findings
- critical: Filesystem / (/dev/sda1) is 98% full
- warning: Filesystem /var (/dev/sdb1) usage high at 88%
- critical: Filesystem / (/dev/sda1) inode usage 96%
```

### systemd_failed — failed 单元

```bash
hostlens demo run systemd_failed
```

期望输出（节选）：

```
有 2 个 systemd 单元处于 failed 状态: nginx.service 与 mysql.service. 建议查看各自的 journal 日志定位启动失败原因.

## Findings
- critical: One or more systemd units are in the failed state (see failed for details)
```

### error_burst — 日志错误突增（计数器型）

```bash
hostlens demo run error_burst
```

期望输出（节选）：

```
最近 5 分钟内出现了 247 条 error 级别日志, 明显高于正常水位. 建议结合具体服务日志定位错误突增的根因.

## Findings
- critical: 247 error log entries in the last 300s
```

> 该场景含 `sampling_window` Inspector（命令带时间戳），demo 在**冻结工具时钟**下
> 回放，保证命令字符串与 fixture 逐字节匹配、输出确定。

### fd_exhaustion — 文件描述符耗尽

```bash
hostlens demo run fd_exhaustion
```

期望输出（节选）：

```
系统级文件描述符已分配 950272/1048576, 达上限的约 91%, 接近耗尽. 建议排查是否有进程泄漏 fd 或调高内核上限.

## Findings
- critical: File descriptors 950272/1048576 allocated (>= 90% of limit)
```

### tls_expiry — TLS 证书临期

```bash
hostlens demo run tls_expiry
```

期望输出（节选）：

```
payments:443 的 TLS 证书将在 3 天后过期, 已进入紧急区间. 建议立即续期, 避免下游握手失败.

## Findings
- critical: TLS cert for payments:443 expires in 3 days
```

---

## 写文件 / 关进度

```bash
hostlens demo run cpu_saturation --quiet -o report.md   # 关进度流，报告写进文件
hostlens demo run cpu_saturation -f json -o report.json # JSON 格式
```

---

## 想真实复现这些故障？（手动指引，不在 demo CLI scope）

demo 是**离线回放**——它回放预录的主机状态与模型回合，不真制造故障。如果你想在
自己的真机上**真实复现**这些故障态再用 `hostlens inspect --intent` 对 live target
跑（需要真实 target + `ANTHROPIC_API_KEY`），可以手动制造负载。**这些命令具有破坏性
/ 占用资源，请只在一次性的测试机上跑，不要在生产或共享机器上执行。**

| 场景 | 手动制造故障（测试机） |
|---|---|
| cpu_saturation | `stress-ng --cpu $(nproc) --timeout 120s`（或 `yes > /dev/null &` 起若干个） |
| memory_oom | `stress-ng --vm 2 --vm-bytes 90% --timeout 60s`（逼近 OOM；注意可能真的触发 OOM-killer） |
| disk_inode | 在专用分区 `for i in $(seq 1 200000); do : > /tmp/inodetest/$i; done`（耗 inode）；`fallocate -l 10G /tmp/big.bin`（耗容量） |
| systemd_failed | 写一个故意 `exit 1` 的 oneshot unit 并 `systemctl start` 让它进 failed |
| error_burst | `for i in $(seq 1 300); do logger -p user.err "synthetic error $i"; done` |
| fd_exhaustion | 写脚本打开大量 fd 不关闭（逼近 `ulimit -n`） |
| dependency_unreachable | 用 iptables / 防火墙临时 drop 某依赖端口，或停掉本机的依赖服务 |
| tls_expiry | 用 `openssl` 签一张有效期很短（如 1 天）的自签证书挂到测试服务上 |

跑完真实故障后用 `hostlens inspect <target> --intent "..."` 走 live 推理诊断路径
（见仓库根 `README.md` 与 `docs/operations/`）。**`hostlens demo` 本身永远是离线、
确定、无副作用的**，上面的手动指引仅供想脱离回放、验证真实链路的人参考。
