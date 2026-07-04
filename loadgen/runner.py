"""Раннер: исполняет матрицу сценариев из config/scenarios.yaml.

Режимы:
  plateau — ramp-up до N линий, полка plateau_s, останов;
  nmax    — полки от start с шагом step до первого нарушения SLA,
            N_max = последняя полка без нарушений;
  spike   — полка N, в середине мгновенное удвоение до N*spike_factor.

Запуск:
  python -m loadgen.runner --scenarios config/scenarios.yaml --profile smoke \\
      --set smoke_scenarios --endpoint ws://127.0.0.1:8022/tts --run-id local
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import wave
from pathlib import Path

import yaml

from loadgen.client import make_transport
from loadgen.metrics import JSONLWriter
from loadgen.session import RefAudio, Session, TextCorpus, VoicePicker
from report.aggregate import aggregate, check_sla, load_jsonl

STOP_GRACE_S = 5.0  # сколько ждём завершения текущих реплик после конца полки


def load_config(path: str, profile: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    merged = dict(cfg["defaults"])
    overrides = (cfg.get("profiles") or {}).get(profile)
    if overrides is None:
        raise SystemExit(f"профиль {profile!r} не найден в {path}")
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    cfg["defaults"] = merged
    return cfg


def load_texts(texts_dir: str) -> tuple[list[str], list[str]]:
    base = Path(texts_dir)
    short = [t for t in (base / "short.txt").read_text(encoding="utf-8").splitlines() if t.strip()]
    long = [t for t in (base / "long.txt").read_text(encoding="utf-8").splitlines() if t.strip()]
    if not short or not long:
        raise SystemExit(f"пустые корпуса текстов в {texts_dir}")
    return short, long


DEFAULT_PROMPT_TEXT = "Надеюсь, у тебя всё будет хорошо."


def load_ref_audios(ref_dir: str) -> list[RefAudio]:
    """wav (16 kHz mono s16) -> raw PCM без заголовка + транскрипт из сайдкара .txt.

    API CosyVoice ждёт prompt_audio как голые int16-сэмплы на 16 kHz и транскрипт
    референса (prompt_text). Транскрипт кладётся рядом: speaker_01.wav + speaker_01.txt.
    """
    wavs = sorted(Path(ref_dir).glob("*.wav"))
    if not wavs:
        raise SystemExit(
            f"нет референс-аудио в {ref_dir} (сгенерировать: make ref-audio)"
        )
    refs = []
    for p in wavs:
        with wave.open(str(p), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
                raise SystemExit(f"{p}: референс должен быть 16-bit mono WAV")
            if wf.getframerate() != 16000:
                raise SystemExit(
                    f"{p}: sample rate {wf.getframerate()}, а API CosyVoice ждёт 16000 — "
                    "пересемплируйте референс"
                )
            pcm = wf.readframes(wf.getnframes())
        txt = p.with_suffix(".txt")
        prompt = txt.read_text(encoding="utf-8").strip() if txt.exists() else DEFAULT_PROMPT_TEXT
        refs.append(RefAudio(pcm=pcm, prompt_text=prompt))
    return refs


class ScenarioRunner:
    def __init__(self, defaults: dict, sla: dict, run_dir: Path, endpoint: str | None):
        self.d = defaults
        self.sla = sla
        self.run_dir = run_dir
        self.endpoint = endpoint or defaults["endpoint"]
        self.rng = random.Random(42)
        self.short, self.long = load_texts(defaults["texts_dir"])
        self.ref_audios = load_ref_audios(defaults["ref_audio_dir"])
        self.sessions: list[Session] = []
        self.tasks: list[asyncio.Task] = []
        self._active = 0

    # -- жизненный цикл линий ------------------------------------------------

    def _spawn(self, sc: dict, writer: JSONLWriter, sid: int) -> None:
        transport = make_transport(self.d["transport"], self.endpoint,
                                   triton_model=self.d.get("triton_model", "cosyvoice2"))
        corpus = TextCorpus(self.short, self.long, sc["texts"], self.rng)
        voices = VoicePicker(sc["voice"], self.d.get("sft_spk_id", ""),
                             self.ref_audios, self.rng)
        s = Session(
            session_id=sid,
            scenario=sc["name"],
            transport=transport,
            corpus=corpus,
            voices=voices,
            writer=writer,
            sample_rate=self.d["sample_rate"],
            pause_s=tuple(self.d["pause_s"]),
            underrun_tolerance_ms=self.d["underrun_tolerance_ms"],
            active_counter=lambda: self._active,
            rng=random.Random(1000 + sid),
        )
        self.sessions.append(s)
        self.tasks.append(asyncio.create_task(s.run()))
        self._active += 1

    async def _ramp_to(self, sc: dict, writer: JSONLWriter, target: int) -> None:
        step = int(self.d["ramp"]["step"])
        interval = float(self.d["ramp"]["interval_s"])
        while self._active < target:
            batch = min(step, target - self._active)
            for _ in range(batch):
                self._spawn(sc, writer, sid=self._active)
            if self._active < target:
                await asyncio.sleep(interval)

    async def _stop_all(self) -> None:
        for s in self.sessions:
            s.stop()
        done, pending = await asyncio.wait(self.tasks, timeout=STOP_GRACE_S)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.sessions.clear()
        self.tasks.clear()
        self._active = 0

    # -- режимы ---------------------------------------------------------------

    async def run_plateau(self, sc: dict, lines: int, tag: str | None = None) -> dict:
        name = tag or sc["name"]
        jsonl = self.run_dir / f"{name}.jsonl"
        writer = JSONLWriter(str(jsonl))
        meta: dict = {"scenario": name, "mode": sc["mode"], "lines": lines,
                      "voice": sc["voice"], "texts": sc["texts"]}
        try:
            await self._ramp_to(sc, writer, lines)
            meta["plateau_start_ts"] = time.time()
            await asyncio.sleep(float(self.d["plateau_s"]))
            meta["plateau_end_ts"] = time.time()
            await self._stop_all()
        finally:
            writer.close()
        summary = aggregate(
            load_jsonl(str(jsonl)), meta["plateau_start_ts"], meta["plateau_end_ts"]
        )
        ok, violations = check_sla(summary, self.sla)
        meta.update({"summary": summary, "sla_ok": ok, "sla_violations": violations})
        (self.run_dir / f"{name}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        status = "OK" if ok else f"SLA FAIL: {'; '.join(violations)}"
        print(f"[{name}] lines={lines} req={summary['requests']} "
              f"ttfb_p95={summary['ttfb_p95_ms']:.0f}ms rtf_max={summary['rtf_max']:.2f} "
              f"underruns={summary['underruns']} -> {status}", flush=True)
        return meta

    async def run_nmax(self, sc: dict) -> dict:
        """Полки от start с шагом step до нарушения SLA."""
        start, step, limit = int(sc["start"]), int(sc["step"]), int(sc.get("limit", 200))
        n_max, history = None, []
        n = start
        while n <= limit:
            meta = await self.run_plateau(sc, n, tag=f"{sc['name']}_n{n}")
            history.append({"lines": n, "sla_ok": meta["sla_ok"],
                            "violations": meta["sla_violations"],
                            "summary": meta["summary"]})
            if not meta["sla_ok"]:
                break
            n_max = n
            n += step
        result = {"scenario": sc["name"], "mode": "nmax", "voice": sc["voice"],
                  "texts": sc["texts"], "n_max": n_max, "history": history}
        (self.run_dir / f"{sc['name']}.nmax.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{sc['name']}] N_max = {n_max}", flush=True)
        return result

    async def run_spike(self, sc: dict) -> dict:
        """Полка N линий, в середине мгновенное удвоение (spike_factor)."""
        lines = int(sc["lines"])
        spiked = int(lines * float(sc.get("spike_factor", 2)))
        name = sc["name"]
        jsonl = self.run_dir / f"{name}.jsonl"
        writer = JSONLWriter(str(jsonl))
        half = float(self.d["plateau_s"]) / 2
        meta: dict = {"scenario": name, "mode": "spike", "lines": lines,
                      "spiked_lines": spiked, "voice": sc["voice"], "texts": sc["texts"]}
        try:
            await self._ramp_to(sc, writer, lines)
            meta["plateau_start_ts"] = time.time()
            await asyncio.sleep(half)
            meta["spike_ts"] = time.time()
            for _ in range(spiked - self._active):  # мгновенно, без ramp
                self._spawn(sc, writer, sid=self._active)
            await asyncio.sleep(half)
            meta["plateau_end_ts"] = time.time()
            await self._stop_all()
        finally:
            writer.close()
        records = load_jsonl(str(jsonl))
        meta["summary_pre"] = aggregate(records, meta["plateau_start_ts"], meta["spike_ts"])
        meta["summary_post"] = aggregate(records, meta["spike_ts"], meta["plateau_end_ts"])
        (self.run_dir / f"{name}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{name}] spike {lines}->{spiked}: ttfb_p95 "
              f"{meta['summary_pre']['ttfb_p95_ms']:.0f}ms -> "
              f"{meta['summary_post']['ttfb_p95_ms']:.0f}ms", flush=True)
        return meta


async def amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.scenarios, args.profile)
    run_dir = Path(cfg["defaults"]["results_dir"]) / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    runner = ScenarioRunner(cfg["defaults"], cfg["sla"], run_dir, args.endpoint)
    scenarios = cfg[args.set]
    if args.only:
        scenarios = [s for s in scenarios if s["name"] in set(args.only)]
    for sc in scenarios:
        if sc["mode"] == "plateau":
            await runner.run_plateau(sc, int(sc["lines"]))
        elif sc["mode"] == "nmax":
            await runner.run_nmax(sc)
        elif sc["mode"] == "spike":
            await runner.run_spike(sc)
        else:
            raise SystemExit(f"неизвестный режим сценария: {sc['mode']!r}")
    print(f"Готово. Результаты: {run_dir}", flush=True)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="TTS load test runner")
    p.add_argument("--scenarios", default="config/scenarios.yaml")
    p.add_argument("--profile", default="cloud", help="cloud | smoke")
    p.add_argument("--set", default="scenarios", dest="set",
                   help="ключ списка сценариев: scenarios | smoke_scenarios")
    p.add_argument("--endpoint", default=None, help="переопределить endpoint TTS")
    p.add_argument("--run-id", default=time.strftime("%Y%m%d_%H%M%S"))
    p.add_argument("--only", nargs="*", default=None, help="запустить только эти сценарии")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
