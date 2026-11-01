#!/usr/bin/env python
"""
Historify Migration Bundle Support for OpenAlgo

Adds the ``interval`` column to ``job_items``. Migration bundle restores
(job_type 'bundle_restore') have one work item per (symbol, exchange, interval)
segment, and the table previously had nowhere to record which interval an item
restored; broker download jobs leave the column NULL.

``init_database`` in database/historify_db.py performs the same idempotent
ALTER on every startup, so this script is not required for the application to
work. It makes the change part of the upgrade, where it can be checked with
--status, instead of leaving it to the first start.

Usage:
    cd upgrade
    uv run migrate_historify_bundle.py           # Apply migration
    uv run migrate_historify_bundle.py --status  # Check status

Migration: 014
Created: 2026-11-01
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from utils.logging import get_logger

logger = get_logger(__name__)

# Migration metadata
MIGRATION_NAME = "historify_bundle_restore"
MIGRATION_VERSION = "014"

# Load environment
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

# Database path
HISTORIFY_DB_PATH = os.getenv("HISTORIFY_DATABASE_PATH", "db/historify.duckdb")


def get_db_path():
    """Get absolute path to the DuckDB database file."""
    if os.path.isabs(HISTORIFY_DB_PATH):
        return HISTORIFY_DB_PATH
    return os.path.join(parent_dir, HISTORIFY_DB_PATH)


def _has_interval_column(conn) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info('job_items')").fetchall()}
    return "interval" in columns


def upgrade():
    """Add job_items.interval when missing."""
    try:
        import duckdb

        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")

        db_path = get_db_path()
        if not os.path.exists(db_path):
            logger.info(
                f"Historify database not found at: {db_path} - nothing to migrate "
                "(a fresh install creates job_items with the interval column)"
            )
            return True

        conn = duckdb.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()
            }
            if "job_items" not in tables:
                logger.info("job_items table does not exist yet - nothing to migrate")
                return True

            if _has_interval_column(conn):
                logger.info("job_items.interval already present - skipping")
                return True

            conn.execute("ALTER TABLE job_items ADD COLUMN interval VARCHAR")
            logger.info("Added interval column to job_items")
            return True
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def status():
    """Report whether job_items has the interval column."""
    try:
        import duckdb

        logger.info(f"Checking status of migration: {MIGRATION_NAME}")

        db_path = get_db_path()
        if not os.path.exists(db_path):
            logger.info(f"Database file not found: {db_path}")
            logger.info("   Nothing to migrate; init_database creates the current schema")
            return True

        conn = duckdb.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()
            }
            if "job_items" not in tables:
                logger.info("job_items table does not exist yet - migration will run when it is created")
                return True

            if _has_interval_column(conn):
                logger.info("job_items.interval: present")
                return True
            logger.info("job_items.interval: MISSING - migration needed")
            return False
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()

    if args.status:
        success = status()
    else:
        success = upgrade()

    sys.exit(0 if success else 1)
