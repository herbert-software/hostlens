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

from hostlens.demo.registry import get_scenario


def _intent(key: str) -> str:
    """Return the scenario intent from the demo registry (intent SOT, design D1).

    The demo registry is the single source of truth for ``intent``; the
    snapshot tests must replay the exact recorded intent (it feeds the cassette
    request key), so a missing key here is a hard failure rather than a silent
    empty string.
    """

    scenario = get_scenario(key)
    if scenario is None:
        raise KeyError(f"demo registry has no scenario {key!r}")
    return scenario.intent


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
    # Diagnosis-phase authored content (design D-3 / D-3.5): the single root-cause
    # hypothesis the scripted Diagnostician records via ``correlate_findings``
    # (referencing ordinal labels), its suggested remediation actions, and the
    # diagnosis loop's finalize narrative. Synthetic-data discipline applies
    # (no IPv4 / real paths / FQDNs; ASCII punctuation only).
    hypothesis: str
    suggested_actions: tuple[str, ...]
    diagnosis_narrative: str
    # Authored ``confidence`` + ``supporting_findings`` for the single
    # ``correlate_findings`` call. Defaults reproduce the historical global
    # constants (``high`` + ``("F1",)``) so the other 7 scenarios stay
    # byte-identical; a scenario overrides them to satisfy diagnostician rule 5
    # (independent failures with no mechanism evidence must NOT be ``high``, and
    # a hypothesis must cite every finding label it discusses).
    diag_confidence: str = "high"
    diag_supporting: tuple[str, ...] = ("F1",)


