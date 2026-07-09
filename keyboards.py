"""
keyboards.py
============
Kumpulan fungsi pembuat inline keyboard & format teks tabel paket VIP.
"""

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Lihat Paket VIP", callback_data="show_vip")],
        [InlineKeyboardButton("📌 Status VIP Saya", callback_data="my_status")],
        [InlineKeyboardButton("💬 Hubungi Admin", url=f"https://t.me/{config.CONTACT_USERNAME}")],
    ])


def vip_list_keyboard():
    packages = db.list_packages()
    buttons = []
    for pkg in packages:
        label = f"{pkg['name']} - Rp{pkg['price']:,}".replace(",", ".")
        buttons.append([InlineKeyboardButton(label, callback_data=f"buy_{pkg['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def format_vip_table() -> str:
    """Kembalikan daftar paket VIP dalam format Rich Text (native Telegram),
    bukan tabel monospace <pre>. Telegram Bot API tidak punya tag <table> HTML,
    jadi "tabel native" di sini diwujudkan lewat kombinasi <b>bold</b>, emoji,
    dan garis pemisah antar paket -- tetap rapi dibaca di semua device (HP/desktop)
    tanpa perlu font monospace/scroll horizontal seperti <pre>.

    Setiap field di-escape HTML per-item (bukan di akhir atas string gabungan),
    supaya tag <b> yang kita tambahkan sendiri tidak ikut ke-escape."""
    packages = db.list_packages()
    if not packages:
        return "<i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i>"

    blocks = []
    for pkg in packages:
        nama = html.escape(str(pkg["name"]))
        harga = html.escape(f"Rp{pkg['price']:,}".replace(",", "."))
        durasi = html.escape(f"{pkg['duration_days']} hari")

        block = (
            f"💎 <b>{nama}</b>\n"
            f"   Harga  : <b>{harga}</b>\n"
            f"   Durasi : {durasi}"
        )

        try:
            deskripsi = (pkg["description"] or "").strip()
        except (IndexError, KeyError):
            deskripsi = ""
        if deskripsi:
            block += f"\n   Ket    : {html.escape(deskripsi)}"

        blocks.append(block)

    divider = "\n" + ("➖" * 11) + "\n"
    table = divider.join(blocks)
    return table


def confirm_proof_keyboard(tx_id: int):
    """Keyboard fallback untuk admin approve/reject manual (dipakai hanya kalau
    verifikasi otomatis gagal / butuh review)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{tx_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{tx_id}"),
        ]
    ])


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Atur QRIS (gambar)", callback_data="set_qris"),
            InlineKeyboardButton("💬 Atur Teks Sapaan", callback_data="set_greeting"),
        ],
        [
            InlineKeyboardButton("📋 Atur Teks Menu VIP", callback_data="set_vip_text"),
            InlineKeyboardButton("🧾 Atur Pesan Tampilan QRIS", callback_data="set_qris_caption"),
        ],
        [
            InlineKeyboardButton("✅ Atur Pesan Berhasil (Approve)", callback_data="set_success_text"),
            InlineKeyboardButton("❌ Atur Pesan Ditolak (Reject)", callback_data="set_reject_text"),
        ],
        [
            InlineKeyboardButton("🖼️ Atur Watermark Testi (stiker)", callback_data="set_watermark"),
            InlineKeyboardButton("📝 Atur Caption Testi", callback_data="set_testi_caption"),
        ],
        [
            InlineKeyboardButton("🔗 Atur Link Akses Statis (Global)", callback_data="set_static_link"),
            InlineKeyboardButton("➕ Tambah Paket VIP", callback_data="add_package"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Paket VIP", callback_data="edit_package"),
            InlineKeyboardButton("🗑️ Hapus Paket VIP", callback_data="delete_package"),
        ],
        [InlineKeyboardButton("❎ Tutup", callback_data="settings_close")],
    ])


def package_pick_keyboard(prefix: str):
    """Keyboard daftar paket untuk keperluan edit/hapus di menu settings.

    Catatan: sengaja TIDAK menambahkan tombol "kembali" sendiri di sini —
    fungsi ini selalu dipanggil lewat bot.py::with_back(), yang sudah
    menambahkan satu tombol "Kembali ke Menu Settings" (callback_data=
    "settings_cancel") di bawahnya. Sebelumnya ada 2 tombol kembali yang
    beda (satunya "settings_back" tanpa handler terdaftar di state
    EDIT_PKG_PICK) — itu yang membuat salah satu tombol terlihat "tidak
    berfungsi". Sekarang hanya ada 1 tombol kembali yang konsisten & selalu
    tertangkap oleh handler.
    """
    packages = db.list_packages()
    buttons = [
        [InlineKeyboardButton(f"{p['name']} (Rp{p['price']:,})".replace(",", "."), callback_data=f"{prefix}_{p['id']}")]
        for p in packages
    ]
    return InlineKeyboardMarkup(buttons)
