"""Метрики одного запроса синтеза и запись их в JSONL.

Никакой агрегации на лету: каждый запрос -> одна JSON-строка в файл.
Агрегация (перцентили, SLA-оценка) — в report/build_report.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class RequestMetrics:
    """Собирается по ходу стрима одного запроса."""

    session_id: int
    scenario: str
    voice_mode: str            # cached | clone
    text_len_words: int
    sample_rate: int
    underrun_tolerance_ms: float = 50.0

    t_start: float = 0.0
    ttfb_ms: float | None = None
    gen_time_s: float = 0.0
    audio_s: float = 0.0       # длительность сгенерированного аудио
    chunks: int = 0
    underruns: int = 0
    inter_arrival_ms: list[float] = field(default_factory=list)
    error: str | None = None
    active_lines: int = 0      # сколько линий было активно в момент запроса

    _t_prev_chunk: float = field(default=0.0, repr=False)
    _prev_chunk_audio_s: float = field(default=0.0, repr=False)

    def start(self) -> None:
        self.t_start = time.monotonic()

    def on_chunk(self, chunk: bytes) -> None:
        now = time.monotonic()
        chunk_audio_s = len(chunk) / (self.sample_rate * 2)  # PCM s16le, моно
        if self.chunks == 0:
            self.ttfb_ms = (now - self.t_start) * 1000.0
        else:
            gap_ms = (now - self._t_prev_chunk) * 1000.0
            self.inter_arrival_ms.append(gap_ms)
            # Underrun: интервал между соседними чанками превысил длительность
            # аудио в предыдущем чанке (+ допуск на джиттер планировщика).
            if gap_ms > self._prev_chunk_audio_s * 1000.0 + self.underrun_tolerance_ms:
                self.underruns += 1
        self.chunks += 1
        self.audio_s += chunk_audio_s
        self._t_prev_chunk = now
        self._prev_chunk_audio_s = chunk_audio_s

    def finish(self) -> None:
        self.gen_time_s = time.monotonic() - self.t_start

    @property
    def rtf(self) -> float | None:
        """RTF потока = время генерации / длительность сгенерированного аудио."""
        if self.audio_s <= 0:
            return None
        return self.gen_time_s / self.audio_s

    def to_record(self) -> dict:
        return {
            "ts": time.time(),
            "scenario": self.scenario,
            "session_id": self.session_id,
            "voice_mode": self.voice_mode,
            "text_len_words": self.text_len_words,
            "active_lines": self.active_lines,
            "ttfb_ms": round(self.ttfb_ms, 2) if self.ttfb_ms is not None else None,
            "rtf": round(self.rtf, 4) if self.rtf is not None else None,
            "gen_time_s": round(self.gen_time_s, 4),
            "audio_s": round(self.audio_s, 4),
            "chunks": self.chunks,
            "underruns": self.underruns,
            "inter_arrival_p95_ms": round(_p95(self.inter_arrival_ms), 2)
            if self.inter_arrival_ms
            else None,
            "error": self.error,
        }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, round(0.95 * (len(s) - 1))))
    return s[idx]


class JSONLWriter:
    """Построчная запись метрик. Один файл на сценарий/полку."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
