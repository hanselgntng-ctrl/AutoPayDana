"""
keyboards.py
============
Kumpulan fungsi pembuat inline keyboard & format teks tabel paket VIP.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database as db


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Lihat Paket VIP", callback_data="show_vip")],
        [InlineKeyboardButton("📌 Status VIP Saya", callback_data="my_status")],
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
    packages = db.list_packages()
    if not packages:
        return "_Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings._"

    header = f"{'Paket':<14}{'Harga':<14}{'Durasi':<10}\n"
    divider = "-" * 36 + "\n"
    rows = ""
    for pkg in packages:
        harga = f"Rp{pkg['price']:,}".replace(",", ".")
        rows += f"{pkg['name']:<14}{harga:<14}{pkg['duration_days']} hari\n"

    table = "```\n" + header + divider + rows + "```"
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
        [InlineKeyboardButton("🖼️ Atur QRIS", callback_data="set_qris")],
        [InlineKeyboardButton("💬 Atur Teks Sapaan", callback_data="set_greeting")],
        [InlineKeyboardButton("📋 Atur Teks Menu VIP", callback_data="set_vip_text")],
        [InlineKeyboardButton("➕ Tambah Paket VIP", callback_data="add_package")],
        [InlineKeyboardButton("✏️ Edit Paket VIP", callback_data="edit_package")],
        [InlineKeyboardButton("🗑️ Hapus Paket VIP", callback_data="delete_package")],
        [InlineKeyboardButton("❎ Tutup", callback_data="settings_close")],
    ])


def package_pick_keyboard(prefix: str):
    """Keyboard daftar paket untuk keperluan edit/hapus di menu settings."""
    packages = db.list_packages()
    buttons = [
        [InlineKeyboardButton(f"{p['name']} (Rp{p['price']:,})".replace(",", "."), callback_data=f"{prefix}_{p['id']}")]
        for p in packages
    ]
    buttons.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back")])
    return InlineKeyboardMarkup(buttons)
