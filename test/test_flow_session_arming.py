"""Flow start triggers armed at concrete session times.

Covers:
  - daily/weekly gated schedules become DateTriggers on the actual open
  - interval gated schedules are end-dated on the mandatory close
  - special sessions (Sunday Muhurat) clip a 09:15 entry to 14:20
  - a one-shot on a closed day is refused and leaves no job
  - gating off keeps the legacy raw cron trigger
  - a scheduled fire the gate refuses is recorded as a "skipped" execution

Run: uv run pytest test/test_flow_session_arming.py -v
"""

import os
import sys
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

import pytest
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
    mc.db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    mc.Base.query = mc.db_session.query_property()
    mc.Base.metadata.create_all(engine)
    mc.clear_market_calendar_cache()
    mc.seed_holidays_2026()
    mc.seed_market_timings()
    return mc


@pytest.fixture()
def armed_scheduler(calendar, monkeypatch):
    """A FlowScheduler backed by a paused in-memory scheduler."""
    from services import flow_scheduler_service as fss

    calendar.clear_market_calendar_cache()

    instance = fss.FlowScheduler.__new__(fss.FlowScheduler)
    instance._scheduler = BackgroundScheduler(
        timezone=IST,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )
    instance._scheduler.start(paused=True)
    monkeypatch.setattr(fss, "flow_scheduler", instance, raising=False)
    yield instance
    instance._scheduler.shutdown(wait=False)


def _dt(day, hour, minute=0, second=0):
    return IST.localize(datetime(day.year, day.month, day.day, hour, minute, second))


def _trigger(armed_scheduler, workflow_id=1):
    job = armed_scheduler.get_workflow_job(workflow_id)
    assert job is not None
    return job.trigger


def _daily(**extra):
    data = {
        "scheduleType": "daily",
        "time": "09:15",
        "marketHoursOnly": True,
        "marketHoursExchange": "NSE",
    }
    data.update(extra)
    return data


def test_daily_gated_arms_date_trigger_on_session_open(armed_scheduler):
    mon = date(2026, 11, 2)
    fss = armed_scheduler.__class__.__module__
    from services import flow_scheduler_service as fss_mod

    job_id, plan = fss_mod.arm_workflow_session_job(
        1, trigger_data=_daily(), now=_dt(mon, 8)
    )
    assert job_id == "flow_workflow_1"
    trigger = _trigger(armed_scheduler)
    assert isinstance(trigger, DateTrigger)
    assert trigger.run_date.astimezone(IST) == _dt(mon, 9, 15)
    assert plan["kind"] == "regular"
    assert plan["must_stop"].startswith("2026-11-02T15:30")


