"""Mock TTS-сервер для локальной проверки всего пайплайна без GPU.

Реализует тот же WS-протокол, что и клиент (loadgen/client.py): JSON-запрос,
бинарные PCM-чанки (шум малой амплитуды), текстовый маркер конца.

Эмуляция GPU: TTFB и RTF растут с числом одновременных сессий; после
max_sessions_before_degradation включаются жёсткие штрафы (сатурация) —
начинаются underruns и нарушение RTF/TTFB SLA. Параметры — mock_server/config.yaml.

Запуск: python -m mock_server.server [--config mock_server/config.yaml] [--port 8022]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import struct

import yaml

_rng = random.Random(7)


class GPUModel:
    """Модель деградации: считает эффективные TTFB/RTF от числа активных сессий."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.active = 0

    def _overload(self) -> int:
        return max(0, self.active - self.cfg["max_sessions_before_degradation"])

    def ttfb_s(self, clone: bool) -> float:
        c = self.cfg
        ms = c["ttfb_ms"]["base"] + c["ttfb_ms"]["per_active_session"] * self.active
        ms += c["overload_ttfb_ms_per_session"] * self._overload()
        if clone:
            ms += c["clone_extra_ms"]
        return ms / 1000.0

    def rtf(self) -> float:
        c = self.cfg
        r = c["rtf"]["base"] + c["rtf"]["per_active_session"] * self.active
        r += c["overload_rtf_per_session"] * self._overload()
        return r

    def jitter(self, value: float) -> float:
        j = self.cfg["jitter"]
        return value * _rng.uniform(1.0 - j, 1.0 + j)


def make_noise_pcm(n_samples: int) -> bytes:
    """Тихий шум s16le — чтобы чанки были «настоящими» PCM-данными."""
    return struct.pack(
        f"<{n_samples}h", *(_rng.randint(-300, 300) for _ in range(n_samples))
    )


async def handle(ws, model: GPUModel) -> None:
    from websockets.exceptions import ConnectionClosed

    # Активная сессия = открытое соединение (одна линия держит одно соединение).
    # Так деградация зависит от числа линий, как у реального выделенного сервинга.
    model.active += 1
    try:
        async for msg in ws:
            try:
                req = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                await ws.send(json.dumps({"event": "error", "message": "bad request"}))
                continue
            await synthesize(ws, req, model)
    except ConnectionClosed:
        pass  # клиент закрыл соединение посреди стрима (конец полки) — норма
    finally:
        model.active -= 1


async def synthesize(ws, req: dict, model: GPUModel) -> None:
    cfg = model.cfg
    sample_rate = int(req.get("sample_rate", 8000))
    clone = bool(req.get("ref_audio_b64"))
    n_words = max(1, len(str(req.get("text", "")).split()))
    audio_s = n_words * cfg["audio_s_per_word"]
    chunk_s = cfg["chunk_ms"] / 1000.0
    chunk_bytes = make_noise_pcm(int(sample_rate * chunk_s))

    # TTFB: «прогрев» запроса (для clone — плюс обработка референс-аудио)
    await asyncio.sleep(model.jitter(model.ttfb_s(clone)))

    remaining = audio_s
    while remaining > 1e-9:
        cur_s = min(chunk_s, remaining)
        if cur_s >= chunk_s:
            payload = chunk_bytes
        else:
            payload = make_noise_pcm(int(sample_rate * cur_s))
        await ws.send(payload)
        remaining -= cur_s
        if remaining > 1e-9:
            # генерация следующего чанка: rtf пересчитывается на каждом чанке —
            # конкурентность могла измениться по ходу стрима
            await asyncio.sleep(model.jitter(cur_s * model.rtf()))
    await ws.send(json.dumps({"event": "end"}))


async def amain(cfg: dict) -> None:
    import websockets

    model = GPUModel(cfg)

    async def _handler(ws):
        await handle(ws, model)

    host, port = cfg["listen"]["host"], cfg["listen"]["port"]
    grpc_port = cfg["listen"].get("grpc_port")
    grpc_server = None
    if grpc_port:
        try:
            from mock_server.grpc_server import serve as grpc_serve

            grpc_server = await grpc_serve(model, host, grpc_port)
            print(f"mock TTS grpc: {host}:{grpc_port}", flush=True)
        except ImportError:
            print("grpc-режим mock пропущен: нет grpcio/стабов (make proto)", flush=True)
    async with websockets.serve(_handler, host, port, max_size=None):
        print(f"mock TTS: ws://{host}:{port}/tts "
              f"(деградация после {cfg['max_sessions_before_degradation']} сессий)",
              flush=True)
        try:
            await asyncio.Future()
        finally:
            if grpc_server is not None:
                await grpc_server.stop(None)


def main() -> None:
    import logging

    # TCP-пробы готовности порта (smoke_local.sh) не проходят WS-handshake —
    # не засоряем вывод трейсбеками
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser(description="Mock TTS server")
    p.add_argument("--config", default="mock_server/config.yaml")
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if args.port:
        cfg["listen"]["port"] = args.port
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
