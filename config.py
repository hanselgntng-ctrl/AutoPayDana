"""
config.py
=========
Semua konfigurasi bot ada di sini. Isi nilai-nilai di bawah sebelum menjalankan bot,
atau gunakan file .env (lihat .env.example).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_BOT_TELEGRAM_KAMU")

# ID Telegram admin (bisa lebih dari satu, pisahkan dengan koma di .env)
# Cara cek ID Telegram: chat ke @userinfobot
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# Grup/channel log untuk notifikasi transaksi (opsional, isi 0 jika tidak dipakai)
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0"))

# ── DANA Bisnis API ───────────────────────────────────────────────────────
# PENTING: DANA tidak mempunyai satu endpoint publik universal untuk cek mutasi.
# Kredensial & endpoint di bawah didapat dari kontrak/akun DANA Bisnis (merchant)
# yang kamu daftarkan sendiri ke pihak DANA. Sesuaikan MERCHANT_ID, PRIVATE_KEY,
# dan API_BASE_URL sesuai dokumentasi resmi yang diberikan DANA ke akun bisnismu.
DANA_API_BASE_URL = os.getenv("DANA_API_BASE_URL", "https://api.dana.id/... (ISI SESUAI DOKUMEN DANA BISNIS KAMU)")
DANA_MERCHANT_ID = os.getenv("DANA_MERCHANT_ID", "ISI_MERCHANT_ID")
DANA_CLIENT_ID = os.getenv("DANA_CLIENT_ID", "ISI_CLIENT_ID")
DANA_CLIENT_SECRET = os.getenv("DANA_CLIENT_SECRET", "ISI_CLIENT_SECRET")
DANA_PRIVATE_KEY_PATH = os.getenv("DANA_PRIVATE_KEY_PATH", "private_key.pem")

# Toleransi waktu (menit) saat mencocokkan mutasi dengan bukti transfer
DANA_MATCH_TIME_WINDOW_MINUTES = 30

# Toleransi selisih nominal (kalau ada biaya admin dsb), biasanya 0
DANA_MATCH_AMOUNT_TOLERANCE = 0

# ── Deteksi mutasi DANA Pribadi & Bisnis lewat notifikasi (lihat README) ────
# DANA tidak menyediakan API resmi untuk mengecek mutasi akun PRIBADI (hanya akun
# Bisnis/merchant yang punya API resmi). Untuk akun pribadi, bot ini memakai
# pendekatan yang sah: admin meneruskan notifikasi "saldo masuk" dari aplikasi
# DANA di HP-nya sendiri ke sebuah chat/grup Telegram (pakai aplikasi forwarder
# notifikasi seperti MacroDroid/Tasker/Automate, BUKAN dengan membobol atau
# meniru API resmi DANA), lalu bot membaca teks notifikasi itu.
#
# Isi ID chat Telegram tempat notifikasi diteruskan di sini. Chat personal &
# bisnis BOLEH sama (bot akan coba tebak jenis akun dari kata kunci di teksnya)
# atau berbeda (lebih akurat, disarankan).
NOTIF_PERSONAL_CHAT_ID = int(os.getenv("NOTIF_PERSONAL_CHAT_ID", "0"))
NOTIF_BUSINESS_CHAT_ID = int(os.getenv("NOTIF_BUSINESS_CHAT_ID", "0"))

# Jumlah pelanggaran (bukti transfer duplikat/palsu) sebelum bot memberi
# peringatan tambahan yang lebih tegas kepada admin di grup log
FRAUD_STRIKE_ALERT_THRESHOLD = int(os.getenv("FRAUD_STRIKE_ALERT_THRESHOLD", "3"))

# ── OCR ───────────────────────────────────────────────────────────────────
# Path ke tesseract, biarkan default kalau sudah ada di PATH sistem
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "tesseract")

# ── Penyimpanan data (Railway Volume) ───────────────────────────────────────
# PENTING soal Railway:
# - Filesystem Railway TIDAK persisten by default — setiap kali redeploy/restart,
#   semua file yang dibuat saat runtime (database sqlite, gambar QRIS, bukti transfer)
#   akan HILANG kalau disimpan di luar Volume.
# - Railway otomatis menyediakan env var RAILWAY_VOLUME_MOUNT_PATH begitu kamu
#   memasang Volume di dashboard (Service → Settings → Volumes → New Volume).
#   Kita pakai env var itu secara otomatis kalau tersedia, supaya kamu tidak perlu
#   set DATA_DIR manual lagi. Kalau belum pasang Volume / jalan lokal, fallback ke "./data".
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR", "data")

# ── Database ──────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot.db"))

# ── Folder penyimpanan gambar QRIS & bukti transfer sementara ───────────────
QRIS_IMAGE_PATH = os.getenv("QRIS_IMAGE_PATH", os.path.join(DATA_DIR, "qris_images", "qris_current.jpg"))
PROOF_IMAGES_DIR = os.path.join(DATA_DIR, "proofs")

# Pastikan semua folder penyimpanan sudah ada saat bot start
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.dirname(QRIS_IMAGE_PATH), exist_ok=True)
os.makedirs(PROOF_IMAGES_DIR, exist_ok=True)
