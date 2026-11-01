"""Tests for the admin record-cleanup preview and resumable batched jobs.

Runs entirely against the isolated test databases configured in conftest and
temporary directories; no MySQL, Docker or broker dependency.
"""

import json
import os
from datetime import datetime, timedelta

import pytest

from services import cleanup_service

NOW = datetime(2026, 11, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_rows():
    """Create the log tables on the isolated test DBs and clear their rows."""
    from database.apilog_db import OrderLog
    from database.apilog_db import db_session as api_session
    from database.apilog_db import init_db as init_api_db
    from database.health_db import HealthMetric, health_session, init_health_db
    from database.latency_db import OrderLatency, init_latency_db, latency_session
    from database.traffic_db import TrafficLog, init_logs_db, logs_session

    init_api_db()
    init_logs_db()
    init_latency_db()
    init_health_db()

    for session, model in (
        (api_session, OrderLog),
        (logs_session, TrafficLog),
        (latency_session, OrderLatency),
        (health_session, HealthMetric),
    ):
        session.query(model).delete()
        session.commit()
        session.remove()

    yield {
        "api": (api_session, OrderLog),
        "traffic": (logs_session, TrafficLog),
        "latency": (latency_session, OrderLatency),
        "health": (health_session, HealthMetric),
    }

    for session, model in (
        (api_session, OrderLog),
        (logs_session, TrafficLog),
        (latency_session, OrderLatency),
        (health_session, HealthMetric),
    ):
        try:
            session.query(model).delete()
            session.commit()
        finally:
            session.remove()


@pytest.fixture
def providers(tmp_path):
    """Providers with fixed clocks, empty log dirs and nothing in use."""
    log_dir = tmp_path / "log"
    (log_dir / "strategies").mkdir(parents=True)
    return cleanup_service.Providers(
        log_dir=log_dir,
        jobs_dir=tmp_path / "jobs",
        now_utc=NOW,
        now_local=NOW,
        now_ist=NOW,
        running_strategies=[],
        open_orders={"count": 0, "oldest_ist": None},
        batch_rows=10,
    )


def _plan(preview, category_id):
    return next(c for c in preview["categories"] if c["id"] == category_id)


# ---------------------------------------------------------------------------
# Preview: database categories
# ---------------------------------------------------------------------------


def test_preview_buckets_rows_by_retention(db_rows, providers):
    api_session, OrderLog = db_rows["api"]
    logs_session, TrafficLog = db_rows["traffic"]
    latency_session, OrderLatency = db_rows["latency"]
    health_session, HealthMetric = db_rows["health"]

    # UTC-basis tables
    for i in range(6):
        logs_session.add(
            TrafficLog(
                client_ip="1.1.1.1",
                method="GET",
                path=f"/api/v1/quotes/{i}",
                status_code=200,
                duration_ms=12.5,
                timestamp=NOW - timedelta(days=60),
            )
        )
    for _ in range(4):
        logs_session.add(
            TrafficLog(
                client_ip="2.2.2.2",
                method="POST",
                path="/api/v1/placeorder",
                status_code=200,
                duration_ms=40.0,
                timestamp=NOW - timedelta(days=1),
            )
        )
    logs_session.commit()
    logs_session.remove()

    # Latency: old data calls deletable; old order calls permanently kept
    for _ in range(5):
        latency_session.add(
            OrderLatency(
                order_id="x",
                total_latency_ms=5,
                order_type="QUOTES",
                timestamp=NOW - timedelta(days=45),
            )
        )
    for _ in range(2):
        latency_session.add(
            OrderLatency(
                order_id="y",
                total_latency_ms=9,
                order_type="PLACE",
                timestamp=NOW - timedelta(days=45),
            )
        )
    for _ in range(3):
        latency_session.add(
            OrderLatency(
                order_id="z",
                total_latency_ms=7,
                order_type="QUOTES",
                timestamp=NOW - timedelta(days=2),
            )
        )
    latency_session.commit()
    latency_session.remove()

    for _ in range(8):
        health_session.add(HealthMetric(timestamp=NOW - timedelta(days=40), fd_count=10))
    for _ in range(2):
        health_session.add(HealthMetric(timestamp=NOW - timedelta(days=1), fd_count=11))
    health_session.commit()
    health_session.remove()

    # IST-basis table
    for _ in range(7):
        api_session.add(
            OrderLog(
                api_type="placeorder",
                request_data='{"symbol":"INFY"}',
                response_data='{"orderid":"1"}',
                created_at=NOW - timedelta(days=50),
            )
        )
    for _ in range(3):
        api_session.add(
            OrderLog(
                api_type="quotes",
                request_data='{"symbol":"SBIN"}',
                response_data="{}",
                created_at=NOW - timedelta(days=2),
            )
        )
    api_session.commit()
    api_session.remove()

    preview = cleanup_service.build_preview({}, providers)

    traffic = _plan(preview, "traffic")
    assert traffic["total_count"] == 10
    assert traffic["delete_count"] == 6
    assert traffic["keep_count"] == 4
    assert traffic["skip_count"] == 0
    assert traffic["delete_bytes"] > 0

    latency = _plan(preview, "latency")
    assert latency["total_count"] == 10
    assert latency["delete_count"] == 5
    assert latency["keep_count"] == 3
    assert latency["skip_count"] == 2
    assert "checking orders" in latency["skips"][0]["reason"]

    health = _plan(preview, "health")
    assert (health["delete_count"], health["keep_count"]) == (8, 2)

    api_logs = _plan(preview, "api_logs")
    assert (api_logs["delete_count"], api_logs["keep_count"]) == (7, 3)

    assert preview["totals"]["delete_count"] == 6 + 5 + 8 + 7
    assert preview["disk"]["log"] is not None


def test_open_order_guard_protects_api_logs_until_orders_checked(db_rows, providers):
    api_session, OrderLog = db_rows["api"]

    for age_days in (45, 35, 5):
        api_session.add(
            OrderLog(
                api_type="placeorder",
                request_data="{}",
                response_data="{}",
                created_at=NOW - timedelta(days=age_days),
            )
        )
    api_session.commit()
    api_session.remove()

    # Retention is 30 days, but an order has been open for 40 days: the audit
    # window stretches back to the order, so the 35-day row is protected even
    # though retention alone would delete it. The 45-day row predates the
    # order and is still deletable.
    providers.open_orders = {
        "count": 1,
        "oldest_ist": NOW - timedelta(days=40),
    }
    preview = cleanup_service.build_preview({"api_logs": 30}, providers)
    plan = _plan(preview, "api_logs")
    assert plan["delete_count"] == 1  # 45 days old
    assert plan["skip_count"] == 1  # 35 days old, order still open
    assert plan["keep_count"] == 1  # 5 days old
    assert "1 order(s) are still open" in plan["skips"][0]["reason"]


# ---------------------------------------------------------------------------
# Preview: files and error log
# ---------------------------------------------------------------------------


def _touch(path, content=b"x" * 100, mtime_days=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime_days is not None:
        stamp = (NOW - timedelta(days=mtime_days)).timestamp()
        os.utime(path, (stamp, stamp))


def test_running_strategy_and_today_log_files_are_skipped(providers):
    _touch(
        providers.log_dir / "strategies" / "runningA_20261001_090000_IST.log",
        mtime_days=30,
    )
    _touch(
        providers.log_dir / "strategies" / "runningA_20261020_090000_IST.log",
        mtime_days=2,
    )
    _touch(
        providers.log_dir / "strategies" / "stoppedB_20260901_090000_IST.log",
        content=b"y" * 200,
        mtime_days=40,
    )
    _touch(
        providers.log_dir / "strategies" / "stoppedC_20261031_090000_IST.log",
        mtime_days=1,
    )
    providers.running_strategies = [("runningA", "Momentum")]

    today_name = f"openalgo_{NOW.strftime('%Y-%m-%d')}.log"
    _touch(providers.log_dir / today_name, mtime_days=0)
    _touch(
        providers.log_dir / "openalgo_2026-09-01.log",
        content=b"z" * 300,
        mtime_days=60,
    )
    _touch(providers.log_dir / "openalgo_2026-10-30.log", mtime_days=2)

    preview = cleanup_service.build_preview({}, providers)

    strategy = _plan(preview, "strategy_logs")
    assert strategy["delete_count"] == 1
    assert strategy["keep_count"] == 1
    assert strategy["skip_count"] == 2
    assert strategy["delete_bytes"] == 200
    assert "Momentum is running" in strategy["skips"][0]["reason"]
    assert any("runningA_" in s for s in strategy["skips"][0]["samples"])

    app_logs = _plan(preview, "app_logs")
    assert app_logs["delete_count"] == 1
    assert app_logs["skip_count"] == 1
    assert app_logs["delete_bytes"] == 300
    assert "still being written" in app_logs["skips"][0]["reason"]


def test_errors_jsonl_keeps_recent_and_unparseable_lines(providers):
    old_line = json.dumps(
        {
            "ts": (NOW - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"),
            "level": "ERROR",
            "message": "old",
        }
    )
    new_line = json.dumps(
        {
            "ts": (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "level": "ERROR",
            "message": "new",
        }
    )
    (providers.log_dir / "errors.jsonl").write_text(
        f"{old_line}\n{new_line}\nthis is not json\n", encoding="utf-8"
    )

    preview = cleanup_service.build_preview({}, providers)
    errors = _plan(preview, "errors")
    assert errors["total_count"] == 3
    assert errors["delete_count"] == 1
    assert errors["keep_count"] == 1
    assert errors["skip_count"] == 1
    assert errors["delete_bytes"] == len(old_line.encode()) + 1


# ---------------------------------------------------------------------------
# Job execution: batching, progress, failure and resume
# ---------------------------------------------------------------------------


def _run_until_settled(job_id, providers, max_ops=5):
    """Advance a job until complete or no further progress is possible."""
    calls = 0
    while True:
        job = cleanup_service.run_next(job_id, max_ops=max_ops, providers=providers)
        calls += 1
        if job["status"] in ("complete", "partial"):
            return job, calls
        if calls > 100:
            raise AssertionError("cleanup job did not settle")


def test_job_deletes_in_batches_and_persists_progress(db_rows, providers):
    logs_session, TrafficLog = db_rows["traffic"]
    for i in range(25):
        logs_session.add(
            TrafficLog(
                client_ip="1.1.1.1",
                method="GET",
                path=f"/p/{i}",
                status_code=200,
                duration_ms=1.0,
                timestamp=NOW - timedelta(days=60),
            )
        )
    for i in range(3):
        logs_session.add(
            TrafficLog(
                client_ip="1.1.1.1",
                method="GET",
                path=f"/keep/{i}",
                status_code=200,
                duration_ms=1.0,
                timestamp=NOW - timedelta(days=1),
            )
        )
    logs_session.commit()
    logs_session.remove()

    _touch(
        providers.log_dir / "strategies" / "stoppedB_20260901_090000_IST.log",
        content=b"old",
        mtime_days=40,
    )

    job = cleanup_service.create_job({}, providers)
    assert job["status"] == "running"
    assert job["categories"]["traffic"]["planned_delete"] == 25

    job, calls = _run_until_settled(job["job_id"], providers, max_ops=2)
    assert calls >= 2  # 25 rows at batch_rows=10 cannot finish in one 2-op call
    assert job["status"] == "complete"
    assert job["categories"]["traffic"]["deleted"] == 25
    assert job["totals_freed"]["count"] >= 26

    remaining = logs_session.query(TrafficLog).count()
    logs_session.remove()
    assert remaining == 3
    assert not (providers.log_dir / "strategies" / "stoppedB_20260901_090000_IST.log").exists()

    # The finished job is persisted and a second call is a harmless no-op.
    assert cleanup_service.get_job(job["job_id"], providers)["status"] == "complete"
    again = cleanup_service.run_next(job["job_id"], providers=providers)
    assert again["status"] == "complete"
    assert cleanup_service.latest_job(providers)["job_id"] == job["job_id"]


def test_failed_batch_keeps_finished_work_and_retry_only_continues(db_rows, providers, monkeypatch):
    logs_session, TrafficLog = db_rows["traffic"]
    for i in range(25):
        logs_session.add(
            TrafficLog(
                client_ip="1.1.1.1",
                method="GET",
                path=f"/p/{i}",
                status_code=200,
                duration_ms=1.0,
                timestamp=NOW - timedelta(days=60),
            )
        )
    logs_session.commit()
    logs_session.remove()

    real_batch = cleanup_service._run_db_batch
    calls = {"n": 0}

    def flaky(category_id, state, ctx):
        calls["n"] += 1
        if calls["n"] == 2:
            state["status"] = "failed"
            state["error"] = "injected failure"
            return True, 1
        return real_batch(category_id, state, ctx)

    monkeypatch.setattr(cleanup_service, "_run_db_batch", flaky)

    job = cleanup_service.create_job({}, providers)
    job = cleanup_service.run_next(job["job_id"], max_ops=50, providers=providers)
    assert job["status"] == "partial"
    traffic_state = job["categories"]["traffic"]
    assert traffic_state["status"] == "failed"
    assert traffic_state["error"] == "injected failure"
    assert traffic_state["deleted"] == 10  # first batch committed before the failure
    assert traffic_state["cursor"] == 10

    remaining = logs_session.query(TrafficLog).count()
    logs_session.remove()
    assert remaining == 15  # finished batch kept, failed batch deleted nothing

    # Retry: cursor unchanged, so only the remaining rows are touched.
    monkeypatch.setattr(cleanup_service, "_run_db_batch", real_batch)
    job = cleanup_service.run_next(job["job_id"], max_ops=50, providers=providers)
    assert job["status"] == "complete"
    assert job["categories"]["traffic"]["deleted"] == 25

    remaining = logs_session.query(TrafficLog).count()
    logs_session.remove()
    assert remaining == 0


def test_errors_jsonl_job_rewrite_keeps_protected_lines(providers):
    old_line = json.dumps(
        {"ts": (NOW - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"), "message": "old"}
    )
    new_line = json.dumps(
        {"ts": (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "message": "new"}
    )
    path = providers.log_dir / "errors.jsonl"
    path.write_text(f"{old_line}\n{new_line}\ngarbage\n", encoding="utf-8")

    job = cleanup_service.create_job({}, providers)
    job, _ = _run_until_settled(job["job_id"], providers)
    assert job["status"] == "complete"

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "garbage" in lines
    assert any("new" in line for line in lines)
    assert not any("old" in line for line in lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_normalize_days_validates():
    defaults = cleanup_service.normalize_days(None)
    assert set(defaults) == {spec.id for spec in cleanup_service.CATEGORY_SPECS}
    assert defaults["traffic"] == 30

    assert cleanup_service.normalize_days({"traffic": 7})["traffic"] == 7

    with pytest.raises(ValueError):
        cleanup_service.normalize_days({"not_a_category": 7})
    with pytest.raises(ValueError):
        cleanup_service.normalize_days({"traffic": "soon"})
    with pytest.raises(ValueError):
        cleanup_service.normalize_days({"traffic": 0})
    with pytest.raises(ValueError):
        cleanup_service.normalize_days("not-a-dict")


def test_run_unknown_job_raises(providers):
    with pytest.raises(KeyError):
        cleanup_service.run_next("does-not-exist", providers=providers)


def test_job_tolerates_microsecond_precision_cutoffs(db_rows, providers):
    # Production clocks carry microseconds; the persisted cutoff must parse
    # back exactly (regression: strptime without %f rejected the job file).
    logs_session, TrafficLog = db_rows["traffic"]
    logs_session.add(
        TrafficLog(
            client_ip="1.1.1.1",
            method="GET",
            path="/p",
            status_code=200,
            duration_ms=1.0,
            timestamp=NOW - timedelta(days=60),
        )
    )
    logs_session.commit()
    logs_session.remove()

    providers.now_utc = NOW.replace(microsecond=123456)
    job = cleanup_service.create_job({}, providers)
    assert "." in job["categories"]["traffic"]["cutoff"]

    job, _ = _run_until_settled(job["job_id"], providers)
    assert job["status"] == "complete"
    assert job["categories"]["traffic"]["deleted"] >= 1

    # Nothing past the cutoff remains after the job settles.
    from database.traffic_db import TrafficLog as _TrafficLog

    leftover = (
        logs_session.query(_TrafficLog)
        .filter(_TrafficLog.timestamp < providers.now_utc - timedelta(days=30))
        .count()
    )
    logs_session.remove()
    assert leftover == 0