SCENARIOS: tuple[IncidentScenario, ...] = (
    # 1. CPU saturation -----------------------------------------------------
    IncidentScenario(
        key="cpu_saturation",
        intent=_intent("cpu_saturation"),
        narrative=(
            "这台主机 CPU 处于饱和状态: mysqld (pid 4242) 正占用 97.5% CPU, "
            "且持续负载 (5 分钟 12.10, 15 分钟 8.00) 已达 4 核容量的 2 倍以上, "
            "说明过载已持续数分钟而非瞬时尖峰. 建议排查 mysqld 的慢查询或失控线程."
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
        hypothesis=(
            "mysqld (pid 4242) 失控占用 CPU 是本次饱和的根因: 单进程 97.5% CPU "
            "叠加持续负载 (5 分钟 12.10, 15 分钟 8.00) 远超 4 核容量, 过载已持续数分钟."
        ),
        suggested_actions=(
            "排查 mysqld 慢查询日志, 定位失控线程或全表扫描.",
            "必要时限流或重启 mysqld, 并观察负载是否回落.",
        ),
        diagnosis_narrative=(
            "综合 top 进程与持续负载两个信号, 根因指向 mysqld 失控占用 CPU. "
            "持续负载 (5/15 分钟均值) 高于核心数, 排除了瞬时尖峰的可能. 建议优先排查其慢查询."
        ),
    ),
    # 2. Memory pressure / OOM ---------------------------------------------
    IncidentScenario(
        key="memory_oom",
        intent=_intent("memory_oom"),
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
        hypothesis=(
            "内存耗尽触发 OOM-killer 杀掉 mysqld 是根因: 可用内存仅 2.0%, "
            "内核已记录一次针对 mysqld 的 OOM 事件."
        ),
        suggested_actions=(
            "立即排查内存占用最高的进程, 确认是否存在内存泄漏.",
            "扩容物理内存或调整 mysqld 内存上限, 避免再次被 OOM.",
        ),
        diagnosis_narrative=(
            "可用内存极低与 OOM-killer 日志两条信号一致指向内存耗尽, "
            "mysqld 被杀是结果而非诱因. 建议先扩容再排查泄漏."
        ),
    ),
    # 3. Disk full / inode exhaustion --------------------------------------
    IncidentScenario(
        key="disk_inode",
        intent=_intent("disk_inode"),
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
        hypothesis=(
            "根分区 / 同时逼近块用尽(98%)与 inode 用尽(96%)是根因: 两类容量都接近写满, "
            "任一耗尽都会导致写入失败."
        ),
        suggested_actions=(
            "清理根分区下的大日志与临时文件, 释放块空间.",
            "排查小文件堆积目录, 降低 inode 占用.",
        ),
        diagnosis_narrative=(
            "磁盘块用量与 inode 用量两个信号都集中在根分区, 根因是根分区容量逼近双重耗尽. "
            "建议优先清理日志."
        ),
    ),
    # 4. systemd failed units ----------------------------------------------
    IncidentScenario(
        key="systemd_failed",
        intent=_intent("systemd_failed"),
        narrative=(
            "有 2 个常驻服务单元 (Type=simple) 处于 failed 状态: nginx.service 与 mysql.service. "
            "二者各自挂掉, 没有共享依赖或同一时间窗的证据, 应视为两条彼此独立的故障. "
            "建议分别查看各自的 journal 日志定位启动失败原因, 不要臆断为同一根因."
        ),
        inspectors=(
            InspectorCall(
                name="linux.systemd.failed_units",
                params={},
                main_stdout=(
                    '{"uptime_seconds": 3110400, "results": ['
                    '{"unit": "nginx.service", "type": "simple", '
                    '"inactive_monotonic_us": 2400000000}, '
                    '{"unit": "mysql.service", "type": "simple", '
                    '"inactive_monotonic_us": 2600000000}]}'
                ),
            ),
        ),
        hypothesis=(
            "nginx.service 与 mysql.service 均为常驻服务 (Type=simple) 且各自 failed, "
            "在没有共享依赖或同一时间窗证据的情况下应视为两条独立故障, 各自需结合自身 journal 排查, "
            "不可臆断为出自同一根因."
        ),
        suggested_actions=(
            "分别查看 nginx.service 与 mysql.service 各自的 journal 日志, 独立定位各自的启动失败原因.",
            "对每个单元单独修复配置或依赖后重启并确认状态恢复, 不要假设二者互为因果.",
        ),
        diagnosis_narrative=(
            "两个常驻服务各自处于 failed 状态, 但二者之间没有共享依赖或同一时间窗的证据, "
            "因此判定为两条彼此独立的 critical 故障, 而非出自同一共同根因. 建议对各单元独立排查其 journal 日志."
        ),
        # rule 5: independent failures with no per-service mechanism evidence must
        # NOT be ``high`` (the "彼此独立" judgment is sound but neither unit's root
        # cause is established) — author ``medium``; the hypothesis discusses both
        # nginx and mysql, so it must cite both labels.
        diag_confidence="medium",
        diag_supporting=("F1", "F2"),
    ),
    # 5. Recent error burst (sampling_window) ------------------------------
    IncidentScenario(
        key="error_burst",
        intent=_intent("error_burst"),
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
        hypothesis=(
            "最近 5 分钟内 247 条 error 级日志的突增是根因信号: 错误水位明显高于正常, "
            "指向某服务在该窗口内集中报错."
        ),
        suggested_actions=(
            "结合具体服务日志定位错误突增的来源服务.",
            "确认是否伴随发布/配置变更, 必要时回滚.",
        ),
        diagnosis_narrative=(
            "采样窗口内 error 计数远超基线, 根因是某服务在最近 5 分钟集中报错. "
            "建议下钻到服务级日志."
        ),
    ),
    # 6. File-descriptor exhaustion ----------------------------------------
    IncidentScenario(
        key="fd_exhaustion",
        intent=_intent("fd_exhaustion"),
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
        hypothesis=(
            "系统级文件描述符接近耗尽是根因: 已分配 950272/1048576 达上限约 91%, "
            "继续增长将导致新连接/打开文件失败."
        ),
        suggested_actions=(
            "排查是否存在进程泄漏 fd, 定位打开数最高的进程.",
            "在确认无泄漏前提下调高内核 fd 上限作为缓冲.",
        ),
        diagnosis_narrative=(
            "已分配 fd 逼近系统上限, 根因是 fd 接近耗尽. 建议优先排查泄漏进程再考虑调高上限."
        ),
    ),
    # 7. Dependency connectivity (parameterized — Option E) ----------------
    IncidentScenario(
        key="dependency_unreachable",
        intent=_intent("dependency_unreachable"),
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
        hypothesis=(
            "下游依赖 database:5432 不可达是根因: 同批探测中 cache:6379 正常, "
            "故障收敛在 database 这一个端点."
        ),
        suggested_actions=(
            "优先排查 database 的网络连通与进程存活.",
            "确认防火墙/安全组未拦截 database:5432.",
        ),
        diagnosis_narrative=(
            "依赖探测显示仅 database:5432 不可达而 cache 正常, 根因是 database 端点故障. "
            "建议优先核查其存活与网络."
        ),
    ),
    # 8. TLS certificate expiry (parameterized — Option E) -----------------
    IncidentScenario(
        key="tls_expiry",
        intent=_intent("tls_expiry"),
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
        hypothesis=(
            "payments:443 的 TLS 证书 3 天后过期是根因隐患: 已进入紧急续期区间, "
            "到期后下游握手将失败."
        ),
        suggested_actions=(
            "立即为 payments:443 续期 TLS 证书.",
            "部署后验证证书链与有效期, 并设置到期前告警.",
        ),
        diagnosis_narrative=(
            "证书有效期仅剩 3 天且已进入紧急区间, 根因是 payments:443 证书即将过期. "
            "建议立即续期避免握手失败."
        ),
    ),
)

SCENARIOS_BY_KEY: dict[str, IncidentScenario] = {s.key: s for s in SCENARIOS}
