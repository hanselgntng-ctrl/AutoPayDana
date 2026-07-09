# Dockerfile untuk Bot VIP Auto-Payment
# Dipakai Railway untuk build & jalankan bot. Menggunakan Dockerfile (bukan Nixpacks)
# supaya paket sistem tesseract-ocr terinstall secara pasti dan konsisten.

FROM python:3.11-slim

# Install tesseract-ocr (untuk OCR bukti transfer) + bahasa Indonesia & Inggris
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-ind \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Folder default kalau Volume belum dipasang (Volume Railway akan menimpa mount
# path ini saat runtime sesuai RAILWAY_VOLUME_MOUNT_PATH)
RUN mkdir -p /app/data

# Bot ini bertipe worker (polling Telegram), tidak butuh membuka port HTTP.
CMD ["python", "bot.py"]
