"""
watermark.py
============
Tempel watermark transparan (dikonversi dari stiker Telegram yang diset admin
lewat /settings) di TENGAH gambar bukti transfer, dipakai saat auto-posting
testimoni ke channel testi (lihat bot.py::post_testimonial).
"""

from PIL import Image

# Opacity watermark (0-255). Sengaja tidak 255 (solid) supaya watermark tetap
# tembus pandang / transparan, tidak menutupi total nominal & info transaksi
# yang ada di bukti transfer di baliknya.
WATERMARK_OPACITY = 110  # ~43% opacity

# Ukuran watermark relatif terhadap LEBAR gambar bukti transfer ("ukuran
# sedang" -> sekitar 45% dari lebar foto, proporsional mengikuti aspect ratio
# stiker aslinya supaya tidak gepeng/melar).
WATERMARK_WIDTH_RATIO = 0.45


def apply_watermark(proof_path: str, watermark_path: str, output_path: str) -> str:
    """Buka gambar bukti transfer (proof_path) & watermark PNG transparan hasil
    konversi stiker (watermark_path), lalu tempel watermark itu di tengah-tengah
    gambar dengan opacity yang dikurangi supaya terlihat sebagai overlay
    transparan, bukan stiker solid yang menutupi bukti transfer. Hasil akhir
    disimpan sebagai JPEG ke output_path, dan path itu dikembalikan."""
    base = Image.open(proof_path).convert("RGBA")
    wm = Image.open(watermark_path).convert("RGBA")

    # Skalakan watermark jadi "ukuran sedang" relatif terhadap lebar bukti
    # transfer, sambil mempertahankan aspect ratio asli stikernya.
    target_w = max(1, int(base.width * WATERMARK_WIDTH_RATIO))
    scale = target_w / wm.width
    target_h = max(1, int(wm.height * scale))
    wm = wm.resize((target_w, target_h), Image.LANCZOS)

    # Kurangi alpha channel watermark supaya jadi transparan/tembus pandang
    # (bukan mengganti background jadi putih/hitam, tetap RGBA asli, hanya
    # opacity-nya yang diturunkan).
    alpha = wm.split()[3].point(lambda a: min(a, WATERMARK_OPACITY))
    wm.putalpha(alpha)

    # Tempel tepat di tengah gambar bukti transfer.
    x = (base.width - wm.width) // 2
    y = (base.height - wm.height) // 2
    composited = base.copy()
    composited.paste(wm, (x, y), wm)  # wm sebagai mask -> alpha channel-nya dipakai untuk blending

    composited.convert("RGB").save(output_path, "JPEG", quality=92)
    return output_path
