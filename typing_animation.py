"""
typing_animation.py
--------------------
Modul yang membuat chat bubble bot Telegram-mu dianimasikan seperti
"three dots typing bubble" (bubble kecil berisi titik yang muncul satu-
satu: ● -> ●● -> ●●●) sebelum akhirnya berganti jadi teks jawaban asli --
gaya yang sama seperti "typing indicator" di WhatsApp/Messenger atau
"thinking indicator" di ChatGPT.

Ini menggantikan versi lama yang cuma memakai status bawaan Telegram
("sedang mengetik...") lalu jeda. Sekarang, untuk pesan teks, bot betulan
mengirim 1 bubble kecil lalu MENG-EDIT bubble itu sendiri beberapa kali
(animasi titiknya) sebelum bubble yang sama berubah jadi jawaban final --
persis seperti animasi di video yang kamu tunjukkan.

Cara pakai (tetap 1 baris, dipanggil sekali saat bot start):

    import typing_animation
    typing_animation.enable()

Yang dapat animasi bubble titik (●, ●●, ●●●):
    - context.bot.send_message(...) / update.message.reply_text(...)
      -> ini bubble BARU yang muncul di chat, jadi pas untuk animasi
         "titik muncul satu-satu" seperti di video.

Yang SENGAJA TIDAK disentuh oleh modul ini:
    - edit_message_text / edit_message_caption (dipakai lewat
      query.edit_message_text) -> bot.py sudah punya animasi transisi
      menu sendiri yang lebih halus (fade_transition & animasi elastis
      berbasis easing.py, lihat komentar di bot.py). Kalau method ini
      ikut dibungkus animasi titik juga, animasi bar/elastis yang sudah
      ada malah jadi rusak/lambat (tiap kali bar di-update, muncul
      dulu 3x animasi titik) dan berisiko kena flood-limit Telegram.
      Jadi untuk edit pesan, biarkan bot.py yang mengatur animasinya
      sendiri lewat fade_transition() / start_processing_animation().
    - send_photo, send_document, send_video, send_animation,
      send_voice, send_audio, edit_message_media -> tetap pakai status
      "sedang mengirim foto/dokumen/dst" bawaan Telegram (animasi titik
      tidak relevan untuk upload media).

Kenapa dipatch di level python-telegram-bot (class Bot), bukan diedit
satu-satu di bot.py? Karena ada 70+ titik pengiriman pesan tersebar di
banyak fungsi. Mem-patch class Bot sekali di sini jauh lebih aman &
gampang dirawat daripada menyisipkan kode animasi di 70+ tempat.
"""

import asyncio
import inspect
import logging

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# Bubble titik yang "muncul satu-satu" (frame 1 -> 2 -> 3), lalu bubble
# yang sama diganti jadi teks jawaban asli.
_DOT_FRAMES = ("●", "● ●", "● ● ●")

# Total lama animasi (detik) disesuaikan kasar dari panjang teks: teks
# pendek -> animasi sebentar, teks panjang -> animasi agak lama. Dibatasi
# MIN & MAX supaya bot tidak terasa lambat/lemot atau kena flood limit
# Telegram (edit pesan terlalu cepat/sering).
_MIN_TOTAL_DELAY = 0.6
_MAX_TOTAL_DELAY = 1.8
_CHARS_PER_SECOND = 60
_MIN_FRAME_DELAY = 0.25  # jarak antar edit, jangan lebih cepat dari ini

# Method Bot yang dibungkus animasi bubble titik (kirim bubble kecil,
# lalu edit jadi teks sungguhan). Hanya send_message -- lihat penjelasan
# di docstring atas soal kenapa edit_message_text/caption tidak disentuh.
_ANIMATED_TEXT_TARGETS = ("send_message",)
# edit_message_text tetap dibutuhkan secara INTERNAL (untuk mengedit bubble
# titik jadi teks final), makanya tetap disimpan referensi aslinya di sini,
# tapi TIDAK dipatch/dibungkus animasi apa pun -- perilakunya tetap 100%
# bawaan python-telegram-bot.
_INTERNAL_ONLY = ("edit_message_text",)

