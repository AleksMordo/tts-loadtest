"""Патч BLS cosyvoice2: сериализация обращений к python-backend стабу.

Причина: BLS потребляет decoupled-стрим tensorrt_llm в отдельном потоке
(_llm_gen_thread), параллельно из главного потока вызывая token2wav.exec().
Обе операции ходят через один IPC-канал стаба; GIL отпускается на C-границе —
гонка в C++ слое теряет сообщения: запросы к LLM исчезают (executor idle,
BLS ждёт вечно), стримы обрезаются. Симптом: интермиттентный клин всего
сервера (FunAudioLLM/CosyVoice#1866).

Фикс: threading.Lock вокруг каждого next() LLM-итератора и каждого exec().

Запуск: python3 patch_bls_lock.py <model.py>
"""
import sys

MARK = "PATCH-BLS-LOCK"

INIT_MARKER = "        self.decoupled = pb_utils.using_decoupled_model_transaction_policy(self.model_config)"
INIT_PATCH = INIT_MARKER + f"""
        # {MARK}: сериализация обращений к стабу (см. docstring патча)
        self._stub_lock = threading.Lock()"""

# forward_llm: обернуть итерацию decoupled-ответов
ITER_MARKER = """        llm_responses = llm_request.exec(decoupled=self.decoupled)
        if self.decoupled:
            for llm_response in llm_responses:
                if llm_response.has_error():
                    raise pb_utils.TritonModelException(llm_response.error().message())"""
ITER_PATCH = f"""        with self._stub_lock:  # {MARK}
            llm_responses = llm_request.exec(decoupled=self.decoupled)
        if self.decoupled:
            _llm_iter = iter(llm_responses)
            while True:
                with self._stub_lock:  # {MARK}
                    try:
                        llm_response = next(_llm_iter)
                    except StopIteration:
                        break
                if llm_response.has_error():
                    raise pb_utils.TritonModelException(llm_response.error().message())"""

# token2wav (и другие унарные exec) — под тем же локом
UNARY_MARKER = "        inference_response = inference_request.exec()"
UNARY_PATCH = f"""        with self._stub_lock:  # {MARK}
            inference_response = inference_request.exec()"""


def main(path: str) -> None:
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("NOCHANGE")
        return
    for marker, patch, what in [
        (INIT_MARKER, INIT_PATCH, "init"),
        (ITER_MARKER, ITER_PATCH, "llm iter"),
    ]:
        if marker not in src:
            raise SystemExit(f"{path}: маркер '{what}' не найден — код BLS изменился")
        src = src.replace(marker, patch, 1)
    n = src.count(UNARY_MARKER)
    if n == 0:
        raise SystemExit(f"{path}: маркер unary exec не найден")
    src = src.replace(UNARY_MARKER, UNARY_PATCH)
    if "import threading" not in src:
        src = src.replace("import numpy as np", "import numpy as np\nimport threading", 1)
    open(path, "w", encoding="utf-8").write(src)
    print(f"patched ({n} unary exec): {path}")
    print("CHANGED")


if __name__ == "__main__":
    main(sys.argv[1])
