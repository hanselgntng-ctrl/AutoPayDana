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
        [InlineKeyboardButton("💎 Lihat Paket VIP", callback_data="show_vip", style="success")],
        [InlineKeyboardButton("📖 Petunjuk Order", callback_data="how_to_order", style="primary")],
        [InlineKeyboardButton("📌 Status VIP Saya", callback_data="my_status", style="primary")],
        [InlineKeyboardButton("💬 Hubungi Admin", url=f"https://t.me/{config.CONTACT_USERNAME}", style="primary")],
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
        [[KeyboardButton("💎 Buka Katalog VIP (Tampilan App)", web_app=WebAppInfo(url=config.WEBAPP_URL), style="success")]],
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
        InlineKeyboardButton(f"📁 {pkg['name']}", callback_data=f"buy_{pkg['id']}", style="success")
        for pkg in packages
    ]

    buttons = [package_buttons[i:i + 2] for i in range(0, len(package_buttons), 2)]
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_main", style="danger")])
    return InlineKeyboardMarkup(buttons)


async def _check_bot_admin_status(bot, target_chat_id: str, _cache: dict) -> bool:
    """Cek apakah bot ini adalah admin (administrator/creator) di
    `target_chat_id`. Dipakai untuk kolom Status di tabel VIP -- BUKAN
    tentang paket mana yang aktif dipakai user, tapi tentang kesiapan
    channel/group tujuan paket itu (apakah bot-nya sudah/masih admin di
    sana, supaya nanti bisa bikin invite link -- lihat send_package_link()
    di bot.py).

    Aturan:
    - Paket belum diisi target_chat_id sama sekali -> dianggap "belum siap"
      -> return False (tampil ❌), karena channel/group tujuannya memang
      belum di-setting oleh admin.
    - Paket sudah diisi target_chat_id, tapi bot ternyata BUKAN admin di
      sana (baik karena memang belum pernah ditambahkan, atau sudah
      di-unadmin/dikeluarkan admin channel) -> return False (❌).
    - Bot administrator/creator di sana -> return True (✅).
    - Kalau terjadi error saat mengecek (mis. chat_id salah/bot belum
      pernah join, chat sudah tidak ada, dll) -> dianggap False (❌) juga,
      supaya admin tahu ada yang perlu dibenahi.

    `_cache` dipakai supaya kalau beberapa paket kebetulan berbagi
    target_chat_id yang sama, kita tidak memanggil get_chat_member() ke
    Telegram berkali-kali untuk chat yang sama dalam satu kali render tabel.
    """
    if not target_chat_id:
        return False

    if target_chat_id in _cache:
        return _cache[target_chat_id]

    is_admin = False
    try:
        member = await bot.get_chat_member(int(target_chat_id), bot.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False

    _cache[target_chat_id] = is_admin
    return is_admin


async def format_vip_table(bot) -> str:
    """Kembalikan daftar paket VIP sebagai RICH BLOCK TABLE (border melengkung),
    dibungkus <pre> supaya kolomnya rapi (monospace) -- kembali ke gaya "tabel
    sungguhan", tapi sudutnya memakai karakter Unicode box-drawing yang
    melengkung (╭ ╮ ╰ ╯) alih-alih siku (┌ ┐ └ ┘).

    Kolom SENGAJA dibatasi hanya: No | Nama | Harga | Status -- Status di
    sini menandai apakah BOT MASIH ADMIN di channel/group tujuan
    (`target_chat_id`) paket tersebut:
    - ✅ = bot terdeteksi masih admin (administrator/creator) di sana.
    - ❌ = paket belum ada target_chat_id (belum di-setting), ATAU bot
      bukan/tidak lagi admin di sana (mis. baru saja di-unadmin/dikeluarkan
      dari channel), ATAU chat_id-nya tidak valid/tidak bisa dicek.

    Catatan teknis alignment: dipakai emoji ✅/❌ sesuai permintaan (bukan
    ✓/✗ polos) -- karena emoji ini lebar render-nya bisa 2 kolom di sebagian
    client, lebar kolom W_STATUS di bawah sengaja dilonggarkan sedikit
    supaya border tabel tetap rapi.

    Fungsi ini ASYNC karena perlu memanggil Bot API (get_chat_member) untuk
    tiap target_chat_id unik -- pemanggil WAJIB `await` fungsi ini.
    """
    packages = db.list_packages()
    if not packages:
        return "<i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i>"

    admin_status_cache: dict = {}
    package_status = {}
    for pkg in packages:
        package_status[pkg["id"]] = await _check_bot_admin_status(
            bot, (pkg["target_chat_id"] or "").strip(), admin_status_cache
        )

    # Lebar kolom (dalam karakter, TIDAK termasuk 1 spasi padding di tiap sisi).
    # "Nama" & "Harga" dipangkas otomatis (dengan "…") kalau kepanjangan supaya
    # border tabel tidak ikut melebar/miring gara-gara satu baris nakal.
    W_NO, W_NAMA, W_HARGA, W_STATUS = 3, 14, 11, 7

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
        status = "✅" if package_status.get(pkg["id"]) else "❌"
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


async def format_vip_table_rich(bot) -> str:
    """Versi Rich HTML (Bot API 10.1 `sendRichMessage`) dari tabel paket VIP --
    pakai tag <table> ASLI (bukan <pre> box-drawing kayak format_vip_table())
    supaya dirender sebagai tabel native oleh client yang sudah support.

    Kolom Status di sini punya arti SAMA seperti di format_vip_table(): ✅
    kalau bot terdeteksi masih admin di target_chat_id paket tsb, ❌ kalau
    belum di-setting channel-nya atau bot bukan/tidak lagi admin di sana
    (lihat docstring _check_bot_admin_status() untuk detail).

    PENTING (baca juga catatan di rich_api.py): `sendRichMessage` baru rilis
    11 Juni 2026. Client yang belum update akan menampilkan tag HTML ini APA
    ADANYA (mentah). Fungsi ini dipanggil dari main_menu_callback() di bot.py
    dengan fallback OTOMATIS ke format_vip_table() (versi <pre> lama) kalau
    request sendRichMessage-nya gagal -- tapi fallback itu hanya menangkap
    kegagalan di level API/jaringan, BUKAN kasus "client user rendernya jelek".
    Cocok dipakai selagi bot masih development/testing (belum dipakai user
    umum yang client-nya beragam).

    Fungsi ini ASYNC (sama seperti format_vip_table()) -- pemanggil WAJIB
    `await` fungsi ini."""
    packages = db.list_packages()
    if not packages:
        return "<p><i>Belum ada paket VIP yang tersedia. Admin bisa menambahkannya lewat /settings.</i></p>"

    admin_status_cache: dict = {}
    package_status = {}
    for pkg in packages:
        package_status[pkg["id"]] = await _check_bot_admin_status(
            bot, (pkg["target_chat_id"] or "").strip(), admin_status_cache
        )

    rows = []
    for idx, pkg in enumerate(packages, start=1):
        harga = f"Rp{pkg['price']:,}".replace(",", ".")
        status = "✅" if package_status.get(pkg["id"]) else "❌"
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
        [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="back_main", style="danger")],
    ])


