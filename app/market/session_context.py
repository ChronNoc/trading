"""Timezone-aware trading-session context detection."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_SESSION_CONFIG = Path("config/session_profiles.yaml")


@dataclass(frozen=True, slots=True)
class SessionDefinition:
    """Configured session boundary."""

    name: str
    display_name: str
    timezone: str
    start: time
    end: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    @property
    def crosses_midnight(self) -> bool:
        """Return whether this session's end time falls on the next local day."""
        return self.end <= self.start


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Resolved session context for one instant."""

    name: str
    display_name: str
    session_date: date | None
    minutes_since_open: int | None
    minutes_until_close: int | None
    london_time: datetime
    new_york_time: datetime
    utc_time: datetime
    is_open: bool


@dataclass(frozen=True, slots=True)
class SessionContextResolver:
    """Resolve the current market session from timezone-aware boundaries."""

    sessions: tuple[SessionDefinition, ...]

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_SESSION_CONFIG) -> "SessionContextResolver":
        """Load session boundaries from YAML."""
        config_path = Path(path)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("session profile config must contain a mapping")
        definitions = []
        for item in raw.get("sessions", ()):
            if not isinstance(item, dict):
                raise ValueError("session entries must be mappings")
            definitions.append(
                SessionDefinition(
                    name=str(item["name"]),
                    display_name=str(item.get("display_name", item["name"])),
                    timezone=str(item["timezone"]),
                    start=_parse_hhmm(str(item["start"])),
                    end=_parse_hhmm(str(item["end"])),
                    weekdays=tuple(int(day) for day in item.get("weekdays", (0, 1, 2, 3, 4))),
                ),
            )
        return cls(sessions=tuple(definitions))

    def resolve(self, instant: datetime) -> SessionContext:
        """Return the configured session containing ``instant`` or Closed."""
        utc_instant = _as_utc(instant)
        london_time = utc_instant.astimezone(_timezone("Europe/London"))
        new_york_time = utc_instant.astimezone(_timezone("America/New_York"))

        for session in self.sessions:
            local = utc_instant.astimezone(_timezone(session.timezone))
            session_start, session_end = _bounds_for_local_date(local, session)
            if _local_weekday_allowed(session_start, session) and session_start <= local < session_end:
                return SessionContext(
                    name=session.name,
                    display_name=session.display_name,
                    session_date=session_start.date(),
                    minutes_since_open=int((local - session_start).total_seconds() // 60),
                    minutes_until_close=int((session_end - local).total_seconds() // 60),
                    london_time=london_time,
                    new_york_time=new_york_time,
                    utc_time=utc_instant,
                    is_open=True,
                )

        return SessionContext(
            name="closed",
            display_name="Closed",
            session_date=None,
            minutes_since_open=None,
            minutes_until_close=None,
            london_time=london_time,
            new_york_time=new_york_time,
            utc_time=utc_instant,
            is_open=False,
        )


def _bounds_for_local_date(local: datetime, session: SessionDefinition) -> tuple[datetime, datetime]:
    local_date = local.date()
    if session.crosses_midnight and local.timetz().replace(tzinfo=None) < session.end:
        local_date = local_date - timedelta(days=1)

    zone = _timezone(session.timezone)
    start = datetime.combine(local_date, session.start, tzinfo=zone)
    end_date = local_date + timedelta(days=1) if session.crosses_midnight else local_date
    end = datetime.combine(end_date, session.end, tzinfo=zone)
    return start, end


def _local_weekday_allowed(session_start: datetime, session: SessionDefinition) -> bool:
    return session_start.weekday() in set(session.weekdays)


def _parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except ValueError as error:
        raise ValueError(f"invalid session time {value!r}; expected HH:MM") from error


def _as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("session resolution requires a timezone-aware datetime")
    return instant.astimezone(UTC)


@functools.lru_cache(maxsize=32)
def _timezone(name: str) -> tzinfo:
    """Resolve a timezone, cached so tzdata is loaded once, not per event.

    ``ZoneInfo(name)`` re-reads the timezone database from disk on every
    call; session resolution invokes this ~14 times per market event, which
    throttled the live pipeline to ~128 events/sec and dropped bursts. The
    cache makes it effectively free after the first lookup.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/New_York":
            return _NewYorkFallback()
        if name == "Europe/London":
            return _LondonFallback()
        raise


class _NewYorkFallback(tzinfo):
    """Fallback New York timezone rules for Windows environments without tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-5)
        return timedelta(hours=-4 if _is_new_york_dst_local(dt.replace(tzinfo=None)) else -5)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1 if _is_new_york_dst_local(dt.replace(tzinfo=None)) else 0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        utc_naive = dt.replace(tzinfo=None)
        offset = timedelta(hours=-4 if _is_new_york_dst_utc(utc_naive) else -5)
        return (utc_naive + offset).replace(tzinfo=self)


class _LondonFallback(tzinfo):
    """Fallback London timezone rules for Windows environments without tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1 if _is_london_dst_local(dt.replace(tzinfo=None)) else 0)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1 if _is_london_dst_local(dt.replace(tzinfo=None)) else 0)

    def tzname(self, dt: datetime | None) -> str:
        return "BST" if self.dst(dt) else "GMT"

    def fromutc(self, dt: datetime) -> datetime:
        utc_naive = dt.replace(tzinfo=None)
        offset = timedelta(hours=1 if _is_london_dst_utc(utc_naive) else 0)
        return (utc_naive + offset).replace(tzinfo=self)


def _is_new_york_dst_utc(utc_naive: datetime) -> bool:
    year = utc_naive.year
    start = datetime.combine(_nth_weekday(year, 3, 6, 2), time(7, 0))
    end = datetime.combine(_nth_weekday(year, 11, 6, 1), time(6, 0))
    return start <= utc_naive < end


def _is_new_york_dst_local(local_naive: datetime) -> bool:
    year = local_naive.year
    start = datetime.combine(_nth_weekday(year, 3, 6, 2), time(2, 0))
    end = datetime.combine(_nth_weekday(year, 11, 6, 1), time(2, 0))
    return start <= local_naive < end


def _is_london_dst_utc(utc_naive: datetime) -> bool:
    year = utc_naive.year
    start = datetime.combine(_last_weekday(year, 3, 6), time(1, 0))
    end = datetime.combine(_last_weekday(year, 10, 6), time(1, 0))
    return start <= utc_naive < end


def _is_london_dst_local(local_naive: datetime) -> bool:
    year = local_naive.year
    start = datetime.combine(_last_weekday(year, 3, 6), time(2, 0))
    end = datetime.combine(_last_weekday(year, 10, 6), time(2, 0))
    return start <= local_naive < end


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    current = date(year, month, 1)
    days_until_weekday = (weekday - current.weekday()) % 7
    return current + timedelta(days=days_until_weekday + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)
