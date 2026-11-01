# services/session_schedule.py
"""Session-aware schedule planning for Flow workflows and /python strategies.

Both schedulers used to arm fixed ``HH:MM`` cron triggers from a weekday list.
A cron knows nothing about Diwali Muhurat on a Sunday, an early close on a
special session, or an MCX evening session that starts at 17:00 on a day NSE
is shut, so jobs were armed into sessions that had already ended and ran past
the close on shortened days. This module turns a schedule configuration into
two concrete IST instants instead:

* ``next_start`` — the next moment the schedule is allowed to start, i.e. the
  user's start time clipped forward into the exchange's actual session;
* ``must_stop``  — the moment it has to be flat/quiet, i.e. the user's stop
  clipped back to that session's actual close.

It also says *which* session those times come from (a regular weekday
session, a special session such as Muhurat, or an exchange's evening session
on another segment's holiday). When the calendar cannot be read the resolver
raises :class:`CalendarUnavailable` rather than guessing: the schedulers fail
closed and arm nothing.

All datetimes are timezone-aware IST. The calendar tables are the single
source of hours; this module only intersects them with the user's window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional

import pytz

from utils.constants import CRYPTO_EXCHANGES
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

#: Exchange consulted when a schedule names none.
DEFAULT_EXCHANGE = "NSE"

#: How far ahead to search for a qualifying session. Two weeks covers any
#: holiday cluster plus a weekend; beyond it the answer is more likely a bad
#: configuration than a wait the user intends.
MAX_SEARCH_DAYS = 14

#: Seconds past a boundary an aligned interval tick is placed, shared with the
#: Flow scheduler (kept overridable so its env setting stays authoritative).
DEFAULT_ALIGN_OFFSET_SECONDS = 2

# Blocked-plan reasons. The UIs localise from these codes.
REASON_INVALID_TIME = "invalid_time"
REASON_WINDOW_MISSES_SESSION = "window_misses_session"
REASON_NO_SESSION_IN_DAYS = "no_session_in_days"
REASON_TIME_IN_THE_PAST = "time_in_the_past"

_STR_TO_WEEKDAY = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


class CalendarUnavailable(RuntimeError):
    """Raised when the exchange calendar cannot be read.

    Distinct from "the exchange is closed" (a normal, answerable result).
    Callers must fail closed: arm no job and start nothing.
    """


@dataclass
class SessionPlan:
    """The concrete schedule resolved against one exchange's sessions."""

    exchange: str
    #: regular | special | evening | crypto | unrestricted
    kind: str
    #: True when no startable moment could be determined; nothing may start.
    blocked: bool = False
    #: Machine code for a blocked plan; None on a startable plan.
    reason: Optional[str] = None
    #: Holiday/session description from the calendar, when one applies.
    description: Optional[str] = None
    is_special: bool = False

    #: The session the next start was resolved against (ISO date string).
    session_date: Optional[str] = None
    session_start: Optional[datetime] = None
    session_end: Optional[datetime] = None
    #: Intersection of the user's window and the session.
    effective_start: Optional[datetime] = None
    effective_end: Optional[datetime] = None

    #: The next moment the schedule may start (IST aware).
    next_start: Optional[datetime] = None
    #: The moment the schedule must stop by (IST aware).
    must_stop: Optional[datetime] = None

    #: Human-readable description of the adopted session.
    session_label: str = ""
    message: str = ""

    @property
    def startable(self) -> bool:
        return not self.blocked and self.next_start is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form: ISO-8601 IST strings plus the display fields."""

        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value is not None else None

        return {
            "exchange": self.exchange,
            "kind": self.kind,
            "blocked": self.blocked,
            "startable": self.startable,
            "reason": self.reason,
            "description": self.description,
            "is_special": self.is_special,
            "session_date": self.session_date,
            "session_start": iso(self.session_start),
            "session_end": iso(self.session_end),
            "effective_start": iso(self.effective_start),
            "effective_end": iso(self.effective_end),
            "next_start": iso(self.next_start),
            "must_stop": iso(self.must_stop),
            "session_label": self.session_label,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_hhmm(value: Any) -> Optional[time]:
    """``"HH:MM"`` as a :class:`datetime.time`, else None (including ``None``).

    An empty value means "inherit the session edge". A present but malformed
    value is a configuration error the caller must not start from; detect it
    with ``value and result is None``.
    """
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        hours_str, minutes_str = text.split(":", 1)
        hours, minutes = int(hours_str), int(minutes_str)
        return time(hour=hours, minute=minutes)
    except (ValueError, TypeError, AttributeError):
        return None


def normalize_weekdays(values: Iterable[Any] | None) -> Optional[set[int]]:
    """Flow's 0..6 integers and /python's ``"mon"`` strings as an int set.

    None/empty means "every day" rather than "no days": every existing
    schedule treats the absence that way.
    """
    if not values:
        return None
    result: set[int] = set()
    for value in values:
        if isinstance(value, str):
            day = _STR_TO_WEEKDAY.get(value.strip().lower()[:3])
            if day is None:
                raise ValueError(f"Unknown weekday {value!r}")
        else:
            day = int(value)
            if not 0 <= day <= 6:
                raise ValueError(f"Weekday out of range: {value!r}")
        result.add(day)
    return result


def _coerce_now(now: Optional[datetime]) -> datetime:
    """Default to IST now; treat a naive datetime as IST."""
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return IST.localize(now)
    return now.astimezone(IST)


def _ms_to_ist(value_ms: int) -> datetime:
    return datetime.fromtimestamp(value_ms / 1000, IST)


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


# ---------------------------------------------------------------------------
# Label / message rendering
# ---------------------------------------------------------------------------


def build_session_label(exchange: str, window: dict[str, Any]) -> str:
    """One line naming the session a schedule was resolved against.

    Example output:
      * ``"NSE regular session 09:15-15:30 IST"``
      * ``"MCX evening session 17:00-23:55 IST (Holi)"``
      * ``"NSE special session 14:20-15:30 IST (Diwali Laxmi Pujan ...)"``
    """
    start = _ms_to_ist(window["start_ms"])
    end = _ms_to_ist(window["end_ms"])
    kind = window.get("kind", "regular")
    span = f"{_hhmm(start)}-{_hhmm(end)} IST"
    if kind == "crypto":
        return f"{exchange} trades 24/7"
    if kind == "special":
        name = window.get("description") or "Special session"
        return f"{exchange} special session {span} ({name})"
    if kind == "evening":
        name = window.get("description") or "Holiday hours"
        return f"{exchange} evening session {span} ({name})"
    return f"{exchange} regular session {span}"


def _build_message(plan: SessionPlan) -> str:
    if plan.blocked:
        return plan.message
    if plan.kind == "unrestricted":
        if plan.next_start is not None:
            return f"Next start {plan.next_start:%a %d %b %Y %H:%M IST} (market-hours gating off)"
        return "Market-hours gating is off"
    if plan.kind == "crypto":
        return f"{plan.exchange} 24/7 — next start {plan.next_start:%a %d %b %Y %H:%M IST}"
    stop_part = f", must stop {_hhmm(plan.must_stop)}" if plan.must_stop else ""
    return (
        f"Next start {plan.next_start:%a %d %b %Y %H:%M IST}{stop_part} · "
        f"{plan.session_label}"
    )


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------


def _blocked(
    exchange: str,
    kind: str,
    reason: str,
    message: str,
    *,
    description: Optional[str] = None,
) -> SessionPlan:
    plan = SessionPlan(
        exchange=exchange,
        kind=kind,
        blocked=True,
        reason=reason,
        description=description,
        message=message,
    )
    return plan


def resolve_plan(
    exchange: Optional[str],
    user_start: Any = None,
    user_stop: Any = None,
    weekdays: Iterable[Any] | None = None,
    now: Optional[datetime] = None,
) -> SessionPlan:
    """Resolve the next startable/stop pair for a repeating daily-style plan.

    Searches from today for up to :data:`MAX_SEARCH_DAYS`, accepting the first
    date that is in ``weekdays``, has a session for ``exchange`` and whose
    session window intersects the user's ``HH:MM`` window.

    Args:
        exchange: Calendar to use (defaults to NSE).
        user_start/user_stop: Optional ``HH:MM`` overrides. Empty/None inherits
            the session edge.
        weekdays: Allowed weekdays (ints 0..6 and/or ``"mon"`` strings).
            None/empty means every day.
        now: Override for the current IST-aware instant (tests).

    Raises:
        CalendarUnavailable: when the calendar store cannot be read.
    """
    from database.market_calendar_db import describe_session_window

    exch = (exchange or DEFAULT_EXCHANGE).strip().upper()
    now = _coerce_now(now)

    t_start = parse_hhmm(user_start)
    if user_start and t_start is None:
        return _blocked(
            exch,
            "regular",
            REASON_INVALID_TIME,
            f"Invalid start time {user_start!r}; schedule not armed",
        )
    t_stop = parse_hhmm(user_stop)
    if user_stop and t_stop is None:
        return _blocked(
            exch,
            "regular",
            REASON_INVALID_TIME,
            f"Invalid stop time {user_stop!r}; schedule not armed",
        )

    try:
        selected = normalize_weekdays(weekdays)
    except ValueError as exc:
        return _blocked(exch, "regular", REASON_INVALID_TIME, str(exc))

    saw_session = False
    saw_miss = False
    last_window: Optional[dict[str, Any]] = None

    try:
        for offset in range(MAX_SEARCH_DAYS + 1):
            day = (now + timedelta(days=offset)).date()
            if selected is not None and day.weekday() not in selected:
                continue

            window = describe_session_window(day, exch)
            if window is None:
                continue  # exchange shut this date (holiday/weekend)
            saw_session = True
            last_window = window

            session_start = _ms_to_ist(window["start_ms"])
            session_end = _ms_to_ist(window["end_ms"])

            edge_start = (
                IST.localize(datetime.combine(day, t_start)) if t_start else session_start
            )
            edge_end = IST.localize(datetime.combine(day, t_stop)) if t_stop else session_end

            effective_start = max(session_start, edge_start)
            effective_end = min(session_end, edge_end)
            if effective_start > effective_end:
                # The user's window never overlaps this session (e.g. a 09:15
                # entry against an MCX 17:00 evening-only holiday).
                saw_miss = True
                continue

            if now < effective_start:
                next_start = effective_start
            elif effective_start <= now <= effective_end:
                next_start = now
            else:
                # This session is already over; keep searching.
                continue

            return _make_plan(
                exch, window, day, session_start, session_end,
                effective_start, effective_end, next_start, effective_end,
            )
    except CalendarUnavailable:
        raise
    except Exception as exc:
        # Any other calendar-store failure is an unknown, not a closure.
        logger.exception(f"Session calendar unreadable for {exch}: {exc}")
        raise CalendarUnavailable(f"Could not read the {exch} session calendar") from exc

    if saw_session and saw_miss:
        return _blocked(
            exch,
            str(last_window.get("kind", "regular")) if last_window else "regular",
            REASON_WINDOW_MISSES_SESSION,
            f"The configured window never overlaps a {exch} session in the next "
            f"{MAX_SEARCH_DAYS} days; schedule not armed",
            description=last_window.get("description") if last_window else None,
        )
    return _blocked(
        exch,
        "regular",
        REASON_NO_SESSION_IN_DAYS,
        f"No {exch} session on the selected days in the next {MAX_SEARCH_DAYS} days",
    )


def _make_plan(
    exch: str,
    window: dict[str, Any],
    day: date,
    session_start: datetime,
    session_end: datetime,
    effective_start: datetime,
    effective_end: datetime,
    next_start: datetime,
    must_stop: datetime,
) -> SessionPlan:
    plan = SessionPlan(
        exchange=exch,
        kind=str(window.get("kind", "regular")),
        blocked=False,
        description=window.get("description"),
        is_special=bool(window.get("is_special")),
        session_date=day.isoformat(),
        session_start=session_start,
        session_end=session_end,
        effective_start=effective_start,
        effective_end=effective_end,
        next_start=next_start,
        must_stop=must_stop,
    )
    plan.session_label = build_session_label(exch, window)
    plan.message = _build_message(plan)
    return plan


def plan_for_datetime(
    exchange: Optional[str],
    run_at: datetime,
    user_start: Any = None,
    user_stop: Any = None,
    now: Optional[datetime] = None,
) -> SessionPlan:
    """Resolve a one-shot ("run once") schedule against that date's session.

    The run goes ahead at ``max(run_at, session open)`` when that is still
    inside the effective window; a date the exchange is shut, a window the
    instant falls outside, or an instant already past all block the plan.
    """
    from database.market_calendar_db import describe_session_window

    exch = (exchange or DEFAULT_EXCHANGE).strip().upper()
    now = _coerce_now(now)
    run_at = _coerce_now(run_at)

    t_start = parse_hhmm(user_start)
    if user_start and t_start is None:
        return _blocked(exch, "regular", REASON_INVALID_TIME,
                        f"Invalid start time {user_start!r}; schedule not armed")
    t_stop = parse_hhmm(user_stop)
    if user_stop and t_stop is None:
        return _blocked(exch, "regular", REASON_INVALID_TIME,
                        f"Invalid stop time {user_stop!r}; schedule not armed")

    if run_at < now:
        return _blocked(
            exch, "regular", REASON_TIME_IN_THE_PAST,
            f"The one-shot time {run_at:%a %d %b %Y %H:%M IST} has already passed",
        )

    day = run_at.date()
    try:
        window = describe_session_window(day, exch)
    except Exception as exc:
        logger.exception(f"Session calendar unreadable for {exch}: {exc}")
        raise CalendarUnavailable(f"Could not read the {exch} session calendar") from exc
    if window is None:
        return _blocked(
            exch, "regular", REASON_NO_SESSION_IN_DAYS,
            f"{exch} has no session on {day:%a %d %b %Y}; one-shot schedule not armed",
        )

    session_start = _ms_to_ist(window["start_ms"])
    session_end = _ms_to_ist(window["end_ms"])
    edge_start = (
        IST.localize(datetime.combine(day, t_start)) if t_start else session_start
    )
    edge_end = IST.localize(datetime.combine(day, t_stop)) if t_stop else session_end
    effective_start = max(session_start, edge_start)
    effective_end = min(session_end, edge_end)

    if effective_start > effective_end or run_at > effective_end:
        return _blocked(
            exch,
            str(window.get("kind", "regular")),
            REASON_WINDOW_MISSES_SESSION,
            f"{run_at:%H:%M} is outside {exch}'s {_hhmm(session_start)}-{_hhmm(session_end)} "
            f"session on {day:%a %d %b}; one-shot schedule not armed",
            description=window.get("description"),
        )

    next_start = max(run_at, effective_start)
    return _make_plan(
        exch, window, day, session_start, session_end,
        effective_start, effective_end, next_start, effective_end,
    )


# ---------------------------------------------------------------------------
# Interval alignment
# ---------------------------------------------------------------------------


def next_aligned_tick(
    now: datetime,
    value: int,
    unit: str,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    offset_seconds: int = DEFAULT_ALIGN_OFFSET_SECONDS,
) -> Optional[datetime]:
    """Next clock-aligned interval tick, clipped into a session window.

    A 5-minute job lands on :00/:05 with a small offset rather than five
    minutes after activation. Sub-minute intervals are not aligned (no
    meaningful boundary). Returns None when the next tick would fall on or
    after ``window_end`` — the session is over and the daily re-arm installs
    the next day's interval job.
    """
    now = _coerce_now(now)
    value = int(value or 1)

    if unit == "seconds":
        tick = now + timedelta(seconds=1)
        step = timedelta(seconds=value)
    else:
        step = timedelta(hours=value) if unit == "hours" else timedelta(minutes=value)
        if unit == "hours":
            anchor = now.replace(minute=0, second=0, microsecond=0)
        else:
            anchor = now.replace(second=0, microsecond=0)
            anchor -= timedelta(minutes=anchor.minute % value)
        tick = anchor + timedelta(seconds=offset_seconds)
        while tick <= now:
            tick += step

    if window_start is not None:
        while tick < window_start:
            tick += step
    if window_end is not None and tick > window_end:
        return None
    return tick


# ---------------------------------------------------------------------------
# Flow trigger planning
# ---------------------------------------------------------------------------


def _next_unrestricted_cron(
    now: datetime, time_str: Any, weekdays: Optional[set[int]]
) -> tuple[Optional[datetime], Optional[str]]:
    """Next future occurrence of a HH:MM cron on the allowed weekdays."""
    t = parse_hhmm(time_str)
    if time_str and t is None:
        return None, REASON_INVALID_TIME
    if t is None:
        t = time(hour=9, minute=15)
    for offset in range(MAX_SEARCH_DAYS + 1):
        day = (now + timedelta(days=offset)).date()
        if weekdays is not None and day.weekday() not in weekdays:
            continue
        candidate = IST.localize(datetime.combine(day, t))
        if candidate > now:
            return candidate, None
    return None, REASON_NO_SESSION_IN_DAYS


def _unrestricted_plan(data: dict[str, Any], now: datetime) -> SessionPlan:
    """Plan for a trigger with market-hours gating OFF: raw schedule math."""
    schedule_type = data.get("scheduleType")
    base = SessionPlan(
        exchange=str(data.get("marketHoursExchange") or DEFAULT_EXCHANGE).upper(),
        kind="unrestricted",
        session_label="Market-hours gating off",
    )

    if schedule_type == "once" and data.get("executeAt"):
        try:
            run_at = _coerce_now(
                datetime.fromisoformat(str(data["executeAt"]).replace("Z", "+00:00"))
            )
        except ValueError:
            return _blocked(base.exchange, "unrestricted", REASON_INVALID_TIME,
                            f"Invalid executeAt {data.get('executeAt')!r}")
        if run_at < now:
            return _blocked(base.exchange, "unrestricted", REASON_TIME_IN_THE_PAST,
                            "The one-shot time has already passed")
        base.next_start = run_at
        base.session_date = run_at.date().isoformat()
        base.message = _build_message(base)
        return base

    if schedule_type == "interval":
        tick = next_aligned_tick(
            now,
            int(data.get("intervalValue") or 1),
            str(data.get("intervalUnit") or "minutes"),
            offset_seconds=int(data.get("_alignOffsetSeconds", DEFAULT_ALIGN_OFFSET_SECONDS)),
        )
        base.next_start = tick
        base.message = _build_message(base)
        return base

    weekdays = None
    if schedule_type == "weekly":
        try:
            weekdays = normalize_weekdays(data.get("days"))
        except ValueError as exc:
            return _blocked(base.exchange, "unrestricted", REASON_INVALID_TIME, str(exc))

    candidate, reason = _next_unrestricted_cron(now, data.get("time", "09:15"), weekdays)
    if candidate is None:
        return _blocked(base.exchange, "unrestricted", reason or REASON_NO_SESSION_IN_DAYS,
                        "No scheduled occurrence in the planning window")
    base.next_start = candidate
    base.session_date = candidate.date().isoformat()
    base.message = _build_message(base)
    return base


def plan_for_flow_trigger(
    data: dict[str, Any], now: Optional[datetime] = None
) -> SessionPlan:
    """Resolve a Flow start-node config into a concrete session plan.

    With market-hours gating off the answer is plain cron math and the plan is
    never blocked by the calendar (legacy behaviour). With it on, every
    schedule type is resolved through the exchange's sessions.
    """
    now = _coerce_now(now)
    data = data or {}
    schedule_type = data.get("scheduleType")

    if not bool(data.get("marketHoursOnly", False)):
        return _unrestricted_plan(data, now)

    exchange = data.get("marketHoursExchange") or DEFAULT_EXCHANGE
    user_start = data.get("marketHoursStart") or None
    user_stop = data.get("marketHoursEnd") or None

    if schedule_type == "once" and data.get("executeAt"):
        try:
            run_at = _coerce_now(
                datetime.fromisoformat(str(data["executeAt"]).replace("Z", "+00:00"))
            )
        except ValueError:
            return _blocked(str(exchange).upper(), "regular", REASON_INVALID_TIME,
                            f"Invalid executeAt {data.get('executeAt')!r}")
        return plan_for_datetime(exchange, run_at, user_start, user_stop, now=now)

    if schedule_type == "interval":
        # Intervals have no weekday selector of their own; the user's
        # market-hours window still narrows the session as for daily jobs.
        plan = resolve_plan(exchange, user_start, user_stop, None, now=now)
        if plan.startable:
            tick = next_aligned_tick(
                now,
                int(data.get("intervalValue") or 1),
                str(data.get("intervalUnit") or "minutes"),
                window_start=plan.effective_start,
                window_end=plan.effective_end,
                offset_seconds=int(
                    data.get("_alignOffsetSeconds", DEFAULT_ALIGN_OFFSET_SECONDS)
                ),
            )
            if tick is None:
                # Inside the planning call "now" was within the window, so a
                # tick should always fit; treat the miss as closed-day drift.
                plan.blocked = True
                plan.reason = REASON_WINDOW_MISSES_SESSION
                plan.message = "No interval tick fits before the session closes"
            else:
                plan.next_start = tick
                plan.message = _build_message(plan)
        return plan

    weekdays = None
    if schedule_type == "weekly":
        try:
            weekdays = normalize_weekdays(data.get("days"))
        except ValueError as exc:
            return _blocked(str(exchange).upper(), "regular", REASON_INVALID_TIME, str(exc))

    return resolve_plan(exchange, user_start, user_stop, weekdays, now=now)
