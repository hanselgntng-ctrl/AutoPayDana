"""
api_server.py
==============
Server HTTP kecil (aiohttp) yang berjalan BERBARENGAN dengan bot Telegram, di
event loop yang sama (lewat post_init Application di bot.py) -- bukan proses
terpisah, jadi tidak butuh container/service kedua di Railway.

Kenapa perlu ini? Telegram Mini App / WebApp ("Lihat Paket VIP" versi tabel
HTML asli, sesuai contoh yang diminta) adalah HALAMAN WEB biasa yang dibuka
di WebView Telegram -- ia butuh sumber data lewat HTTP fetch() biasa, bukan
lewat Bot API. Jadi bot ini sekalian jadi backend kecil untuk WebApp-nya.

Endpoint yang disediakan:
- GET /api/packages -> daftar paket VIP (read-only, publik, tanpa data
  sensitif -- cuma nama/harga/durasi, sama seperti yang sudah ditampilkan ke
  semua user lewat menu "Lihat Paket VIP").
- GET /health -> untuk healthcheck Railway (opsional).
"""

import logging

import aiohttp
from aiohttp import web

import config
import database as db

logger = logging.getLogger(__name__)


def _with_cors(resp: web.Response) -> web.Response:
    """Izinkan diakses dari domain manapun (mis. GitHub Pages) -- aman karena
    endpoint ini read-only & isinya memang sudah publik (daftar harga)."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_packages(request: web.Request) -> web.Response:
    packages = db.list_packages()
    data = [
        {
            "id": pkg["id"],
            "name": pkg["name"],
            "price": pkg["price"],
            "duration_days": pkg["duration_days"],
            "status": "active",  # list_packages() default cuma kembalikan yang aktif
        }
        for pkg in packages
    ]
    return _with_cors(web.json_response({"packages": data}))


async def handle_select_package(request: web.Request) -> web.Response:
    """Dipakai KHUSUS untuk Mini App yang dibuka lewat Menu Button/inline
    (bukan reply keyboard) -- jalur ini tidak bisa pakai Telegram.WebApp.
    sendData() (lihat catatan panjang di keyboards.py), jadi sebagai
    gantinya index.html mengirim pilihan paket ke endpoint HTTP ini,
    lalu KITA yang memanggil Bot API answerWebAppQuery ke Telegram.

    answerWebAppQuery akan menyisipkan SATU pesan ke chat (seolah dikirim
    user via inline mode) berisi tombol "Lanjut Bayar" dengan
    callback_data=f"buy_{pkg_id}" -- persis pola yang SUDAH ditangani oleh
    CallbackQueryHandler(main_menu_callback, pattern="...buy_\\d+...") di
    bot.py, jadi alur pembelian selanjutnya (start_purchase_flow, dst)
    otomatis jalan tanpa perlu kode baru di bot.py."""
    try:
        body = await request.json()
        query_id = str(body.get("query_id") or "").strip()
        pkg_id = int(body.get("package_id"))
    except Exception:
        return _with_cors(web.json_response({"ok": False, "error": "Payload tidak valid"}, status=400))

    if not query_id:
        return _with_cors(web.json_response({"ok": False, "error": "query_id kosong (Mini App tidak dibuka lewat menu/inline)"}, status=400))

    pkg = db.get_package(pkg_id)
    if not pkg:
        return _with_cors(web.json_response({"ok": False, "error": "Paket tidak ditemukan / sudah tidak aktif"}, status=404))

    harga = f"Rp{pkg['price']:,}".replace(",", ".")
    result = {
        "type": "article",
        "id": f"pkg{pkg_id}"[:64],
        "title": f"Paket {pkg['name']}",
        "description": f"{harga} • {pkg['duration_days']} hari",
        "input_message_content": {
            "message_text": (
                f"💎 Kamu memilih paket <b>{pkg['name']}</b> ({harga}, "
                f"{pkg['duration_days']} hari).\n\nTekan tombol di bawah untuk lanjut ke pembayaran."
            ),
            "parse_mode": "HTML",
        },
        "reply_markup": {
            "inline_keyboard": [[
                {"text": f"➡️ Lanjut Bayar {pkg['name']}", "callback_data": f"buy_{pkg_id}"}
            ]]
        },
    }

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/answerWebAppQuery"
    payload = {"web_app_query_id": query_id, "result": result}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
    except Exception as e:
        logger.error(f"answerWebAppQuery error: {e}")
        return _with_cors(web.json_response({"ok": False, "error": "Gagal menghubungi Telegram"}, status=502))

    if not data.get("ok"):
        logger.error(f"answerWebAppQuery ditolak Telegram: {data}")
        return _with_cors(web.json_response(
            {"ok": False, "error": data.get("description", "Telegram menolak permintaan")}, status=502
        ))

    return _with_cors(web.json_response({"ok": True}))


async def handle_select_package_options(request: web.Request) -> web.Response:
    return _with_cors(web.Response())


async def handle_options(request: web.Request) -> web.Response:
    return _with_cors(web.Response())


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/packages", handle_packages)
    app.router.add_route("OPTIONS", "/api/packages", handle_options)
    app.router.add_post("/api/select-package", handle_select_package)
    app.router.add_route("OPTIONS", "/api/select-package", handle_select_package_options)
    app.router.add_get("/health", handle_health)
    return app


async def start_api_server(port: int) -> web.AppRunner:
    """Jalankan server sebagai background task di event loop yang sedang
    berjalan (dipanggil dari post_init Application PTB). Mengembalikan
    `runner`-nya supaya bisa di-cleanup lewat post_shutdown kalau perlu."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"API server WebApp aktif di port {port} (GET /api/packages)")
    return runner
