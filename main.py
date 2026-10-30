"""Binance Rebate Dashboard Backend Service.
Provides REST APIs for overview, customers, followups, insights, dynamics, imports, and sync.
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))

from database import get_db, init_db, metric_is_complete
from dashboard_service import dashboard_service, get_default_reference_end
from sync_service import get_binance_client, sync_manager

# Ensure DB initialized on module load
init_db()

# Safe client loader: do not crash if keys are missing
try:
    client = get_binance_client()
except Exception:
    client = None

app = FastAPI(title="Binance Rebate Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FollowupRequest(BaseModel):
    note: str
    next_followup_date: Optional[str] = None


class SyncStartRequest(BaseModel):
    scope: str = "sample_active"  # "sample_active" (51 UIDs), "single_uid", or "all"
    user_id: Optional[str] = None
    days: int = 15
    reference_end: Optional[str] = None
    granularity: str = "period"  # "period" or "daily"


# ---- Health & Diagnostics ----

@app.get("/health")
def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        total_cust = cur.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        total_metrics = cur.execute("SELECT COUNT(*) FROM customer_metrics").fetchone()[0]
        total_subs = cur.execute("SELECT COUNT(*) FROM subaccount_links").fetchone()[0]
        conn.close()
        return {
            "status": "ok",
            "api_client_available": client is not None,
            "database": {
                "connected": True,
                "total_customers": total_cust,
                "subaccounts": total_subs,
                "cached_metrics": total_metrics,
            },
        }
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


# ---- Dashboard Business Endpoints ----

@app.get("/api/dashboard/overview")
def get_overview(
    days: int = Query(15, description="查询天数"),
    excludeTop: int = Query(0, description="排除前N大客户"),
    referenceEnd: Optional[str] = Query(None, description="数据截止日期 YYYY-MM-DD，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    return dashboard_service.get_overview(days=days, exclude_top=excludeTop, reference_end=ref_date)


@app.get("/api/dashboard/customers")
def get_customers(
    days: int = Query(15, description="期间天数"),
    tab: str = Query("all", description="筛选标签: all, star, decline, silent"),
    search: str = Query("", description="搜索关键词"),
    sort: str = Query("commission", description="排序方式: commission, decline"),
    referenceEnd: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    return dashboard_service.get_customers(days=days, tab=tab, search=search, sort_by=sort, reference_end=ref_date)


@app.get("/api/dashboard/customers/{user_id}")
def get_customer_detail(
    user_id: str,
    days: int = Query(15, description="期间天数"),
    referenceEnd: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    detail = dashboard_service.get_customer_detail(user_id=user_id, days=days, reference_end=ref_date)
    if not detail:
        raise HTTPException(status_code=404, detail="Customer not found")
    return detail


@app.post("/api/dashboard/customers/{user_id}/star")
def toggle_star(user_id: str):
    success = dashboard_service.toggle_star(user_id)
    return {"user_id": user_id, "is_starred": success}


@app.post("/api/dashboard/customers/{user_id}/followup")
def add_followup(user_id: str, payload: FollowupRequest):
    ok = dashboard_service.add_followup(user_id, payload.note, payload.next_followup_date)
    return {"user_id": user_id, "success": ok}


@app.get("/api/dashboard/care")
def get_care(
    days: int = Query(15, description="期间天数"),
    referenceEnd: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    return dashboard_service.get_care_dashboard(days=days, reference_end=ref_date)


@app.get("/api/dashboard/followups")
def get_followups(
    days: int = Query(15, description="期间天数"),
    referenceEnd: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    return dashboard_service.get_care_dashboard(days=days, reference_end=ref_date)


@app.get("/api/dashboard/insights")
def get_insights(
    days: int = Query(15, description="期间天数"),
    referenceEnd: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    ref_date = referenceEnd or get_default_reference_end()
    return dashboard_service.get_insights(days=days, reference_end=ref_date)


@app.get("/api/dashboard/dynamics")
def get_dynamics():
    return dashboard_service.get_dynamics()


# ---- Imports & Sync Jobs ----

@app.get("/api/dashboard/imports")
def list_imports():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM import_batches ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"batches": rows}


@app.post("/api/dashboard/imports/update_csv")
@app.post("/api/dashboard/imports/default")
def update_csv_action(
    auto_sync_3m: bool = Query(True, description="是否自动拉取近 3 个月数据"),
    reference_end: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    """Check/update CSV files, detect newly added customer UIDs (or UIDs lacking 3-month metrics),
    and automatically trigger 3-month data sync.
    """
    from dashboard_service import compute_period_bounds
    ref_date = reference_end if isinstance(reference_end, str) and reference_end.strip() else get_default_reference_end()
    try:
        curr_90_s, curr_90_e, prev_90_s, prev_90_e = compute_period_bounds(90, ref_date)
        curr_30_s, curr_30_e, prev_30_s, prev_30_e = compute_period_bounds(30, ref_date)
        curr_15_s, curr_15_e, prev_15_s, prev_15_e = compute_period_bounds(15, ref_date)
        curr_7_s, curr_7_e, prev_7_s, prev_7_e = compute_period_bounds(7, ref_date)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"无效的截止日期参数 reference_end='{ref_date}': {e}")

    from importer import CsvValidationError, update_csv_and_detect_new_uids
    try:
        res = update_csv_and_detect_new_uids(directory=ROOT_DIR)
    except CsvValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描更新 CSV 失败: {e}")

    new_uids = res.get("new_uids", [])
    total_uids = res.get("total_uids", 0)

    # Find UIDs that need 3-month/30-day sync (all new UIDs + any customer lacking 90d or 30d metrics)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM customer_metrics WHERE start_date = ? AND end_date = ? AND status IN ('success', 'empty_zero')", (curr_90_s, curr_90_e))
        synced_90d = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT user_id FROM customer_metrics WHERE start_date = ? AND end_date = ? AND status IN ('success', 'empty_zero')", (curr_30_s, curr_30_e))
        synced_30d = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT user_id FROM customers")
        all_custs = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    # Priority: new UIDs always synced; plus any existing UIDs without 90d or 30d data
    uids_to_sync = list(dict.fromkeys(new_uids + [u for u in all_custs if u not in synced_90d or u not in synced_30d]))

    auto_job_id = None
    cli = None
    if auto_sync_3m and uids_to_sync:
        cli = get_binance_client()
        if cli:
            dates = [
                (curr_90_s, curr_90_e, False),
                (prev_90_s, prev_90_e, False),
                (curr_30_s, curr_30_e, False),
                (prev_30_s, prev_30_e, False),
                (curr_15_s, curr_15_e, False),
                (prev_15_s, prev_15_e, False),
                (curr_7_s, curr_7_e, False),
                (prev_7_s, prev_7_e, False),
            ]
            auto_job_id = f"auto-3m-{dt.datetime.now():%Y%m%d%H%M%S%f}"
            t = threading.Thread(
                target=sync_manager.run_backfill_job,
                args=(auto_job_id, uids_to_sync, dates),
                kwargs={"skip_existing": True},
                daemon=True,
            )
            t.start()

    if auto_job_id:
        if new_uids:
            msg = f"更新成功！发现 {len(new_uids)} 位新增客户，已自动启动近 3 个月与近 30 天数据拉取落档 (任务ID: {auto_job_id})。"
        else:
            msg = f"更新完成：共扫描 {total_uids} 位客户，自动为 {len(uids_to_sync)} 位客户补齐近 3 个月与近 30 天数据 (任务ID: {auto_job_id})。"
    else:
        if new_uids:
            msg = f"更新成功！发现 {len(new_uids)} 位新增客户，客户档案已入库。（若需拉取交易额与佣金指标，请在 .env 配置只读 API Key）"
        elif uids_to_sync and not cli:
            msg = f"扫描完成：共 {total_uids} 位客户档案。（提示：未配置 .env API 凭证，暂未启动 SAPI 数据回补）"
        else:
            msg = f"更新完成：共扫描 {total_uids} 位客户，客户档案已是最新，近 3 个月与近 30 天数据已全部落档。"

    return {
        "status": "success",
        "csv": res,
        "new_uids": new_uids,
        "new_uids_count": len(new_uids),
        "synced_uids_count": len(uids_to_sync),
        "auto_sync_job_id": auto_job_id,
        "message": msg,
    }


@app.post("/api/dashboard/imports/upload")
async def upload_csv_file(
    file: UploadFile = File(...),
    reference_end: Optional[str] = Query(None, description="截止日期，默认 UTC 昨日"),
):
    """Upload a new rebate CSV export, safely stage & validate it before saving
    to data/ directory, and trigger incremental UID detection & synchronization.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported")

    # 1. Validate reference_end and compute period bounds BEFORE any file or DB modifications
    from dashboard_service import compute_period_bounds
    ref_date = reference_end if isinstance(reference_end, str) and reference_end.strip() else get_default_reference_end()
    try:
        curr_90_s, curr_90_e, prev_90_s, prev_90_e = compute_period_bounds(90, ref_date)
        curr_30_s, curr_30_e, prev_30_s, prev_30_e = compute_period_bounds(30, ref_date)
        curr_15_s, curr_15_e, prev_15_s, prev_15_e = compute_period_bounds(15, ref_date)
        curr_7_s, curr_7_e, prev_7_s, prev_7_e = compute_period_bounds(7, ref_date)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"无效的截止日期参数 reference_end='{ref_date}': {e}")

    save_dir = os.path.abspath(os.path.join(ROOT_DIR, "data"))
    os.makedirs(save_dir, exist_ok=True)
    safe_name = os.path.basename(file.filename)
    dest_path = os.path.abspath(os.path.join(save_dir, safe_name))

    # 2. Create hidden, unique staging file in target data/ directory and write full content
    staged_fd = None
    staged_path = None
    try:
        contents = await file.read()
        staged_fd, staged_path = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=save_dir)
        staged_path = os.path.abspath(staged_path)
        try:
            with os.fdopen(staged_fd, "wb") as f:
                staged_fd = None
                f.write(contents)
                f.flush()
        finally:
            if staged_fd is not None:
                try:
                    os.close(staged_fd)
                except OSError:
                    pass
    except Exception as e:
        if staged_path and os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"写入上传暂存文件失败: {e}")

    from importer import CsvValidationError, validate_csv_file, update_csv_and_detect_new_uids

    conn = None
    backup_fd = None
    backup_path = None
    had_existing_dest = os.path.exists(dest_path)
    replaced_dest = False
    committed = False
    keep_backup_for_recovery = False

    try:
        # 3. Validate staged file format before opening DB transaction
        validate_csv_file(staged_path, display_name=safe_name)

        # 4 & 5. Open caller-managed SQLite transaction and execute importer SQL without committing
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        res = update_csv_and_detect_new_uids(
            directory=ROOT_DIR,
            staged_file=staged_path,
            staged_target_path=dest_path,
            staged_target_name=safe_name,
            conn=conn,
        )

        # 6. If target already exists, create a hidden backup in the same directory before replacing
        if had_existing_dest:
            backup_fd, backup_path = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=save_dir)
            backup_path = os.path.abspath(backup_path)
            os.close(backup_fd)
            backup_fd = None
            shutil.copyfile(dest_path, backup_path)

        # 7. Atomically replace target in same directory, then commit SQLite transaction
        os.replace(staged_path, dest_path)
        replaced_dest = True
        conn.commit()
        committed = True

        # 8. Only after commit succeeds, remove old backup (cleanup failure does not invalidate committed import)
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError as cleanup_err:
                print(f"[upload_csv_file] Warning: failed to remove backup {backup_path} after commit: {cleanup_err}")

    except CsvValidationError as e:
        if conn is not None and not committed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        if conn is not None and not committed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception as e:
        if conn is not None and not committed:
            try:
                conn.rollback()
            except Exception:
                pass

        if replaced_dest and not committed:
            if had_existing_dest and backup_path:
                try:
                    os.replace(backup_path, dest_path)
                except Exception as restore_err:
                    keep_backup_for_recovery = True
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            f"数据库提交失败且自动恢复原文件失败，保留同目录备份文件 "
                            f"{os.path.basename(backup_path)} 以供恢复 {safe_name}: "
                            f"commit_error={e}; restore_error={restore_err}"
                        ),
                    )
            elif not had_existing_dest:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception as cleanup_err:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            f"数据库提交失败且移除新建目标文件 {safe_name} 失败，请手动清理: "
                            f"commit_error={e}; cleanup_error={cleanup_err}"
                        ),
                    )
        raise HTTPException(status_code=500, detail=f"导入保存失败，已回滚更改: {e}")
    finally:
        if backup_fd is not None:
            try:
                os.close(backup_fd)
            except OSError:
                pass
        if staged_path and os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except OSError:
                pass
        if backup_path and not committed and not keep_backup_for_recovery and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    new_uids = res.get("new_uids", [])
    total_uids = res.get("total_uids", 0)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM customer_metrics WHERE start_date = ? AND end_date = ? AND status IN ('success', 'empty_zero')", (curr_90_s, curr_90_e))
        synced_90d = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT user_id FROM customers")
        all_custs = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    uids_to_sync = list(dict.fromkeys(new_uids + [u for u in all_custs if u not in synced_90d]))

    auto_job_id = None
    cli = get_binance_client()
    if cli and uids_to_sync:
        dates = [
            (curr_90_s, curr_90_e, False),
            (prev_90_s, prev_90_e, False),
            (curr_30_s, curr_30_e, False),
            (prev_30_s, prev_30_e, False),
            (curr_15_s, curr_15_e, False),
            (prev_15_s, prev_15_e, False),
            (curr_7_s, curr_7_e, False),
            (prev_7_s, prev_7_e, False),
        ]
        auto_job_id = f"auto-upload-{dt.datetime.now():%Y%m%d%H%M%S%f}"
        t = threading.Thread(
            target=sync_manager.run_backfill_job,
            args=(auto_job_id, uids_to_sync, dates),
            kwargs={"skip_existing": True},
            daemon=True,
        )
        t.start()

    if auto_job_id:
        msg = f"上传成功！已解析 {safe_name}：覆盖 {total_uids} 个 UID（新增 {len(new_uids)} 人），已自动启动数据同步 (任务ID: {auto_job_id})。"
    else:
        msg = f"上传成功！已解析 {safe_name}：覆盖 {total_uids} 个 UID（新增 {len(new_uids)} 人）。（若需拉取交易额与佣金指标，请在 .env 配置只读 API Key）"
    return {
        "status": "success",
        "file_name": safe_name,
        "message": msg,
        "new_uids": new_uids,
        "new_uids_count": len(new_uids),
        "total_uids": total_uids,
        "auto_sync_job_id": auto_job_id,
    }


