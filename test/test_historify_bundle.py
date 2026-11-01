"""Migration bundles: build -> preview -> resumable, idempotent restore.

These tests never touch MySQL, Docker or a broker: both machines in the
"two machines" story are two isolated DuckDB files in one tmp directory.
"""

import io
import json
import os
import shutil
import uuid
import zipfile

import pandas as pd
import pytest

from database import historify_db
from services import historify_service as hs


DAY = 86400
# Fixed epoch base (2024-01-01 UTC) - values only need to be consistent here.
BASE = 1704067200


@pytest.fixture
def historify_database(monkeypatch, tmp_path):
    """
    Isolated DuckDB files for the two-machine story.

    Yields a ``use(name)`` switcher: the bundle is built against the "source"
    database and restored into a separate "target" database, each with its own
    bundle staging directory.
    """
    original_path = historify_db.HISTORIFY_DB_PATH

    def use(name: str) -> str:
        db_dir = tmp_path / name
        db_dir.mkdir(parents=True, exist_ok=True)
        path = str(db_dir / "historify.duckdb")
        monkeypatch.setattr(historify_db, "HISTORIFY_DB_PATH", path)
        historify_db.init_database()
        return path

    use("source")
    yield use
    monkeypatch.setattr(historify_db, "HISTORIFY_DB_PATH", original_path)


@pytest.fixture(autouse=True)
def clean_job_state():
    """Leave the service job registries untouched between tests."""
    running = dict(hs._running_jobs)
    paused = dict(hs._paused_jobs)
    yield
    hs._running_jobs.clear()
    hs._running_jobs.update(running)
    hs._paused_jobs.clear()
    hs._paused_jobs.update(paused)


def _candles(prefix: str, interval: str, timestamps):
    return pd.DataFrame(
        [
            {
                "timestamp": ts,
                "open": float(prefix + i),
                "high": float(prefix + i + 1),
                "low": float(prefix + i - 1),
                "close": float(prefix + i + 2),
                "volume": 100 + i,
                "oi": 0,
            }
            for i, ts in enumerate(timestamps)
        ]
    )


def _seed_source_database():
    """Populate the 'source machine' with two symbols and both intervals."""
    historify_db.upsert_market_data(
        _candles(10, "D", [BASE + i * DAY for i in range(5)]), "SBIN", "NSE", "D"
    )
    historify_db.upsert_market_data(
        _candles(20, "D", [BASE + i * DAY for i in range(3)]), "RELIANCE", "NSE", "D"
    )
    historify_db.upsert_market_data(
        _candles(30, "1m", [BASE + i * 60 for i in range(4)]), "SBIN", "NSE", "1m"
    )


def _build_bundle(output_path, **kwargs):
    defaults = {"intervals": ["1m", "D"]}
    defaults.update(kwargs)
    ok, message, summary = historify_db.build_migration_bundle(output_path, **defaults)
    assert ok, message
    return summary


def _read_manifest(bundle_path):
    with zipfile.ZipFile(bundle_path) as zf:
        return json.loads(zf.read("manifest.json"))


def _stage_bundle(bundle_path, tmp_path):
    """Extract a built bundle into this database's staging area."""
    bundle_id = uuid.uuid4().hex
    bundle_dir = historify_db.get_bundle_dir(bundle_id)
    os.makedirs(bundle_dir, exist_ok=False)
    saved = os.path.join(bundle_dir, "bundle.hbpkg")
    shutil.copy(bundle_path, saved)
    hs._safe_extract_bundle(saved, bundle_dir)
    return bundle_id


def test_build_bundle_structure_and_manifest(historify_database, tmp_path):
    _seed_source_database()
    bundle_path = str(tmp_path / "export.hbpkg")
    summary = _build_bundle(bundle_path)

    assert summary["segments"] == 3
    assert summary["total_records"] == 12
    assert summary["symbol_count"] == 2

    manifest = _read_manifest(bundle_path)
    assert manifest["format"] == "historify-bundle"
    keys = {(s["symbol"], s["exchange"], s["interval"]) for s in manifest["segments"]}
    assert keys == {("SBIN", "NSE", "D"), ("RELIANCE", "NSE", "D"), ("SBIN", "NSE", "1m")}
    for segment in manifest["segments"]:
        assert segment["record_count"] == (
            5 if segment["interval"] == "D" and segment["symbol"] == "SBIN" else
            3 if segment["interval"] == "D" else 4
        )
        assert len(segment["sha256"]) == 64
        with zipfile.ZipFile(bundle_path) as zf:
            assert segment["file"] in zf.namelist()


