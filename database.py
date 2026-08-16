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

# Teks halaman "📖 Petunjuk Order" di menu utama -- panduan singkat alur
# pemesanan supaya user baru tidak bingung sebelum mulai transaksi.
DEFAULT_HOW_TO_ORDER = (
    "📖 <b>Petunjuk Order</b>\n\n"
    "1️⃣ Tekan <b>💎 Lihat Paket VIP</b> di menu utama.\n"
    "2️⃣ Pilih salah satu paket VIP yang tersedia.\n"
    "3️⃣ Scan/bayar QRIS sesuai nominal yang tertera (termasuk 3 digit kode unik).\n"
    "4️⃣ Kirim <b>foto/screenshot bukti transfer</b> ke chat ini.\n"
    "5️⃣ Bot akan memverifikasi otomatis & mengaktifkan VIP kamu.\n\n"
    "❓ Ada kendala? Tekan <b>💬 Hubungi Admin</b> di menu utama."
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

# Teks postingan "Klaim Kode Promo" yang diposting/diupdate otomatis di
# channel promo_post_channel (default @viphanseller). Placeholder yang bisa
# dipakai: {discount} {channel} {max_uses} {claimed} {remaining}
# PENTING: kode promo itu sendiri SENGAJA TIDAK pernah muncul di teks
# postingan ini -- kode cuma muncul lewat popup (answerCallbackQuery) saat
# tombol "🎁 Klaim Kode Promo" dipencet, dan HANYA kalau user sudah terverifikasi
# join channel {channel} (lihat promo_claim_callback() di bot.py).
DEFAULT_PROMO_POST_TEXT = (
    "🎁 <b>KLAIM KODE PROMO DISKON PAKET VIP!</b>\n\n"
    "Dapatkan potongan harga <b>Rp{discount}</b> untuk pembelian paket VIP apa saja!\n\n"
    "📌 <b>Cara klaim:</b>\n"
    "1️⃣ Join channel {channel} (wajib)\n"
    "2️⃣ Tekan tombol \"🎁 Klaim Kode Promo\" di bawah ini\n"
    "3️⃣ Kode promo akan muncul lewat popup (cuma untuk {max_uses} orang pertama!)\n"
    "4️⃣ Masukkan kode itu saat checkout paket VIP untuk dapat potongannya\n\n"
    "📊 Slot terklaim: <b>{claimed}/{max_uses}</b> (sisa {remaining})\n"
    "⏳ Kode diperbarui berkala -- buruan klaim sebelum kuota habis!"
)

# Teks broadcast malam otomatis (default jam 00:00 WIB) yang dikirim ke semua
# user bot untuk mengingatkan soal kode promo yang sedang aktif. Placeholder
# sama seperti DEFAULT_PROMO_POST_TEXT di atas -- kode promo JUGA sengaja
# tidak disertakan di sini, user tetap harus klaim lewat channel.
DEFAULT_PROMO_BROADCAST_TEXT = (
    "🌙🎁 <b>Jangan sampai kelewatan!</b>\n\n"
    "Kode promo diskon <b>Rp{discount}</b> untuk paket VIP masih aktif malam ini.\n"
    "Klaim kodenya di {channel} -- tinggal join channel-nya, tekan tombol "
    "\"🎁 Klaim Kode Promo\", kodenya langsung muncul (kalau kuota masih ada, "
    "sisa {remaining}/{max_uses}).\n\n"
    "Buruan sebelum kuotanya habis! 🚀"
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
        # Daftar channel/grup untuk PAKET KOLEKTIF (mis. "Paket 16 Link" yang
        # berisi 16 channel VIP sekaligus). Satu paket (vip_packages) bisa
        # punya banyak baris di sini -- beda dari kolom target_chat_id di
        # vip_packages yang cuma menampung SATU chat id untuk paket biasa.
        # Kalau paket punya baris di tabel ini, send_package_link() akan
        # membuatkan invite link 1x-pakai (member_limit=1) TERPISAH untuk
        # SETIAP baris/channel, lalu mengirim semuanya sekaligus ke pembeli
        # -- lihat send_package_link() di bot.py.
        c.execute("""
            CREATE TABLE IF NOT EXISTS package_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                label TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        # Klaim kode promo (fitur diskon paket VIP). Satu baris = satu user
        # yang SUDAH mengambil jatah dari kode promo yang sedang aktif --
        # dipakai untuk membatasi kode promo cuma berlaku untuk maksimal N
        # orang (default 15, lihat settings "promo_max_uses"). Kolom `code`
        # ikut disimpan (bukan cuma flag global) supaya begitu kodenya
        # di-rotasi (mingguan / manual lewat /settings), kuota otomatis
        # "reset" -- kode BARU otomatis mulai dari 0 klaim lagi, karena
        # hitungannya selalu difilter per `code`, riwayat kode lama tetap
        # tersimpan apa adanya untuk keperluan statistik/audit.
        c.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                used_at TEXT,
                UNIQUE(code, user_id)
            )
        """)
        # Set default settings kalau belum ada
        defaults = {
            "greeting_text": DEFAULT_GREETING,
            "vip_menu_text": DEFAULT_VIP_INTRO,
            "how_to_order_text": DEFAULT_HOW_TO_ORDER,
            "qris_caption_text": DEFAULT_QRIS_CAPTION,
            "payment_success_text": DEFAULT_PAYMENT_SUCCESS,
            "payment_reject_text": DEFAULT_PAYMENT_REJECT,
            "testi_caption_text": DEFAULT_TESTI_CAPTION,
            # file_id foto Telegram untuk pesan sapaan /start. Kosong ("") berarti
            # sapaan tampil sebagai teks biasa seperti sebelumnya (tanpa foto).
            # file_id (bukan path file lokal) sengaja dipakai karena Telegram
            # mengizinkan file_id yang sama dipakai berulang kali untuk kirim ulang
            # foto tanpa perlu upload ulang -- lihat save_greeting_photo() di bot.py.
            "greeting_photo_file_id": "",
            # Link akses statis GLOBAL — dipakai sebagai cadangan untuk SEMUA paket
            # yang tidak diset target_chat_id (grup Telegram) dan tidak override link
            # sendiri. Diset SEKALI lewat /settings, tidak perlu diinput ulang setiap
            # kali menambah/mengedit paket. Lihat get_setting("static_access_link").
            "static_access_link": "",
            # ── Kode Promo (diskon paket VIP) ────────────────────────────
            "promo_enabled": "0",
            # Channel yang WAJIB di-join dulu sebelum kode promo mau
            # ditampilkan lewat popup (tombol "Klaim Kode Promo").
            "promo_required_channel": "@tahansel",
            # Channel tempat postingan "Klaim Kode Promo" otomatis
            # diposting/diupdate.
            "promo_post_channel": "@viphanseller",
            "promo_discount_amount": "5000",
            "promo_max_uses": "15",
            "promo_rotate_days": "7",
            "promo_code": "",
            "promo_code_created_at": "",
            "promo_post_chat_id": "",
            "promo_post_message_id": "",
            "promo_broadcast_enabled": "0",
            # Jam broadcast malam (WIB, 0-23). Default 0 = jam 00:00 WIB.
            "promo_broadcast_hour_wib": "0",
            "promo_broadcast_last_at": "",
            "promo_post_text": DEFAULT_PROMO_POST_TEXT,
            "promo_broadcast_text": DEFAULT_PROMO_BROADCAST_TEXT,
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


# ── Paket VIP Kolektif (banyak channel dalam satu paket) ────────────────────
# Contoh pemakaian: "Paket 16 Link" -- satu paket VIP yang saat dibeli
# mengirim 16 link akses sekaligus (1 link per channel), tapi TIAP link tetap
# 1x pakai/auto-expire persis seperti paket biasa (lihat send_package_link()
# di bot.py). Paket biasa (1 channel via kolom target_chat_id di
# vip_packages) tidak perlu pakai tabel ini sama sekali.

def add_package_channel(package_id: int, chat_id: str, label: str = "") -> int:
    """Tambah satu channel/grup ke paket kolektif. Return id baris baru."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO package_channels (package_id, chat_id, label, created_at) VALUES (?, ?, ?, ?)",
            (package_id, chat_id.strip(), label.strip(), datetime.datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def list_package_channels(package_id: int):
    """Semua channel milik satu paket kolektif, urut sesuai waktu ditambahkan."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM package_channels WHERE package_id=? ORDER BY id ASC",
            (package_id,),
        ).fetchall()


def count_package_channels(package_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM package_channels WHERE package_id=?", (package_id,)
        ).fetchone()
        return row["c"] if row else 0


def get_package_channel(channel_row_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM package_channels WHERE id=?", (channel_row_id,)).fetchone()


def delete_package_channel(channel_row_id: int):
    """Hapus SATU channel dari paket kolektif (berdasarkan id baris, bukan chat_id,
    karena chat_id yang sama secara teori bisa saja ditambahkan admin lebih dari
    sekali dengan label berbeda)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM package_channels WHERE id=?", (channel_row_id,))


def clear_package_channels(package_id: int) -> int:
    """Hapus SEMUA channel milik satu paket sekaligus (mis. saat admin mau reset
    total daftar link paket kolektif). Return jumlah baris yang dihapus."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM package_channels WHERE package_id=?", (package_id,))
        return cur.rowcount


# ── Kode Promo (diskon paket VIP) ────────────────────────────────────────
# Lihat catatan lengkap di CREATE TABLE promo_redemptions (init_db) & di
# bot.py (apply_promo_code, promo_claim_callback, rotate_promo_code).

def count_promo_redemptions(code: str) -> int:
    """Jumlah user UNIK yang sudah mengklaim/memakai kode promo tertentu."""
    if not code:
        return 0
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM promo_redemptions WHERE code=?", (code,)
        ).fetchone()
        return row["c"] if row else 0


def get_promo_redemption(code: str, user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM promo_redemptions WHERE code=? AND user_id=?", (code, user_id)
        ).fetchone()


def claim_promo_redemption(code: str, user_id: int, username: str, max_uses: int) -> str:
    """Coba \"ambil\" satu slot kode promo untuk `user_id`. Return salah satu:
    - "existing" -- user ini SUDAH pernah klaim kode ini sebelumnya (tidak
      memakan slot baru, aman dipanggil berkali-kali oleh user yang sama,
      mis. kalau dia pencet ulang tombol "Klaim Kode Promo").
    - "new"      -- berhasil, slot baru terpakai (jumlah klaim unik +1).
    - "full"     -- kuota `max_uses` untuk kode ini sudah penuh & user ini
      belum pernah klaim sebelumnya -- ditolak.

    Dilakukan dalam SATU koneksi/transaksi (cek jumlah + insert) supaya tidak
    ada race condition kalau kebetulan 2 orang klaim di waktu yang nyaris
    bersamaan pas slot tersisa tinggal 1.
    """
    if not code:
        return "full"
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM promo_redemptions WHERE code=? AND user_id=?", (code, user_id)
        ).fetchone()
        if existing:
            return "existing"
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM promo_redemptions WHERE code=?", (code,)
        ).fetchone()
        current = row["c"] if row else 0
        if current >= max_uses:
            return "full"
        conn.execute(
            "INSERT INTO promo_redemptions (code, user_id, username, used_at) VALUES (?, ?, ?, ?)",
            (code, user_id, username or "", datetime.datetime.utcnow().isoformat()),
        )
        return "new"


