"""Tests for services.session_schedule — concrete next-start / must-stop planning.

Uses an in-memory market calendar seeded with the shipped 2026 holidays:
  - 2026-04-14 (Tue) Ambedkar Jayanti: NSE closed, MCX evening 17:00-23:55
  - 2026-11-08 (Sun) Diwali Muhurat: NSE special session 14:20-15:30
  - 2026-11-10 (Tue) Balipratipada: NSE closed, MCX evening
  - Plain weekdays carry the default NSE 09:15-15:30 window.

Run: uv run pytest test/test_session_schedule.py -v
"""

import os
import sys
from datetime import date, datetime

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(scope="module")
def calendar():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import StaticPool

    import database.market_calendar_db as mc

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mc.engine = engine
    mc.db_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    mc.Base.query = mc.db_session.query_property()
    mc.Base.metadata.create_all(engine)
    mc.clear_market_calendar_cache()
    mc.seed_holidays_2026()
    mc.seed_market_timings()
    return mc


@pytest.fixture()
def planner(calendar):
    from services import session_schedule as ss

    calendar.clear_market_calendar_cache()
    return ss


def _dt(day: date, hour: int, minute: int = 0):
    return IST.localize(datetime(day.year, day.month, day.day, hour, minute))


# ---------------------------------------------------------------------------
# Regular sessions
# ---------------------------------------------------------------------------


def test_regular_weekday_before_open_clips_user_start_to_session_open(planner):
    # Monday 2-Nov-2026 is a normal NSE day. User asks 09:00; the exchange
    # itself opens 09:15, so nothing may start until 09:15.
    mon = date(2026, 11, 2)
    assert mon.weekday() == 0

    plan = planner.resolve_plan("NSE", "09:00", "15:30", now=_dt(mon, 8, 0))

    assert plan.startable
    assert plan.kind == "regular"
    assert plan.next_start == _dt(mon, 9, 15)
    assert plan.must_stop == _dt(mon, 15, 30)
    assert plan.session_label.startswith("NSE regular session 09:15-15:30")


def test_after_close_moves_to_next_session(planner):
    mon = date(2026, 11, 2)
    plan = planner.resolve_plan("NSE", "09:15", "15:30", now=_dt(mon, 16, 0))
    assert plan.startable
    assert plan.next_start == _dt(date(2026, 11, 3), 9, 15)
    assert plan.must_stop == _dt(date(2026, 11, 3), 15, 30)


def test_inside_window_next_start_is_now(planner):
    mon = date(2026, 11, 2)
    now = _dt(mon, 10, 30)
    plan = planner.resolve_plan("NSE", "09:15", "15:30", now=now)
    assert plan.next_start == now


def test_missing_user_times_inherit_session_edges(planner):
    mon = date(2026, 11, 2)
    plan = planner.resolve_plan("NFO", None, None, now=_dt(mon, 8, 0))
    assert plan.next_start == _dt(mon, 9, 15)
    # NFO runs to 15:40 after the Closing Auction Session migration.
    assert plan.must_stop == _dt(mon, 15, 40)


def test_invalid_time_blocks(planner):
    plan = planner.resolve_plan("NSE", "25:99", None, now=_dt(date(2026, 11, 2), 8))
    assert plan.blocked
    assert plan.reason == planner.REASON_INVALID_TIME


# ---------------------------------------------------------------------------
# Partial holiday with an MCX evening session (Tue 14-Apr-2026)
# ---------------------------------------------------------------------------


def test_mcx_evening_session_on_nse_holiday(planner):
    tue = date(2026, 4, 14)
    plan = planner.resolve_plan("MCX", "09:00", "23:55", now=_dt(tue, 10, 0))

    assert plan.startable
    assert plan.kind == "evening"
    # User start 09:00 is clipped forward to the evening session's open.
    assert plan.next_start == _dt(tue, 17, 0)
    assert plan.must_stop == _dt(tue, 23, 55)
    assert "evening session 17:00-23:55" in plan.session_label


