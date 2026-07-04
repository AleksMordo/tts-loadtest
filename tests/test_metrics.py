"""Юнит-тесты метрик: TTFB, RTF, детекция underrun, перцентили."""

import time

from loadgen.metrics import RequestMetrics
from report.aggregate import aggregate, check_sla, quantile

SR = 8000
BYTES_PER_S = SR * 2


def chunk_of(audio_s: float) -> bytes:
    return b"\x00" * int(audio_s * BYTES_PER_S)


def make_metrics(**kw) -> RequestMetrics:
    defaults = dict(session_id=0, scenario="t", voice_mode="cached",
                    text_len_words=5, sample_rate=SR, underrun_tolerance_ms=50)
    defaults.update(kw)
    return RequestMetrics(**defaults)


class FakeClock:
    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: self.t)

    def advance(self, s: float):
        self.t += s


def test_ttfb_and_rtf(monkeypatch):
    clock = FakeClock(monkeypatch)
    m = make_metrics()
    m.start()
    clock.advance(0.2)                 # TTFB 200 мс
    m.on_chunk(chunk_of(0.1))
    clock.advance(0.05)
    m.on_chunk(chunk_of(0.1))
    clock.advance(0.05)
    m.on_chunk(chunk_of(0.1))
    m.finish()
    assert abs(m.ttfb_ms - 200.0) < 1e-6
    assert abs(m.audio_s - 0.3) < 1e-9
    # RTF = время генерации / длительность аудио = 0.3 / 0.3
    assert abs(m.rtf - 1.0) < 1e-6
    assert m.underruns == 0


def test_underrun_detected(monkeypatch):
    clock = FakeClock(monkeypatch)
    m = make_metrics()
    m.start()
    clock.advance(0.1)
    m.on_chunk(chunk_of(0.1))          # 100 мс аудио
    clock.advance(0.16)                # гэп 160 мс > 100 + 50 допуск -> underrun
    m.on_chunk(chunk_of(0.1))
    clock.advance(0.14)                # 140 мс < 150 -> не underrun
    m.on_chunk(chunk_of(0.1))
    assert m.underruns == 1


def test_first_chunk_is_ttfb_not_underrun(monkeypatch):
    clock = FakeClock(monkeypatch)
    m = make_metrics()
    m.start()
    clock.advance(5.0)                 # долгий TTFB — не underrun
    m.on_chunk(chunk_of(0.1))
    assert m.underruns == 0
    assert m.ttfb_ms == 5000.0


def test_quantile_nearest_rank():
    vals = [float(i) for i in range(1, 101)]
    assert quantile(vals, 0.50) == 51.0
    assert quantile(vals, 0.95) == 95.0
    assert quantile([], 0.95) == 0.0
    assert quantile([7.0], 0.99) == 7.0


def _rec(ttfb=100.0, rtf=0.5, underruns=0, error=None, ts=100.0):
    return {"ts": ts, "ttfb_ms": ttfb, "rtf": rtf, "underruns": underruns,
            "error": error, "audio_s": 1.0}


SLA = {"ttfb_p95_ms": 500, "rtf_per_stream": 0.8, "underruns": 0, "error_rate": 0.001}


def test_aggregate_and_sla_ok():
    recs = [_rec() for _ in range(100)]
    s = aggregate(recs)
    ok, violations = check_sla(s, SLA)
    assert ok and not violations
    assert s["requests"] == 100 and s["errors"] == 0


def test_sla_violations():
    recs = [_rec() for _ in range(97)] + [
        _rec(ttfb=900.0),      # тянет p99, не p95 — не нарушение
        _rec(rtf=0.95),        # RTF на потоке > 0.8 -> нарушение
        _rec(error="boom"),    # 1/100 = 1% > 0.1% -> нарушение
    ]
    ok, violations = check_sla(aggregate(recs), SLA)
    assert not ok
    joined = " ".join(violations)
    assert "RTF" in joined and "error rate" in joined
    assert "TTFB" not in joined


def test_single_underrun_breaks_sla():
    recs = [_rec() for _ in range(99)] + [_rec(underruns=1)]
    ok, violations = check_sla(aggregate(recs), SLA)
    assert not ok and any("underrun" in v for v in violations)


def test_aggregate_window_filters_ramp():
    recs = [_rec(ts=10.0, ttfb=9999.0), _rec(ts=100.0), _rec(ts=200.0, ttfb=9999.0)]
    s = aggregate(recs, ts_from=50.0, ts_to=150.0)
    assert s["requests"] == 1 and s["ttfb_p95_ms"] == 100.0
