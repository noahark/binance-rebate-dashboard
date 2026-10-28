"""Importer for Binance CSV exports and BAPI JSON snapshots.
Handles streaming parsing, deduplication, safe note merges, and subaccount links.
"""

import csv
import datetime as dt
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from database import format_decimal, get_db, ROOT_DIR


def compute_file_sha256(filepath: str) -> str:
    """Compute SHA-256 hash of a file for idempotent import tracking."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def import_csv_file(filepath: str, batch_id: Optional[str] = None) -> Dict[str, Any]:
    """Stream-import Binance affiliate CSV file.
    Extracts UID, referral codes, notes, and subaccounts.
    Does NOT use CSV amounts to overwrite API data.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    file_hash = compute_file_sha256(filepath)
    filename = os.path.basename(filepath)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    conn = get_db()
    cur = conn.cursor()

    # Check if already imported
    cur.execute("SELECT * FROM import_batches WHERE file_sha256 = ?", (file_hash,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return {
            "batch_id": existing["batch_id"],
            "status": "already_imported",
            "message": f"File already imported on {existing['imported_at']}",
            "file_name": filename,
            "total_rows": existing["total_rows"],
            "unique_uids": existing["unique_uids"],
            "subaccount_count": existing["subaccount_count"],
        }

    if not batch_id:
        batch_id = f"batch-csv-{dt.datetime.now():%Y%m%d%H%M%S}"

    # Streaming read
    total_rows = 0
    unique_uids: Set[str] = set()
    parent_sub_pairs: Set[Tuple[str, str]] = set()
    uid_metadata: Dict[str, Dict[str, Any]] = {}

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            raw_uid = row.get("好友ID（现货）", "").strip()
            if not raw_uid:
                continue
            unique_uids.add(raw_uid)

            # Subaccount
            raw_sub = row.get("好友子账户ID（现货）", "").strip()
            if raw_sub and raw_sub != raw_uid:
                parent_sub_pairs.add((raw_uid, raw_sub))

            # Metadata extraction
            if raw_uid not in uid_metadata:
                code = row.get("推荐码", "").strip() or None
                note = row.get("referee_note", "").strip() or row.get("附言", "").strip() or None
                reg_str = row.get("注册时间", "").strip()
                reg_ts = None
                if reg_str:
                    try:
                        reg_ts = int(dt.datetime.strptime(reg_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                    except Exception:
                        pass
                uid_metadata[raw_uid] = {
                    "code": code,
                    "note": note,
                    "reg_time": reg_ts,
                }
            else:
                # Update non-empty metadata if previous was empty
                if not uid_metadata[raw_uid]["code"] and row.get("推荐码", "").strip():
                    uid_metadata[raw_uid]["code"] = row.get("推荐码", "").strip()
                row_note = row.get("referee_note", "").strip() or row.get("附言", "").strip()
                if not uid_metadata[raw_uid]["note"] and row_note:
                    uid_metadata[raw_uid]["note"] = row_note

    # Batch insert/update customers in DB
    new_uids_count = 0
    updated_uids_count = 0

    for uid, meta in uid_metadata.items():
        cur.execute("SELECT user_id, platform_note, referral_code, reg_time FROM customers WHERE user_id = ?", (uid,))
        existing_cust = cur.fetchone()
        if existing_cust:
            # Preserve existing non-empty platform_note
            plat_note = existing_cust["platform_note"] or meta["note"]
            ref_code = existing_cust["referral_code"] or meta["code"]
            reg_time = existing_cust["reg_time"] or meta["reg_time"]
            cur.execute("""
                UPDATE customers
                SET platform_note = ?, referral_code = ?, reg_time = ?, updated_at = ?
                WHERE user_id = ?
            """, (plat_note, ref_code, reg_time, now_iso, uid))
            updated_uids_count += 1
        else:
            cur.execute("""
                INSERT INTO customers (
                    user_id, referral_code, platform_note, manual_note, tags,
                    vip_level, reg_time, is_starred, created_at, updated_at
                ) VALUES (?, ?, ?, '', '[]', 0, ?, 0, ?, ?)
            """, (uid, meta["code"], meta["note"], meta["reg_time"], now_iso, now_iso))
            new_uids_count += 1

    # Insert subaccounts
    new_subs_count = 0
    for parent_uid, sub_uid in parent_sub_pairs:
        # First ensure parent customer exists
        cur.execute("INSERT OR IGNORE INTO customers (user_id, is_starred, created_at, updated_at) VALUES (?, 0, ?, ?)",
                    (parent_uid, now_iso, now_iso))
        try:
            cur.execute("""
                INSERT INTO subaccount_links (parent_user_id, sub_account_id, source_batch_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sub_account_id) DO UPDATE SET parent_user_id = excluded.parent_user_id
            """, (parent_uid, sub_uid, batch_id, now_iso))
            new_subs_count += 1
        except Exception:
            pass

    # Save batch record
    summary_text = f"Imported {total_rows} rows from {filename}. Found {len(unique_uids)} UIDs ({new_uids_count} new, {updated_uids_count} updated), {len(parent_sub_pairs)} subaccounts."
    cur.execute("""
        INSERT INTO import_batches (
            batch_id, file_name, file_sha256, source_type,
            total_rows, unique_uids, subaccount_count, imported_at, summary
        ) VALUES (?, ?, ?, 'csv', ?, ?, ?, ?, ?)
    """, (batch_id, filename, file_hash, total_rows, len(unique_uids), len(parent_sub_pairs), now_iso, summary_text))

    conn.commit()
    conn.close()

    return {
        "batch_id": batch_id,
        "status": "success",
        "file_name": filename,
        "total_rows": total_rows,
        "unique_uids": len(unique_uids),
        "new_uids": new_uids_count,
        "updated_uids": updated_uids_count,
        "subaccount_count": len(parent_sub_pairs),
        "imported_at": now_iso,
    }


def find_all_csv_files(directory: str = ROOT_DIR) -> List[str]:
    """Find all relevant CSV export files in the directory and data subdirectory."""
    csvs = []
    target_dirs = [directory]
    data_dir = os.path.join(directory, "data")
    if os.path.isdir(data_dir):
        target_dirs.append(data_dir)

    seen_paths = set()
    for d in target_dirs:
        for f in os.listdir(d):
            if f.lower().endswith(".csv") and not f.startswith("."):
                full_path = os.path.abspath(os.path.join(d, f))
                if full_path not in seen_paths:
                    seen_paths.add(full_path)
                    csvs.append(full_path)
    # Sort by modification time, newest first
    csvs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return csvs


def update_csv_and_detect_new_uids(directory: str = ROOT_DIR) -> Dict[str, Any]:
    """Scans for CSV export files, syncs customer basic profiles,
    and returns newly detected customer UIDs.
    """
    csv_files = find_all_csv_files(directory)
    if not csv_files:
        return {
            "status": "no_csv_found",
            "message": "未在工作目录中找到 CSV 文件",
            "new_uids": [],
            "new_uids_count": 0,
            "total_uids": 0,
        }

    conn = get_db()
    cur = conn.cursor()

    # Get existing UIDs currently in customers table
    cur.execute("SELECT user_id FROM customers")
    existing_uids = {r[0] for r in cur.fetchall()}

    all_found_uids: Set[str] = set()
    parent_sub_pairs: Set[Tuple[str, str]] = set()
    uid_metadata: Dict[str, Dict[str, Any]] = {}
    total_rows = 0

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    processed_filenames = []

    # Read all CSV files (newest first, so newer metadata takes precedence)
    for csv_path in csv_files:
        filename = os.path.basename(csv_path)
        processed_filenames.append(filename)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1
                raw_uid = row.get("好友ID（现货）", "").strip()
                if not raw_uid:
                    continue
                all_found_uids.add(raw_uid)

                # Subaccount
                raw_sub = row.get("好友子账户ID（现货）", "").strip()
                if raw_sub and raw_sub != raw_uid:
                    parent_sub_pairs.add((raw_uid, raw_sub))

                # Metadata
                code = row.get("推荐码", "").strip() or None
                note = row.get("referee_note", "").strip() or row.get("附言", "").strip() or None
                reg_str = row.get("注册时间", "").strip()
                reg_ts = None
                if reg_str:
                    try:
                        reg_ts = int(dt.datetime.strptime(reg_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                    except Exception:
                        pass

                if raw_uid not in uid_metadata:
                    uid_metadata[raw_uid] = {
                        "code": code,
                        "note": note,
                        "reg_time": reg_ts,
                    }
                else:
                    if not uid_metadata[raw_uid]["code"] and code:
                        uid_metadata[raw_uid]["code"] = code
                    if not uid_metadata[raw_uid]["note"] and note:
                        uid_metadata[raw_uid]["note"] = note
                    if not uid_metadata[raw_uid]["reg_time"] and reg_ts:
                        uid_metadata[raw_uid]["reg_time"] = reg_ts

    # Identify new UIDs
    new_uids = [u for u in all_found_uids if u not in existing_uids]
    updated_uids = [u for u in all_found_uids if u in existing_uids]

    # Insert new UIDs into customers
    for uid in new_uids:
        meta = uid_metadata.get(uid, {})
        cur.execute("""
            INSERT INTO customers (
                user_id, referral_code, platform_note, manual_note, tags,
                vip_level, reg_time, is_starred, created_at, updated_at
            ) VALUES (?, ?, ?, '', '[]', 0, ?, 0, ?, ?)
        """, (uid, meta.get("code"), meta.get("note"), meta.get("reg_time"), now_iso, now_iso))

    # Update existing UIDs (preserve existing non-empty notes)
    for uid in updated_uids:
        meta = uid_metadata.get(uid, {})
        cur.execute("SELECT platform_note, referral_code, reg_time FROM customers WHERE user_id = ?", (uid,))
        r = cur.fetchone()
        plat_note = (r["platform_note"] if r and r["platform_note"] else meta.get("note"))
        ref_code = (r["referral_code"] if r and r["referral_code"] else meta.get("code"))
        reg_time = (r["reg_time"] if r and r["reg_time"] else meta.get("reg_time"))
        cur.execute("""
            UPDATE customers
            SET platform_note = ?, referral_code = ?, reg_time = ?, updated_at = ?
            WHERE user_id = ?
        """, (plat_note, ref_code, reg_time, now_iso, uid))

    # Subaccounts upsert
    for parent_uid, sub_uid in parent_sub_pairs:
        cur.execute("INSERT OR IGNORE INTO customers (user_id, is_starred, created_at, updated_at) VALUES (?, 0, ?, ?)",
                    (parent_uid, now_iso, now_iso))
        try:
            cur.execute("""
                INSERT INTO subaccount_links (parent_user_id, sub_account_id, source_batch_id, created_at)
                VALUES (?, ?, 'update-csv', ?)
                ON CONFLICT(sub_account_id) DO UPDATE SET parent_user_id = excluded.parent_user_id
            """, (parent_uid, sub_uid, now_iso))
        except Exception:
            pass

    # Record import batch
    batch_id = f"batch-update-{dt.datetime.now():%Y%m%d%H%M%S}"
    summary_text = (
        f"更新 CSV 完成：解析 {total_rows} 行数据，覆盖 {len(all_found_uids)} 个 UID "
        f"(新增 {len(new_uids)} 人，已建档 {len(updated_uids)} 人)，绑定 {len(parent_sub_pairs)} 个子账户。"
    )
    cur.execute("""
        INSERT INTO import_batches (
            batch_id, file_name, file_sha256, source_type,
            total_rows, unique_uids, subaccount_count, imported_at, summary
        ) VALUES (?, ?, 'dynamic-update', 'csv', ?, ?, ?, ?, ?)
    """, (batch_id, ", ".join(processed_filenames), total_rows, len(all_found_uids), len(parent_sub_pairs), now_iso, summary_text))

    conn.commit()
    conn.close()

    return {
        "batch_id": batch_id,
        "status": "success",
        "files_processed": processed_filenames,
        "total_rows": total_rows,
        "total_uids": len(all_found_uids),
        "existing_uids_count": len(updated_uids),
        "new_uids": new_uids,
        "new_uids_count": len(new_uids),
        "subaccount_count": len(parent_sub_pairs),
        "summary": summary_text,
    }


if __name__ == "__main__":
    from database import init_db
    init_db()

    # Test updating CSV
    res_update = update_csv_and_detect_new_uids()
    print("CSV update result:", res_update)

