"""Стриминговый клиент TTS с абстракцией транспорта.

Боевой сервис — FastCosyVoice (https://github.com/Brakanier/FastCosyVoice),
его gRPC-сервер runtime/python/grpc: `CosyVoice.Inference(Request) -> stream Response`,
proto лежит в loadgen/proto/cosyvoice.proto (скопирован из репозитория 1:1).
Маппинг наших режимов на API:
  clone  -> zeroshotRequest(tts_text, prompt_text, prompt_audio)  # случайный референс
  cached -> sftRequest(spk_id, tts_text), если задан sft_spk_id;
            иначе тоже zeroshotRequest, но с ФИКСИРОВАННЫМ референсом
            (в этом API нет кеша speaker embedding — фронтенд пересчитывает
            референс на каждый запрос, это надо учитывать при трактовке цифр)
  prompt_audio — raw PCM s16le 16 kHz mono БЕЗ wav-заголовка (см. их client.py)

WS-протокол (только для mock_server / локальной проверки):
  клиент -> сервер: один текстовый фрейм JSON:
      {"text": str, "sample_rate": int,
       "voice_id": str | null,          # режим cached
       "ref_audio_b64": str | null}     # режим clone
  сервер -> клиент: бинарные фреймы PCM s16le, затем текстовый фрейм
      {"event": "end"}  (или {"event": "error", "message": ...})
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


class TTSError(Exception):
    """Ошибка синтеза, о которой сообщил сервер или транспорт."""


@dataclass
class VoiceSpec:
    """Параметры голоса для одного запроса."""

    mode: str                       # "cached" | "clone"
    voice_id: str | None = None     # sft spk_id (если сервис поддерживает sft)
    ref_audio: bytes | None = None  # raw PCM s16le 16kHz mono (без wav-заголовка)
    prompt_text: str = ""           # транскрипт референс-аудио (нужен zero_shot)


class Transport(ABC):
    """Абстракция стримингового транспорта TTS."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def stream(self, text: str, voice: VoiceSpec, sample_rate: int) -> AsyncIterator[bytes]:
        """Отправить text и стримить бинарные PCM-чанки до конца синтеза."""
        ...


