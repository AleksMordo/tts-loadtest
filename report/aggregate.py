"""Агрегация JSONL-метрик полки и проверка SLA.

Используется и раннером (решение в поиске N_max), и build_report.py.
Только stdlib: csv/statistics хватает, pandas не нужен.
"""

from __future__ import annotations

import json
from collections.abc import Iterable


def quantile(values: list[float], q: float) -> float:
    """Перцентиль методом ближайшего ранга (без numpy)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[idx]


def load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def aggregate(records: Iterable[dict], ts_from: float | None = None,
              ts_to: float | None = None) -> dict:
    """Сводка по запросам полки. ts_from/ts_to — окно полки (отсекаем ramp-up)."""
    recs = [
        r for r in records
        if (ts_from is None or r.get("ts", 0) >= ts_from)
        and (ts_to is None or r.get("ts", 0) <= ts_to)
    ]
    total = len(recs)
    errors = [r for r in recs if r.get("error")]
    ok = [r for r in recs if not r.get("error")]
    ttfb = [r["ttfb_ms"] for r in ok if r.get("ttfb_ms") is not None]
    rtf = [r["rtf"] for r in ok if r.get("rtf") is not None]
    return {
        "requests": total,
        "errors": len(errors),
        "error_rate": (len(errors) / total) if total else 0.0,
        "ttfb_p50_ms": quantile(ttfb, 0.50),
        "ttfb_p95_ms": quantile(ttfb, 0.95),
        "ttfb_p99_ms": quantile(ttfb, 0.99),
        "rtf_p50": quantile(rtf, 0.50),
        "rtf_p95": quantile(rtf, 0.95),
        "rtf_max": max(rtf) if rtf else 0.0,
        "underruns": sum(r.get("underruns", 0) for r in ok),
        "audio_s_total": sum(r.get("audio_s", 0.0) for r in ok),
    }


def check_sla(summary: dict, sla: dict) -> tuple[bool, list[str]]:
    """SLA полки: p95 TTFB, RTF на каждом потоке, 0 underruns, error rate."""
    violations = []
    if summary["requests"] == 0:
        return False, ["нет успешных запросов на полке"]
    if summary["ttfb_p95_ms"] > sla["ttfb_p95_ms"]:
        violations.append(
            f"TTFB p95 {summary['ttfb_p95_ms']:.0f}ms > {sla['ttfb_p95_ms']}ms"
        )
    if summary["rtf_max"] > sla["rtf_per_stream"]:
        violations.append(
            f"RTF max {summary['rtf_max']:.3f} > {sla['rtf_per_stream']} (на каждом потоке)"
        )
    if summary["underruns"] > sla["underruns"]:
        violations.append(f"underruns {summary['underruns']} > {sla['underruns']}")
    if summary["error_rate"] > sla["error_rate"]:
        violations.append(
            f"error rate {summary['error_rate']:.4%} > {sla['error_rate']:.2%}"
        )
    return (not violations), violations
