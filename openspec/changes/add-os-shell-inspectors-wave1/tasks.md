## 1. 准备与样板对齐

- [x] 1.1 通读《Inspector 作者契约》spec + `builtin/docker/containers_restart_loop.yaml`（fail-loud + jq + `{results:[...]}`）、`builtin/net/dependency_tcp_check.yaml`（数组参数 `| map('sh')` + `pattern`）、聚合型 `builtin/system/uptime.yaml` 与 `builtin/linux/memory_pressure.yaml`（裸标量键）四份样板，固化本 wave 复用的纪律清单
- [x] 1.2 确认 fixture 录制器用法（`python -m hostlens.inspectors.recorder`）：对真 Linux host 录一份样例 fixture 跑通 `ReplayTarget` 回放，作为后续每个 inspector 的录制模板
- [x] 1.3 备好录制环境：本地 Linux 开发机或 docker-compose Linux 容器（systemd + cgroup v2），用于一次性采集 fixture
- [x] 1.4 命名空间纪律（design D-8）：所有 OS 域 inspector 用 `linux.*` 前缀落 `builtin/linux/`，网络/DNS/NTP 用 `net.*` 落 `builtin/net/`，日志用 `log.*` 落 `builtin/log/`；不新建顶层目录、不用裸命名空间

> **每个 inspector 的通用验收（§2–§9 各项均适用）**：纯 YAML 遵守契约 + 至少一份**触发预期 finding 的异常场景 fixture**（snapshot 断言预期 severity+message）+ 可选 happy-path fixture + 勾覆盖矩阵。仅「干净注册 + 无 finding」不算验收通过。

## 2. 计算 CPU 域（builtin/linux/）

- [x] 2.1 `linux.cpu.throttling`：读 `/sys/fs/cgroup/.../cpu.stat`（或 `/proc/stat` 派生）算 throttled 比率，collector 吐 JSON，DSL 判阈值；fail-loud
- [x] 2.2 `linux.cpu.cpufreq`：读 `/sys/devices/system/cpu/.../cpufreq`（scaling governor / cur vs max freq），文档式声明 Linux-only
- [x] 2.3 两者各录异常场景 fixture（如 throttled 比率超阈 / governor=powersave）+ snapshot 断言 finding + 勾矩阵

## 3. 内存域（builtin/linux/）

- [x] 3.1 `linux.memory.swap`：`/proc/meminfo` 派生 swap 使用率 + swappiness，collector 算派生量
- [x] 3.2 `linux.memory.hugepages`：`/proc/meminfo` HugePages_* / `/sys/kernel/mm/hugepages`，判已分配 vs 空闲
- [x] 3.3 两者各录异常场景 fixture（如 swap 高占用）+ snapshot 断言 finding + 勾矩阵

## 4. 磁盘 / FS 域（builtin/linux/）

- [x] 4.1 `linux.disk.io`：**collector 自行 read→sleep→read 双读** `/proc/diskstats` 算 IO 利用率/await（design D-3：sampling_window 不做差分，差分须命令自己做；`collect.timeout_seconds` > sleep 时长）
- [x] 4.2 `linux.disk.smart`：**单个非特权 inspector**（design D-6）——读 `/sys/block/*/queue/rotational`、可用 `smartctl --json` 时附带健康位但不强制 root；完整 root SMART 自检属性 defer 到独立提案
- [x] 4.3 `linux.fs.mount_health`：`findmnt --json` / `/proc/mounts` 判只读重挂、缺失挂载点
- [x] 4.4 `linux.fs.logrotate`：检查 logrotate 状态/配置陈旧（`/var/lib/logrotate/status` 时间戳派生）
- [x] 4.5 四者各录异常场景 fixture（如只读重挂 / logrotate 陈旧）+ snapshot 断言 finding + 勾矩阵

## 5. 网络 + DNS + NTP 域（builtin/net/）

- [x] 5.1 `net.connections`：`ss -s` / `ss -tan` 聚合连接态计数（TIME_WAIT / ESTABLISHED 等），collector 聚合
- [x] 5.2 `net.listening_ports`：`ss -tlnp` 列监听端口，列表集顶层键 `results`，参数（如端口白名单）走 `pattern` + `| sh`
- [x] 5.3 `net.dns.resolve`：对参数化待查名 `dig +short` 探测解析与时延，参数 `| map('sh')` + `pattern` 收紧域名字符集
- [x] 5.4 `net.ntp.drift`：`chronyc tracking` / `timedatectl` 派生时钟偏移，文档式声明所需守护进程
- [x] 5.5 四者各录异常场景 fixture（含注入 payload 安全测试场景）+ snapshot 断言 finding + 勾矩阵

