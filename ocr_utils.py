"""
ocr_utils.py
============
Ekstrak informasi (nominal transfer, tanggal, nama penerima, kode referensi)
dari gambar bukti transfer menggunakan Tesseract OCR.

Verifikasi bukti transfer 100% LOKAL: bot MENCOCOKKAN LANGSUNG hasil OCR
gambar ini ke 3 hal (nama penerima QRIS, tanggal, nominal) -- TIDAK cross-check
ke API pihak ketiga ataupun notifikasi HP yang diteruskan admin. Konsekuensinya:
akurasi OCR jadi satu-satunya garda, jadi preprocessing gambar & toleransi
pencocokan (lihat name_matches, config.PROOF_DATE_TOLERANCE_HOURS) sengaja
dibuat tidak longgar tapi juga tidak 100% kaku (mentolerir noise OCR wajar),
supaya tidak salah tolak transaksi yang sebenarnya sah.
"""

import re
import io
import difflib
import hashlib
import datetime
import pytesseract
from pytesseract import Output
from PIL import Image, ImageOps, ImageFilter, ImageChops, ImageStat, ImageDraw

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


_BULAN_ID = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2, "mar": 3, "maret": 3,
    "apr": 4, "april": 4, "mei": 5, "may": 5, "jun": 6, "juni": 6,
    "jul": 7, "juli": 7, "agu": 8, "ags": 8, "agustus": 8, "aug": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10, "oct": 10,
    "nov": 11, "november": 11, "des": 12, "desember": 12, "dec": 12,
}


def extract_date(text: str) -> datetime.datetime | None:
    """Cari tanggal (& jam kalau ada) transaksi dari teks bukti transfer.
    Mendukung format umum struk e-wallet Indonesia:
    '10 Jul 2026, 14:23', '10 Juli 2026 14:23:45', '10/07/2026', '2026-07-10'.

    Kalau jam tidak ketemu, dikembalikan sebagai jam 00:00 di tanggal itu --
    cukup untuk dicocokkan lewat toleransi dalam hitungan JAM
    (config.PROOF_DATE_TOLERANCE_HOURS), bukan butuh presisi ke detik."""
    def _try_time(s: str):
        m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
        if m:
            h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if 0 <= h < 24 and 0 <= mi < 60:
                return h, mi, se
        return 0, 0, 0

    # Format "10 Jul(i) 2026" / "10 January 2026"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", text)
    if m:
        day, mon_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _BULAN_ID.get(mon_str[:4]) or _BULAN_ID.get(mon_str[:3])
        if month and 1 <= day <= 31:
            h, mi, se = _try_time(text[m.end(): m.end() + 20])
            try:
                return datetime.datetime(year, month, day, h, mi, se)
            except ValueError:
                pass

    # Format "10/07/2026" atau "10-07-2026" (DD/MM/YYYY -- format Indonesia)
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b", text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi, se = _try_time(text[m.end(): m.end() + 20])
        try:
            return datetime.datetime(year, month, day, h, mi, se)
        except ValueError:
            pass

    # Format ISO "2026-07-10"
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi, se = _try_time(text[m.end(): m.end() + 20])
        try:
            return datetime.datetime(year, month, day, h, mi, se)
        except ValueError:
            pass

    return None


def _normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def name_matches(expected_name: str, ocr_raw_text: str, threshold: float = 0.82) -> bool:
    """Cek apakah nama penerima yang diharapkan (config.QRIS_RECIPIENT_NAME)
    muncul di teks hasil OCR bukti transfer -- dengan toleransi fuzzy (bukan
    exact match), karena OCR sering salah baca 1-2 huruf (mis. "ZONA BASAH"
    kebaca "Z0NA BASAH"). Dicek dengan 2 cara:
    1. Substring langsung (case-insensitive, tanpa simbol) -- kalau OCR
       kebetulan bersih, ini langsung match.
    2. Sliding window kata demi kata + SequenceMatcher ratio -- menangani
       kasus OCR meleset dikit tapi jumlah kata & posisinya masih mirip."""
    if not expected_name or not expected_name.strip():
        return True  # admin belum isi nama -> lewati pengecekan ini (lihat config.py)

    expected_norm = _normalize_name(expected_name)
    text_norm = _normalize_name(ocr_raw_text)

    if expected_norm in text_norm:
        return True

    expected_words = expected_norm.split()
    text_words = text_norm.split()
    window = len(expected_words)
    if window == 0 or len(text_words) < window:
        return False

    best_ratio = 0.0
    for i in range(len(text_words) - window + 1):
        candidate = " ".join(text_words[i:i + window])
        ratio = difflib.SequenceMatcher(None, expected_norm, candidate).ratio()
        best_ratio = max(best_ratio, ratio)

    return best_ratio >= threshold


def analyze_proof(image_path: str) -> dict:
    """Fungsi utama: kembalikan dict berisi hasil OCR terstruktur."""
    text = extract_text(image_path)
    return {
        "raw_text": text,
        "amount": extract_amount(text),
        "date": extract_date(text),
        "reference_code": extract_reference_code(text),
        "recipient_hint": extract_recipient_hint(text),
    }


