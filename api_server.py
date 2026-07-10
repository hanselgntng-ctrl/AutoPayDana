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

from aiohttp import web

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


async def handle_options(request: web.Request) -> web.Response:
    return _with_cors(web.Response())


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/packages", handle_packages)
    app.router.add_route("OPTIONS", "/api/packages", handle_options)
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
