"""Scenario registry — the single source of truth for demo scenario metadata.

Carries **only** the product-facing fields ``{key, intent, description}`` per
the field boundary in design「字段边界」: ``{narrative, inspectors,
main_stdout}`` stay in ``tests/incidents/_scenarios.py`` as test/recording raw
material. The registry is the SOT for ``intent`` — ``_scenarios.py`` proxies
its ``intent`` field back to here after migration (design D1 单向决议).

``intent`` strings are copied byte-for-byte from the recorded scenarios: they
feed the cassette request key (model + messages hash), so any normalization
(punctuation / whitespace) would break replay matching. The Chinese text uses
ASCII punctuation per the repo convention.

Key normalization (design D5): callers may pass kebab-case (``cpu-spike``);
``get_scenario`` replaces ``-`` with ``_`` before lookup. snake_case is the
only stored key — there is no separate alias table.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "DemoScenario",
    "get_scenario",
    "list_scenarios",
    "normalize_key",
]


class DemoScenario(BaseModel):
    """Product-facing metadata for one demo scenario.

    Frozen so a registry entry cannot be mutated after assembly; ``extra``
    forbidden so the field boundary (only key / intent / description) is
    enforced structurally rather than by convention.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    intent: str
    description: str


def normalize_key(key: str) -> str:
    """Normalize a user-supplied scenario key to its snake_case canonical form.

    Single mechanical rule (design D5): ``-`` → ``_``. No alias table.
    """

    return key.replace("-", "_")


_SCENARIOS: tuple[DemoScenario, ...] = (
    DemoScenario(
        key="cpu_saturation",
        intent="这台机器 CPU 为什么飙高? 帮我排查 CPU 饱和的原因.",
        description="CPU 饱和: 定位占用 CPU 最高的进程与系统负载.",
    ),
    DemoScenario(
        key="memory_oom",
        intent="这台机器内存是不是快爆了? 最近有没有进程被 OOM 杀掉?",
        description="内存压力: 检查可用内存与内核 OOM-killer 记录.",
    ),
    DemoScenario(
        key="disk_inode",
        intent="磁盘是不是满了? 帮我看看哪个分区快撑不住了, inode 有没有问题.",
        description="磁盘容量: 检查各分区使用率与 inode 耗尽情况.",
    ),
    DemoScenario(
        key="systemd_failed",
        intent="系统服务有没有挂掉的? 帮我列一下 failed 的 systemd 单元.",
        description="服务状态: 列出处于 failed 状态的 systemd 单元.",
    ),
    DemoScenario(
        key="error_burst",
        intent="最近几分钟日志里是不是错误突然变多了? 帮我看看错误日志量.",
        description="日志突增: 统计最近时间窗口内的错误日志量.",
    ),
    DemoScenario(
        key="fd_exhaustion",
        intent="是不是文件描述符快用光了? 帮我看看 fd 分配情况.",
        description="文件描述符: 检查系统级 fd 分配是否接近上限.",
    ),
    DemoScenario(
        key="dependency_unreachable",
        intent="帮我检查下游依赖 database:5432 和 cache:6379 还连得上吗?",
        description="下游依赖: 探测关键依赖端口的 TCP 连通性.",
    ),
    DemoScenario(
        key="tls_expiry",
        intent="帮我检查 payments:443 的 TLS 证书还有多久过期.",
        description="证书过期: 检查 TLS 证书距离到期的剩余天数.",
    ),
)

_SCENARIOS_BY_KEY: dict[str, DemoScenario] = {scenario.key: scenario for scenario in _SCENARIOS}


def list_scenarios() -> list[DemoScenario]:
    """Return all registered scenarios in declaration order."""

    return list(_SCENARIOS)


def get_scenario(key: str) -> DemoScenario | None:
    """Look up a scenario by key, normalizing ``-`` → ``_`` first (design D5).

    Returns ``None`` for an unknown key so the CLI pre-flight can map a miss
    to exit 3 without catching an exception (design D8).
    """

    return _SCENARIOS_BY_KEY.get(normalize_key(key))
