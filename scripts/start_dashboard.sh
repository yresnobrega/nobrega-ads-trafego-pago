#!/bin/bash
# start_dashboard.sh — sobe http server na pasta de dashboards (idempotente)
set -euo pipefail

DASH_DIR="${HOME}/.operacao-ia/dashboards"
PORT="${PORT:-8888}"
LOG="${HOME}/.operacao-ia/logs/dashboard-server.log"

mkdir -p "$(dirname "$LOG")" "$DASH_DIR"

# lsof pode não estar no PATH restrito de um LaunchAgent — usa caminho absoluto
LSOF="$(command -v lsof || echo /usr/sbin/lsof)"

# Mata server anterior se já estiver rodando
if "$LSOF" -ti tcp:${PORT} >/dev/null 2>&1; then
  echo "Server já rodando em :${PORT}, reiniciando..."
  "$LSOF" -ti tcp:${PORT} | xargs kill -9 2>/dev/null || true
  sleep 1
fi

echo "[$(date)] start dashboard server (com refresh ao vivo) on :${PORT}" >> "$LOG"
exec python3 "$(dirname "$0")/dashboard_server_mac.py" >> "$LOG" 2>&1
