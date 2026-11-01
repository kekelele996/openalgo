"""Concrete session-armed jobs for /python strategies.

Verifies arm_strategy_session installs absolute DateTrigger start/stop jobs
resolved from the exchange calendar (Muhurat early close, MCX holiday evening,
no surprise start mid-window), fails closed when the calendar is unreadable,
and that scheduled_start_strategy refuses a Muhurat morning but starts once
the special session opens.

Run: uv run pytest test/test_python_strategy_session_arming.py -v
"""

import os
import sys
from datetime import date, datetime

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

IST = pytz.timezone("Asia/Kolkata")
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


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
    mc.db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    mc.Base.query = mc.db_session.query_property()
    mc.Base.metadata.create_all(engine)
    mc.clear_market_calendar_cache()
    mc.seed_holidays_2026()
    mc.seed_market_timings()
    return mc


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def add_job(self, func=None, trigger=None, id=None, replace_existing=False):
        self.jobs[id] = trigger


@pytest.fixture()
def ps(calendar, monkeypatch):
    from blueprints import python_strategy as module

    module.STRATEGY_CONFIGS.clear()
    module.RUNNING_STRATEGIES.clear()
    calendar.clear_market_calendar_cache()
    scheduler = FakeScheduler()
    monkeypatch.setattr(module, "SCHEDULER", scheduler)
    monkeypatch.setattr(module, "save_configs", lambda: None)
    yield module
    module.STRATEGY_CONFIGS.clear()
    module.RUNNING_STRATEGIES.clear()


def _dt(day, hour, minute=0):
    return IST.localize(datetime(day.year, day.month, day.day, hour, minute))


def _run_dates(scheduler):
    return {
        key: trigger.run_date.astimezone(IST) if trigger.run_date.tzinfo else IST.localize(trigger.run_date)
        for key, trigger in scheduler.jobs.items()
    }


def test_arm_regular_day_concrete_times(ps):
    mon = date(2026, 11, 2)
    ps.STRATEGY_CONFIGS["s1"] = {
        "exchange": "NSE",
        "schedule_start": "09:00",  # before the 09:15 open
        "schedule_stop": "16:00",   # after the 15:30 close
        "schedule_days": ALL_DAYS,
    }
    ps.arm_strategy_session("s1", now=_dt(mon, 8))

    runs = _run_dates(ps.SCHEDULER)
    assert runs["start_s1"] == _dt(mon, 9, 15)
    assert runs["stop_s1"] == _dt(mon, 15, 30)
    cfg = ps.STRATEGY_CONFIGS["s1"]
    assert "regular session" in cfg["session_label"]
    assert cfg["schedule_blocked_reason"] is None


def test_arm_muhurat_sunday_uses_special_session(ps):
    sun = date(2026, 11, 8)
    ps.STRATEGY_CONFIGS["s2"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "16:00",
        "schedule_days": ALL_DAYS,
    }
    ps.arm_strategy_session("s2", now=_dt(sun, 9))

    runs = _run_dates(ps.SCHEDULER)
    assert runs["start_s2"] == _dt(sun, 14, 20)
    # Mandatory stop is clipped to the special 15:30 close, not 16:00.
    assert runs["stop_s2"] == _dt(sun, 15, 30)
    assert "special session" in ps.STRATEGY_CONFIGS["s2"]["session_label"]


def test_arm_mcx_evening_session_on_holiday(ps):
    tue = date(2026, 4, 14)
    ps.STRATEGY_CONFIGS["s3"] = {
        "exchange": "MCX",
        "schedule_start": "09:00",
        "schedule_stop": "23:55",
        "schedule_days": WEEKDAYS,
    }
    ps.arm_strategy_session("s3", now=_dt(tue, 10))

    runs = _run_dates(ps.SCHEDULER)
    assert runs["start_s3"] == _dt(tue, 17, 0)
    assert runs["stop_s3"] == _dt(tue, 23, 55)
    assert "evening session" in ps.STRATEGY_CONFIGS["s3"]["session_label"]


def test_arm_inside_window_does_not_surprise_start(ps):
    mon = date(2026, 11, 2)
    ps.STRATEGY_CONFIGS["s4"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "15:30",
        "schedule_days": ALL_DAYS,
    }
    ps.arm_strategy_session("s4", now=_dt(mon, 10, 30))

    runs = _run_dates(ps.SCHEDULER)
    # No start at "now": it targets the next session (Tue 09:15)... next day.
    assert runs["start_s4"] == _dt(date(2026, 11, 3), 9, 15)
    # ...while today's running instance is still protected by today's stop.
    assert runs["stop_s4"] == _dt(mon, 15, 30)
    assert ps.STRATEGY_CONFIGS["s4"]["session_active"] is True


def test_arm_full_closure_moves_to_next_session(ps):
    # Good Friday 2026-04-03: everything including MCX is closed.
    fri = date(2026, 4, 3)
    ps.STRATEGY_CONFIGS["s5"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "15:30",
        "schedule_days": ALL_DAYS,
    }
    ps.arm_strategy_session("s5", now=_dt(fri, 9))

    runs = _run_dates(ps.SCHEDULER)
    assert runs["start_s5"] == _dt(date(2026, 4, 6), 9, 15)
    assert runs["stop_s5"] == _dt(date(2026, 4, 6), 15, 30)


