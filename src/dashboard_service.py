"""Dashboard business logic and query service.
Computes metrics, periods, trends, customer profiles, followups, and activations.
"""

from decimal import Decimal
import datetime as dt
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))

from database import format_decimal, get_db, to_decimal, metric_is_complete


def parse_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str, "%Y-%m-%d").date()


def format_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_customer_datetime(val: Any) -> Optional[dt.datetime]:
    """Parse integer timestamp (ms or s) or ISO date string into UTC datetime."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", ""):
        return None
    try:
        ts = float(s)
        if ts > 1e11:
            ts /= 1000.0
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except Exception:
            pass
    return None


def get_default_reference_end() -> str:
    """Return yesterday in UTC as YYYY-MM-DD string (the latest complete UTC date)."""
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    return yesterday.isoformat()


def compute_period_bounds(days: int = 15, reference_end: Optional[str] = None) -> Tuple[str, str, str, str]:
    """Compute current and previous closed-interval date bounds [start, end].
    Returns (curr_start, curr_end, prev_start, prev_end).
    """
    if not isinstance(reference_end, str) or not reference_end:
        reference_end = get_default_reference_end()
    end_d = parse_date(reference_end)

    # Closed interval of 'days' count: curr_start is end_d - (days - 1)
    curr_start_d = end_d - dt.timedelta(days=days - 1)
    prev_end_d = curr_start_d - dt.timedelta(days=1)
    prev_start_d = prev_end_d - dt.timedelta(days=days - 1)

    return (
        format_date(curr_start_d),
        format_date(end_d),
        format_date(prev_start_d),
        format_date(prev_end_d),
    )


class DashboardService:
    """Provides high-performance aggregated metrics from SQLite."""

    def get_overview(
        self,
        days: int = 15,
        exclude_top: int = 0,
        reference_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate overview metrics and linked trends."""
        reference_end = reference_end or get_default_reference_end()
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(days, reference_end)

        conn = get_db()
        cur = conn.cursor()

        # Total customers count
        cur.execute("SELECT COUNT(*) FROM customers")
        total_customers = cur.fetchone()[0]

        # Starred count
        cur.execute("SELECT COUNT(*) FROM customers WHERE is_starred = 1")
        starred_count = cur.fetchone()[0]

        # Fetch current period customer metrics
        cur.execute("""
            SELECT user_id, commission, trade_vol, status, normalization_reason
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND source = 'sapi'
        """, (curr_start, curr_end))
        curr_rows = cur.fetchall()

        # Fetch previous period customer metrics
        cur.execute("""
            SELECT user_id, commission, trade_vol, status, normalization_reason
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND source = 'sapi'
        """, (prev_start, prev_end))
        prev_rows = cur.fetchall()

        curr_map = {r["user_id"]: r for r in curr_rows if metric_is_complete(r)}
        prev_map = {r["user_id"]: r for r in prev_rows if metric_is_complete(r)}

        def add_complete_daily_totals(metric_map, start_date, end_date):
            daily_totals = {}
            cur.execute("""
                SELECT user_id, start_date, commission, trade_vol
                FROM customer_metrics
                WHERE is_single_day = 1 AND source = 'sapi'
                  AND status IN ('success', 'empty_zero')
                  AND start_date >= ? AND start_date <= ?
            """, (start_date, end_date))
            for row in cur.fetchall():
                comm = to_decimal(row["commission"])
                vol = to_decimal(row["trade_vol"])
                if comm is None or vol is None:
                    continue
                uid = row["user_id"]
                if uid not in daily_totals:
                    daily_totals[uid] = {"dates": set(), "commission": Decimal("0"), "trade_vol": Decimal("0")}
                daily_totals[uid]["dates"].add(row["start_date"])
                daily_totals[uid]["commission"] += comm
                daily_totals[uid]["trade_vol"] += vol

            for uid, totals in daily_totals.items():
                if uid not in metric_map and len(totals["dates"]) == days:
                    metric_map[uid] = {
                        "user_id": uid,
                        "commission": format_decimal(totals["commission"], 4),
                        "trade_vol": format_decimal(totals["trade_vol"], 4),
                        "status": "success",
                    }

        add_complete_daily_totals(curr_map, curr_start, curr_end)
        add_complete_daily_totals(prev_map, prev_start, prev_end)

        synced_uids = set(curr_map.keys())
        active_uids = set()

        total_comm = Decimal("0")
        total_vol = Decimal("0")

        # Per-client breakdown for ranking and exclusion
        client_rankings = []
        for uid, row in curr_map.items():
            comm = to_decimal(row["commission"]) or Decimal("0")
            vol = to_decimal(row["trade_vol"]) or Decimal("0")
            prev_row = prev_map.get(uid)
            prev_comm = to_decimal(prev_row["commission"]) if prev_row else Decimal("0")

            if comm > 0:
                active_uids.add(uid)

            total_comm += comm
            total_vol += vol

            delta_pct = None
            if prev_comm and prev_comm > 0:
                delta_pct = float((comm - prev_comm) / prev_comm * 100)

            client_rankings.append({
                "user_id": uid,
                "commission": comm,
                "volume": vol,
                "prev_commission": prev_comm,
                "delta_pct": delta_pct,
            })

        # Sort by current period commission descending
        client_rankings.sort(key=lambda x: x["commission"], reverse=True)

        # Exclusion of top N clients
        excluded_rankings = client_rankings[:exclude_top] if exclude_top > 0 else []
        included_rankings = client_rankings[exclude_top:] if exclude_top > 0 else client_rankings

        excluded_uids = [c["user_id"] for c in excluded_rankings]
        included_uids = [c["user_id"] for c in included_rankings]

        # Calculate metrics for included clients
        total_comm = sum((c["commission"] for c in included_rankings), Decimal("0"))
        total_vol = sum((c["volume"] for c in included_rankings), Decimal("0"))
        total_prev_comm = sum((c["prev_commission"] for c in included_rankings), Decimal("0"))
        total_prev_vol = sum((to_decimal(prev_map[c["user_id"]]["trade_vol"]) if (c["user_id"] in prev_map and prev_map[c["user_id"]]["trade_vol"]) else Decimal("0")) for c in included_rankings)

        excluded_comm = sum((c["commission"] for c in excluded_rankings), Decimal("0"))
        excluded_vol = sum((c["volume"] for c in excluded_rankings), Decimal("0"))

        delta_pct_total = None
        if total_prev_comm > 0:
            delta_pct_total = float((total_comm - total_prev_comm) / total_prev_comm * 100)

        # Top 10 customers for preview table
        top_10_uids = [c["user_id"] for c in client_rankings[:10]]
        top_10_customers = []
        if top_10_uids:
            placeholders = ",".join("?" for _ in top_10_uids)
            cur.execute(f"""
                SELECT user_id, platform_note, manual_note, referral_code, vip_level, is_starred
                FROM customers WHERE user_id IN ({placeholders})
            """, top_10_uids)
            info_map = {r["user_id"]: r for r in cur.fetchall()}
            for rank_item in client_rankings[:10]:
                uid = rank_item["user_id"]
                info = info_map.get(uid)
                disp_name = (info["platform_note"] if info and info["platform_note"] else f"客户 {uid[-4:]}")
                top_10_customers.append({
                    "user_id": uid,
                    "name": disp_name,
                    "vip_level": info["vip_level"] if info else 0,
                    "referral_code": info["referral_code"] if info else "",
                    "is_starred": bool(info["is_starred"]) if info else False,
                    "commission": format_decimal(rank_item["commission"], 2),
                    "prev_commission": format_decimal(rank_item["prev_commission"], 2) if rank_item.get("prev_commission") is not None else None,
                    "volume": format_decimal(rank_item["volume"], 2),
                    "delta_pct": rank_item["delta_pct"],
                })

        # Fetch excluded names
        excluded_names = []
        if excluded_uids:
            placeholders = ",".join("?" for _ in excluded_uids)
            cur.execute(f"SELECT user_id, platform_note FROM customers WHERE user_id IN ({placeholders})", excluded_uids)
            for r in cur.fetchall():
                n = r["platform_note"] or f"客户 {r['user_id'][-4:]}"
                excluded_names.append(n)

        # Linked trend series from daily metrics
        # Generate list of dates for current period and previous period
        curr_dates = [format_date(curr_start_d + dt.timedelta(days=i)) for curr_start_d in [parse_date(curr_start)] for i in range(days)]
        prev_dates = [format_date(prev_start_d + dt.timedelta(days=i)) for prev_start_d in [parse_date(prev_start)] for i in range(days)]

        daily_series_curr_comm = [0.0] * days
        daily_series_curr_vol = [0.0] * days
        daily_series_prev_comm = [0.0] * days
        daily_series_prev_vol = [0.0] * days

        synced_curr_dates = set()
        synced_prev_dates = set()

        # Daily trends remain available even when period aggregates have not been synced.
        cur.execute("""
            SELECT DISTINCT user_id
            FROM customer_metrics
            WHERE is_single_day = 1 AND source = 'sapi'
              AND ((start_date >= ? AND start_date <= ?) OR (start_date >= ? AND start_date <= ?))
        """, (curr_start, curr_end, prev_start, prev_end))
        excluded_uid_set = set(excluded_uids)
        trend_uids = [r[0] for r in cur.fetchall() if r[0] not in excluded_uid_set]

        if trend_uids:
            uid_placeholders = ",".join("?" for _ in trend_uids)
            # Current daily metrics
            cur.execute(f"""
                SELECT start_date, SUM(CAST(commission AS REAL)), SUM(CAST(trade_vol AS REAL))
                FROM customer_metrics
                WHERE is_single_day = 1 AND user_id IN ({uid_placeholders})
                  AND start_date >= ? AND start_date <= ? AND source = 'sapi'
                GROUP BY start_date
            """, (*trend_uids, curr_start, curr_end))
            for row in cur.fetchall():
                d_str = row[0]
                if d_str in curr_dates:
                    synced_curr_dates.add(d_str)
                    idx = curr_dates.index(d_str)
                    daily_series_curr_comm[idx] = float(row[1] or 0.0)
                    daily_series_curr_vol[idx] = float(row[2] or 0.0)

            # Previous daily metrics
            cur.execute(f"""
                SELECT start_date, SUM(CAST(commission AS REAL)), SUM(CAST(trade_vol AS REAL))
                FROM customer_metrics
                WHERE is_single_day = 1 AND user_id IN ({uid_placeholders})
                  AND start_date >= ? AND start_date <= ? AND source = 'sapi'
                GROUP BY start_date
            """, (*trend_uids, prev_start, prev_end))
            for row in cur.fetchall():
                d_str = row[0]
                if d_str in prev_dates:
                    synced_prev_dates.add(d_str)
                    idx = prev_dates.index(d_str)
                    daily_series_prev_comm[idx] = float(row[1] or 0.0)
                    daily_series_prev_vol[idx] = float(row[2] or 0.0)

        # Check if we have daily records
        synced_days_count = len(synced_curr_dates)
        has_daily_data = (synced_days_count > 0)
        synced_mask = [d in synced_curr_dates for d in curr_dates]

        if synced_days_count == days:
            notice = f"已完整接入本期全部 {days} 天日级时序数据"
        elif synced_days_count > 0:
            notice = f"日级时序已覆盖 {synced_days_count}/{days} 天（覆盖 {min(synced_curr_dates)} 至 {max(synced_curr_dates)}，更早日期需回补）"
        else:
            notice = f"暂无近 {days} 天的日级时序数据，需执行历史回补以生成趋势折线。"

        conn.close()

        coverage_ratio = float(len(synced_uids) / total_customers) if total_customers > 0 else 0.0

        return {
            "period": {
                "days": days,
                "current_start": curr_start,
                "current_end": curr_end,
                "previous_start": prev_start,
                "previous_end": prev_end,
                "curr_range": [curr_start, curr_end],
                "prev_range": [prev_start, prev_end],
                "label": f"{curr_start} ~ {curr_end}",
            },
            "coverage": {
                "total_customers": total_customers,
                "synced_customers": len(synced_uids),
                "active_customers": len(active_uids),
                "coverage_ratio": round(coverage_ratio, 4),
                "coverage_pct_label": f"{round(coverage_ratio * 100, 1)}%",
                "note": f"已同步 {len(synced_uids)} 位客户（占库内 {round(coverage_ratio * 100, 1)}%）；未同步客户未参与本期聚合。",
            },
            "metrics": {
                "total_commission": format_decimal(total_comm, 2),
                "total_volume": format_decimal(total_vol, 2),
                "prev_commission": format_decimal(total_prev_comm, 2),
                "prev_volume": format_decimal(total_prev_vol, 2),
                "delta_pct": round(delta_pct_total, 1) if delta_pct_total is not None else None,
                "unit_disclaimer": "计价单位：USDT",
            },
            "market_status": {
                "mode": "all",
                "spot_futures_available": False,
                "reason": "底层 API 暂未提供现货与合约的可靠拆分数据，暂按全部业务口径汇总",
                "composition_disclaimer": "能力未接通（API 未拆分业务线）",
            },
            "star_summary": {
                "starred_count": starred_count,
            },
            "exclusion": {
                "exclude_top": exclude_top,
                "excluded_count": len(excluded_uids),
                "included_count": len(included_uids),
                "excluded_names": excluded_names,
                "excluded_commission": format_decimal(excluded_comm, 2),
                "excluded_volume": format_decimal(excluded_vol, 2),
            },
            "top_customers": top_10_customers,
            "trends": {
                "has_daily_data": has_daily_data,
                "synced_days_count": synced_days_count,
                "total_days": days,
                "is_complete": synced_days_count == days,
                "synced_mask": synced_mask,
                "dates": [d[5:].replace("-", "/") for d in curr_dates],
                "full_dates": curr_dates,
                "current_commission": daily_series_curr_comm,
                "current_volume": daily_series_curr_vol,
                "previous_commission": daily_series_prev_comm,
                "previous_volume": daily_series_prev_vol,
                "notice": notice,
            },
        }

    def get_customers(
        self,
        days: int = 15,
        tab: str = "all",
        search: str = "",
        sort_by: str = "commission",
        reference_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch customer list with period metrics, star flags, search and sort."""
        reference_end = reference_end or get_default_reference_end()
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(days, reference_end)

        conn = get_db()
        cur = conn.cursor()

        # Query all customers
        cur.execute("""
            SELECT user_id, referral_code, platform_note, manual_note, vip_level, is_starred,
                   reg_time, kyc_time, first_deposit_time, first_trade_time,
                   next_followup_date, all_time_trade_vol, all_time_commission
            FROM customers
        """)
        cust_rows = cur.fetchall()

        # Query metrics for current period
        cur.execute("""
            SELECT *
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND source = 'sapi'
        """, (curr_start, curr_end))
        curr_metrics = {r["user_id"]: r for r in cur.fetchall() if metric_is_complete(r)}

        # Query metrics for previous period
        cur.execute("""
            SELECT *
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND source = 'sapi'
        """, (prev_start, prev_end))
        prev_metrics = {r["user_id"]: r for r in cur.fetchall() if metric_is_complete(r)}

        coverage = {
            "total_customers": len(cust_rows),
            "current_missing": sum(c["user_id"] not in curr_metrics for c in cust_rows),
            "previous_missing": sum(c["user_id"] not in prev_metrics for c in cust_rows),
        }

        # Query last rebate date from single-day customer_metrics or csv
        cur.execute("""
            SELECT user_id, MAX(start_date) as last_date
            FROM customer_metrics
            WHERE is_single_day = 1 AND CAST(commission AS REAL) > 0
            GROUP BY user_id
        """)
        last_rebate_map = {r["user_id"]: r["last_date"] for r in cur.fetchall()}

        # Query sparkline points (up to 15 days of recent single-day metrics)
        cur.execute("""
            SELECT user_id, start_date, commission
            FROM customer_metrics
            WHERE is_single_day = 1 AND start_date >= ? AND start_date <= ?
            ORDER BY start_date ASC
        """, (curr_start, curr_end))
        spark_rows = cur.fetchall()
        spark_map: Dict[str, List[float]] = {}
        for r in spark_rows:
            uid = r["user_id"]
            if uid not in spark_map:
                spark_map[uid] = []
            spark_map[uid].append(float(to_decimal(r["commission"]) or Decimal("0")))

        results = []
        search_lower = search.strip().lower()

        for c in cust_rows:
            uid = c["user_id"]
            plat_note = c["platform_note"] or ""
            man_note = c["manual_note"] or ""
            ref_code = c["referral_code"] or ""
            disp_name = plat_note if plat_note else f"客户 {uid[-4:]}"

            # Search filter
            if search_lower:
                match = (search_lower in uid.lower() or
                         search_lower in disp_name.lower() or
                         search_lower in ref_code.lower() or
                         search_lower in man_note.lower())
                if not match:
                    continue

            curr_m = curr_metrics.get(uid)
            prev_m = prev_metrics.get(uid)

            has_synced = curr_m is not None
            comm = to_decimal(curr_m["commission"]) if curr_m and curr_m["commission"] is not None else None
            vol = to_decimal(curr_m["trade_vol"]) if curr_m and curr_m["trade_vol"] is not None else None
            prev_comm = to_decimal(prev_m["commission"]) if prev_m and prev_m["commission"] is not None else None

            delta_pct = None
            if comm is not None and prev_comm is not None and prev_comm > 0:
                delta_pct = float((comm - prev_comm) / prev_comm * 100)

            is_starred = bool(c["is_starred"])
            last_date = last_rebate_map.get(uid)

            # Tab filter
            if tab == "star" and not is_starred:
                continue
            if tab == "decline":
                # Significant decline: delta_pct <= -20%
                if delta_pct is None or delta_pct > -20:
                    continue
            if tab == "silent":
                # Silent: has synced data and current period commission == 0
                if comm is None or comm > 0:
                    continue

            spark_points = spark_map.get(uid, [])
            if not spark_points and comm is not None:
                # If no daily points, leave spark empty
                spark_points = []

            results.append({
                "user_id": uid,
                "name": disp_name,
                "platform_note": plat_note,
                "manual_note": man_note,
                "referral_code": ref_code,
                "vip_level": c["vip_level"],
                "is_starred": is_starred,
                "next_followup_date": c["next_followup_date"],
                "has_synced": has_synced,
                "commission": format_decimal(comm, 2) if comm is not None else "待同步",
                "volume": format_decimal(vol, 2) if vol is not None else "待同步",
                "prev_commission": format_decimal(prev_comm, 2) if prev_comm is not None else None,
                "delta_pct": delta_pct,
                "last_rebate_date": last_date or "-",
                "sparkline": spark_points,
                "_comm_dec": comm if comm is not None else Decimal("-999999"),
                "_decline_dec": (prev_comm - comm) if (prev_comm is not None and comm is not None) else Decimal("-999999"),
            })

        # Sorting
        if sort_by == "decline":
            results.sort(key=lambda x: x["_decline_dec"], reverse=True)
        else:
            results.sort(key=lambda x: x["_comm_dec"], reverse=True)

        # Remove sort helper keys
        for r in results:
            del r["_comm_dec"]
            del r["_decline_dec"]

        conn.close()

        return {
            "period": {
                "days": days,
                "start_date": curr_start,
                "end_date": curr_end,
                "current_start": curr_start,
                "current_end": curr_end,
                "curr_range": [curr_start, curr_end],
                "prev_range": [prev_start, prev_end],
                "label": f"{curr_start} ~ {curr_end}",
            },
            "tab": tab,
            "coverage": coverage,
            "total_count": len(results),
            "customers": results,
        }

    def get_customer_detail(self, user_id: str, days: int = 15, reference_end: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get full details of a single customer."""
        reference_end = reference_end or get_default_reference_end()
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(days, reference_end)

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT * FROM customers WHERE user_id = ?", (user_id,))
        c = cur.fetchone()
        if not c:
            conn.close()
            return None

        # Period metrics
        cur.execute("""
            SELECT commission, trade_vol, status, normalization_reason, fetched_at
            FROM customer_metrics
            WHERE user_id = ? AND start_date = ? AND end_date = ? AND source = 'sapi'
        """, (user_id, curr_start, curr_end))
        curr_m = cur.fetchone()

        # If current period metrics not in database, attempt lazy on-demand sync from SAPI
        if not curr_m:
            try:
                from sync_service import get_binance_client, sync_manager
                client = get_binance_client()
                if client:
                    sync_manager.sync_customer_metric(client, user_id, curr_start, curr_end, is_single_day=False)
                    sync_manager.sync_customer_metric(client, user_id, prev_start, prev_end, is_single_day=False)
                    cur.execute("""
                        SELECT commission, trade_vol, status, normalization_reason, fetched_at
                        FROM customer_metrics
                        WHERE user_id = ? AND start_date = ? AND end_date = ? AND source = 'sapi'
                    """, (user_id, curr_start, curr_end))
                    curr_m = cur.fetchone()
            except Exception:
                pass

        cur.execute("""
            SELECT commission, trade_vol, status
            FROM customer_metrics
            WHERE user_id = ? AND start_date = ? AND end_date = ? AND source = 'sapi'
        """, (user_id, prev_start, prev_end))
        prev_m = cur.fetchone()

        comm = to_decimal(curr_m["commission"]) if curr_m and curr_m["commission"] is not None else None
        vol = to_decimal(curr_m["trade_vol"]) if curr_m and curr_m["trade_vol"] is not None else None
        prev_comm = to_decimal(prev_m["commission"]) if prev_m and prev_m["commission"] is not None else None

        delta_pct = None
        if comm is not None and prev_comm is not None and prev_comm > 0:
            delta_pct = float((comm - prev_comm) / prev_comm * 100)

        # Subaccounts
        cur.execute("SELECT sub_account_id FROM subaccount_links WHERE parent_user_id = ?", (user_id,))
        subs = [r[0] for r in cur.fetchall()]

        # Followup history
        cur.execute("""
            SELECT id, note, followup_date, created_at
            FROM customer_followups
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,))
        followups = [dict(r) for r in cur.fetchall()]

        # Daily sparkline points
        cur.execute("""
            SELECT start_date, commission
            FROM customer_metrics
            WHERE user_id = ? AND is_single_day = 1 AND start_date >= ? AND start_date <= ?
            ORDER BY start_date ASC
        """, (user_id, curr_start, curr_end))
        daily_points = [float(to_decimal(r["commission"]) or Decimal("0")) for r in cur.fetchall()]

        conn.close()

        disp_name = (c["platform_note"] if c["platform_note"] else f"客户 {user_id[-4:]}")

        return {
            "user_id": user_id,
            "name": disp_name,
            "platform_note": c["platform_note"],
            "manual_note": c["manual_note"],
            "vip_level": c["vip_level"],
            "referral_code": c["referral_code"],
            "is_starred": bool(c["is_starred"]),
            "next_followup_date": c["next_followup_date"],
            "reg_time": c["reg_time"],
            "kyc_time": c["kyc_time"],
            "first_deposit_time": c["first_deposit_time"],
            "first_trade_time": c["first_trade_time"],
            "spot_rebate_rate": c["spot_rebate_rate"],
            "futures_rebate_rate": c["futures_rebate_rate"],
            "all_time_trade_vol": c["all_time_trade_vol"],
            "all_time_commission": c["all_time_commission"],
            "subaccounts": subs,
            "period": {
                "days": days,
                "start_date": curr_start,
                "end_date": curr_end,
                "commission": format_decimal(comm, 2) if comm is not None else "待同步",
                "volume": format_decimal(vol, 2) if vol is not None else "待同步",
                "prev_commission": format_decimal(prev_comm, 2) if prev_comm is not None else None,
                "delta_pct": delta_pct,
                "daily_points": daily_points,
            },
            "followups": followups,
        }

    def get_care_dashboard(
        self,
        days: int = 15,
        reference_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch unified care & re-engagement dashboard data (workbench, channels, review)."""
        reference_end = reference_end or get_default_reference_end()
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(days, reference_end)

        conn = get_db()
        cur = conn.cursor()

        def normalize_date_str(val) -> str:
            if not val:
                return ""
            if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit() and len(val) >= 10):
                ts = int(val)
                if ts > 1e11:
                    ts /= 1000
                return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
            return str(val)[:10]

        # 1. Query all customers
        cur.execute("""
            SELECT user_id, referral_code, platform_note, manual_note, tags, vip_level,
                   reg_time, kyc_time, first_deposit_time, first_trade_time,
                   spot_rebate_rate, futures_rebate_rate,
                   all_time_trade_vol, all_time_commission, is_starred, next_followup_date
            FROM customers
        """)
        cust_rows = cur.fetchall()

        # 2. Query metrics for current period (prefer period aggregate, fallback to sum of daily)
        cur.execute("""
            SELECT *
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND is_single_day = 0 AND source = 'sapi'
        """, (curr_start, curr_end))
        curr_metrics = {r["user_id"]: r for r in cur.fetchall() if metric_is_complete(r)}

        cur.execute("""
            SELECT user_id, SUM(CAST(trade_vol AS REAL)) as vol, SUM(CAST(commission AS REAL)) as comm, COUNT(*) as cnt
            FROM customer_metrics
            WHERE is_single_day = 1 AND start_date >= ? AND end_date <= ?
            GROUP BY user_id
        """, (curr_start, curr_end))
        for r in cur.fetchall():
            uid = r["user_id"]
            if uid not in curr_metrics and r["cnt"] > 0:
                curr_metrics[uid] = {
                    "user_id": uid,
                    "trade_vol": str(r["vol"] or 0),
                    "commission": str(r["comm"] or 0),
                }

        # 3. Query metrics for previous period
        cur.execute("""
            SELECT *
            FROM customer_metrics
            WHERE start_date = ? AND end_date = ? AND is_single_day = 0 AND source = 'sapi'
        """, (prev_start, prev_end))
        prev_metrics = {r["user_id"]: r for r in cur.fetchall() if metric_is_complete(r)}

        cur.execute("""
            SELECT user_id, SUM(CAST(trade_vol AS REAL)) as vol, SUM(CAST(commission AS REAL)) as comm, COUNT(*) as cnt
            FROM customer_metrics
            WHERE is_single_day = 1 AND start_date >= ? AND end_date <= ?
            GROUP BY user_id
        """, (prev_start, prev_end))
        for r in cur.fetchall():
            uid = r["user_id"]
            if uid not in prev_metrics and r["cnt"] > 0:
                prev_metrics[uid] = {
                    "user_id": uid,
                    "trade_vol": str(r["vol"] or 0),
                    "commission": str(r["comm"] or 0),
                }

        # 4. Referral code details & notes
        cur.execute("SELECT code, note, referrals, trade_vol, commission FROM referral_metrics")
        ref_rows = cur.fetchall()
        ref_map = {r["code"]: r for r in ref_rows}
        ref_notes = {r["code"]: (r["note"] or "") for r in ref_rows}

        # 5. Followup logs & latest followups map
        cur.execute("""
            SELECT f.id, f.user_id, f.note, f.followup_date, f.created_at,
                   c.platform_note, c.manual_note
            FROM customer_followups f
            LEFT JOIN customers c ON f.user_id = c.user_id
            ORDER BY f.created_at DESC
        """)
        raw_logs = cur.fetchall()
        all_logs = []
        latest_followup_map = {}
        contacted_uids = set()

        for l in raw_logs:
            uid = l["user_id"]
            contacted_uids.add(uid)
            c_name = l["platform_note"] or f"客户 {uid[-4:]}"
            item = {
                "id": l["id"],
                "user_id": uid,
                "customer_name": c_name,
                "note": l["note"] or "",
                "followup_date": l["followup_date"] or "",
                "created_at": l["created_at"] or "",
            }
            all_logs.append(item)
            if uid not in latest_followup_map:
                latest_followup_map[uid] = item

        # 6. Sparkline points (single-day breakdown within current period)
        cur.execute("""
            SELECT user_id, start_date, commission
            FROM customer_metrics
            WHERE is_single_day = 1 AND start_date >= ? AND start_date <= ?
            ORDER BY start_date ASC
        """, (curr_start, curr_end))
        spark_map: Dict[str, List[float]] = {}
        for r in cur.fetchall():
            uid = r["user_id"]
            if uid not in spark_map:
                spark_map[uid] = []
            spark_map[uid].append(float(to_decimal(r["commission"]) or Decimal("0")))

        # 7. Process customers & statuses
        customer_list = []
        status_counts = {"沉睡": 0, "降温": 0, "活跃": 0, "新星": 0, "数据待补齐": 0}
        code_stats: Dict[str, Dict[str, int]] = {}

        for c in cust_rows:
            uid = c["user_id"]
            ref_code = c["referral_code"] or ""
            plat_note = c["platform_note"] or ""
            man_note = c["manual_note"] or ""
            disp_name = plat_note if plat_note else (f"UID {uid}" if len(uid) <= 8 else f"客户 {uid[-4:]}")

            reg_str = normalize_date_str(c["reg_time"])
            dep_str = normalize_date_str(c["first_deposit_time"])
            trade_str = normalize_date_str(c["first_trade_time"])

            all_time_comm = float(c["all_time_commission"] or 0)
            all_time_vol = float(c["all_time_trade_vol"] or 0)

            curr_m = curr_metrics.get(uid)
            prev_m = prev_metrics.get(uid)

            is_missing = (curr_m is None or prev_m is None)
            comm = float(to_decimal(curr_m["commission"])) if curr_m and curr_m["commission"] is not None else 0.0
            vol = float(to_decimal(curr_m["trade_vol"])) if curr_m and curr_m["trade_vol"] is not None else 0.0
            prev_comm = float(to_decimal(prev_m["commission"])) if prev_m and prev_m["commission"] is not None else 0.0
            prev_vol = float(to_decimal(prev_m["trade_vol"])) if prev_m and prev_m["trade_vol"] is not None else 0.0

            delta_pct = None
            if not is_missing:
                if prev_comm > 0:
                    delta_pct = float(round((comm - prev_comm) / prev_comm * 100, 1))
                else:
                    delta_pct = 100.0 if comm > 0 else 0.0

            # Classification
            if is_missing:
                status = "数据待补齐"
            else:
                newstar_cutoff = format_date(parse_date(reference_end) - dt.timedelta(days=61))
                is_newstar = (reg_str >= newstar_cutoff or trade_str >= newstar_cutoff or (c["tags"] and "新星" in c["tags"]))
                if is_newstar:
                    status = "新星"
                elif (delta_pct is not None and delta_pct <= -80.0) or (comm == 0.0 and all_time_comm >= 50.0):
                    status = "沉睡"
                elif delta_pct is not None and delta_pct <= -30.0:
                    status = "降温"
                else:
                    status = "活跃"

            status_counts[status] += 1

            if ref_code:
                if ref_code not in code_stats:
                    code_stats[ref_code] = {"sample_count": 0, "sample_risk_count": 0}
                code_stats[ref_code]["sample_count"] += 1
                if status in ("沉睡", "降温"):
                    code_stats[ref_code]["sample_risk_count"] += 1

            cust_entry = {
                "uid": uid,
                "name": disp_name,
                "code": ref_code,
                "code_note": ref_notes.get(ref_code, ""),
                "vip": str(c["vip_level"] or 0),
                "reg": reg_str,
                "deposit": dep_str,
                "trade": trade_str,
                "total": all_time_comm,
                "volume": all_time_vol,
                "current": comm,
                "current_volume": vol,
                "previous": prev_comm,
                "previous_volume": prev_vol,
                "delta": delta_pct if delta_pct is not None else 0.0,
                "status": status,
                "missing": is_missing,
                "spot": c["spot_rebate_rate"] or "0%",
                "futures": c["futures_rebate_rate"] or "0%",
                "sparkline": spark_map.get(uid, []),
                "latest_log": latest_followup_map.get(uid),
                "has_followed": uid in contacted_uids,
                "is_starred": bool(c["is_starred"]),
            }
            customer_list.append(cust_entry)

        # 8. Build referral_codes list
        referral_code_list = []
        for code, r in ref_map.items():
            stats = code_stats.get(code, {"sample_count": 0, "sample_risk_count": 0})
            referral_code_list.append({
                "code": code,
                "note": r["note"] or "",
                "count": int(r["referrals"] or 0),
                "sample_count": stats["sample_count"],
                "sample_risk_count": stats["sample_risk_count"],
                "volume": float(r["trade_vol"] or 0),
                "total": float(r["commission"] or 0),
            })
        referral_code_list.sort(key=lambda x: x["volume"], reverse=True)

        # 9. Followup Review Stats
        risk_uids = set(c["uid"] for c in customer_list if c["status"] in ("沉睡", "降温"))
        pending_count = len(risk_uids - contacted_uids)
        waiting_count = sum(1 for c in customer_list if c.get("latest_log") and c["latest_log"].get("followup_date", "") >= reference_end)

        followup_review = {
            "contacted_count": len(contacted_uids),
            "pending_count": pending_count,
            "waiting_count": waiting_count,
            "logs": all_logs,
        }

        # 10. Care trigger signals
        care_signals = []
        for c in customer_list:
            if c["vip"] in ("1", "2"):
                care_signals.append({
                    "type": "vip",
                    "label": f"VIP {c['vip']} 尊享关怀",
                    "uid": c["uid"],
                    "name": c["name"],
                    "desc": f"历史总佣金 {format_decimal(Decimal(str(c['total'])), 2)} U，保持高规格服务响应与专属跟进。",
                    "target": "了解高阶交易工具与大客户返点服务体验",
                })
            elif c["status"] == "新星":
                care_signals.append({
                    "type": "newstar",
                    "label": "新星客户培育",
                    "uid": c["uid"],
                    "name": c["name"],
                    "desc": f"近期首充与首交易转化，本期交易额已达 {format_decimal(Decimal(str(c['current_volume'])), 0)}。",
                    "target": "协助顺畅运行策略，并确认返点结算机制",
                })
            elif c["status"] == "沉睡" and c["total"] >= 20000:
                care_signals.append({
                    "type": "sleep_big",
                    "label": "巨头唤醒预警",
                    "uid": c["uid"],
                    "name": c["name"],
                    "desc": f"历史总佣金超 {format_decimal(Decimal(str(c['total'])), 0)} U，本期贡献骤降，需及时探寻近况。",
                    "target": "核实策略停歇原因或服务支持需求",
                })

        conn.close()

        return {
            "period": {
                "days": days,
                "current_start": curr_start,
                "current_end": curr_end,
                "previous_start": prev_start,
                "previous_end": prev_end,
                "curr_range": [curr_start, curr_end],
                "prev_range": [prev_start, prev_end],
                "label": f"{curr_start} ~ {curr_end}",
            },
            "summary": {
                "total_customers": len(customer_list),
                "sleeping_count": status_counts["沉睡"],
                "cooling_count": status_counts["降温"],
                "active_count": status_counts["活跃"],
                "newstar_count": status_counts["新星"],
                "missing_count": status_counts["数据待补齐"],
            },
            "customers": customer_list,
            "referral_codes": referral_code_list,
            "followup_review": followup_review,
            "care_signals": care_signals[:5],
        }

    def add_followup(self, user_id: str, note: str, next_followup_date: Optional[str] = None) -> bool:
        """Add a followup record and update customer's next date."""
        conn = get_db()
        cur = conn.cursor()
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

        cur.execute("""
            INSERT INTO customer_followups (user_id, note, followup_date, created_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, note, next_followup_date, now_iso))

        cur.execute("""
            UPDATE customers SET
                manual_note = ?,
                next_followup_date = COALESCE(?, next_followup_date),
                updated_at = ?
            WHERE user_id = ?
        """, (note, next_followup_date, now_iso, user_id))

        conn.commit()
        conn.close()
        return True

    def toggle_star(self, user_id: str) -> bool:
        """Toggle is_starred flag for customer."""
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT is_starred FROM customers WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return False
        new_val = 0 if row["is_starred"] else 1
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        cur.execute("UPDATE customers SET is_starred = ?, updated_at = ? WHERE user_id = ?", (new_val, now_iso, user_id))
        conn.commit()
        conn.close()
        return bool(new_val)

    def get_insights(self, days: int = 15, reference_end: Optional[str] = None) -> Dict[str, Any]:
        """Fetch care signals, independent activation milestones, and referral code performance."""
        reference_end = reference_end or get_default_reference_end()
        curr_start, curr_end, prev_start, prev_end = compute_period_bounds(days, reference_end)

        conn = get_db()
        cur = conn.cursor()

        # Parse period boundary timestamps in UTC
        start_d = parse_date(curr_start)
        end_d = parse_date(curr_end)
        start_dt = dt.datetime.combine(start_d, dt.time.min).replace(tzinfo=dt.timezone.utc)
        end_dt = dt.datetime.combine(end_d, dt.time.max).replace(tzinfo=dt.timezone.utc)

        # 1. Independent Activation Milestones (Real-time dynamic filtering by period)
        cur.execute("SELECT user_id, reg_time, kyc_time, first_deposit_time, first_trade_time, all_time_trade_vol FROM customers")
        all_customers = cur.fetchall()

        # Total trading customers across the database (59 customers in database with trade history)
        trading_customers_count = sum(
            1 for c in all_customers
            if (c["first_trade_time"] is not None and str(c["first_trade_time"]).strip() != "")
            or float(to_decimal(c["all_time_trade_vol"]) or Decimal("0")) > 0
        )
        if trading_customers_count == 0:
            trading_customers_count = len(all_customers)

        # Filter customers newly registered within the selected period [curr_start, curr_end]
        new_registrants = []
        for c in all_customers:
            reg_dt = parse_customer_datetime(c["reg_time"])
            if reg_dt and start_dt <= reg_dt <= end_dt:
                new_registrants.append(c)

        period_reg_count = len(new_registrants)

        # From these newly registered customers, count completed KYC, first deposit, and first trade
        period_kyc_count = sum(
            1 for c in new_registrants
            if parse_customer_datetime(c["kyc_time"]) is not None
        )
        period_dep_count = sum(
            1 for c in new_registrants
            if parse_customer_datetime(c["first_deposit_time"]) is not None
        )
        period_trade_count = sum(
            1 for c in new_registrants
            if parse_customer_datetime(c["first_trade_time"]) is not None
        )

        # 2. Referral Code Metrics
        cur.execute("""
            SELECT code, note, clicks, referrals, trade_vol, commission
            FROM referral_metrics
        """)
        code_rows = cur.fetchall()
        codes_list = []
        for r in code_rows:
            codes_list.append({
                "code": r["code"],
                "note": r["note"] or "-",
                "clicks": r["clicks"],
                "referrals": r["referrals"],
                "trade_vol": r["trade_vol"] or "-",
                "commission": r["commission"] or "-",
            })

        # 3. Milestones (e.g. cumulative commission >= 10000 or trade anniversary)
        cur.execute("""
            SELECT user_id, manual_note, platform_note, all_time_commission, first_trade_time
            FROM customers
            WHERE CAST(all_time_commission AS REAL) >= 10000 OR first_trade_time IS NOT NULL
            LIMIT 10
        """)
        milestones = []
        for r in cur.fetchall():
            uid = r["user_id"]
            name = r["platform_note"] or f"客户 {uid[-4:]}"
            comm = float(to_decimal(r["all_time_commission"]) or Decimal("0"))
            if comm >= 10000:
                milestones.append({
                    "user_id": uid,
                    "name": name,
                    "event": "累计返佣破万",
                    "detail": f"已达 {round(comm, 2)} USDT",
                })
            elif r["first_trade_time"]:
                ft_val = r["first_trade_time"]
                ft_str = ""
                if isinstance(ft_val, (int, float)) or (isinstance(ft_val, str) and ft_val.isdigit()):
                    ts = int(ft_val)
                    if ts > 1e11:
                        ts /= 1000
                    ft_str = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
                else:
                    ft_str = str(ft_val)[:10]
                milestones.append({
                    "user_id": uid,
                    "name": name,
                    "event": "交易纪念",
                    "detail": f"首次交易日期：{ft_str}",
                })

        # 4. Care signals: check if we have sufficient daily history
        sig_start, sig_end, _, _ = compute_period_bounds(14, reference_end)
        cur.execute("""
            SELECT COUNT(DISTINCT start_date) FROM customer_metrics
            WHERE is_single_day = 1 AND start_date >= ? AND start_date <= ?
        """, (sig_start, sig_end))
        distinct_days = cur.fetchone()[0]

        has_sufficient_history = distinct_days >= 14

        conn.close()

        return {
            "period": {
                "days": days,
                "current_start": curr_start,
                "current_end": curr_end,
                "curr_range": [curr_start, curr_end],
                "prev_range": [prev_start, prev_end],
                "label": f"{curr_start} ~ {curr_end}",
            },
            "activations": {
                "registered": period_reg_count,
                "kyc": period_kyc_count,
                "deposited": period_dep_count,
                "traded": period_trade_count,
                "trading_customers": trading_customers_count,
                "total_customers": len(all_customers),
                "disclaimer": "注册人数按当前所选周期实时统计；完成 KYC、首次充值与首次交易均统计自本期新客；交易人数展示库内累计具备真实交易行为的客户总盘。",
            },
            "referral_codes": {
                "codes": codes_list,
                "disclaimer": "点击量与注册人数为独立统计指标，点击链接不涵盖移动端 App 手动填码等注册路径，不计算直接点击转化率。",
            },
            "milestones": milestones[:5],
            "signals": {
                "has_sufficient_history": has_sufficient_history,
                "available_days": distinct_days,
                "required_days": 14,
                "notice": "已具备 14 天完整日级时序数据，可生成预警信号。" if has_sufficient_history else f"当前本地日级时序覆盖仅 {distinct_days}/14 天，数据覆盖不足，暂不生成虚假沉默或回归标签，避免误判。",
            },
        }

    def get_dynamics(self) -> Dict[str, Any]:
        """Fetch real main-sub relationships from subaccount_links."""
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT s.parent_user_id, c.manual_note, c.platform_note, COUNT(s.sub_account_id) as sub_count
            FROM subaccount_links s
            LEFT JOIN customers c ON s.parent_user_id = c.user_id
            GROUP BY s.parent_user_id
            ORDER BY sub_count DESC
        """)
        parents = []
        for r in cur.fetchall():
            pid = r[0]
            name = r[2] or f"母账户 {pid[-4:]}"
            cur.execute("SELECT sub_account_id FROM subaccount_links WHERE parent_user_id = ?", (pid,))
            subs = [row[0] for row in cur.fetchall()]
            parents.append({
                "parent_user_id": pid,
                "parent_name": name,
                "subaccount_count": r[3],
                "subaccounts": subs,
            })

        conn.close()

        return {
            "teams": parents,
            "disclaimer": "基于已导入 CSV 维护的明确主子账户绑定关系。底层 API 无法穿透查询子账户，亦不承诺名下全部子账户均已发现。",
            "removed_features_notice": "已彻底剔除原型虚构的秒级协同网络、跨主 UID 协同推断与自动策略画像。",
        }


dashboard_service = DashboardService()