def test_muhurat_sunday_clips_start_to_special_session(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    sun = date(2026, 11, 8)
    _job_id, plan = fss_mod.arm_workflow_session_job(
        2, trigger_data=_daily(), now=_dt(sun, 9)
    )
    trigger = _trigger(armed_scheduler, 2)
    assert isinstance(trigger, DateTrigger)
    assert trigger.run_date.astimezone(IST) == _dt(sun, 14, 20)
    assert plan["kind"] == "special"
    assert plan["must_stop"].startswith("2026-11-08T15:30")
    assert "Muhurat" in plan["session_label"] or "Diwali" in plan["session_label"]


def test_weekly_honors_days_and_skips_partial_holiday(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    tue = date(2026, 4, 14)  # NSE closed (MCX evening); only Mondays selected
    data = _daily(scheduleType="weekly", days=[0])
    fss_mod.arm_workflow_session_job(3, trigger_data=data, now=_dt(tue, 10))
    trigger = _trigger(armed_scheduler, 3)
    assert isinstance(trigger, DateTrigger)
    assert trigger.run_date.astimezone(IST) == _dt(date(2026, 4, 20), 9, 15)


def test_interval_gated_is_end_dated_on_session_close(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    mon = date(2026, 11, 2)
    data = _daily(
        scheduleType="interval", intervalValue=5, intervalUnit="minutes"
    )
    fss_mod.arm_workflow_session_job(4, trigger_data=data, now=_dt(mon, 9, 14))
    trigger = _trigger(armed_scheduler, 4)
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.start_date.astimezone(IST) == _dt(mon, 9, 15, second=2)
    assert trigger.end_date.astimezone(IST) == _dt(mon, 15, 30)


def test_one_shot_on_a_closed_day_is_refused_without_a_job(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    fri = date(2026, 4, 3)  # Good Friday, fully closed
    data = _daily(
        scheduleType="once",
        executeAt=_dt(fri, 10).isoformat(),
    )
    with pytest.raises(fss_mod.WorkflowScheduleBlocked):
        fss_mod.arm_workflow_session_job(5, trigger_data=data, now=_dt(fri, 9))
    assert armed_scheduler.get_workflow_job(5) is None


def test_ungated_daily_keeps_legacy_cron_trigger(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    data = _daily(marketHoursOnly=False)
    _job_id, plan = fss_mod.arm_workflow_session_job(
        6, trigger_data=data, now=_dt(date(2026, 4, 3), 8)
    )
    trigger = _trigger(armed_scheduler, 6)
    assert isinstance(trigger, CronTrigger)
    assert plan["kind"] == "unrestricted"
    # Gating off: it would fire on the holiday itself.
    nxt = trigger.get_next_fire_time(None, _dt(date(2026, 4, 3), 8))
    assert nxt.astimezone(IST) == _dt(date(2026, 4, 3), 9, 15)


def test_gated_daily_on_mcx_holiday_uses_mcx_evening(armed_scheduler):
    from services import flow_scheduler_service as fss_mod

    tue = date(2026, 4, 14)
    data = _daily(marketHoursExchange="MCX", time="17:00")
    fss_mod.arm_workflow_session_job(7, trigger_data=data, now=_dt(tue, 10))
    trigger = _trigger(armed_scheduler, 7)
    assert isinstance(trigger, DateTrigger)
    assert trigger.run_date.astimezone(IST) == _dt(tue, 17, 0)


# ---------------------------------------------------------------------------
# Skipped execution recorded
# ---------------------------------------------------------------------------


def test_scheduled_fire_refused_by_creates_skipped_execution(monkeypatch):
    """A gate refusal lands in the execution history, not just the logs."""
    from database import flow_db
    from services import flow_scheduler_service as fss_mod

    enabled_node = {
        "type": "start",
        "data": {
            "marketHoursOnly": True,
            "marketHoursExchange": "NSE",
            "marketHoursStart": None,
            "marketHoursEnd": None,
        },
    }
    fake_wf = SimpleNamespace(
        id=42, is_active=True, nodes=[enabled_node], api_key=None
    )
    monkeypatch.setattr(flow_db, "get_workflow", lambda _id: fake_wf)
    monkeypatch.setattr(flow_db, "get_workflow_api_key", lambda _wf: "key")

    updates = []
    monkeypatch.setattr(
        flow_db, "create_execution", lambda _id, status="pending": SimpleNamespace(id=99)
    )
    monkeypatch.setattr(
        flow_db,
        "update_execution_status",
        lambda eid, status, error=None, logs=None: updates.append((eid, status, error)),
    )

    def boom(_day, _exchange):
        raise RuntimeError("calendar table missing")

    monkeypatch.setattr("database.market_calendar_db.resolve_session_window", boom)

    def fail_if_executed(*_a, **_k):
        raise AssertionError("workflow must not execute while the calendar is unknown")

    monkeypatch.setattr(
        "services.flow_executor_service.execute_workflow", fail_if_executed
    )

    fss_mod.execute_workflow_scheduled(42)

    assert updates == [(99, "skipped", mock.ANY)]
    assert "calendar" in updates[0][2].lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
