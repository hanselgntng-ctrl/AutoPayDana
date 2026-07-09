"""
bot.py
======
Bot Telegram VIP dengan auto-approve/reject bukti transfer.

Alur pembayaran:
1. User /start -> lihat teks sapaan + menu.
2. User pilih "Lihat Paket VIP" -> tabel paket VIP (custom via /settings).
3. User pilih salah satu paket -> bot kirim QRIS + nominal unik untuk dibayar.
4. User upload foto bukti transfer -> bot OCR nominal & referensi dari gambar,
   lalu cross-check ke mutasi resmi DANA Bisnis (dana_api.py).
5. Kalau cocok -> otomatis APPROVE, VIP langsung aktif, tanpa admin pencet apa pun.
   Kalau tidak cocok -> otomatis REJECT + alasan, dengan opsi diteruskan ke admin
   untuk fallback manual (jaring pengaman kalau OCR/API tidak yakin).

Jalankan dengan: python bot.py
"""

import os
import logging
import datetime
import asyncio
import warnings

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning
from PIL import Image

# settings_conv (di bawah) sengaja mencampur CallbackQueryHandler (tombol) dan
# MessageHandler (ketik teks) di dalam state yang sama -> per_message WAJIB False
# (default), dan PTB akan selalu memunculkan warning FAQ soal ini walau
# perilakunya sudah sesuai yang kita inginkan (state tetap dilacak per
# chat/user, hanya bukan per pesan individual -> tidak relevan untuk alur ini).
# Redam warning spesifik ini saja supaya log tidak berisik, tanpa menyembunyikan
# warning PTB lain yang mungkin penting.
warnings.filterwarnings(
    "ignore", message=r".*per_message.*", category=PTBUserWarning
)

import config
import database as db
import ocr_utils
import dana_api
import keyboards as kb
import stats_broadcast as sb
import watermark

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Conversation states (untuk menu /settings admin) ───────────────────────
(
    SET_GREETING, SET_VIP_TEXT, SET_QRIS,
    SET_QRIS_CAPTION, SET_SUCCESS_TEXT, SET_REJECT_TEXT,
    SET_WATERMARK, SET_TESTI_CAPTION,
    ADD_PKG_NAME, ADD_PKG_PRICE, ADD_PKG_DURATION, ADD_PKG_DESC, ADD_PKG_CHATID, ADD_PKG_LINK,
    EDIT_PKG_PICK, EDIT_PKG_NAME, EDIT_PKG_PRICE, EDIT_PKG_DURATION, EDIT_PKG_CHATID, EDIT_PKG_LINK,
    BROADCAST_WAIT, BROADCAST_CONFIRM,
) = range(22)

_tx_counter = 0  # counter sederhana untuk bikin nominal unik (idealnya simpan di DB kalau bot restart terus)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ── Dukungan emoji premium/custom tanpa perlu admin mengetik ID manual ─────
# Saat admin memakai emoji premium Telegram di dalam pesan (teks sapaan, teks
# menu VIP, atau broadcast), Telegram sudah otomatis menyertakan info emoji itu
# sebagai "entity" custom_emoji di pesan admin (lengkap dengan custom_emoji_id-nya)
# — admin tidak perlu tahu atau ketik ID-nya sama sekali. python-telegram-bot
# punya property bawaan `message.text_html` / `message.caption_html` yang
# mengonversi teks + seluruh entity (bold, italic, dan emoji premium/custom)
# jadi satu string HTML siap-kirim (format <tg-emoji emoji-id="...">🔥</tg-emoji>
# untuk emoji premium). String HTML inilah yang kita simpan ke database, dan
# nanti dikirim ulang ke user dengan parse_mode=HTML supaya emoji premiumnya
# ikut tampil (catatan: pengguna yang menerima perlu Telegram Premium supaya
# terlihat animasi/versi premiumnya; kalau tidak, tetap tampil emoji biasa).
def html_of_text(message) -> str:
    """Ambil versi HTML dari teks pesan (termasuk emoji premium), fallback ke
    teks polos kalau text_html tidak tersedia."""
    return getattr(message, "text_html", None) or message.text or ""


def html_of_caption(message) -> str:
    """Sama seperti html_of_text tapi untuk caption foto."""
    return getattr(message, "caption_html", None) or message.caption or ""


def render_template(template: str, **placeholders) -> str:
    """Ganti placeholder seperti {package}/{amount}/{expiry}/{reason} pada
    template pesan (qris_caption_text, payment_success_text, payment_reject_text,
    dll) dengan nilai transaksi yang sebenarnya. Placeholder yang tidak dikenali
    dibiarkan apa adanya (tidak menyebabkan error), sehingga admin bebas menulis
    kurung kurawal biasa di teksnya tanpa membuat bot crash."""
    text = template
    for key, value in placeholders.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def rupiah(amount: int) -> str:
    """Format angka rupiah dengan pemisah ribuan titik, mis. 50000 -> '50.000'."""
    return f"{amount:,}".replace(",", ".")


# Username Telegram admin/kontak yang ditampilkan di pesan hasil approve/reject
# (isi asli & default ada di config.py -> CONTACT_USERNAME).
CONTACT_USERNAME = config.CONTACT_USERNAME


def result_kb() -> InlineKeyboardMarkup:
    """Keyboard yang disertakan pada pesan hasil pembayaran (approved/rejected):
    tombol kembali ke menu utama bot + tombol kontak admin."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="back_main")],
        [InlineKeyboardButton("💬 Hubungi Admin", url=f"https://t.me/{CONTACT_USERNAME}")],
    ])


# ── Tombol "Kembali" untuk semua langkah di dalam /settings ────────────────

def back_kb() -> InlineKeyboardMarkup:
    """Keyboard sederhana berisi 1 tombol untuk batal & kembali ke menu settings."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Batal & Kembali ke Menu Settings", callback_data="settings_cancel")]])


def with_back(markup):
    """Tambahkan baris tombol 'Kembali' di bawah keyboard yang sudah ada (mis. daftar paket)."""
    rows = list(markup.inline_keyboard) if markup else []
    rows.append([InlineKeyboardButton("🔙 Kembali ke Menu Settings", callback_data="settings_cancel")])
    return InlineKeyboardMarkup(rows)


# ── Transisi "fade" antar menu ──────────────────────────────────────────────
# Telegram Bot API tidak punya animasi visual asli (bukan web/native UI), jadi
# efek "fade in/out" di sini disimulasikan dengan 2 tahap edit pesan: pesan
# lama "memudar" jadi placeholder titik-titik sebentar, baru muncul konten
# baru — supaya pergantian menu terasa halus/berjeda, bukan berubah mendadak.
FADE_OUT_TEXT = "· · ·"
FADE_STEP_DELAY = 0.18  # detik, sengaja singkat supaya tidak terasa lambat/lag