## 6. 进程域（builtin/linux/）

- [x] 6.1 `linux.process.zombies`：`ps` 统计 Z 状态进程，fail-loud（ps 失败必非零退出）
- [x] 6.2 `linux.process.total`：进程总数 vs `/proc/sys/kernel/pid_max` 派生使用率
- [x] 6.3 `linux.process.critical_alive`：对参数化关键进程名列表（`| map('sh')` + `pattern`）检查存活，输出键避让 parameter
- [x] 6.4 三者各录异常场景 fixture（如僵尸进程存在 / 关键进程缺失）+ snapshot 断言 finding + 勾矩阵

## 7. 服务管理器 + 调度器域（builtin/linux/）

- [x] 7.1 `linux.systemd.timer_status`：`systemctl list-timers --all -o json`（或解析），判超期未触发 timer
- [x] 7.2 `linux.systemd.masked`：`systemctl list-unit-files --state=masked`，列被 mask 单元
- [x] 7.3 `linux.cron.last_runs`：解析 cron 日志/`/var/log` 最近执行（窄 scope 声明日志路径假设）
- [x] 7.4 `linux.cron.failures`：派生失败 cron 计数（非零退出/错误行）
- [x] 7.5 四者各录异常场景 fixture + snapshot 断言 finding + 勾矩阵；声明 systemd 假设

## 8. 内核 / 系统域（builtin/linux/）

- [x] 8.1 `linux.system.reboot_required`：检查 `/var/run/reboot-required` / `needs-restarting`，输出布尔 + 原因
- [x] 8.2 `linux.kernel.taint`：读 `/proc/sys/kernel/tainted` 派生 taint 标志位语义
- [x] 8.3 `linux.kernel.messages`：优先非特权 `journalctl -k -p err`（替代特权 `dmesg`，D-6），聚合近窗内核错误；用 sampling_window 的 `window_start` 做 `--since` 区间查询（design D-3：此为区间查询类、非计数器差分）
- [x] 8.4 三者各录异常场景 fixture（如 reboot-required 存在 / tainted≠0 / 内核错误突增）+ snapshot 断言 finding + 勾矩阵

## 9. 日志域（builtin/log/）

- [x] 9.1 `log.exception_burst`：按异常类型/stack-trace 签名聚合突增（区别于已有 `log.tail.error_burst` 的按行计数，确认两者 collector 不重叠，见 design §实现期注记）
- [x] 9.2 录异常场景 fixture（exception 突增）+ snapshot 断言 finding + 勾矩阵

## 10. 套件级验收

- [x] 10.1 扩 `tests/inspectors/test_builtin_inspectors.py`：断言全部 23 个新增 inspector 经 `load_manifest` 干净加载、registry `errors == []`
- [x] 10.2 扩 `tests/inspectors/test_builtin_capability_gate.py`：断言各 inspector 的 capability/binary preflight gate 行为正确（缺二进制→`requires_unmet` skip）
- [x] 10.3 注入安全回归：对带参数 inspector 跑注入 payload（`'; whoami; #` / `$(curl evil)`）验证渲染转义正确
- [x] 10.4 检出能力验收：逐 inspector 确认其异常场景 snapshot 断言了预期 finding（severity+message），无 no-op inspector
- [x] 10.5 全部勾上 `TODO.md` §M6 覆盖矩阵对应单元格（以 `linux.*`/`net.*`/`log.*` 前缀全名落地）
- [x] 10.6 确认零对外契约变更：manifest schema / capability enum / parse format / Agent 工具数组（`list_inspectors`+`run_inspector`）均未变；无新 Python 依赖；仅用现有 schema 字段（含 sampling_window）

## 11. 收尾

- [x] 11.1 `mypy --strict` + `ruff` + 全量 `pytest`（默认 replay 模式，不消耗 API）全绿
- [x] 11.2 跑 Demo Path：`hostlens inspectors list --tag <域>` 看新 inspector 注册、`hostlens inspect localhost --inspector linux.process.total` 或 fixture 回放出报告
- [x] 11.3 对本次变更跑对抗性 review（`/review-loop-codex`），triage + 修复到放行
- [ ] 11.4 开 feature branch `feat/add-os-shell-inspectors-wave1` → commit → push → `\gh pr create --base main`，CI 绿后 squash-merge
- [ ] 11.5 归档：`openspec-cn archive add-os-shell-inspectors-wave1`，delta 合入 `openspec/specs/`