def test_bundle_respects_date_range(historify_database, tmp_path):
    _seed_source_database()
    bundle_path = str(tmp_path / "range.hbpkg")
    summary = _build_bundle(
        bundle_path,
        symbols=[{"symbol": "SBIN", "exchange": "NSE"}],
        intervals=["D"],
        start_timestamp=BASE + DAY,
        end_timestamp=BASE + 2 * DAY,
    )
    # Only the two middle daily candles fall inside the window.
    assert summary["segments"] == 1
    assert summary["total_records"] == 2


def test_preview_classifies_new_partial_and_existing(historify_database, tmp_path):
    _seed_source_database()
    bundle_path = str(tmp_path / "preview.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)
    manifest = historify_db.load_bundle_manifest(bundle_id)

    preview = hs._build_bundle_preview(bundle_id, manifest)
    states = {(r["symbol"], r["interval"]): r["state"] for r in preview["segments"]}
    assert set(states.values()) == {"new"}
    assert preview["totals"]["new_records"] == 12

    # Local database already holds one of the five SBIN daily candles.
    historify_db.upsert_market_data(
        _candles(99, "D", [BASE]), "SBIN", "NSE", "D"
    )
    preview = hs._build_bundle_preview(bundle_id, manifest)
    rows = {(r["symbol"], r["interval"]): r for r in preview["segments"]}
    assert rows[("SBIN", "D")]["state"] == "partial"
    assert rows[("SBIN", "D")]["new"] == 4
    assert rows[("SBIN", "D")]["existing"] == 1
    assert rows[("RELIANCE", "D")]["state"] == "new"

    # Import the segment, then every segment must read as fully existing.
    sbin_d = next(s for s in manifest["segments"] if (s["symbol"], s["interval"]) == ("SBIN", "D"))
    file_path = historify_db._segment_file_path(
        historify_db.get_bundle_dir(bundle_id), sbin_d
    )
    historify_db.import_bundle_segment(file_path, sbin_d)
    preview = hs._build_bundle_preview(bundle_id, manifest)
    rows = {(r["symbol"], r["interval"]): r for r in preview["segments"]}
    assert rows[("SBIN", "D")]["state"] == "existing"
    assert rows[("SBIN", "D")]["new"] == 0


def test_import_is_idempotent_and_keeps_local_rows(historify_database, tmp_path):
    _seed_source_database()
    bundle_path = str(tmp_path / "idem.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)
    manifest = historify_db.load_bundle_manifest(bundle_id)

    # Target already holds the first candle at BASE with different prices.
    historify_db.upsert_market_data(_candles(99, "D", [BASE]), "SBIN", "NSE", "D")

    segment = next(
        s for s in manifest["segments"]
        if (s["symbol"], s["exchange"], s["interval"]) == ("SBIN", "NSE", "D")
    )
    file_path = historify_db._segment_file_path(
        historify_db.get_bundle_dir(bundle_id), segment
    )

    first = historify_db.import_bundle_segment(file_path, segment)
    assert first == {"total": 5, "inserted": 4}
    # Re-importing the same bundle adds nothing and changes nothing.
    second = historify_db.import_bundle_segment(file_path, segment)
    assert second == {"total": 5, "inserted": 0}

    with historify_db.get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM market_data WHERE symbol='SBIN' AND interval='D'"
        ).fetchone()[0]
        kept = conn.execute(
            "SELECT close FROM market_data WHERE symbol='SBIN' AND interval='D' AND timestamp=?",
            [BASE],
        ).fetchone()[0]
        catalog = conn.execute(
            "SELECT record_count FROM data_catalog WHERE symbol='SBIN' AND interval='D'"
        ).fetchone()[0]
    assert total == 5
    assert catalog == 5
    # Local candle (close = 99 + 2) was preserved, not overwritten.
    assert kept == 101.0


def test_restore_job_runs_resumes_after_failure_and_retries(
    historify_database, tmp_path, monkeypatch
):
    _seed_source_database()
    bundle_path = str(tmp_path / "job.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)

    # Run the background processor synchronously on submit.
    monkeypatch.setattr(
        hs._job_executor, "submit", lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )

    # Tamper one segment's data file: its recorded SHA-256 must fail.
    manifest = historify_db.load_bundle_manifest(bundle_id)
    victim = manifest["segments"][0]
    data_file = historify_db._segment_file_path(
        historify_db.get_bundle_dir(bundle_id), victim
    )
    with open(data_file, "r+b") as fh:
        fh.seek(10)
        fh.write(b"\xff" * 8)

    ok, response, code = hs.start_bundle_restore(bundle_id)
    assert ok and code == 200
    job_id = response["job_id"]

    job = historify_db.get_download_job(job_id)
    assert job["status"] == "completed_with_errors"
    assert job["failed_symbols"] == 1
    assert job["completed_symbols"] == 2
    items = historify_db.get_job_items(job_id)
    assert {i["status"] for i in items} == {"success", "error"}
    failed = next(i for i in items if i["status"] == "error")
    assert failed["interval"] == victim["interval"]
    assert "Checksum mismatch" in (failed["error_message"] or "")

    # Two good segments landed; the failed one did not.
    with historify_db.get_connection() as conn:
        good_keys = conn.execute(
            """
            SELECT COUNT(DISTINCT symbol || ':' || interval) FROM market_data
            """
        ).fetchone()[0]
    assert good_keys == 2

    # Repair the segment exactly as re-copying the bundle would, then retry.
    with zipfile.ZipFile(bundle_path) as zf:
        with zf.open(victim["file"]) as src, open(data_file, "wb") as dst:
            shutil.copyfileobj(src, dst)

    ok, response, code = hs.retry_failed_items(job_id, None)
    assert ok and code == 200

    job = historify_db.get_download_job(job_id)
    assert job["status"] == "completed"
    assert job["failed_symbols"] == 0
    assert job["completed_symbols"] == 3
    with historify_db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM data_catalog").fetchone()[0] == 3

    # Re-running the whole restore (same bundle) is a no-op write.
    ok, response, code = hs.start_bundle_restore(bundle_id)
    second_job = response["job_id"]
    job = historify_db.get_download_job(second_job)
    assert job["status"] == "completed"
    restored = sum(
        i["records_downloaded"] for i in historify_db.get_job_items(second_job)
    )
    assert restored == 0
    with historify_db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0] == 12


def test_restore_resumes_from_interrupted_restoring_item(
    historify_database, tmp_path, monkeypatch
):
    """A processor killed mid-segment leaves it 'restoring'; a fresh run finishes it."""
    _seed_source_database()
    bundle_path = str(tmp_path / "interrupt.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)

    calls = {"n": 0}
    real_import = historify_db.import_bundle_segment

    def flaky_import(path, segment):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated process death mid-restore")
        return real_import(path, segment)

    monkeypatch.setattr(historify_db, "import_bundle_segment", flaky_import)
    monkeypatch.setattr(
        hs._job_executor, "submit", lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )

    ok, response, _ = hs.start_bundle_restore(bundle_id)
    job_id = response["job_id"]
    job = historify_db.get_download_job(job_id)
    assert job["status"] == "completed_with_errors"

    # Server restart marks the zombie job failed; retry reprocesses the segment.
    historify_db.update_job_status(job_id, "failed")
    hs._cleanup_job(job_id)
    monkeypatch.setattr(historify_db, "import_bundle_segment", real_import)
    ok, _, code = hs.retry_failed_items(job_id, None)
    assert ok and code == 200
    with historify_db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0] == 12


def test_duplicate_active_restore_is_rejected(historify_database, tmp_path, monkeypatch):
    _seed_source_database()
    bundle_path = str(tmp_path / "dup.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)

    started = []

    def block_submit(fn, *args, **kwargs):
        started.append(args)
        # Do not run the processor; pretend it is still in flight.
        class _Future:
            def result(self_inner):
                return None
        return _Future()

    monkeypatch.setattr(hs._job_executor, "submit", block_submit)
    ok, response, _ = hs.start_bundle_restore(bundle_id)
    assert ok
    ok, response, code = hs.start_bundle_restore(bundle_id)
    assert not ok and code == 409
    assert response["job_id"] == started[0][0]
    hs._cleanup_job(started[0][0])


def test_deleting_restore_job_removes_staging(historify_database, tmp_path, monkeypatch):
    _seed_source_database()
    bundle_path = str(tmp_path / "cleanup.hbpkg")
    _build_bundle(bundle_path)
    historify_database("target")
    bundle_id = _stage_bundle(bundle_path, tmp_path)
    monkeypatch.setattr(
        hs._job_executor, "submit", lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )

    ok, response, _ = hs.start_bundle_restore(bundle_id)
    job_id = response["job_id"]
    assert os.path.isdir(historify_db.get_bundle_dir(bundle_id))
    ok, _, code = hs.delete_job(job_id)
    assert ok and code == 200
    assert not os.path.exists(historify_db.get_bundle_dir(bundle_id))


def test_safe_extract_rejects_zip_slip(historify_database, tmp_path):
    evil = str(tmp_path / "evil.hbpkg")
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("../escape.parquet", b"x")
    dest = str(tmp_path / "dest")
    os.makedirs(dest)
    saved = os.path.join(dest, "bundle.hbpkg")
    shutil.copy(evil, saved)
    with pytest.raises(ValueError, match="Unsafe path"):
        hs._safe_extract_bundle(saved, dest)
    assert not os.path.exists(str(tmp_path / "escape.parquet"))


def test_safe_extract_rejects_unexpected_entries(historify_database, tmp_path):
    bad = str(tmp_path / "bad.hbpkg")
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("notes.txt", b"x")
    dest = str(tmp_path / "dest")
    os.makedirs(dest)
    saved = os.path.join(dest, "bundle.hbpkg")
    shutil.copy(bad, saved)
    with pytest.raises(ValueError, match="Unexpected entry"):
        hs._safe_extract_bundle(saved, dest)


def test_save_rejects_non_bundle_and_bad_format(historify_database, tmp_path):
    class FakeFile:
        def __init__(self, buf, name):
            self._buf = buf
            self.filename = name

        def seek(self, *a):
            self._buf.seek(*a)

        def tell(self):
            return self._buf.tell()

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(self._buf.getvalue())

    # Wrong extension.
    ok, response, code = hs.save_migration_bundle(FakeFile(io.BytesIO(b"x"), "data.csv"))
    assert not ok and code == 400
    assert ".hbpkg" in response["message"]

    # Valid zip, wrong manifest format.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "something-else"}))
    ok, response, code = hs.save_migration_bundle(FakeFile(buf, "x.hbpkg"))
    assert not ok and code == 400
    assert "bundle" in response["message"].lower()


def test_init_database_upgrades_legacy_job_items(monkeypatch, tmp_path):
    """An older job_items schema gains the interval column on startup."""
    import duckdb

    db_path = str(tmp_path / "legacy.duckdb")
    with duckdb.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE job_items (
                id INTEGER PRIMARY KEY, job_id VARCHAR, symbol VARCHAR,
                exchange VARCHAR, status VARCHAR, records_downloaded INTEGER,
                error_message VARCHAR, started_at TIMESTAMP, completed_at TIMESTAMP
            )
        """)
    monkeypatch.setattr(historify_db, "HISTORIFY_DB_PATH", db_path)
    historify_db.init_database()

    with historify_db.get_connection() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info('job_items')").fetchall()}
    assert "interval" in columns

    # Restore-style items persist their interval; download items stay NULL.
    ok, _msg = historify_db.create_download_job(
        job_id="JOB1",
        job_type="bundle_restore",
        symbols=[],
        interval="bundle",
        start_date="2024-01-01",
        end_date="2024-01-02",
        config={"bundle_id": "abcdef0123"},
        items=[{"symbol": "SBIN", "exchange": "NSE", "interval": "D"}],
    )
    assert ok
    items = historify_db.get_job_items("JOB1")
    assert len(items) == 1 and items[0]["interval"] == "D"