def compute_image_hash(image_path: str) -> str:
    """Hash SHA-256 dari isi file gambar. Dipakai untuk mendeteksi kalau ada bukti
    transfer yang PERSIS SAMA dikirim ulang untuk transaksi lain (indikasi kuat
    penyalahgunaan/bukti daur ulang), tanpa perlu menyimpan gambar aslinya selamanya."""
    with open(image_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ── Sensor otomatis no. rekening & nama pengirim (buat posting ke channel testi) ──
# Dipanggil HANYA saat menyiapkan versi bukti transfer yang akan diposting
# publik ke channel testi (lihat bot.py::post_testimonial) -- gambar ASLI yang
# dipakai untuk verifikasi OCR/approve TIDAK disensor, supaya proses verifikasi
# tetap membaca data lengkap & akurat. Sensor hanya untuk salinan yang tampil
# ke publik, demi privasi pembeli (no. rekening & nama pengirim bukan urusan
# publik yang lihat testimoni).

_KEYWORD_SENDER_LINE = re.compile(
    r"(dari|pengirim|nama\s*pengirim|sumber\s*dana|no\.?\s*rekening|rekening\s*(?:tujuan|sumber|pengirim)?|"
    r"a\.?n\.?|atas\s*nama)",
    re.IGNORECASE,
)


def _group_words_by_line(ocr_data: dict) -> list[list[int]]:
    """Kelompokkan index kata (dari pytesseract.image_to_data) yang ada di baris
    yang sama (kombinasi block_num+par_num+line_num), supaya bisa dianalisis
    per-baris teks, bukan per-kata lepas."""
    lines: dict[tuple, list[int]] = {}
    n = len(ocr_data["text"])
    for i in range(n):
        if not ocr_data["text"][i].strip():
            continue
        key = (ocr_data["block_num"][i], ocr_data["par_num"][i], ocr_data["line_num"][i])
        lines.setdefault(key, []).append(i)
    return list(lines.values())


def detect_sensitive_boxes(image_path: str) -> list[tuple[int, int, int, int]]:
    """Deteksi bounding box (left, top, width, height) area yang berpotensi
    berisi NO. REKENING atau NAMA PENGIRIM di gambar bukti transfer, dari
    layout hasil OCR (pytesseract.image_to_data, bukan cuma teks polos).

    Heuristik dipakai (bukan jaminan 100% -- OCR/layout struk bisa bervariasi
    antar aplikasi e-wallet, admin tetap disarankan cek sekilas hasil postingan
    testi kalau ada yang terasa janggal):
    1. Baris yang mengandung kata kunci ("dari", "pengirim", "no rekening",
       "a.n.", dst) -> bagian SETELAH kata kunci di baris itu (nilainya, bukan
       label-nya) ikut disensor.
    2. Token angka polos sepanjang 8-20 digit di MANAPUN (ciri khas nomor
       rekening/telepon) -- KECUALI token itu didahului kata "Rp" di baris
       yang sama (supaya nominal transaksi, yang justru harus tetap terlihat
       sebagai bukti, tidak ikut tersensor)."""
    img = preprocess_image(image_path)
    data = pytesseract.image_to_data(img, lang="ind+eng", output_type=Output.DICT)
    boxes = []

    for line_idx_list in _group_words_by_line(data):
        words = [data["text"][i] for i in line_idx_list]
        line_text = " ".join(words)
        has_rp = any(w.strip().lower().startswith("rp") for w in words)

        keyword_match = _KEYWORD_SENDER_LINE.search(line_text)
        if keyword_match:
            # Cari kata kunci ada di token index ke berapa dalam baris ini,
            # lalu sensor SEMUA token SETELAHNYA (nilai/isinya), bukan label-nya.
            consumed = 0
            keyword_word_end_idx = None
            for w_pos, i in enumerate(line_idx_list):
                consumed += len(words[w_pos]) + 1
                if consumed > keyword_match.end():
                    keyword_word_end_idx = w_pos
                    break
            if keyword_word_end_idx is not None:
                value_indices = line_idx_list[keyword_word_end_idx + 1:]
                if value_indices:
                    xs = [data["left"][i] for i in value_indices]
                    ys = [data["top"][i] for i in value_indices]
                    x2s = [data["left"][i] + data["width"][i] for i in value_indices]
                    y2s = [data["top"][i] + data["height"][i] for i in value_indices]
                    boxes.append((min(xs), min(ys), max(x2s) - min(xs), max(y2s) - min(ys)))
                continue  # baris ini sudah ditangani lewat jalur kata kunci

        # Token angka panjang (kemungkinan no. rekening/telepon) di baris TANPA "Rp"
        if not has_rp:
            for w_pos, i in enumerate(line_idx_list):
                token = re.sub(r"\D", "", words[w_pos])
                if 8 <= len(token) <= 20 and token == re.sub(r"[.\-\s]", "", words[w_pos]):
                    boxes.append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))

    return boxes


def censor_sensitive_info(image_path: str, output_path: str) -> str:
    """Tempel kotak hitam solid di atas area yang terdeteksi
    detect_sensitive_boxes() (no. rekening & nama pengirim), lalu simpan hasil
    ke output_path. Dipakai SEBELUM watermark ditempel (lihat
    bot.py::post_testimonial), supaya urutan akhirnya: sensor dulu -> baru
    watermark di atasnya."""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    padding = 3  # px, supaya kotak sensor sedikit lebih besar dari box teks asli (tidak mepet/ke-crop)

    for (x, y, w, h) in detect_sensitive_boxes(image_path):
        draw.rectangle(
            [x - padding, y - padding, x + w + padding, y + h + padding],
            fill=(0, 0, 0),
        )

    img.save(output_path, "JPEG", quality=92)
    return output_path


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
