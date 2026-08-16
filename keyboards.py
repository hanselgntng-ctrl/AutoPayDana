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


# ── Wrapper aman-versi untuk tombol (lihat catatan panjang di bot.py::make_button) ──
# `style` pada InlineKeyboardButton/KeyboardButton baru didukung mulai
# python-telegram-bot v22.7 (Bot API 9.4). Kalau versi yang terpasang di
# server lebih lama, meneruskan `style=...` langsung akan melempar TypeError
# dan MENGGAGALKAN TOTAL keyboard yang sedang dibuat -- termasuk
# vip_list_keyboard(), yang sebelumnya menyebabkan seluruh Daftar Paket VIP
# gagal tampil (bukan cuma warnanya hilang) begitu library belum diupgrade.
# Semua pembuatan tombol di file ini WAJIB lewat wrapper ini, BUKAN
# memanggil InlineKeyboardButton/KeyboardButton langsung dengan style=...
_style_unsupported_warned = False


def _safe_button(cls, text: str, **kwargs):
    global _style_unsupported_warned
    try:
        return cls(text, **kwargs)
    except TypeError as e:
        if "style" in kwargs and "style" in str(e):
            if not _style_unsupported_warned:
                import logging
                logging.getLogger(__name__).warning(
                    "python-telegram-bot yang terpasang belum mendukung field "
                    "'style' pada tombol (baru didukung mulai v22.7, sesuai "
                    "Bot API 9.4). Tombol akan dibuat tanpa warna. Upgrade "
                    "dengan: pip install -U python-telegram-bot"
                )
                _style_unsupported_warned = True
            fallback_kwargs = {k: v for k, v in kwargs.items() if k != "style"}
            return cls(text, **fallback_kwargs)
        raise


def _btn(text: str, **kwargs) -> InlineKeyboardButton:
    return _safe_button(InlineKeyboardButton, text, **kwargs)


def _kbtn(text: str, **kwargs) -> KeyboardButton:
    return _safe_button(KeyboardButton, text, **kwargs)


def package_is_available(pkg) -> bool:
    """Sebuah paket VIP dianggap TERSEDIA (bisa langsung dikirim otomatis oleh
    send_package_link() di bot.py saat dibeli) kalau salah satu dari ini
    terisi -- BUKAN cuma target_chat_id saja seperti sebelumnya:
    - Paket KOLEKTIF: punya baris di tabel package_channels (mis. Paket 16/17/
      18 Link). Paket jenis ini SENGAJA punya target_chat_id & link kosong,
      jadi cek lama (`target_chat_id` saja) selalu salah menandainya "tidak
      tersedia" walau channel-nya lengkap.
    - Paket biasa: target_chat_id (grup/channel Telegram) ATAU link statis
      milik paket itu sendiri sudah diisi.
    - Fallback: link akses statis GLOBAL (/settings -> Atur Link Akses Statis)
      sudah diisi -- paket yang tidak set link/target_chat_id sendiri akan
      otomatis pakai ini juga saat dibeli, jadi tetap "tersedia".
    """
    if db.count_package_channels(pkg["id"]) > 0:
        return True
    if (pkg["target_chat_id"] or "").strip():
        return True
    if (pkg["link"] or "").strip():
        return True
    if (db.get_setting("static_access_link", "") or "").strip():
        return True
    return False


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
        [_btn("💎 Lihat Paket VIP", callback_data="show_vip", style="success")],
        [_btn("📖 Petunjuk Order", callback_data="how_to_order", style="primary")],
        [_btn("📌 Status VIP Saya", callback_data="my_status", style="primary")],
        [_btn("💬 Hubungi Admin", url=f"https://t.me/{config.CONTACT_USERNAME}", style="primary")],
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
        [[_kbtn("💎 Buka Katalog VIP (Tampilan App)", web_app=WebAppInfo(url=config.WEBAPP_URL), style="success")]],
        resize_keyboard=True,
    )


def remove_webapp_keyboard():
    """Dipakai kalau suatu saat perlu menyembunyikan reply keyboard di atas
    (mis. admin baru menghapus WEBAPP_URL) -- Telegram tidak otomatis
    menghilangkan custom reply keyboard lama tanpa perintah eksplisit ini."""
    return ReplyKeyboardRemove()


