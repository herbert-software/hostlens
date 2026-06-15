"""Crosscheck for the ``inspector-authoring-contract`` finding-message i18n
quality规约 (``improve-report-rendering-and-i18n`` group 2.1).

This is the **static, machine-audited**防漂移 gate for the staged FindingRule
``message`` rewrite (中文标签 + 注入关键数据). It is intentionally **two-tier**:

  * **Migrated allowlist** (``_MIGRATED_ALLOWLIST``, initially the single旗舰
    sample ``linux.systemd.failed_units``): for every FindingRule of an
    inspector in this set we assert the three message-quality rules —
      (d) no ``see .* for details`` 类 empty-pointer pattern,
      (a) the message contains at least one CJK character (中文标签),
      (c) **if-inject-then-declared**: every ``{field}`` injected by the
          message has its root field declared in ``output_schema.properties``
          (防 ``KeyError`` at ``str.format`` time). This is a守卫, NOT a
          mandate that every message must inject — a genuinely
          no-variable-data标签 inspector needs no injection and no豁免 marker.
  * **Backlog** (``_BACKLOG``, the remaining 71 not-yet-migrated inspectors):
    these are deliberately NOT subjected to the中文 / 注入 assertions. Asserting
    "含中文" on the 71 English-message inspectors would make this crosscheck red
    the moment it ships, failing the proposal's own archive. The backlog shrinks
    as各域长尾 PR migrate inspectors into the allowlist.

The **anti-drift invariant** (``test_every_builtin_inspector_is_classified``)
asserts that every builtin inspector is in exactly one of allowlist / backlog
and that their union equals the full builtin set — so a newly-added inspector
that is left unclassified fails loud, making the "全量 vs 长尾" boundary显式可见
rather than relying on "碰巧没人加新 inspector".

Scope discipline (spec §场景:契约由 crosscheck 机审防漂移): this is a **static**
check — it does NOT instantiate any collector, run any command, validate that an
injected ``{field}`` is truly present at runtime, or判定 "该注入的有没有注入". Those
belong to collector unit tests + 真机 demo + 人审.
"""

from __future__ import annotations

import re
import string

import pytest

from hostlens.core.config import Settings
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.inspectors.schema import InspectorManifest

# --------------------------------------------------------------------------- #
# Two-tier classification of every builtin inspector (by canonical `name`).
# --------------------------------------------------------------------------- #
#
# `_MIGRATED_ALLOWLIST` is the set of inspectors whose FindingRule messages have
# been rewritten to the 中文标签 + 注入数据 contract. The message-quality
# assertions (a)/(c)/(d) apply ONLY to this set. Each各域长尾 PR appends the
# canonical `name` of every inspector it migrates.
#
# `_BACKLOG` is the **explicit** literal of not-yet-migrated inspectors (English
# messages, awaiting their domain PR). It is deliberately NOT computed as
# `all - allowlist`: a derived backlog auto-classifies any new inspector and so
# can never be "未分类", which would make the spec's drift guard ("新增 inspector
# 若未分类则 crosscheck 失败,强制作者把它纳入其一", inspector-authoring-contract
# §防漂移) vacuous. With both sets explicit, a newly-added inspector lands in
# neither → the union==all assertion fails → the author is forced to classify it
# (migrate to the allowlist with a 中文 message, or park it in the backlog). Each
# domain PR moves names from `_BACKLOG` to `_MIGRATED_ALLOWLIST` as it migrates.

_MIGRATED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "linux.systemd.failed_units",
    }
)

_BACKLOG: frozenset[str] = frozenset(
    {
        "docker.containers.restart_loop",
        "docker.images.disk_usage",
        "docker.networks",
        "go.goroutines",
        "go.heap",
        "hello.echo",
        "jvm.gc",
        "jvm.heap",
        "jvm.threads",
        "k8s.events.warnings",
        "k8s.nodes.conditions",
        "k8s.pods.evicted",
        "k8s.pods.oom_killed",
        "k8s.pods.stuck_pending",
        "linux.cpu.cpufreq",
        "linux.cpu.throttling",
        "linux.cpu.top_processes",
        "linux.cron.failures",
        "linux.cron.last_runs",
        "linux.disk.io",
        "linux.disk.smart",
        "linux.disk.usage",
        "linux.fs.inode_pressure",
        "linux.fs.logrotate",
        "linux.fs.mount_health",
        "linux.kernel.messages",
        "linux.kernel.oom_killer",
        "linux.kernel.taint",
        "linux.memory.hugepages",
        "linux.memory.pressure",
        "linux.memory.swap",
        "linux.process.critical_alive",
        "linux.process.fd_usage",
        "linux.process.total",
        "linux.process.zombies",
        "linux.system.load_avg",
        "linux.system.reboot_required",
        "linux.systemd.masked",
        "linux.systemd.timer_status",
        "log.exception_burst",
        "log.tail.error_burst",
        "mysql.connection_usage",
        "mysql.deadlocks",
        "mysql.replication_lag",
        "mysql.slow_queries",
        "net.connections",
        "net.dependency.tcp_check",
        "net.dns.resolve",
        "net.listening_ports",
        "net.ntp.drift",
        "net.tls.cert_expiry",
        "net.tls.chain_validity",
        "nginx.config_test",
        "nginx.error_rate",
        "nginx.health",
        "nginx.upstream",
        "pkg.held_back",
        "pkg.pending_updates",
        "pkg.security_patches",
        "postgres.bloat_tables",
        "postgres.connection_usage",
        "postgres.long_queries",
        "postgres.replication_lag",
        "redis.memory_usage",
        "redis.persistence",
        "redis.replication_lag",
        "redis.slowlog",
        "security.failed_logins",
        "security.sudo_history",
        "security.world_writable_dirs",
        "system.uptime",
    }
)

