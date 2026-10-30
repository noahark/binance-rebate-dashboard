"""Isolated checks: no production DB or network access."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import database
import dashboard_service
import sync_service


class MissingSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        database.init_db(self.path)
        self.connect = lambda: database.get_db(self.path)
        self.patches = [patch.object(m, "get_db", self.connect) for m in (dashboard_service, sync_service)]
        for p in self.patches:
            p.start()
        with self.connect() as c:
            c.execute("INSERT INTO customers(user_id,created_at,updated_at) VALUES ('one','now','now')")
            c.execute("INSERT INTO customers(user_id,created_at,updated_at) VALUES ('two','now','now')")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def metric(self, uid, start, end, status="success", reason="normal"):
        with self.connect() as c:
            c.execute("INSERT INTO customer_metrics(user_id,start_date,end_date,commission,trade_vol,status,normalization_reason,fetched_at) VALUES (?,?,?,'0','0',?,?, 'now')", (uid,start,end,status,reason))

    def test_missing_counts_ignore_filters_and_failed_or_legacy_totals(self):
        self.metric('one','2026-10-01','2026-10-15')
        self.metric('two','2026-10-01','2026-10-15','http_error')
        self.metric('one','2026-09-16','2026-09-30',reason='sliced_aggregation_5_slices')
        result = dashboard_service.dashboard_service.get_customers(search='no-match', reference_end='2026-10-15')
        self.assertEqual(result['customers'], [])
        self.assertEqual(result['coverage'], dict(total_customers=2,current_missing=1,previous_missing=2))
        result = dashboard_service.dashboard_service.get_customers(reference_end='2026-10-15')
        failed = next(c for c in result['customers'] if c['user_id']=='two')
        self.assertFalse(failed['has_synced'])
        self.assertEqual(failed['commission'], '待同步')

    def test_backfill_skips_complete_but_retries_failed(self):
        self.metric('one','2026-10-01','2026-10-15')
        self.metric('two','2026-10-01','2026-10-15','http_error')
        manager = sync_service.SyncManager(delay_seconds=0)
        with patch.object(sync_service, 'get_binance_client', return_value=object()), patch.object(manager,'sync_customer_metric',return_value={'status':'success'}) as query:
            manager.run_backfill_job('test-job',['one','two'],[('2026-10-01','2026-10-15',False)],skip_existing=True)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[1], 'two')

    def test_failed_annual_slice_never_publishes_partial_success(self):
        manager = sync_service.SyncManager(delay_seconds=0)
        good = dict(status='success',commission='10',trade_vol='100',raw_data={})
        with patch.object(manager,'query_invitee_sapi',side_effect=[good,{'status':'business_error'}]):
            result = manager.sync_customer_metric(object(),'one','2025-10-16','2026-10-15')
        self.assertEqual(result['status'], 'incomplete')
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM customer_metrics').fetchone()[0],0)

    def test_complete_annual_slices_are_valid_cache(self):
        manager = sync_service.SyncManager(delay_seconds=0)
        good = dict(status='success',commission='10',trade_vol='100',raw_data={})
        with patch.object(manager,'query_invitee_sapi',return_value=good) as query:
            result = manager.sync_customer_metric(object(),'one','2025-10-16','2026-10-15')
        self.assertEqual(query.call_count,5)
        self.assertEqual(result['commission'],'50.0000')
        with self.connect() as c:
            self.assertTrue(database.metric_is_complete(c.execute('SELECT * FROM customer_metrics').fetchone()))

if __name__ == '__main__':
    unittest.main()