def test_arm_calendar_unavailable_installs_no_jobs(ps, monkeypatch):
    import database.market_calendar_db as mc

    def boom(_day, _exchange):
        raise RuntimeError("no calendar table")

    monkeypatch.setattr(mc, "describe_session_window", boom)
    ps.STRATEGY_CONFIGS["s6"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "15:30",
        "schedule_days": ALL_DAYS,
    }

    result = ps.arm_strategy_session("s6", now=_dt(date(2026, 11, 2), 8))

    assert result["blocked"] is True
    assert result["reason"] == "calendar_unavailable"
    assert ps.SCHEDULER.jobs == {}
    cfg = ps.STRATEGY_CONFIGS["s6"]
    assert cfg["paused_reason"] == "calendar_unavailable"
    assert cfg["armed_start_iso"] is None


def test_arm_crypto_without_stop_has_no_stop_job(ps):
    sun = date(2026, 4, 12)
    ps.STRATEGY_CONFIGS["s7"] = {
        "exchange": "CRYPTO",
        "schedule_start": "00:00",
        "schedule_stop": None,
        "schedule_days": ALL_DAYS,
    }
    ps.arm_strategy_session("s7", now=_dt(sun, 3))

    assert "start_s7" in ps.SCHEDULER.jobs
    assert "stop_s7" not in ps.SCHEDULER.jobs
    assert ps.STRATEGY_CONFIGS["s7"]["must_stop_iso"] is None


# ---------------------------------------------------------------------------
# Fire-time gate
# ---------------------------------------------------------------------------


def _patch_now(ps_mod, day, hour, minute=0):
    fixed = _dt(day, hour, minute)
    started = []

    def fake_start(strategy_id):
        started.append(strategy_id)
        return True, "started"

    import unittest.mock as mock

    dt_patcher = mock.patch("blueprints.python_strategy.datetime")
    mock_dt = dt_patcher.start()
    mock_dt.now.return_value = fixed
    mock_dt.combine = datetime.combine
    mock_dt.fromtimestamp = datetime.fromtimestamp
    mock_dt.min = datetime.min
    start_patcher = mock.patch.object(ps_mod, "_start_unless_stopped_by_trader", fake_start)
    start_patcher.start()
    return dt_patcher, start_patcher, started


def test_scheduled_start_refuses_before_muhurat_opens(ps):
    sun = date(2026, 11, 8)
    ps.STRATEGY_CONFIGS["m1"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "16:00",
        "schedule_days": ALL_DAYS,
    }
    dt_p, start_p, started = _patch_now(ps, sun, 9, 15)
    try:
        ps.scheduled_start_strategy("m1")
        assert started == []
        assert ps.STRATEGY_CONFIGS["m1"]["paused_reason"] == "before_market"
    finally:
        dt_p.stop()
        start_p.stop()


def test_scheduled_start_runs_inside_muhurat_window(ps):
    sun = date(2026, 11, 8)
    ps.STRATEGY_CONFIGS["m2"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "16:00",
        "schedule_days": ALL_DAYS,
    }
    dt_p, start_p, started = _patch_now(ps, sun, 14, 25)
    try:
        ps.scheduled_start_strategy("m2")
        assert started == ["m2"]
    finally:
        dt_p.stop()
        start_p.stop()


def test_scheduled_start_calendar_failure_does_not_start(ps, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    # python_strategy imports the name directly, so patch it where it is used.
    monkeypatch.setattr(ps, "get_effective_session_window", boom)
    mon = date(2026, 11, 2)
    ps.STRATEGY_CONFIGS["m3"] = {
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "15:30",
        "schedule_days": ALL_DAYS,
    }
    dt_p, start_p, started = _patch_now(ps, mon, 10, 0)
    try:
        ps.scheduled_start_strategy("m3")
        assert started == []
    finally:
        dt_p.stop()
        start_p.stop()


def test_enforcer_stops_after_special_session_close(ps, monkeypatch):
    """Muhurat 15:31 (special session ended 15:30): running strategy is stopped
    even though the user's own stop was 16:00."""
    sun = date(2026, 11, 8)
    ps.STRATEGY_CONFIGS["m4"] = {
        "is_scheduled": True,
        "exchange": "NSE",
        "schedule_start": "09:15",
        "schedule_stop": "16:00",
        "schedule_days": ALL_DAYS,
    }
    stopped = []
    monkeypatch.setattr(ps, "stop_strategy_process", lambda sid: stopped.append(sid))
    monkeypatch.setattr(ps, "_is_strategy_running", lambda sid, cfg: True)

    dt_p, start_p, _started = _patch_now(ps, sun, 15, 31)
    # _patch_now also fakes the start guard; it is not used on this path.
    start_p.stop()
    try:
        ps.market_hours_enforcer()
    finally:
        dt_p.stop()
    assert stopped == ["m4"]
    assert ps.STRATEGY_CONFIGS["m4"]["paused_reason"] == "after_market"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