class WSTransport(Transport):
    """WebSocket-транспорт. Одно соединение на линию, запросы последовательные."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._ws = None

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self.endpoint, max_size=None, ping_interval=20, ping_timeout=20
        )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def stream(self, text: str, voice: VoiceSpec, sample_rate: int) -> AsyncIterator[bytes]:
        if self._ws is None:
            await self.connect()
        req = {
            "text": text,
            "sample_rate": sample_rate,
            "voice_id": voice.voice_id if voice.mode == "cached" else None,
            "ref_audio_b64": (
                base64.b64encode(voice.ref_audio).decode()
                if voice.mode == "clone" and voice.ref_audio
                else None
            ),
        }
        await self._ws.send(json.dumps(req))
        while True:
            msg = await self._ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                yield bytes(msg)
                continue
            evt = json.loads(msg)
            if evt.get("event") == "end":
                return
            if evt.get("event") == "error":
                raise TTSError(evt.get("message", "server error"))
            # неизвестные текстовые события игнорируем


class CosyVoiceGRPCTransport(Transport):
    """gRPC-транспорт под реальный API FastCosyVoice (runtime/python/grpc).

    Inference — server-streaming: один Request, поток Response.tts_audio
    (raw PCM s16le на sample rate модели: CosyVoice2 = 24000 Hz).
    Конец синтеза = конец стрима. sample_rate запросом не передаётся —
    задаётся моделью на сервере, в конфиге он нужен клиенту для расчёта
    длительности чанков (underrun/RTF).
    """

    def __init__(self, endpoint: str):
        self.endpoint = endpoint  # host:port
        self._channel = None
        self._stub = None

    async def connect(self) -> None:
        try:
            import grpc

            from loadgen.proto import cosyvoice_pb2_grpc  # сгенерировать: make proto
        except ImportError as e:  # pragma: no cover - grpcio опционален
            raise TTSError(
                "grpc-транспорт требует grpcio и сгенерированных стабов (make proto)"
            ) from e
        self._channel = grpc.aio.insecure_channel(self.endpoint)
        self._stub = cosyvoice_pb2_grpc.CosyVoiceStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    async def stream(self, text: str, voice: VoiceSpec, sample_rate: int) -> AsyncIterator[bytes]:
        if self._stub is None:
            await self.connect()
        import grpc

        from loadgen.proto import cosyvoice_pb2

        req = cosyvoice_pb2.Request()
        if voice.mode == "cached" and voice.voice_id:
            req.sft_request.spk_id = voice.voice_id
            req.sft_request.tts_text = text
        else:
            # zero-shot: и clone, и cached-без-sft (фиксированный референс)
            req.zero_shot_request.tts_text = text
            req.zero_shot_request.prompt_text = voice.prompt_text or ""
            req.zero_shot_request.prompt_audio = voice.ref_audio or b""
        try:
            async for resp in self._stub.Inference(req):
                if resp.tts_audio:
                    yield resp.tts_audio
        except grpc.aio.AioRpcError as e:
            raise TTSError(f"{e.code().name}: {e.details()}") from e


def f32_to_s16le(chunk) -> bytes:
    """float32 numpy waveform [-1..1] -> PCM s16le (метрики считают байты s16)."""
    import numpy as np

    return (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class TritonTransport(Transport):
    """Triton Inference Server (backend triton_trtllm форка, decoupled/streaming).

    Модель `cosyvoice2` (BLS-оркестратор): входы reference_wav (float32 16k),
    reference_wav_len, reference_text, target_text; выход — стрим чанков
    float32 `waveform` (sample rate модели, CosyVoice2 = 24 kHz).
    Кеша эмбеддинга нет и здесь: cached = фиксированный референс, clone = случайный.
    endpoint: host:8001 (gRPC-порт Triton).
    """

    def __init__(self, endpoint: str, model_name: str = "cosyvoice2"):
        self.endpoint = endpoint
        self.model_name = model_name
        self._client = None
        self._queue = None
        self._loop = None

    async def connect(self) -> None:
        try:
            import tritonclient.grpc.aio as grpcclient
        except ImportError as e:  # pragma: no cover - опциональная зависимость
            raise TTSError(
                "triton-транспорт требует tritonclient[grpc] и numpy "
                "(pip install -r requirements-triton.txt)"
            ) from e
        # Чистый asyncio-клиент. ВАЖНО: ранние «зависания» aio-клиента были
        # вызваны docker-proxy (ответы decoupled-стрима не доходили до удалённых
        # клиентов); с network_mode: host aio-путь работает. Sync-клиент с
        # callback-потоками под нагрузкой дедлочился на stop_stream.
        self._client = grpcclient.InferenceServerClient(url=self.endpoint)

    async def close(self) -> None:
        if self._client is not None:
            client = self._client
            self._client = None
            await client.close()

    async def stream(self, text: str, voice: VoiceSpec, sample_rate: int) -> AsyncIterator[bytes]:
        if self._client is None:
            await self.connect()
        import numpy as np
        import tritonclient.grpc.aio as grpcclient
        from tritonclient.utils import np_to_triton_dtype

        if not voice.ref_audio:
            raise TTSError("triton-транспорт требует референс-аудио (zero-shot)")
        # raw PCM s16le 16k -> float32 [-1..1], как в их client_grpc.py
        waveform = (
            np.frombuffer(voice.ref_audio, dtype=np.int16).astype(np.float32) / 32768.0
        ).reshape(1, -1)
        lengths = np.array([[waveform.shape[1]]], dtype=np.int32)

        inputs = [
            grpcclient.InferInput("reference_wav", waveform.shape,
                                  np_to_triton_dtype(waveform.dtype)),
            grpcclient.InferInput("reference_wav_len", lengths.shape,
                                  np_to_triton_dtype(lengths.dtype)),
            grpcclient.InferInput("reference_text", [1, 1], "BYTES"),
            grpcclient.InferInput("target_text", [1, 1], "BYTES"),
        ]
        inputs[0].set_data_from_numpy(waveform)
        inputs[1].set_data_from_numpy(lengths)
        inputs[2].set_data_from_numpy(
            np.array([[voice.prompt_text or ""]], dtype=object))
        inputs[3].set_data_from_numpy(np.array([[text]], dtype=object))
        outputs = [grpcclient.InferRequestedOutput("waveform")]

        async def request_iter():
            yield {
                "model_name": self.model_name,
                "inputs": inputs,
                "outputs": outputs,
                "enable_empty_final_response": True,
            }

        try:
            async for result, error in self._client.stream_infer(
                inputs_iterator=request_iter()
            ):
                if error is not None:
                    raise TTSError(str(error))
                resp = result.get_response()
                final = (
                    "triton_final_response" in resp.parameters
                    and resp.parameters["triton_final_response"].bool_param
                )
                chunk = result.as_numpy("waveform")
                if chunk is not None and chunk.size > 0:
                    yield f32_to_s16le(chunk.reshape(-1))
                if final:
                    return
        except TTSError:
            raise
        except Exception as e:  # grpc/triton ошибки транспорта
            raise TTSError(f"{type(e).__name__}: {e}") from e


def make_transport(kind: str, endpoint: str, triton_model: str = "cosyvoice2") -> Transport:
    if kind == "ws":
        return WSTransport(endpoint)
    if kind == "grpc":
        return CosyVoiceGRPCTransport(endpoint)
    if kind == "triton":
        return TritonTransport(endpoint, model_name=triton_model)
    raise ValueError(f"неизвестный транспорт: {kind!r} (ожидается ws | grpc | triton)")
