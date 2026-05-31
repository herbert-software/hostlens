"""The 8 incident-pack scenarios (M2.8 group 4).

Each scenario declares the inspectors the Agent calls, the canned "failure
state" stdout each inspector's main command returns (the data that drives the
Finding DSL to the expected severity), and the narrative the scripted model
emits. Both the generator and the snapshot tests read this single source.

Synthetic-data discipline (cassette commit gate, ``core.redact``): NO IPv4
literals, NO ``/home`` / ``/Users`` paths, NO emails, NO dotted FQDNs with a
flagged suffix. Network endpoints use single-label service names (``database``,
``cache``, ``payments``) so they satisfy the manifest ``host:port`` pattern
without tripping the ``hostname_or_fqdn`` rule. Chinese text uses ASCII
punctuation per the repo convention (ruff RUF001 flags fullwidth ,?:; ).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InspectorCall:
    """One inspector the Agent runs in a scenario.

    ``params`` is the Agent-surface ``dict[str, str]`` (array parameters arrive
    JSON-encoded — incident-pack Option E). ``main_stdout`` is the canned
    failure-state output of the inspector's main ``collect.command``.
    """

    name: str
    params: dict[str, str]
    main_stdout: str


@dataclass(frozen=True)
class IncidentScenario:
    key: str
    intent: str
    narrative: str
    inspectors: tuple[InspectorCall, ...]


SCENARIOS: tuple[IncidentScenario, ...] = (
    # 1. CPU saturation -----------------------------------------------------
    IncidentScenario(
        key="cpu_saturation",
        intent="这台机器 CPU 为什么飙高? 帮我排查 CPU 饱和的原因.",
        narrative=(
            "这台主机 CPU 处于饱和状态: mysqld (pid 4242) 正占用 97.5% CPU, "
            "且 1 分钟负载 16.40 已达 4 核的约 4 倍. 建议排查 mysqld 的慢查询或失控线程."
        ),
        inspectors=(
            InspectorCall(
                name="linux.cpu.top_processes",
                params={},
                main_stdout="4242 97.5 12.3 mysqld\n4310 64.2 3.1 python3\n1180 12.0 1.0 nginx\n",
            ),
            InspectorCall(
                name="linux.system.load_avg",
                params={},
                main_stdout="load1=16.40\nload5=12.10\nload15=8.00\nncpu=4\n",
            ),
        ),
    ),
    # 2. Memory pressure / OOM ---------------------------------------------
    IncidentScenario(
        key="memory_oom",
        intent="这台机器内存是不是快爆了? 最近有没有进程被 OOM 杀掉?",
        narrative=(
            "内存严重不足: 当前可用内存仅 2.0%, 且内核 OOM-killer 最近触发过一次, "
            "杀掉了 mysqld (pid 4242). 建议立刻扩容或排查内存泄漏进程."
        ),
        inspectors=(
            InspectorCall(
                name="linux.memory.pressure",
                params={},
                main_stdout="mem_total_kb=16384000\nmem_avail_kb=327680\navail_pct=2.0\n",
            ),
            InspectorCall(
                name="linux.kernel.oom_killer",
                params={},
                main_stdout=(
                    '{"oom_events":[{"event":"Out of memory: Killed process 4242 (mysqld)"}]}'
                ),
            ),
        ),
    ),
    # 3. Disk full / inode exhaustion --------------------------------------
    IncidentScenario(
        key="disk_inode",
        intent="磁盘是不是满了? 帮我看看哪个分区快撑不住了, inode 有没有问题.",
        narrative=(
            "根分区 / (/dev/sda1) 已用 98%, 接近写满; /var (/dev/sdb1) 也到了 88%. "
            "同时根分区 inode 使用率达 96%. 建议清理日志与临时文件."
        ),
        inspectors=(
            InspectorCall(
                name="linux.disk.usage",
                params={},
                main_stdout="/dev/sda1 98 /\n/dev/sdb1 88 /var\n",
            ),
            InspectorCall(
                name="linux.fs.inode_pressure",
                params={},
                main_stdout="/dev/sda1 96 /\n/dev/sdb1 40 /var\n",
            ),
        ),
    ),
    # 4. systemd failed units ----------------------------------------------
    IncidentScenario(
        key="systemd_failed",
        intent="系统服务有没有挂掉的? 帮我列一下 failed 的 systemd 单元.",
        narrative=(
            "有 2 个 systemd 单元处于 failed 状态: nginx.service 与 mysql.service. "
            "建议查看各自的 journal 日志定位启动失败原因."
        ),
        inspectors=(
            InspectorCall(
                name="linux.systemd.failed_units",
                params={},
                main_stdout='{"failed":[{"unit":"nginx.service"},{"unit":"mysql.service"}]}',
            ),
        ),
    ),
    # 5. Recent error burst (sampling_window) ------------------------------
    IncidentScenario(
        key="error_burst",
        intent="最近几分钟日志里是不是错误突然变多了? 帮我看看错误日志量.",
        narrative=(
            "最近 5 分钟内出现了 247 条 error 级别日志, 明显高于正常水位. "
            "建议结合具体服务日志定位错误突增的根因."
        ),
        inspectors=(
            InspectorCall(
                name="log.tail.error_burst",
                params={},
                main_stdout="error_count=247\nwindow_seconds=300\n",
            ),
        ),
    ),
    # 6. File-descriptor exhaustion ----------------------------------------
    IncidentScenario(
        key="fd_exhaustion",
        intent="是不是文件描述符快用光了? 帮我看看 fd 分配情况.",
        narrative=(
            "系统级文件描述符已分配 950272/1048576, 达上限的约 91%, 接近耗尽. "
            "建议排查是否有进程泄漏 fd 或调高内核上限."
        ),
        inspectors=(
            InspectorCall(
                name="linux.process.fd_usage",
                params={},
                main_stdout="allocated=950272\nmax=1048576\n",
            ),
        ),
    ),
    # 7. Dependency connectivity (parameterized — Option E) ----------------
    IncidentScenario(
        key="dependency_unreachable",
        intent="帮我检查下游依赖 database:5432 和 cache:6379 还连得上吗?",
        narrative=(
            "下游依赖探测: database:5432 已不可达, cache:6379 正常. "
            "建议优先排查 database 的网络连通与进程存活."
        ),
        inspectors=(
            InspectorCall(
                name="net.dependency.tcp_check",
                params={"endpoints": '["database:5432", "cache:6379"]'},
                main_stdout=(
                    '{"results":[{"endpoint":"database:5432","reachable":false},'
                    '{"endpoint":"cache:6379","reachable":true}]}'
                ),
            ),
        ),
    ),
    # 8. TLS certificate expiry (parameterized — Option E) -----------------
    IncidentScenario(
        key="tls_expiry",
        intent="帮我检查 payments:443 的 TLS 证书还有多久过期.",
        narrative=(
            "payments:443 的 TLS 证书将在 3 天后过期, 已进入紧急区间. "
            "建议立即续期, 避免下游握手失败."
        ),
        inspectors=(
            InspectorCall(
                name="net.tls.cert_expiry",
                params={"endpoints": '["payments:443"]'},
                main_stdout='{"results":[{"endpoint":"payments:443","days_until_expiry":3}]}',
            ),
        ),
    ),
)

SCENARIOS_BY_KEY: dict[str, IncidentScenario] = {s.key: s for s in SCENARIOS}
