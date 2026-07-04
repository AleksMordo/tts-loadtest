"""Итоговый отчёт: report.md + raw CSV из results/<run_id>/.

Собирает:
  - таблицу по полкам (TTFB p50/p95/p99, p95 RTF, underruns, errors, GPU util, VRAM);
  - N_max по каждому режиму + ASCII-график деградации TTFB от числа линий;
  - таблицу стоимости (cost_model + pricing.yaml);
  - раздел «Ограничения измерений».

Запуск: python -m report.build_report --run-dir results/<run_id> \\
            --pricing config/pricing.yaml [--targets 10 30 100]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from report import cost_model

LIMITATIONS = """\
## Ограничения измерений

- Сеть до телефонии не учтена: генератор нагрузки стоит в той же подсети, что и TTS.
  В проде добавится RTT до SBC/оператора и джиттер-буфер.
- Измерялась только TTS-часть. Задержки ASR и LLM (и их стоимость) в TTFB диалога
  не входят.
- Стоимость исходящего трафика, снапшотов, образов и NAT не включена в модель —
  только ВМ + диск + публичный IP из pricing.yaml.
- duty_cycle — оценка (см. pricing.yaml); реальная доля речи бота зависит от сценария
  диалога.
- Прерываемые ВМ (preemptible) дешевле, но их могут отозвать: полки 100 линий и
  бенчмарки для прайсинга гнать на обычных ВМ.
- Mock-режим проверяет пайплайн, а не производительность: цифры из локального
  прогона к GPU отношения не имеют.
