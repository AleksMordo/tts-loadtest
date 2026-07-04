"""Патч token2wav: паддинг хвостового мел-чанка < 3 фреймов.

hift f0_predictor (conv kernel 3) падает на финальном чанке из 1-2 мел-фреймов:
"Calculated padded input size per channel: (2). Kernel size: (3)".
Паддим репликацией последнего фрейма — пара мс тишины в конце, безвредно.
Запуск: python3 patch_token2wav.py <model.py> [<model.py> ...]
"""
import sys

MARKER = "        # keep overlap mel and hift cache"
PATCH = (
    "        # PATCH(стенд): hift f0_predictor требует >=3 мел-фреймов (conv kernel 3);\n"
    "        # пустой хвост пропускаем, крошечный — паддим репликацией последнего фрейма\n"
    "        if tts_mel.shape[2] == 0:\n"
    "            return torch.zeros(1, 0)\n"
    "        if tts_mel.shape[2] < 4:\n"
    "            tts_mel = torch.nn.functional.pad(tts_mel, (0, 4 - tts_mel.shape[2]), mode=\"replicate\")\n"
    + MARKER
)

changed = False
for path in sys.argv[1:]:
    try:
        src = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        continue
    if "PATCH(стенд)" in src:
        continue
    if MARKER not in src:
        raise SystemExit(f"{path}: маркер не найден — код token2wav изменился, обновите патч")
    open(path, "w", encoding="utf-8").write(src.replace(MARKER, PATCH, 1))
    changed = True
    print(f"patched: {path}")
print("CHANGED" if changed else "NOCHANGE")
