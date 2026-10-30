"""Automated Test Suite for Binance Rebate Dashboard.
Covers:
- Database initialization and constraints
- Streaming CSV and snapshot importing (idempotency, note preservation, subaccounts)
- Decimal financial precision
- SAPI error/omission/zero normalization rules
- Closed-interval date boundaries
- Small-sample real read-only SAPI integration
- Dashboard service endpoints and followup persistence
"""

from contextlib import contextmanager
from decimal import Decimal
import asyncio
import datetime as dt
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from fastapi import UploadFile, HTTPException

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))
sys.path.insert(0, ROOT_DIR)

import database
from database import format_decimal, get_db, init_db, to_decimal
from dashboard_service import compute_period_bounds, dashboard_service
from importer import import_csv_file
from sync_service import get_binance_client, sync_manager

_orig_get_db = database.get_db
_orig_init_db = database.init_db


class _FaultInjectingCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        fail_substr = getattr(self.connection, "fail_sql_contains", None)
        if fail_substr and fail_substr in sql:
            raise sqlite3.OperationalError(f"Simulated mid-transaction SQL failure on: {fail_substr}")
        return super().execute(sql, parameters)


class _FaultInjectingConnection(sqlite3.Connection):
    fail_on_commit = False
    fail_sql_contains = None

    def cursor(self, factory=_FaultInjectingCursor):
        return super().cursor(factory)

    def commit(self):
        if self.fail_on_commit:
            raise sqlite3.OperationalError("Simulated SQLite commit failure")
        return super().commit()


class TestDashboardSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = os.path.join(cls.tmp_dir.name, "isolated_test.db")
        _orig_init_db(cls.test_db_path)
        cls.test_get_db = lambda db_path=None: _orig_get_db(db_path or cls.test_db_path)
        cls.test_init_db = lambda db_path=None: _orig_init_db(db_path or cls.test_db_path)
        cls.patchers = [
            patch("database.DB_PATH", cls.test_db_path),
            patch("database.get_db", cls.test_get_db),
            patch("database.init_db", cls.test_init_db),
            patch("dashboard_service.get_db", cls.test_get_db),
            patch("importer.get_db", cls.test_get_db),
            patch("sync_service.get_db", cls.test_get_db),
            patch.object(sys.modules[__name__], "get_db", cls.test_get_db),
        ]
        for p in cls.patchers:
            p.start()

        # Import main only AFTER DB and credential loaders are isolated
        with patch("sync_service.get_binance_client", return_value=None):
            import main as _main
            cls.main_mod = _main

        main_patches = [
            patch.object(cls.main_mod, "get_db", cls.test_get_db),
            patch.object(cls.main_mod, "init_db", cls.test_init_db),
        ]
        for mp in main_patches:
            mp.start()
            cls.patchers.append(mp)

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.patchers):
            p.stop()
        cls.tmp_dir.cleanup()

    def test_01_database_tables(self):
        """Verify database tables and indexes exist."""
        conn = get_db()
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for expected in ["customers", "subaccount_links", "import_batches", "customer_metrics", "referral_metrics", "customer_followups", "sync_jobs"]:
            self.assertIn(expected, tables)
        conn.close()

    def test_02_importer_csv_idempotency(self):
        """Test streaming CSV import, idempotency, and subaccount linking.

        Needs a real Binance rebate CSV export; point TEST_CSV_PATH at one.
        Skipped by default so the suite runs on a fresh clone.
        """
        csv_path = os.environ.get("TEST_CSV_PATH")
        if not csv_path or not os.path.exists(csv_path):
            self.skipTest("TEST_CSV_PATH not set; skipping CSV import integration test")

        res_csv1 = import_csv_file(csv_path)
        self.assertIn(res_csv1["status"], ("success", "already_imported"))
        # Test duplicate import idempotency
        res_csv2 = import_csv_file(csv_path)
        self.assertEqual(res_csv2["status"], "already_imported")

        conn = get_db()
        cur = conn.cursor()
        cust_count = cur.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        self.assertGreater(cust_count, 0)
        conn.close()

    def test_03_note_preservation_and_manual_notes(self):
        """Ensure platform notes and manual notes are protected from clobbering."""
        conn = get_db()
        cur = conn.cursor()
        test_uid = "test-protect-uid-9999"

        # Insert customer with manual note
        cur.execute("""
            INSERT OR REPLACE INTO customers (user_id, platform_note, manual_note, is_starred, created_at, updated_at)
            VALUES (?, 'Platform Original', 'Manual Important Note', 1, '2026-01-01', '2026-01-01')
        """, (test_uid,))
        conn.commit()

        # Update customer with empty platform note: manual note must remain intact
        cur.execute("""
            UPDATE customers SET platform_note = COALESCE(NULLIF('', ''), platform_note)
            WHERE user_id = ?
        """, (test_uid,))
        conn.commit()

        row = cur.execute("SELECT platform_note, manual_note, is_starred FROM customers WHERE user_id = ?", (test_uid,)).fetchone()
        self.assertEqual(row["platform_note"], "Platform Original")
        self.assertEqual(row["manual_note"], "Manual Important Note")
        self.assertEqual(row["is_starred"], 1)

        # Clean up
        cur.execute("DELETE FROM customers WHERE user_id = ?", (test_uid,))
        conn.commit()
        conn.close()

    def test_04_decimal_precision(self):
        """Test Decimal parsing and rounding."""
        self.assertEqual(format_decimal("0.123456", 4), "0.1235")
        self.assertEqual(format_decimal("1000.5", 2), "1000.50")
        self.assertEqual(format_decimal(None), None)
        self.assertEqual(to_decimal("0.0000"), Decimal("0.0000"))
        self.assertEqual(to_decimal(""), None)

    def test_05_date_bounds_closed_intervals(self):
        """Test closed interval calculation [curr_start, curr_end] and [prev_start, prev_end]."""
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(15, "2026-10-15")
        self.assertEqual(curr_start, "2026-10-01")
        self.assertEqual(curr_end, "2026-10-15")
        self.assertEqual(prev_start, "2026-09-16")
        self.assertEqual(prev_end, "2026-09-30")

        # 15 days count check
        d_start = dt.datetime.strptime(curr_start, "%Y-%m-%d").date()
        d_end = dt.datetime.strptime(curr_end, "%Y-%m-%d").date()
        self.assertEqual((d_end - d_start).days + 1, 15)

    def test_06_small_sample_real_readonly_sapi_sync(self):
        """Small-sample real read-only SAPI live integration.

        Requires API credentials (BK_KEY/BK_SEC) plus env vars TEST_UID_1,
        TEST_UID_2, TEST_CODE pointing at real invitees of your own account.
        Skipped by default so the suite runs on a fresh clone.
        """
        client = get_binance_client()
        uid_1 = os.environ.get("TEST_UID_1")
        uid_2 = os.environ.get("TEST_UID_2")
        code = os.environ.get("TEST_CODE")
        if not client or not (uid_1 and uid_2 and code):
            self.skipTest("Set BK_KEY/BK_SEC and TEST_UID_1/TEST_UID_2/TEST_CODE to run live SAPI test")

        # 1. Sync UID 1 for period 2026-10-01 to 2026-10-14
        res1 = sync_manager.sync_customer_metric(client, uid_1, "2026-10-01", "2026-10-14", is_single_day=False)
        self.assertEqual(res1["status"], "success")
        self.assertIsNotNone(res1.get("commission"))

        # 2. Sync UID 2 for single day 2026-10-14
        res2 = sync_manager.sync_customer_metric(client, uid_2, "2026-10-14", "2026-10-14", is_single_day=True)
        self.assertEqual(res2["status"], "success")
        self.assertIsNotNone(res2.get("commission"))

        # 3. Sync UID 2 for zero-volume candidate day 2026-10-13 for UID 1
        res3 = sync_manager.sync_customer_metric(client, uid_1, "2026-10-13", "2026-10-13", is_single_day=True)
        self.assertEqual(res3["status"], "empty_zero")
        self.assertEqual(res3["commission"], "0.0000")
        self.assertEqual(res3["normalization_reason"], "omitted_in_successful_period_query")

        # 4. Sync referral code
        res_code = sync_manager.sync_referral_code_metric(client, code)
        self.assertEqual(res_code["status"], "success")
        self.assertIn("referrals", res_code["data"])

        # Verify database records
        conn = get_db()
        cur = conn.cursor()
        m_row = cur.execute("SELECT * FROM customer_metrics WHERE user_id = ? AND start_date = '2026-10-01' AND end_date = '2026-10-15'", (uid_1,)).fetchone()
        self.assertIsNotNone(m_row)
        self.assertEqual(m_row["status"], "success")

        code_row = cur.execute("SELECT * FROM referral_metrics WHERE code = ?", (code,)).fetchone()
        self.assertIsNotNone(code_row)
        conn.close()

    def test_07_dashboard_service_and_followup_persistence(self):
        """Test DashboardService API output structure and followup persistence.

        Requires a populated local database and TEST_UID pointing at one of
        your imported customers. Skipped on a fresh clone.
        """
        sample_uid = os.environ.get("TEST_UID")
        if not sample_uid:
            self.skipTest("TEST_UID not set; skipping dashboard service integration test")

        overview = dashboard_service.get_overview(days=15, reference_end="2026-10-15")
        self.assertIn("period", overview)
        self.assertIn("coverage", overview)
        self.assertIn("metrics", overview)
        self.assertIn("market_status", overview)
        self.assertFalse(overview["market_status"]["spot_futures_available"])

        customers_res = dashboard_service.get_customers(days=15, reference_end="2026-10-15")
        self.assertIn("customers", customers_res)
        self.assertGreater(customers_res["total_count"], 0)

        # Test single customer detail
        detail = dashboard_service.get_customer_detail(sample_uid, days=15, reference_end="2026-10-15")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["user_id"], sample_uid)

        # Test followup persistence
        test_note = "沟通良好，商讨费率需求"
        test_date = "2026-11-01"
        ok = dashboard_service.add_followup(sample_uid, test_note, test_date)
        self.assertTrue(ok)

        # Verify saved in customer and followups table
        updated_detail = dashboard_service.get_customer_detail(sample_uid, days=15, reference_end="2026-10-15")
        self.assertEqual(updated_detail["manual_note"], test_note)
        self.assertEqual(updated_detail["next_followup_date"], test_date)
        self.assertGreater(len(updated_detail["followups"]), 0)

        # Test star toggle
        initial_star = updated_detail["is_starred"]
        toggled = dashboard_service.toggle_star(sample_uid)
        self.assertEqual(toggled, not initial_star)
        # Restore
        dashboard_service.toggle_star(sample_uid)

        # Test insights & activations
        insights = dashboard_service.get_insights(days=15, reference_end="2026-10-15")
        self.assertIn("activations", insights)
        self.assertIn("referral_codes", insights)
        self.assertIn("disclaimer", insights["activations"])
        self.assertEqual(insights["activations"]["trading_customers"], 59)
        self.assertEqual(insights["activations"]["registered"], 0)
        
        insights_year = dashboard_service.get_insights(days=365, reference_end="2026-10-15")
        self.assertGreater(insights_year["activations"]["registered"], 0)
        self.assertEqual(insights_year["activations"]["trading_customers"], 59)

        # Test dynamics (subaccount links)
        dynamics = dashboard_service.get_dynamics()
        self.assertIn("teams", dynamics)
        self.assertGreater(len(dynamics["teams"]), 0)

    def test_08_exclude_top_recalculation(self):
        """Verify that exclude_top correctly subtracts top clients and reports exclusion volume."""
        ov_all = dashboard_service.get_overview(days=15, exclude_top=0, reference_end="2026-10-15")
        ov_ex1 = dashboard_service.get_overview(days=15, exclude_top=1, reference_end="2026-10-15")

        if ov_all["top_customers"]:
            top1_comm = Decimal(ov_all["top_customers"][0]["commission"])
            all_comm = Decimal(ov_all["metrics"]["total_commission"])
            ex1_comm = Decimal(ov_ex1["metrics"]["total_commission"])
            self.assertEqual(ov_ex1["exclusion"]["excluded_count"], 1)
            self.assertGreaterEqual(len(ov_ex1["exclusion"]["excluded_names"]), 1)
            self.assertEqual(Decimal(ov_ex1["exclusion"]["excluded_commission"]), top1_comm)
            self.assertEqual(all_comm - top1_comm, ex1_comm)

    def test_09_care_dashboard(self):
        """Verify unified care & re-engagement dashboard service data structure and classifications."""
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        conn.close()
        if n == 0:
            self.skipTest("local database is empty; import a CSV first")

        care = dashboard_service.get_care_dashboard(days=15, reference_end="2026-10-15")

        self.assertIn("period", care)
        self.assertIn("summary", care)
        self.assertIn("customers", care)
        self.assertIn("referral_codes", care)
        self.assertIn("followup_review", care)
        self.assertIn("care_signals", care)

        summary = care["summary"]
        self.assertGreater(summary["total_customers"], 0)
        self.assertEqual(
            summary["sleeping_count"] + summary["cooling_count"] + summary["active_count"] + summary["newstar_count"] + summary["missing_count"],
            summary["total_customers"]
        )

        # Verify customer entries have required fields for care page
        for c in care["customers"]:
            self.assertIn("uid", c)
            self.assertIn("name", c)
            self.assertIn("status", c)
            self.assertIn(c["status"], ("沉睡", "降温", "活跃", "新星", "数据待补齐"))
            self.assertIn("total", c)
            self.assertIn("current", c)
            self.assertIn("previous", c)
            self.assertIn("delta", c)
            self.assertIn("sparkline", c)

        # Verify referral codes
        self.assertGreaterEqual(len(care["referral_codes"]), 8)
        for r in care["referral_codes"]:
            self.assertIn("code", r)
            self.assertIn("count", r)
            self.assertIn("sample_count", r)

        # Verify followup review
        review = care["followup_review"]
        self.assertIn("contacted_count", review)
        self.assertIn("pending_count", review)
        self.assertIn("waiting_count", review)
        self.assertIn("logs", review)

    def test_10_dynamic_utc_yesterday_bounds(self):
        """Verify dynamic default reference date is UTC yesterday and explicit historical dates are preserved."""
        from dashboard_service import get_default_reference_end
        expected_yesterday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
        self.assertEqual(get_default_reference_end(), expected_yesterday)

        # Without reference_end, defaults to yesterday UTC
        curr_s, curr_e, prev_s, prev_e = compute_period_bounds(days=7)
        self.assertEqual(curr_e, expected_yesterday)
        expected_curr_start = (dt.date.fromisoformat(expected_yesterday) - dt.timedelta(days=6)).isoformat()
        self.assertEqual(curr_s, expected_curr_start)

        # Explicit historical query preserves exact bounds
        curr_s, curr_e, prev_s, prev_e = compute_period_bounds(days=15, reference_end="2026-10-15")
        self.assertEqual(curr_e, "2026-10-15")
        self.assertEqual(curr_s, "2026-10-01")

    def test_11_placeholder_credentials_detection(self):
        """Verify get_binance_client treats .env.example placeholders as unconfigured."""
        from sync_service import is_valid_credential
        self.assertFalse(is_valid_credential("your_binance_api_key_here"))
        self.assertFalse(is_valid_credential("your_binance_api_secret_here"))
        self.assertFalse(is_valid_credential(""))
        self.assertFalse(is_valid_credential("   "))
        self.assertFalse(is_valid_credential("None"))
        self.assertTrue(is_valid_credential("real_test_key_1234567890"))

        # Ensure get_binance_client returns None when only placeholders are configured in .env or env vars
        with tempfile.TemporaryDirectory() as td:
            dummy_env = os.path.join(td, ".env")
            with open(dummy_env, "w", encoding="utf-8") as f:
                f.write("BK_KEY=your_binance_api_key_here\nBK_SEC=your_binance_api_secret_here\n")
            with patch("sync_service.ROOT_DIR", td), patch.dict(os.environ, {"BK_KEY": "", "BK_SEC": ""}, clear=False):
                cli = get_binance_client()
                self.assertIsNone(cli)

            with patch("sync_service.ROOT_DIR", td), patch.dict(os.environ, {"BK_KEY": "your_binance_api_key_here", "BK_SEC": "your_binance_api_secret_here"}, clear=False):
                cli2 = get_binance_client()
                self.assertIsNone(cli2)

    def test_12_invalid_csv_header_validation(self):
        """Verify importer raises ValueError on missing UID header or 0 valid UIDs, and does not commit dirty records."""
        with tempfile.TemporaryDirectory() as td:
            # Missing required header
            bad_csv = os.path.join(td, "no_header.csv")
            with open(bad_csv, "w", encoding="utf-8-sig") as f:
                f.write("UID,推荐码,备注\n123456,CODE_A,测试\n")

            with self.assertRaises(ValueError) as ctx:
                import_csv_file(bad_csv)
            self.assertIn("好友ID（现货）", str(ctx.exception))

            # Has header but 0 valid rows
            empty_csv = os.path.join(td, "empty_uids.csv")
            with open(empty_csv, "w", encoding="utf-8-sig") as f:
                f.write("好友ID（现货）,推荐码,referee_note\n,,备注1\n , ,备注2\n")

            with self.assertRaises(ValueError) as ctx2:
                import_csv_file(empty_csv)
            self.assertIn("有效客户数为 0", str(ctx2.exception))

            # Ensure no customer or batch was inserted into the isolated DB
            conn = get_db()
            batches = conn.execute("SELECT COUNT(*) FROM import_batches WHERE file_name IN ('no_header.csv', 'empty_uids.csv')").fetchone()[0]
            self.assertEqual(batches, 0)
            conn.close()

    def _snapshot_db_state(self, db_path: str):
        """Return full content snapshot of customers, subaccount_links, and import_batches."""
        conn = _orig_get_db(db_path)
        try:
            customers = [
                dict(r)
                for r in conn.execute(
                    "SELECT user_id, referral_code, platform_note, manual_note, tags, vip_level, reg_time, is_starred "
                    "FROM customers ORDER BY user_id"
                ).fetchall()
            ]
            subaccount_links = [
                dict(r)
                for r in conn.execute(
                    "SELECT parent_user_id, sub_account_id, source_batch_id "
                    "FROM subaccount_links ORDER BY parent_user_id, sub_account_id"
                ).fetchall()
            ]
            import_batches = [
                dict(r)
                for r in conn.execute(
                    "SELECT file_name, file_sha256, source_type, total_rows, unique_uids, subaccount_count, summary "
                    "FROM import_batches ORDER BY id"
                ).fetchall()
            ]
            return {
                "customers": customers,
                "subaccount_links": subaccount_links,
                "import_batches": import_batches,
            }
        finally:
            conn.close()

    @contextmanager
    def _isolated_upload_env(self, conn_factory=None):
        """Create an isolated temporary directory, database, seeded existing.csv, and offline mocks."""
        import importer

        main = self.main_mod
        with tempfile.TemporaryDirectory() as td:
            data_dir = os.path.join(td, "data")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(td, "upload_isolated.db")
            _orig_init_db(db_path)

            def _make_conn(target_db_path=None):
                p = target_db_path or db_path
                if conn_factory is not None:
                    return conn_factory(p)
                return _orig_get_db(p)

            existing_csv = os.path.join(data_dir, "existing.csv")
            original_bytes = (
                "好友ID（现货）,好友子账户ID（现货）,推荐码,referee_note,注册时间\n"
                "FAKE_UID_ORIG,FAKE_SUB_ORIG,DEMO_ORIG,原平台备注,2026-01-01 00:00:00\n"
            ).encode("utf-8")
            with open(existing_csv, "wb") as f:
                f.write(original_bytes)

            def _forbid_network(*args, **kwargs):
                raise AssertionError("Network call (requests.get) is strictly forbidden in offline upload tests")

            with patch.object(main, "ROOT_DIR", td), \
                 patch.object(importer, "ROOT_DIR", td), \
                 patch("database.get_db", _make_conn), \
                 patch("importer.get_db", _make_conn), \
                 patch.object(main, "get_db", _make_conn), \
                 patch.object(main, "get_binance_client", return_value=None) as mock_get_cli, \
                 patch.object(main.sync_manager, "run_backfill_job") as mock_backfill, \
                 patch("requests.get", side_effect=_forbid_network) as mock_req_get:

                # Seed initial state via import_csv_file and attach a manual_note
                seed_conn = _orig_get_db(db_path)
                try:
                    with patch("importer.get_db", lambda: _orig_get_db(db_path)):
                        import_csv_file(existing_csv, batch_id="batch-seed-orig")
                    seed_conn.execute(
                        "UPDATE customers SET manual_note = ?, is_starred = 1 WHERE user_id = ?",
                        ("保留手工备注_勿覆盖", "FAKE_UID_ORIG"),
                    )
                    seed_conn.commit()
                finally:
                    seed_conn.close()

                baseline = self._snapshot_db_state(db_path)
                yield {
                    "td": td,
                    "data_dir": data_dir,
                    "db_path": db_path,
                    "existing_csv": existing_csv,
                    "original_bytes": original_bytes,
                    "baseline": baseline,
                    "mock_get_cli": mock_get_cli,
                    "mock_backfill": mock_backfill,
                    "mock_req_get": mock_req_get,
                    "upload_csv_file": main.upload_csv_file,
                }

    def _assert_no_hidden_temp_files(self, data_dir: str):
        leftovers = [f for f in os.listdir(data_dir) if f.startswith(".upload-") or f.startswith(".backup-")]
        self.assertEqual(leftovers, [], f"Unexpected hidden staging/backup files left in data/: {leftovers}")

    def test_13_upload_csv_scenario_1_validation_failures(self):
        """Scenario 1: Same-name upload missing header or 0 UIDs, or other existing CSV corrupted -> HTTP 400, old file & DB unchanged, no sync."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]
            existing_csv = env["existing_csv"]
            original_bytes = env["original_bytes"]
            db_path = env["db_path"]
            baseline = env["baseline"]

            # 1a. Missing required header
            bad_upload = UploadFile(filename="existing.csv", file=io.BytesIO(b"wrong_header\nFAKE\n"))
            with self.assertRaises(HTTPException) as ctx1:
                asyncio.run(upload_csv_file(file=bad_upload))
            self.assertEqual(ctx1.exception.status_code, 400)
            self.assertIn("好友ID（现货）", ctx1.exception.detail)
            with open(existing_csv, "rb") as f:
                self.assertEqual(f.read(), original_bytes)
            self.assertEqual(self._snapshot_db_state(db_path), baseline)
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

            # 1b. Valid header but 0 valid UIDs
            empty_upload = UploadFile(filename="existing.csv", file=io.BytesIO("好友ID（现货）,推荐码\n,,\n , \n".encode("utf-8-sig")))
            with self.assertRaises(HTTPException) as ctx2:
                asyncio.run(upload_csv_file(file=empty_upload))
            self.assertEqual(ctx2.exception.status_code, 400)
            self.assertIn("有效客户数为 0", ctx2.exception.detail)
            with open(existing_csv, "rb") as f:
                self.assertEqual(f.read(), original_bytes)
            self.assertEqual(self._snapshot_db_state(db_path), baseline)
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

            # 1c. Other existing CSV in data/ is corrupted
            broken_path = os.path.join(env["data_dir"], "corrupted_other.csv")
            with open(broken_path, "wb") as f:
                f.write(b"broken_col_only\n123\n")
            try:
                good_bytes = "好友ID（现货）,推荐码\nVALID_NEW_1,CODE_NEW\n".encode("utf-8")
                valid_upload = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))
                with self.assertRaises(HTTPException) as ctx3:
                    asyncio.run(upload_csv_file(file=valid_upload))
                self.assertEqual(ctx3.exception.status_code, 400)
                self.assertIn("corrupted_other.csv", ctx3.exception.detail)
                with open(existing_csv, "rb") as f:
                    self.assertEqual(f.read(), original_bytes)
                self.assertEqual(self._snapshot_db_state(db_path), baseline)
                self._assert_no_hidden_temp_files(env["data_dir"])
                self.assertEqual(env["mock_backfill"].call_count, 0)
            finally:
                if os.path.exists(broken_path):
                    os.remove(broken_path)

    def test_13b_upload_csv_scenario_2_staging_write_failure(self):
        """Scenario 2: Staging file write fails -> HTTP 500, old file & DB unchanged, staging cleaned up, no sync."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]

            def _failing_fdopen(*args, **kwargs):
                # Close fd so descriptor does not leak, then simulate write I/O error
                os.close(args[0])
                raise OSError("Simulated disk full while writing staging file")

            good_bytes = "好友ID（现货）,推荐码\nNEW_UID_STAGE,CODE_S\n".encode("utf-8")
            up = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))
            with patch("main.os.fdopen", side_effect=_failing_fdopen):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(upload_csv_file(file=up))
            self.assertEqual(ctx.exception.status_code, 500)
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13c_upload_csv_scenario_3_backup_creation_failure(self):
        """Scenario 3: Backup creation fails -> HTTP 500, DB rolled back, old file & DB unchanged, no sync."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]
            good_bytes = "好友ID（现货）,推荐码\nNEW_UID_BAK,CODE_B\n".encode("utf-8")
            up = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))

            with patch("main.shutil.copyfile", side_effect=OSError("Simulated backup copy failure")):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(upload_csv_file(file=up))
            self.assertEqual(ctx.exception.status_code, 500)
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13d_upload_csv_scenario_4_initial_os_replace_failure(self):
        """Scenario 4: Initial os.replace(staged_path, dest_path) fails -> HTTP 500, DB rolled back, old file & DB unchanged, no sync."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]
            good_bytes = "好友ID（现货）,推荐码\nNEW_UID_REP,CODE_R\n".encode("utf-8")
            up = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))

            with patch("main.os.replace", side_effect=OSError("Simulated os.replace failure")):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(upload_csv_file(file=up))
            self.assertEqual(ctx.exception.status_code, 500)
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13e_upload_csv_scenario_5_importer_mid_sql_failure(self):
        """Scenario 5: Importer fails mid-SQL after inserting customers -> HTTP 500, DB rolled back to baseline, old file unchanged, no sync."""
        def _fault_sql_conn(db_path):
            c = sqlite3.connect(db_path, timeout=30.0, factory=_FaultInjectingConnection)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.fail_sql_contains = "INSERT INTO import_batches"
            return c

        with self._isolated_upload_env(conn_factory=_fault_sql_conn) as env:
            upload_csv_file = env["upload_csv_file"]
            good_bytes = (
                "好友ID（现货）,好友子账户ID（现货）,推荐码,referee_note\n"
                "FAKE_UID_ORIG,NEW_SUB_9,NEW_CODE_X,试图修改备注\n"
                "MID_SQL_UID_5,MID_SUB_5,CODE_5,新客户5\n"
            ).encode("utf-8")
            up = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))

            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(upload_csv_file(file=up))
            self.assertEqual(ctx.exception.status_code, 500)
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13f_upload_csv_scenario_6_commit_failure_and_compensation(self):
        """Scenario 6: Commit fails after os.replace -> DB rolled back, existing target restored from backup, or new target deleted, no sync."""
        def _fault_commit_conn(db_path):
            c = sqlite3.connect(db_path, timeout=30.0, factory=_FaultInjectingConnection)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.fail_on_commit = True
            return c

        with self._isolated_upload_env(conn_factory=_fault_commit_conn) as env:
            upload_csv_file = env["upload_csv_file"]

            # 6a. Same-name replacement where commit fails -> original file restored via backup os.replace
            new_bytes = "好友ID（现货）,推荐码\nCOMMIT_FAIL_UID_1,CODE_CF1\n".encode("utf-8")
            up_existing = UploadFile(filename="existing.csv", file=io.BytesIO(new_bytes))
            with self.assertRaises(HTTPException) as ctx1:
                asyncio.run(upload_csv_file(file=up_existing))
            self.assertEqual(ctx1.exception.status_code, 500)
            self.assertTrue(os.path.exists(env["existing_csv"]))
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

            # 6b. Brand-new target filename where commit fails -> newly placed file is removed
            brand_new_path = os.path.join(env["data_dir"], "brand_new_fail.csv")
            up_new = UploadFile(filename="brand_new_fail.csv", file=io.BytesIO(new_bytes))
            with self.assertRaises(HTTPException) as ctx2:
                asyncio.run(upload_csv_file(file=up_new))
            self.assertEqual(ctx2.exception.status_code, 500)
            self.assertFalse(os.path.exists(brand_new_path))
            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

            # 6c. Commit fails AND backup restore os.replace also fails -> backup is preserved for manual recovery
            real_replace = os.replace
            replace_calls = []

            def _replace_fail_on_second(src, dst):
                replace_calls.append((src, dst))
                if len(replace_calls) == 1:
                    return real_replace(src, dst)
                raise OSError("Simulated restore os.replace failure")

            up_restore_fail = UploadFile(filename="existing.csv", file=io.BytesIO(new_bytes))
            with patch("main.os.replace", side_effect=_replace_fail_on_second):
                with self.assertRaises(HTTPException) as ctx3:
                    asyncio.run(upload_csv_file(file=up_restore_fail))
            self.assertEqual(ctx3.exception.status_code, 500)
            self.assertIn(".backup-", ctx3.exception.detail)
            backups = [f for f in os.listdir(env["data_dir"]) if f.startswith(".backup-")]
            self.assertEqual(len(backups), 1)
            backup_full = os.path.join(env["data_dir"], backups[0])
            with open(backup_full, "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13g_upload_csv_scenario_7_normal_add_and_replace(self):
        """Scenario 7: Normal new file upload and same-name replacement update file bytes, customers/links/batches, and preserve manual_note."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]
            db_path = env["db_path"]

            # 7a. Normal new file upload
            new_csv_bytes = (
                "好友ID（现货）,好友子账户ID（现货）,推荐码,referee_note,注册时间\n"
                "NEW_USER_701,NEW_SUB_701,PROMO_701,新客备注701,2026-02-01 12:00:00\n"
            ).encode("utf-8")
            up_new = UploadFile(filename="batch_two.csv", file=io.BytesIO(new_csv_bytes))
            res1 = asyncio.run(upload_csv_file(file=up_new))
            self.assertEqual(res1["status"], "success")
            batch_two_path = os.path.join(env["data_dir"], "batch_two.csv")
            self.assertTrue(os.path.exists(batch_two_path))
            with open(batch_two_path, "rb") as f:
                self.assertEqual(f.read(), new_csv_bytes)

            # 7b. Normal same-name replacement of existing.csv ( ensuring root-level same-named file is untouched )
            root_same_name = os.path.join(env["td"], "existing.csv")
            root_bytes = "好友ID（现货）,推荐码\nROOT_ONLY_UID,ROOT_CODE\n".encode("utf-8")
            with open(root_same_name, "wb") as f:
                f.write(root_bytes)

            replaced_bytes = (
                "好友ID（现货）,好友子账户ID（现货）,推荐码,referee_note,注册时间\n"
                "FAKE_UID_ORIG,FAKE_SUB_ORIG_2,DEMO_NEW_CODE,新平台备注不覆盖已有非空,2026-01-01 00:00:00\n"
                "REPL_USER_702,REPL_SUB_702,PROMO_702,替换新增客户,2026-03-01 08:00:00\n"
            ).encode("utf-8")
            up_replace = UploadFile(filename="existing.csv", file=io.BytesIO(replaced_bytes))
            res2 = asyncio.run(upload_csv_file(file=up_replace))
            self.assertEqual(res2["status"], "success")

            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), replaced_bytes)
            with open(root_same_name, "rb") as f:
                self.assertEqual(f.read(), root_bytes)

            snap = self._snapshot_db_state(db_path)
            cust_map = {c["user_id"]: c for c in snap["customers"]}
            self.assertIn("FAKE_UID_ORIG", cust_map)
            self.assertIn("NEW_USER_701", cust_map)
            self.assertIn("REPL_USER_702", cust_map)
            self.assertIn("ROOT_ONLY_UID", cust_map)
            # Manual note and existing non-empty platform note must be preserved!
            self.assertEqual(cust_map["FAKE_UID_ORIG"]["manual_note"], "保留手工备注_勿覆盖")
            self.assertEqual(cust_map["FAKE_UID_ORIG"]["platform_note"], "原平台备注")
            self.assertEqual(cust_map["FAKE_UID_ORIG"]["is_starred"], 1)

            links = {(l["parent_user_id"], l["sub_account_id"]) for l in snap["subaccount_links"]}
            self.assertIn(("FAKE_UID_ORIG", "FAKE_SUB_ORIG"), links)
            self.assertIn(("FAKE_UID_ORIG", "FAKE_SUB_ORIG_2"), links)
            self.assertIn(("NEW_USER_701", "NEW_SUB_701"), links)
            self.assertIn(("REPL_USER_702", "REPL_SUB_702"), links)
            self.assertEqual(len(snap["import_batches"]), 3)
            self._assert_no_hidden_temp_files(env["data_dir"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13h_upload_csv_scenario_8_invalid_reference_end(self):
        """Scenario 8: Invalid reference_end returns HTTP 400 BEFORE any staging file write or DB connection."""
        with self._isolated_upload_env() as env:
            upload_csv_file = env["upload_csv_file"]
            good_bytes = "好友ID（现货）,推荐码\nUID_REF_TEST,CODE_RT\n".encode("utf-8")

            for bad_date in ("not-a-date", "2026-13-45", "2026/10/15"):
                up = UploadFile(filename="existing.csv", file=io.BytesIO(good_bytes))
                with patch("main.tempfile.mkstemp") as spy_mkstemp, \
                     patch.object(self.main_mod, "get_db") as spy_get_db:
                    with self.assertRaises(HTTPException) as ctx:
                        asyncio.run(upload_csv_file(file=up, reference_end=bad_date))
                    self.assertEqual(ctx.exception.status_code, 400)
                    self.assertEqual(spy_mkstemp.call_count, 0)
                    self.assertEqual(spy_get_db.call_count, 0)

            with open(env["existing_csv"], "rb") as f:
                self.assertEqual(f.read(), env["original_bytes"])
            self.assertEqual(self._snapshot_db_state(env["db_path"]), env["baseline"])
            self.assertEqual(env["mock_backfill"].call_count, 0)

    def test_13i_upload_csv_scenario_9_fictitious_credentials_offline_isolation(self):
        """Scenario 9: With fictitious BK_KEY/BK_SEC in env, upload tests remain strictly offline (requests.get == 0, run_backfill_job == 0)."""
        fictitious_env = {
            "BK_KEY": "FICTITIOUS_TEST_API_KEY_DO_NOT_USE",
            "BK_SEC": "FICTITIOUS_TEST_API_SEC_DO_NOT_USE",
        }
        with patch.dict(os.environ, fictitious_env, clear=False):
            with self._isolated_upload_env() as env:
                upload_csv_file = env["upload_csv_file"]

                # Failed upload under fictitious env
                bad_up = UploadFile(filename="existing.csv", file=io.BytesIO(b"bad_header\n1\n"))
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(upload_csv_file(file=bad_up))
                self.assertEqual(ctx.exception.status_code, 400)

                # Successful upload under fictitious env (with main.get_binance_client patched to None)
                good_bytes = "好友ID（现货）,推荐码\nOFFLINE_UID_9,OFFLINE_CODE\n".encode("utf-8")
                good_up = UploadFile(filename="offline_ok.csv", file=io.BytesIO(good_bytes))
                res = asyncio.run(upload_csv_file(file=good_up))
                self.assertEqual(res["status"], "success")
                self.assertIsNone(res["auto_sync_job_id"])

                self.assertEqual(env["mock_req_get"].call_count, 0)
                self.assertEqual(env["mock_backfill"].call_count, 0)

                # Also verify that if a dummy client is injected, sync only triggers AFTER successful commit and never hits network
                env["mock_get_cli"].return_value = object()
                fail_up2 = UploadFile(filename="existing.csv", file=io.BytesIO(b"bad_header\n1\n"))
                with self.assertRaises(HTTPException):
                    asyncio.run(upload_csv_file(file=fail_up2))
                self.assertEqual(env["mock_backfill"].call_count, 0)

                good_up2 = UploadFile(
                    filename="offline_sync_trigger.csv",
                    file=io.BytesIO("好友ID（现货）,推荐码\nOFFLINE_UID_10,OFFLINE_CODE\n".encode("utf-8")),
                )
                res2 = asyncio.run(upload_csv_file(file=good_up2, reference_end="2026-10-20"))
                self.assertEqual(res2["status"], "success")
                self.assertIsNotNone(res2["auto_sync_job_id"])
                self.assertEqual(env["mock_backfill"].call_count, 1)
                self.assertEqual(env["mock_req_get"].call_count, 0)

    def test_14_period_labels_consistency(self):
        """Verify overview, customers, care, and insights return consistent period ranges and format."""
        ref_end = "2026-10-20"
        days = 15
        expected_curr_start = "2026-10-06"
        expected_curr_end = "2026-10-20"
        expected_label = f"{expected_curr_start} ~ {expected_curr_end}"

        ov = dashboard_service.get_overview(days=days, reference_end=ref_end)
        self.assertEqual(ov["period"]["label"], expected_label)
        self.assertEqual(ov["period"]["curr_range"], [expected_curr_start, expected_curr_end])

        cu = dashboard_service.get_customers(days=days, reference_end=ref_end)
        self.assertEqual(cu["period"]["label"], expected_label)
        self.assertEqual(cu["period"]["curr_range"], [expected_curr_start, expected_curr_end])

        ca = dashboard_service.get_care_dashboard(days=days, reference_end=ref_end)
        self.assertEqual(ca["period"]["label"], expected_label)
        self.assertEqual(ca["period"]["curr_range"], [expected_curr_start, expected_curr_end])

        ins = dashboard_service.get_insights(days=days, reference_end=ref_end)
        self.assertEqual(ins["period"]["label"], expected_label)
        self.assertEqual(ins["period"]["current_start"], expected_curr_start)
        self.assertEqual(ins["period"]["current_end"], expected_curr_end)


if __name__ == "__main__":
    unittest.main()

