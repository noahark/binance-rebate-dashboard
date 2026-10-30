"""Importer for Binance rebate CSV exports.
Handles streaming parsing, deduplication, safe note merges, and subaccount links.
"""

import csv
import datetime as dt
import hashlib
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from database import get_db, ROOT_DIR


def compute_file_sha256(filepath: str) -> str:
    """Compute SHA-256 hash of a file for idempotent import tracking."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class CsvValidationError(ValueError):
    """Raised when a CSV file fails required header, encoding, or non-empty UID validation."""
    pass


def validate_csv_file(filepath: str, display_name: Optional[str] = None) -> None:
    """Validate a CSV file: must have '好友ID（现货）' header and at least 1 valid UID row.
    Raises CsvValidationError (a ValueError subclass) on failure.
    """
    name = display_name or os.path.basename(filepath)
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "好友ID（现货）" not in reader.fieldnames:
                raise CsvValidationError(f"CSV 文件「{name}」缺少必需列「好友ID（现货）」，无法识别客户 UID。")
            has_uid = False
            for row in reader:
                if row.get("好友ID（现货）", "").strip():
                    has_uid = True
                    break
            if not has_uid:
                raise CsvValidationError(f"CSV 文件「{name}」未包含任何有效客户 UID 数据行（有效客户数为 0）。")
    except UnicodeDecodeError:
        raise CsvValidationError(f"CSV 文件「{name}」编码格式不正确，请确保为 UTF-8 编码。")


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
    try:
        cur = conn.cursor()

        # Check if already imported
        cur.execute("SELECT * FROM import_batches WHERE file_sha256 = ?", (file_hash,))
        existing = cur.fetchone()
        if existing:
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
            batch_id = f"batch-csv-{dt.datetime.now():%Y%m%d%H%M%S%f}"

        # Streaming read
        total_rows = 0
        unique_uids: Set[str] = set()
        parent_sub_pairs: Set[Tuple[str, str]] = set()
        uid_metadata: Dict[str, Dict[str, Any]] = {}

        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or "好友ID（现货）" not in reader.fieldnames:
                    raise CsvValidationError(f"CSV 文件「{filename}」缺少必需列「好友ID（现货）」，无法识别客户 UID。")

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
        except UnicodeDecodeError:
            raise CsvValidationError(f"CSV 文件「{filename}」编码格式不正确，请确保为 UTF-8 编码。")

        if not unique_uids:
            raise CsvValidationError(f"CSV 文件「{filename}」未包含任何有效客户 UID 数据行（有效客户数为 0）。")

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
            cur.execute("""
                INSERT INTO subaccount_links (parent_user_id, sub_account_id, source_batch_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sub_account_id) DO UPDATE SET parent_user_id = excluded.parent_user_id
            """, (parent_uid, sub_uid, batch_id, now_iso))
            new_subs_count += 1

        # Save batch record
        summary_text = f"Imported {total_rows} rows from {filename}. Found {len(unique_uids)} UIDs ({new_uids_count} new, {updated_uids_count} updated), {len(parent_sub_pairs)} subaccounts."
        cur.execute("""
            INSERT INTO import_batches (
                batch_id, file_name, file_sha256, source_type,
                total_rows, unique_uids, subaccount_count, imported_at, summary
            ) VALUES (?, ?, ?, 'csv', ?, ?, ?, ?, ?)
        """, (batch_id, filename, file_hash, total_rows, len(unique_uids), len(parent_sub_pairs), now_iso, summary_text))

        conn.commit()

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
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def find_all_csv_files(directory: str = ROOT_DIR) -> List[str]:
    """Find all relevant CSV export files in the directory and data subdirectory."""
    csvs = []
    target_dirs = [directory]
    data_dir = os.path.join(directory, "data")
    if os.path.isdir(data_dir):
        target_dirs.append(data_dir)

    seen_paths = set()
    for d in target_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(".csv") and not f.startswith("."):
                full_path = os.path.abspath(os.path.join(d, f))
                if full_path not in seen_paths:
                    seen_paths.add(full_path)
                    csvs.append(full_path)
    # Sort by modification time, newest first
    csvs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return csvs


def update_csv_and_detect_new_uids(
    directory: str = ROOT_DIR,
    staged_file: Optional[str] = None,
    staged_target_path: Optional[str] = None,
    staged_target_name: Optional[str] = None,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Scans for CSV export files, syncs customer basic profiles,
    and returns newly detected customer UIDs.
    If staged_file is provided, staged_file replaces the exact target CSV file
    (matched by absolute path) or supplements the scan list during processing
    without modifying the target on disk until caller confirms success.
    If conn is provided, caller owns transaction management (commit/rollback/close).
    """
    csv_files = find_all_csv_files(directory)
    target_abs = None
    if staged_target_path:
        target_abs = os.path.abspath(staged_target_path)
    elif staged_target_name:
        target_abs = os.path.abspath(os.path.join(directory, "data", staged_target_name))

    if staged_file and target_abs:
        staged_abs = os.path.abspath(staged_file)
        replaced = False
        new_csv_files = []
        for p in csv_files:
            if os.path.abspath(p) == target_abs:
                new_csv_files.append(staged_abs)
                replaced = True
            else:
                new_csv_files.append(p)
        if not replaced:
            new_csv_files.insert(0, staged_abs)
        csv_files = new_csv_files

    if not csv_files:
        return {
            "status": "no_csv_found",
            "message": "未在工作目录中找到 CSV 文件",
            "new_uids": [],
            "new_uids_count": 0,
            "total_uids": 0,
        }

    external_conn = conn is not None
    db_conn = conn if external_conn else get_db()
    try:
        cur = db_conn.cursor()

        # Get existing UIDs currently in customers table
        cur.execute("SELECT user_id FROM customers")
        existing_uids = {r[0] for r in cur.fetchall()}

        all_found_uids: Set[str] = set()
        parent_sub_pairs: Set[Tuple[str, str]] = set()
        uid_metadata: Dict[str, Dict[str, Any]] = {}
        total_rows = 0

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        processed_filenames = []
        display_target_name = staged_target_name or (os.path.basename(target_abs) if target_abs else None)
        staged_abs = os.path.abspath(staged_file) if staged_file else None

        # Read all CSV files (newest first, so newer metadata takes precedence)
        for csv_path in csv_files:
            if staged_abs and os.path.abspath(csv_path) == staged_abs:
                filename = display_target_name or os.path.basename(csv_path)
            else:
                filename = os.path.basename(csv_path)
            try:
                with open(csv_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames or "好友ID（现货）" not in reader.fieldnames:
                        raise CsvValidationError(f"CSV 文件「{filename}」缺少必需列「好友ID（现货）」，无法识别客户 UID。")

                    file_uids_found = 0
                    for row in reader:
                        total_rows += 1
                        raw_uid = row.get("好友ID（现货）", "").strip()
                        if not raw_uid:
                            continue
                        file_uids_found += 1
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

                    if file_uids_found == 0:
                        raise CsvValidationError(f"CSV 文件「{filename}」未包含任何有效客户 UID 数据行（有效客户数为 0）。")
            except UnicodeDecodeError:
                raise CsvValidationError(f"CSV 文件「{filename}」编码格式不正确，请确保为 UTF-8 编码。")

            processed_filenames.append(filename)

        if not all_found_uids:
            raise CsvValidationError("扫描到的 CSV 文件中未包含任何有效客户 UID，未执行任何写入。")

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
            cur.execute("""
                INSERT INTO subaccount_links (parent_user_id, sub_account_id, source_batch_id, created_at)
                VALUES (?, ?, 'update-csv', ?)
                ON CONFLICT(sub_account_id) DO UPDATE SET parent_user_id = excluded.parent_user_id
            """, (parent_uid, sub_uid, now_iso))

        # Record import batch
        batch_id = f"batch-update-{dt.datetime.now():%Y%m%d%H%M%S%f}"
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

        if not external_conn:
            db_conn.commit()

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
    except Exception:
        if not external_conn:
            try:
                db_conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if not external_conn:
            try:
                db_conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    from database import init_db
    init_db()

    # Test updating CSV
    res_update = update_csv_and_detect_new_uids()
    print("CSV update result:", res_update)

