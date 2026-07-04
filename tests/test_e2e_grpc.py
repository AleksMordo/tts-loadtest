"""E2E gRPC-транспорта: боевой FastCosyVoice говорит по gRPC
(CosyVoice.Inference -> stream Response), поэтому путь
клиент(CosyVoiceGRPCTransport) -> mock gRPC-сервер проверяется так же, как WS.

Пропускается, если grpcio/стабы не установлены (make proto).
Поиск N_max остаётся на WS-e2e: там конкурентность считается по соединениям
и результат детерминирован.
"""

import asyncio

import pytest
import yaml

pytest.importorskip("grpc")
pytest.importorskip("loadgen.proto.cosyvoice_pb2")

from loadgen.runner import ScenarioRunner  # noqa: E402
from mock_server.grpc_server import serve as grpc_serve  # noqa: E402
from mock_server.server import GPUModel  # noqa: E402

PORT = 8124
SR = 24000  # CosyVoice2 отдаёт 24 kHz; клиент должен считать длительности по этому SR
SLA = {"ttfb_p95_ms": 500, "rtf_per_stream": 0.8, "underruns": 0, "error_rate": 0.001}


def make_defaults(results_dir: str, sft_spk_id: str = "") -> dict:
    return {
        "endpoint": f"127.0.0.1:{PORT}",
        "transport": "grpc",
        "sample_rate": SR,
        "plateau_s": 4,
        "ramp": {"step": 10, "interval_s": 0.3},
        "pause_s": [0.2, 0.5],
        "underrun_tolerance_ms": 50,
        "sft_spk_id": sft_spk_id,
        "texts_dir": "loadgen/texts",
        "ref_audio_dir": "assets/ref_audio",
        "results_dir": results_dir,
    }


async def run_plateau_with_mock(defaults, tmp_path, scenario, lines):
    with open("mock_server/config.yaml", encoding="utf-8") as fh:
        model = GPUModel(yaml.safe_load(fh))
    server = await grpc_serve(model, "127.0.0.1", PORT, sample_rate=SR)
    try:
        runner = ScenarioRunner(defaults, SLA, tmp_path, None)
        return await runner.run_plateau(scenario, lines)
    finally:
        await server.stop(None)


@pytest.mark.e2e
def test_grpc_zero_shot_end_to_end(tmp_path, ref_audio):
    """mixed-голос: и clone, и cached-без-sft идут через zeroshotRequest."""
    meta = asyncio.run(run_plateau_with_mock(
        make_defaults(str(tmp_path)), tmp_path,
        {"name": "e2e_grpc_zs", "mode": "plateau", "texts": "short", "voice": "mixed"}, 5,
    ))
    s = meta["summary"]
    assert s["requests"] > 0, "по gRPC не прошло ни одного запроса"
    assert s["errors"] == 0, f"ошибки gRPC-транспорта: {s}"
    assert s["ttfb_p95_ms"] > 0 and s["underruns"] == 0
    # zero_shot платит clone_extra_ms (пересчёт эмбеддинга) — TTFB заметно выше базы
    assert s["ttfb_p50_ms"] > 350


@pytest.mark.e2e
def test_grpc_sft_path(tmp_path, ref_audio):
    """cached с заданным sft_spk_id уходит в sftRequest — без clone-надбавки."""
    meta = asyncio.run(run_plateau_with_mock(
        make_defaults(str(tmp_path), sft_spk_id="ru_female_1"), tmp_path,
        {"name": "e2e_grpc_sft", "mode": "plateau", "texts": "short", "voice": "cached"}, 5,
    ))
    s = meta["summary"]
    assert s["requests"] > 0 and s["errors"] == 0
    # sft без пересчёта эмбеддинга: TTFB около базовых 120-200 мс
    assert s["ttfb_p95_ms"] < 350
    assert meta["sla_ok"]
