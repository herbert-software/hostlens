"""Schedule manifest schema SOT — strongly-typed Pydantic v2 models.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``.

The manifest (`schedules/*.yaml`) is the scheduling source of truth. This
module defines the **field-level** contract; cross-file / registry-aware
semantic checks (target existence, name uniqueness, M4 single-target) live
in `loader.py` because they need an injected `TargetRegistry`.

Discriminated `schedule` (design D-8): exactly one of `cron` (standard
5-field crontab) or `interval` (`IntervalSpec`) plus a `zoneinfo`-resolvable
`timezone`. The model validators reject "both" / "neither" / illegal
timezone / non-5-field cron / all-zero interval so an invalid manifest
fails at parse time (fail-loud) rather than at fire time.

`report.diff_with_last` and `notify` are **M4 placeholders**: typed and
parsed, but never consumed (no auto-diff at assembly, no notification send,
no secret resolution). See design D-9.
"""

from __future__ import annotations

from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "IntervalSpec",
    "NotifyConfig",
    "ReportConfig",
    "ScheduleManifest",
    "ScheduleSpec",
]


class IntervalSpec(BaseModel):
    """Interval trigger fields (mirrors APScheduler ``IntervalTrigger``).

    At least one field must be a positive integer (`model_validator`
    rejects all-zero / all-omitted). Maps to ``IntervalTrigger(weeks=...,
    days=..., hours=..., minutes=..., seconds=..., timezone=tz)``.
    """

    model_config = ConfigDict(extra="forbid")

    weeks: int = 0
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0

    @model_validator(mode="after")
    def _at_least_one_positive(self) -> Self:
        if not any(
            value > 0 for value in (self.weeks, self.days, self.hours, self.minutes, self.seconds)
        ):
            raise ValueError(
                "interval must declare at least one positive period field "
                "(weeks/days/hours/minutes/seconds); all-zero / all-omitted "
                "interval has no valid period"
            )
        return self


class ScheduleSpec(BaseModel):
    """cron / interval discriminated trigger spec + timezone.

    Exactly one of ``cron`` / ``interval`` (`model_validator` enforces the
    XOR). ``cron`` is a **standard 5-field crontab** (minute hour
    day-of-month month day-of-week); second-level cron is a non-goal (use
    ``interval``). ``timezone`` must be ``zoneinfo``-resolvable.
    """

    model_config = ConfigDict(extra="forbid")

    cron: str | None = None
    interval: IntervalSpec | None = None
    timezone: str

    @model_validator(mode="after")
    def _exactly_one_trigger(self) -> Self:
        has_cron = self.cron is not None
        has_interval = self.interval is not None
        if has_cron and has_interval:
            raise ValueError(
                "schedule must provide exactly one of 'cron' or 'interval', "
                "not both (cron and interval are mutually exclusive)"
            )
        if not has_cron and not has_interval:
            raise ValueError(
                "schedule must provide exactly one of 'cron' or 'interval'; neither was provided"
            )
        return self

    @model_validator(mode="after")
    def _cron_is_standard_five_field(self) -> Self:
        if self.cron is None:
            return self
        # Standard 5-field crontab only; APScheduler's `from_crontab`
        # accepts exactly 5 fields. Validate field count first so a 6-field
        # (second-level) expression is rejected with a precise message
        # rather than a generic parse error.
        field_count = len(self.cron.split())
        if field_count != 5:
            raise ValueError(
                f"cron must be a standard 5-field crontab "
                f"(minute hour day-of-month month day-of-week); "
                f"got {field_count} field(s): {self.cron!r}"
            )
        try:
            CronTrigger.from_crontab(self.cron)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid cron expression {self.cron!r}: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _timezone_is_resolvable(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"invalid timezone {self.timezone!r}: not resolvable by zoneinfo"
            ) from exc
        return self


class ReportConfig(BaseModel):
    """Report rendering config — aligned with `inspect` / `reports` CLI.

    ``format`` is **consumed** (drives render format). ``diff_with_last`` is
    an **M4 placeholder**: parsed as a typed field but never consumed (no
    auto-diff at report assembly, no embedded diff section — regression diff
    stays a post-hoc `reports diff` op). See design D-9.

    ``format`` is ``Literal["md", "json"]`` to match the existing
    ``--format`` literal; ``markdown`` / ``html`` are deliberately rejected.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["md", "json"] = "md"
    diff_with_last: bool = False


class NotifyConfig(BaseModel):
    """Notify channel config — **M4 placeholder** (typed, not consumed).

    M5 will consume `channel` + `only_if` for routing. M4 parses this as a
    typed structure (so a manifest carrying `notify` validates) but never
    evaluates `only_if`, resolves `${ENV_VAR}` secrets, or instantiates any
    Notifier. ``extra="allow"`` keeps M5-bound fields parseable without
    pinning their full shape into the M4 contract.
    """

    model_config = ConfigDict(extra="allow")

    channel: str
    only_if: str | None = None


class ScheduleManifest(BaseModel):
    """A single ``schedules/*.yaml`` manifest — one APScheduler job.

    ``extra="forbid"``: an unknown top-level field (e.g. a misspelled
    ``scheduel``) raises ``ValidationError`` rather than being silently
    dropped. ``targets`` is ``list[str]`` (list form reserved for future
    fan-out) but the **loader** enforces exactly one member in M4.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    schedule: ScheduleSpec
    targets: list[str] = Field(min_length=1)
    intent: str = Field(min_length=1)
    inspectors: list[str] | None = None
    report: ReportConfig = ReportConfig()
    notify: list[NotifyConfig] = []

    @field_validator("name", mode="after")
    @classmethod
    def _name_is_valid_job_id(cls, value: str) -> str:
        if "/" in value or "\\" in value or value != value.strip():
            raise ValueError(
                "name must be a valid job_id: no path separators (/ \\) or surrounding whitespace"
            )
        return value