def test_nse_strategy_skips_holiday_to_next_regular_day(planner):
    tue = date(2026, 4, 14)
    plan = planner.resolve_plan("NSE", "09:15", "15:30", now=_dt(tue, 10, 0))
    assert plan.startable
    # 15-Apr-2026 Wednesday, normal session.
    assert plan.next_start == _dt(date(2026, 4, 15), 9, 15)
    assert plan.kind == "regular"


def test_mcx_window_that_misses_evening_blocks_one_shot(planner):
    # A 10:00 one-shot bounded to a 09:00-15:30 window can never meet the
    # 17:00 evening session; without the user window it would clip to 17:00.
    tue = date(2026, 4, 14)
    plan = planner.plan_for_datetime(
        "MCX", _dt(tue, 10, 0), user_start="09:00", user_stop="15:30", now=_dt(tue, 9, 0)
    )
    assert plan.blocked
    assert plan.reason == planner.REASON_WINDOW_MISSES_SESSION


def test_mcx_early_user_stop_caps_before_session_close(planner):
    tue = date(2026, 4, 14)
    plan = planner.resolve_plan("MCX", "17:00", "20:00", now=_dt(tue, 10, 0))
    assert plan.startable
    assert plan.next_start == _dt(tue, 17, 0)
    # Must stop is the user's stop — it is earlier than the session close.
    assert plan.must_stop == _dt(tue, 20, 0)


# ---------------------------------------------------------------------------
# Sunday Muhurat special session (8-Nov-2026)
# ---------------------------------------------------------------------------


def test_sunday_muhurat_when_sunday_selected(planner):
    sun = date(2026, 11, 8)
    assert sun.weekday() == 6
    plan = planner.resolve_plan(
        "NSE", "09:15", "15:30", weekdays=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        now=_dt(sun, 9, 0),
    )
    assert plan.startable
    assert plan.kind == "special"
    assert plan.next_start == _dt(sun, 14, 20)
    assert plan.must_stop == _dt(sun, 15, 30)
    assert "Muhurat" in plan.session_label or "Diwali" in plan.session_label


def test_sunday_muhurat_skipped_when_only_weekdays_selected(planner):
    sun = date(2026, 11, 8)
    plan = planner.resolve_plan(
        "NSE", "09:15", "15:30",
        weekdays=["mon", "tue", "wed", "thu", "fri"],
        now=_dt(sun, 9, 0),
    )
    assert plan.startable
    # Monday 9-Nov is a normal session; Tuesday 10-Nov is Balipratipada.
    assert plan.next_start == _dt(date(2026, 11, 9), 9, 15)


def test_one_shot_before_muhurat_open_clipped_forward(planner):
    sun = date(2026, 11, 8)
    plan = planner.plan_for_datetime("NSE", _dt(sun, 9, 15), now=_dt(sun, 9, 0))
    assert plan.startable
    assert plan.kind == "special"
    assert plan.next_start == _dt(sun, 14, 20)
    assert plan.must_stop == _dt(sun, 15, 30)


def test_one_shot_after_early_close_blocks(planner):
    sun = date(2026, 11, 8)
    plan = planner.plan_for_datetime("NSE", _dt(sun, 16, 0), now=_dt(sun, 9, 0))
    assert plan.blocked
    assert plan.reason == planner.REASON_WINDOW_MISSES_SESSION


def test_one_shot_on_full_closure_blocks(planner):
    # Republic Day 2026-01-26: every listed exchange including MCX is closed.
    plan = planner.plan_for_datetime(
        "NSE", _dt(date(2026, 1, 26), 10, 0), now=_dt(date(2026, 1, 26), 9, 0)
    )
    assert plan.blocked
    assert plan.reason == planner.REASON_NO_SESSION_IN_DAYS


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


