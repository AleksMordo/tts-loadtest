"""Модель одной «линии» — закрытый цикл, имитирующий диалог бота с абонентом.

loop:
    text = реплика из корпуса (по профилю сценария)
    стримим синтез, меряем TTFB / inter-arrival / underruns / RTF
    sleep(random pause)   # «абонент говорит, ASR+LLM думают»
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from loadgen.client import Transport, TTSError, VoiceSpec
from loadgen.metrics import JSONLWriter, RequestMetrics


class TextCorpus:
    """Корпуса реплик: short (5–15 слов), long (100+ слов), mixed (80/20)."""

    def __init__(self, short: list[str], long: list[str], profile: str, rng: random.Random):
        self.short = short
        self.long = long
        self.profile = profile
        self.rng = rng

    def pick(self) -> str:
        if self.profile == "short":
            return self.rng.choice(self.short)
        if self.profile == "long":
            return self.rng.choice(self.long)
        # mixed: 80% short / 20% long
        pool = self.short if self.rng.random() < 0.8 else self.long
        return self.rng.choice(pool)


@dataclass
class RefAudio:
    """Референс для zero-shot: raw PCM s16le 16kHz + его транскрипт."""

    pcm: bytes
    prompt_text: str


class VoicePicker:
    """cached | clone | mixed (90% cached / 10% clone).

    В API CosyVoice нет кеша speaker embedding, поэтому:
      clone  — случайный референс на каждый запрос (худший случай);
      cached — sftRequest(spk_id), если sft_spk_id задан (задеплоена SFT-модель),
               иначе zero-shot с одним и тем же референсом (лучший случай
               для этого API; embedding всё равно пересчитывается сервером).
    """

    def __init__(self, profile: str, sft_spk_id: str, ref_audios: list[RefAudio],
                 rng: random.Random):
        self.profile = profile
        self.sft_spk_id = sft_spk_id
        self.ref_audios = ref_audios
        self.rng = rng

    def pick(self) -> VoiceSpec:
        mode = self.profile
        if mode == "mixed":
            mode = "cached" if self.rng.random() < 0.9 else "clone"
        if mode == "clone":
            ref = self.rng.choice(self.ref_audios)
            return VoiceSpec(mode="clone", ref_audio=ref.pcm, prompt_text=ref.prompt_text)
        if self.sft_spk_id:
            return VoiceSpec(mode="cached", voice_id=self.sft_spk_id)
        ref = self.ref_audios[0]
        return VoiceSpec(mode="cached", ref_audio=ref.pcm, prompt_text=ref.prompt_text)


class Session:
    """Одна одновременная стриминговая сессия синтеза (линия)."""

    def __init__(
        self,
        session_id: int,
        scenario: str,
        transport: Transport,
        corpus: TextCorpus,
        voices: VoicePicker,
        writer: JSONLWriter,
        sample_rate: int,
        pause_s: tuple[float, float],
        underrun_tolerance_ms: float,
        active_counter,
        rng: random.Random,
    ):
        self.session_id = session_id
        self.scenario = scenario
        self.transport = transport
        self.corpus = corpus
        self.voices = voices
        self.writer = writer
        self.sample_rate = sample_rate
        self.pause_s = pause_s
        self.underrun_tolerance_ms = underrun_tolerance_ms
        self.active_counter = active_counter  # callable -> текущее число активных линий
        self.rng = rng
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            await self.transport.connect()
        except Exception as e:
            self.writer.write(
                {
                    "scenario": self.scenario,
                    "session_id": self.session_id,
                    "error": f"connect: {e}",
                }
            )
            return
        try:
            while not self._stop.is_set():
                await self._one_utterance()
                pause = self.rng.uniform(*self.pause_s)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=pause)
                except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - на py3.10 это разные классы
                    pass
        finally:
            await self.transport.close()

    async def _one_utterance(self) -> None:
        text = self.corpus.pick()
        voice = self.voices.pick()
        m = RequestMetrics(
            session_id=self.session_id,
            scenario=self.scenario,
            voice_mode=voice.mode,
            text_len_words=len(text.split()),
            sample_rate=self.sample_rate,
            underrun_tolerance_ms=self.underrun_tolerance_ms,
        )
        m.active_lines = self.active_counter()
        m.start()
        try:
            async for chunk in self.transport.stream(text, voice, self.sample_rate):
                m.on_chunk(chunk)
        except (TTSError, OSError, asyncio.IncompleteReadError) as e:
            m.error = str(e) or type(e).__name__
        except Exception as e:  # соединение умерло — фиксируем и завершаем линию
            m.error = f"{type(e).__name__}: {e}"
            m.finish()
            self.writer.write(m.to_record())
            self._stop.set()
            return
        m.finish()
        if m.chunks == 0 and not m.error:
            # стрим завершился без единого аудио-чанка (LLM выдал 0 токенов) —
            # это отказ синтеза, иначе такие записи ложно проходят SLA
            m.error = "empty_stream: 0 чанков"
        self.writer.write(m.to_record())
