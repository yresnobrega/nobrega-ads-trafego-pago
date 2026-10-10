#!/usr/bin/env bash
# run_fetch_vps.sh — coleta + dashboard rodando direto na VPS (sem precisar do Mac).
# Usado pelo cron da VPS (com.zxlab.meta-fetch equivalente).

META_DIR="$HOME/.operacao-ia/scripts/meta"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

"$PYTHON_BIN" "$META_DIR/fetch_metrics.py" "$@"
FETCH_EXIT=$?

# Dashboard roda mesmo se alguma janela individual falhou (rate limit pontual)
# — usa o que deu certo em vez de travar tudo por causa de uma janela.
if [ -f "$META_DIR/paid_traffic_dashboard.py" ]; then
    "$PYTHON_BIN" "$META_DIR/paid_traffic_dashboard.py"
fi

exit $FETCH_EXIT