async def fade_transition(query, text: str, **kwargs):
    """Ganti isi pesan (lewat query.edit_message_text) dengan efek transisi
    fade in/out sederhana. `kwargs` diteruskan apa adanya ke edit_message_text
    akhir (parse_mode, reply_markup, dst) — tahap "fade out" sengaja tanpa
    keyboard/parse_mode supaya terlihat polos/redup dulu.

    Kalau tahap fade gagal (mis. pesan sumbernya foto/caption, bukan teks
    biasa, yang tidak boleh diedit pakai edit_message_text), fungsi ini tetap
    lanjut ke tahap akhir seperti biasa tanpa mengganggu jalannya menu."""
    try:
        await query.edit_message_text(FADE_OUT_TEXT)
        await asyncio.sleep(FADE_STEP_DELAY)
    except Exception:
        pass
    await query.edit_message_text(text, **kwargs)


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipanggil dari tombol 'Kembali' di tengah alur /settings (add/edit paket, dll)."""
    query = update.callback_query
    await query.answer()
    for k in ("new_pkg", "edit_pkg_id", "edit_pkg_name", "edit_pkg_price", "edit_pkg_duration", "edit_pkg_chatid", "broadcast_payload"):
        context.user_data.pop(k, None)
    await fade_transition(
        query, "⚙️✨ *Menu Pengaturan Bot*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_menu_kb()
    )
    return ConversationHandler.END


# ── Emoji "premium" untuk mempercantik tampilan menu ────────────────────────
#
# Catatan jujur: Telegram punya "Custom Emoji" khusus Telegram Premium yang bisa
# animasi, tapi (1) animasinya HANYA terlihat oleh penerima yang juga punya
# Telegram Premium (user biasa cuma lihat versi statis), dan (2) butuh
# custom_emoji_id spesifik dari paket emoji Premium tertentu — bukan emoji
# unicode biasa. Karena ID paket itu belum ditentukan, di bawah ini dipakai
# emoji unicode "mewah" (✨💎👑) yang tampil identik di SEMUA perangkat tanpa
# syarat Premium. Kalau nanti kamu mau custom_emoji_id asli, isi EMOJI_IDS di
# config.py (mis. PREMIUM_EMOJI_IDS = {"crown": "5xxxxxxxxxxxxxxxxx"}) dan
# beri tahu Claude — helper premium_entities() di bawah sudah disiapkan untuk itu.

EMOJI_IDS = getattr(config, "PREMIUM_EMOJI_IDS", {})  # {} kalau belum diisi admin


def premium_entities(text: str, emoji_map: dict):
    """Bangun list MessageEntity CUSTOM_EMOJI untuk teks yang mengandung emoji
    unicode di `emoji_map` (mis. {"👑": "crown"}), HANYA kalau ID-nya sudah
    diisi di config.PREMIUM_EMOJI_IDS. Kalau belum diisi, kembalikan None
    (fallback otomatis ke emoji unicode biasa, tetap tampil normal)."""
    from telegram import MessageEntity
    if not EMOJI_IDS:
        return None
    entities = []
    for ch, key in emoji_map.items():
        custom_id = EMOJI_IDS.get(key)
        if not custom_id:
            continue
        idx = text.find(ch)
        while idx != -1:
            entities.append(MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI, offset=idx, length=len(ch), custom_emoji_id=custom_id
            ))
            idx = text.find(ch, idx + 1)
    return entities or None


def settings_menu_kb() -> InlineKeyboardMarkup:
    """Menu utama /settings + tombol 📊 Statistik & 📢 Broadcast, tanpa perlu
    mengubah keyboards.py (menyisipkan 2 baris tombol tambahan di bawah menu
    dasar yang sudah ada)."""
    base = kb.settings_menu_keyboard()
    rows = list(base.inline_keyboard) if base else []
    rows.append([InlineKeyboardButton("📊✨ Statistik Bot", callback_data="settings_stats")])
    rows.append([InlineKeyboardButton("📢💎 Broadcast Pesan", callback_data="settings_broadcast")])
    return InlineKeyboardMarkup(rows)


# ── /start & menu utama ─────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    greeting = db.get_setting("greeting_text", db.DEFAULT_GREETING)
    await update.message.reply_text(
        greeting, parse_mode=ParseMode.HTML, reply_markup=kb.main_menu_keyboard()
    )


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "show_vip":
        vip_text = db.get_setting("vip_menu_text", db.DEFAULT_VIP_INTRO)
        table = kb.format_vip_table()
        await fade_transition(
            query,
            f"{vip_text}\n\n{table}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.vip_list_keyboard(),
        )

    elif query.data == "back_main":
        greeting = db.get_setting("greeting_text", db.DEFAULT_GREETING)
        await fade_transition(
            query, greeting, parse_mode=ParseMode.HTML, reply_markup=kb.main_menu_keyboard()
        )

    elif query.data == "my_status":
        vip = db.get_vip(query.from_user.id)
        if vip and vip["expiry_date"]:
            expiry = datetime.datetime.fromisoformat(vip["expiry_date"])
            if expiry > datetime.datetime.utcnow():
                sisa = expiry - datetime.datetime.utcnow()
                text = (
                    f"✅ Status VIP: *AKTIF*\n"
                    f"Berlaku sampai: {expiry.strftime('%d %B %Y %H:%M')} UTC\n"
                    f"Sisa waktu: {sisa.days} hari"
                )
            else:
                text = "❌ VIP kamu sudah *habis masa berlakunya*. Silakan perpanjang."
        else:
            text = "Kamu belum memiliki status VIP. Yuk pilih paket dulu!"
        await fade_transition(
            query, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb.main_menu_keyboard()
        )

    elif query.data.startswith("buy_"):
        pkg_id = int(query.data.split("_")[1])
        pkg = db.get_package(pkg_id)
        if not pkg:
            await query.edit_message_text("Paket tidak ditemukan / sudah tidak aktif.")
            return

        global _tx_counter
        _tx_counter += 1
        final_amount, unique_code = dana_api.generate_unique_code(pkg["price"], _tx_counter)

        username = query.from_user.username or query.from_user.first_name
        tx_id = db.create_transaction(query.from_user.id, username, pkg_id, final_amount, unique_code)
        context.user_data["pending_tx_id"] = tx_id

        qris_path = config.QRIS_IMAGE_PATH
        caption_template = db.get_setting("qris_caption_text", db.DEFAULT_QRIS_CAPTION)
        caption = render_template(
            caption_template,
            package=pkg["name"],
            duration=pkg["duration_days"],
            amount=rupiah(final_amount),
        )

        if os.path.exists(qris_path):
            with open(qris_path, "rb") as f:
                await query.message.reply_photo(photo=f, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await query.message.reply_text(
                caption + "\n\n<i>(QRIS belum diset oleh admin, hubungi admin untuk kode QRIS)</i>",
                parse_mode=ParseMode.HTML,
            )
        await query.delete_message()


async def send_package_link(chat_id: int, context: ContextTypes.DEFAULT_TYPE, pkg):
    """Kirim akses VIP ke user sesuai paket yang dipesan.

    - Kalau paket sudah diset `target_chat_id` (grup/channel Telegram VIP), bot akan
      MEMBUAT SENDIRI link undangan baru yang HANYA BERLAKU UNTUK 1 ORANG
      (member_limit=1) khusus untuk pembelian ini, lalu mengirimkannya. Link lama
      tidak pernah dipakai ulang, jadi tidak bisa dibagikan/dipakai orang lain.
    - Kalau paket hanya diset link statis (bukan grup Telegram yang bot kelola),
      bot mengirim link itu apa adanya — TIDAK bisa dibatasi otomatis oleh bot,
      karena bot cuma bisa membuat & membatasi invite link untuk chat Telegram
      yang bot sendiri jadi salah satu adminnya.
    """
    target_chat_id = (pkg["target_chat_id"] or "").strip()

    if target_chat_id:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=int(target_chat_id),
                member_limit=1,
                name=f"VIP-{pkg['name']}-{chat_id}"[:32],
            )
            await context.bot.send_message(
                chat_id,
                f"🔗 *Akses {pkg['name']}*\n{invite.invite_link}\n\n"
                f"_Link ini dibuat khusus untukmu dan hanya bisa dipakai 1 kali oleh 1 akun. "
                f"Jangan bagikan ke orang lain karena akan otomatis tidak berlaku lagi setelah dipakai._",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"Buka {pkg['name']}", url=invite.invite_link)]]
                ),
            )
        except Exception as e:
            logger.error(f"Gagal membuat invite link untuk paket {pkg['name']} (chat_id {target_chat_id}): {e}")
            await context.bot.send_message(
                chat_id,
                "⚠️ Pembayaran kamu sudah *berhasil*, tapi bot gagal membuat link akses "
                "otomatis. Admin akan segera mengirimkan link akses secara manual.",
                parse_mode=ParseMode.MARKDOWN,
            )
            if config.LOG_CHAT_ID:
                await context.bot.send_message(
                    config.LOG_CHAT_ID,
                    f"⚠️ Gagal membuat invite link otomatis untuk paket '{pkg['name']}' "
                    f"(chat_id {target_chat_id}). Pastikan bot sudah jadi admin dengan izin "
                    f"'Invite Users via Link' di grup/channel tersebut.\nError: {e}",
                )
        return

    link = (pkg["link"] or "").strip()
    if not link:
        return
    is_url = link.startswith("http://") or link.startswith("https://")
    await context.bot.send_message(
        chat_id,
        f"🔗 *Akses {pkg['name']}*\n{link}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"Buka {pkg['name']}", url=link)]]
        ) if is_url else None,
    )


# ── Terima notifikasi saldo masuk (diteruskan dari HP admin) ────────────

async def handle_incoming_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Chat khusus (NOTIF_PERSONAL_CHAT_ID / NOTIF_BUSINESS_CHAT_ID di config.py) dipakai
    admin untuk meneruskan notifikasi "saldo masuk" dari aplikasi DANA di HP-nya sendiri
    (lewat aplikasi forwarder notifikasi seperti MacroDroid/Tasker/Automate yang mengirim
    teks notifikasi ke bot ini via Telegram). Bot membaca nominal dari teks tsb dan
    menyimpannya sebagai "bukti saldo masuk" yang nanti dicocokkan ke transaksi user.
    """
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    if chat_id == config.NOTIF_PERSONAL_CHAT_ID:
        account_type = "personal"
    elif chat_id == config.NOTIF_BUSINESS_CHAT_ID:
        account_type = "bisnis"
    else:
        return

    amount = ocr_utils.extract_amount(text)
    if not amount:
        logger.warning(f"Notifikasi masuk dari chat {chat_id} tapi nominal tidak terbaca: {text[:100]}")
        return

    db.add_notification(account_type, amount, text)
    logger.info(f"Notifikasi saldo masuk ({account_type}) tercatat: Rp{amount:,}".replace(",", "."))

    # Catatan: notifikasi ini SENGAJA tidak langsung meng-approve transaksi apa
    # pun di sini. Notifikasi cuma disimpan sebagai "bukti saldo masuk" yang
    # SIAP dicocokkan. Approve (+ kirim pesan "Pembayaran berhasil" & link akses)
    # baru terjadi saat user benar-benar mengirim foto bukti transfer lewat
    # handle_proof_photo() di bawah, yang kemudian mencocokkan ke notifikasi ini
    # lewat dana_api.find_matching_mutation(). Ini disengaja: bukti transfer dari
    # user tetap jadi syarat wajib sebelum bot mengirim konfirmasi + link — kalau
    # notifikasi datang lebih dulu, itu cuma mempercepat & memastikan proses
    # verifikasi begitu bukti transfer dikirim (karena datanya sudah siap), bukan
    # meng-skip syarat kirim bukti transfer itu sendiri.


