# tts-loadtest

> **История проекта и текущее состояние**: [docs/session-journal-2026-07.md](docs/session-journal-2026-07.md) —
> хронология трёх GPU-сессий, все 17 найденных проблем с решениями, экономика по
> фактическому биллингу. Метрики: [results/report_a100_2026-07-03.md](results/report_a100_2026-07-03.md).

Стенд нагрузочного тестирования стримингового TTS (CosyVoice 2/3, zero-shot клонирование)
для телефонии: сайзинг GPU в Yandex Cloud под 10/30/100 одновременных линий и расчёт
стоимости секунды синтеза.

## SLA и определения

| Метрика | SLA |
|---|---|
| TTFB (текст → первый аудио-чанк), p95 | ≤ 500 мс |
| RTF потока (время генерации / длительность аудио), каждый поток | ≤ 0.8 |
| Underruns (гэп между чанками > длительности предыдущего чанка) | 0 на полке |
| Error rate | < 0.1% |
| VRAM за 30 мин | без роста |
| GPU util в устойчивом режиме | ≤ 85% |

**N_max** — максимум одновременных линий на одном GPU-инстансе, при котором все SLA
выполняются на 15-минутной полке. Ищется автоматически: полки с шагом +10 до первого
нарушения SLA.

## Быстрый старт (локально, без GPU)

```bash
make venv          # python3.11+; ставит зависимости + ansible-коллекции
make lint          # ruff, terraform validate, ansible-lint, pytest (юнит + e2e против mock)
make smoke-local   # mock-сервер + squeezed-матрица + отчёт в results/smoke_*/report.md
```

Mock-сервер (`mock_server/`) эмулирует GPU: TTFB/RTF деградируют с ростом числа линий,
после `max_sessions_before_degradation: 20` начинаются underruns и срыв SLA.
E2E-тест (`tests/test_e2e_mock.py`) проверяет, что раннер **сам** находит N_max = 20±2
и что это попадает в отчёт. Цифры локального прогона к производительности GPU
отношения не имеют — проверяется пайплайн, не железо.

## Запуск в облаке

### 1. Что заполнить перед запуском

- `config/stand.yaml` — `folder_id`, зона, **платформа GPU** (`gpu_platform_id`),
  ваш IP для SSH (`allowed_ssh_cidr`); секция `tts`: GitHub-репозиторий сервиса
  (FastCosyVoice), ревизия (`git_ref` — зафиксировать коммит!), модель
  (`model_id`, ModelScope id), `max_conc`, `stream`.
  Платформы-кандидаты: `gpu-standard-v3` (A100 80GB), `standard-v3-t4` (T4 16GB),
  `gpu-standard-v2` (V100 32GB), новая платформа V4 — **сверить актуальный список и цены**:
  <https://yandex.cloud/ru/docs/compute/pricing>.
- `terraform/yandex/terraform.tfvars` — скопировать из `terraform.tfvars.example`,
  значения те же, что в stand.yaml. Дефолтные размеры ужаты под стандартные квоты
  облака (32 vCPU / 128 GB RAM / 200 GB SSD): GPU-ВМ 28 vCPU+150 GB, loadgen 4 vCPU/8 GB/30 GB.
- `config/pricing.yaml` — цены аренды из прайса YC (ВМ целиком, диск, IP). В коде цен нет.
- Аутентификация Terraform: `export YC_TOKEN=$(yc iam create-token)`.
- Референс-аудио для clone: положить в `assets/ref_audio/` wav+транскрипт
  (в репозитории их нет — реальные записи голоса не публикуются; синтетический
  референс для mock генерирует `python scripts/gen_ref_audio.py`).
  Для реального прогона нужны настоящие записи речи
  **16 kHz mono WAV 5–15 сек** + рядом `.txt` с точным транскриптом
  (zero-shot CosyVoice требует prompt_text): на псевдо-шуме клонирование даст мусор
  на выходе — на нагрузку это не влияет, но слушать сгенерированное будет нельзя.

### 2. Прогон

```bash
make up                                   # terraform apply + ansible deploy (сборка образа ~20 мин)
make test  RUN_ID=a100_run1               # полная матрица scenarios.yaml (выполняется на loadgen-ВМ)
make report RUN_ID=a100_run1              # GPU-метрики из Prometheus + report.md + CSV
make down                                 # terraform destroy — НЕ ЗАБЫТЬ
```

Деплой TTS не использует private registry: ansible клонирует FastCosyVoice на GPU-ВМ,
подкладывает наш патченный gRPC-сервер (`ansible/roles/tts/files/grpc_server_stream.py`:
`--stream` — аудио чанками по мере генерации, иначе TTFB равен генерации целого
предложения; `--max_conc` — лимит одновременных RPC, сверх которого сервер
отклоняет запросы), собирает docker-образ и качает веса модели с ModelScope.

Матрица (config/scenarios.yaml): полки {10, 30, 100} × mixed-тексты × {cached, clone},
поиск N_max (mixed/cached, шаг +10), spike-тест 30→60. Полка 15 мин, ramp-up +5 линий/10 c.

