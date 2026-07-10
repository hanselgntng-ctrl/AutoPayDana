"""
keyboards.py
============
Kumpulan fungsi pembuat inline keyboard & format teks tabel paket VIP.
"""

import html
import datetime

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
)

import config
import database as db


def main_menu_keyboard():
    # PENTING: tombol WebApp di sini SENGAJA selalu pakai callback_data="show_vip"
    # (tabel teks di chat), BUKAN web_app=WebAppInfo(...) langsung di inline
    # button. Alasannya bukan soal preferensi tampilan, tapi keterbatasan resmi
    # Telegram: Telegram.WebApp.sendData() -- cara Mini App mengirim package_id
    # terpilih balik ke bot -- HANYA berfungsi kalau Mini App dibuka lewat
    # KeyboardButton (custom reply keyboard). Kalau dibuka lewat
    # InlineKeyboardButton (seperti sebelumnya di sini), sendData() akan
    # menutup Mini App TANPA mengirim apa pun ke bot -- makanya bot terlihat
    # "tidak merespon sama sekali" setelah user pilih paket. Lihat referensi:
    # https://core.telegram.org/bots/webapps#keyboard-button-web-apps
    #
    # Tombol WebApp yang BENAR-BENAR bisa sendData() ada di
    # webapp_launch_keyboard() di bawah -- itu yang harus dikirim sebagai
    # reply keyboard (persistent, di luar bubble pesan), bukan inline.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Lihat Paket VIP", callback_data="show_vip")],
        [InlineKeyboardButton("📌 Status VIP Saya", callback_data="my_status")],
        [InlineKeyboardButton("💬 Hubungi Admin", url=f"https://t.me/{config.CONTACT_USERNAME}")],
    ])


def webapp_launch_keyboard():
    """Reply keyboard (custom keyboard, BUKAN inline) berisi 1 tombol yang
    membuka Mini App "Lihat Paket VIP" (tabel HTML asli). Ini satu-satunya
    cara Mini App bisa memanggil Telegram.WebApp.sendData() dan datanya benar-
    benar sampai ke handle_webapp_data() di bot.py -- lihat catatan panjang di
    main_menu_keyboard() di atas.

    Kembalikan None kalau WEBAPP_URL belum di-setup admin (supaya pemanggil
    bisa skip mengirim keyboard ini sama sekali, dan bot tetap jalan normal
    lewat tabel teks biasa)."""
    if not config.WEBAPP_URL:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💎 Buka Katalog VIP (Tampilan App)", web_app=WebAppInfo(url=config.WEBAPP_URL))]],
        resize_keyboard=True,
    )


def remove_webapp_keyboard():
    """Dipakai kalau suatu saat perlu menyembunyikan reply keyboard di atas
    (mis. admin baru menghapus WEBAPP_URL) -- Telegram tidak otomatis
    menghilangkan custom reply keyboard lama tanpa perintah eksplisit ini."""
    return ReplyKeyboardRemove()


