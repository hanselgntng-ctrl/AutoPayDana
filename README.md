# Bot Telegram VIP Auto-Payment (DANA)

Bot Telegram yang otomatis memverifikasi bukti transfer DANA menggunakan **OCR** yang
di-**cross-check** ke mutasi/saldo masuk akun **DANA Pribadi** dan **DANA Bisnis**,
lalu otomatis approve/reject akses VIP tanpa admin harus klik manual — dengan
**link akses 1x pakai** yang dibuat bot sendiri, log lengkap ke grup admin, dan
peringatan tegas untuk bukti transfer palsu/duplikat.

## Fitur

- `/start` — teks sapaan + menu utama (bisa diedit admin)
- Menu **Lihat Paket VIP** — tabel paket (nama, harga, durasi) yang bisa diatur admin
- Alur beli: pilih paket → bot kirim **QRIS** + nominal unik → user upload bukti transfer
- **Log bukti transfer real-time** — begitu user mengirim bukti transfer, bot langsung
  meneruskannya ke grup log admin sebelum diproses, jadi semua bukti tercatat
- **Auto-verifikasi dari 2 sumber sekaligus**:
  1. Mutasi resmi API **DANA Bisnis** (kalau kredensial merchant sudah dipasang)
  2. Notifikasi **DANA Pribadi & DANA Bisnis** yang diteruskan dari HP admin ke Telegram
     (lihat penjelasan lengkap di bagian "Deteksi mutasi" di bawah)
- Begitu salah satu sumber konfirmasi dana **benar-benar masuk**, bot:
  1. Kirim pesan **"Status Pembayaran: BERHASIL"** ke grup log (lengkap dengan sumber verifikasinya)
  2. Baru approve pembayaran & aktifkan VIP
- **Link akses dibuat bot sendiri & dibatasi 1 pengguna** — begitu approve, bot memakai
  Telegram Bot API untuk membuat *invite link* baru dengan `member_limit=1` khusus
  untuk grup/channel VIP paket tersebut, lalu mengirimkannya ke user. Link ini **tidak
  bisa dipakai ulang** oleh orang lain, beda dengan link statis biasa yang bisa dibagikan bebas
- **Deteksi bukti transfer duplikat/di-daur ulang** — bot menghitung hash gambar bukti
  transfer; kalau ada gambar yang PERSIS SAMA dipakai di transaksi lain, bot otomatis
  menolak dan mengirim **peringatan keras** ke user yang bersangkutan, sekaligus mencatat
  pelanggaran (strike) dan melaporkannya ke grup log
- Fallback ke admin (grup log) hanya kalau OCR cocok tapi belum ada konfirmasi
  mutasi/notifikasi (jaring pengaman, bukan approval manual rutin)
- `/settings` (khusus admin) — atur:
  - Gambar QRIS
  - Teks sapaan
  - Teks menu VIP
  - Tambah / edit / hapus paket VIP (nama, harga, durasi, deskripsi, **Chat ID grup VIP**, link fallback)
- **Penyimpanan data persisten via Railway Volume** — database, gambar QRIS, dan bukti
  transfer tidak hilang saat bot di-redeploy/restart