### 3. Ожидаемая стоимость самого теста

Матрица: 6 полок × 15 мин + поиск N_max (~4–8 полок) + spike ≈ **3–4 часа** чистого прогона;
с деплоем и повторами закладывайте **6–8 часов** аренды.

Стоимость ≈ `часы × (gpu_vm_per_hour + disk_per_hour + public_ip_per_hour)` из вашего
pricing.yaml + ~50 ₽-уровень за CPU-ВМ loadgen. Фактически (июль 2026, A100
`gpu-standard-v3`): GPU-ВМ целиком 472.37 ₽/час, C_hour = 475.62 ₽/час; весь проект
(~14 GPU·ч за три сессии, включая отладку) обошёлся в **6 728 ₽**.

`preemptible: true` в tfvars удешевляет тест, но прерываемую ВМ могут отозвать в любой
момент — полку 100 линий и финальные замеры для прайсинга гнать на обычной ВМ.

### 4. Что где происходит

- **Terraform** (`terraform/yandex/`): VPC + подсеть, security group (внутри всё открыто,
  снаружи только SSH с вашего IP), GPU-ВМ (TTS), CPU-ВМ (генератор нагрузки + Prometheus).
  Генератор в той же подсети — внешняя сеть исключена из измерений.
- **Ansible** (`ansible/`): роли common (chrony, sysctl, fd-лимиты под 100+ линий),
  nvidia (драйверы + container toolkit, пропускается на GPU-optimized образе), docker,
  tts (docker compose из private registry), monitoring (dcgm-exporter + node-exporter на GPU,
  Prometheus на loadgen-ВМ), loadgen (код + venv). Inventory генерируется из
  `terraform output` (`make inventory`).
- **Раннер** (`loadgen/runner.py`): идёт по scenarios.yaml сам; на каждый запрос пишет
  JSONL-строку (TTFB, RTF, underruns, inter-arrival, ошибки) — без агрегации на лету.
- **Отчёт** (`report/`): `collect.py` снимает GPU util/VRAM из Prometheus за окно каждой
  полки, `cost_model.py` считает стоимость по формулам ниже, `build_report.py` собирает
  `report.md` + `summary.csv`.

## Бэкенды сервинга

`config/stand.yaml: tts.backend` переключает способ деплоя TTS на GPU-ВМ:

- **`triton_v3`** (основной, текущий): CosyVoice3 (`Fun-CosyVoice3-0.5B-2512`),
  runtime апстрима FunAudioLLM/CosyVoice (`v3_runtime_ref`): tritonserver
  :18000-18002 (BLS `cosyvoice3`) + отдельный `trtllm-serve` :8000 для LLM —
  архитектура без BLS-клина v2. **Русский язык и клонирование голоса работают**
  (v2 на русском детерминированно ломался: пустые ответы / болтовня до токен-капа).
  Прогнана полная матрица 10/30/100 × cached/clone: GPU util 98–100% на всех
  полках, throughput ~6.5 аудио-сек/сек (≈16 линий при duty 0.4), одиночный
  TTFB ~900 мс > SLA 500 мс — нужен тюнинг латентности (см. итоги ниже).
