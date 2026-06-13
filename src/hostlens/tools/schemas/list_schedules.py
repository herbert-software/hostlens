"""Pydantic schemas for the `list_schedules` ToolSpec.

`ScheduleSummary` projects one fresh-loaded `ScheduleManifest` into the
read-only surface shape: `name` / `schedule` expression / `next_fire_time`
/ `targets` / `intent` / `notify`. Each `notify` entry binds a `channel` to
an optional `only_if` routing expression — fulfilling the "routing
visibility is exposed by list_schedules" promise (design D-7.1). `only_if`
is manifest text, not a secret.

There is deliberately **no `enabled` field**: M4 has no schedule-level
on/off concept — every loaded manifest is active (design D-7.1 /
`ScheduleManifest` has no such field).

`next_fire_time` is computed in the handler directly from the manifest
trigger via apscheduler `CronTrigger` / `IntervalTrigger`; this schema only
holds the result. It is nullable because a trigger can have no further fire
time.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ListSchedulesInput",
    "ListSchedulesOutput",
    "ScheduleNotifyBinding",
    "ScheduleSummary",
]


class ScheduleNotifyBinding(BaseModel):
    """One `notify` binding on a schedule: `channel` + optional `only_if`.

    Mirrors `scheduler.schema.NotifyConfig`. `only_if` is the per-schedule
    routing expression (manifest text, not a secret); `None` means "always
    route to this channel".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    only_if: str | None = None


class ScheduleSummary(BaseModel):
    """Read-only projection of one `ScheduleManifest`.

    `schedule` is the human-readable trigger expression (e.g.
    `cron(0 9 * * *)` / `interval(1h)`). `next_fire_time` is the computed
    next fire instant (nullable). `notify` exposes per-schedule channel
    bindings + routing so a remote LLM can see where a schedule's report
    would be delivered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    schedule: str
    next_fire_time: datetime | None
    targets: list[str]
    intent: str
    notify: list[ScheduleNotifyBinding]


class ListSchedulesInput(BaseModel):
    """Input schema for `list_schedules` — no parameters."""

    model_config = ConfigDict(extra="forbid")


class ListSchedulesOutput(BaseModel):
    """Output schema for `list_schedules`."""

    model_config = ConfigDict(extra="forbid")

    schedules: list[ScheduleSummary]
