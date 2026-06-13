"""Pydantic schemas for the `list_channels` ToolSpec + `ChannelSummary`.

`ChannelSummary` is the read-only `{name, type}` projection of one
`notifiers.yaml` channel entry. It is a **positive whitelist** (not a
"deny token" blacklist): `extra="forbid"` seals the shape so the only two
fields that can ever reach the surface are `name` (the instance key) and
`type` (the channel type). Any credential key in the raw entry
(`bot_token` / `webhook_url` / `secret` / `chat_id` ...), and even its
`${ENV_VAR}` literal, is dropped at the source because the
`load_channel_summaries` reader only ever copies `name` / `type` — it does
**not** reuse `notifiers.config.load_channels` (which expands `${ENV_VAR}`
into plaintext secrets).

Per design D-2 / D-7.1: a channel config has no `enabled` field and
`only_if` is a per-schedule notify binding (surfaced by `list_schedules`,
not by `list_channels`), so neither appears here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ChannelSummary",
    "ListChannelsInput",
    "ListChannelsOutput",
]


class ChannelSummary(BaseModel):
    """Read-only `{name, type}` projection of one notifier channel.

    `extra="forbid"` physically enforces the positive whitelist: no
    credential key, no `${ENV_VAR}` literal, no `enabled` / `only_if` can
    be smuggled in. Constructing a `ChannelSummary` with any extra key
    raises `ValidationError`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str


class ListChannelsInput(BaseModel):
    """Input schema for `list_channels` — no parameters."""

    model_config = ConfigDict(extra="forbid")


class ListChannelsOutput(BaseModel):
    """Output schema for `list_channels` — a list of `{name, type}` summaries."""

    model_config = ConfigDict(extra="forbid")

    channels: list[ChannelSummary]
