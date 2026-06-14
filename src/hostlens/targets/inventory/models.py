"""``CandidateTarget`` — an unvalidated import nomination.

Spec: ``inventory-source/spec.md`` §需求:`CandidateTarget` 必须是未验证候选.

``CandidateTarget`` is the source layer's output: a nomination intent that
has NOT yet been validated (``TargetEntry`` is the validated config-layer
type). Keeping the two distinct stops the source layer from polluting the
config-layer contract. The model carries credential **references** only
(``*_env`` env-var names / ``key_path``) — never a plaintext secret field.

``name`` is derived deterministically from the source's raw identifier
(ssh_config ``Host`` alias / yaml dict-key) via ``normalize_target_name``
so it satisfies ``TargetEntry.name``'s ``^[a-z][a-z0-9_-]{0,63}$`` regex
*before* it ever reaches Pydantic — otherwise an alias with uppercase /
dots / a leading digit would crash the whole batch at promotion time with
a ``ValidationError`` instead of a structured per-candidate error.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hostlens.core.exceptions import ConfigError

__all__ = [
    "CandidateTarget",
    "normalize_target_name",
]


# Mirror of ``targets/config.py:_NAME_PATTERN`` (``TargetEntry.name``). The
# normalized name MUST match this or promotion to ``TargetEntry`` fails.
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")

# Mirror of ``cli/target.py:_ENV_VAR_NAME_PATTERN`` — credential env-var
# references must be valid POSIX-ish env names so they round-trip back to
# a ``${VAR}`` placeholder the loader can actually expand.
_ENV_VAR_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Characters that survive into a valid name verbatim. Everything else
# (dots, spaces, uppercase after lowering, etc.) collapses to ``-``.
_NAME_ILLEGAL_RUN: re.Pattern[str] = re.compile(r"[^a-z0-9_-]+")


def normalize_target_name(raw: str) -> str:
    """Deterministically map a source identifier to a valid target name.

    Pipeline (spec §需求:name 派生契约): lowercase → replace any run of
    illegal characters (incl. dots) with a single ``-`` → collapse repeated
    ``-`` → strip leading non-``[a-z]`` characters → truncate to 64.

    Raises ``ConfigError(kind="invalid_target_name")`` when the result is
    empty or still violates ``_NAME_PATTERN`` (e.g. the raw identifier was
    pure punctuation like ``***``). The raw identifier is echoed in the
    error so the operator can locate the offending entry; it is the
    source's own alias / dict-key (not a secret).
    """

    lowered = raw.lower()
    dashed = _NAME_ILLEGAL_RUN.sub("-", lowered)
    collapsed = re.sub(r"-{2,}", "-", dashed)
    # Strip leading characters that are not ``[a-z]`` (the regex requires a
    # leading lowercase letter). Trailing ``-`` / ``_`` are allowed by the
    # pattern, so only the head is trimmed.
    stripped = re.sub(r"^[^a-z]+", "", collapsed)
    truncated = stripped[:64]

    if not truncated or _NAME_PATTERN.fullmatch(truncated) is None:
        raise ConfigError(
            "source identifier could not be normalized to a valid target name",
            kind="invalid_target_name",
            raw_identifier=raw,
        )
    return truncated


class CandidateTarget(BaseModel):
    """An unvalidated import nomination produced by an ``InventorySource``.

    ``type`` is ``Literal["local", "ssh"]`` — first-version import only
    produces these two; Pydantic rejects ``docker`` / ``k8s`` at parse time
    so an unsupported type cannot leak into the pipeline.

    Credentials are references only:

    - ``password_env`` / ``passphrase_env``: env-var names (validated
      against ``_ENV_VAR_NAME_PATTERN``) that round-trip to ``${VAR}``.
    - ``key_path``: a literal filesystem path (already ``~``-expanded by the
      source; ``${VAR}`` is rejected upstream, never expanded here).

    There is deliberately **no** plaintext ``password`` / ``passphrase``
    field: a source that encounters one fails closed at parse time.

    ``source_metadata`` carries the ``原始标识 → 派生 name`` mapping (and any
    other provenance) so ``ImportPlan`` can show the operator what was
    rewritten. It holds non-secret string scalars only.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN.pattern)
    type: Literal["local", "ssh"]
    host: str | None = None
    user: str | None = None
    port: int | None = None
    password_env: str | None = Field(default=None, pattern=_ENV_VAR_NAME_PATTERN.pattern)
    passphrase_env: str | None = Field(default=None, pattern=_ENV_VAR_NAME_PATTERN.pattern)
    key_path: str | None = None
    source_metadata: dict[str, str] = Field(default_factory=dict)
