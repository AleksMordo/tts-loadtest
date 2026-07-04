"""Выгрузка GPU-метрик из Prometheus API за окно каждой полки.

Вызывается после прогона (make report). Для каждого *.meta.json в results/<run_id>/
берёт окно [plateau_start_ts, plateau_end_ts] и снимает:
  - средняя/максимальная утилизация GPU (dcgm),
  - VRAM в начале и в конце окна + за 30 мин (детект утечки),
  - CPU ВМ (node-exporter) — для контроля, что не упёрлись в CPU.

Только stdlib (urllib). Если Prometheus недоступен (локальный smoke) —
пишет gpu: null и не падает.

Запуск: python -m report.collect --run-dir results/<run_id> --prometheus http://<host>:9090
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUERIES = {
    "gpu_util_avg": "avg_over_time(DCGM_FI_DEV_GPU_UTIL[{w}s])",
    "gpu_util_max": "max_over_time(DCGM_FI_DEV_GPU_UTIL[{w}s])",
    "vram_used_mb_max": "max_over_time(DCGM_FI_DEV_FB_USED[{w}s])",
    "vram_used_mb_min": "min_over_time(DCGM_FI_DEV_FB_USED[{w}s])",
    "cpu_busy_avg": (
        "100 - avg(rate(node_cpu_seconds_total{{mode=\"idle\"}}[{w}s])) * 100"
    ),
}
# Рост VRAM за 30 минут (детект утечки): считается по окну всего прогона.
VRAM_LEAK_QUERY = "delta(DCGM_FI_DEV_FB_USED[30m])"


def prom_query(base_url: str, query: str, at_ts: float, timeout: float = 10.0):
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode(
        {"query": query, "time": f"{at_ts:.3f}"}
    )
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "success":
        raise RuntimeError(f"prometheus error: {data}")
    result = data["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def collect_window(base_url: str, start_ts: float, end_ts: float) -> dict:
    window = max(1, int(end_ts - start_ts))
    out: dict = {}
    for name, tmpl in QUERIES.items():
        try:
            out[name] = prom_query(base_url, tmpl.format(w=window), end_ts)
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            out[name] = None
            out.setdefault("errors", []).append(f"{name}: {e}")
    try:
        out["vram_delta_30m_mb"] = prom_query(base_url, VRAM_LEAK_QUERY, end_ts)
    except (urllib.error.URLError, RuntimeError, OSError):
        out["vram_delta_30m_mb"] = None
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Collect GPU metrics from Prometheus")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--prometheus", default=None,
                   help="http://host:9090; пусто = пропустить (локальный smoke)")
    args = p.parse_args()
    run_dir = Path(args.run_dir)
    metas = sorted(run_dir.glob("*.meta.json"))
    if not metas:
        raise SystemExit(f"нет *.meta.json в {run_dir}")
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        start = meta.get("plateau_start_ts")
        end = meta.get("plateau_end_ts")
        if args.prometheus and start and end:
            meta["gpu"] = collect_window(args.prometheus, start, end)
        else:
            meta["gpu"] = None
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{meta_path.name}: gpu={'ok' if meta['gpu'] else 'skipped'}", flush=True)


if __name__ == "__main__":
    main()