def vip_list_keyboard():
    """Tombol daftar paket VIP (dipencet untuk mulai beli, callback_data
    'buy_{id}'), disusun 2 tombol per baris supaya lebih ringkas -- kalau
    jumlah paket ganjil, tombol terakhir otomatis sendirian di barisnya
    (tidak dipaksa berpasangan dengan tombol "Kembali"). Label tombol
    SENGAJA hanya menampilkan nama paket (TANPA harga/nominal) supaya teks
    tombol lebih pendek & tampilan lebih rapi -- harga tetap ditampilkan di
    tabel VIP di atasnya, jadi user tidak kehilangan info harga. Tombol
    "⬅️ Kembali" tetap 1 baris sendiri di paling bawah, terpisah dari grid
    paket, supaya tetap jelas beda fungsi & gampang ditemukan."""
    packages = db.list_packages()
    package_buttons = [
        _btn(f"📁 {pkg['name']}", callback_data=f"buy_{pkg['id']}", style="success")
        for pkg in packages
    ]

    buttons = [package_buttons[i:i + 2] for i in range(0, len(package_buttons), 2)]
    buttons.append([_btn("⬅️ Kembali", callback_data="back_main", style="danger")])
    return InlineKeyboardMarkup(buttons)


def format_vip_table(bot=None) -> str:
    """Kembalikan daftar paket VIP sebagai RICH BLOCK TABLE (border melengkung),
    dibungkus <pre> supaya kolomnya rapi (monospace), sudutnya memakai karakter
    Unicode box-drawing yang melengkung (╭ ╮ ╰ ╯) alih-alih siku (┌ ┐ └ ┘).

    Kolom: No | Nama | Harga | Status -- Status di sini MURNI cek data lokal
    di database (lihat package_is_available()), BUKAN cek ke Telegram:
    - ✅ Tersedia     = paket punya channel kolektif (package_channels), ATAU
      target_chat_id sendiri, ATAU link sendiri, ATAU link akses statis
      global sudah diisi -- salah satu saja cukup, karena itu semua jalur
      yang dipakai send_package_link() (bot.py) untuk mengirim akses.
    - ❌ Tidak Tersedia = semua jalur di atas kosong (paket ini belum bisa
      mengirim akses apa pun kalau ada yang beli).

    SENGAJA tidak memanggil Bot API apa pun (mis. get_chat_member) ke channel/
    group tujuan paket untuk mengecek apakah bot benar-benar sudah jadi admin
    di sana -- status ini HANYA menandakan datanya sudah diisi atau belum,
    bukan konfirmasi bot sudah/masih admin di channel tsb. Satu-satunya
    interaksi bot ke channel VIP tetap murni saat MEMBUAT invite link 1 kali
    pakai di send_package_link() (bot.py).

    Parameter `bot` dipertahankan (opsional, tidak dipakai) supaya pemanggil
    lama yang masih mengirim `context.bot` tetap kompatibel tanpa perlu diubah.
    """
    packages = db.list_packages()
    if not packages:
        return "<i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i>"

    # Lebar kolom (dalam karakter, TIDAK termasuk 1 spasi padding di tiap sisi).
    # "Nama" & "Harga" dipangkas otomatis (dengan "…") kalau kepanjangan supaya
    # border tabel tidak ikut melebar/miring gara-gara satu baris nakal.
    W_NO, W_NAMA, W_HARGA, W_STATUS = 3, 14, 11, 15

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
        status = "✅ Tersedia" if package_is_available(pkg) else "❌ Tidak Tersedia"
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