def qris_back_keyboard():
    """Tombol kembali di tampilan scan QRIS (langkah sebelum QRIS = daftar
    paket VIP), dipakai supaya user tidak 'buntu' kalau salah pilih paket
    atau ingin batal & lihat paket lain dulu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Daftar Paket VIP", callback_data="show_vip", style="danger")],
    ])


def confirm_proof_keyboard(tx_id: int):
    """Keyboard fallback untuk admin approve/reject manual (dipakai hanya kalau
    verifikasi otomatis gagal / butuh review)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{tx_id}", style="success"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{tx_id}", style="danger"),
        ]
    ])


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Atur QRIS (gambar)", callback_data="set_qris", style="primary"),
            InlineKeyboardButton("💬 Atur Teks Sapaan", callback_data="set_greeting", style="primary"),
        ],
        [
            InlineKeyboardButton("📋 Atur Teks Menu VIP", callback_data="set_vip_text", style="primary"),
            InlineKeyboardButton("🧾 Atur Pesan Tampilan QRIS", callback_data="set_qris_caption", style="primary"),
        ],
        [
            InlineKeyboardButton("✅ Atur Pesan Berhasil (Approve)", callback_data="set_success_text", style="success"),
            InlineKeyboardButton("❌ Atur Pesan Ditolak (Reject)", callback_data="set_reject_text", style="danger"),
        ],
        [
            InlineKeyboardButton("🖼️ Atur Watermark Testi (stiker)", callback_data="set_watermark", style="primary"),
            InlineKeyboardButton("📝 Atur Caption Testi", callback_data="set_testi_caption", style="primary"),
        ],
        [
            InlineKeyboardButton("🔗 Atur Link Akses Statis (Global)", callback_data="set_static_link", style="primary"),
            InlineKeyboardButton("➕ Tambah Paket VIP", callback_data="add_package", style="success"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Paket VIP", callback_data="edit_package", style="primary"),
            InlineKeyboardButton("🗑️ Hapus Paket VIP", callback_data="delete_package", style="danger"),
        ],
        [InlineKeyboardButton("❎ Tutup", callback_data="settings_close", style="danger")],
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
        [InlineKeyboardButton(f"{p['name']} (Rp{p['price']:,})".replace(",", "."), callback_data=f"{prefix}_{p['id']}", style=style)]
        for p in packages
    ]
    return InlineKeyboardMarkup(buttons)
