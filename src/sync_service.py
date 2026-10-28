"""SAPI Sync Service with rate limiting, error classification, and database caching.
Handles serial queries, backoff on 429, safe zero-normalization, and job progress.
"""

from decimal import Decimal
import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))
sys.path.insert(0, ROOT_DIR)

from binance_client import BinanceClient
from database import format_decimal, get_db, metric_is_complete

logger = logging.getLogger("sync_service")


def get_binance_client() -> Optional[BinanceClient]:
    """Instantiate BinanceClient from environment or .env file."""
    bk_key = os.environ.get("BK_KEY", "")
    bk_sec = os.environ.get("BK_SEC", "")
    if not bk_key or not bk_sec:
        # Try loading .env
        env_path = os.path.join(ROOT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k == "BK_KEY" and not bk_key:
                            bk_key = v
                        elif k == "BK_SEC" and not bk_sec:
                            bk_sec = v
    if not bk_key or not bk_sec:
        return None
    return BinanceClient(bk_key, bk_sec)


def split_date_range_into_slices(start_date_str: str, end_date_str: str, max_slice_days: int = 90) -> List[Tuple[str, str]]:
    """Splits a date interval [start_date, end_date] into smaller chunks <= max_slice_days.
    Used for safe multi-month and 1-year historical aggregations without exceeding SAPI span limits.
    """
    s_d = dt.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    e_d = dt.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    if s_d > e_d:
        return []
    if (e_d - s_d).days + 1 <= max_slice_days:
        return [(start_date_str, end_date_str)]

    slices = []
    curr_s = s_d
    while curr_s <= e_d:
        curr_e = min(e_d, curr_s + dt.timedelta(days=max_slice_days - 1))
        slices.append((curr_s.strftime("%Y-%m-%d"), curr_e.strftime("%Y-%m-%d")))
        curr_s = curr_e + dt.timedelta(days=1)
    return slices


class SyncManager:
    """Manages rate-limited SAPI queries and backfills."""

    def __init__(self, delay_seconds: float = 0.25):
        self.delay_seconds = delay_seconds
        self._cancel_flags: Dict[str, bool] = {}
        self.active_jobs: set = set()
        self._lock = threading.Lock()

    def request_cancel(self, job_id: str) -> None:
        """Signal a running job to cancel."""
        with self._lock:
            self._cancel_flags[job_id] = True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return self._cancel_flags.get(job_id, False)

    def is_job_active(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self.active_jobs

    def query_invitee_sapi(
        self,
        client: BinanceClient,
        user_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Perform signed query to SAPI invitee performance endpoint with backoff and error classification."""
        for attempt in range(max_retries):
            try:
                resp = client.invitee_performance(user_id, start_date, end_date)
                used_weight = resp.headers.get("X-SAPI-USED-IP-WEIGHT-1M", "unknown")

                # Handle rate limiting
                if resp.status_code in (429, 418):
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    backoff = max(retry_after, 5 * (attempt + 1))
                    logger.warning(f"Rate limited (status {resp.status_code}) on UID {user_id}. Backing off {backoff}s...")
                    time.sleep(backoff)
                    continue

                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError:
                        return {"status": "parse_error", "raw_body": resp.text[:200], "used_weight": used_weight}

                    data = body.get("data")
                    if isinstance(data, dict):
                        # Case A: explicit commission and tradeVol present
                        if "commission" in data and data["commission"] is not None:
                            return {
                                "status": "success",
                                "commission": format_decimal(data["commission"], 4),
                                "trade_vol": format_decimal(data.get("tradeVol", "0"), 4),
                                "normalization_reason": "explicit_value",
                                "raw_data": data,
                                "used_weight": used_weight,
                            }
                        # Case B: successful HTTP 200 with date params, but keys omitted (period had zero volume)
                        elif start_date or end_date:
                            return {
                                "status": "empty_zero",
                                "commission": "0.0000",
                                "trade_vol": "0.0000",
                                "normalization_reason": "omitted_in_successful_period_query",
                                "raw_data": data,
                                "used_weight": used_weight,
                            }
                        # Case C: all-time query returned explicit 0
                        else:
                            return {
                                "status": "empty_zero",
                                "commission": "0.0000",
                                "trade_vol": "0.0000",
                                "normalization_reason": "never_traded_all_time",
                                "raw_data": data,
                                "used_weight": used_weight,
                            }
                    else:
                        return {
                            "status": "missing_data_field",
                            "raw_body": body,
                            "normalization_reason": "missing_data_dict",
                            "used_weight": used_weight,
                        }

                elif resp.status_code == 400:
                    try:
                        err_body = resp.json()
                        err_code = err_body.get("code")
                        err_msg = err_body.get("msg") or err_body.get("message")
                    except Exception:
                        err_code = -1
                        err_msg = resp.text[:200]
                    return {
                        "status": "business_error",
                        "error_code": err_code,
                        "error_msg": err_msg,
                        "normalization_reason": "business_error_400",
                        "used_weight": used_weight,
                    }
                else:
                    return {
                        "status": "http_error",
                        "status_code": resp.status_code,
                        "error_msg": resp.text[:200],
                        "used_weight": used_weight,
                    }

            except Exception as e:
                if attempt == max_retries - 1:
                    return {
                        "status": "network_exception",
                        "error_msg": str(e),
                        "normalization_reason": "network_exception",
                    }
                time.sleep(1.0 * (attempt + 1))

        return {"status": "exhausted_retries", "normalization_reason": "retries_failed"}

    def sync_customer_metric(
        self,
        client: BinanceClient,
        user_id: str,
        start_date: str,
        end_date: str,
        is_single_day: bool = False,
    ) -> Dict[str, Any]:
        # Check if span > 180 days (e.g. 1-year query) and not single day
        start_d = dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        end_d = dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        span_days = (end_d - start_d).days + 1

        if span_days > 180 and not is_single_day:
            slices = split_date_range_into_slices(start_date, end_date, max_slice_days=90)
            total_comm = Decimal("0")
            total_vol = Decimal("0")
            last_raw_data = {}

            for s_str, e_str in slices:
                sub_res = self.query_invitee_sapi(client, user_id, s_str, e_str)
                if sub_res.get("status") in ("success", "empty_zero"):
                    c_dec = Decimal(str(sub_res.get("commission", "0") or "0"))
                    v_dec = Decimal(str(sub_res.get("trade_vol", "0") or "0"))
                    total_comm += c_dec
                    total_vol += v_dec
                    if sub_res.get("raw_data"):
                        last_raw_data.update({k: v for k, v in sub_res["raw_data"].items() if v is not None})
                else:
                    # Never publish a partial sum as a complete annual total.
                    return {**sub_res, "status": "incomplete", "normalization_reason": "failed_slice",
                            "error_msg": f"Missing slice {s_str}..{e_str}; retry or supply historical snapshots"}
                time.sleep(self.delay_seconds)

            res = {
                "status": "success",
                "commission": format_decimal(total_comm, 4),
                "trade_vol": format_decimal(total_vol, 4),
                "normalization_reason": f"complete_sliced_{len(slices)}_slices",
                "raw_data": last_raw_data,
                "used_weight": "sliced",
            }
        else:
            res = self.query_invitee_sapi(client, user_id, start_date, end_date)

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        conn = get_db()
        cur = conn.cursor()

        try:
            status = res["status"]
            raw_data = res.get("raw_data") or {}

            # Update customer profile attributes if returned in raw_data
            if raw_data:
                cur.execute("""
                    UPDATE customers SET
                        vip_level = COALESCE(?, vip_level),
                        kyc_time = COALESCE(?, kyc_time),
                        first_deposit_time = COALESCE(?, first_deposit_time),
                        first_trade_time = COALESCE(?, first_trade_time),
                        spot_rebate_rate = COALESCE(?, spot_rebate_rate),
                        futures_rebate_rate = COALESCE(?, futures_rebate_rate),
                        referral_code = COALESCE(?, referral_code),
                        platform_note = COALESCE(platform_note, ?),
                        last_synced_at = ?
                    WHERE user_id = ?
                """, (
                    raw_data.get("vipLevel"),
                    raw_data.get("kycTime"),
                    raw_data.get("firstDepositTime"),
                    raw_data.get("firstTradeTime"),
                    str(raw_data.get("inviteeSpotRate")) if raw_data.get("inviteeSpotRate") is not None else None,
                    str(raw_data.get("inviteeFuturesRate")) if raw_data.get("inviteeFuturesRate") is not None else None,
                    raw_data.get("referralCode"),
                    raw_data.get("note") or None,
                    now_iso,
                    user_id,
                ))

            # Store metrics only when query succeeded (success or empty_zero)
            if status in ("success", "empty_zero"):
                comm_str = res["commission"]
                vol_str = res["trade_vol"]
                norm_reason = res["normalization_reason"]

                cur.execute("""
                    INSERT INTO customer_metrics (
                        user_id, start_date, end_date, is_single_day,
                        trade_vol, commission, source, unit_status,
                        status, normalization_reason, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'sapi', 'unconfirmed', ?, ?, ?)
                    ON CONFLICT(user_id, start_date, end_date, source) DO UPDATE SET
                        trade_vol = excluded.trade_vol,
                        commission = excluded.commission,
                        status = excluded.status,
                        normalization_reason = excluded.normalization_reason,
                        fetched_at = excluded.fetched_at
                """, (
                    user_id, start_date, end_date, 1 if is_single_day else 0,
                    vol_str, comm_str, status, norm_reason, now_iso,
                ))
            else:
                # If error occurred, DO NOT overwrite an existing successful record!
                # Only insert a failed placeholder if no prior success exists.
                cur.execute("""
                    SELECT id, status FROM customer_metrics
                    WHERE user_id = ? AND start_date = ? AND end_date = ? AND source = 'sapi'
                """, (user_id, start_date, end_date))
                existing = cur.fetchone()
                if not existing or existing["status"] not in ("success", "empty_zero"):
                    cur.execute("""
                        INSERT INTO customer_metrics (
                            user_id, start_date, end_date, is_single_day,
                            trade_vol, commission, source, unit_status,
                            status, normalization_reason, error_code, error_msg, fetched_at
                        ) VALUES (?, ?, ?, ?, NULL, NULL, 'sapi', 'unconfirmed', ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, start_date, end_date, source) DO UPDATE SET
                            status = excluded.status,
                            normalization_reason = excluded.normalization_reason,
                            error_code = excluded.error_code,
                            error_msg = excluded.error_msg,
                            fetched_at = excluded.fetched_at
                    """, (
                        user_id, start_date, end_date, 1 if is_single_day else 0,
                        status, res.get("normalization_reason"),
                        res.get("error_code"), str(res.get("error_msg") or "")[:200], now_iso,
                    ))

            conn.commit()
        finally:
            conn.close()

        return res

    def sync_customer_profile_all_time(
        self,
        client: BinanceClient,
        user_id: str,
    ) -> Dict[str, Any]:
        """Fetch all-time cumulative performance and profile for a customer."""
        res = self.query_invitee_sapi(client, user_id)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        conn = get_db()
        cur = conn.cursor()

        try:
            if res.get("status") in ("success", "empty_zero"):
                raw_data = res.get("raw_data") or {}
                comm_str = res["commission"]
                vol_str = res["trade_vol"]
                cur.execute("""
                    UPDATE customers SET
                        all_time_trade_vol = ?,
                        all_time_commission = ?,
                        vip_level = COALESCE(?, vip_level),
                        kyc_time = COALESCE(?, kyc_time),
                        first_deposit_time = COALESCE(?, first_deposit_time),
                        first_trade_time = COALESCE(?, first_trade_time),
                        spot_rebate_rate = COALESCE(?, spot_rebate_rate),
                        futures_rebate_rate = COALESCE(?, futures_rebate_rate),
                        referral_code = COALESCE(?, referral_code),
                        platform_note = COALESCE(platform_note, ?),
                        last_synced_at = ?
                    WHERE user_id = ?
                """, (
                    vol_str,
                    comm_str,
                    raw_data.get("vipLevel"),
                    raw_data.get("kycTime"),
                    raw_data.get("firstDepositTime"),
                    raw_data.get("firstTradeTime"),
                    str(raw_data.get("inviteeSpotRate")) if raw_data.get("inviteeSpotRate") is not None else None,
                    str(raw_data.get("inviteeFuturesRate")) if raw_data.get("inviteeFuturesRate") is not None else None,
                    raw_data.get("referralCode"),
                    raw_data.get("note") or None,
                    now_iso,
                    user_id,
                ))
                conn.commit()
        finally:
            conn.close()

        return res

    def sync_referral_code_metric(
        self,
        client: BinanceClient,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch and store performance for a referral code."""
        try:
            resp = client.code_performance(code, start_date, end_date)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
                conn = get_db()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO referral_metrics (
                        code, start_date, end_date, clicks, referrals,
                        trade_vol, commission, note, create_time, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code, start_date, end_date) DO UPDATE SET
                        clicks = excluded.clicks,
                        referrals = excluded.referrals,
                        trade_vol = excluded.trade_vol,
                        commission = excluded.commission,
                        note = COALESCE(excluded.note, referral_metrics.note),
                        create_time = COALESCE(excluded.create_time, referral_metrics.create_time),
                        fetched_at = excluded.fetched_at
                """, (
                    code,
                    start_date or "ALL",
                    end_date or "ALL",
                    int(data.get("clicks") or 0),
                    int(data.get("referrals") or 0),
                    format_decimal(data.get("tradeVol"), 4),
                    format_decimal(data.get("commission"), 4),
                    data.get("note"),
                    data.get("createTime"),
                    now_iso,
                ))
                conn.commit()
                conn.close()
                return {"status": "success", "data": data}
            else:
                return {"status": "error", "code": resp.status_code, "msg": resp.text[:200]}
        except Exception as e:
            return {"status": "exception", "msg": str(e)}

    def run_backfill_job(
        self,
        job_id: str,
        user_ids: List[str],
        dates: List[Tuple[str, str, bool]],  # (start_date, end_date, is_single_day)
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        skip_existing: bool = False,
    ) -> None:
        """Run batch backfill job in serial with delay."""
        with self._lock:
            self.active_jobs.add(job_id)

        try:
            client = get_binance_client()
            conn = get_db()
            cur = conn.cursor()

            total_ops = len(user_ids) * len(dates)
            now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

            cur.execute("""
                INSERT INTO sync_jobs (
                    id, job_type, status, total_items, processed_items,
                    success_items, failed_items, started_at, updated_at
                ) VALUES (?, 'backfill', 'running', ?, 0, 0, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status = 'running', updated_at = excluded.updated_at
            """, (job_id, total_ops, now_iso, now_iso))
            conn.commit()

            existing_set = set()
            if skip_existing:
                cur.execute("""
                    SELECT *
                    FROM customer_metrics
                    WHERE source = 'sapi' AND status IN ('success', 'empty_zero')
                """)
                existing_set = {(r["user_id"], r["start_date"], r["end_date"], bool(r["is_single_day"])) for r in cur.fetchall() if metric_is_complete(r)}

            conn.close()

            if not client:
                self._update_job_status(job_id, "failed", "API credentials missing (BK_KEY/BK_SEC)")
                return

            processed = 0
            success = 0
            failed = 0

            for uid in user_ids:
                if self.is_cancelled(job_id):
                    self._update_job_status(job_id, "cancelled", f"Job cancelled after {processed}/{total_ops} operations")
                    return

                # Keep customer all-time cumulative performance & profile up to date
                try:
                    if hasattr(client, "invitee_performance"):
                        self.sync_customer_profile_all_time(client, uid)
                except Exception:
                    pass

                for s_date, e_date, is_single in dates:
                    if self.is_cancelled(job_id):
                        self._update_job_status(job_id, "cancelled", f"Job cancelled after {processed}/{total_ops} operations")
                        return

                    if skip_existing and (uid, s_date, e_date, bool(is_single)) in existing_set:
                        processed += 1
                        success += 1
                        if processed % 5 == 0 or processed == total_ops:
                            self._update_job_progress(job_id, processed, success, failed, uid)
                        continue

                    res = self.sync_customer_metric(client, uid, s_date, e_date, is_single_day=is_single)
                    processed += 1
                    if res.get("status") in ("success", "empty_zero"):
                        success += 1
                    else:
                        failed += 1

                    # Update progress every 5 items or at end
                    if processed % 5 == 0 or processed == total_ops:
                        self._update_job_progress(job_id, processed, success, failed, uid)
                        if on_progress:
                            on_progress({
                                "job_id": job_id,
                                "processed": processed,
                                "total": total_ops,
                                "success": success,
                                "failed": failed,
                                "current_uid": uid,
                            })

                    # Safe rate-limiting delay
                    time.sleep(self.delay_seconds)

            self._update_job_status(job_id, "completed", f"Backfill finished: {success} succeeded, {failed} failed")
        finally:
            with self._lock:
                self.active_jobs.discard(job_id)

    def _update_job_progress(self, job_id: str, processed: int, success: int, failed: int, uid: str) -> None:
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        conn = get_db()
        conn.execute("""
            UPDATE sync_jobs SET
                processed_items = ?, success_items = ?, failed_items = ?,
                current_uid = ?, updated_at = ?
            WHERE id = ?
        """, (processed, success, failed, uid, now_iso, job_id))
        conn.commit()
        conn.close()

    def _update_job_status(self, job_id: str, status: str, message: str) -> None:
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        conn = get_db()
        conn.execute("""
            UPDATE sync_jobs SET
                status = ?, message = ?, updated_at = ?, finished_at = ?
            WHERE id = ?
        """, (status, message, now_iso, now_iso, job_id))
        conn.commit()
        conn.close()


# Global singleton instance
sync_manager = SyncManager(delay_seconds=0.25)