## 1. Instalasi (lokal)

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-ind
pip install -r requirements.txt
```

## 2. Konfigurasi dasar

1. Copy `.env.example` menjadi `.env`.
2. Isi `BOT_TOKEN` dari [@BotFather](https://t.me/BotFather).
3. Isi `ADMIN_IDS` dengan Telegram user ID kamu (cek via [@userinfobot](https://t.me/userinfobot)), pisah koma kalau lebih dari satu.
4. Isi `LOG_CHAT_ID` — ID grup Telegram tempat bot mengirim log bukti transfer, status pembayaran, dan peringatan fraud. **Sangat disarankan diisi** karena ini pusat monitoring semua transaksi.

## 3. Link akses VIP otomatis (1x pakai)

Untuk tiap paket VIP, kamu punya 2 opsi (bisa dua-duanya sebagai cadangan):

### Opsi A — Grup/Channel Telegram (direkomendasikan, otomatis dibatasi 1 user)
1. Buat grup/channel Telegram khusus untuk paket VIP tersebut.
2. Tambahkan bot kamu ke grup/channel itu sebagai **admin**, dengan izin **"Invite Users via Link"** dicentang.
3. Cari Chat ID grup/channel itu (forward salah satu pesannya ke [@userinfobot](https://t.me/userinfobot), atau tambahkan @userinfobot ke grup itu).
4. Saat menambah/mengedit paket lewat `/settings`, masukkan Chat ID tersebut saat diminta.
5. Setiap kali ada pembayaran yang disetujui, bot akan **membuat invite link baru** khusus
   untuk pembeli itu, dengan `member_limit=1` — jadi link tersebut otomatis tidak berlaku
   lagi setelah dipakai 1 kali, dan tidak bisa dibagikan ke orang lain.

### Opsi B — Link statis biasa (fallback, TIDAK dibatasi otomatis oleh bot)
Kalau paket tidak berbasis grup Telegram (misalnya link Google Drive, website, dsb),
isi field "link akses statis" saat menambah/edit paket. Bot akan mengirim link ini apa
adanya — karena ini bukan resource yang bot kelola, bot tidak bisa membatasi
penggunaannya hanya untuk 1 orang.

## 4. Deteksi mutasi DANA Pribadi & Bisnis

Bot mengecek **dua sumber** untuk memastikan dana benar-benar masuk sebelum approve:

### Sumber 1 — API resmi DANA Bisnis (opsional, kalau kamu punya akses merchant)

⚠️ DANA tidak menyediakan API publik universal. Untuk pakai jalur ini kamu perlu:
1. Daftar akun **DANA Bisnis** di https://dana.id/bisnis
2. Ajukan akses API/merchant ke tim DANA
3. Isi kredensial (`DANA_MERCHANT_ID`, `DANA_CLIENT_ID`, `DANA_CLIENT_SECRET`, `DANA_PRIVATE_KEY_PATH`) di `.env`
4. Sesuaikan endpoint & format response di `dana_api.py` (`_call_dana_api`, `get_recent_mutations`) sesuai dokumentasi resmi yang kamu terima dari DANA

### Sumber 2 — Notifikasi diteruskan dari HP kamu sendiri (untuk akun DANA Pribadi *dan* Bisnis)

DANA **tidak** punya API resmi untuk mengecek mutasi akun **pribadi**. Cara yang sah dan
aman untuk tetap bisa memverifikasi saldo masuk di akun pribadi adalah dengan meneruskan
notifikasi yang **sudah kamu terima sendiri** di HP-mu ke Telegram — bukan dengan
membobol, reverse-engineer, atau login otomatis ke sistem DANA.

Langkah-langkahnya:

1. Buat 2 chat/grup Telegram privat khusus (boleh 1 chat pribadi kamu dengan bot, atau
   grup terpisah): satu untuk notifikasi akun DANA **Pribadi**, satu untuk DANA **Bisnis**
   (boleh juga digabung jadi satu, bot akan coba menebak jenis akun dari kata "bisnis" di teksnya,
   tapi memisahkannya lebih akurat).
2. Cari Chat ID masing-masing (lewat @userinfobot), isi ke `NOTIF_PERSONAL_CHAT_ID` dan
   `NOTIF_BUSINESS_CHAT_ID` di `.env`.
3. Di HP Android tempat aplikasi DANA (pribadi & bisnis) terpasang, install aplikasi
   forwarder notifikasi seperti **MacroDroid**, **Tasker**, atau **Automate**.
4. Buat automasi: "kalau ada notifikasi baru dari aplikasi DANA yang mengandung kata
   'menerima'/'masuk', kirim teks notifikasi itu ke Telegram" — banyak tutorial untuk
   ini di internet dengan mencari "MacroDroid forward notification to telegram bot".
   Aplikasi tsb mengirim teks lewat `sendMessage` bot Telegram kamu ke chat ID yang sesuai.
5. Begitu notifikasi masuk ke chat tersebut, bot otomatis membaca nominal dari teksnya dan
   menyimpannya sebagai catatan "saldo masuk", untuk dicocokkan ke transaksi user yang pending.

Ini sepenuhnya memakai notifikasi yang memang sudah kamu terima secara sah di HP milikmu
sendiri, hanya diteruskan lewat aplikasi resmi Telegram — bukan mengakses sistem DANA
dengan cara yang tidak semestinya.

**Kalau kamu ingin solusi yang lebih matang/scalable dan diakui resmi**, pertimbangkan
memakai payment gateway pihak ketiga (Midtrans, Xendit, Tripay, dll) yang sudah mendukung
QRIS lintas e-wallet/bank dengan webhook konfirmasi resmi — jauh lebih reliable daripada
kombinasi OCR + forward notifikasi, meskipun ada biaya transaksi.

## 5. Deteksi bukti transfer palsu/duplikat

Bot menghitung SHA-256 hash dari tiap gambar bukti transfer yang masuk. Kalau ada gambar
yang **identik persis** dipakai lagi di transaksi lain (baik oleh user yang sama atau user
lain), bot akan:
- Menolak transaksi tersebut secara otomatis
- Mengirim **peringatan keras** ke user yang bersangkutan
- Mencatat pelanggaran (strike) untuk user tersebut
- Melaporkan ke grup log, dengan penanda khusus kalau user sudah melewati ambang batas (`FRAUD_STRIKE_ALERT_THRESHOLD`, default 3) untuk ditinjau/diblokir admin

Catatan jujur soal batasannya: deteksi ini efektif untuk kasus bukti yang **dipakai
ulang/dibagikan** (hash-nya identik), tapi tidak bisa mendeteksi semua bentuk foto editan
secara pasti (mis. screenshot baru yang dibuat lewat aplikasi edit — hash-nya akan
berbeda). `ocr_utils.error_level_analysis_score()` disediakan sebagai sinyal tambahan
(bukan bukti pasti) yang bisa kamu manfaatkan untuk menambah catatan di log admin kalau
mau dikembangkan lebih lanjut. Keputusan approve yang sesungguhnya tetap selalu bergantung
pada cross-check ke mutasi/notifikasi riil di poin 4, bukan dari analisa gambar semata.

## 6. Jalankan bot (lokal)

```bash
python bot.py
```

## 7. Deploy ke Railway

### a. Buat project & hubungkan repo

1. Push folder ini ke repo GitHub kamu.
2. Di [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → pilih repo ini.
3. Railway otomatis mendeteksi `Dockerfile` di project ini (sudah termasuk instalasi `tesseract-ocr`).

### b. Pasang Volume (WAJIB — supaya data tidak hilang)

1. Service bot → tab **Settings** → **Volumes** → **New Volume**.
2. Set **Mount Path** ke `/app/data`.
3. Simpan & redeploy.

Railway otomatis menyediakan env var `RAILWAY_VOLUME_MOUNT_PATH` yang sudah otomatis
dibaca bot ini (lihat `config.py`) — kamu tidak perlu set `DATA_DIR` manual.

### c. Set environment variables

| Variable | Wajib | Keterangan |
|---|---|---|
| `BOT_TOKEN` | ya | Token dari @BotFather |
| `ADMIN_IDS` | ya | ID Telegram admin, pisah koma |
| `LOG_CHAT_ID` | sangat disarankan | Grup log bukti transfer, status pembayaran, & peringatan fraud |
| `NOTIF_PERSONAL_CHAT_ID`, `NOTIF_BUSINESS_CHAT_ID` | untuk deteksi mutasi personal/bisnis | Lihat bagian 4 |
| `DANA_API_BASE_URL`, `DANA_MERCHANT_ID`, `DANA_CLIENT_ID`, `DANA_CLIENT_SECRET` | opsional | Kredensial DANA Bisnis resmi |
| `FRAUD_STRIKE_ALERT_THRESHOLD` | opsional | Default 3 |

### d. Deploy

Push ke branch yang terhubung, Railway otomatis build & jalankan `python bot.py`
(worker/polling, tidak butuh port HTTP terbuka).

## 8. Struktur file

```
dana_vip_bot/
├── bot.py             # entry point, semua handler Telegram
├── config.py          # konfigurasi (token, admin, kredensial DANA, notif, path data)
├── database.py        # SQLite: settings, paket VIP, transaksi, vip_users, notifikasi, strikes
├── ocr_utils.py        # OCR + hash gambar + heuristik ELA
├── dana_api.py         # cross-check gabungan: API bisnis + notifikasi personal/bisnis
├── keyboards.py        # inline keyboard & format tabel VIP
├── requirements.txt
├── Dockerfile / railway.json / Procfile / .dockerignore
├── .env.example
├── data/               # (lokal) database sqlite, qris, proof — di Railway: pakai Volume
└── qris_images/
```

## Catatan penting soal keamanan & keandalan

- **Nominal unik** (harga + 3 digit kode unik acak) dipakai supaya pencocokan lebih presisi.
- Keputusan approve akhir **selalu** menunggu konfirmasi dari mutasi/notifikasi riil
  (poin 4), bukan dari OCR semata — OCR hanya mempercepat & jadi info tambahan di log.
- Link akses via grup Telegram baru benar-benar terbatas 1 pengguna kalau bot sudah
  jadi admin grup dengan izin membuat invite link; kalau gagal, bot memberi tahu user
  & admin agar bisa ditindaklanjuti manual.
- Simpan private key & kredensial DANA di environment variable Railway, jangan commit ke repo.
- Pastikan Volume Railway sudah terpasang **sebelum** dipakai transaksi sungguhan.
- Metode notifikasi-forward untuk akun pribadi bergantung pada aplikasi forwarder pihak
  ketiga di HP kamu tetap berjalan; pastikan HP tidak mati/offline dalam waktu lama
  supaya notifikasi tidak terlewat.