@app.get("/api/dashboard/sync/status")
def get_sync_status(job_id: Optional[str] = None):
    conn = get_db()
    cur = conn.cursor()

    # Reconcile stale/orphan running jobs (e.g. terminated by hot reload or restart)
    cur.execute("SELECT id FROM sync_jobs WHERE status = 'running'")
    orphan_ids = [r[0] for r in cur.fetchall() if not sync_manager.is_job_active(r[0])]
    if orphan_ids:
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        for oid in orphan_ids:
            cur.execute("""
                UPDATE sync_jobs
                SET status = 'interrupted',
                    message = '服务重载导致任务中断，已自动复位，可重新点击同步。',
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
            """, (now_iso, now_iso, oid))
        conn.commit()

    cur.execute("SELECT * FROM sync_jobs ORDER BY rowid DESC LIMIT 5")
    jobs = [dict(r) for r in cur.fetchall()]
    conn.close()
    if job_id:
        conn = get_db()
        row = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        return {"latest_job": dict(row) if row else None}
    latest_job = jobs[0] if jobs else None
    return {"latest_job": latest_job, "recent_jobs": jobs}


@app.post("/api/dashboard/sync/start")
def start_sync_job(payload: SyncStartRequest):
    """Start an asynchronous rate-limited backfill or sync job."""
    cli = get_binance_client()
    if not cli:
        raise HTTPException(status_code=400, detail="API credentials (BK_KEY/BK_SEC) not found in environment or .env")

    conn = get_db()
    cur = conn.cursor()

    uids = []
    if payload.scope == "single_uid" and payload.user_id:
        uids = [payload.user_id]
    elif payload.scope == "sample_active":
        # First 51 active UIDs identified from CSV
        cur.execute("""
            SELECT DISTINCT parent_user_id FROM subaccount_links
            UNION
            SELECT user_id FROM customers WHERE platform_note IS NOT NULL
        """)
        uids = [r[0] for r in cur.fetchall()]
        if not uids:
            cur.execute("SELECT user_id FROM customers LIMIT 51")
            uids = [r[0] for r in cur.fetchall()]
    else:
        # All customers
        cur.execute("SELECT user_id FROM customers")
        uids = [r[0] for r in cur.fetchall()]

    conn.close()

    if not uids:
        raise HTTPException(status_code=400, detail="No customer UIDs available to sync")

    # Determine date intervals to sync
    from dashboard_service import compute_period_bounds, parse_date, format_date
    curr_start, curr_end, prev_start, prev_end = compute_period_bounds(payload.days, payload.reference_end)

    dates = []
    if payload.granularity == "daily":
        # Backfill daily slices for current period
        start_d = parse_date(curr_start)
        for i in range(payload.days):
            d_str = format_date(start_d + dt.timedelta(days=i))
            dates.append((d_str, d_str, True))
    else:
        # Sync period aggregates: current period and previous period
        dates.append((curr_start, curr_end, False))
        dates.append((prev_start, prev_end, False))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM customer_metrics
        WHERE source = 'sapi' AND status IN ('success', 'empty_zero')
    """)
    existing_set = {(r["user_id"], r["start_date"], r["end_date"], bool(r["is_single_day"])) for r in cur.fetchall() if metric_is_complete(r)}
    conn.close()

    total_ops = len(uids) * len(dates)
    cached_ops = sum(1 for u in uids for s, e, is_s in dates if (u, s, e, bool(is_s)) in existing_set)
    to_fetch_ops = total_ops - cached_ops

    job_id = f"job-{dt.datetime.now():%Y%m%d%H%M%S%f}"

    # Launch background thread with skip_existing verification
    t = threading.Thread(
        target=sync_manager.run_backfill_job,
        args=(job_id, uids, dates),
        kwargs={"skip_existing": True},
        daemon=True,
    )
    t.start()

    if to_fetch_ops == 0:
        msg = f"已完成数据库校验：所选 {len(dates)} 天全部 {total_ops} 项数据已在本地在库，无需重复请求。"
    elif cached_ops > 0:
        msg = f"校验完成：已在库 {cached_ops} 项秒级跳过，已启动拉取剩余缺失的 {to_fetch_ops} 项数据 (任务ID: {job_id})。"
    else:
        msg = f"已启动全量 {total_ops} 项数据拉取落档 (任务ID: {job_id})。"

    return {
        "job_id": job_id,
        "status": "started" if to_fetch_ops > 0 else "already_complete",
        "uids_count": len(uids),
        "dates_count": len(dates),
        "total_operations": total_ops,
        "cached_operations": cached_ops,
        "to_fetch_operations": to_fetch_ops,
        "granularity": payload.granularity,
        "message": msg,
    }


@app.post("/api/dashboard/sync/cancel")
def cancel_sync_job(job_id: str = Query(..., description="Job ID to cancel")):
    sync_manager.request_cancel(job_id)
    return {"job_id": job_id, "status": "cancel_requested"}


# ---- Raw SAPI Debugging Proxies (Preserved) ----

def proxy(resp: requests.Response) -> JSONResponse:
    try:
        body = resp.json()
    except ValueError:
        body = {"code": -1, "message": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=body)


@app.get("/api/invitee-performance")
def invitee_performance(
    userId: str = Query(..., description="被邀请人的用户 ID"),
    startDate: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
    endDate: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD"),
):
    cli = get_binance_client()
    if not cli:
        return JSONResponse(status_code=500, content={"error": "API credentials missing"})
    resp = cli.invitee_performance(userId, startDate, endDate)
    return proxy(resp)


@app.get("/api/code-performance")
def code_performance(
    code: str = Query(..., description="推荐码"),
    startDate: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
    endDate: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD"),
):
    cli = get_binance_client()
    if not cli:
        return JSONResponse(status_code=500, content={"error": "API credentials missing"})
    resp = cli.code_performance(code, startDate, endDate)
    return proxy(resp)


# ---- Static Frontend Mounting ----

dashboard_dir = os.path.join(ROOT_DIR, "dashboard")
if os.path.exists(dashboard_dir):
    app.mount("/dashboard", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")


@app.get("/")
def root_redirect():
    return RedirectResponse(url="/dashboard/")
