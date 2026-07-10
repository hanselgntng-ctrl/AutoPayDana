"""
dana_api.py
===========
Modul cross-check pembayaran ke mutasi/saldo masuk akun DANA — mendukung DUA sumber:

1. API resmi DANA Bisnis (get_recent_mutations / _call_dana_api) — kalau kamu sudah
   punya kredensial merchant resmi dari DANA. Lihat catatan panjang di bawah.

2. Notifikasi saldo masuk yang diteruskan dari HP admin (akun DANA PRIBADI *dan*
   DANA Bisnis) ke Telegram — lihat database.find_unconsumed_notification() &
   bot.py::handle_incoming_notification(). Ini dipakai karena DANA tidak
   menyediakan API resmi untuk akun pribadi; cara paling aman dan sah untuk
   memantau saldo masuk di akun pribadi adalah dengan meneruskan notifikasi yang
   memang sudah kamu terima di HP-mu sendiri (pakai aplikasi forwarder notifikasi
   seperti MacroDroid/Tasker/Automate → kirim ke bot Telegram lewat sendMessage),
   BUKAN dengan reverse-engineering atau login otomatis ke akun DANA.

find_matching_mutation() mengecek kedua sumber itu dan mengembalikan info dari
sumber mana pembayaran terverifikasi (business_api / notif_personal / notif_bisnis).

⚠️ PENTING soal API resmi DANA Bisnis — WAJIB DIBACA:
DANA tidak menyediakan satu endpoint publik universal yang bisa langsung dipakai
siapa saja untuk "cek mutasi otomatis". Untuk bisa query mutasi transaksi secara
programatik, kamu perlu:
  1. Mendaftar sebagai merchant/partner resmi di DANA Bisnis
     (https://dana.id/bisnis atau melalui akun DANA Bisnis kamu).
  2. Mendapatkan kredensial resmi: MERCHANT_ID, CLIENT_ID, CLIENT_SECRET,
     dan private key untuk signing request (umumnya API DANA memakai standar
     signature RSA/SHA256 mirip Snap BI - Standar Nasional Open API Pembayaran).
  3. Membaca dokumentasi API resmi yang diberikan pihak DANA ke akun bisnismu,
     karena struktur endpoint & field bisa berbeda tergantung jenis produk
     (QRIS DANA Bisnis, Payment Gateway DANA, dsb).

Fungsi-fungsi di bawah ini sudah disiapkan strukturnya (signing request, request
mutasi, pencarian transaksi yang cocok) tapi bagian `_call_dana_api()` HARUS kamu
sesuaikan dengan endpoint & format request/response asli sesuai dokumen resmi yang
kamu terima dari DANA. Tanpa itu, jalur API bisnis ini tidak akan bisa terhubung ke
server DANA — tapi bot tetap bisa jalan lewat jalur notifikasi (poin 2 di atas).
"""

import time
import base64
import hashlib
import datetime
import requests

import config
import database as db