# Method Bot yang tetap pakai status bawaan Telegram + jeda (upload
# media, animasi titik tidak relevan di sini).
_CHAT_ACTION_TARGETS = {
    "edit_message_media": ChatAction.UPLOAD_PHOTO,
    "send_photo": ChatAction.UPLOAD_PHOTO,
    "send_document": ChatAction.UPLOAD_DOCUMENT,
    "send_video": ChatAction.UPLOAD_VIDEO,
    "send_animation": ChatAction.UPLOAD_VIDEO,
    "send_voice": ChatAction.RECORD_VOICE,
    "send_audio": ChatAction.UPLOAD_VOICE,
}

_ENABLED = False
# Referensi ke method ASLI (sebelum dipatch), dipakai secara internal
# supaya animasi tidak memicu dirinya sendiri berulang (rekursi).
_ORIGINALS = {}


def _compute_total_delay(text):
    """Hitung total lama animasi berdasarkan panjang teks/caption."""
    if not text:
        return _MIN_TOTAL_DELAY
    try:
        length = len(str(text))
    except Exception:
        return _MIN_TOTAL_DELAY
    delay = length / _CHARS_PER_SECOND
    return max(_MIN_TOTAL_DELAY, min(_MAX_TOTAL_DELAY, delay))


def _bind_kwargs(original, self_obj, args, kwargs):
    """Ambil SEMUA argumen pemanggilan (positional maupun keyword) jadi
    satu dict keyword, supaya gampang dibaca/diteruskan ulang tanpa perlu
    tahu urutan parameter aslinya."""
    try:
        sig = inspect.signature(original)
        bound = sig.bind_partial(self_obj, *args, **kwargs)
        bound.apply_defaults()
        call_kwargs = dict(bound.arguments)
        call_kwargs.pop("self", None)
        extra = call_kwargs.pop("kwargs", None)
        if isinstance(extra, dict):
            call_kwargs.update(extra)
        return call_kwargs
    except Exception:
        return dict(kwargs)


async def _animate_bubble(self, original_edit, chat_id, message_id, inline_message_id, is_caption, total_delay):
    """Edit bubble yang sudah ada (atau baru dikirim) supaya menampilkan
    titik yang muncul satu-satu: ● -> ●● -> ●●●."""
    frame_delay = max(_MIN_FRAME_DELAY, total_delay / len(_DOT_FRAMES))
    target = {"message_id": message_id} if chat_id is not None else {"inline_message_id": inline_message_id}
    if chat_id is not None:
        target["chat_id"] = chat_id
    field = "caption" if is_caption else "text"

    for frame in _DOT_FRAMES:
        try:
            await original_edit(self, **{**target, field: frame})
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("typing_animation: gagal animasikan bubble: %s", exc)
        except Exception as exc:
            logger.debug("typing_animation: gagal animasikan bubble: %s", exc)
        await asyncio.sleep(frame_delay)


