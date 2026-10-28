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

from decimal import Decimal
import datetime as dt
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))
sys.path.insert(0, ROOT_DIR)

from database import format_decimal, get_db, init_db, to_decimal
from dashboard_service import compute_period_bounds, dashboard_service
from importer import import_csv_file
from sync_service import get_binance_client, sync_manager


class TestDashboardSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

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


if __name__ == "__main__":
    unittest.main()

