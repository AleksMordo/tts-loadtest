"""Генерация синтетических референс-wav для режима clone (mock-режим).

Для реального прогона на GPU замените файлы в assets/ref_audio/ на настоящие
записи голоса (5–10 сек, формат уточнить у команды TTS — по умолчанию
16 kHz / 16-bit PCM mono для референса).

Запуск: python scripts/gen_ref_audio.py [--out assets/ref_audio] [--count 4]
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import wave
from pathlib import Path


def synth_voice_like(duration_s: float, sample_rate: int, seed: int) -> bytes:
    """Псевдо-речь: сумма гармоник с медленной амплитудной модуляцией и паузами."""
    rng = random.Random(seed)
    f0 = rng.uniform(90, 220)  # «основной тон» условного диктора
    n = int(duration_s * sample_rate)
    samples = []
    for i in range(n):
        t = i / sample_rate
        # огибающая «слогов» ~4 Гц + паузы между «фразами»
        syllable = 0.5 * (1 + math.sin(2 * math.pi * 4.2 * t + seed))
        phrase = 1.0 if (t % 2.5) < 2.0 else 0.05
        v = 0.0
        for k, amp in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12)):
            v += amp * math.sin(2 * math.pi * f0 * k * t)
        v *= 0.15 * syllable * phrase
        v += rng.uniform(-0.01, 0.01)  # лёгкое придыхание
        samples.append(max(-1.0, min(1.0, v)))
    return struct.pack(f"<{n}h", *(int(s * 32767) for s in samples))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="assets/ref_audio")
    p.add_argument("--count", type=int, default=4)
    p.add_argument("--sample-rate", type=int, default=16000)
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(args.count):
        duration = 5.0 + i * 1.5  # 5–9.5 сек
        path = out / f"speaker_{i + 1:02d}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(args.sample_rate)
            wf.writeframes(synth_voice_like(duration, args.sample_rate, seed=i * 17 + 3))
        # Сайдкар-транскрипт (prompt_text для zero-shot). Для реальных записей
        # сюда кладётся точный текст, произнесённый в wav.
        path.with_suffix(".txt").write_text(
            "Надеюсь, у тебя всё будет хорошо.", encoding="utf-8"
        )
        print(f"{path} ({duration:.1f}s) + транскрипт")


if __name__ == "__main__":
    main()