- **`triton`** (CosyVoice2): Triton Inference Server + TensorRT-LLM (bfloat16),
  decoupled streaming. **Runtime берётся из апстрима FunAudioLLM/CosyVoice, коммит
  запинован (`triton_runtime_ref`)** — triton-каталог форка несовместим с образом
  `soar97/triton-cosyvoice:25.06` (проверено на GPU 2026-07-03). Замерено на A100:
  TTFB 241–262 мс (тёплый), RTF 0.10–0.23 — SLA проходят с запасом на одиночном потоке.
  Критические особенности (все учтены в роли):
  `network_mode: host` обязателен (docker-proxy молча ломает decoupled-стрим);
  KV-кеш LLM поднят 2560→65536 токенов; **`bls_instance_num` держать = 1**:
  инстансы ≥2 навсегда виснут на вызове tensorrt_llm — подтверждено апстримным
  [issue #1866](https://github.com/FunAudioLLM/CosyVoice/issues/1866), фикс ожидается
  только с новым TRT-LLM (1.x) — см. results/report_a100_2026-07-03.md, раздел
  «Следующие шаги». До фикса конкурентность = 1 и полная матрица не имеет смысла.
- **`python_grpc`** (для сравнения): штатный `runtime/python/grpc` + наши патчи
  (--stream/--fp16/--load_trt, tmp-wav для zero-shot, обрыв по отмене RPC).
  **Замерен 2026-07-03 на A100: непригоден для телефонии** — N_max = 0 при SLA
  (TTFB 1.4 c на одиночном потоке, RTF удваивается с каждой линией, GPU util ~2%,
  узкое место CPU/GIL). Результаты: `results/diag/report.md`.

## Протокол TTS

Боевой сервис — [FastCosyVoice](https://github.com/Brakanier/FastCosyVoice)
(форк CosyVoice), его gRPC-сервер `runtime/python/grpc`:
`CosyVoice.Inference(Request) → stream Response{tts_audio}` (server-streaming).
Proto скопирован из репозитория 1:1 в `loadgen/proto/cosyvoice.proto`
(перегенерация стабов — `make proto`). Ключевые особенности API:

- `prompt_audio` — **raw PCM s16le 16 kHz mono без wav-заголовка** (клиент сам
  срезает заголовок при загрузке референсов);
- ответ — PCM на **sample rate модели (CosyVoice2 = 24 kHz)**, до телефонных 8 kHz
  ресемплится дальше по тракту (вне этого теста); все метрики считаются на 24 kHz;
- **кеша speaker embedding в API нет**: наш режим `clone` = zero_shot со случайным
  референсом на каждый запрос, `cached` = zero_shot с одним и тем же референсом
  (embedding всё равно пересчитывается сервером). Если задеплоите SFT-модель —
  задайте `sft_spk_id` в scenarios.yaml, тогда `cached` пойдёт через `sftRequest`;
- у сервера жёсткий лимит `--max_conc` (одновременные RPC): сверх лимита —
  RESOURCE_EXHAUSTED (у нас считается ошибкой и валит SLA — это корректно).

**WebSocket**-транспорт остаётся только для mock/локальной проверки: JSON
`{text, voice_id | ref_audio_b64, sample_rate}` → бинарные PCM-чанки → `{"event": "end"}`.
Mock-сервер поднимает оба протокола (ws :8022, grpc :8023 — тот же cosyvoice.proto).
Поиск N_max в e2e гоняется по WS (конкурентность по соединениям — детерминирована),
корректность gRPC-пути проверяют `tests/test_e2e_grpc.py` (zero_shot и sft).

Режимы голоса: `cached` (voice_id заранее подготовленного эмбеддинга) и `clone`
(референс-wav в каждом запросе), профиль `mixed` = 90/10.

## Модель стоимости

Из `config/pricing.yaml` (заполняется вручную из прайса YC) и измеренного N_max:

```
C_hour              = gpu_vm_per_hour + disk_per_hour + public_ip_per_hour
линия/час           = C_hour / N_max
секунда синтеза     = C_hour / (N_max · 3600)                  # грязная, 100% занятость
секунда синтеза     = C_hour / (N_max · 3600 · duty_cycle)     # реальная
инстансов под X     = ceil(X / (N_max / 1.3))                  # 30% запас на пики
минута разговора    = линия/час / 60
```

Отчёт выводит таблицу для X ∈ {10, 30, 100}: инстансы, ₽/час, ₽/месяц (720 ч),
₽/сек синтеза, ₽/мин разговора.

## Структура

```
Makefile            — единая точка входа (lint / smoke-local / up / test / report / down)
config/             — stand.yaml, scenarios.yaml, pricing.yaml
terraform/yandex/   — VPC, GPU-ВМ, loadgen-ВМ, security groups
ansible/            — роли деплоя; inventory генерируется из terraform output
loadgen/            — asyncio-клиент, модель линии, метрики, раннер, корпуса текстов
mock_server/        — mock TTS с эмуляцией деградации GPU
report/             — сбор метрик из Prometheus, cost-модель, сборка отчёта
tests/              — pytest: юнит (metrics, cost_model) + e2e против mock
scripts/            — генерация inventory/group_vars/референс-аудио, smoke-скрипт
```

## Итоги (2026-07-04) и что дальше

Реальные прогоны на A100 выполнены (три GPU-сессии, ~14 GPU·ч, подробности —
в [журнале](docs/session-journal-2026-07.md) и отчётах в `results/`):

- **python_grpc непригоден для телефонии**: N_max = 0 при SLA (TTFB 1.4 c, GPU util ~2%,
  узкое место CPU/GIL).
- **CosyVoice2 + Triton**: перф целевой на одиночном потоке (TTFB 241–262 мс, RTF 0.10–0.23),
  но русский язык сломан (пустые ответы / болтовня до токен-капа) и BLS ≥ 2 клинит.
- **CosyVoice3 + Triton (`triton_v3`) — рабочий путь**: русский и клонирование голоса
  работают, GPU загружается честно (98–100%), throughput ~6.5 аудио-сек/сек.
  По фактическому биллингу: **секунда синтеза 0.0202 ₽, ~16 линий/GPU при duty 0.4,
  минута разговора 0.495 ₽**; 10/30/100 линий = 1/3/9 A100 ≈ 342 тыс / 1.03 млн /
  3.08 млн ₽/мес. Оговорка: это ёмкость по throughput (мягкие SLA).

Следующий шаг: тюнинг латентности v3 (одиночный TTFB ~900 мс > SLA 500 мс) —
стартовый `token_hop`, `TRITON_MAX_BATCH_SIZE=1` для token2wav/vocoder,
`BLS_INSTANCE_NUM`, kv-fraction; затем полки 2–8 линий → строгий SLA-N_max.