def list_promo_redemptions(code: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM promo_redemptions WHERE code=? ORDER BY id ASC", (code,)
        ).fetchall()


# ── Transactions ──────────────────────────────────────────────────────────

def create_transaction(user_id: int, username: str, package_id: int, expected_amount: int, unique_code: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (user_id, username, package_id, expected_amount, unique_code, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, username, package_id, expected_amount, unique_code, datetime.datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def set_transaction_amount(tx_id: int, expected_amount: int, unique_code: str):
    """Update `expected_amount`/`unique_code` transaksi yang SUDAH dibuat.

    Dipakai supaya nominal unik bisa dihitung dari `tx_id` (dijamin unik &
    permanen oleh database) alih-alih counter di memori (`_tx_counter` di
    bot.py) yang reset ke 0 setiap kali bot restart -- reset itu bisa bikin
    dua transaksi beda dapat nominal unik yang SAMA persis kalau ada
    pembelian sebelum & sesudah restart, sehingga sistem pencocokan
    otomatis salah mencocokkan pembayaran ke transaksi yang keliru.

    Alurnya: create_transaction() dipanggil dulu dengan nilai SEMENTARA
    (harga dasar paket, tanpa kode unik) supaya dapat `tx_id`, baru fungsi
    ini dipanggil untuk menimpa dengan nominal final yang sudah menyertakan
    kode unik berbasis `tx_id` tsb."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET expected_amount=?, unique_code=? WHERE id=?",
            (expected_amount, unique_code, tx_id),
        )


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


def get_all_transactions(limit: int = None):
    """Ambil SEMUA transaksi (status apa pun -- pending/approved/rejected),
    urut dari yang terbaru. SEBELUM INI TIDAK ADA fungsi bulk-fetch untuk
    transaksi sama sekali di database.py -- akibatnya /exportdata di bot.py
    (lewat build_export_snapshot() -> _try_call(db, ["get_all_transactions", ...]))
    tidak pernah menemukan fungsi apa pun untuk dipanggil, dan field
    "transactions" di hasil export SELALU null, PADAHAL datanya sendiri ada
    di tabel `transactions`. Baris di sini dikembalikan sebagai dict biasa
    (bukan sqlite3.Row) supaya langsung siap di-serialize ke JSON."""
    with get_conn() as conn:
        q = "SELECT * FROM transactions ORDER BY id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(row) for row in conn.execute(q).fetchall()]


def get_all_vip_members():
    """Ambil SEMUA member VIP (termasuk yang sudah lewat expiry_date-nya).
    Sama seperti get_all_transactions() -- SEBELUM INI tidak ada fungsi bulk
    untuk vip_users, jadi field "vip_members" di /exportdata SELALU null."""
    with get_conn() as conn:
        return [
            dict(row) for row in
            conn.execute("SELECT * FROM vip_users ORDER BY expiry_date DESC").fetchall()
        ]


def get_all_packages():
    """Ambil SEMUA paket TERMASUK yang sudah dinonaktifkan (active=0).
    Beda dari list_packages() yang defaultnya (active_only=True) cuma
    mengembalikan paket aktif -- cocok untuk ditampilkan ke user, tapi
    kurang tepat untuk backup/export karena paket yang di-nonaktifkan jadi
    ikut hilang dari cadangan data."""
    with get_conn() as conn:
        return [
            dict(row) for row in
            conn.execute("SELECT * FROM vip_packages ORDER BY id ASC").fetchall()
        ]


def import_packages(packages: list) -> int:
    """Restore isi tabel vip_packages dari list hasil export (get_all_packages()).

    PENTING: nama fungsi ini sengaja "import_packages" karena
    restoredb_confirm_cb() di bot.py mencari fungsi importer lewat
    getattr(db, fn_name) dengan daftar nama kandidat
    ["restore_packages", "import_packages", "bulk_set_packages", "set_all_packages"]
    -- kalau tidak ada satu pun nama yang cocok persis, bot melaporkan
    "dilewati (belum ada fungsi importer di database.py)" walau ada fungsi
    lain yang secara fungsi sebenarnya melakukan hal yang sama.

    Baris di-restore pakai INSERT OR REPLACE berdasarkan `id` supaya
    idempotent (import ulang snapshot yang sama tidak menghasilkan
    duplikat). Baris yang BUKAN dict (mis. backup lama yang korup jadi
    string representasi sqlite3.Row seperti "<sqlite3.Row object at
    0x...>") dilewati dengan aman -- SEBELUM INI baris seperti itu bikin
    pkg.get("id") melempar AttributeError yang menghentikan seluruh
    proses restore paket, padahal baris-baris valid lain di backup yang
    sama seharusnya tetap bisa dipulihkan. Return jumlah baris yang
    BERHASIL di-restore (baris korup tidak dihitung)."""
    count = 0
    with get_conn() as conn:
        for pkg in (packages or []):
            if not isinstance(pkg, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO vip_packages "
                "(id, name, price, duration_days, description, link, target_chat_id, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pkg.get("id"),
                    pkg.get("name"),
                    pkg.get("price"),
                    pkg.get("duration_days"),
                    pkg.get("description", ""),
                    pkg.get("link", ""),
                    pkg.get("target_chat_id", ""),
                    pkg.get("active", 1),
                ),
            )
            count += 1
    return count


def import_transactions(transactions: list) -> int:
    """Restore isi tabel transactions dari list hasil export (get_all_transactions()).

    Nama "import_transactions" cocok dengan salah satu kandidat yang dicari
    restoredb_confirm_cb() di bot.py (["restore_transactions",
    "import_transactions", "bulk_set_transactions"]). Lihat catatan di
    import_packages() untuk alasan lengkapnya.

    INSERT OR REPLACE berdasarkan `id` -- idempotent. Baris yang bukan
    dict dilewati dengan aman (lihat catatan di import_packages() untuk
    alasannya). Return jumlah baris yang berhasil di-restore."""
    count = 0
    with get_conn() as conn:
        for tx in (transactions or []):
            if not isinstance(tx, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO transactions "
                "(id, user_id, username, package_id, expected_amount, unique_code, "
                "proof_file_id, ocr_amount, ocr_raw_text, image_hash, status, "
                "reject_reason, created_at, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tx.get("id"),
                    tx.get("user_id"),
                    tx.get("username"),
                    tx.get("package_id"),
                    tx.get("expected_amount"),
                    tx.get("unique_code"),
                    tx.get("proof_file_id"),
                    tx.get("ocr_amount"),
                    tx.get("ocr_raw_text"),
                    tx.get("image_hash"),
                    tx.get("status", "pending"),
                    tx.get("reject_reason"),
                    tx.get("created_at"),
                    tx.get("verified_at"),
                ),
            )
            count += 1
    return count


def import_vip_members(vip_members: list) -> int:
    """Restore isi tabel vip_users dari list hasil export (get_all_vip_members()).

    Nama "import_vip_members" cocok dengan salah satu kandidat yang dicari
    restoredb_confirm_cb() di bot.py (["restore_vip", "restore_vip_members",
    "import_vip_members", "bulk_set_vip"]). Lihat catatan di
    import_packages() untuk alasan lengkapnya.

    INSERT OR REPLACE berdasarkan `user_id` -- idempotent. Baris yang
    bukan dict dilewati dengan aman (lihat catatan di import_packages()
    untuk alasannya). Return jumlah baris yang berhasil di-restore."""
    count = 0
    with get_conn() as conn:
        for member in (vip_members or []):
            if not isinstance(member, dict):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO vip_users (user_id, username, package_id, expiry_date) "
                "VALUES (?, ?, ?, ?)",
                (
                    member.get("user_id"),
                    member.get("username"),
                    member.get("package_id"),
                    member.get("expiry_date"),
                ),
            )
            count += 1
    return count


def import_data(data: dict) -> dict:
    """Alias praktis: restore SEMUA bagian snapshot export (settings, packages,
    transactions, vip_members) sekaligus lewat satu panggilan. Berguna untuk
    dipakai manual di luar alur /restoredb bot.py (mis. lewat skrip/console),
    yang mana justru memanggil import_packages()/import_transactions()/
    import_vip_members() satu-satu secara dinamis -- lihat catatan di
    masing-masing fungsi itu.

    Return dict jumlah baris yang berhasil di-restore per tabel, contoh:
    {"settings": 8, "packages": 3, "transactions": 42, "vip_members": 5}"""
    data = data or {}
    counts = {"settings": 0, "packages": 0, "transactions": 0, "vip_members": 0}

    for key, value in (data.get("settings") or {}).items():
        set_setting(key, value)
        counts["settings"] += 1

    counts["packages"] = import_packages(data.get("packages") or [])
    counts["transactions"] = import_transactions(data.get("transactions") or [])
    counts["vip_members"] = import_vip_members(data.get("vip_members") or [])

    return counts


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
