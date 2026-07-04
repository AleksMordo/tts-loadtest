"""Патч BLS cosyvoice2: LLM вызывается в offline-режиме (не decoupled).

Причина: decoupled-стрим tensorrt_llm + потребление его итератора в отдельном
потоке BLS (параллельно с token2wav.exec из главного потока) интермиттентно
теряет запросы/сообщения в python-backend (клин всего сервера, симптомы
FunAudioLLM/CosyVoice#1866; воспроизводится и на TRT-LLM 1.x).
decoupled=False для LLM стабилен (подтверждено в issue). Аудио к клиенту
по-прежнему стримится чанками (chunk-loop BLS работает от массива токенов);
цена — TTFB включает полную генерацию токенов (~0.2-0.6 c на реплику на A100).

Требует decoupled_mode:False в config.pbtxt модели tensorrt_llm (sed в run.sh).
Запуск: python3 patch_llm_offline.py <model.py>
"""
import sys

MARK = "PATCH-LLM-OFFLINE"

STREAMING_MARKER = '            "streaming": np.array([[self.decoupled]], dtype=np.bool_),'
STREAMING_PATCH = (
    f'            "streaming": np.array([[False]], dtype=np.bool_),  # {MARK}'
)

EXEC_MARKER = """        llm_responses = llm_request.exec(decoupled=self.decoupled)
        if self.decoupled:"""
EXEC_PATCH = f"""        llm_responses = llm_request.exec(decoupled=False)  # {MARK}
        if False:  # {MARK}: offline-ветка ниже (else)"""


def main(path: str) -> None:
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("NOCHANGE")
        return
    for marker, patch, what in [
        (STREAMING_MARKER, STREAMING_PATCH, "streaming input"),
        (EXEC_MARKER, EXEC_PATCH, "exec/branch"),
    ]:
        if marker not in src:
            raise SystemExit(f"{path}: маркер '{what}' не найден — код BLS изменился")
        src = src.replace(marker, patch, 1)
    open(path, "w", encoding="utf-8").write(src)
    print(f"patched: {path}")
    print("CHANGED")


if __name__ == "__main__":
    main(sys.argv[1])