def _make_animated_send_message_wrapper():
    original = getattr(Bot, "send_message")
    if getattr(original, "_is_typing_animation_wrapper", False):
        return None
    _ORIGINALS["send_message"] = original
    if "edit_message_text" not in _ORIGINALS:
        _ORIGINALS["edit_message_text"] = getattr(Bot, "edit_message_text")

    async def wrapper(self, *args, **kwargs):
        original_send = _ORIGINALS["send_message"]
        original_edit_text = _ORIGINALS["edit_message_text"]

        call_kwargs = _bind_kwargs(original_send, self, args, kwargs)
        chat_id = call_kwargs.get("chat_id")
        text = call_kwargs.get("text")

        if not text or chat_id is None:
            return await original_send(self, *args, **kwargs)

        total_delay = _compute_total_delay(text)
        placeholder = None

        try:
            # 1) Kirim bubble kecil dulu (titik pertama muncul). Tetap bawa
            #    parameter yang mempengaruhi POSISI/PERILAKU pesan (reply-to,
            #    thread, notifikasi, dst) supaya threading balasan tidak
            #    hilang -- tapi buang parameter yang mempengaruhi TAMPILAN
            #    konten (parse_mode/entities/reply_markup/link preview),
            #    karena itu baru relevan begitu isinya sudah jadi teks asli.
            placeholder_kwargs = dict(call_kwargs)
            placeholder_kwargs["text"] = _DOT_FRAMES[0]
            for visual_only in (
                "parse_mode", "entities", "reply_markup",
                "link_preview_options", "disable_web_page_preview",
            ):
                placeholder_kwargs.pop(visual_only, None)
            send_sig = inspect.signature(original_send)
            send_allowed = set(send_sig.parameters.keys())
            placeholder_kwargs = {k: v for k, v in placeholder_kwargs.items() if k in send_allowed}

            first_delay = max(_MIN_FRAME_DELAY, total_delay / len(_DOT_FRAMES))
            sent = await original_send(self, **placeholder_kwargs)
            placeholder = sent
            await asyncio.sleep(first_delay)

            # 2) Animasikan bubble yang sama: titik kedua, lalu ketiga.
            await _animate_bubble(
                self, original_edit_text, chat_id, placeholder.message_id, None, False,
                max(total_delay - first_delay, _MIN_FRAME_DELAY),
            )

            # 3) Ganti bubble titik jadi teks jawaban sungguhan (bubble-nya
            #    SAMA, cuma isinya berubah -- persis seperti di video).
            final_kwargs = dict(call_kwargs)
            final_kwargs["message_id"] = placeholder.message_id
            for drop in (
                "reply_to_message_id", "disable_notification", "protect_content",
                "message_thread_id", "allow_sending_without_reply", "reply_parameters",
                "message_effect_id", "business_connection_id",
            ):
                final_kwargs.pop(drop, None)
            edit_sig = inspect.signature(original_edit_text)
            allowed = set(edit_sig.parameters.keys())
            final_kwargs = {k: v for k, v in final_kwargs.items() if k in allowed}
            return await original_edit_text(self, **final_kwargs)
        except Exception as exc:
            logger.debug("typing_animation: animasi gagal: %s", exc)
            if placeholder is not None:
                # Bubble titik sudah terlanjur terkirim -> jangan biarkan
                # nyangkut, coba ganti langsung jadi teks asli.
                try:
                    edit_sig = inspect.signature(original_edit_text)
                    allowed = set(edit_sig.parameters.keys())
                    fallback_kwargs = {k: v for k, v in call_kwargs.items() if k in allowed}
                    fallback_kwargs["message_id"] = placeholder.message_id
                    fallback_kwargs["chat_id"] = chat_id
                    fallback_kwargs["text"] = text
                    return await original_edit_text(self, **fallback_kwargs)
                except Exception as exc2:
                    logger.debug("typing_animation: fallback edit bubble juga gagal: %s", exc2)
            return await original_send(self, *args, **kwargs)

    wrapper._is_typing_animation_wrapper = True
    return wrapper


def _make_chat_action_wrapper(method_name, action):
    original = getattr(Bot, method_name)
    if getattr(original, "_is_typing_animation_wrapper", False):
        return None
    _ORIGINALS[method_name] = original

    async def wrapper(self, *args, **kwargs):
        call_kwargs = _bind_kwargs(original, self, args, kwargs)
        chat_id = call_kwargs.get("chat_id")
        if chat_id is not None:
            try:
                await self.send_chat_action(chat_id=chat_id, action=action)
            except Exception as exc:
                logger.debug("typing_animation: gagal kirim chat action untuk %s: %s", method_name, exc)
            else:
                await asyncio.sleep(_compute_total_delay(None))
        return await original(self, *args, **kwargs)

    wrapper._is_typing_animation_wrapper = True
    return wrapper


def enable():
    """Panggil sekali saat bot start (sebelum Application dipakai kirim
    pesan) untuk mengaktifkan animasi bubble titik di semua chat bubble."""
    global _ENABLED
    if _ENABLED:
        return

    for method_name in (*_ANIMATED_TEXT_TARGETS, *_INTERNAL_ONLY, *_CHAT_ACTION_TARGETS):
        if hasattr(Bot, method_name) and method_name not in _ORIGINALS:
            _ORIGINALS[method_name] = getattr(Bot, method_name)

    if hasattr(Bot, "send_message"):
        wrapper = _make_animated_send_message_wrapper()
        if wrapper is not None:
            setattr(Bot, "send_message", wrapper)

    for method_name, action in _CHAT_ACTION_TARGETS.items():
        if not hasattr(Bot, method_name):
            continue
        wrapper = _make_chat_action_wrapper(method_name, action)
        if wrapper is not None:
            setattr(Bot, method_name, wrapper)

    _ENABLED = True
    logger.info(
        "typing_animation: animasi bubble titik (●/●●/●●●) aktif untuk %s method teks, "
        "%s method media pakai status bawaan Telegram.",
        len(_ANIMATED_TEXT_TARGETS), len(_CHAT_ACTION_TARGETS),
    )
