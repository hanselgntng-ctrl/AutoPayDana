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

# ── Telegram Mini App "Lihat Paket VIP" (halaman HTML asli, bukan teks chat) ─
# Diisi URL halaman statis yang sudah kamu hosting (mis. GitHub Pages, format:
# https://<username>.github.io/<repo>/). Kalau dikosongkan, bot otomatis
# FALLBACK ke tabel teks biasa di chat (perilaku lama) -- jadi bot tetap jalan
# normal walau WebApp ini belum kamu setup.
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Port HTTP internal untuk endpoint publik GET /api/packages (dipakai halaman
# WebApp di atas buat ambil data paket VIP terbaru via fetch()). Railway
# otomatis meng-inject env var PORT begitu Networking > Public Domain
# diaktifkan di dashboard service ini -- generate_domain akan expose port ini.
PORT = int(os.getenv("PORT", "8080"))

# ID Telegram admin (bisa lebih dari satu, pisahkan dengan koma di .env)
# Cara cek ID Telegram: chat ke @userinfobot
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# Grup/channel log untuk notifikasi transaksi (opsional, isi 0 jika tidak dipakai)
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0"))

# Username Telegram admin/kontak yang ditampilkan di menu utama & pesan hasil
# pembayaran (approve/reject). Isi TANPA tanda @ (mis. "gosahsoknal").
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "gosahsoknal")

# ── Verifikasi bukti transfer: 100% LOKAL, TANPA pihak kedua/ketiga ─────────
# Bot TIDAK memanggil API DANA Bisnis ataupun notifikasi HP yang diteruskan
# admin -- semua keputusan approve/reject MURNI dari hasil OCR pada gambar
# bukti transfer yang diunggah user itu sendiri, dicocokkan ke 3 hal: NAMA
# penerima (QRIS), TANGGAL transaksi, dan NOMINAL.

# Nama penerima resmi yang terdaftar di QRIS/akun DANA Bisnis kamu, PERSIS
# seperti yang muncul di struk/bukti transfer (mis. "ZONA BASAH" atau nama
# pribadi kamu kalau QRIS-nya atas nama pribadi). WAJIB diisi -- kalau
# dikosongkan, pengecekan nama akan DILEWATI (bot jadi cuma cek tanggal &
# nominal saja, kurang ketat).
QRIS_RECIPIENT_NAME = os.getenv("QRIS_RECIPIENT_NAME", "")

# Toleransi (dalam JAM) antara waktu SEKARANG dan tanggal/jam yang terbaca di
# bukti transfer. Dibuat tidak nol sama sekali (bukan wajib "hari ini persis
# jam ini juga") karena OCR & jam sistem HP user bisa beda beberapa saat --
# tapi tetap ketat (default 24 jam) supaya bukti transfer LAMA (dipakai ulang
# dari transaksi lain/hari lain) tetap tertangkap & ditolak.
PROOF_DATE_TOLERANCE_HOURS = int(os.getenv("PROOF_DATE_TOLERANCE_HOURS", "24"))

# ── Auto-posting testimoni ke channel ────────────────────────────────────
# Isi Chat ID channel "testi" (bot HARUS sudah jadi admin di channel tsb
# dengan izin post message) kalau mau bukti transfer yang APPROVED otomatis
# diposting ke sana sebagai testimoni/social proof, lengkap dengan watermark
# & caption sesuai nama paket + #testi. Isi 0 kalau fitur ini tidak dipakai.
# Cara cek Chat ID channel: forward salah satu pesan dari channel itu ke
# @userinfobot.
TESTI_CHANNEL_ID = int(os.getenv("TESTI_CHANNEL_ID", "0"))

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

# Watermark (PNG transparan, hasil konversi dari stiker statis yang diset admin
# lewat /settings) yang ditempel di tengah bukti transfer sebelum diposting
# otomatis ke channel testi (lihat TESTI_CHANNEL_ID di atas).
WATERMARK_IMAGE_PATH = os.getenv("WATERMARK_IMAGE_PATH", os.path.join(DATA_DIR, "watermark.png"))

# Pastikan semua folder penyimpanan sudah ada saat bot start
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.dirname(QRIS_IMAGE_PATH), exist_ok=True)
os.makedirs(PROOF_IMAGES_DIR, exist_ok=True)

# ── Emoji Premium Kustom (opsional, untuk fitur 📢 Broadcast) ───────────────
# Kalau diisi, karakter emoji unicode "pemicu" di bawah ini (👑 ✨ 💎 🔥 ⭐) yang
# admin ketik di teks/caption broadcast akan OTOMATIS diganti jadi emoji
# Premium custom saat dikirim ke semua user -- BERGUNA khususnya untuk admin
# yang TIDAK punya akun Telegram Premium sendiri (kalau admin PUNYA Premium,
# emoji custom yang ditempel langsung dari keyboard Telegram-nya sendiri sudah
# otomatis ikut terkirim apa adanya, tanpa perlu pengaturan ini sama sekali).
#
# Cara mendapatkan custom_emoji_id:
#   1. Minta siapa pun yang PUNYA Telegram Premium mengirim emoji premium
#      tersebut sebagai pesan ke bot semacam @RawDataBot atau @userinfobot.
#   2. Lihat field "custom_emoji_id" pada JSON hasil balasan bot itu, salin
#      angkanya (contoh: "5368324170671202286").
#   3. Tempel angka itu (sebagai string, pakai tanda kutip) ke value yang
#      sesuai di bawah ini.
#
# Kalau dibiarkan None / kosong, karakter emoji unicode biasa tetap dikirim
# apa adanya (tampil normal di semua perangkat) -- tidak ada yang rusak.
PREMIUM_EMOJI_IDS = {
    "crown":   os.getenv("PREMIUM_EMOJI_CROWN")   or None,   # dipicu oleh 👑
    "sparkle": os.getenv("PREMIUM_EMOJI_SPARKLE") or None,   # dipicu oleh ✨
    "diamond": os.getenv("PREMIUM_EMOJI_DIAMOND") or None,   # dipicu oleh 💎
    "fire":    os.getenv("PREMIUM_EMOJI_FIRE")    or None,   # dipicu oleh 🔥
    "star":    os.getenv("PREMIUM_EMOJI_STAR")    or None,   # dipicu oleh ⭐
}
