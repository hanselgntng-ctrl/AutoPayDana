"""
ocr_utils.py
============
Ekstrak informasi (nominal transfer, kode referensi, nama penerima) dari
gambar bukti transfer menggunakan Tesseract OCR.

Catatan: hasil OCR pada screenshot aplikasi e-wallet biasanya cukup bersih,
tapi tetap tidak 100% akurat. Karena itu hasil OCR selalu di-cross-check lagi
dengan mutasi resmi DANA Bisnis (lihat dana_api.py) sebelum bot approve otomatis.
"""

import re
import io
import hashlib
import pytesseract
from PIL import Image, ImageOps, ImageFilter, ImageChops, ImageStat

import config

pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


def preprocess_image(image_path: str) -> Image.Image:
    """Preprocessing sederhana supaya OCR lebih akurat: grayscale, kontras, sharpen."""
    img = Image.open(image_path)
    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def extract_text(image_path: str) -> str:
    img = preprocess_image(image_path)
    text = pytesseract.image_to_string(img, lang="ind+eng")
    return text


def extract_amount(text: str) -> int | None:
    """
    Cari pola nominal uang seperti:
    'Rp100.000', 'Rp 100,000', 'Nominal: Rp150.000,00', '250000'
    Mengembalikan integer rupiah atau None kalau tidak ketemu.
    """
    patterns = [
        r"Rp\.?\s?([\d.,]+)",
        r"nominal\s*[:\-]?\s*Rp?\.?\s?([\d.,]+)",
        r"jumlah\s*[:\-]?\s*Rp?\.?\s?([\d.,]+)",
        r"total\s*[:\-]?\s*Rp?\.?\s?([\d.,]+)",
    ]
    candidates = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1)
            cleaned = raw.replace(".", "").replace(",", "")
            # buang desimal sen kalau ada (2 digit terakhir setelah koma asli)
            cleaned = re.sub(r"00$", "", cleaned) if "," in raw and len(cleaned) > 5 else cleaned
            if cleaned.isdigit():
                candidates.append(int(cleaned))
    if not candidates:
        return None
    # ambil nominal terbesar yang masuk akal (biasanya itu nominal transaksi utama)
    return max(candidates)


def extract_reference_code(text: str) -> str | None:
    """
    Cari nomor referensi / ID transaksi, biasanya berupa deretan angka/huruf panjang
    seperti 'No. Ref: 1234567890123' atau 'Transaction ID: ABCD1234'.
    """
    patterns = [
        r"(?:no\.?\s?ref(?:erensi)?|reference|transaction\s?id|id\s?transaksi)\s*[:\-]?\s*([A-Za-z0-9]{6,})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def extract_recipient_hint(text: str) -> str | None:
    """Cari nama/nomor akun tujuan transfer, kalau ada, untuk pencocokan tambahan."""
    patterns = [
        r"(?:ke|tujuan|penerima)\s*[:\-]?\s*([A-Za-z\s]{3,40})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def analyze_proof(image_path: str) -> dict:
    """Fungsi utama: kembalikan dict berisi hasil OCR terstruktur."""
    text = extract_text(image_path)
    return {
        "raw_text": text,
        "amount": extract_amount(text),
        "reference_code": extract_reference_code(text),
        "recipient_hint": extract_recipient_hint(text),
    }


def compute_image_hash(image_path: str) -> str:
    """Hash SHA-256 dari isi file gambar. Dipakai untuk mendeteksi kalau ada bukti
    transfer yang PERSIS SAMA dikirim ulang untuk transaksi lain (indikasi kuat
    penyalahgunaan/bukti daur ulang), tanpa perlu menyimpan gambar aslinya selamanya."""
    with open(image_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def error_level_analysis_score(image_path: str, quality: int = 90) -> float:
    """
    Heuristik Error Level Analysis (ELA) sederhana: gambar disimpan ulang sebagai
    JPEG kualitas tertentu, lalu dibandingkan dengan aslinya. Area yang sudah
    di-edit/ditempel biasanya punya level kompresi berbeda dari area asli,
    sehingga menghasilkan skor perbedaan yang lebih tinggi.

    PENTING: ini HANYA sinyal tambahan untuk membantu admin melakukan review,
    BUKAN bukti pasti bahwa gambar sudah diedit. Skor tinggi bisa juga muncul
    dari kompresi ulang oleh aplikasi chat/e-wallet itu sendiri. Jangan jadikan
    ini satu-satunya dasar untuk menuduh atau menghukum pengguna.
    """
    try:
        original = Image.open(image_path).convert("RGB")
        buffer = io.BytesIO()
        original.save(buffer, "JPEG", quality=quality)
        buffer.seek(0)
        resaved = Image.open(buffer)
        diff = ImageChops.difference(original, resaved)
        stat = ImageStat.Stat(diff)
        return sum(stat.mean) / len(stat.mean)
    except Exception:
        return 0.0