def _generate_signature(payload: str) -> str:
    """
    Placeholder pembuatan signature request. Sesuaikan algoritma sesuai
    dokumentasi resmi DANA Bisnis (umumnya RSA-SHA256 dengan private key merchant).
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        with open(config.DANA_PRIVATE_KEY_PATH, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)

        signature = private_key.sign(
            payload.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()
    except FileNotFoundError:
        # Kalau private key belum di-setup, kembalikan string kosong.
        # Pada tahap ini bot masih bisa jalan pakai OCR-only sebagai fallback.
        return ""


def _call_dana_api(endpoint: str, body: dict) -> dict:
    """
    Kirim request ke API DANA Bisnis. SESUAIKAN header, path, dan format body
    di bawah ini dengan dokumentasi resmi yang kamu terima dari DANA.
    """
    timestamp = datetime.datetime.utcnow().isoformat()
    signature = _generate_signature(str(body) + timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-TIMESTAMP": timestamp,
        "X-CLIENT-KEY": config.DANA_CLIENT_ID,
        "X-SIGNATURE": signature,
    }

    url = f"{config.DANA_API_BASE_URL}{endpoint}"
    response = requests.post(url, json=body, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()


def get_recent_mutations(minutes: int = None) -> list[dict]:
    """
    Ambil daftar mutasi (transaksi masuk) terbaru dari akun DANA Bisnis.
    Format kembalian yang diharapkan tiap item minimal punya:
      { "amount": int, "reference_id": str, "timestamp": iso-string, "status": "SUCCESS" }
    SESUAIKAN parsing response.json() di bawah dengan struktur asli dari API DANA.
    """
    minutes = minutes or config.DANA_MATCH_TIME_WINDOW_MINUTES
    body = {
        "merchantId": config.DANA_MERCHANT_ID,
        "fromTime": (datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)).isoformat(),
        "toTime": datetime.datetime.utcnow().isoformat(),
    }
    try:
        data = _call_dana_api("/v1/mutation/history", body)  # ganti path sesuai dokumen resmi
        return data.get("mutations", [])
    except Exception as e:
        # Kalau API belum ter-setup / gagal konek, kembalikan list kosong
        # supaya bot fallback ke keputusan berbasis OCR + review manual.
        print(f"[dana_api] Gagal mengambil mutasi: {e}")
        return []


def find_matching_mutation(expected_amount: int, unique_code: str, ocr_amount: int = None) -> dict | None:
    """
    Cari bukti bahwa dana benar-benar masuk, dari SEMUA sumber yang tersedia:

      1. Mutasi resmi API DANA Bisnis (kalau sudah dikonfigurasi)
      2. Notifikasi DANA Pribadi yang diteruskan ke Telegram
      3. Notifikasi DANA Bisnis yang diteruskan ke Telegram

    Mengembalikan dict {"source": ..., "amount": ..., "notif_id": ... (kalau dari notifikasi)}
    atau None kalau tidak ditemukan kecocokan di sumber manapun.
    """
    tolerance = config.DANA_MATCH_AMOUNT_TOLERANCE
    window = config.DANA_MATCH_TIME_WINDOW_MINUTES

    # 1) API resmi DANA Bisnis
    mutations = get_recent_mutations()
    for mut in mutations:
        amount_match = abs(mut.get("amount", 0) - expected_amount) <= tolerance
        status_ok = mut.get("status", "").upper() == "SUCCESS"
        if amount_match and status_ok:
            return {"source": "business_api", "amount": mut.get("amount"), "raw": mut}

    # 2) & 3) Notifikasi DANA Pribadi & Bisnis yang diteruskan ke Telegram
    notif = db.find_unconsumed_notification(expected_amount, tolerance, window)
    if notif:
        return {
            "source": f"notif_{notif['account_type']}",
            "amount": notif["amount"],
            "notif_id": notif["id"],
        }

    # Fallback: nominal hasil OCR dicocokkan juga ke ketiga sumber di atas
    # (berguna kalau nominal unik di notifikasi sedikit berbeda format penulisannya)
    if ocr_amount:
        for mut in mutations:
            if mut.get("amount") == ocr_amount and mut.get("status", "").upper() == "SUCCESS":
                return {"source": "business_api", "amount": mut.get("amount"), "raw": mut}

        notif = db.find_unconsumed_notification(ocr_amount, tolerance, window)
        if notif:
            return {
                "source": f"notif_{notif['account_type']}_ocr_fallback",
                "amount": notif["amount"],
                "notif_id": notif["id"],
            }

    return None


def generate_unique_code(base_amount: int, tx_counter: int) -> tuple[int, str]:
    """
    Buat nominal unik (contoh: 50000 -> 50123) supaya matching mutasi lebih presisi
    saat banyak user membayar nominal yang sama secara bersamaan.
    Kembalikan (nominal_final, kode_unik_3_digit).
    """
    unique_suffix = (tx_counter % 899) + 100  # angka 100-998
    final_amount = base_amount + unique_suffix
    return final_amount, str(unique_suffix)
