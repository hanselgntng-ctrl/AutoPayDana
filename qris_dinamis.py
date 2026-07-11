"""
qris_dinamis.py
===============
Modul untuk mengubah QRIS STATIS (yang Anda dapat dari akun DANA Bisnis)
menjadi QRIS DINAMIS -- yaitu QR code baru yang sudah menyertakan NOMINAL
transaksi tertentu, dibuat murni lewat manipulasi string EMVCo secara lokal
(TIDAK memanggil API DANA/pihak ketiga mana pun -- semuanya dihitung sendiri
di server Anda).

Dependency: opencv-python-headless (`pip install opencv-python-headless
--break-system-packages`). Dipilih dibanding pyzbar supaya TIDAK perlu
install library sistem tambahan (pyzbar butuh `apt-get install libzbar0`,
yang kadang tidak bisa dilakukan di shared hosting/VPS terbatas) -- opencv
sudah menyediakan encoder & decoder QR bawaan lewat pip install biasa.

Cara kerja singkat format EMVCo (dipakai QRIS/QR Code Indonesian Standard):
Setiap data dibungkus TLV (Tag-Length-Value):
    - 2 digit Tag
    - 2 digit Length (jumlah karakter Value)
    - Value sepanjang Length tsb
Contoh: "54" (tag Transaction Amount) + "06" (length 6) + "150000" (value)
        -> "5406150000"

Field-field yang relevan untuk konversi statis -> dinamis:
    - Tag 01 (Point of Initiation Method): "11" = statis, "12" = dinamis.
      Wajib diubah ke "12" begitu kita menyisipkan nominal.
    - Tag 54 (Transaction Amount): TIDAK ADA di QRIS statis. Kita sisipkan
      tag ini (posisinya harus tepat setelah tag 53/Transaction Currency,
      sesuai urutan field standar EMVCo).
    - Tag 63 (CRC): checksum CRC-16/CCITT-FALSE dari SELURUH string
      (termasuk tag "6304" di akhir, TIDAK termasuk 4 karakter nilai CRC
      itu sendiri). Wajib dihitung ulang setiap kali isi QR berubah,
      kalau tidak, QR akan ditolak/invalid saat discan.
"""

import io
import re

import cv2
import numpy as np


class QRISDecodeError(Exception):
    """Dilempar kalau gambar yang diberikan tidak mengandung QR code yang
    bisa dibaca (bukan foto QRIS yang valid, blur, atau rusak)."""


def decode_qris_image(path: str) -> str:
    """Baca file gambar QRIS statis di `path`, kembalikan string EMVCo
    mentahnya. Dipanggil SEKALI SAJA setiap kali admin upload/ganti gambar
    QRIS lewat /settings (bukan setiap kali ada pembelian) -- hasilnya
    disimpan di database (lihat db.set_setting('qris_static_string', ...))
    supaya tidak perlu decode ulang gambar setiap transaksi."""
    img = cv2.imread(path)
    if img is None:
        raise QRISDecodeError(f"Gagal membuka file gambar: {path}")

    detector = cv2.QRCodeDetector()
    data, points, _ = detector.detectAndDecode(img)

    if not data:
        raise QRISDecodeError(
            "Tidak ada QR code yang terbaca di gambar ini. Pastikan foto QRIS "
            "tidak blur/terpotong, dan kode QR-nya terlihat utuh & jelas."
        )
    return data


