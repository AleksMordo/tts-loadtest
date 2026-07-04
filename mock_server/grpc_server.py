"""gRPC-режим mock TTS: реальный протокол FastCosyVoice (loadgen/proto/cosyvoice.proto).

Реализует ровно тот API, что у боевого сервера runtime/python/grpc:
`CosyVoice.Inference(Request) -> stream Response{tts_audio}` c oneof
sft/zero_shot/cross_lingual/instruct. Позволяет проверить gRPC-путь клиента
(loadgen/client.py::CosyVoiceGRPCTransport) локально, без GPU.
Требует grpcio + стабы (make proto).

Семантика деградации — тот же GPUModel, что и в WS-режиме. clone_extra_ms
применяется к zero_shot-запросам (пересчёт speaker embedding на каждый запрос —
в реальном API его платят и clone, и cached-без-sft). sft-запросы идут без надбавки.

Замечание про подсчёт конкурентности: здесь «активная сессия» — одновременный
вызов Inference (в WS-режиме — соединение), поэтому детерминированный поиск
N_max в e2e гоняется по WS, а gRPC-тест проверяет корректность транспорта.
"""

from __future__ import annotations

import asyncio

from mock_server.server import GPUModel, make_noise_pcm


def build_servicer(model: GPUModel, sample_rate: int = 24000):
    from loadgen.proto import cosyvoice_pb2, cosyvoice_pb2_grpc

    class MockCosyVoiceServicer(cosyvoice_pb2_grpc.CosyVoiceServicer):
        async def Inference(self, request, context):
            model.active += 1
            try:
                if request.HasField("sft_request"):
                    text, clone = request.sft_request.tts_text, False
                elif request.HasField("zero_shot_request"):
                    text, clone = request.zero_shot_request.tts_text, True
                elif request.HasField("cross_lingual_request"):
                    text, clone = request.cross_lingual_request.tts_text, True
                else:
                    text, clone = request.instruct_request.tts_text, False

                cfg = model.cfg
                n_words = max(1, len(text.split()))
                audio_s = n_words * cfg["audio_s_per_word"]
                chunk_s = cfg["chunk_ms"] / 1000.0

                await asyncio.sleep(model.jitter(model.ttfb_s(clone)))

                remaining = audio_s
                while remaining > 1e-9:
                    cur_s = min(chunk_s, remaining)
                    payload = make_noise_pcm(int(sample_rate * cur_s))
                    yield cosyvoice_pb2.Response(tts_audio=payload)
                    remaining -= cur_s
                    if remaining > 1e-9:
                        await asyncio.sleep(model.jitter(cur_s * model.rtf()))
            finally:
                model.active -= 1

    return MockCosyVoiceServicer()


async def serve(model: GPUModel, host: str, port: int, sample_rate: int = 24000):
    """Поднять gRPC-сервер; вернуть объект server (await server.stop(None) для останова)."""
    import grpc

    from loadgen.proto import cosyvoice_pb2_grpc

    server = grpc.aio.server()
    cosyvoice_pb2_grpc.add_CosyVoiceServicer_to_server(build_servicer(model, sample_rate), server)
    server.add_insecure_port(f"{host}:{port}")
    await server.start()
    return server