def vip_list_keyboard():
    packages = db.list_packages()
    buttons = []
    for pkg in packages:
        label = f"{pkg['name']} - Rp{pkg['price']:,}".replace(",", ".")
        buttons.append([InlineKeyboardButton(label, callback_data=f"buy_{pkg['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def format_vip_table(user_id: int = None) -> str:
    """Kembalikan daftar paket VIP sebagai RICH BLOCK TABLE (border melengkung),
    dibungkus <pre> supaya kolomnya rapi (monospace) -- kembali ke gaya "tabel
    sungguhan", tapi sudutnya memakai karakter Unicode box-drawing yang
    melengkung (╭ ╮ ╰ ╯) alih-alih siku (┌ ┐ └ ┘).

    Kolom SENGAJA dibatasi hanya: No | Nama | Harga | Status -- Status HANYA
    berisi simbol ceklis (✓) atau silang (✗), menandai apakah paket itu yang
    SEDANG AKTIF dipakai user peminta (kalau `user_id` diisi & VIP-nya belum
    kedaluwarsa). Kalau `user_id` tidak diisi (mis. dipakai di konteks lain
    tanpa info user), semua baris otomatis tampil ✗.

    Catatan teknis alignment: dipakai simbol ✓/✗ (bukan emoji berwarna ✅/❌)
    karena simbol polos ini lebar tampilannya konsisten 1 kolom di font
    monospace hampir semua client Telegram, sedangkan emoji berwarna cenderung
    dirender lebih lebar (2 kolom) sehingga bisa membuat border tabel geser/
    tidak rapi di sebagian device.
    """
    packages = db.list_packages()
    if not packages:
        return "<i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i>"

    # Cek paket mana (kalau ada) yang jadi VIP AKTIF user saat ini (belum expired)
    active_package_id = None
    if user_id is not None:
        vip = db.get_vip(user_id)
        if vip and vip["expiry_date"]:
            try:
                expiry = datetime.datetime.fromisoformat(vip["expiry_date"])
                if expiry > datetime.datetime.utcnow():
                    active_package_id = vip["package_id"]
            except (ValueError, TypeError):
                pass

    # Lebar kolom (dalam karakter, TIDAK termasuk 1 spasi padding di tiap sisi).
    # "Nama" & "Harga" dipangkas otomatis (dengan "…") kalau kepanjangan supaya
    # border tabel tidak ikut melebar/miring gara-gara satu baris nakal.
    W_NO, W_NAMA, W_HARGA, W_STATUS = 3, 14, 11, 6

    def clip(value, width: int) -> str:
        text = str(value)
        return text if len(text) <= width else text[: max(0, width - 1)] + "…"

    def cell(value, width: int) -> str:
        return f"{clip(value, width):<{width}}"

    def border(left: str, joint: str, right: str) -> str:
        segs = [
            "─" * (W_NO + 2), "─" * (W_NAMA + 2),
            "─" * (W_HARGA + 2), "─" * (W_STATUS + 2),
        ]
        return left + joint.join(segs) + right

    top_border = border("╭", "┬", "╮")     # sudut atas melengkung
    mid_border = border("├", "┼", "┤")
    bottom_border = border("╰", "┴", "╯")  # sudut bawah melengkung

    header = f"│ {cell('No', W_NO)} │ {cell('Nama', W_NAMA)} │ {cell('Harga', W_HARGA)} │ {cell('Status', W_STATUS)} │"

    lines = [top_border, header, mid_border]
    for idx, pkg in enumerate(packages, start=1):
        harga = f"Rp{pkg['price']:,}".replace(",", ".")
        status = "✓" if pkg["id"] == active_package_id else "✗"
        lines.append(
            f"│ {cell(idx, W_NO)} │ {cell(pkg['name'], W_NAMA)} │ {cell(harga, W_HARGA)} │ {cell(status, W_STATUS)} │"
        )
        # Garis pemisah horizontal SETELAH SETIAP baris (bukan cuma di bawah
        # header) -- supaya tabel jadi grid penuh & tidak "terputus" seperti
        # tabel Excel/spreadsheet asli, bukan cuma dipisah 1 garis di atas saja.
        lines.append(bottom_border if idx == len(packages) else mid_border)

    # Escape SEKALI SAJA di akhir atas gabungan teks mentahnya -- padding di
    # atas dihitung dari panjang ASLI (sebelum escape), sama seperti versi
    # tabel sebelumnya, supaya kolom tetap rapi walau nama paket mengandung
    # karakter seperti "&"/"<" (yang baru melebar SETELAH di-escape).
    body_raw = "\n".join(lines)
    return "<pre>" + html.escape(body_raw) + "</pre>"


def qris_back_keyboard():
    """Tombol kembali di tampilan scan QRIS (langkah sebelum QRIS = daftar
    paket VIP), dipakai supaya user tidak 'buntu' kalau salah pilih paket
    atau ingin batal & lihat paket lain dulu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Daftar Paket VIP", callback_data="show_vip")],
    ])


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
