"""E2E: весь пайплайн (раннер -> метрики -> детекция underrun -> SLA -> отчёт ->
cost_model) против mock TTS-сервера, без GPU.

Mock сконфигурирован так (mock_server/config.yaml), что деградация начинается
после 20 одновременных линий — раннер обязан сам найти N_max ≈ 20±2,
и это должно попасть в отчёт.
"""

import asyncio
import json

import pytest
import yaml

from loadgen.runner import ScenarioRunner
from mock_server.server import GPUModel, handle
from report.build_report import build

PORT = 8123
SLA = {"ttfb_p95_ms": 500, "rtf_per_stream": 0.8, "underruns": 0, "error_rate": 0.001}


def make_defaults(results_dir: str, plateau_s: float) -> dict:
    return {
        "endpoint": f"ws://127.0.0.1:{PORT}/tts",
        "transport": "ws",
        "sample_rate": 8000,
        "plateau_s": plateau_s,
        "ramp": {"step": 10, "interval_s": 0.3},
        "pause_s": [0.2, 0.5],
        "underrun_tolerance_ms": 50,
        "texts_dir": "loadgen/texts",
        "ref_audio_dir": "assets/ref_audio",
        "results_dir": results_dir,
    }


async def with_mock_server(coro):
    import websockets

    with open("mock_server/config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    model = GPUModel(cfg)

    async def _handler(ws):
        await handle(ws, model)

    async with websockets.serve(_handler, "127.0.0.1", PORT, max_size=None):
        return await coro


@pytest.mark.e2e
def test_runner_finds_planted_nmax(tmp_path, ref_audio):
    """Раннер должен найти заложенный в mock N_max ≈ 20±2, отчёт — показать его."""
    defaults = make_defaults(str(tmp_path), plateau_s=8)
    runner = ScenarioRunner(defaults, SLA, tmp_path, None)
    scenario = {"name": "e2e_nmax", "mode": "nmax", "texts": "mixed",
                "voice": "cached", "start": 10, "step": 10, "limit": 60}

    result = asyncio.run(with_mock_server(runner.run_nmax(scenario)))

    assert result["n_max"] is not None, "N_max не найден вовсе"
    assert 18 <= result["n_max"] <= 22, f"ожидали N_max ~ 20±2, получили {result['n_max']}"

    # Полка 30 (за порогом деградации) обязана нарушить SLA, причём заметно:
    over = [h for h in result["history"] if h["lines"] > 22]
    assert over and all(not h["sla_ok"] for h in over)
    # ... и на перегрузе должны детектироваться underruns (rtf mock > 1)
    assert any(h["summary"]["underruns"] > 0 for h in over)

    # Отчёт: N_max и полки попадают в markdown, cost-модель на заполненном прайсе
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(
        "currency: RUB\nvat_included: true\n"
        "components: {gpu_vm_per_hour: 900.0, disk_per_hour: 10.0, public_ip_per_hour: 5.0}\n"
        "duty_cycle: 0.4\npeak_headroom: 1.3\nhours_per_month: 720\n",
        encoding="utf-8",
    )
    md = build(tmp_path, str(pricing), [10, 30, 100], stand=None)
    assert f"N_max = {result['n_max']}" in md
    assert "Стоимость" in md and "мин разговора" in md
    assert "Ограничения измерений" in md


@pytest.mark.e2e
def test_clone_ttfb_penalty_visible(tmp_path, ref_audio):
    """Режим clone должен показывать надбавку TTFB ~ clone_extra_ms относительно cached."""
    defaults = make_defaults(str(tmp_path), plateau_s=4)

    async def scenario():
        r1 = ScenarioRunner(defaults, SLA, tmp_path, None)
        cached = await r1.run_plateau(
            {"name": "e2e_cached", "mode": "plateau", "texts": "short", "voice": "cached"}, 5
        )
        r2 = ScenarioRunner(defaults, SLA, tmp_path, None)
        clone = await r2.run_plateau(
            {"name": "e2e_clone", "mode": "plateau", "texts": "short", "voice": "clone"}, 5
        )
        return cached, clone

    cached, clone = asyncio.run(with_mock_server(scenario()))
    delta = clone["summary"]["ttfb_p50_ms"] - cached["summary"]["ttfb_p50_ms"]
    assert 250 <= delta <= 600, f"надбавка clone {delta:.0f}ms, ожидали ~400ms"


@pytest.mark.e2e
def test_spike_mode_produces_pre_post(tmp_path, ref_audio):
    """Spike-режим: метрики до/после удвоения, после — TTFB не лучше, чем до."""
    defaults = make_defaults(str(tmp_path), plateau_s=8)
    runner = ScenarioRunner(defaults, SLA, tmp_path, None)
    meta = asyncio.run(with_mock_server(runner.run_spike(
        {"name": "e2e_spike", "mode": "spike", "lines": 10, "spike_factor": 2,
         "texts": "short", "voice": "cached"}
    )))
    pre, post = meta["summary_pre"], meta["summary_post"]
    assert pre["requests"] > 0 and post["requests"] > 0
    assert post["ttfb_p95_ms"] > pre["ttfb_p95_ms"]  # 20 линий медленнее 10

    saved = json.loads((tmp_path / "e2e_spike.meta.json").read_text(encoding="utf-8"))
    assert saved["spiked_lines"] == 20 and "spike_ts" in saved
