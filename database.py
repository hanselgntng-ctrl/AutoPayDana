"""
database.py
===========
Semua operasi ke SQLite dikumpulkan di sini:
- settings (teks sapaan, teks menu VIP, path QRIS aktif)
- vip_packages (daftar paket VIP + harga + durasi + target grup Telegram + link fallback)
- transactions (riwayat transaksi & status verifikasi, termasuk hash gambar bukti transfer)
- vip_users (member VIP aktif & tanggal expired)
- incoming_notifications (notifikasi saldo masuk DANA pribadi & bisnis yang diteruskan ke bot)
- user_strikes (penghitung pelanggaran/bukti palsu per user)
"""

import os
import sqlite3
import datetime
from contextlib import contextmanager

import config

DEFAULT_GREETING = (
    "👋 Selamat datang di <b>Bot VIP Otomatis</b>!\n\n"
    "Silakan pilih menu di bawah untuk melihat paket VIP yang tersedia."
)

DEFAULT_VIP_INTRO = (
    "✨ <b>Daftar Paket VIP</b>\n"
    "Pilih salah satu paket di bawah ini untuk melanjutkan pembayaran."
)

# Pesan tampilan QRIS saat user memilih paket. Placeholder yang bisa dipakai:
# {package} {duration} {amount}
DEFAULT_QRIS_CAPTION = (
    "🧾 <b>Detail Pembayaran</b>\n\n"
    "Paket: <b>{package}</b>\n"
    "Durasi: {duration} hari\n"
    "Total transfer: <b>Rp{amount}</b>\n\n"
    "⚠️ Transfer <b>harus persis</b> sesuai nominal di atas (termasuk 3 digit "
    "kode unik terakhir) agar sistem bisa memverifikasi otomatis.\n\n"
    "Setelah transfer, langsung kirim <b>foto/screenshot bukti transfer</b> ke chat ini."
)

# Pesan saat pembayaran berhasil/disetujui (otomatis maupun manual oleh admin).
# Placeholder: {package} {duration} {amount} {expiry}
DEFAULT_PAYMENT_SUCCESS = (
    "✅ <b>Pembayaran terverifikasi!</b>\n\n"
    "Paket: <b>{package}</b>\n"
    "VIP kamu aktif sampai: <b>{expiry}</b>\n\n"
    "Terima kasih! 🎉"
)

# Pesan saat pembayaran ditolak/gagal diverifikasi (otomatis maupun manual oleh admin).
# Placeholder: {package} {amount} {reason}
DEFAULT_PAYMENT_REJECT = (
    "❌ <b>Verifikasi pembayaran gagal.</b>\n{reason}\n\n"
    "Nominal yang diharapkan: <b>Rp{amount}</b>\n"
    "Silakan cek kembali dan kirim ulang bukti transfer, atau hubungi admin."
)

# Caption saat bukti transfer yang APPROVED diposting otomatis ke channel
# testi. Placeholder: {package}
DEFAULT_TESTI_CAPTION = (
    "✅ <b>Testimoni Pembayaran</b>\n"
    "Paket: <b>{package}</b>\n\n"
    "#testi"
)

# Catatan format teks (greeting_text, vip_menu_text, qris_caption_text,
# payment_success_text, payment_reject_text, testi_caption_text, broadcast):
# Nilai-nilai ini disimpan dalam format HTML (parse_mode=HTML), BUKAN Markdown lagi.
# Ini supaya emoji premium/custom yang dikirim admin lewat chat (Telegram otomatis
# menyertakan entity custom_emoji pada pesan admin) ikut tersimpan & tampil balik
# ke user tanpa admin perlu tahu/ketik custom_emoji_id secara manual. Lihat
# bot.py::html_of() untuk cara menangkapnya dari update.message.text_html.