# `see <something> for details` 类 empty-pointer — case-insensitive, tolerant of
# the field name between `see` and `for details`. The 旗舰 sample's old message
# ("... (see failed for details)") is the canonical example this forbids.
_EMPTY_POINTER_RE = re.compile(r"see\s+.*\s+for\s+details", re.IGNORECASE)

# `string.Formatter().parse` yields the `{field}` references (field_name is None
# for literal text). The ROOT name is the segment before the first `.`/`[` —
# `{u.unit}` → `u`, `{arr[0]}` → `arr`, `{failed_names}` → `failed_names`.
_ROOT_FIELD_SPLIT = re.compile(r"[.\[]")

# `for_each: "<expr> as <var>"` — the loop variable name is bound per-iteration
# and is NOT an output_schema field, so it must be excluded from the
# if-inject-then-declared check. Mirror the schema's FOR_EACH pattern's tail.
_FOR_EACH_VAR_RE = re.compile(r"\s+as\s+([a-z_][a-z_0-9]*)\s*$")


def _builtin_registry_names() -> list[str]:
    result = build_registry_from_search_paths([], settings=Settings())
    assert result.errors == [], f"builtin registry build surfaced errors: {result.errors}"
    return result.registry.names()


def _load_builtin(name: str) -> InspectorManifest:
    result = build_registry_from_search_paths([], settings=Settings())
    return result.registry.get(name)


def _has_cjk(text: str) -> bool:
    """True iff ``text`` contains at least one CJK Unified Ideograph.

    Same idiom as ``tests/agent/test_diagnostician_agent.py._has_cjk`` — the
    BMP CJK block ``U+4E00``..``U+9FFF`` covers every simplified-Chinese label
    the message contract requires.
    """

    return any("一" <= ch <= "鿿" for ch in text)


def _injected_root_fields(message: str) -> set[str]:
    """Return the set of ROOT field names a ``str.format`` message injects."""

    roots: set[str] = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(message):
        if field_name is None or field_name == "":
            continue
        roots.add(_ROOT_FIELD_SPLIT.split(field_name, maxsplit=1)[0])
    return roots


def _declared_output_fields(manifest: InspectorManifest) -> set[str]:
    """Return the top-level field names declared in ``output_schema.properties``."""

    props = manifest.output_schema.get("properties", {})
    return set(props.keys()) if isinstance(props, dict) else set()


def _for_each_var(rule_for_each: str | None) -> str | None:
    if rule_for_each is None:
        return None
    m = _FOR_EACH_VAR_RE.search(rule_for_each)
    return m.group(1) if m is not None else None


# --------------------------------------------------------------------------- #
# 防漂移: every builtin inspector is classified exactly once.
# --------------------------------------------------------------------------- #


class TestAntiDriftClassification:
    """spec §场景:防漂移——每个内置 inspector 必在 allowlist 或 backlog 之一."""

    def test_allowlist_initial_membership_is_flagship_only(self) -> None:
        # Pin the initial allowlist to the single旗舰 sample so a casual edit
        # that widens it without migrating + asserting the new inspector fails
        # loud. 各域长尾 PR intentionally edit this assertion when they migrate.
        assert frozenset({"linux.systemd.failed_units"}) == _MIGRATED_ALLOWLIST

    def test_allowlist_members_are_real_builtins(self) -> None:
        all_names = set(_builtin_registry_names())
        unknown = _MIGRATED_ALLOWLIST - all_names
        assert not unknown, f"allowlist references non-builtin inspector(s): {sorted(unknown)}"

    def test_every_builtin_inspector_is_classified(self) -> None:
        # The drift guard: BOTH tiers are explicit literals, so a newly-added
        # builtin inspector lands in neither → `unclassified` is non-empty → this
        # test fails and forces the author to classify it (migrate to the
        # allowlist with a 中文 message, or park it in `_BACKLOG`). A *derived*
        # backlog would auto-absorb new inspectors and make this guard vacuous.
        all_names = set(_builtin_registry_names())
        # allowlist and backlog are disjoint (no inspector in both tiers).
        both = _MIGRATED_ALLOWLIST & _BACKLOG
        assert not both, f"inspector(s) in both allowlist and backlog: {sorted(both)}"
        # No tier references a non-existent inspector.
        stale = (_MIGRATED_ALLOWLIST | _BACKLOG) - all_names
        assert not stale, f"allowlist/backlog reference non-builtin inspector(s): {sorted(stale)}"
        # Every builtin is classified — a new inspector in neither tier fails here.
        unclassified = all_names - _MIGRATED_ALLOWLIST - _BACKLOG
        assert not unclassified, (
            f"unclassified builtin inspector(s): {sorted(unclassified)} — add each to "
            f"_MIGRATED_ALLOWLIST (with a 中文 message) or _BACKLOG"
        )
        # The backlog must be non-empty while the long-tail rewrite is ongoing.
        assert _BACKLOG, "backlog empty — every inspector migrated? update the tiering"


