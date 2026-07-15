"""
bot.py
======
Bot Telegram VIP dengan auto-approve/reject bukti transfer.

Alur pembayaran:
1. User /start -> lihat teks sapaan + menu.
2. User pilih "Lihat Paket VIP" -> tabel paket VIP (custom via /settings).
3. User pilih salah satu paket -> bot kirim QRIS + nominal unik untuk dibayar.
4. User upload foto bukti transfer -> bot OCR gambar itu, cocokkan LOKAL ke
   nama penerima QRIS + tanggal + nominal (verify_proof_locally(), TIDAK ada
   panggilan ke API/pihak ketiga mana pun).
5. Kalau cocok -> otomatis APPROVE, VIP langsung aktif, tanpa admin pencet apa pun.
   Kalau tidak cocok -> otomatis REJECT + alasan, dengan opsi diteruskan ke admin
   untuk review manual (tombol Approve/Reject, lihat confirm_proof_keyboard()).

Jalankan dengan: python bot.py
"""

import os
import json
import html
import time
import logging
import datetime
import asyncio
import warnings

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning
from PIL import Image

from easing import Animator, ease_out_cubic, ease_out_elastic, render_bar
import api_server

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
import qris_dinamis
import keyboards as kb
import stats_broadcast as sb
import watermark
import rich_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Conversation states (untuk menu /settings admin) ───────────────────────
(
    SET_GREETING, SET_VIP_TEXT, SET_QRIS,
    SET_QRIS_CAPTION, SET_SUCCESS_TEXT, SET_REJECT_TEXT,
    SET_WATERMARK, SET_TESTI_CAPTION, SET_STATIC_LINK,
    ADD_PKG_NAME, ADD_PKG_PRICE, ADD_PKG_DURATION, ADD_PKG_DESC, ADD_PKG_CHATID,
    EDIT_PKG_PICK, EDIT_PKG_NAME, EDIT_PKG_PRICE, EDIT_PKG_DURATION, EDIT_PKG_CHATID,
    BROADCAST_WAIT, BROADCAST_CONFIRM,
) = range(21)



def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def generate_unique_code(base_amount: int, tx_counter: int) -> tuple[int, str]:
    """Buat nominal unik (contoh: 50000 -> 50123) berbasis `tx_id`, supaya
    matching bukti transfer (nama+tanggal+nominal, lihat verify_proof_locally())
    lebih presisi saat banyak user membayar nominal dasar yang sama secara
    bersamaan. Kembalikan (nominal_final, kode_unik_3_digit). Murni lokal --
    tidak memanggil pihak ketiga mana pun."""
    unique_suffix = (tx_counter % 899) + 100  # angka 100-998
    final_amount = base_amount + unique_suffix
    return final_amount, str(unique_suffix)


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
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="back_main", style="danger")],
        [InlineKeyboardButton("💬 Hubungi Admin", url=f"https://t.me/{CONTACT_USERNAME}", style="primary")],
    ])


# ── Tombol "Kembali" untuk semua langkah di dalam /settings ────────────────

