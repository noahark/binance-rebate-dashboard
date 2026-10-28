"""Database management module for Binance Rebate Dashboard.
Uses SQLite with Decimal-safe string storage for financial values.
"""

from decimal import Decimal, ROUND_HALF_UP
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, "data", "rebate_dashboard.db")


def to_decimal(val: Any) -> Optional[Decimal]:
    """Safely convert any numeric/string value to Decimal."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s.lower() in ("none", "null", "nan"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def format_decimal(val: Any, places: int = 4) -> Optional[str]:
    """Format decimal to exact string with specified places."""
    d = to_decimal(val)
    if d is None:
        return None
    # Quantize using ROUND_HALF_UP
    fmt = Decimal(10) ** -places
    return str(d.quantize(fmt, rounding=ROUND_HALF_UP))


def get_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get sqlite connection with dict row factory."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize database tables and indexes."""
    conn = get_db(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT UNIQUE NOT NULL,
        file_name TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        source_type TEXT NOT NULL,
        total_rows INTEGER DEFAULT 0,
        unique_uids INTEGER DEFAULT 0,
        subaccount_count INTEGER DEFAULT 0,
        imported_at TEXT NOT NULL,
        summary TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        user_id TEXT PRIMARY KEY,
        referral_code TEXT,
        platform_note TEXT,
        manual_note TEXT,
        tags TEXT DEFAULT '[]',
        vip_level INTEGER DEFAULT 0,
        reg_time INTEGER,
        kyc_time INTEGER,
        first_deposit_time INTEGER,
        first_trade_time INTEGER,
        spot_rebate_rate TEXT,
        futures_rebate_rate TEXT,
        all_time_trade_vol TEXT,
        all_time_commission TEXT,
        is_starred INTEGER DEFAULT 0,
        next_followup_date TEXT,
        last_synced_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subaccount_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_user_id TEXT NOT NULL,
        sub_account_id TEXT NOT NULL UNIQUE,
        source_batch_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (parent_user_id) REFERENCES customers(user_id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS customer_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        is_single_day INTEGER DEFAULT 0,
        trade_vol TEXT,
        commission TEXT,
        source TEXT DEFAULT 'sapi',
        unit_status TEXT DEFAULT 'unconfirmed',
        status TEXT DEFAULT 'success',
        normalization_reason TEXT,
        error_code INTEGER,
        error_msg TEXT,
        fetched_at TEXT NOT NULL,
        UNIQUE(user_id, start_date, end_date, source)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS referral_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        clicks INTEGER DEFAULT 0,
        referrals INTEGER DEFAULT 0,
        trade_vol TEXT,
        commission TEXT,
        note TEXT,
        create_time INTEGER,
        fetched_at TEXT NOT NULL,
        UNIQUE(code, start_date, end_date)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS customer_followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        note TEXT NOT NULL,
        followup_date TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES customers(user_id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sync_jobs (
        id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL,
        total_items INTEGER DEFAULT 0,
        processed_items INTEGER DEFAULT 0,
        success_items INTEGER DEFAULT 0,
        failed_items INTEGER DEFAULT 0,
        start_date TEXT,
        end_date TEXT,
        current_uid TEXT,
        message TEXT,
        error_log TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """)

    # Indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_customers_starred ON customers(is_starred)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_subaccount_parent ON subaccount_links(parent_user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_metrics_lookup ON customer_metrics(user_id, start_date, end_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_metrics_single ON customer_metrics(user_id, is_single_day, start_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_followups_user ON customer_followups(user_id, created_at DESC)")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)


def metric_is_complete(row) -> bool:
    """Legacy sliced totals may have silently omitted failed slices."""
    if not row or row["status"] not in ("success", "empty_zero"):
        return False
    if row["commission"] is None or row["trade_vol"] is None:
        return False
    return not (row["normalization_reason"] or "").startswith("sliced_aggregation_")
