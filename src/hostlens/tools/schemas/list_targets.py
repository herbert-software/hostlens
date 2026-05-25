"""Pydantic schemas for the `list_targets` ToolSpec + inventory scrubber.

`TargetSummary` is the M2 + M7-safe projection of an internal target
config: it carries **exactly** seven public fields and strictly forbids
any credential / connection / host / username / env / raw_config field
name (see spec §需求:TargetSummary 输出 schema 必须脱敏).

In addition to the field-name allowlist, every string value flowing
into a `TargetSummary` MUST pass through `scrub_inventory_string`,
which catches the case where a sensitive substring (path / IP / token /
username) is smuggled in via an otherwise-innocent field like
`display_name="login as admin@10.0.0.5"`.

Allowed capability tokens for `TargetSummary.capabilities`: this module
also exposes `CAPABILITY_ALLOWLIST`, used by
`hostlens.tools.default_tools.list_targets_handler` to strip
non-allowlisted capability tokens silently (e.g. an internal
"internal_admin_root" capability is filtered out before reaching the
agent). The allowlist intentionally mirrors the basic capability tokens
the M1 ExecutionTarget abstraction will expose.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from hostlens.targets.base import Capability

__all__ = [
    "CAPABILITY_ALLOWLIST",
    "ListTargetsInput",
    "ListTargetsOutput",
    "TargetSummary",
    "scrub_inventory_string",
]


# ---------------------------------------------------------------------------
# Capability allowlist (M1: derived from Capability enum — single source of truth)
# ---------------------------------------------------------------------------

CAPABILITY_ALLOWLIST: Final[frozenset[str]] = frozenset({c.value for c in Capability})
"""Capability tokens that may appear in `TargetSummary.capabilities`.

Derived from ``hostlens.targets.base.Capability`` (M1 single source of
truth). M1 value set is exactly ``{"shell", "file_read", "ssh",
"systemd", "docker_cli"}``; M8 ``K8S_EXEC`` and M9 ``FILE_WRITE`` will
expand both the enum and this allowlist together (spec §场景:capabilities
与 ``CAPABILITY_ALLOWLIST`` 严格相等).

Any token outside this set is silently dropped by
`list_targets_handler` before reaching the agent — this prevents
leaking internal capability names (e.g. ``"internal_admin_root"``) that
were never meant to be agent-visible.
"""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ListTargetsInput(BaseModel):
    """Input schema for `list_targets`."""

    model_config = ConfigDict(extra="forbid")

    include_disabled: bool = False


class TargetSummary(BaseModel):
    """M2 + M7-safe per-target summary.

    Field set is **exactly** the seven entries below — any addition must
    pass a separate spec review (credentials / connection strings / host
    / port / username / env / raw_config are forbidden field names per
    spec §需求:TargetSummary 输出 schema 必须脱敏).

    `kind` mirrors the ExecutionTarget Protocol `type` enum from
    docs/ARCHITECTURE.md §5: `"local" | "ssh" | "docker" | "k8s"`. Use
    `"k8s"` rather than `"kubernetes"` to avoid naming drift across
    layers.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["local", "ssh", "docker", "k8s"]
    display_name: str | None
    description: str | None
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    enabled: bool


class ListTargetsOutput(BaseModel):
    """Output schema for `list_targets`."""

    model_config = ConfigDict(extra="forbid")

    targets: list[TargetSummary]


# ---------------------------------------------------------------------------
# Inventory string scrubber
# ---------------------------------------------------------------------------

# Patterns whose match means: drop the whole TargetSummary (returns None
# from scrub_inventory_string). Half-leaked inventory is a worse outcome
# than a missing row.
_SKIP_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"/Users/[^/\s]+"
    r"|/home/[^/\s]+"
    r"|\.ssh(/|$)"
    r"|\.aws/credentials"
    r"|\.kube/config"
)

# IPv4 dotted-quad literal. We intentionally accept the loose
# \d{1,3}.\d{1,3}... form rather than a strict 0-255 validator: false
# positives in inventory strings (e.g. version numbers like "10.0.0.5"
# masquerading as IPs) are acceptable, false negatives are not.
_SKIP_IPV4_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")

# Simplified IPv6 detector: at least two `::` groups OR a hex+colon
# cluster long enough to be unmistakable. Avoids regex tarpit while
# catching common forms (`::1`, `fe80::1`, `2001:db8:85a3::8a2e:370:7334`).
_SKIP_IPV6_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]+"
    r"|::1\b"
    r"|::[0-9a-fA-F]{1,4}\b"
)

# Credential signatures: env-style `NAME_KEY=value` (with KEY/TOKEN/
# SECRET/PASSWORD suffix), Bearer tokens, OpenAI-style `sk-...`.
_SKIP_CRED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z]+_(?:KEY|TOKEN|SECRET|PASSWORD)=[^\s]+"
    r"|[Bb]earer\s+[\w.-]+"
    r"|sk-[a-zA-Z0-9]{20,}"
)

# Username-keyword pattern: literal "user" / "username" / "usr" as an
# *independent* word, followed by whitespace, followed by an identifier
# token. The leading `\b` rejects compound words like "user-service"
# (the `-` is not a word boundary on the left side of "user"; \b only
# fires if the preceding char is non-word, which `-` is) — but it would
# match "Owned by user alice". The trailing identifier token is captured
# in group 1 so we can replace just it with `"***"`, preserving the
# surrounding context.
_USERNAME_KEYWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:user|username|usr)\s+([A-Za-z0-9_.@-]+)"
)


def scrub_inventory_string(value: str, *, field_kind: str) -> str | None:
    """Scrub a string field value bound for a `TargetSummary`.

    Returns:
        - `None` if the value matches any "skip the entire target"
          pattern (path / IPv4 / IPv6 / credential signature). Callers
          MUST drop the whole target when they see `None`.
        - A redacted string when the "user / username / usr" keyword
          appears as an independent token followed by an identifier
          (the identifier is replaced with `"***"`; the keyword and
          surrounding context are preserved).
        - The original value otherwise.

    `field_kind` is unused by the matcher itself but is part of the
    public signature so that callers can pass it for structured logging
    when they decide to drop a target.
    """

    del field_kind  # callers use it for warning structured-field binding

    if (
        _SKIP_PATH_PATTERN.search(value) is not None
        or _SKIP_IPV4_PATTERN.search(value) is not None
        or _SKIP_IPV6_PATTERN.search(value) is not None
        or _SKIP_CRED_PATTERN.search(value) is not None
    ):
        return None

    if _USERNAME_KEYWORD_PATTERN.search(value) is not None:
        # Replace only the captured identifier (group 1) with "***",
        # keep keyword + surrounding context.
        return _USERNAME_KEYWORD_PATTERN.sub(
            lambda m: m.group(0).replace(m.group(1), "***"),
            value,
        )

    return value