# --------------------------------------------------------------------------- #
# Message-quality assertions — ALLOWLIST ONLY.
# --------------------------------------------------------------------------- #


class TestMigratedAllowlistMessageQuality:
    """spec §需求:FindingRule message 必须是简短中文标签 + 注入关键数据.

    These assertions apply ONLY to the migrated allowlist. The backlog (71
    not-yet-migrated inspectors) is deliberately exempt — see module docstring.
    """

    @pytest.mark.parametrize("name", sorted(_MIGRATED_ALLOWLIST), ids=sorted(_MIGRATED_ALLOWLIST))
    def test_no_empty_pointer_phrase(self, name: str) -> None:
        # (d) — no `see X for details` 类 empty-pointer in any FindingRule message.
        manifest = _load_builtin(name)
        for index, rule in enumerate(manifest.findings):
            assert not _EMPTY_POINTER_RE.search(rule.message), (
                f"{name} findings[{index}]: message contains a 'see ... for "
                f"details' empty-pointer phrase: {rule.message!r}"
            )

    @pytest.mark.parametrize("name", sorted(_MIGRATED_ALLOWLIST), ids=sorted(_MIGRATED_ALLOWLIST))
    def test_message_contains_cjk(self, name: str) -> None:
        # (a) — every FindingRule message carries a 中文标签 (≥1 CJK char).
        manifest = _load_builtin(name)
        for index, rule in enumerate(manifest.findings):
            assert _has_cjk(rule.message), (
                f"{name} findings[{index}]: message has no Chinese character "
                f"(中文标签 required): {rule.message!r}"
            )

    @pytest.mark.parametrize("name", sorted(_MIGRATED_ALLOWLIST), ids=sorted(_MIGRATED_ALLOWLIST))
    def test_injected_fields_are_declared(self, name: str) -> None:
        # (c) — if-inject-then-declared守卫. Every `{field}` a message injects
        # must have its root declared in output_schema.properties (防 KeyError).
        # The for_each loop variable (if any) is bound per-iteration, NOT an
        # output field, so it is excluded from the declared-field requirement.
        # This does NOT mandate injection — a纯标签 message with no `{field}`
        # passes vacuously and needs no豁免 marker.
        manifest = _load_builtin(name)
        declared = _declared_output_fields(manifest)
        for index, rule in enumerate(manifest.findings):
            loop_var = _for_each_var(rule.for_each)
            injected = _injected_root_fields(rule.message)
            if loop_var is not None:
                injected.discard(loop_var)
            missing = injected - declared
            assert not missing, (
                f"{name} findings[{index}]: message injects field(s) {sorted(missing)} "
                f"not declared in output_schema.properties {sorted(declared)} "
                f"(if-inject-then-declared guard, message={rule.message!r})"
            )


# --------------------------------------------------------------------------- #
# Backlog negative control — the exemption is real (non-vacuous tiering).
# --------------------------------------------------------------------------- #


class TestBacklogIsExemptFromQualityAssertions:
    """Prove the message-quality assertions are NOT applied to the backlog.

    Without this, a future refactor that accidentally widens the parametrize to
    the full registry would turn 71 English messages red — but only at that
    later edit. This positive control documents (and locks) the intent: at least
    one backlog inspector ships a non-CJK English message today, and that is
    accepted by the contract until its域 is migrated.
    """

    def test_at_least_one_backlog_inspector_has_english_only_message(self) -> None:
        english_only = []
        for name in sorted(_BACKLOG):
            manifest = _load_builtin(name)
            for rule in manifest.findings:
                if rule.message and not _has_cjk(rule.message):
                    english_only.append(name)
                    break
        assert english_only, (
            "expected ≥1 backlog inspector with an English-only message (the "
            "tiering exists precisely to NOT assert中文 on un-migrated inspectors); "
            "found none — has the long tail already been migrated?"
        )
