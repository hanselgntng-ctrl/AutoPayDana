"""
stats_broadcast.py
===================
Modul tambahan untuk fitur 📊 Statistik & 📢 Broadcast di /settings.

Query langsung ke database SQLite yang sama dipakai database.py (default:
<DATA_DIR>/bot.db, mengikuti config.DB_PATH kalau ada, atau fallback ke
config.DATA_DIR/bot.db).

PENTING: karena database.py tidak ikut di-upload saat modul ini dibuat, semua
query di sini DEFENSIF — kalau nama tabel/kolom di instalasi kamu ternyata
berbeda dari asumsi di bawah, fungsi akan mengembalikan nilai default (0 /
list kosong) alih-alih membuat bot crash. Kalau angka statistik terlihat 0
semua padahal seharusnya ada data, kirim isi database.py ke Claude supaya
query di bawah bisa disesuaikan persis dengan skema aslinya.

Asumsi skema (sesuai desain awal proyek ini):
- transactions(id, user_id, username, package_id, expected_amount, unique_code,
                status, reject_reason, proof_file_id, ocr_amount, ocr_raw_text,
                image_hash, created_at)
- vip_users(user_id, username, package_id, expiry_date)
- packages(id, name, price, duration_days, description, link, target_chat_id, active)
- user_strikes(user_id, count)
"""

import os
import sqlite3
import datetime

import config

DB_PATH = getattr(config, "DB_PATH", None) or os.path.join(
    getattr(config, "DATA_DIR", "data"), "bot.db"
)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_scalar(cur, sql, params=(), default=0):
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        val = row[0] if row else None
        return val if val is not None else default
    except sqlite3.OperationalError:
        return default


def get_stats() -> dict:
    """Kumpulkan angka statistik utama bot. Aman dipanggil walau ada tabel yang
    belum ada di skema (defaultnya 0, tidak melempar error ke user)."""
    conn = _connect()
    cur = conn.cursor()

    total_tx = _safe_scalar(cur, "SELECT COUNT(*) FROM transactions")
    approved_tx = _safe_scalar(cur, "SELECT COUNT(*) FROM transactions WHERE status='approved'")
    rejected_tx = _safe_scalar(cur, "SELECT COUNT(*) FROM transactions WHERE status='rejected'")
    pending_tx = _safe_scalar(cur, "SELECT COUNT(*) FROM transactions WHERE status='pending'")
    total_revenue = _safe_scalar(cur, "SELECT SUM(expected_amount) FROM transactions WHERE status='approved'")

    total_vip_ever = _safe_scalar(cur, "SELECT COUNT(*) FROM vip_users")
    active_vip = _safe_scalar(
        cur,
        "SELECT COUNT(*) FROM vip_users WHERE expiry_date > ?",
        (datetime.datetime.utcnow().isoformat(),),
    )

    total_packages = _safe_scalar(cur, "SELECT COUNT(*) FROM packages WHERE active=1")
    users_with_strikes = _safe_scalar(cur, "SELECT COUNT(*) FROM user_strikes WHERE count > 0")
    total_strikes = _safe_scalar(cur, "SELECT SUM(count) FROM user_strikes")

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    tx_today = _safe_scalar(cur, "SELECT COUNT(*) FROM transactions WHERE created_at >= ?", (today,))
    revenue_today = _safe_scalar(
        cur,
        "SELECT SUM(expected_amount) FROM transactions WHERE status='approved' AND created_at >= ?",
        (today,),
    )

    total_unique_users = _safe_scalar(
        cur, "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM transactions)"
    )

    conn.close()
    return {
        "total_tx": total_tx,
        "approved_tx": approved_tx,
        "rejected_tx": rejected_tx,
        "pending_tx": pending_tx,
        "total_revenue": total_revenue,
        "total_vip_ever": total_vip_ever,
        "active_vip": active_vip,
        "total_packages": total_packages,
        "users_with_strikes": users_with_strikes,
        "total_strikes": total_strikes,
        "tx_today": tx_today,
        "revenue_today": revenue_today,
        "total_unique_users": total_unique_users,
    }


def get_broadcast_user_ids() -> list:
    """Ambil semua user_id unik yang pernah berinteraksi (pernah transaksi
    apa pun ATAU pernah jadi VIP). Dipakai sebagai target broadcast."""
    conn = _connect()
    cur = conn.cursor()
    ids = set()
    for sql in (
        "SELECT DISTINCT user_id FROM transactions",
        "SELECT DISTINCT user_id FROM vip_users",
    ):
        try:
            cur.execute(sql)
            ids.update(row[0] for row in cur.fetchall() if row[0])
        except sqlite3.OperationalError:
            pass
    conn.close()
    return list(ids)
