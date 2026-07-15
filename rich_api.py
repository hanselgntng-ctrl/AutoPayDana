"""
rich_api.py
===========
Helper tipis untuk memanggil method Bot API 10.1 `sendRichMessage` yang BELUM
didukung secara typed oleh python-telegram-bot (per Juli 2026, lihat issue
python-telegram-bot/python-telegram-bot#5261) -- jadi dipanggil langsung lewat
HTTP request mentah ke api.telegram.org.

PENTING: `sendRichMessage` baru dirilis Telegram 11 Juni 2026 (Bot API 10.1).
Client Telegram yang belum update ke versi yang support akan menampilkan
tag HTML-nya APA ADANYA (mentah, tidak dirender jadi tabel/heading beneran).
Dipakai di bot ini HANYA untuk format_vip_table_rich() (lihat keyboards.py)
selagi bot masih tahap development/testing -- kalau nanti sudah dipakai user
umum, pertimbangkan lagi apakah tetap dipakai atau kembali ke versi teks biasa
(format_vip_table()), karena mayoritas user kemungkinan belum update client-nya.
"""

import aiohttp

import config


class RichMessageError(Exception):
    """Dilempar kalau Telegram menolak permintaan sendRichMessage (mis. server
    Bot API belum rollout penuh method ini, payload HTML tidak valid, dsb)."""


async def send_rich_message(chat_id: int, html: str, reply_markup=None) -> dict:
    """Panggil method sendRichMessage secara langsung. Mengembalikan dict
    `result` (objek Message) dari Telegram kalau sukses, atau melempar
    RichMessageError kalau gagal -- supaya pemanggil bisa fallback ke cara
    lama (format_vip_table() + sendMessage biasa)."""
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendRichMessage"
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": html},
    }
    if reply_markup is not None:
        payload["reply_markup"] = (
            reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup
        )

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
    except Exception as e:
        raise RichMessageError(f"Gagal menghubungi Telegram: {e}") from e

    if not data.get("ok"):
        raise RichMessageError(data.get("description", "sendRichMessage gagal tanpa deskripsi"))

    return data["result"]
