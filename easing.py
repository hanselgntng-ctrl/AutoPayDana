"""
easing.py
=========
Kumpulan fungsi easing (dipakai animator Disney & UI/UX Apple/Android) plus
`Animator`, kelas kecil berbasis delta-time NYATA (wall-clock, `time.monotonic()`)
untuk mengganti interpolasi linier (Lerp) polos yang sebelumnya dipakai bot ini.

PENTING soal konteks bot ini (jujur, biar ekspektasinya pas):
Ini bot Telegram (python-telegram-bot), BUKAN game Pygame dengan canvas piksel.
Telegram Bot API tidak expose animasi visual asli — satu-satunya "gerakan" yang
bisa kita tampilkan adalah mengedit ULANG teks pesan (`edit_message_text`), dan
itu pun dibatasi rate-limit Telegram (flood control: kalau satu pesan diedit
terlalu sering/cepat, Telegram akan melempar `RetryAfter`). Jadi versi
"ultra-smooth 60fps" ala game tidak bisa 1:1 dipindah ke sini.

Yang BISA dan SUDAH dilakukan di modul ini, agar tetap dapat esensi tekniknya:
1. Mengganti Lerp (linear, kaku, terasa "robotic") dengan Ease-Out Cubic /
   Elastic / Back — sehingga nilai yang dianimasikan (mis. isi progress bar)
   punya kesan "berbobot": cepat di awal, melambat halus di akhir (prinsip
   animasi Disney "slow in slow out"), atau sedikit "overshoot & settle" ala
   Apple/Material Design.
2. Timeline dihitung dari delta-time NYATA (`time.monotonic()`), bukan asumsi
   "tiap frame delay-nya pasti sama". Jadi kalau ada jeda tak terduga (network
   lag saat memanggil Telegram API), progress animasi tetap akurat & tidak
   "meloncat" — ini teknik delta-time yang sama dipakai game engine (termasuk
   Pygame) supaya animasi tidak stuttering walau framerate tidak stabil.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

EasingFn = Callable[[float], float]


# ── Easing functions ─────────────────────────────────────────────────────────
# Semua menerima t dalam rentang [0.0, 1.0] (progress waktu mentah/linear) dan
# mengembalikan nilai teranimasi (biasanya juga sekitar 0.0-1.0, kecuali yang
# ber-"overshoot" seperti back/elastic yang boleh sedikit lewat dari 1.0).

def linear(t: float) -> float:
    """Lerp klasik -- disertakan hanya sebagai pembanding/fallback, BUKAN yang
    dipakai lagi untuk animasi utama bot ini."""
    return t


def ease_in_cubic(t: float) -> float:
    return t ** 3


def ease_out_cubic(t: float) -> float:
    """Deselerasi halus: gerakan cepat di awal, melambat mulus ke akhir.
    Ini kurva utama yang dipakai transisi menu bot (kesan "settle", tidak kaku)."""
    return 1 - pow(1 - t, 3)


def ease_in_out_cubic(t: float) -> float:
    """Akselerasi di awal, deselerasi di akhir -- cocok untuk animasi yang
    'berangkat pelan, kencang di tengah, mendarat pelan'."""
    if t < 0.5:
        return 4 * t ** 3
    return 1 - pow(-2 * t + 2, 3) / 2


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    """Sedikit 'lewat' target lalu balik pas (overshoot & settle) -- gaya khas
    micro-interaction Apple (mis. ikon yang 'mantul tipis' saat muncul)."""
    c1 = overshoot
    c3 = c1 + 1
    return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)


def ease_out_elastic(t: float) -> float:
    """Efek elastis/mantul ala karet -- paling 'hidup' & organik, gaya animasi
    Disney untuk elemen yang terasa lentur/berbobot (dipakai secukupnya saja,
    karena kalau kebanyakan malah terasa norak di konteks chat)."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    c4 = (2 * math.pi) / 3
    return pow(2, -10 * t) * math.sin((t * 10 - 0.75) * c4) + 1


# ── Animator berbasis delta-time ─────────────────────────────────────────────

@dataclass
class Animator:
    """Pengganti Lerp manual (`current += (target-current)*speed`) yang kaku.

    Dipakai dengan cara:
        anim = Animator(duration=0.6, easing=ease_out_cubic)
        while not anim.is_done():
            nilai = anim.value()       # 0.0 - 1.0, sudah melalui easing
            ...render nilai...
            await asyncio.sleep(tick)

    `duration` dalam detik. `easing` salah satu fungsi di atas (atau custom).
    Progress dihitung dari `time.monotonic()` (delta-time NYATA), jadi tetap
    presisi walau ada jeda I/O (mis. `await bot.edit_message_text(...)` yang
    kadang lambat karena jaringan) -- animasi tidak akan "meloncat" ataupun
    "keburu selesai" gara-gara timing yang tidak presisi.
    """

    duration: float
    easing: EasingFn = ease_out_cubic
    _start_time: float = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.duration = max(0.001, self.duration)
        self._start_time = time.monotonic()

    def reset(self) -> None:
        self._start_time = time.monotonic()

    def raw_progress(self) -> float:
        """Progress waktu MENTAH (linear), 0.0 - 1.0, belum melalui easing."""
        elapsed = time.monotonic() - self._start_time
        return min(1.0, max(0.0, elapsed / self.duration))

    def value(self) -> float:
        """Nilai teranimasi (sesudah easing) untuk waktu SAAT INI dipanggil."""
        return self.easing(self.raw_progress())

    def is_done(self) -> bool:
        return self.raw_progress() >= 1.0


def render_bar(value: float, width: int = 12, filled_char: str = "▓", empty_char: str = "░") -> str:
    """Render progress bar teks dari nilai 0.0-1.0 (hasil easing).
    Nilai di-clamp ke [0,1] dulu supaya kurva yang sedikit overshoot
    (ease_out_back/elastic) tidak menghasilkan bar 'lebih dari penuh'."""
    value = min(1.0, max(0.0, value))
    filled = round(value * width)
    return filled_char * filled + empty_char * (width - filled)
