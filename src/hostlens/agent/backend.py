"""LLM backend Protocol, data models, and capability gate.

This module is the single abstraction surface between the Agent loop and the
underlying LLM provider. Per CLAUDE.md §4.11 it is **Anthropic-schema-first**:
input dicts (``system`` / ``messages`` / ``tools``) flow through verbatim to
the SDK; the response is wrapped in a thin Pydantic model so the Agent loop
gets typed attribute access without depending on the SDK object types.

Module growth is intentionally staged across the
``add-llm-backend-protocol`` change groups:

- Group 1: ``BackendCapabilities`` dataclass.
- Group 2: ``MessageResponse`` data model family (``TextBlock`` /
  ``ToolUseBlock`` / ``ContentBlock`` / ``Usage`` / ``MessageResponse``),
  the ``LLMBackend`` runtime-checkable Protocol, and the optional
  ``BackendDiagnostics`` Protocol with its ``BackendHealth`` /
  ``QuotaStatus`` payload models.
- Group 3: ``check_capability_consistency`` helper + ``api_key_fingerprint``.
- Group 4 (this commit): ``create_backend`` factory + ``is_daemon_mode``
  hook. The factory wires ``BackendSettings`` / ``AgentSettings`` into
  the three M2 backend implementations and gates ``ensure_safe_for_daemon``
  via the daemon-mode hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hostlens.core.exceptions import BackendCapabilityViolation, ConfigError

if TYPE_CHECKING:
    from hostlens.core.config import Settings

__all__ = [
    "BackendCapabilities",
    "BackendDiagnostics",
    "BackendHealth",
    "ContentBlock",
    "LLMBackend",
    "MessageResponse",
    "QuotaStatus",
    "RedactedThinkingBlock",
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "Usage",
    "api_key_fingerprint",
    "check_capability_consistency",
    "create_backend",
    "is_daemon_mode",
]


@dataclass(frozen=True)
class BackendCapabilities:
    """Declared LLM provider capabilities consumed by the Agent loop.

    All 7 boolean fields are **required** (no defaults) so a backend cannot
    silently default-declare an unsupported capability. The set is locked to
    capabilities the Agent loop actively branches on; new fields are only
    added when a real consumer needs them (CLAUDE.md §4.11 "按需扩展").

    Fields:
        prompt_caching: ``cache_control: ephemeral`` block actually takes
            effect (Anthropic API). When False the Agent loop MUST NOT inject
            ``cache_control``; if it does, the backend raises
            ``BackendCapabilityViolation`` rather than silently dropping the
            field (CLAUDE.md §4.11 rule #2).
        tool_use: Anthropic ``tool_use`` API supported.
        structured_output: ``tool_use`` schema usable as structured-output
            forcing JSON shape (Hostlens Planner consumes this).
        parallel_tool_use: Multiple ``tool_use`` blocks emitted per turn.
        extended_thinking: Extended thinking mode (M3+ Diagnostician). M2
            backends MUST declare False because the Protocol signature does
            not yet carry a ``thinking`` parameter. This flag means **only**
            "Hostlens does not actively request thinking" — it does **not**
            mean "the response will be thinking-free": inbound thinking is
            tolerated unconditionally (``ContentBlock`` now includes
            ``ThinkingBlock`` / ``RedactedThinkingBlock``), so a consumer
            MUST NOT treat ``extended_thinking == False`` as a guarantee that
            ``response.content`` contains no thinking blocks. Tolerating
            inbound thinking needs no capability field because the Agent loop
            never branches on it (design.md D-2); the flag stays False.
        vision: Image inputs accepted (placeholder; Hostlens does not use).
        streaming: Streaming responses (placeholder; M2 fixed False — the
            Protocol returns a single ``MessageResponse``, not a chunk
            iterator).
    """

    prompt_caching: bool
    tool_use: bool
    structured_output: bool
    parallel_tool_use: bool
    extended_thinking: bool
    vision: bool
    streaming: bool


class TextBlock(BaseModel):
    """``type="text"`` content block returned by the Anthropic Messages API.

    The Pydantic shape mirrors the SDK ``anthropic.types.TextBlock`` only on
    the fields the Agent loop actually consumes — extra SDK fields (e.g.
    ``citations``) are silently dropped by ``MessageResponse``'s
    ``extra="ignore"`` config when they arrive on the wire, but we do not
    re-declare them here because the Agent loop never branches on them.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["text"]
    text: str


class ToolUseBlock(BaseModel):
    """``type="tool_use"`` content block returned by the Anthropic Messages API.

    Carries the structured tool invocation the Agent loop must dispatch to
    the ``ToolRegistry`` (CLAUDE.md §4.10).
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ThinkingBlock(BaseModel):
    """``type="thinking"`` content block (extended-thinking / reasoning trace).

    Modeled to **tolerate** inbound thinking blocks that thinking-on
    anthropic-compatible endpoints (e.g. DeepSeek v4 via
    ``https://api.deepseek.com/anthropic``) force into the response — the
    Agent loop must parse them without crashing and relay them verbatim in
    multi-turn tool loops (omitting a prior turn's thinking block makes the
    next turn 400). This is the Path-1 tolerate slice: Hostlens neither
    actively requests nor consumes thinking (that is the future Path 2).

    ``extra="allow"`` (unlike ``TextBlock`` / ``ToolUseBlock``'s
    ``extra="ignore"``): a thinking block is a verbatim-relay object, so
    ``model_dump()`` must preserve any provider-private fields rather than
    drop them — otherwise the relayed block stops being byte-for-byte
    faithful (design.md D-4).

    ``signature`` is a **required** ``str``: DeepSeek pro/flash and native
    Anthropic both always carry it (DeepSeek's value happens to equal the
    message ``id``, which is irrelevant to Hostlens — it is simply a string
    that must be relayed verbatim). Modeling it optional would let
    ``model_dump()`` emit ``"signature": null`` and change the wire shape; a
    block that genuinely lacks it should surface as ``invalid_response``,
    not be silently tolerated (design.md D-5).
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["thinking"]
    thinking: str
    signature: str


class RedactedThinkingBlock(BaseModel):
    """``type="redacted_thinking"`` content block (opaque redacted reasoning).

    Native Anthropic emits ``redacted_thinking`` blocks carrying only an
    opaque ``data`` payload (no ``signature``) when a reasoning trace is
    redacted. Modeled separately from ``ThinkingBlock`` so the discriminated
    union routes it correctly: filtering only on ``type="thinking"`` would
    drop redacted blocks and break the multi-turn verbatim-relay protocol.

    ``extra="allow"`` for the same verbatim-relay reason as ``ThinkingBlock``
    (design.md D-4). Shape is taken from the native Anthropic spec — not yet
    observed from DeepSeek probes — so it is a defensive model against
    dropping the block, backed by ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["redacted_thinking"]
    data: str


# Discriminated union by ``type`` field. The ``Annotated[..., Field(discriminator=...)]``
# form is required so Pydantic v2 produces stable validation errors for unknown
# ``type`` values; a bare union would not enforce strict discriminator
# semantics inside list containers (so a genuinely unknown block type would
# yield an unstable error instead of a clean ``union_tag_invalid``).
ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock,
    Field(discriminator="type"),
]


class Usage(BaseModel):
    """Token usage block from the Anthropic Messages API response.

    The four fields below are the ones the Agent loop cares about: ``input``
    / ``output`` for budget accounting and ``cache_creation`` /
    ``cache_read`` for prompt-caching effectiveness metrics (CLAUDE.md §4.8).
    The cache fields default to 0 so a backend / cassette that omits them
    (e.g. ``FakeBackend`` responses constructed in tests) still validates.

    The Anthropic SDK ``Usage.model_dump()`` emits explicit ``None`` for
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` on
    non-cached responses; Pydantic v2 does not fall back to a field default
    when the input is explicitly ``None``, so a ``before`` validator
    normalizes ``None`` → 0. Keeping the public type as ``int`` lets the
    Agent loop add the fields together without ``or 0`` at every call site;
    the Anthropic API never reports negative counts so 0 is unambiguous.
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @field_validator(
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        mode="before",
    )
    @classmethod
    def _none_to_zero(cls, v: Any) -> Any:
        return 0 if v is None else v


class MessageResponse(BaseModel):
    """Thin typed wrapper over the Anthropic ``Message`` response object.

    Per design.md D-8 the request side stays as raw ``list[dict]`` for max
    contract stability with the SDK, but the response is wrapped here so the
    Agent loop gets attribute access (``response.stop_reason`` etc.) instead
    of dict indexing. ``extra="ignore"`` lets the SDK add fields without
    breaking us (spec §需求:MessageResponse §场景:SDK 新增字段不破坏解析).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    model: str
    role: Literal["assistant"]
    content: list[ContentBlock]
    stop_reason: Literal[
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
        "pause_turn",
        "refusal",
    ]
    usage: Usage


@runtime_checkable
class LLMBackend(Protocol):
    """Anthropic-schema-first protocol the Agent loop talks through.

    Implementations MUST pass ``system`` / ``messages`` / ``tools`` through
    verbatim — no silent normalization, no vendor-agnostic abstraction
    (CLAUDE.md §4.11 rule). When the input asks for a capability the
    backend does not declare (e.g. ``cache_control`` block on a backend with
    ``capabilities.prompt_caching == False``), the implementation MUST raise
    ``BackendCapabilityViolation`` rather than silently drop the field.
    """

    name: str
    capabilities: BackendCapabilities

    async def messages_create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> MessageResponse: ...


class BackendHealth(BaseModel):
    """Result of a ``BackendDiagnostics.health_check`` call.

    ``error`` is the last failure message after backend-internal redaction
    (the redaction is the backend's responsibility — see
    ``hostlens.core.redact``; ``BackendHealth`` itself accepts any string).
    """

    is_healthy: bool
    backend_name: str
    latency_ms: float | None = None
    error: str | None = None


class QuotaStatus(BaseModel):
    """Best-effort remaining-quota snapshot for a backend.

    All fields optional — backends that cannot introspect quota
    (``AnthropicAPIBackend`` in M2) return ``None`` for the whole
    ``QuotaStatus`` from ``quota_check`` rather than filling zeros.
    """

    remaining_input_tokens: int | None = None
    remaining_output_tokens: int | None = None
    reset_at: datetime | None = None


@runtime_checkable
class BackendDiagnostics(Protocol):
    """Optional duck-typed diagnostics surface for a backend.

    Kept independent of ``LLMBackend`` so test backends (``FakeBackend`` /
    ``PlaybackBackend``) can skip it cleanly — ``hostlens doctor`` uses
    ``isinstance(backend, BackendDiagnostics)`` to detect support
    (design.md D-3).
    """

    async def health_check(self) -> BackendHealth: ...

    async def quota_check(self) -> QuotaStatus | None: ...

    def ensure_safe_for_daemon(self) -> None: ...


# ---------------------------------------------------------------------------
# Helpers: api_key_fingerprint + capability gate
# ---------------------------------------------------------------------------


# Minimum api_key length below which slicing the first 4 + last 4 chars would
# overlap or expose nearly the full original value. Per spec §需求:Backend 实现
# 必须脱敏所有敏感字段 §场景:短 api_key 不切片泄露, anything shorter than 12
# characters MUST collapse to a constant ``"<redacted>"`` placeholder.
_API_KEY_FINGERPRINT_MIN_LEN = 12


def api_key_fingerprint(secret: str | None) -> str:
    """Return a non-reversible fingerprint suitable for logs and ``__repr__``.

    Output domain (deterministic given the input):

    - ``None`` / empty string → ``"<unset>"``
    - shorter than ``_API_KEY_FINGERPRINT_MIN_LEN`` (12) → ``"<redacted>"``
      (slicing would expose >50% of the value)
    - otherwise → ``f"{secret[:4]}...{secret[-4:]}"``

    This helper is the **only** sanctioned way to display an Anthropic /
    Bedrock / Vertex API key in user-visible output; ``__repr__`` of any
    backend implementation calls into this function (see
    ``AnthropicAPIBackend.__repr__``).
    """

    if secret is None or secret == "":
        return "<unset>"
    if len(secret) < _API_KEY_FINGERPRINT_MIN_LEN:
        return "<redacted>"
    return f"{secret[:4]}...{secret[-4:]}"


def _scan_cache_control_in_block_list(
    blocks: list[dict[str, Any]] | Any,
) -> bool:
    """Return True if any block in ``blocks`` carries a ``cache_control`` key.

    Defensive against non-list ``blocks`` values (e.g. ``messages[*].content``
    is sometimes a bare string when the caller passed a plain message). Only
    a list of dicts is scanned; everything else returns False.
    """

    if not isinstance(blocks, list):
        return False
    return any(isinstance(block, dict) and "cache_control" in block for block in blocks)


def check_capability_consistency(
    *,
    backend_name: str,
    capabilities: BackendCapabilities,
    system: list[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> None:
    """Gate ``messages_create`` inputs against the backend's capability set.

    Per CLAUDE.md §4.11 rule #2 and spec §需求:`BackendCapabilityViolation`
    必须在 `cache_control` 与 capability 不一致时 raise, the backend MUST
    raise rather than silently strip ``cache_control`` blocks or honor a
    non-empty ``tools`` array when ``capabilities.tool_use == False``.

    Scans three locations for ``cache_control`` (in order, so the failing
    surface is reported correctly):

    1. ``system`` — when ``list[dict]`` form (Anthropic API allows ``str``
       as well, which by definition cannot carry ``cache_control``).
    2. ``messages[*].content`` — when ``content`` is a list of blocks.
    3. ``tools[*]`` — each tool definition may carry ``cache_control`` per
       the Anthropic API.

    Also rejects a non-empty ``tools`` array when the backend declared
    ``tool_use == False``.
    """

    # (a) system blocks
    if not capabilities.prompt_caching and _scan_cache_control_in_block_list(system):
        raise BackendCapabilityViolation(
            backend_name=backend_name,
            capability="prompt_caching",
            attempted_feature="cache_control_in_system_block",
        )

    # (b) messages[*].content[*] blocks
    if not capabilities.prompt_caching:
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if _scan_cache_control_in_block_list(content):
                raise BackendCapabilityViolation(
                    backend_name=backend_name,
                    capability="prompt_caching",
                    attempted_feature="cache_control_in_messages_block",
                )

    # (c) tools[*] blocks
    if not capabilities.prompt_caching and _scan_cache_control_in_block_list(tools):
        raise BackendCapabilityViolation(
            backend_name=backend_name,
            capability="prompt_caching",
            attempted_feature="cache_control_in_tools_array",
        )

    # tool_use gate
    if len(tools) > 0 and not capabilities.tool_use:
        raise BackendCapabilityViolation(
            backend_name=backend_name,
            capability="tool_use",
            attempted_feature="tools_array_non_empty",
        )


# ---------------------------------------------------------------------------
# Group 4: create_backend factory + is_daemon_mode hook
# ---------------------------------------------------------------------------


def is_daemon_mode(settings: Settings) -> bool:
    """Return whether Hostlens is running in scheduler daemon mode.

    M4 scope (add-scheduler, design D-12): reads ``settings.daemon_mode``.
    The ``schedule daemon`` / ``schedule run`` entry points set that flag to
    True before calling ``create_backend``, so the existing daemon-safety
    gate inside the factory fires for those long-running paths only. Every
    other code path leaves ``daemon_mode`` at its False default.

    The function is intentionally defined at module level (next to
    ``create_backend``) so tests can ``monkeypatch.setattr(
    "hostlens.agent.backend.is_daemon_mode", lambda s: True)`` and have the
    patch take effect on the factory's call site. Importing
    ``is_daemon_mode`` into another module and binding it to a local name
    would break that test pattern, which is why ``create_backend`` calls
    it via the module-level name rather than a local alias.
    """

    return settings.daemon_mode


def create_backend(settings: Settings) -> LLMBackend:
    """Single-source factory dispatching ``settings.backend.type`` to a backend.

    Construction policy (spec §需求:`create_backend` 工厂):

    - ``settings.backend is None`` → ``ConfigError`` (the caller asked for
      an LLM feature without configuring a backend).
    - ``type == "anthropic_api"`` → ``AnthropicAPIBackend`` with
      ``.get_secret_value()`` UNWRAPPING of the SecretStr so the live SDK
      client receives the raw key (the SecretStr's redacted ``str()`` would
      otherwise be sent on the wire and 401).
    - ``type == "fake"`` → empty-response ``FakeBackend`` (tests typically
      bypass this path and construct ``FakeBackend(responses=[...])``
      directly; this branch exists so a config-driven fake mode is
      possible without a separate code path).
    - ``type == "playback"`` → ``PlaybackBackend`` reading the cassette
      file at construction time.
    - ``type in {bedrock, vertex, claude_subscription}`` →
      ``NotImplementedError`` (M10.5 / 1.0 placeholders).

    Daemon-mode gate: after construction, when ``is_daemon_mode(settings)``
    returns True and the backend implements ``BackendDiagnostics``, the
    factory calls ``backend.ensure_safe_for_daemon()`` and lets any raised
    ``BackendDaemonUnsafe`` propagate (the caller — typically the Scheduler
    boot path — handles the failure). M2 ``is_daemon_mode`` returns False
    unconditionally so this branch is currently unreachable except via
    monkey-patch in tests.
    """

    if settings.backend is None:
        raise ConfigError("backend.type required to use LLM features")

    backend_settings = settings.backend
    backend_type = backend_settings.type

    # Lazy imports break the circular dependency: ``backends.anthropic_api``
    # / ``backends.fake`` / ``backends.playback`` all import from
    # ``hostlens.agent.backend`` (this module) at module load time. Importing
    # them at the top would create a cycle. The factory is the only place
    # that needs the concrete backend classes, so deferring import to call
    # time is the cleanest fix.
    # ``backend`` is typed as ``Any`` locally because the three concrete
    # implementations declare ``name`` / ``capabilities`` as ``ClassVar``
    # while ``LLMBackend`` Protocol expects them as instance vars; mypy
    # rejects a tight ``LLMBackend`` annotation under ``--strict``. The
    # function still returns ``LLMBackend`` and the duck-typed isinstance
    # check against ``BackendDiagnostics`` below stays sound at runtime.
    backend: Any
    if backend_type == "anthropic_api":
        if backend_settings.api_key is None:
            # Schema validator already enforces this; defensive check here
            # catches programmatic construction that bypassed validation.
            raise ConfigError("api_key required for type=anthropic_api")
        from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

        # ``base_url`` is an ``HttpUrl`` (or None); the SDK expects ``str``
        # so we coerce here rather than at the backend boundary so the
        # backend signature stays string-typed.
        base_url_str = str(backend_settings.base_url) if backend_settings.base_url else None
        # ``settings.agent`` is optional; fall back to the AgentSettings
        # default for ``health_check_model`` when the user did not
        # configure an agent block.
        health_check_model = (
            settings.agent.health_check_model if settings.agent is not None else "claude-haiku-4-5"
        )
        backend = AnthropicAPIBackend(
            api_key=backend_settings.api_key.get_secret_value(),
            base_url=base_url_str,
            health_check_model=health_check_model,
            disable_thinking=backend_settings.disable_thinking,
        )
    elif backend_type == "fake":
        from hostlens.agent.backends.fake import FakeBackend

        # Config-driven fake mode constructs an empty response queue. Tests
        # that need canned responses bypass the factory and construct
        # FakeBackend directly with ``responses=[...]``.
        backend = FakeBackend(responses=[])
    elif backend_type == "playback":
        if backend_settings.cassette_path is None:
            # Same defensive note as anthropic_api branch.
            raise ConfigError("cassette_path required for type=playback")
        from hostlens.agent.backends.playback import PlaybackBackend

        backend = PlaybackBackend(cassette_path=backend_settings.cassette_path)
    elif backend_type in ("bedrock", "vertex", "claude_subscription"):
        raise NotImplementedError(
            f"backend type {backend_type!r} 将在 M10.5 / 1.0 落地；当前请使用 anthropic_api"  # noqa: RUF001
        )
    else:
        # Unreachable: BackendType Literal covers all branches, but mypy /
        # runtime defense-in-depth still benefit from an explicit else.
        raise ConfigError(f"unknown backend.type: {backend_type!r}")

    # Daemon-mode gate via the module-level hook. The Protocol-level
    # ``isinstance`` check is duck-typed (``BackendDiagnostics`` is
    # ``runtime_checkable``) so backends that opt out (e.g. FakeBackend /
    # PlaybackBackend) skip cleanly.
    if is_daemon_mode(settings) and isinstance(backend, BackendDiagnostics):
        # Let any ``BackendDaemonUnsafe`` propagate to the caller; M5
        # Scheduler boot is the consumer.
        backend.ensure_safe_for_daemon()

    # Cast back to LLMBackend Protocol. All three branch-built objects
    # satisfy the protocol structurally; mypy's ``--strict`` cannot prove
    # this through the ``Any`` local (necessary for the ClassVar mismatch
    # workaround above), so an explicit return type assertion is needed.
    return backend  # type: ignore[no-any-return]