def back_kb() -> InlineKeyboardMarkup:
    """Keyboard sederhana berisi 1 tombol untuk batal & kembali ke menu settings."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Batal & Kembali ke Menu Settings", callback_data="settings_cancel", style="danger")]])


def with_back(markup):
    """Tambahkan baris tombol 'Kembali' di bawah keyboard yang sudah ada (mis. daftar paket)."""
    rows = list(markup.inline_keyboard) if markup else []
    rows.append([InlineKeyboardButton("🔙 Kembali ke Menu Settings", callback_data="settings_cancel", style="danger")])
    return InlineKeyboardMarkup(rows)


# ── Transisi antar menu: Ease-Out Cubic (bukan Lerp/linear lagi) ────────────
# Telegram Bot API tidak punya animasi visual asli (bukan canvas/web UI), jadi
# "gerakan" di sini disimulasikan lewat beberapa kali edit pesan berisi progress
# bar yang mengisi mengikuti kurva Ease-Out Cubic: cepat di awal, melambat halus
# menjelang akhir ("slow in, slow out" -- salah satu dari 12 prinsip animasi
# Disney, juga dasar easing curve standar di iOS/Material Design). Ini jelas
# lebih "berbobot" dibanding versi lama yang cuma satu jeda linear ("· · ·").
#
# Timeline dihitung pakai Animator (easing.py) yang berbasis delta-time NYATA
# (time.monotonic()), bukan asumsi tiap frame delay-nya sama persis -- jadi
# progress tetap presisi walau ada jeda jaringan saat memanggil Telegram API.
#
# Jumlah frame & total durasi SENGAJA dijaga tetap kecil (di bawah ini) supaya
# tidak melanggar rate-limit edit pesan Telegram (flood control) -- kalau
# Telegram sempat membalas RetryAfter, animasi langsung dihentikan dan pesan
# akhir tetap ditampilkan seperti biasa (menu tidak pernah gagal tampil hanya
# gara-gara animasinya kena limit).
FADE_DURATION = 0.6     # detik, total durasi animasi reveal
FADE_FRAME_TICK = 0.12  # detik, jarak antar sampling progress (~5 frame)
FADE_BAR_WIDTH = 10


async def fade_transition(query, text: str, **kwargs):
    """Ganti isi pesan (lewat query.edit_message_text) dengan animasi reveal
    berbasis Ease-Out Cubic (progress bar yang mengisi lalu melambat & settle),
    baru menampilkan konten menu yang sebenarnya. `kwargs` diteruskan apa
    adanya ke edit_message_text akhir (parse_mode, reply_markup, dst).

    Kalau animasi gagal di tengah jalan (mis. pesan sumbernya foto/caption
    yang tidak boleh diedit pakai edit_message_text, atau Telegram membalas
    RetryAfter karena rate-limit), fungsi ini langsung lompat ke tahap akhir
    tanpa mengganggu jalannya menu -- animasi hanya "hiasan", bukan sesuatu
    yang boleh membuat menu gagal tampil."""
    anim = Animator(duration=FADE_DURATION, easing=ease_out_cubic)
    try:
        while not anim.is_done():
            bar = render_bar(anim.value(), width=FADE_BAR_WIDTH)
            await query.edit_message_text(bar)
            await asyncio.sleep(FADE_FRAME_TICK)
    except RetryAfter:
        pass  # kena flood-control -> langsung skip ke tahap akhir, jangan retry animasi
    except Exception:
        pass  # mis. BadRequest "message is not modified" / sumber pesan berupa foto
    await query.edit_message_text(text, **kwargs)


# ── Animasi "sedang memproses" saat verifikasi bukti transfer ──────────────
# Beda dengan fade_transition (durasi tetap/diketahui), proses verifikasi di
# sini (download foto -> hash -> OCR -> cross-check API DANA) durasinya TIDAK
# diketahui pasti. Karena itu animasinya dibuat "looping" (progress bar mengisi
# lalu reset dengan efek elastis di titik baliknya, Ease-Out Elastic) dan
# dijalankan sebagai asyncio.Task TERPISAH yang benar-benar berjalan PARALEL
# dengan kerja aslinya -- bukan animasi pura-pura yang jalan sendiri lepas dari
# progres asli. Dihentikan dari luar (stop_processing_animation) begitu hasil
# verifikasi sudah didapat.
PROCESSING_ANIM_TICK = 0.4          # detik antar edit pesan selama animasi berjalan
PROCESSING_ANIM_CYCLE = 1.2         # detik, durasi satu siklus isi -> reset progress bar
PROCESSING_ANIM_MAX_DURATION = 25.0  # detik, jaring pengaman terakhir (lihat docstring di bawah)


async def animate_processing(message, label: str):
    """Jalankan animasi Ease-Out Elastic di `message` sampai di-cancel dari luar.

    `PROCESSING_ANIM_MAX_DURATION` adalah jaring pengaman: kalau task ini lupa
    di-cancel (mis. ada exception tak terduga di tengah proses verifikasi yang
    membuat alur normal tidak sempat memanggil stop_processing_animation),
    animasi akan berhenti sendiri setelah durasi maksimum itu -- supaya tidak
    mengedit pesan yang sama tanpa henti dan menyedot rate-limit Telegram
    selamanya."""
    overall_start = time.monotonic()
    cycle = 0
    try:
        while (time.monotonic() - overall_start) < PROCESSING_ANIM_MAX_DURATION:
            anim = Animator(duration=PROCESSING_ANIM_CYCLE, easing=ease_out_elastic)
            dots = "." * ((cycle % 3) + 1)
            while not anim.is_done():
                bar = render_bar(anim.value(), width=10)
                await message.edit_text(f"{label}{dots}\n{bar}")
                await asyncio.sleep(PROCESSING_ANIM_TICK)
            cycle += 1
    except asyncio.CancelledError:
        raise
    except RetryAfter:
        return
    except Exception:
        return


async def stop_processing_animation(task: "asyncio.Task | None"):
    """Hentikan task animate_processing dengan aman. Dipanggil TEPAT SEBELUM
    setiap edit_text hasil akhir verifikasi (approve/reject/pending/duplikat),
    supaya animasi tidak terus mengedit pesan yang sudah berisi hasil final."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


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
    mengubah keyboards.py (menyisipkan baris tombol tambahan di atas baris
    "❎ Tutup" milik menu dasar). Statistik & Broadcast sengaja digabung jadi
    satu baris (sejajar 2 kolom) supaya konsisten dengan tombol-tombol lain di
    menu ini yang juga berpasangan 2, bukan menumpuk 1 tombol per baris --
    dan "Tutup" tetap dijaga jadi baris PALING BAWAH (bukan malah tertindih
    di tengah oleh baris yang disisipkan)."""
    base = kb.settings_menu_keyboard()
    rows = list(base.inline_keyboard) if base else []

    # Baris terakhir menu dasar adalah "❎ Tutup" -- pisahkan dulu supaya baris
    # baru (Statistik + Broadcast) bisa disisipkan SEBELUM Tutup, bukan sesudahnya.
    tutup_row = rows.pop() if rows else [InlineKeyboardButton("❎ Tutup", callback_data="settings_close", style="danger")]

    rows.append([
        InlineKeyboardButton("📊✨ Statistik Bot", callback_data="settings_stats", style="primary"),
        InlineKeyboardButton("📢💎 Broadcast Pesan", callback_data="settings_broadcast", style="primary"),
    ])
    rows.append(tutup_row)
    return InlineKeyboardMarkup(rows)


# ── /start & menu utama ─────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    greeting = db.get_setting("greeting_text", db.DEFAULT_GREETING)
    await update.message.reply_text(
        greeting, parse_mode=ParseMode.HTML, reply_markup=kb.main_menu_keyboard()
    )

    # Tombol Mini App "Lihat Paket VIP" HARUS dikirim lewat reply keyboard
    # (bukan inline) supaya Telegram.WebApp.sendData() di halamannya benar-
    # benar sampai ke handle_webapp_data() -- lihat catatan di
    # keyboards.py::main_menu_keyboard(). Reply keyboard tidak bisa dipasang
    # bareng di pesan yang sama dengan inline keyboard di atas, jadi dikirim
    # sebagai pesan kecil terpisah. Sekali terkirim, tombol ini akan tetap
    # "menempel" di bawah kolom chat user (persistent) sampai dihapus lewat
    # ReplyKeyboardRemove -- jadi tidak perlu dikirim ulang tiap menu dibuka.
    webapp_kb = kb.webapp_launch_keyboard()
    if webapp_kb:
        await update.message.reply_text(
            "Atau tekan tombol di bawah ini untuk melihat katalog VIP dalam tampilan tabel (Mini App):",
            reply_markup=webapp_kb,
        )


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "show_vip":
        vip_text = db.get_setting("vip_menu_text", db.DEFAULT_VIP_INTRO)

        # Coba dulu versi Rich HTML (Bot API 10.1 sendRichMessage, tabel
        # <table> asli, bukan <pre> box-drawing) -- lihat catatan lengkap di
        # keyboards.py::format_vip_table_rich() & rich_api.py. Kalau gagal
        # (server Bot API belum rollout method ini, error jaringan, dll),
        # otomatis fallback ke tabel teks lama (format_vip_table()) supaya
        # bot tetap jalan normal.
        try:
            table_html = await kb.format_vip_table_rich(context.bot)
            await rich_api.send_rich_message(
                query.message.chat_id,
                f"<p>{vip_text}</p>{table_html}",
                reply_markup=kb.vip_list_keyboard(),
            )
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning(f"sendRichMessage untuk show_vip gagal, fallback ke tabel teks biasa: {e}")

        table = await kb.format_vip_table(context.bot)
        text = f"{vip_text}\n\n{table}"
        try:
            await fade_transition(
                query, text, parse_mode=ParseMode.HTML, reply_markup=kb.vip_list_keyboard(),
            )
        except Exception:
            # Tombol "show_vip" ini bisa dipencet dari tampilan scan QRIS, yang
            # pesannya berupa FOTO (edit_message_text tidak berlaku untuk pesan
            # foto/caption) -> fallback: hapus pesan lama, kirim pesan baru.
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                query.message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb.vip_list_keyboard(),
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
        if query.message is not None:
            # Tombol biasa (dari pesan normal yang dikirim bot, mis. daftar
            # paket VIP di chat) -> chat_id & source_message seperti biasa.
            await start_purchase_flow(
                query.message.chat_id, query.from_user, context, pkg_id,
                source_message=query.message,
            )
        else:
            # Tombol ini berasal dari pesan hasil answerWebAppQuery (Mini App
            # yang dibuka lewat Menu Button, lihat api_server.py::
            # handle_select_package()) -- untuk pesan jenis ini Telegram TIDAK
            # menyertakan `query.message` sama sekali (cuma inline_message_id,
            # lihat https://core.telegram.org/bots/api#callbackquery), jadi
            # `query.message.chat_id` akan selalu crash (AttributeError).
            # chat_id diambil dari query.from_user.id -- aman karena alur ini
            # SELALU terjadi di private chat 1-on-1 dengan bot, dan di
            # Telegram chat_id private chat == user_id lawan bicaranya.
            # source_message=None karena tidak ada message_id yang bisa
            # dipakai untuk menghapus pesan ini lewat Bot API.
            await start_purchase_flow(
                query.from_user.id, query.from_user, context, pkg_id,
                source_message=None,
            )


async def start_purchase_flow(chat_id: int, telegram_user, context: ContextTypes.DEFAULT_TYPE,
                               pkg_id: int, source_message=None):
    """Alur mulai pembelian paket (buat transaksi + kirim QRIS & nominal unik).

    Direfactor jadi fungsi terpisah (bukan inline di dalam handler tombol)
    supaya bisa dipakai dari DUA sumber trigger yang beda bentuknya:
    1. Tombol inline biasa "buy_<id>" (callback query, ada `query.message`).
    2. Data dari Telegram Mini App "Lihat Paket VIP" (pesan biasa berisi
       `web_app_data`, TIDAK ada callback query/`query.message` sama sekali)
       -- lihat handle_webapp_data() di bawah.

    `source_message`, kalau diisi, akan dihapus setelah QRIS terkirim (dipakai
    supaya pesan daftar paket lama tidak menumpuk di chat -- perilaku yang
    sama seperti sebelum refactor ini)."""
    pkg = db.get_package(pkg_id)
    if not pkg:
        await context.bot.send_message(chat_id, "Paket tidak ditemukan / sudah tidak aktif.")
        return

    # Buat transaksi DULU dengan nominal sementara (harga dasar, belum ada
    # kode unik) supaya dapat `tx_id` dari database -- baru kode unik &
    # nominal final dihitung BERBASIS `tx_id` ini (lihat generate_unique_code()
    # & database.set_transaction_amount() untuk alasan kenapa harus begini,
    # bukan pakai counter di memori seperti sebelumnya).
    username = telegram_user.username or telegram_user.first_name
    tx_id = db.create_transaction(telegram_user.id, username, pkg_id, pkg["price"], "")
    context.user_data["pending_tx_id"] = tx_id

    final_amount, unique_code = generate_unique_code(pkg["price"], tx_id)
    db.set_transaction_amount(tx_id, final_amount, unique_code)

    qris_path = config.QRIS_IMAGE_PATH
    caption_template = db.get_setting("qris_caption_text", db.DEFAULT_QRIS_CAPTION)
    caption = render_template(
        caption_template,
        package=pkg["name"],
        duration=pkg["duration_days"],
        amount=rupiah(final_amount),
    )

    # Kalau QRIS statis berhasil di-decode saat admin upload (lihat save_qris()),
    # kita generate QRIS DINAMIS baru dengan nominal `final_amount` SUDAH
    # ter-embed di dalam kode QR-nya -- user tinggal scan & konfirmasi bayar
    # di app-nya, TANPA perlu mengetik nominal manual sama sekali. Nominal unik
    # (unique_code) tetap dipakai persis seperti sebelumnya untuk pencocokan
    # otomatis lewat verify_proof_locally() (nama+tanggal+nominal, lihat di
    # bawah) -- cuma cara usernya bayar yang berubah jadi lebih gampang.
    static_qris_string = db.get_setting("qris_static_string", "")
    dynamic_qris_sent = False

    if static_qris_string:
        try:
            dynamic_str = qris_dinamis.inject_amount(static_qris_string, final_amount)
            dynamic_qris_path = os.path.join(
                os.path.dirname(config.QRIS_IMAGE_PATH) or ".", f"qris_dinamis_tx{tx_id}.png"
            )
            qris_dinamis.generate_qris_image(dynamic_str, dynamic_qris_path)
            with open(dynamic_qris_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id, photo=f, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=kb.qris_back_keyboard(),
                )
            dynamic_qris_sent = True
            try:
                os.remove(dynamic_qris_path)
            except OSError:
                pass
        except Exception as e:
            logger.error(f"Gagal generate QRIS dinamis untuk TX #{tx_id}, fallback ke QRIS statis: {e}")
            # Lanjut ke fallback di bawah -- jangan biarkan user tidak dapat QRIS sama sekali.

    if not dynamic_qris_sent:
        # Fallback: cara lama (QRIS statis + minta user transfer nominal
        # unik secara manual) -- dipakai kalau admin belum pernah upload QRIS
        # sejak fitur nominal-otomatis ini ada, atau generate QRIS dinamis
        # gagal karena sebab lain (mis. file gambar QRIS hilang/rusak).
        if os.path.exists(qris_path):
            with open(qris_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id, photo=f, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=kb.qris_back_keyboard(),
                )
        else:
            await context.bot.send_message(
                chat_id,
                caption + "\n\n<i>(QRIS belum diset oleh admin, hubungi admin untuk kode QRIS)</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb.qris_back_keyboard(),
            )

    if source_message is not None:
        try:
            await source_message.delete()
        except Exception:
            pass


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ditrigger saat user pilih paket dari Mini App "Lihat Paket VIP" (halaman
    HTML statis, mis. di-hosting GitHub Pages) -- WebApp mengirim data lewat
    `Telegram.WebApp.sendData(...)` di sisi JS, yang sampai ke bot sebagai
    pesan biasa berisi `web_app_data` (BUKAN callback query)."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        pkg_id = int(payload["package_id"])
    except Exception:
        await update.effective_message.reply_text(
            "Data dari halaman paket tidak terbaca. Coba buka lagi & pilih paketnya."
        )
        return
    await start_purchase_flow(update.effective_chat.id, update.effective_user, context, pkg_id)


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
    - Kalau paket tidak punya link sendiri (kolom `link` kosong), bot otomatis
      memakai *link akses statis global* yang diset SEKALI lewat /settings
      (tombol "🔗 Atur Link Akses Statis (Global)"), supaya admin tidak perlu
      input link yang sama berulang-ulang untuk tiap paket baru.
    """
    target_chat_id = (pkg["target_chat_id"] or "").strip()

    if target_chat_id:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=int(target_chat_id),
                member_limit=1,
                name=f"VIP-{pkg['name']}-{chat_id}"[:32],
            )
            # PENTING: pakai HTML, BUKAN Markdown, untuk pesan ini -- invite
            # link Telegram (contoh: https://t.me/+AbC_dEfGh...) hampir selalu
            # mengandung underscore, dan Markdown versi lama (legacy) akan
            # menganggap SATU underscore sebagai pembuka teks miring. Karena
            # tidak ada underscore penutup pasangannya, Telegram akan menolak
            # parsing seluruh pesan dengan error "Can't parse entities: can't
            # find end of the entity..." -- lalu error itu keliru "disalahkan"
            # ke create_chat_invite_link() di atas oleh except block, padahal
            # invite link-nya sendiri sudah berhasil dibuat. HTML tidak
            # bermasalah dengan underscore, jadi aman dipakai di sini.
            await context.bot.send_message(
                chat_id,
                f"🔗 <b>Akses {html.escape(pkg['name'])}</b>\n{html.escape(invite.invite_link)}\n\n"
                f"<i>Link ini dibuat khusus untukmu dan hanya bisa dipakai 1 kali oleh 1 akun. "
                f"Jangan bagikan ke orang lain karena akan otomatis tidak berlaku lagi setelah dipakai.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"Buka {pkg['name']}", url=invite.invite_link, style="success")]]
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

    # Prioritas: link khusus paket (kolom `link`) -> kalau kosong, pakai link
    # akses statis GLOBAL yang diset sekali lewat /settings.
    link = (pkg["link"] or "").strip()
    if not link:
        link = db.get_setting("static_access_link", "").strip()
    if not link:
        return
    is_url = link.startswith("http://") or link.startswith("https://")
    await context.bot.send_message(
        chat_id,
        f"🔗 <b>Akses {html.escape(pkg['name'])}</b>\n{html.escape(link)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"Buka {pkg['name']}", url=link, style="success")]]
        ) if is_url else None,
    )


