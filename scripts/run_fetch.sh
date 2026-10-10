#!/usr/bin/env bash
# run_fetch.sh — wrapper pro LaunchAgent meta-fetch.
# Carrega meta.env (META_ACCESS_TOKEN, META_AD_ACCOUNT_ID) e roda fetch_metrics.py.
# Usado por com.nobregaads.meta-fetch.plist.

META_ENV="$HOME/.operacao-ia/config/meta.env"
FETCH_PY="$HOME/.operacao-ia/scripts/meta/fetch_metrics.py"

if [ -f "$META_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$META_ENV"
    set +a
fi

DASHBOARD_PY="$HOME/.operacao-ia/scripts/meta/paid_traffic_dashboard.py"
DASH_DIR="$HOME/.operacao-ia/dashboards"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

"$PYTHON_BIN" "$FETCH_PY" "$@"
FETCH_EXIT=$?

# Dashboard e sincronização rodam mesmo se alguma janela individual falhou
# (ex: rate limit pontual) — o que importa é usar o que deu certo, em vez de
# travar tudo (inclusive o 1d, usado pelo WhatsApp) por causa de uma janela.
if [ -f "$DASHBOARD_PY" ]; then
    "$PYTHON_BIN" "$DASHBOARD_PY"
fi

# Sincroniza o dashboard inteiro (JSON + HTML) pra VPS — é lá que o WhatsApp
# (Evolution API) e a URL pública do dashboard (dashboard-trafego.service) rodam.
if [ -d "$DASH_DIR" ]; then
    rsync -az -e "ssh -o ConnectTimeout=10" "$DASH_DIR/" vps-claude:.operacao-ia/dashboards/ 2>>"$HOME/.operacao-ia/logs/meta-fetch.log" || true
fi

exit $FETCH_EXIT
