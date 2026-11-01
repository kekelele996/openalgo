"""Cleanup preview and resumable batched deletion for accumulated records.

Several stores grow for the whole life of an install and only had partial
bounds: ``logs.db`` (traffic), ``latency.db``, ``health.db``, the API order
log in the main database, ``log/errors.jsonl`` and the run logs under
``log/strategies``. The admin page calls here to show what deleting past a
retention window would actually remove *before* removing it:

* how many records (or files) each category holds;
* how many would be deleted and the bytes that would reclaim;
* how many are kept by the retention window;
* and, explicitly, what is *skipped because it is still in use* - run logs of
  strategies that are running, and the audit window of orders that have not
  reached a terminal state yet.

Deletion runs as a persisted job, one small batch at a time. Each batch
commits on its own and advances a cursor, so a batch that fails keeps every
already-finished batch and a retry only ever touches what remains.
"""

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from sqlalchemy import delete, func

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

#: Rows per database batch. Small enough that one batch cannot hold the single
#: worker for long; the frontend asks for several batches per request.
BATCH_ROWS = 500

#: Operations (one db batch, one file, or the error-log rewrite) performed per
#: "next" call. Each is a quick commit, so twenty keep a request snappy while a
#: normal cleanup finishes in a handful of calls.
OPS_PER_CALL = 20

#: Fixed per-row bytes added to the measured text columns when estimating
#: reclaimable space. Integer/float columns take 2-9 bytes each in SQLite, so
#: these deliberately err high rather than claiming more space than is there.
_ROW_OVERHEAD = 48

#: Retention bounds. A day count outside this is a hand-rolled API call or a
#: fat-fingered form, not an intent to keep everything forever or nothing at
#: all.
MIN_DAYS = 1
MAX_DAYS = 3650

#: How many protected item names the preview carries back. The UI shows "and N
#: more"; listing thousands of file names would be a multi-MB response.
SAMPLE_LIMIT = 5

_TERMINAL_ORDER_STATUSES = ("complete", "cancelled", "rejected")
_OPEN_ORDER_STATUSES = ("open", "trigger pending")


# ---------------------------------------------------------------------------
# Category registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySpec:
    """One cleanable record category."""

    id: str
    label: str
    description: str
    unit: str  # "rows", "lines" or "files"
    default_days: int
    kind: str  # "db", "jsonl" or "files"
    basis: str = "utc"  # how its timestamps are stored: "utc" | "local" | "ist"


# Order is display order on the admin page.
CATEGORY_SPECS = (
    CategorySpec(
        id="api_logs",
        label="API order logs",
        description="Request and response payloads sent to the broker for every order and data call.",
        unit="rows",
        default_days=30,
        kind="db",
        basis="ist",
    ),
    CategorySpec(
        id="traffic",
        label="HTTP traffic logs",
        description="One row per request made to this server: path, status, duration and client address.",
        unit="rows",
        default_days=30,
        kind="db",
    ),
    CategorySpec(
        id="latency",
        label="API latency records",
        description="Timing of market-data and read calls. Order execution timing is kept for checking orders.",
        unit="rows",
        default_days=30,
        kind="db",
    ),
    CategorySpec(
        id="health",
        label="Health metrics",
        description="Periodic snapshots of file descriptors, memory, threads and connections. Alerts are never deleted.",
        unit="rows",
        default_days=30,
        kind="db",
    ),
    CategorySpec(
        id="errors",
        label="Error log entries",
        description="error.jsonl lines written by the application. Lines without a readable timestamp are kept.",
        unit="lines",
        default_days=30,
        kind="jsonl",
        basis="local",
    ),
    CategorySpec(
        id="strategy_logs",
        label="Strategy run logs",
        description="Per-run log files produced by scheduled and manual Python strategy runs.",
        unit="files",
        default_days=14,
        kind="files",
    ),
    CategorySpec(
        id="app_logs",
        label="Rotated application logs",
        description="Daily rotated openalgo log files. The file being written today is left in place.",
        unit="files",
        default_days=14,
        kind="files",
    ),
)