# ── Terima & verifikasi bukti transfer (100% lokal, tanpa pihak ketiga) ──

def verify_proof_locally(ocr_result: dict, expected_amount: int) -> tuple[bool, str]:
    """Cocokkan hasil OCR bukti transfer ke 3 hal, MURNI lokal (tanpa API/
    notifikasi pihak ketiga mana pun):
    1. Nominal harus PERSIS sama dengan `expected_amount` (termasuk kode unik).
    2. Nama penerima (config.QRIS_RECIPIENT_NAME) harus terdeteksi di teks OCR
       (fuzzy match, lihat ocr_utils.name_matches) -- dilewati kalau admin
       belum mengisi QRIS_RECIPIENT_NAME.
    3. Tanggal transaksi (kalau terbaca) harus dalam rentang toleransi
       config.PROOF_DATE_TOLERANCE_HOURS jam dari sekarang -- mencegah bukti
       transfer lama/daur ulang dari transaksi lain.

    Return (matched: bool, reason: str) -- `reason` diisi HANYA kalau
    matched=False, untuk ditampilkan sebagai alasan reject ke user/admin."""
    if ocr_result["amount"] != expected_amount:
        return False, "Nominal pada bukti transfer tidak sesuai / tidak terbaca."

    if not ocr_utils.name_matches(config.QRIS_RECIPIENT_NAME, ocr_result["raw_text"]):
        return False, "Nama penerima pada bukti transfer tidak cocok dengan nama penerima QRIS terdaftar."

    proof_date = ocr_result.get("date")
    if proof_date is not None:
        now = datetime.datetime.utcnow()
        delta_hours = abs((now - proof_date).total_seconds()) / 3600
        if delta_hours > config.PROOF_DATE_TOLERANCE_HOURS:
            return False, (
                f"Tanggal/jam pada bukti transfer ({proof_date.strftime('%d %B %Y %H:%M')}) "
                f"di luar batas wajar (lebih dari {config.PROOF_DATE_TOLERANCE_HOURS} jam dari sekarang)."
            )

    return True, ""


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
    proof_anim_task = asyncio.create_task(
        animate_processing(processing_msg, "🔍 Memproses bukti transfer")
    )

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
        await stop_processing_animation(proof_anim_task)
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

    # 3) Verifikasi 100% LOKAL: cocokkan nama penerima + tanggal + nominal,
    #    semua murni dari hasil OCR gambar ini sendiri -- TIDAK ada panggilan
    #    ke API/pihak ketiga mana pun.
    matched, mismatch_reason = verify_proof_locally(ocr_result, tx["expected_amount"])

    if matched:
        # ── Kirim status berhasil ke grup log DULU, baru approve ──
        if config.LOG_CHAT_ID:
            await context.bot.send_message(
                config.LOG_CHAT_ID,
                f"✅ *Status Pembayaran: BERHASIL*\n"
                f"TX #{tx_id} | User: {user.id} ({username_display})\n"
                f"Paket: {pkg['name']} | Rp{tx['expected_amount']:,}\n"
                f"Terverifikasi via: OCR lokal (nama+tanggal+nominal)".replace(",", "."),
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
        await stop_processing_animation(proof_anim_task)
        await processing_msg.edit_text(success_text, parse_mode=ParseMode.HTML, reply_markup=result_kb())
        await send_package_link(update.effective_chat.id, context, pkg)
        await post_testimonial(context, tx, pkg)

    else:
        # ── AUTO REJECT ──
        reason = mismatch_reason
        db.set_transaction_status(tx_id, "rejected", reject_reason=reason)
        reject_template = db.get_setting("payment_reject_text", db.DEFAULT_PAYMENT_REJECT)
        reject_text = render_template(
            reject_template,
            package=pkg["name"],
            amount=rupiah(tx["expected_amount"]),
            reason=reason,
        )
        await stop_processing_animation(proof_anim_task)
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
    testi (config.TESTI_CHANNEL_ID). Urutan pemrosesan gambar: SENSOR dulu
    (no. rekening & nama pengirim dihitamkan, lihat ocr_utils.censor_sensitive_info
    -- demi privasi pembeli, karena publik yang lihat testimoni tidak perlu tahu
    itu), BARU watermark transparan (dikonversi dari stiker yang diset admin
    lewat /settings) ditempel di tengah gambar. Caption berisi nama paket + #testi.

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
        # 1) Sensor dulu (no. rekening/nama pengirim) -- kalau gagal (mis. OCR
        # layout tidak terbaca), tetap lanjut pakai gambar asli (belum tersensor)
        # daripada tidak posting sama sekali.
        try:
            censored_path = os.path.join(config.PROOF_IMAGES_DIR, f"censored_{tx['id']}.jpg")
            ocr_utils.censor_sensitive_info(proof_path, censored_path)
            output_path = censored_path
        except Exception as e:
            logger.warning(f"Testi: gagal sensor info sensitif TX #{tx['id']}, pakai gambar asli: {e}")
            output_path = proof_path

        # 2) Baru tempel watermark di atas hasil sensor (kalau watermark belum
        # diset admin, tetap posting apa adanya -- sudah tersensor -- daripada
        # tidak posting sama sekali).
        if os.path.exists(config.WATERMARK_IMAGE_PATH):
            watermarked_path = os.path.join(config.PROOF_IMAGES_DIR, f"testi_{tx['id']}.jpg")
            watermark.apply_watermark(output_path, config.WATERMARK_IMAGE_PATH, watermarked_path)
            output_path = watermarked_path

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
        await query.edit_message_text("Transaksi tidak ditemukan.")
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
        await query.edit_message_text(f"✅ TX #{tx_id} disetujui manual oleh admin.")
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
        await query.edit_message_text(f"❌ TX #{tx_id} ditolak manual oleh admin.")


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

    if data == "set_static_link":
        current = db.get_setting("static_access_link", "") or "(belum diset)"
        await fade_transition(
            query,
            "Kirim <b>link akses statis GLOBAL</b> yang baru.\n\n"
            "Link ini otomatis dipakai sebagai akses untuk <b>semua paket VIP</b> "
            "yang tidak diset grup Telegram (Chat ID) dan tidak punya link khusus "
            "sendiri — jadi cukup diisi <b>SEKALI di sini</b>, tidak perlu diulang "
            "tiap tambah/edit paket.\n\n"
            "Kirim '-' untuk mengosongkan.\n\n"
            f"Link saat ini: {html.escape(current)}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return SET_STATIC_LINK

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


async def save_static_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    link = "" if text == "-" else text
    db.set_setting("static_access_link", link)
    await update.message.reply_text(
        "✅ Link akses statis global berhasil diperbarui. Link ini otomatis dipakai "
        "untuk semua paket yang tidak punya grup Telegram / link khusus sendiri.",
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

    # Decode SEKALI di sini (bukan setiap kali ada pembelian) supaya QRIS
    # dinamis per-transaksi bisa dibuat instan tanpa perlu baca ulang file
    # gambar tiap kali user checkout -- lihat start_purchase_flow() &
    # qris_dinamis.py untuk detail cara kerja konversi statis -> dinamisnya.
    try:
        static_qris_string = qris_dinamis.decode_qris_image(config.QRIS_IMAGE_PATH)
        if not qris_dinamis.is_static_qris(static_qris_string):
            db.set_setting("qris_static_string", "")
            await update.message.reply_text(
                "⚠️ QRIS berhasil disimpan sebagai gambar, TAPI kode QR ini "
                "terdeteksi sudah DINAMIS (bukan QRIS statis biasa dari akun "
                "DANA Bisnis kamu). Fitur nominal-otomatis TIDAK bisa dipakai "
                "untuk QRIS jenis ini -- bot akan pakai cara lama (nominal "
                "unik manual). Upload ulang dengan QRIS STATIS kalau mau "
                "fitur nominal-otomatis aktif.",
                reply_markup=settings_menu_kb(),
            )
            return ConversationHandler.END
        db.set_setting("qris_static_string", static_qris_string)
        await update.message.reply_text(
            "✅ QRIS berhasil diperbarui. Nominal-otomatis AKTIF -- user akan "
            "menerima QR dengan nominal sudah terisi otomatis, tinggal scan & bayar.",
            reply_markup=settings_menu_kb(),
        )
    except qris_dinamis.QRISDecodeError as e:
        # Tetap simpan gambarnya (supaya bot tidak "kosong QRIS-nya"), tapi
        # nonaktifkan mode nominal-otomatis & jelaskan alasannya ke admin.
        db.set_setting("qris_static_string", "")
        await update.message.reply_text(
            f"⚠️ QRIS berhasil disimpan sebagai gambar, TAPI kode QR-nya gagal "
            f"dibaca ({e}). Fitur nominal-otomatis TIDAK aktif untuk gambar "
            f"ini -- bot akan pakai cara lama (nominal unik manual) sampai "
            f"kamu upload ulang foto QRIS yang lebih jelas.",
            reply_markup=settings_menu_kb(),
        )
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
        "Kirim '-' kalau paket ini tidak pakai grup Telegram — paket akan otomatis memakai "
        "*link akses statis global* yang sudah kamu set lewat menu \"🔗 Atur Link Akses Statis "
        "(Global)\" (tidak perlu diinput lagi di sini):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return ADD_PKG_CHATID


async def add_pkg_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    p = context.user_data["new_pkg"]
    p["target_chat_id"] = "" if text == "-" else text
    db.add_package(p["name"], p["price"], p["duration_days"], p["description"], "", p.get("target_chat_id", ""))
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
        "Masukkan *Chat ID baru* (bot harus jadi admin di sana), kirim '-' untuk mengosongkan "
        "(paket akan otomatis memakai *link akses statis global* dari menu \"🔗 Atur Link Akses "
        "Statis (Global)\"), atau kirim '=' untuk membiarkan tetap sama:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return EDIT_PKG_CHATID


async def edit_pkg_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "=":
        chatid = None  # pertahankan nilai lama
    elif text == "-":
        chatid = ""
    else:
        chatid = text

    db.edit_package(
        context.user_data["edit_pkg_id"],
        context.user_data["edit_pkg_name"],
        context.user_data["edit_pkg_price"],
        context.user_data["edit_pkg_duration"],
        link=None,  # pertahankan link khusus paket (kalau ada) apa adanya
        target_chat_id=chatid,
    )
    await update.message.reply_text("✅ Paket berhasil diperbarui.", reply_markup=settings_menu_kb())
    for k in ("edit_pkg_id", "edit_pkg_name", "edit_pkg_price", "edit_pkg_duration"):
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
        [InlineKeyboardButton(f"✅💎 Kirim ke {target_count} user", callback_data="broadcast_send", style="success")],
        [InlineKeyboardButton("🔙 Batal", callback_data="broadcast_cancel", style="danger")],
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler -- SEBELUM ini ditambahkan, exception apa pun yang
    terjadi di dalam handler (mis. handle_webapp_data) hanya tercatat diam-diam
    di log PTB internal, TANPA pemberitahuan apapun ke user (persis gejala
    'tekan tombol Pilih di Mini App, tidak ada respon apapun'). Sekarang:
    1. Traceback LENGKAP selalu dicetak ke log (mudah dicari, ada prefix jelas).
    2. Kalau ada chat yang jelas terkait (update berupa Update object dengan
       effective_chat), user dikirimi pesan singkat supaya tahu ada yang gagal
       -- bukan cuma diam tanpa respon seolah bot tidak berfungsi."""
    logger.error("Unhandled exception saat memproses update:", exc_info=context.error)

    chat_id = None
    if isinstance(update, Update):
        if update.effective_chat:
            chat_id = update.effective_chat.id
        elif update.callback_query and update.callback_query.from_user:
            # Callback query dari pesan inline (mis. hasil answerWebAppQuery,
            # lihat catatan di main_menu_callback::buy_) tidak punya
            # effective_chat -- fallback ke from_user.id, aman karena semua
            # alur Mini App bot ini selalu terjadi di private chat 1-on-1.
            chat_id = update.callback_query.from_user.id

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id,
                "⚠️ Terjadi kesalahan saat memproses permintaanmu. Coba lagi, atau hubungi admin kalau berulang.",
            )
        except Exception:
            pass  # kalau bahkan kirim pesan error ini gagal, jangan sampai bikin exception baru


async def on_startup(app_: Application):
    """post_init: jalan SEKALI setelah bot siap tapi sebelum polling mulai.
    Kalau admin sudah setup WEBAPP_URL, nyalakan server API kecil (lihat
    api_server.py) di event loop yang SAMA -- supaya cuma perlu 1 proses/
    service di Railway, bukan 2."""
    if config.WEBAPP_URL:
        await api_server.start_api_server(config.PORT)
    else:
        logger.info("WEBAPP_URL belum diisi -> Mini App 'Lihat Paket VIP' nonaktif, fallback ke tabel teks di chat.")


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
    app = Application.builder().token(config.BOT_TOKEN).request(request).post_init(on_startup).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(CallbackQueryHandler(
        main_menu_callback, pattern="^(show_vip|back_main|my_status|buy_\\d+)$"
    ))
    app.add_handler(CallbackQueryHandler(admin_manual_decision, pattern="^admin_(approve|reject)_\\d+$"))

    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_cmd),
            CallbackQueryHandler(settings_router, pattern="^(set_greeting|set_vip_text|set_qris|set_qris_caption|set_success_text|set_reject_text|set_watermark|set_testi_caption|set_static_link|add_package|edit_package|delete_package|delpkg_\\d+|editpkg_\\d+|settings_back|settings_close|settings_stats|settings_broadcast)$"),
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
            SET_STATIC_LINK: [
                CallbackQueryHandler(settings_cancel, pattern="^settings_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_static_link),
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

    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