def _connect():
    # Pastikan folder tempat file DB berada sudah ada (penting untuk Railway Volume,
    # karena volume mount kadang belum berisi folder sama sekali saat pertama kali dipasang).
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS vip_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                duration_days INTEGER NOT NULL,
                description TEXT DEFAULT '',
                link TEXT DEFAULT '',
                target_chat_id TEXT DEFAULT '',
                active INTEGER DEFAULT 1
            )
        """)
        # Migrasi untuk instalasi lama yang tabelnya belum punya kolom-kolom baru
        for ddl in (
            "ALTER TABLE vip_packages ADD COLUMN link TEXT DEFAULT ''",
            "ALTER TABLE vip_packages ADD COLUMN target_chat_id TEXT DEFAULT ''",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass  # kolom sudah ada
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                package_id INTEGER NOT NULL,
                expected_amount INTEGER NOT NULL,
                unique_code TEXT NOT NULL,
                proof_file_id TEXT,
                ocr_amount INTEGER,
                ocr_raw_text TEXT,
                image_hash TEXT,
                status TEXT DEFAULT 'pending',  -- pending, approved, rejected
                reject_reason TEXT,
                created_at TEXT,
                verified_at TEXT
            )
        """)
        try:
            c.execute("ALTER TABLE transactions ADD COLUMN image_hash TEXT")
        except sqlite3.OperationalError:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                package_id INTEGER,
                expiry_date TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS incoming_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_type TEXT NOT NULL,  -- 'personal' atau 'bisnis'
                amount INTEGER NOT NULL,
                raw_text TEXT,
                consumed INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_strikes (
                user_id INTEGER PRIMARY KEY,
                count INTEGER DEFAULT 0,
                last_strike_at TEXT
            )
        """)
        # Set default settings kalau belum ada
        defaults = {
            "greeting_text": DEFAULT_GREETING,
            "vip_menu_text": DEFAULT_VIP_INTRO,
            "qris_caption_text": DEFAULT_QRIS_CAPTION,
            "payment_success_text": DEFAULT_PAYMENT_SUCCESS,
            "payment_reject_text": DEFAULT_PAYMENT_REJECT,
            "testi_caption_text": DEFAULT_TESTI_CAPTION,
            # Link akses statis GLOBAL — dipakai sebagai cadangan untuk SEMUA paket
            # yang tidak diset target_chat_id (grup Telegram) dan tidak override link
            # sendiri. Diset SEKALI lewat /settings, tidak perlu diinput ulang setiap
            # kali menambah/mengedit paket. Lihat get_setting("static_access_link").
            "static_access_link": "",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


# ── Settings ──────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ── VIP Packages ──────────────────────────────────────────────────────────

def add_package(name: str, price: int, duration_days: int, description: str = "", link: str = "", target_chat_id: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO vip_packages (name, price, duration_days, description, link, target_chat_id) VALUES (?, ?, ?, ?, ?, ?)",
            (name, price, duration_days, description, link, target_chat_id),
        )


def edit_package(pkg_id: int, name: str, price: int, duration_days: int, description: str = "",
                  link: str = None, target_chat_id: str = None):
    """Kalau link/target_chat_id=None, nilai lama tidak diubah (dipertahankan)."""
    with get_conn() as conn:
        current = conn.execute("SELECT link, target_chat_id FROM vip_packages WHERE id=?", (pkg_id,)).fetchone()
        final_link = current["link"] if link is None else link
        final_chat_id = current["target_chat_id"] if target_chat_id is None else target_chat_id
        conn.execute(
            "UPDATE vip_packages SET name=?, price=?, duration_days=?, description=?, link=?, target_chat_id=? WHERE id=?",
            (name, price, duration_days, description, final_link, final_chat_id, pkg_id),
        )


def delete_package(pkg_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE vip_packages SET active=0 WHERE id=?", (pkg_id,))


def list_packages(active_only: bool = True):
    with get_conn() as conn:
        q = "SELECT * FROM vip_packages"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY price ASC"
        return conn.execute(q).fetchall()


def get_package(pkg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM vip_packages WHERE id=?", (pkg_id,)).fetchone()


# ── Transactions ──────────────────────────────────────────────────────────

def create_transaction(user_id: int, username: str, package_id: int, expected_amount: int, unique_code: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (user_id, username, package_id, expected_amount, unique_code, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, username, package_id, expected_amount, unique_code, datetime.datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def attach_proof(tx_id: int, file_id: str, ocr_amount: int, ocr_raw_text: str, image_hash: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET proof_file_id=?, ocr_amount=?, ocr_raw_text=?, image_hash=? WHERE id=?",
            (file_id, ocr_amount, ocr_raw_text, image_hash, tx_id),
        )


def check_duplicate_image_hash(image_hash: str, exclude_tx_id: int):
    """Cari transaksi LAIN (selain exclude_tx_id) yang pernah pakai gambar bukti
    transfer dengan hash persis sama. Dipakai untuk mendeteksi bukti yang dipakai ulang
    (foto lama dikirim lagi) atau dibagikan antar user."""
    if not image_hash:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE image_hash=? AND id!=? ORDER BY id ASC LIMIT 1",
            (image_hash, exclude_tx_id),
        ).fetchone()


def set_transaction_status(tx_id: int, status: str, reject_reason: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET status=?, reject_reason=?, verified_at=? WHERE id=?",
            (status, reject_reason, datetime.datetime.utcnow().isoformat(), tx_id),
        )


def get_transaction(tx_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()


def get_pending_transaction_for_user(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()


# ── VIP Users ─────────────────────────────────────────────────────────────

def grant_vip(user_id: int, username: str, package_id: int, duration_days: int):
    with get_conn() as conn:
        existing = conn.execute("SELECT * FROM vip_users WHERE user_id=?", (user_id,)).fetchone()
        now = datetime.datetime.utcnow()
        if existing and existing["expiry_date"]:
            current_expiry = datetime.datetime.fromisoformat(existing["expiry_date"])
            base = current_expiry if current_expiry > now else now
        else:
            base = now
        new_expiry = base + datetime.timedelta(days=duration_days)
        conn.execute(
            "INSERT INTO vip_users (user_id, username, package_id, expiry_date) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, package_id=excluded.package_id, expiry_date=excluded.expiry_date",
            (user_id, username, package_id, new_expiry.isoformat()),
        )
        return new_expiry


def get_vip(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM vip_users WHERE user_id=?", (user_id,)).fetchone()


# ── Notifikasi saldo masuk (diteruskan dari HP admin ke Telegram) ──────────
# Lihat README bagian "Deteksi mutasi DANA Pribadi & Bisnis" untuk cara kerja lengkap.

def add_notification(account_type: str, amount: int, raw_text: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO incoming_notifications (account_type, amount, raw_text, created_at) VALUES (?, ?, ?, ?)",
            (account_type, amount, raw_text, datetime.datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def find_unconsumed_notification(amount: int, tolerance: int, window_minutes: int, account_type: str = None):
    """Cari notifikasi saldo masuk yang belum dipakai (consumed=0), nominal cocok
    (dalam toleransi), dan masih dalam rentang waktu window_minutes menit terakhir."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=window_minutes)).isoformat()
    with get_conn() as conn:
        q = "SELECT * FROM incoming_notifications WHERE consumed=0 AND created_at>=? AND ABS(amount-?)<=?"
        params = [cutoff, amount, tolerance]
        if account_type:
            q += " AND account_type=?"
            params.append(account_type)
        q += " ORDER BY created_at ASC LIMIT 1"
        return conn.execute(q, params).fetchone()


def mark_notification_consumed(notif_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE incoming_notifications SET consumed=1 WHERE id=?", (notif_id,))


# ── Strike / pelanggaran (bukti transfer palsu / duplikat) ─────────────────

def increment_strike(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT count FROM user_strikes WHERE user_id=?", (user_id,)).fetchone()
        new_count = (row["count"] if row else 0) + 1
        conn.execute(
            "INSERT INTO user_strikes (user_id, count, last_strike_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET count=excluded.count, last_strike_at=excluded.last_strike_at",
            (user_id, new_count, datetime.datetime.utcnow().isoformat()),
        )
        return new_count


def get_strike_count(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT count FROM user_strikes WHERE user_id=?", (user_id,)).fetchone()
        return row["count"] if row else 0