"""


def fmt(v, digits=0, dash="—"):
    if v is None:
        return dash
    return f"{v:,.{digits}f}".replace(",", " ")


def load_run(run_dir: Path) -> tuple[list[dict], list[dict]]:
    """-> (полки: meta с summary, nmax-результаты)."""
    plateaus, nmaxes = [], []
    for p in sorted(run_dir.glob("*.meta.json")):
        plateaus.append(json.loads(p.read_text(encoding="utf-8")))
    for p in sorted(run_dir.glob("*.nmax.json")):
        nmaxes.append(json.loads(p.read_text(encoding="utf-8")))
    return plateaus, nmaxes


def plateau_rows(plateaus: list[dict]) -> list[dict]:
    rows = []
    for m in plateaus:
        s = m.get("summary") or m.get("summary_post")
        if not s:
            continue
        gpu = m.get("gpu") or {}
        rows.append({
            "scenario": m["scenario"],
            "lines": m.get("spiked_lines", m.get("lines")),
            "voice": m.get("voice", ""),
            "texts": m.get("texts", ""),
            "requests": s["requests"],
            "ttfb_p50_ms": s["ttfb_p50_ms"],
            "ttfb_p95_ms": s["ttfb_p95_ms"],
            "ttfb_p99_ms": s["ttfb_p99_ms"],
            "rtf_p95": s["rtf_p95"],
            "rtf_max": s["rtf_max"],
            "underruns": s["underruns"],
            "errors": s["errors"],
            "error_rate": s["error_rate"],
            "gpu_util_avg": gpu.get("gpu_util_avg"),
            "gpu_util_max": gpu.get("gpu_util_max"),
            "vram_used_mb_max": gpu.get("vram_used_mb_max"),
            "vram_delta_30m_mb": gpu.get("vram_delta_30m_mb"),
            "sla_ok": m.get("sla_ok"),
        })
    return rows


def ascii_degradation(nmax: dict) -> str:
    """ASCII-график: TTFB p95 от числа линий по истории поиска N_max."""
    hist = nmax.get("history", [])
    if not hist:
        return "(нет данных)"
    max_ttfb = max(h["summary"]["ttfb_p95_ms"] for h in hist) or 1.0
    width = 46
    lines = []
    for h in hist:
        v = h["summary"]["ttfb_p95_ms"]
        bar = "#" * max(1, round(v / max_ttfb * width))
        mark = "ok " if h["sla_ok"] else "SLA"
        lines.append(f"{h['lines']:>4} линий | {bar:<{width}} {v:7.0f} ms  [{mark}]")
    return "\n".join(lines)


def build(run_dir: Path, pricing_path: str | None, targets: list[int],
          stand: dict | None) -> str:
    plateaus, nmaxes = load_run(run_dir)
    rows = plateau_rows(plateaus)

    md = ["# Отчёт нагрузочного тестирования TTS", ""]
    md += [f"Прогон: `{run_dir.name}`", ""]

    md += ["## Конфигурация стенда", ""]
    if stand:
        cloud = stand.get("cloud", {})
        tts = stand.get("tts", {})
        md += [
            f"- Платформа GPU: `{cloud.get('gpu_platform_id')}` × {cloud.get('gpu_count')} GPU",
            f"- Зона: {cloud.get('zone')}, preemptible: {cloud.get('preemptible')}",
            f"- Образ TTS: `{tts.get('registry')}/{tts.get('image')}:{tts.get('tag')}`",
            f"- Транспорт: {tts.get('transport')}, sample rate: {tts.get('sample_rate')} Hz",
            "",
        ]
    else:
        md += ["- (локальный smoke-прогон против mock-сервера, GPU не использовался)", ""]

    md += ["## Полки", ""]
    md += ["| Сценарий | Линии | Голос | TTFB p50/p95/p99, мс | RTF p95 | RTF max "
           "| Underruns | Errors | GPU util avg/max, % | VRAM max, MB | SLA |"]
    md += ["|---|---:|---|---|---:|---:|---:|---:|---|---:|---|"]
    for r in rows:
        md += [
            f"| {r['scenario']} | {r['lines']} | {r['voice']} "
            f"| {fmt(r['ttfb_p50_ms'])}/{fmt(r['ttfb_p95_ms'])}/{fmt(r['ttfb_p99_ms'])} "
            f"| {r['rtf_p95']:.2f} | {r['rtf_max']:.2f} | {r['underruns']} "
            f"| {r['errors']} ({r['error_rate']:.2%}) "
            f"| {fmt(r['gpu_util_avg'])}/{fmt(r['gpu_util_max'])} "
            f"| {fmt(r['vram_used_mb_max'])} "
            f"| {'✅' if r['sla_ok'] else '❌' if r['sla_ok'] is not None else '—'} |"
        ]
    md += [""]

    leak_rows = [r for r in rows if r["vram_delta_30m_mb"] is not None]
    if leak_rows:
        md += ["### Утечки VRAM (delta за 30 мин)", ""]
        for r in leak_rows:
            md += [f"- {r['scenario']}: {fmt(r['vram_delta_30m_mb'])} MB"]
        md += [""]

    n_max_by_mode: dict[str, int | None] = {}
    if nmaxes:
        md += ["## Ёмкость (N_max)", ""]
        for nm in nmaxes:
            key = f"{nm.get('texts')}/{nm.get('voice')}"
            n_max_by_mode[key] = nm.get("n_max")
            md += [f"### {nm['scenario']} ({key}): **N_max = {nm.get('n_max')}**", ""]
            md += ["Деградация TTFB p95 по полкам поиска:", "", "```",
                   ascii_degradation(nm), "```", ""]

    if pricing_path:
        pricing = cost_model.Pricing.load(pricing_path)
        md += ["## Стоимость", ""]
        if pricing.c_hour <= 0:
            md += ["⚠️ `config/pricing.yaml` не заполнен (C_hour = 0) — таблица "
                   "стоимости пропущена. Заполните цены из прайса YC и повторите "
                   "`make report`.", ""]
        else:
            for key, n_max in n_max_by_mode.items():
                if not n_max:
                    md += [f"- {key}: N_max не найден, расчёт пропущен", ""]
                    continue
                md += [
                    f"### Режим {key} (N_max = {n_max}, "
                    f"C_hour = {fmt(pricing.c_hour, 2)} {pricing.currency}/час, "
                    f"duty_cycle = {pricing.duty_cycle})", "",
                    f"- Линия/час: {fmt(cost_model.line_cost_per_hour(pricing, n_max), 2)} "
                    f"{pricing.currency}",
                    f"- Секунда синтеза (грязная, 100% занятость): "
                    f"{cost_model.synth_second_dirty(pricing, n_max):.5f} {pricing.currency}",
                    f"- Секунда синтеза (реальная, duty_cycle={pricing.duty_cycle}): "
                    f"{cost_model.synth_second_real(pricing, n_max):.5f} {pricing.currency}",
                    "",
                    f"| Линий | Инстансов (запас ×{pricing.peak_headroom}) | "
                    f"{pricing.currency}/час | {pricing.currency}/мес "
                    f"({pricing.hours_per_month:.0f} ч) | {pricing.currency}/сек синтеза "
                    f"(реальная) | {pricing.currency}/мин разговора |",
                    "|---:|---:|---:|---:|---:|---:|",
                ]
                for row in cost_model.cost_table(pricing, n_max, targets):
                    md += [
                        f"| {row['lines']} | {row['instances']} "
                        f"| {fmt(row['cost_per_hour'], 2)} "
                        f"| {fmt(row['cost_per_month'], 0)} "
                        f"| {row['synth_second_real']:.5f} "
                        f"| {row['talk_minute']:.3f} |"
                    ]
                md += [""]

    md += [LIMITATIONS]
    return "\n".join(md)


def write_csv(run_dir: Path, rows: list[dict]) -> Path:
    out = run_dir / "summary.csv"
    if rows:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Build final report")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--pricing", default="config/pricing.yaml")
    p.add_argument("--stand", default="config/stand.yaml")
    p.add_argument("--targets", nargs="*", type=int, default=[10, 30, 100])
    p.add_argument("--local", action="store_true",
                   help="локальный smoke: не включать конфигурацию облака")
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    stand = None
    if not args.local:
        import yaml

        with open(args.stand, encoding="utf-8") as fh:
            stand = yaml.safe_load(fh)

    md = build(run_dir, args.pricing, args.targets, stand)
    report_path = run_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")
    plateaus, _ = load_run(run_dir)
    csv_path = write_csv(run_dir, plateau_rows(plateaus))
    print(f"Отчёт: {report_path}\nCSV:   {csv_path}", flush=True)


if __name__ == "__main__":
    main()