def test_crypto_sunday_24x7(planner):
    sun = date(2026, 4, 12)
    assert sun.weekday() == 6
    now = _dt(sun, 3, 0)
    plan = planner.resolve_plan("CRYPTO", "00:00", "23:59", weekdays=["sun"], now=now)
    assert plan.startable
    assert plan.kind == "crypto"
    assert plan.next_start == now
    assert plan.must_stop == _dt(sun, 23, 59)


def test_crypto_no_user_times_runs_all_day(planner):
    mon = date(2026, 11, 2)
    now = _dt(mon, 5, 0)
    plan = planner.resolve_plan("CRYPTO", None, None, now=now)
    assert plan.startable
    # Inside the 24/7 window: startable immediately.
    assert plan.next_start == now
    assert plan.must_stop.strftime("%H:%M") == "23:59"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_calendar_failure_raises(planner, monkeypatch):
    import database.market_calendar_db as mc

    def boom(_day, _exchange):
        raise RuntimeError("calendar table missing")

    monkeypatch.setattr(mc, "describe_session_window", boom)
    with pytest.raises(planner.CalendarUnavailable):
        planner.resolve_plan("NSE", "09:15", "15:30", now=_dt(date(2026, 11, 2), 8))


# ---------------------------------------------------------------------------
# Flow trigger plans
# ---------------------------------------------------------------------------


def test_flow_gated_interval_first_tick_aligned_within_session(planner):
    mon = date(2026, 11, 2)
    plan = planner.plan_for_flow_trigger(
        {
            "scheduleType": "interval",
            "intervalValue": 5,
            "intervalUnit": "minutes",
            "marketHoursOnly": True,
            "marketHoursExchange": "NSE",
        },
        now=_dt(mon, 9, 14),
    )
    assert plan.startable
    assert plan.next_start == _dt(mon, 9, 15).replace(second=2)
    assert plan.must_stop == _dt(mon, 15, 30)


def test_flow_gated_daily_resolves_concrete_session_time(planner):
    fri = date(2026, 4, 3)  # Good Friday: full closure incl. MCX
    plan = planner.plan_for_flow_trigger(
        {
            "scheduleType": "daily",
            "time": "09:15",
            "marketHoursOnly": True,
            "marketHoursExchange": "NSE",
        },
        now=_dt(fri, 9, 0),
    )
    assert plan.startable
    # 4/5 weekend after the closed Friday -> Monday 6-Apr normal session.
    assert plan.next_start == _dt(date(2026, 4, 6), 9, 15)


def test_flow_ungated_daily_is_pure_cron_math(planner):
    fri = date(2026, 4, 10)
    plan = planner.plan_for_flow_trigger(
        {"scheduleType": "daily", "time": "09:15", "marketHoursOnly": False},
        now=_dt(fri, 8, 0),
    )
    assert plan.kind == "unrestricted"
    assert plan.startable
    # Gating off: it would fire on the holiday itself — calendar never consulted.
    assert plan.next_start == _dt(fri, 9, 15)


def test_flow_weekly_honors_day_list(planner):
    # Tuesday 14-Apr NSE closed; only Monday selected, so Tuesday is skipped
    # entirely and the next hit is Monday 20-Apr.
    tue = date(2026, 4, 14)
    plan = planner.plan_for_flow_trigger(
        {
            "scheduleType": "weekly",
            "time": "09:15",
            "days": [0],
            "marketHoursOnly": True,
            "marketHoursExchange": "NSE",
        },
        now=_dt(tue, 10, 0),
    )
    assert plan.startable
    assert plan.next_start == _dt(date(2026, 4, 20), 9, 15)


def test_plan_to_dict_is_json_ready(planner):
    plan = planner.resolve_plan("NSE", "09:15", "15:30", now=_dt(date(2026, 11, 2), 8))
    payload = plan.to_dict()
    assert payload["startable"] is True
    assert payload["next_start"].endswith("+05:30")
    assert payload["kind"] == "regular"
    assert payload["must_stop"].startswith("2026-11-02T15:30")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
