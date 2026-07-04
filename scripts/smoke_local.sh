#!/usr/bin/env bash
# Локальный smoke: mock-сервер + squeezed-матрица (профиль smoke) + отчёт.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN_ID="smoke_${1:-$(date +%Y%m%d_%H%M%S)}"
PY=.venv/bin/python
PORT=8022

$PY -m mock_server.server --port $PORT &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT

# ждём, пока сервер начнёт слушать
for _ in $(seq 1 50); do
  if $PY - <<EOF
import socket, sys
s = socket.socket()
s.settimeout(0.2)
sys.exit(0 if s.connect_ex(("127.0.0.1", $PORT)) == 0 else 1)
EOF
  then break; fi
  sleep 0.2
done

$PY -m loadgen.runner --scenarios config/scenarios.yaml --profile smoke \
    --set smoke_scenarios --endpoint "ws://127.0.0.1:$PORT/tts" --run-id "$RUN_ID"

$PY -m report.build_report --run-dir "results/$RUN_ID" --pricing config/pricing.yaml --local

echo
echo ">>> Smoke OK. Отчёт: results/$RUN_ID/report.md"