def format_vip_table_rich(bot=None) -> str:
    """Versi Rich HTML (Bot API 10.1 `sendRichMessage`) dari tabel paket VIP --
    pakai tag <table> ASLI (bukan <pre> box-drawing kayak format_vip_table())
    supaya dirender sebagai tabel native oleh client yang sudah support.

    Kolom Status di sini punya arti SAMA seperti di format_vip_table(): MURNI
    cek data lokal di database lewat package_is_available(), BUKAN cek ke
    Telegram. Lihat docstring format_vip_table() untuk detail lengkap.

    PENTING (baca juga catatan di rich_api.py): `sendRichMessage` baru rilis
    11 Juni 2026. Client yang belum update akan menampilkan tag HTML ini APA
    ADANYA (mentah). Fungsi ini dipanggil dari main_menu_callback() di bot.py
    dengan fallback OTOMATIS ke format_vip_table() (versi <pre> lama) kalau
    request sendRichMessage-nya gagal -- tapi fallback itu hanya menangkap
    kegagalan di level API/jaringan, BUKAN kasus "client user rendernya jelek".
    Cocok dipakai selagi bot masih development/testing (belum dipakai user
    umum yang client-nya beragam).

    Parameter `bot` dipertahankan (opsional, tidak dipakai) supaya pemanggil
    lama yang masih mengirim `context.bot` tetap kompatibel tanpa perlu diubah.
    """
    packages = db.list_packages()
    if not packages:
        return "<p><i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i></p>"

    rows = []
    for idx, pkg in enumerate(packages, start=1):
        harga = f"Rp{pkg['price']:,}".replace(",", ".")
        status = "✅ Tersedia" if package_is_available(pkg) else "❌ Tidak Tersedia"
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{html.escape(pkg['name'])}</td>"
            f"<td>{harga}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )

    return (
        "<table>"
        "<thead><tr><th>No</th><th>Nama</th><th>Harga</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def how_to_order_keyboard():
    """Tombol kembali di halaman "Petunjuk Order" (dibuka dari menu utama),
    balik ke menu utama (bukan ke daftar paket VIP) karena halaman ini
    diakses langsung dari main_menu_keyboard(), bukan dari alur beli."""
    return InlineKeyboardMarkup([
        [_btn("⬅️ Kembali ke Menu Utama", callback_data="back_main", style="danger")],
    ])


def qris_back_keyboard():
    """Tombol kembali di tampilan scan QRIS (langkah sebelum QRIS = daftar
    paket VIP), dipakai supaya user tidak 'buntu' kalau salah pilih paket
    atau ingin batal & lihat paket lain dulu."""
    return InlineKeyboardMarkup([
        [_btn("⬅️ Kembali ke Daftar Paket VIP", callback_data="show_vip", style="danger")],
    ])


def confirm_proof_keyboard(tx_id: int):
    """Keyboard fallback untuk admin approve/reject manual (dipakai hanya kalau
    verifikasi otomatis gagal / butuh review)."""
    return InlineKeyboardMarkup([
        [
            _btn("✅ Approve", callback_data=f"admin_approve_{tx_id}", style="success"),
            _btn("❌ Reject", callback_data=f"admin_reject_{tx_id}", style="danger"),
        ]
    ])


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            _btn("🖼️ Atur QRIS (gambar)", callback_data="set_qris", style="primary"),
            _btn("💬 Atur Teks Sapaan", callback_data="set_greeting", style="primary"),
        ],
        [
            _btn("📋 Atur Teks Menu VIP", callback_data="set_vip_text", style="primary"),
            _btn("🧾 Atur Pesan Tampilan QRIS", callback_data="set_qris_caption", style="primary"),
        ],
        [
            _btn("✅ Atur Pesan Berhasil (Approve)", callback_data="set_success_text", style="success"),
            _btn("❌ Atur Pesan Ditolak (Reject)", callback_data="set_reject_text", style="danger"),
        ],
        [
            _btn("🖼️ Atur Watermark Testi (stiker)", callback_data="set_watermark", style="primary"),
            _btn("📝 Atur Caption Testi", callback_data="set_testi_caption", style="primary"),
        ],
        [
            _btn("🔗 Atur Link Akses Statis (Global)", callback_data="set_static_link", style="primary"),
            _btn("➕ Tambah Paket VIP", callback_data="add_package", style="success"),
        ],
        [
            _btn("✏️ Edit Paket VIP", callback_data="edit_package", style="primary"),
            _btn("🗑️ Hapus Paket VIP", callback_data="delete_package", style="danger"),
        ],
        [_btn("❎ Tutup", callback_data="settings_close", style="danger")],
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
    # Warna tombol menyesuaikan konteks pemanggil: prefix "delpkg" (hapus)
    # dipakai style="danger" (merah) supaya konsisten sebagai aksi
    # destruktif, prefix lain (mis. "editpkg") dipakai style="primary"
    # (biru) sebagai aksi netral/info.
    style = "danger" if prefix == "delpkg" else "primary"
    packages = db.list_packages()
    buttons = [
        [_btn(f"{p['name']} (Rp{p['price']:,})".replace(",", "."), callback_data=f"{prefix}_{p['id']}", style=style)]
        for p in packages
    ]
    return InlineKeyboardMarkup(buttons)