def generate_qris_image(qris_string: str, output_path: str, size: int = 700):
    """Buat file gambar PNG dari string QRIS (statis maupun dinamis) di
    `output_path`. `size` adalah lebar/tinggi gambar akhir dalam piksel
    (di-upscale dari matriks QR asli memakai nearest-neighbor supaya tetap
    tegas/tidak blur -- penting supaya QR tetap gampang discan aplikasi
    pembayaran)."""
    encoder = cv2.QRCodeEncoder.create()
    matrix = encoder.encode(qris_string)
    upscaled = cv2.resize(matrix, (size, size), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(output_path, upscaled)
import io

import cv2


def decode_qris_image(image_path: str) -> str:
    """Baca file gambar QRIS statis (hasil upload admin lewat /settings ->
    'Atur QRIS (gambar)') dan kembalikan string mentah EMVCo-nya.

    Dipanggil setiap kali ada user mulai pembelian -- dengan begitu, kalau
    admin ganti gambar QRIS di /settings, generate QR dinamis otomatis ikut
    pakai QRIS yang PALING BARU tanpa perlu restart bot atau ubah kode.

    Melempar ValueError kalau file tidak bisa dibaca atau tidak ada QR
    code yang kebaca sama sekali di gambar tsb (mis. gambar rusak/blur)."""
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Tidak bisa membaca file gambar QRIS: {image_path}")

    detector = cv2.QRCodeDetector()
    data, _points, _straight = detector.detectAndDecode(img)
    if not data:
        raise ValueError(
            "Tidak ada kode QR yang terbaca dari gambar QRIS ini "
            "(coba upload ulang foto QRIS yang lebih jelas/tidak blur)"
        )
    return data


def generate_qr_png_bytes(qris_string: str, size: int = 600) -> bytes:
    """Generate gambar QR (format PNG, dalam bytes) dari string QRIS --
    dipakai untuk kirim balik ke Telegram lewat send_photo() tanpa perlu
    simpan file sementara ke disk (langsung dari memory pakai io.BytesIO)."""
    encoder = cv2.QRCodeEncoder.create()
    img = encoder.encode(qris_string)
    img_upscaled = cv2.resize(img, (size, size), interpolation=cv2.INTER_NEAREST)
    success, buf = cv2.imencode(".png", img_upscaled)
    if not success:
        raise ValueError("Gagal encode gambar QR ke format PNG")
    return buf.tobytes()


def build_dynamic_qris_png(static_image_path: str, amount: int) -> bytes:
    """Fungsi gabungan (dipakai langsung dari bot.py): baca QRIS statis dari
    `static_image_path`, sisipkan `amount`, kembalikan PNG (bytes) siap kirim.
    Melempar ValueError yang sama seperti decode_qris_image()/inject_amount()
    kalau ada langkah yang gagal -- pemanggil WAJIB siapkan fallback ke
    gambar QRIS statis asli kalau ini gagal (lihat catatan di bot.py)."""
    static_string = decode_qris_image(static_image_path)
    dynamic_string = inject_amount(static_string, amount)
    return generate_qr_png_bytes(dynamic_string)


def _crc16_ccitt_false(data: str) -> str:
    """Hitung CRC-16/CCITT-FALSE (polynomial 0x1021, initial value 0xFFFF)
    dari string `data`, kembalikan sebagai 4 karakter HEX UPPERCASE --
    persis format yang dipakai QRIS di tag 63."""
    crc = 0xFFFF
    for ch in data.encode("ascii"):
        crc ^= ch << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return format(crc, "04X")


def _parse_tlv(s: str):
    """Parse string EMVCo jadi list (tag, length, value). Tidak rekursif --
    dipakai untuk parsing level atas saja."""
    result = []
    i = 0
    while i < len(s):
        tag = s[i:i + 2]
        length = int(s[i + 2:i + 4])
        value = s[i + 4:i + 4 + length]
        result.append((tag, value))
        i += 4 + length
    return result


def _build_tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def is_static_qris(qris_string: str) -> bool:
    """True kalau tag 01 (Point of Initiation Method) == '11' (statis)."""
    for tag, value in _parse_tlv(qris_string):
        if tag == "01":
            return value == "11"
    return False


def inject_amount(qris_string: str, amount: int) -> str:
    """Ambil QRIS STATIS `qris_string`, kembalikan versi BARU yang sudah
    disisipi nominal `amount` (dalam Rupiah, integer, TANPA desimal --
    QRIS Indonesia pakai integer rupiah, bukan sen).

    Langkah:
    1. Buang tag 63 (CRC) lama -- akan dihitung ulang di akhir.
    2. Ubah tag 01 dari '11' (statis) jadi '12' (dinamis).
    3. Sisipkan/timpa tag 54 (Transaction Amount) tepat setelah tag 53
       (Transaction Currency), sesuai urutan field standar EMVCo.
    4. Hitung ulang CRC atas seluruh string (termasuk '6304' di akhir),
       lalu gabungkan jadi QRIS dinamis yang utuh & valid.

    Melempar ValueError kalau tag wajib (00/01/53) tidak ditemukan --
    tandanya `qris_string` bukan QRIS yang valid/lengkap.
    """
    if amount <= 0:
        raise ValueError("Nominal transaksi harus lebih besar dari 0")

    fields = [(t, v) for t, v in _parse_tlv(qris_string) if t != "63"]

    tag_map = {t: v for t, v in fields}
    if "00" not in tag_map or "53" not in tag_map:
        raise ValueError("String QRIS tidak valid: tag wajib (00/53) tidak ditemukan")

    amount_str = str(int(amount))  # QRIS pakai integer rupiah, tanpa desimal/koma

    new_fields = []
    inserted = False
    for tag, value in fields:
        if tag == "01":
            new_fields.append(("01", "12"))  # paksa jadi dinamis
            continue
        if tag == "54":
            # tag 54 lama (harusnya tidak ada di QRIS statis, tapi jaga-jaga
            # kalau suatu saat dipakai ulang untuk QRIS yang SUDAH dinamis)
            continue
        new_fields.append((tag, value))
        if tag == "53" and not inserted:
            new_fields.append(("54", amount_str))
            inserted = True

    if not inserted:
        # Fallback: kalau entah kenapa tag 53 tidak ada, taruh tag 54 di akhir
        # sebelum CRC (seharusnya tidak pernah kejadian karena sudah dicek di atas)
        new_fields.append(("54", amount_str))

    body = "".join(_build_tlv(t, v) for t, v in new_fields)
    body_with_crc_tag = body + "6304"
    crc = _crc16_ccitt_false(body_with_crc_tag)
    return body_with_crc_tag + crc


def extract_merchant_name(qris_string: str) -> str:
    """Ambil nama merchant (tag 59) dari QRIS -- untuk ditampilkan sebagai
    konfirmasi ke admin/user, memastikan QRIS yang diproses benar."""
    for tag, value in _parse_tlv(qris_string):
        if tag == "59":
            return value
    return "(tidak diketahui)"


if __name__ == "__main__":
    # Uji cepat pakai QRIS statis asli yang Anda upload.
    static_qris = (
        "00020101021126570011ID.DANA.WWW011893600915389549064402098954906440"
        "303UMI51440014ID.CO.QRIS.WWW0215ID10254001404280303UMI5204481453033"
        "605802ID5913RKDESTU STORE6015Kota Pekalongan61055114163047CAA"
    )

    print("Merchant terdeteksi:", extract_merchant_name(static_qris))
    print("Apakah statis?      ", is_static_qris(static_qris))

    dynamic_qris = inject_amount(static_qris, 50000)
    print("\nQRIS dinamis (Rp50.000):")
    print(dynamic_qris)