# ── Terima & verifikasi bukti transfer ──────────────────────────────────

async def handle_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tx_id = context.user_data.get("pending_tx_id")
    if not tx_id:
        tx = db.get_pending_transaction_for_user(update.effective_user.id)
        if not tx:
            await update.message.reply_text(
                "Sepertinya kamu belum memilih paket VIP. Ketik /start untuk mulai."
            )
            return
        tx_id = tx["id"]

    tx = db.get_transaction(tx_id)
    if not tx:
        await update.message.reply_text("Transaksi tidak ditemukan atau sudah diproses sebelumnya.")
        return
    if tx["status"] == "approved":
        # Transaksi ini sudah berstatus approved sebelumnya (misalnya admin sudah
        # approve manual lewat fallback review, atau ini foto kedua yang dikirim
        # untuk transaksi yang sama yang sudah selesai diproses). Bukti transfer
        # tetap boleh dikirim untuk arsip, tapi tidak perlu diproses ulang.
        await update.message.reply_text(
            "✅ Transaksi ini sudah *terverifikasi* sebelumnya. "
            "Bukti transfer ini tidak perlu diproses lagi — kalau VIP kamu belum aktif, hubungi admin.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if tx["status"] != "pending":
        await update.message.reply_text("Transaksi tidak ditemukan atau sudah diproses sebelumnya.")
        return

    pkg = db.get_package(tx["package_id"])
    user = update.effective_user
    username_display = f"@{user.username}" if user.username else user.first_name

    processing_msg = await update.message.reply_text("🔍 Memproses bukti transfer, mohon tunggu sebentar...")

    # Download foto (disimpan di PROOF_IMAGES_DIR, yang ada di dalam DATA_DIR / Railway Volume)
    photo = update.message.photo[-1]
    file = await photo.get_file()
    local_path = os.path.join(config.PROOF_IMAGES_DIR, f"proof_{tx_id}.jpg")
    await file.download_to_drive(local_path)

    # ── Log bukti transfer ke grup log SEGERA setelah diterima ──
    if config.LOG_CHAT_ID:
        await context.bot.send_photo(
            config.LOG_CHAT_ID,
            photo=photo.file_id,
            caption=(
                f"📥 *Bukti transfer masuk*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n"
                f"Paket: {pkg['name']} | Nominal diharapkan: Rp{tx['expected_amount']:,}\n"
                f"Sedang diverifikasi otomatis...".replace(",", ".")
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    # 1) Hash gambar — deteksi kalau bukti ini PERSIS SAMA dengan yang pernah dipakai
    #    di transaksi lain (indikasi kuat bukti daur ulang / dibagikan antar user)
    image_hash = ocr_utils.compute_image_hash(local_path)
    duplicate_tx = db.check_duplicate_image_hash(image_hash, exclude_tx_id=tx_id)

    # 2) OCR
    ocr_result = ocr_utils.analyze_proof(local_path)
    db.attach_proof(tx_id, photo.file_id, ocr_result["amount"], ocr_result["raw_text"], image_hash)

    if duplicate_tx:
        # ── TERDETEKSI BUKTI DAUR ULANG / KEMUNGKINAN PALSU ──
        reason = f"Gambar bukti transfer identik dengan TX #{duplicate_tx['id']} yang sudah pernah diproses sebelumnya."
        db.set_transaction_status(tx_id, "rejected", reject_reason=reason)
        strike_count = db.increment_strike(user.id)

        warning_text = (
            "⚠️ *PERINGATAN KERAS*\n\n"
            "Bukti transfer yang kamu kirim terdeteksi *identik* dengan bukti transfer yang "
            "sudah pernah dipakai sebelumnya di transaksi lain. Mengirim ulang bukti transfer "
            "lama, hasil editan, atau bukti milik orang lain untuk mendapatkan akses VIP adalah "
            "*pelanggaran serius* dan tidak akan diproses.\n\n"
            f"Ini adalah pelanggaran ke-*{strike_count}* yang tercatat dari akun kamu. "
            "Pelanggaran yang berulang dapat mengakibatkan akun kamu *diblokir secara permanen* "
            "dari layanan ini.\n\n"
            "Kalau kamu merasa ini kesalahan, silakan hubungi admin untuk klarifikasi."
        )
        await processing_msg.edit_text(warning_text, parse_mode=ParseMode.MARKDOWN)

        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"🚨 *TERDETEKSI BUKTI TRANSFER DUPLIKAT/PALSU*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n"
                f"Duplikat persis dari TX #{duplicate_tx['id']}\n"
                f"Total pelanggaran user ini sejauh ini: *{strike_count}*"
                + (
                    "\n\n⚠️ *User ini sudah melewati ambang batas pelanggaran, mohon ditinjau/diblokir manual.*"
                    if strike_count >= config.FRAUD_STRIKE_ALERT_THRESHOLD else ""
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data.pop("pending_tx_id", None)
        return

    # 3) Cross-check gabungan: API resmi DANA Bisnis + notifikasi DANA Pribadi & Bisnis
    match = dana_api.find_matching_mutation(
        expected_amount=tx["expected_amount"],
        unique_code=tx["unique_code"],
        ocr_amount=ocr_result["amount"],
    )

    if match:
        # Tandai notifikasi terpakai supaya tidak dipakai transaksi lain (kalau dari jalur notifikasi)
        if match.get("notif_id"):
            db.mark_notification_consumed(match["notif_id"])

        source_label = {
            "business_api": "mutasi resmi DANA Bisnis",
            "notif_personal": "notifikasi DANA Pribadi",
            "notif_bisnis": "notifikasi DANA Bisnis",
            "notif_personal_ocr_fallback": "notifikasi DANA Pribadi (cocok via OCR)",
            "notif_bisnis_ocr_fallback": "notifikasi DANA Bisnis (cocok via OCR)",
        }.get(match["source"], match["source"])

        # ── Kirim status berhasil ke grup log DULU, baru approve ──
        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"✅ *Status Pembayaran: BERHASIL*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n"
                f"Paket: {pkg['name']} | Rp{tx['expected_amount']:,}\n"
                f"Terverifikasi via: {source_label}".replace(",", "."),
                parse_mode=ParseMode.MARKDOWN,
            )

        # ── AUTO APPROVE ──
        db.set_transaction_status(tx_id, "approved")
        expiry = db.grant_vip(user.id, user.username or "", tx["package_id"], pkg["duration_days"])

        success_template = db.get_setting("payment_success_text", db.DEFAULT_PAYMENT_SUCCESS)
        success_text = render_template(
            success_template,
            package=pkg["name"],
            duration=pkg["duration_days"],
            amount=rupiah(tx["expected_amount"]),
            expiry=f"{expiry.strftime('%d %B %Y %H:%M')} UTC",
        )
        await processing_msg.edit_text(success_text, parse_mode=ParseMode.HTML, reply_markup=result_kb())
        await send_package_link(update.effective_chat.id, context, pkg)
        await post_testimonial(context, tx, pkg)

    elif ocr_result["amount"] == tx["expected_amount"]:
        # OCR cocok tapi belum ada konfirmasi dari API/notifikasi (mungkin delay) →
        # fallback ke admin untuk review manual supaya tidak salah approve.
        db.set_transaction_status(tx_id, "pending", reject_reason="Menunggu review manual (belum ada konfirmasi mutasi/notifikasi)")
        await processing_msg.edit_text(
            "⏳ Bukti transfer terbaca sesuai nominal, tapi sistem masih menunggu "
            "konfirmasi saldo masuk. Tim admin akan segera mengecek manual jika "
            "lebih dari beberapa menit."
        )
        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"⏳ *Butuh review manual*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n"
                f"Expected: Rp{tx['expected_amount']:,} | OCR: Rp{ocr_result['amount']:,}\n"
                f"Belum ada mutasi/notifikasi yang cocok.".replace(",", "."),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb.confirm_proof_keyboard(tx_id),
            )

    else:
        # ── AUTO REJECT (mismatch biasa, bukan indikasi kuat penipuan) ──
        reason = "Nominal pada bukti transfer tidak sesuai / tidak terbaca, dan tidak ditemukan mutasi/notifikasi yang cocok."
        db.set_transaction_status(tx_id, "rejected", reject_reason=reason)
        reject_template = db.get_setting("payment_reject_text", db.DEFAULT_PAYMENT_REJECT)
        reject_text = render_template(
            reject_template,
            package=pkg["name"],
            amount=rupiah(tx["expected_amount"]),
            reason=reason,
        )
        await processing_msg.edit_text(reject_text, parse_mode=ParseMode.HTML, reply_markup=result_kb())
        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"❌ *Auto-rejected*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n{reason}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb.confirm_proof_keyboard(tx_id),
            )

    context.user_data.pop("pending_tx_id", None)


# ── Auto-posting testimoni bukti transfer (approved) ke channel testi ───────

def proof_image_path(tx_id: int) -> str:
    """Path lokal bukti transfer yang di-download saat handle_proof_photo()
    (selalu di-download apa pun hasil verifikasinya, jadi masih ada di sini
    baik untuk approve otomatis maupun approve manual admin belakangan)."""
    return os.path.join(config.PROOF_IMAGES_DIR, f"proof_{tx_id}.jpg")


async def post_testimonial(context: ContextTypes.DEFAULT_TYPE, tx, pkg):
    """Posting otomatis bukti transfer yang baru saja di-APPROVE ke channel
    testi (config.TESTI_CHANNEL_ID), lengkap dengan watermark transparan
    (dikonversi dari stiker yang diset admin lewat /settings) yang ditempel di
    tengah gambar, dan caption berisi nama paket + #testi.

    Sengaja TIDAK pernah melempar exception ke pemanggil: kalau channel belum
    diset, watermark belum diset, atau bot gagal posting (mis. bukan admin di
    channel tsb), fitur ini cukup dilewati/dicatat ke log saja — TIDAK BOLEH
    menggagalkan proses approve transaksi & pengiriman VIP ke user."""
    if not config.TESTI_CHANNEL_ID:
        return

    proof_path = proof_image_path(tx["id"])
    if not os.path.exists(proof_path):
        logger.warning(f"Testi: bukti transfer TX #{tx['id']} tidak ditemukan di {proof_path}, lewati posting testi.")
        return

    output_path = proof_path
    try:
        if os.path.exists(config.WATERMARK_IMAGE_PATH):
            output_path = os.path.join(config.PROOF_IMAGES_DIR, f"testi_{tx['id']}.jpg")
            watermark.apply_watermark(proof_path, config.WATERMARK_IMAGE_PATH, output_path)
        # Kalau watermark belum diset admin, tetap posting apa adanya (tanpa
        # watermark) daripada tidak posting sama sekali.

        caption_template = db.get_setting("testi_caption_text", db.DEFAULT_TESTI_CAPTION)
        caption = render_template(caption_template, package=pkg["name"])

        with open(output_path, "rb") as f:
            await context.bot.send_photo(
                config.TESTI_CHANNEL_ID, photo=f, caption=caption, parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Gagal posting testimoni TX #{tx['id']} ke channel testi: {e}")
        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"⚠️ Gagal auto-posting testimoni TX #{tx['id']} ke channel testi.\n"
                f"Pastikan TESTI_CHANNEL_ID benar & bot sudah jadi admin (dengan izin post) "
                f"di channel tsb.\nError: {e}",
            )


# ── Fallback manual admin (khusus kasus butuh review, lihat handler di atas) ─

async def admin_manual_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return

    action, tx_id = query.data.rsplit("_", 1)
    tx_id = int(tx_id)
    tx = db.get_transaction(tx_id)
    if not tx:
        await query.edit_message_caption("Transaksi tidak ditemukan.")
        return

    pkg = db.get_package(tx["package_id"])

    if action == "admin_approve":
        db.set_transaction_status(tx_id, "approved")
        expiry = db.grant_vip(tx["user_id"], tx["username"], tx["package_id"], pkg["duration_days"])
        success_template = db.get_setting("payment_success_text", db.DEFAULT_PAYMENT_SUCCESS)
        success_text = render_template(
            success_template,
            package=pkg["name"],
            duration=pkg["duration_days"],
            amount=rupiah(tx["expected_amount"]),
            expiry=f"{expiry.strftime('%d %B %Y %H:%M')} UTC",
        )
        await context.bot.send_message(tx["user_id"], success_text, parse_mode=ParseMode.HTML, reply_markup=result_kb())
        await send_package_link(tx["user_id"], context, pkg)
        await post_testimonial(context, tx, pkg)
        await query.edit_message_caption(caption=f"✅ TX #{tx_id} disetujui manual oleh admin.")
    else:
        reason = "Ditolak manual oleh admin"
        db.set_transaction_status(tx_id, "rejected", reject_reason=reason)
        reject_template = db.get_setting("payment_reject_text", db.DEFAULT_PAYMENT_REJECT)
        reject_text = render_template(
            reject_template,
            package=pkg["name"],
            amount=rupiah(tx["expected_amount"]),
            reason=reason,
        )
        await context.bot.send_message(tx["user_id"], reject_text, parse_mode=ParseMode.HTML, reply_markup=result_kb())
        await query.edit_message_caption(caption=f"❌ TX #{tx_id} ditolak manual oleh admin.")


# ── /settings (khusus admin) ─────────────────────────────────────────────

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return ConversationHandler.END
    await update.message.reply_text("⚙️✨ *Menu Pengaturan Bot*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_menu_kb())
    return ConversationHandler.END


async def settings_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return ConversationHandler.END

    data = query.data

    if data == "settings_close":
        await query.edit_message_text("Menu settings ditutup.")
        return ConversationHandler.END

    if data == "settings_back":
        await fade_transition(query, "⚙️✨ *Menu Pengaturan Bot*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_menu_kb())
        return ConversationHandler.END

    if data == "settings_stats":
        s = sb.get_stats()
        text = (
            "📊✨ *Statistik Bot* 💎\n\n"
            "*Transaksi*\n"
            f"💳 Total: *{s['total_tx']}*\n"
            f"✅ Disetujui: *{s['approved_tx']}*\n"
            f"❌ Ditolak: *{s['rejected_tx']}*\n"
            f"⏳ Pending: *{s['pending_tx']}*\n"
            f"🧾 Hari ini: *{s['tx_today']}*\n\n"
            "*Revenue*\n"
            f"💰 Total (approved): *Rp{s['total_revenue']:,}*\n".replace(",", ".") +
            f"💵 Hari ini: *Rp{s['revenue_today']:,}*\n\n".replace(",", ".") +
            "*Member & Paket*\n"
            f"👑 Total pernah VIP: *{s['total_vip_ever']}*\n"
            f"💎 VIP aktif sekarang: *{s['active_vip']}*\n"
            f"🏷️ Paket aktif: *{s['total_packages']}*\n"
            f"👤 Total user unik: *{s['total_unique_users']}*\n\n"
            "*Keamanan*\n"
            f"🚨 User dengan pelanggaran: *{s['users_with_strikes']}*\n"
            f"⚠️ Total strike tercatat: *{s['total_strikes']}*"
        )
        await fade_transition(query, text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        return ConversationHandler.END

    if data == "settings_broadcast":
        await fade_transition(
            query,
            "📢💎 *Broadcast Pesan*\n\n"
            "Kirim pesan yang mau di-broadcast ke *semua user* yang pernah "
            "bertransaksi/VIP (boleh teks biasa, atau foto + caption).\n\n"
            "💎 Bold/italic dan emoji premium yang kamu pakai langsung di chat ini "
            "akan ikut tampil ke user (tidak perlu isi ID emoji manual).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return BROADCAST_WAIT

    if data == "set_greeting":
        await fade_transition(
            query,
            "Kirim teks sapaan baru.\n\n"
            "💎 Mau pakai emoji premium? Tinggal sisipkan emoji premium-nya langsung "
            "di teks yang kamu ketik/kirim di chat ini — tidak perlu cari/isi ID emoji "
            "secara manual, bot otomatis mendeteksi & menyimpannya. Bold/italic/format "
            "lain yang kamu pakai di chat juga ikut tersimpan.",
            reply_markup=back_kb(),
        )
        return SET_GREETING

    if data == "set_vip_text":
        await fade_transition(
            query,
            "Kirim teks intro menu VIP baru.\n\n"
            "💎 Sama seperti teks sapaan — emoji premium bisa langsung disisipkan di "
            "chat, tidak perlu ID emoji manual.",
            reply_markup=back_kb(),
        )
        return SET_VIP_TEXT

    if data == "set_qris":
        await fade_transition(query, "Kirim foto QRIS baru:", reply_markup=back_kb())
        return SET_QRIS

    if data == "set_qris_caption":
        current = db.get_setting("qris_caption_text", db.DEFAULT_QRIS_CAPTION)
        await fade_transition(
            query,
            "Kirim teks <b>pesan tampilan QRIS</b> yang baru (ini pesan yang muncul begitu user "
            "memilih paket, bersama gambar QRIS).\n\n"
            "Placeholder yang bisa dipakai (akan otomatis diganti nilai asli):\n"
            "<code>{package}</code> nama paket, <code>{duration}</code> durasi (hari), "
            "<code>{amount}</code> total transfer.\n\n"
            "💎 Emoji premium & bold/italic yang kamu pakai langsung di chat ini ikut tersimpan.\n\n"
            f"Teks saat ini:\n{current}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_QRIS_CAPTION

    if data == "set_success_text":
        current = db.get_setting("payment_success_text", db.DEFAULT_PAYMENT_SUCCESS)
        await fade_transition(
            query,
            "Kirim teks <b>pesan pembayaran berhasil</b> yang baru (dikirim ke user saat "
            "transaksi di-approve, baik otomatis maupun manual oleh admin).\n\n"
            "Placeholder yang bisa dipakai:\n"
            "<code>{package}</code> nama paket, <code>{duration}</code> durasi (hari), "
            "<code>{amount}</code> nominal transfer, <code>{expiry}</code> tanggal VIP berakhir.\n\n"
            "💎 Emoji premium & bold/italic yang kamu pakai langsung di chat ini ikut tersimpan.\n\n"
            f"Teks saat ini:\n{current}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_SUCCESS_TEXT

    if data == "set_reject_text":
        current = db.get_setting("payment_reject_text", db.DEFAULT_PAYMENT_REJECT)
        await fade_transition(
            query,
            "Kirim teks <b>pesan pembayaran ditolak/gagal</b> yang baru (dikirim ke user saat "
            "transaksi di-reject, baik otomatis maupun manual oleh admin).\n\n"
            "Placeholder yang bisa dipakai:\n"
            "<code>{package}</code> nama paket, <code>{amount}</code> nominal yang diharapkan, "
            "<code>{reason}</code> alasan penolakan.\n\n"
            "💎 Emoji premium & bold/italic yang kamu pakai langsung di chat ini ikut tersimpan.\n\n"
            f"Teks saat ini:\n{current}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_REJECT_TEXT

    if data == "set_watermark":
        wm_status = "✅ sudah diset" if os.path.exists(config.WATERMARK_IMAGE_PATH) else "⚠️ belum diset"
        await fade_transition(
            query,
            "Kirim <b>stiker (sticker)</b> yang mau dipakai sebagai watermark testi.\n\n"
            "Watermark ini akan ditempel <b>transparan, ukuran sedang, di tengah</b> setiap "
            "bukti transfer yang diposting otomatis ke channel testi (channel testi diset lewat "
            "env var <code>TESTI_CHANNEL_ID</code>).\n\n"
            "⚠️ Kirim stiker <b>statis</b> (gambar diam), bukan stiker animasi/video.\n\n"
            f"Status watermark saat ini: {wm_status}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_WATERMARK

    if data == "set_testi_caption":
        current = db.get_setting("testi_caption_text", db.DEFAULT_TESTI_CAPTION)
        await fade_transition(
            query,
            "Kirim teks <b>caption testi</b> yang baru (dipakai saat bukti transfer yang "
            "approved diposting otomatis ke channel testi).\n\n"
            "Placeholder yang bisa dipakai:\n"
            "<code>{package}</code> nama paket VIP yang dibeli.\n\n"
            "💎 Emoji premium & bold/italic yang kamu pakai langsung di chat ini ikut tersimpan.\n\n"
            f"Teks saat ini:\n{current}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_TESTI_CAPTION

    if data == "add_package":
        context.user_data["new_pkg"] = {}
        await fade_transition(
            query, "Masukkan *nama paket* baru (contoh: VIP Bulanan):", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
        )
        return ADD_PKG_NAME

    if data == "edit_package":
        await fade_transition(
            query, "Pilih paket yang ingin diedit:", reply_markup=with_back(kb.package_pick_keyboard("editpkg"))
        )
        return EDIT_PKG_PICK

    if data == "delete_package":
        await fade_transition(
            query, "Pilih paket yang ingin dihapus:", reply_markup=with_back(kb.package_pick_keyboard("delpkg"))
        )
        return ConversationHandler.END

    if data.startswith("delpkg_"):
        pkg_id = int(data.split("_")[1])
        db.delete_package(pkg_id)
        await fade_transition(query, "✅ Paket berhasil dihapus (dinonaktifkan).", reply_markup=settings_menu_kb())
        return ConversationHandler.END

    if data.startswith("editpkg_"):
        pkg_id = int(data.split("_")[1])
        context.user_data["edit_pkg_id"] = pkg_id
        pkg = db.get_package(pkg_id)
        await fade_transition(
            query,
            f"Paket saat ini: *{pkg['name']}*, harga Rp{pkg['price']:,}, durasi {pkg['duration_days']} hari.\n".replace(",", ".") +
            "Masukkan *nama baru*:", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
        )
        return EDIT_PKG_NAME

    return ConversationHandler.END


async def save_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("greeting_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Teks sapaan berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_vip_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("vip_menu_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Teks menu VIP berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_qris_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("qris_caption_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Pesan tampilan QRIS berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_success_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("payment_success_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Pesan pembayaran berhasil berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_reject_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("payment_reject_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Pesan pembayaran ditolak berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.message.sticker
    if not sticker:
        await update.message.reply_text(
            "Mohon kirim dalam bentuk <b>stiker (sticker)</b>, bukan foto/dokumen biasa.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_WATERMARK
    if sticker.is_animated or sticker.is_video:
        await update.message.reply_text(
            "Stiker animasi/video belum didukung untuk watermark. Mohon kirim stiker "
            "<b>statis</b> (gambar diam) saja.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_WATERMARK

    file = await sticker.get_file()
    raw_path = os.path.join(config.DATA_DIR, "watermark_raw.webp")
    await file.download_to_drive(raw_path)

    try:
        img = Image.open(raw_path).convert("RGBA")
        img.save(config.WATERMARK_IMAGE_PATH, "PNG")
    except Exception as e:
        logger.error(f"Gagal memproses stiker jadi watermark: {e}")
        await update.message.reply_text(
            "⚠️ Gagal memproses stiker itu jadi watermark. Coba kirim stiker statis lain.",
            reply_markup=back_kb(),
        )
        return SET_WATERMARK
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)

    await update.message.reply_text(
        "✅ Watermark testi berhasil diperbarui. Mulai sekarang watermark ini otomatis "
        "ditempel transparan di tengah tiap bukti transfer yang diposting ke channel testi.",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_testi_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("testi_caption_text", html_of_text(update.message))
    await update.message.reply_text(
        "✅ Caption testi berhasil diperbarui (emoji premium/format yang kamu pakai ikut tersimpan).",
        reply_markup=settings_menu_kb(),
    )
    return ConversationHandler.END


async def save_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Mohon kirim dalam bentuk foto.", reply_markup=back_kb())
        return SET_QRIS
    photo = update.message.photo[-1]
    file = await photo.get_file()
    await file.download_to_drive(config.QRIS_IMAGE_PATH)
    await update.message.reply_text("✅ QRIS berhasil diperbarui.", reply_markup=settings_menu_kb())
    return ConversationHandler.END


async def add_pkg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_pkg"]["name"] = update.message.text
    await update.message.reply_text("Masukkan *harga* (angka saja, contoh: 50000):", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    return ADD_PKG_PRICE


async def add_pkg_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text("Harga harus berupa angka. Coba lagi:", reply_markup=back_kb())
        return ADD_PKG_PRICE
    context.user_data["new_pkg"]["price"] = int(update.message.text)
    await update.message.reply_text("Masukkan *durasi VIP* dalam hari (contoh: 30):", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    return ADD_PKG_DURATION


async def add_pkg_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text("Durasi harus berupa angka. Coba lagi:", reply_markup=back_kb())
        return ADD_PKG_DURATION
    context.user_data["new_pkg"]["duration_days"] = int(update.message.text)
    await update.message.reply_text(
        "Masukkan *deskripsi singkat* paket (atau kirim '-' untuk kosongkan):",
        parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb()
    )
    return ADD_PKG_DESC


async def add_pkg_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = "" if update.message.text.strip() == "-" else update.message.text
    context.user_data["new_pkg"]["description"] = desc
    await update.message.reply_text(
        "Masukkan *Chat ID grup/channel Telegram VIP* untuk paket ini.\n"
        "⚠️ Bot HARUS sudah jadi admin di grup/channel tsb dengan izin *Invite Users via Link*, "
        "supaya bot bisa otomatis membuat link akses 1x pakai untuk tiap pembeli.\n\n"
        "_Cara cek Chat ID: tambahkan @userinfobot ke grup/channel itu, atau forward salah satu "
        "pesan dari grup itu ke @userinfobot._\n\n"
        "Kirim '-' kalau paket ini tidak pakai grup Telegram (nanti kamu isi link statis biasa di langkah berikutnya):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return ADD_PKG_CHATID


async def add_pkg_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_pkg"]["target_chat_id"] = "" if text == "-" else text
    await update.message.reply_text(
        "Masukkan *link akses statis* sebagai cadangan (dipakai HANYA kalau Chat ID di atas "
        "kamu kosongkan), atau kirim '-' untuk kosongkan:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return ADD_PKG_LINK


async def add_pkg_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = "" if update.message.text.strip() == "-" else update.message.text.strip()
    p = context.user_data["new_pkg"]
    db.add_package(p["name"], p["price"], p["duration_days"], p["description"], link, p.get("target_chat_id", ""))
    await update.message.reply_text("✅ Paket VIP baru berhasil ditambahkan.", reply_markup=settings_menu_kb())
    context.user_data.pop("new_pkg", None)
    return ConversationHandler.END


async def edit_pkg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edit_pkg_name"] = update.message.text
    await update.message.reply_text("Masukkan *harga baru*:", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    return EDIT_PKG_PRICE


async def edit_pkg_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text("Harga harus angka. Coba lagi:", reply_markup=back_kb())
        return EDIT_PKG_PRICE
    context.user_data["edit_pkg_price"] = int(update.message.text)
    await update.message.reply_text("Masukkan *durasi baru* (hari):", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
    return EDIT_PKG_DURATION


async def edit_pkg_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text("Durasi harus angka. Coba lagi:", reply_markup=back_kb())
        return EDIT_PKG_DURATION
    context.user_data["edit_pkg_duration"] = int(update.message.text)
    pkg = db.get_package(context.user_data["edit_pkg_id"])
    current_chatid = pkg["target_chat_id"] or "(belum ada)"
    await update.message.reply_text(
        f"Chat ID grup VIP saat ini: {current_chatid}\n"
        "Masukkan *Chat ID baru* (bot harus jadi admin di sana), kirim '-' untuk mengosongkan, "
        "atau kirim '=' untuk membiarkan tetap sama:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return EDIT_PKG_CHATID


async def edit_pkg_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "=":
        context.user_data["edit_pkg_chatid"] = None  # pertahankan nilai lama
    elif text == "-":
        context.user_data["edit_pkg_chatid"] = ""
    else:
        context.user_data["edit_pkg_chatid"] = text

    pkg = db.get_package(context.user_data["edit_pkg_id"])
    current_link = pkg["link"] or "(belum ada)"
    await update.message.reply_text(
        f"Link statis cadangan saat ini: {current_link}\n"
        "Masukkan *link baru*, kirim '-' untuk mengosongkan, "
        "atau kirim '=' untuk membiarkan tetap sama:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return EDIT_PKG_LINK


async def edit_pkg_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    pkg_id = context.user_data["edit_pkg_id"]

    if text == "=":
        link = None  # pertahankan link lama
    elif text == "-":
        link = ""
    else:
        link = text

    db.edit_package(
        pkg_id,
        context.user_data["edit_pkg_name"],
        context.user_data["edit_pkg_price"],
        context.user_data["edit_pkg_duration"],
        link=link,
        target_chat_id=context.user_data.get("edit_pkg_chatid"),
    )
    await update.message.reply_text("✅ Paket berhasil diperbarui.", reply_markup=settings_menu_kb())
    for k in ("edit_pkg_id", "edit_pkg_name", "edit_pkg_price", "edit_pkg_duration", "edit_pkg_chatid"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END


# ── 📢 Broadcast ─────────────────────────────────────────────────────────

async def broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima konten broadcast dari admin (teks atau foto+caption), simpan
    sementara, lalu tampilkan preview + tombol konfirmasi kirim/batal."""
    msg = update.message
    if msg.photo:
        payload = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": html_of_caption(msg)}
        preview_prefix = "🖼️ <b>Preview foto broadcast:</b>\n"
    else:
        if not msg.text:
            await update.message.reply_text("Kirim teks atau foto ya. Coba lagi:", reply_markup=back_kb())
            return BROADCAST_WAIT
        payload = {"type": "text", "text": html_of_text(msg)}
        preview_prefix = "📝 <b>Preview pesan broadcast:</b>\n"

    context.user_data["broadcast_payload"] = payload
    target_count = len(sb.get_broadcast_user_ids())

    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅💎 Kirim ke {target_count} user", callback_data="broadcast_send")],
        [InlineKeyboardButton("🔙 Batal", callback_data="broadcast_cancel")],
    ])

    if payload["type"] == "photo":
        await update.message.reply_photo(
            photo=payload["file_id"],
            caption=preview_prefix + (payload["caption"] or "<i>(tanpa caption)</i>"),
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_kb,
        )
    else:
        await update.message.reply_text(
            preview_prefix + payload["text"], parse_mode=ParseMode.HTML, reply_markup=confirm_kb
        )
    return BROADCAST_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return ConversationHandler.END

    if query.data == "broadcast_cancel":
        context.user_data.pop("broadcast_payload", None)
        await query.edit_message_text("Broadcast dibatalkan.")
        await context.bot.send_message(
            query.from_user.id, "⚙️✨ *Menu Pengaturan Bot*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_menu_kb()
        )
        return ConversationHandler.END

    payload = context.user_data.get("broadcast_payload")
    if not payload:
        await query.edit_message_text("Sesi broadcast kedaluwarsa, silakan ulangi.")
        return ConversationHandler.END

    await query.edit_message_text("📤 Mengirim broadcast, mohon tunggu...")

    user_ids = sb.get_broadcast_user_ids()
    success, failed = 0, 0
    for uid in user_ids:
        try:
            if payload["type"] == "photo":
                await context.bot.send_photo(
                    uid, photo=payload["file_id"], caption=payload["caption"] or None, parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(uid, payload["text"], parse_mode=ParseMode.HTML)
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast gagal terkirim ke user {uid}: {e}")
        await asyncio.sleep(0.05)  # jaga rate limit Telegram (~20-30 pesan/detik ke chat berbeda)

    report = (
        f"📢✅ *Broadcast selesai*\n\n"
        f"Berhasil: *{success}*\n"
        f"Gagal: *{failed}*\n"
        f"Total target: *{len(user_ids)}*"
    )
    await context.bot.send_message(query.from_user.id, report, parse_mode=ParseMode.MARKDOWN, reply_markup=settings_menu_kb())
    if config.LOG_CHAT_ID:
        await context.bot.send_message(config.LOG_CHAT_ID, report, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop("broadcast_payload", None)
    return ConversationHandler.END


def main():
    db.init_db()
    # connect_timeout dinaikkan (default PTB cukup ketat, ~5 detik) supaya tidak
    # gampang TimedOut saat container baru cold-start dan jaringannya belum "panas"
    # (umum terjadi di awal deploy Railway). Kalau tetap gagal di percobaan pertama,
    # restart policy Railway (lihat railway.json) akan tetap jadi jaring pengaman.
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )
    app = Application.builder().token(config.BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(
        main_menu_callback, pattern="^(show_vip|back_main|my_status|buy_\\d+)$"
    ))
    app.add_handler(CallbackQueryHandler(admin_manual_decision, pattern="^admin_(approve|reject)_\\d+$"))

    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_cmd),
            CallbackQueryHandler(settings_router, pattern="^(set_greeting|set_vip_text|set_qris|set_qris_caption|set_success_text|set_reject_text|set_watermark|set_testi_caption|add_package|edit_package|delete_package|delpkg_\\d+|editpkg_\\d+|settings_back|settings_close|settings_stats|settings_broadcast)$"),
            # Tombol "Kembali/Batal" (settings_cancel) juga didaftarkan sebagai entry
            # point, bukan cuma di dalam states={...} di bawah. Alasannya: beberapa
            # layar (mis. Statistik, atau daftar paket saat "Hapus Paket") sengaja
            # meng-END-kan ConversationHandler begitu ditampilkan (karena tidak perlu
            # melacak state lanjutan), tapi tombol "Kembali ke Menu Settings" di
            # layar itu tetap memakai callback_data="settings_cancel". Tanpa entry
            # point ini, begitu ConversationHandler sudah END, klik tombol itu tidak
            # tertangkap oleh state manapun -> tombol terlihat "tidak berfungsi".
            # Dengan didaftarkan di sini, tombol itu SELALU tertangkap, baik saat
            # masih di tengah state manapun, maupun setelah conversation berakhir.
            CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
        ],
        states={
            SET_GREETING: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_greeting),
            ],
            SET_VIP_TEXT: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_vip_text),
            ],
            SET_QRIS: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.PHOTO, save_qris),
            ],
            SET_QRIS_CAPTION: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_qris_caption),
            ],
            SET_SUCCESS_TEXT: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_success_text),
            ],
            SET_REJECT_TEXT: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_reject_text),
            ],
            SET_WATERMARK: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.Sticker.ALL, save_watermark),
            ],
            SET_TESTI_CAPTION: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_testi_caption),
            ],
            ADD_PKG_NAME: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_name),
            ],
            ADD_PKG_PRICE: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_price),
            ],
            ADD_PKG_DURATION: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_duration),
            ],
            ADD_PKG_DESC: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_desc),
            ],
            ADD_PKG_CHATID: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_chatid),
            ],
            ADD_PKG_LINK: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_pkg_link),
            ],
            EDIT_PKG_PICK: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                CallbackQueryHandler(settings_router, pattern="^editpkg_\\d+$"),
            ],
            EDIT_PKG_NAME: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_name),
            ],
            EDIT_PKG_PRICE: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_price),
            ],
            EDIT_PKG_DURATION: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_duration),
            ],
            EDIT_PKG_CHATID: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_chatid),
            ],
            EDIT_PKG_LINK: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pkg_link),
            ],
            BROADCAST_WAIT: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, broadcast_receive),
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(broadcast_confirm, pattern="^(broadcast_send|broadcast_cancel)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(settings_conv)

    # Didaftarkan SETELAH settings_conv dengan sengaja: kalau admin sedang di
    # tengah alur /settings (misalnya state SET_QRIS menunggu upload foto QRIS),
    # settings_conv harus lebih dulu "mengklaim" pesan foto itu. Kalau tidak ada
    # percakapan /settings yang aktif, ConversationHandler otomatis tidak match,
    # dan foto akan jatuh ke sini sebagai bukti transfer normal dari pembeli.
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_proof_photo))

    # Handler notifikasi saldo masuk yang diteruskan dari HP admin (DANA Pribadi & Bisnis)
    notif_chat_ids = [cid for cid in (config.NOTIF_PERSONAL_CHAT_ID, config.NOTIF_BUSINESS_CHAT_ID) if cid]
    if notif_chat_ids:
        app.add_handler(MessageHandler(
            filters.TEXT & filters.Chat(chat_id=notif_chat_ids) & ~filters.COMMAND,
            handle_incoming_notification,
        ))

    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
