"""
typing_animation.py
--------------------
Modul kecil yang membuat SETIAP kali bot mau menampilkan chat bubble
(baik pesan baru maupun edit pesan) diawali dengan animasi "sedang
mengetik..." / "sedang mengirim foto..." dsb -- persis seperti animasi
proses AI (mis. ChatGPT) sebelum jawabannya muncul.

Cara pakai (cukup 1 baris, dipanggil SEKALI saat bot start):

    import typing_animation
    typing_animation.enable()

Setelah dipanggil, SEMUA pemanggilan berikut otomatis kebagian animasi,
tanpa perlu mengubah satu pun baris kode di bot.py / keyboards.py / dll:

    - context.bot.send_message(...)      -> animasi "mengetik..."
    - update.message.reply_text(...)     -> animasi "mengetik..." (lewat send_message)
    - context.bot.send_photo(...)        -> animasi "mengunggah foto..."
    - context.bot.send_document(...)     -> animasi "mengunggah dokumen..."
    - context.bot.send_video(...)        -> animasi "mengunggah video..."
    - context.bot.send_animation(...)    -> animasi "mengunggah video..."
    - context.bot.send_voice(...)        -> animasi "merekam suara..."
    - context.bot.edit_message_text(...) -> animasi "mengetik..." sebelum bubble berubah
    - context.bot.edit_message_caption(...) / edit_message_media(...) -> idem

Kenapa dipatch di level python-telegram-bot (class Bot), bukan diedit
satu-satu di bot.py? Karena ada 70+ titik pengiriman pesan tersebar di
banyak fungsi (reply_text, context.bot.send_*, edit_message_text, dst).
Mem-patch class Bot sekali di sini jauh lebih aman & gampang dirawat
daripada menyisipkan send_chat_action() + sleep() manual di 70+ tempat.
"""

import asyncio
import functools
import inspect
import logging

from telegram import Bot
from telegram.constants import ChatAction

logger = logging.getLogger(__name__)

# Lama animasi (detik). Disesuaikan kasar dari panjang teks supaya terasa
# alami: teks pendek -> animasi sebentar, teks panjang -> animasi agak lama.
# Tetap dibatasi MIN & MAX supaya bot tidak terasa lambat/lemot.
_MIN_DELAY = 0.5
_MAX_DELAY = 1.8
_CHARS_PER_SECOND = 60

# Method Bot yang akan "dibungkus" animasi, beserta jenis chat action-nya.
# (ChatAction inilah yang membuat user melihat status "sedang mengetik...",
# "sedang mengirim foto...", dst di chat Telegram mereka.)
_PATCH_TARGETS = {
    "send_message": ChatAction.TYPING,
    "edit_message_text": ChatAction.TYPING,
    "edit_message_caption": ChatAction.TYPING,
    "edit_message_media": ChatAction.UPLOAD_PHOTO,
    "send_photo": ChatAction.UPLOAD_PHOTO,
    "send_document": ChatAction.UPLOAD_DOCUMENT,
    "send_video": ChatAction.UPLOAD_VIDEO,
    "send_animation": ChatAction.UPLOAD_VIDEO,
    "send_voice": ChatAction.RECORD_VOICE,
    "send_audio": ChatAction.UPLOAD_VOICE,
}

_ENABLED = False


def _compute_delay(text):
    """Hitung lama animasi berdasarkan panjang teks/caption (kalau ada)."""
    if not text:
        return _MIN_DELAY
    try:
        length = len(str(text))
    except Exception:
        return _MIN_DELAY
    delay = length / _CHARS_PER_SECOND
    return max(_MIN_DELAY, min(_MAX_DELAY, delay))


def _extract(original, self_obj, args, kwargs):
    """Ambil chat_id & text/caption dari pemanggilan, baik yang dikirim
    secara positional maupun keyword, tanpa perlu tahu urutan pastinya."""
    try:
        sig = inspect.signature(original)
        bound = sig.bind_partial(self_obj, *args, **kwargs)
        bound.apply_defaults()
        chat_id = bound.arguments.get("chat_id")
        text = bound.arguments.get("text")
        if text is None:
            text = bound.arguments.get("caption")
        return chat_id, text
    except Exception:
        chat_id = kwargs.get("chat_id")
        text = kwargs.get("text") or kwargs.get("caption")
        return chat_id, text


def _make_wrapper(method_name, action):
    original = getattr(Bot, method_name)

    if getattr(original, "_is_typing_animation_wrapper", False):
        # Sudah pernah dipatch sebelumnya -> jangan dobel.
        return None

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        chat_id, text = _extract(original, self, args, kwargs)

        if chat_id is not None:
            try:
                await self.send_chat_action(chat_id=chat_id, action=action)
            except Exception as exc:  # jangan sampai animasi gagal = pesan gagal
                logger.debug(
                    "typing_animation: gagal kirim chat action untuk %s: %s",
                    method_name, exc,
                )
            else:
                await asyncio.sleep(_compute_delay(text))

        return await original(self, *args, **kwargs)

    wrapper._is_typing_animation_wrapper = True
    return wrapper


def enable():
    """Panggil sekali saat bot start (sebelum Application dipakai kirim
    pesan) untuk mengaktifkan animasi "sedang proses" di semua chat bubble."""
    global _ENABLED
    if _ENABLED:
        return
    for method_name, action in _PATCH_TARGETS.items():
        if not hasattr(Bot, method_name):
            continue
        wrapper = _make_wrapper(method_name, action)
        if wrapper is not None:
            setattr(Bot, method_name, wrapper)
    _ENABLED = True
    logger.info("typing_animation: animasi chat bubble aktif untuk %s method.", len(_PATCH_TARGETS))
