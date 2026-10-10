#!/usr/bin/env python3
"""Checagem de fim de dia — alerta se alguma conta passou do teto de gasto diário
configurado em meta_perfil.json (budget_caps). Roda 1x/dia perto do fim do dia
via cron; usa o paid-traffic-1d.json (gasto de HOJE, em tempo real) já
coletado pelo fetch horário, sem chamada nova à Graph API.

IMPORTANTE: "0d" no dashboard é "ontem" (dia fechado, ver window_date_range()
em fetch_metrics.py) — usar 0d aqui faria essa checagem de 21:30 avaliar o dia
ANTERIOR ao invés do dia que está terminando, atrasando todo alerta em 1 dia
(bug corrigido em 2026-09-07).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_whatsapp_report import load_config, mask_number, send_cloud_api, send_evolution  # noqa: E402

OPERACAO = Path.home() / ".operacao-ia"
PERFIL = OPERACAO / "config" / "meta_perfil.json"
DASHBOARD_1D = OPERACAO / "dashboards" / "paid-traffic-1d.json"
STATE_PATH = OPERACAO / "config" / "budget_cap_alert_state.json"
LOG = OPERACAO / "logs" / "meta-budget-cap-alert.log"
BRT = timezone(timedelta(hours=-3))


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(BRT).isoformat(timespec="seconds")
    try:
        with LOG.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_alert(message):
    try:
        config, missing = load_config()
    except ValueError as e:
        log(f"ALERTA NÃO ENVIADO — config WhatsApp inválida: {e}")
        return False
    if missing:
        log(f"ALERTA NÃO ENVIADO — config WhatsApp incompleta: {missing}")
        return False
    try:
        if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
            send_cloud_api(config, message)
        else:
            send_evolution(config, message)
    except Exception as e:
        log(f"ALERTA FALHOU ao enviar: {e}")
        return False
    log(f"ALERTA ENVIADO para {mask_number(str(config['REPORT_WHATSAPP_TO']))}")
    return True


def main():
    if not PERFIL.exists():
        print("❌ meta_perfil.json não existe.")
        return 1
    perfil = json.loads(PERFIL.read_text())
    budget_caps = perfil.get("budget_caps") or {}
    if not budget_caps:
        log("Nenhum budget_caps configurado — nada a checar.")
        return 0

    if not DASHBOARD_1D.exists():
        log("paid-traffic-1d.json ausente — rode o fetch antes desta checagem.")
        return 1
    dashboard = json.loads(DASHBOARD_1D.read_text())
    spend_by_account = {
        acc.get("ad_account_id"): acc.get("spend")
        for acc in dashboard.get("accounts") or []
        if acc.get("ad_account_id") is not None
    }
    aliases = perfil.get("account_aliases") or {}

    today = datetime.now(BRT).strftime("%Y-%m-%d")
    state = load_state()
    already_alerted_today = set(state.get(today, []))

    over_cap = []
    for acc_id, cap in budget_caps.items():
        spend = spend_by_account.get(acc_id)
        if spend is None:
            log(f"{acc_id}: sem dado de gasto de hoje no dashboard 1d — pulando.")
            continue
        if spend > cap and acc_id not in already_alerted_today:
            over_cap.append((acc_id, aliases.get(acc_id, acc_id), spend, cap))

    if not over_cap:
        log("Nenhuma conta acima do teto diário (ou já alertada hoje).")
        return 0

    lines = ["💸 *ALERTA — TETO DE GASTO DIÁRIO ULTRAPASSADO*", ""]
    for acc_id, name, spend, cap in over_cap:
        lines.append(f"• {name}: R$ {spend:.2f} (teto R$ {cap:.2f})")
    lines.append("")
    lines.append("Confira as campanhas ativas no Gerenciador de Anúncios.")
    lines.append(datetime.now(BRT).isoformat(timespec="seconds"))

    sent = send_alert("\n".join(lines))
    if sent:
        alerted_today = already_alerted_today | {acc_id for acc_id, *_ in over_cap}
        state[today] = sorted(alerted_today)
        # mantém só os últimos 7 dias de estado, evita crescer pra sempre
        for key in list(state):
            if key < (datetime.now(BRT) - timedelta(days=7)).strftime("%Y-%m-%d"):
                del state[key]
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