SPECS_BY_ID = {spec.id: spec for spec in CATEGORY_SPECS}


@dataclass
class Providers:
    """Runtime hooks, overridable in tests.

    Database bindings are deliberately not here: every store already owns its
    engine/session under database/, and cleanup must use those same sessions
    rather than opening parallel engines.
    """

    log_dir: Path
    jobs_dir: Path
    now_utc: datetime
    now_local: datetime
    now_ist: datetime
    running_strategies: list = field(default_factory=list)  # [(id, name)]
    open_orders: dict = field(default_factory=dict)  # {"count": int, "oldest_ist": datetime|None}
    batch_rows: int = BATCH_ROWS


def default_providers(jobs_dir=None, log_dir=None, now=None):
    """Build the production providers, resolving live state at call time."""
    from blueprints.python_strategy import snapshot_running_strategies

    resolved_log = Path(log_dir or os.getenv("LOG_DIR", "log"))
    resolved_jobs = Path(jobs_dir or os.getenv("CLEANUP_JOBS_DIR", "db/cleanup_jobs"))

    if now is None:
        now = datetime.now()
    now_utc = datetime.utcnow()
    now_ist = datetime.now(IST).replace(tzinfo=None)

    try:
        running = snapshot_running_strategies()
    except Exception:
        # The strategy registry is best-effort context: a failure to read it
        # must never block a cleanup, and file rules stay conservative.
        logger.exception("Could not read the running strategy list")
        running = []

    return Providers(
        log_dir=resolved_log,
        jobs_dir=resolved_jobs,
        now_utc=now_utc,
        now_local=now,
        now_ist=now_ist,
        running_strategies=running,
        open_orders=_open_orders_snapshot(),
    )


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def normalize_days(raw):
    """Turn the client's retention map into validated per-category days.

    Unknown keys are rejected (so a typo never silently skips a category),
    values are clamped into [MIN_DAYS, MAX_DAYS], missing ones take defaults.
    Returns an {category_id: days} dict in display order.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Retention periods must be a JSON object")

    days = {}
    for key, value in raw.items():
        if key not in SPECS_BY_ID:
            raise ValueError(f"Unknown record category: {key}")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Retention for {key} must be a whole number of days") from exc
        if number < MIN_DAYS or number > MAX_DAYS:
            raise ValueError(f"Retention for {key} must be between {MIN_DAYS} and {MAX_DAYS} days")
        days[key] = number

    return {spec.id: days.get(spec.id, spec.default_days) for spec in CATEGORY_SPECS}


def _cutoff(spec, days, providers):
    if spec.basis == "ist":
        return providers.now_ist - timedelta(days=days)
    if spec.basis == "local":
        return providers.now_local - timedelta(days=days)
    return providers.now_utc - timedelta(days=days)


# ---------------------------------------------------------------------------
# "In use" providers
# ---------------------------------------------------------------------------


def _open_orders_snapshot():
    """Orders that have not reached a terminal state, as an audit guard.

    Only sandbox orders are persisted (live order state lives with the
    broker), but they are the one on-disk signal that reconciliation work is
    still open. The timestamp is written as naive UTC (``func.now()``) and the
    API log it guards is written as naive IST, so it is converted here.
    """
    try:
        from database.sandbox_db import SandboxOrders
        from database.sandbox_db import db_session as sandbox_session

        try:
            oldest_utc, count = (
                sandbox_session.query(
                    func.min(SandboxOrders.order_timestamp), func.count(SandboxOrders.id)
                )
                .filter(SandboxOrders.order_status.in_(_OPEN_ORDER_STATUSES))
                .one()
            )
        finally:
            sandbox_session.remove()

        if not count or oldest_utc is None:
            return {"count": 0, "oldest_ist": None}
        if oldest_utc.tzinfo is not None:
            oldest_utc = oldest_utc.astimezone(IST).replace(tzinfo=None)
        else:
            oldest_utc = oldest_utc + IST.utcoffset(oldest_utc)
        return {"count": int(count), "oldest_ist": oldest_utc}
    except Exception:
        # No sandbox table yet (fresh install before init), or a transient DB
        # error: fail safe by reporting nothing in use rather than guarding on
        # a guess.
        logger.exception("Could not read open orders for the cleanup guard")
        return {"count": 0, "oldest_ist": None}


# ---------------------------------------------------------------------------
# Database category planning
# ---------------------------------------------------------------------------


def _db_binding(category_id):
    """(session, model, timestamp attribute) for a database category."""
    if category_id == "traffic":
        from database.traffic_db import TrafficLog, logs_session

        return (
            logs_session,
            TrafficLog,
            TrafficLog.timestamp,
            (
                func.coalesce(func.length(TrafficLog.client_ip), 0)
                + func.coalesce(func.length(TrafficLog.method), 0)
                + func.coalesce(func.length(TrafficLog.path), 0)
                + func.coalesce(func.length(TrafficLog.host), 0)
                + func.coalesce(func.length(TrafficLog.error), 0)
                + _ROW_OVERHEAD
            ),
        )
    if category_id == "api_logs":
        from database.apilog_db import OrderLog, db_session

        return (
            db_session,
            OrderLog,
            OrderLog.created_at,
            (
                func.coalesce(func.length(OrderLog.api_type), 0)
                + func.coalesce(func.length(OrderLog.request_data), 0)
                + func.coalesce(func.length(OrderLog.response_data), 0)
                + _ROW_OVERHEAD
            ),
        )
    if category_id == "latency":
        from database.latency_db import ORDER_EXECUTION_TYPES, OrderLatency, latency_session

        return (
            latency_session,
            OrderLatency,
            OrderLatency.timestamp,
            (
                func.coalesce(func.length(OrderLatency.broker), 0)
                + func.coalesce(func.length(OrderLatency.symbol), 0)
                + func.coalesce(func.length(OrderLatency.order_type), 0)
                + func.coalesce(func.length(OrderLatency.status), 0)
                + func.coalesce(func.length(OrderLatency.error), 0)
                + func.coalesce(func.length(OrderLatency.request_body), 0)
                + func.coalesce(func.length(OrderLatency.response_body), 0)
                + _ROW_OVERHEAD
            ),
            ORDER_EXECUTION_TYPES,
        )
    if category_id == "health":
        from database.health_db import HealthMetric, health_session

        return (
            health_session,
            HealthMetric,
            HealthMetric.timestamp,
            (
                func.coalesce(func.length(HealthMetric.db_connections), 0)
                + func.coalesce(func.length(HealthMetric.ws_connections), 0)
                + func.coalesce(func.length(HealthMetric.thread_details), 0)
                + func.coalesce(func.length(HealthMetric.process_details), 0)
                + 120
            ),
        )
    raise ValueError(f"Not a database category: {category_id}")


def _plan_db(spec, days, providers):
    """Counts and estimated bytes for one database-backed category."""
    binding = _db_binding(spec.id)
    session, model, ts_attr, bytes_expr = binding[:4]
    order_types = binding[4] if len(binding) > 4 else None

    retention_cutoff = _cutoff(spec, days, providers)

    # The API log is the audit trail for broker requests. When an order has
    # been open longer than the retention window, nothing from that order's
    # lifetime may be removed: the effective boundary moves back to the order,
    # protecting rows retention alone would have deleted.
    effective_cutoff = retention_cutoff
    skips = []
    if spec.id == "api_logs":
        oldest_open = providers.open_orders.get("oldest_ist")
        open_count = providers.open_orders.get("count", 0)
        if oldest_open is not None and oldest_open < effective_cutoff:
            effective_cutoff = oldest_open

    candidate_filter = ts_attr < effective_cutoff
    if order_types is not None:
        candidate_filter = candidate_filter & (~model.order_type.in_(tuple(order_types)))

    try:
        total_count, total_bytes = session.query(
            func.count(model.id), func.coalesce(func.sum(bytes_expr), 0)
        ).one()
        delete_count, delete_bytes = (
            session.query(func.count(model.id), func.coalesce(func.sum(bytes_expr), 0))
            .filter(candidate_filter)
            .one()
        )
    finally:
        session.remove()

    total_count = int(total_count or 0)
    total_bytes = int(total_bytes or 0)
    delete_count = int(delete_count or 0)
    delete_bytes = int(delete_bytes or 0)

    skip_count = 0
    if order_types is not None:
        # Order execution rows are permanent by policy - they are what an
        # operator compares the broker's book against.
        try:
            order_rows = (
                session.query(func.count(model.id))
                .filter(model.order_type.in_(tuple(order_types)))
                .scalar()
            )
        finally:
            session.remove()
        order_rows = int(order_rows or 0)
        skip_count += order_rows
        if order_rows:
            skips.append(
                {
                    "reason": "Order execution records are kept for checking orders against the broker.",
                    "count": order_rows,
                    "samples": [],
                }
            )

    if spec.id == "api_logs" and effective_cutoff != retention_cutoff:
        try:
            protected = (
                session.query(func.count(model.id))
                .filter(ts_attr >= effective_cutoff, ts_attr < retention_cutoff)
                .scalar()
            )
        finally:
            session.remove()
        protected = int(protected or 0)
        skip_count += protected
        if protected:
            skips.append(
                {
                    "reason": (
                        f"{open_count} order(s) are still open "
                        f"(oldest from {effective_cutoff.strftime('%Y-%m-%d %H:%M')}); "
                        "records since then are kept until they have been checked."
                    ),
                    "count": protected,
                    "samples": [],
                }
            )

    keep_count = total_count - delete_count - skip_count
    return {
        "id": spec.id,
        "label": spec.label,
        "description": spec.description,
        "unit": spec.unit,
        "retention_days": days,
        "cutoff": retention_cutoff.isoformat(sep=" "),
        "effective_cutoff": effective_cutoff.isoformat(sep=" "),
        "total_count": total_count,
        "delete_count": delete_count,
        "keep_count": max(keep_count, 0),
        "skip_count": skip_count,
        "delete_bytes": delete_bytes,
        "skips": skips,
    }


# ---------------------------------------------------------------------------
# File / line category planning
# ---------------------------------------------------------------------------


def _classify_jsonl(path, cutoff_local):
    """Bucket every line of an errors.jsonl-style file.

    Returns (total_nonblank, delete_count, keep_count, skip_count,
    delete_bytes, skips). Parseable entries older than cutoff are deletable;
    unparseable nonblank lines are protected rather than guessed at.
    """
    total = delete_count = keep_count = skip_count = 0
    delete_bytes = 0
    if not path.exists():
        return 0, 0, 0, 0, 0, []

    raw = path.read_text(encoding="utf-8", errors="replace")
    unparseable_samples = []
    for line in raw.splitlines():
        # Blank lines are line-ending artifacts, not records: they do not
        # belong to any bucket (the rewrite still carries them across).
        if not line.strip():
            continue
        total += 1
        size = len(line.encode("utf-8", errors="replace")) + 1
        ts_dt = _jsonl_timestamp(line)
        if ts_dt is None:
            skip_count += 1
            if len(unparseable_samples) < SAMPLE_LIMIT:
                unparseable_samples.append(line[:80])
        elif ts_dt < cutoff_local:
            delete_count += 1
            delete_bytes += size
        else:
            keep_count += 1

    skips = []
    if skip_count:
        skips.append(
            {
                "reason": "Lines without a readable timestamp are kept rather than guessed at.",
                "count": skip_count,
                "samples": unparseable_samples,
            }
        )
    return total, delete_count, keep_count, skip_count, delete_bytes, skips


def _jsonl_timestamp(line):
    """The 'ts' of a JSON log line, parsed on the same local basis it was written."""
    try:
        entry = json.loads(line)
        ts = entry.get("ts") if isinstance(entry, dict) else None
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _strategy_log_files(log_dir):
    """All per-strategy run log files, if the directory exists."""
    folder = log_dir / "strategies"
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.log"), key=lambda p: p.name)


def _plan_files(spec, days, providers):
    cutoff_dt = _cutoff(spec, days, providers)
    if spec.id == "strategy_logs":
        files = _strategy_log_files(providers.log_dir)
        running = dict(providers.running_strategies)
        protected = {sid: [] for sid in running}
        for path in files:
            sid = path.name.split("_", 1)[0]
            if sid in running:
                protected[sid].append(path)
        skips = []
        for sid, paths in protected.items():
            if not paths:
                continue
            size = sum(_stat_size(p) for p in paths)
            skips.append(
                {
                    "reason": f"{running[sid]} is running; its run logs stay until it stops.",
                    "count": len(paths),
                    "bytes": size,
                    "samples": [p.name for p in paths[:SAMPLE_LIMIT]],
                }
            )
        candidates = [p for p in files if p.name.split("_", 1)[0] not in running]
    else:
        # Top-level rotated logs only. errors.jsonl and anything below a
        # subdirectory (strategies/) belong to other categories.
        files = [
            p for p in providers.log_dir.glob("*.log*") if p.is_file() and p.name != "errors.jsonl"
        ]
        today_name = f"openalgo_{providers.now_local.strftime('%Y-%m-%d')}.log"
        active = [p for p in files if p.name == today_name]
        skips = []
        if active:
            skips.append(
                {
                    "reason": "Today's application log is still being written.",
                    "count": len(active),
                    "bytes": sum(_stat_size(p) for p in active),
                    "samples": [p.name for p in active[:SAMPLE_LIMIT]],
                }
            )
        candidates = [p for p in files if p.name != today_name]

    delete_items = []
    keep_count = 0
    for path in candidates:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        size = path.stat().st_size
        if mtime < cutoff_dt:
            delete_items.append({"path": str(path), "size": size})
        else:
            keep_count += 1

    delete_count = len(delete_items)
    delete_bytes = sum(item["size"] for item in delete_items)
    skip_count = sum(entry["count"] for entry in skips)
    total_count = delete_count + keep_count + skip_count
    return {
        "id": spec.id,
        "label": spec.label,
        "description": spec.description,
        "unit": spec.unit,
        "retention_days": days,
        "cutoff": cutoff_dt.isoformat(sep=" "),
        "effective_cutoff": cutoff_dt.isoformat(sep=" "),
        "total_count": total_count,
        "delete_count": delete_count,
        "keep_count": keep_count,
        "skip_count": skip_count,
        "delete_bytes": delete_bytes,
        "skips": skips,
        "_items": delete_items,
    }


def _plan_jsonl(spec, days, providers):
    cutoff_dt = _cutoff(spec, days, providers)
    path = providers.log_dir / "errors.jsonl"
    total, delete_count, keep_count, skip_count, delete_bytes, skips = _classify_jsonl(
        path, cutoff_dt
    )
    return {
        "id": spec.id,
        "label": spec.label,
        "description": spec.description,
        "unit": spec.unit,
        "retention_days": days,
        "cutoff": cutoff_dt.isoformat(sep=" "),
        "effective_cutoff": cutoff_dt.isoformat(sep=" "),
        "total_count": total,
        "delete_count": delete_count,
        "keep_count": keep_count,
        "skip_count": skip_count,
        "delete_bytes": delete_bytes,
        "skips": skips,
        "_path": str(path),
    }


def _stat_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def build_preview(days_raw=None, providers=None):
    """Compute the cleanup preview without changing anything."""
    days = normalize_days(days_raw)
    providers = providers or default_providers()

    categories = []
    for spec in CATEGORY_SPECS:
        category_days = days[spec.id]
        if spec.kind == "db":
            plan = _plan_db(spec, category_days, providers)
        elif spec.kind == "jsonl":
            plan = _plan_jsonl(spec, category_days, providers)
        else:
            plan = _plan_files(spec, category_days, providers)
        categories.append(plan)

    totals = {
        "delete_count": sum(c["delete_count"] for c in categories),
        "delete_bytes": sum(c["delete_bytes"] for c in categories),
        "skip_count": sum(c["skip_count"] for c in categories),
    }
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "days": days,
        "categories": [_public_plan(c) for c in categories],
        "totals": totals,
        "disk": _disk_snapshot(providers),
    }


def _public_plan(plan):
    """Strip execution-only keys before a plan leaves the service."""
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def _disk_snapshot(providers):
    """Free space on the volumes holding logs and databases (best effort)."""
    import shutil

    out = {}
    for label, target in (("log", providers.log_dir), ("db", providers.jobs_dir.parent)):
        try:
            usage = shutil.disk_usage(str(target if target.exists() else target.parent))
            out[label] = {
                "total_gb": round(usage.total / (1024**3), 2),
                "free_gb": round(usage.free / (1024**3), 2),
                "used_percent": round(100 * (usage.total - usage.free) / usage.total, 1),
            }
        except OSError:
            out[label] = None
    return out


# ---------------------------------------------------------------------------
# Persisted, resumable jobs
# ---------------------------------------------------------------------------

_JOB_LOCK = threading.RLock()


def _jobs_dir(providers=None):
    providers = providers or default_providers()
    providers.jobs_dir.mkdir(parents=True, exist_ok=True)
    return providers.jobs_dir


def _job_path(job_id, providers=None):
    return _jobs_dir(providers) / f"{job_id}.json"


def _is_valid_job_id(job_id):
    """Job ids are server-generated uuid hex; reject anything else (path safety)."""
    return bool(re.fullmatch(r"[0-9a-f]{8,32}", str(job_id or "")))


def _save_job(job, providers=None):
    path = _job_path(job["job_id"], providers)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_job(job_id, providers=None):
    if not _is_valid_job_id(job_id):
        return None
    path = _job_path(job_id, providers)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def create_job(days_raw=None, providers=None):
    """Plan a cleanup and persist it as a not-yet-started job."""
    days = normalize_days(days_raw)
    providers = providers or default_providers()

    with _JOB_LOCK:
        plans = {}
        for spec in CATEGORY_SPECS:
            if spec.kind == "db":
                plan = _plan_db(spec, days[spec.id], providers)
            elif spec.kind == "jsonl":
                plan = _plan_jsonl(spec, days[spec.id], providers)
            else:
                plan = _plan_files(spec, days[spec.id], providers)
            plans[spec.id] = plan

        categories = {}
        for spec in CATEGORY_SPECS:
            plan = plans[spec.id]
            if spec.kind == "db":
                state = {
                    "kind": "db",
                    "status": "pending",
                    "cutoff": plan["effective_cutoff"],
                    "cursor": 0,
                    "planned_delete": plan["delete_count"],
                    "delete_bytes": plan["delete_bytes"],
                    "deleted": 0,
                    "bytes_freed": 0,
                    "error": None,
                }
            elif spec.kind == "jsonl":
                state = {
                    "kind": "jsonl",
                    "status": "pending",
                    "path": plan["_path"],
                    "cutoff": plan["effective_cutoff"],
                    "planned_delete": plan["delete_count"],
                    "delete_bytes": plan["delete_bytes"],
                    "deleted": 0,
                    "bytes_freed": 0,
                    "error": None,
                }
            else:
                items = [
                    {
                        "path": item["path"],
                        "size": item["size"],
                        "status": "pending",
                        "error": None,
                    }
                    for item in plan["_items"]
                ]
                state = {
                    "kind": "files",
                    "status": "pending",
                    "items": items,
                    "planned_delete": plan["delete_count"],
                    "bytes_freed": 0,
                    "error": None,
                }
            categories[spec.id] = state

        job = {
            "job_id": uuid.uuid4().hex[:12],
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
            "status": "running",
            "days": days,
            "plans": {spec.id: _public_plan(plans[spec.id]) for spec in CATEGORY_SPECS},
            "categories": categories,
            "totals_freed": {"count": 0, "bytes": 0},
        }
        _save_job(job, providers)
        return job


def get_job(job_id, providers=None):
    """A job snapshot, or None when the id is unknown."""
    with _JOB_LOCK:
        return _load_job(job_id, providers)


def latest_job(providers=None):
    """The newest persisted job so a reload can resume a partial cleanup."""
    with _JOB_LOCK:
        folder = _jobs_dir(providers)
        candidates = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        return json.loads(candidates[0].read_text(encoding="utf-8"))


def run_next(job_id, max_ops=OPS_PER_CALL, providers=None):
    """Run up to ``max_ops`` remaining batches of a job.

    A failed batch is recorded on its category with the cursor unchanged;
    already-finished batches stay committed. The call then moves on to the
    other categories, and calling again retries only what remains.
    """
    providers = providers or default_providers()
    max_ops = max(1, min(int(max_ops or OPS_PER_CALL), 200))

    with _JOB_LOCK:
        job = _load_job(job_id, providers)
        if job is None:
            raise KeyError(f"Unknown cleanup job: {job_id}")

        ops = 0
        for spec in CATEGORY_SPECS:
            if ops >= max_ops:
                break
            state = job["categories"][spec.id]
            if state["status"] == "done":
                continue
            if state["status"] == "skipped":
                continue
            if state["planned_delete"] == 0:
                state["status"] = "done"
                continue
            # "failed" is retryable: the cursor/items were never advanced.
            if state["status"] not in ("pending", "running", "failed"):
                continue
            state["status"] = "running"
            state["error"] = None
            if state["kind"] == "files":
                # A file that failed last time is still part of the remainder.
                for item in state["items"]:
                    if item["status"] == "failed":
                        item["status"] = "pending"
                        item["error"] = None

            while ops < max_ops:
                if state["kind"] == "db":
                    done, progress = _run_db_batch(spec.id, state, providers)
                elif state["kind"] == "jsonl":
                    done, progress = _run_jsonl_batch(state)
                else:
                    done, progress = _run_file_batch(state)
                ops += progress
                # The executor marks the category itself when a batch failed;
                # do not overwrite that with "done". Finished batches are
                # already committed and the cursor was not advanced.
                if state["status"] == "failed":
                    break
                if done:
                    state["status"] = "done"
                    break
                if not progress:
                    # No failure surfaced but nothing moved: stop this call.
                    break
            _save_job(job, providers)

        _finalise(job)
        _save_job(job, providers)
        return job


def _finalise(job):
    statuses = [state["status"] for state in job["categories"].values()]
    if all(status == "done" for status in statuses):
        job["status"] = "complete"
    elif any(status == "failed" for status in statuses):
        job["status"] = "partial"
    else:
        job["status"] = "running"
    total_count = 0
    total_bytes = 0
    for state in job["categories"].values():
        if state["kind"] == "files":
            total_count += sum(1 for item in state["items"] if item["status"] == "done")
        else:
            total_count += state.get("deleted", 0)
        total_bytes += state.get("bytes_freed", 0)
    job["totals_freed"] = {"count": total_count, "bytes": total_bytes}


# ---------------------------------------------------------------------------
# Batch executors
# ---------------------------------------------------------------------------


def _run_db_batch(category_id, state, providers):
    """Delete one id-window of rows. Returns (category_finished, ops_used)."""
    binding = _db_binding(category_id)
    session, model, ts_attr, bytes_expr = binding[:4]
    order_types = binding[4] if len(binding) > 4 else None

    # fromisoformat accepts both second- and microsecond-precision cutoffs.
    cutoff_dt = datetime.fromisoformat(state["cutoff"])
    try:
        query = session.query(model.id, bytes_expr).filter(
            ts_attr < cutoff_dt, model.id > state["cursor"]
        )
        if order_types is not None:
            query = query.filter(~model.order_type.in_(tuple(order_types)))
        rows = query.order_by(model.id).limit(providers.batch_rows).all()
        if not rows:
            return True, 0
        ids = [row[0] for row in rows]
        state["bytes_freed"] += int(sum(row[1] or 0 for row in rows))
        session.execute(delete(model).where(model.id.in_(ids)))
        session.commit()
        state["cursor"] = ids[-1]
        state["deleted"] += len(ids)
        return False, 1
    except Exception as exc:
        session.rollback()
        state["status"] = "failed"
        state["error"] = str(exc)[:300]
        logger.exception(f"Cleanup batch failed for {category_id}")
        return True, 1
    finally:
        session.remove()


def _run_file_batch(state):
    """Unlink one remaining file. Returns (finished, ops_used)."""
    for item in state["items"]:
        if item["status"] != "pending":
            continue
        try:
            try:
                Path(item["path"]).unlink()
            except FileNotFoundError:
                # Another process or the rotation removed it first - the end
                # state (file gone) is what the job wanted.
                pass
            item["status"] = "done"
            state["bytes_freed"] += int(item["size"])
            return False, 1
        except OSError as exc:
            item["status"] = "failed"
            item["error"] = str(exc)[:300]
            state["status"] = "failed"
            state["error"] = f"{Path(item['path']).name}: {exc}"[:300]
            logger.exception("Cleanup could not remove a log file")
            return False, 1
    return True, 0


def _run_jsonl_batch(state):
    """Rewrite errors.jsonl once, dropping timestamped entries past cutoff."""
    path = Path(state["path"])
    cutoff_dt = datetime.fromisoformat(state["cutoff"])
    try:
        kept_lines, removed, removed_bytes = _rewrite_jsonl(path, cutoff_dt)
        state["deleted"] = removed
        state["bytes_freed"] = removed_bytes
        return True, 1
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:300]
        logger.exception("Cleanup could not trim errors.jsonl")
        return True, 1


def _rewrite_jsonl(path, cutoff_dt):
    """Copy surviving lines to a temp file and atomically replace the original.

    The live logging FileHandler keeps writing by open file descriptor, so
    after the replace it is pointed back at the new file. A line appended in
    the few-millisecond window lands in the replaced (old) inode and is lost;
    the trim is a deliberate operator action and the same window exists for
    log rotation.
    """
    import logging

    if not path.exists():
        return [], 0, 0

    _flush_logging_handlers(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    kept = []
    removed = 0
    removed_bytes = 0
    for line in raw.splitlines():
        if not line.strip():
            kept.append(line)
            continue
        size = len(line.encode("utf-8", errors="replace")) + 1
        ts_dt = _jsonl_timestamp(line)
        if ts_dt is not None and ts_dt < cutoff_dt:
            removed += 1
            removed_bytes += size
        else:
            kept.append(line)

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        if kept:
            handle.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    _reopen_logging_handlers(path)
    return kept, removed, removed_bytes


def _matching_handlers(path):
    import logging

    target = path.resolve()
    handlers = []
    for handler in logging.getLogger().handlers:
        base = getattr(handler, "baseFilename", None)
        if base and Path(base).resolve() == target:
            handlers.append(handler)
    return handlers


def _flush_logging_handlers(path):
    for handler in _matching_handlers(path):
        try:
            handler.flush()
        except Exception:
            pass


def _reopen_logging_handlers(path):
    for handler in _matching_handlers(path):
        try:
            handler.acquire()
            try:
                if handler.stream is not None:
                    handler.stream.close()
                handler.stream = handler._open()
            finally:
                handler.release()
        except Exception:
            logger.exception("Could not reopen the error log handler after trim")
