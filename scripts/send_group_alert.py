#!/usr/bin/env python3
"""Lê uma mensagem do stdin e envia via WhatsApp usando a config local.

Roda na VPS — recebe alertas do monitor de contas (check_account_alerts.py,
no Mac, via SSH) e entrega usando o Evolution API/grupo já configurados aqui
em meta.env. Mantém o token do Meta fora da VPS (só quem chega é o texto do
alerta) e o token do WhatsApp fora do Mac.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_whatsapp_report import load_config, send_cloud_api, send_evolution  # noqa: E402


def main() -> int:
    message = sys.stdin.read()
    if not message.strip():
        print("mensagem vazia", file=sys.stderr)
        return 1

    try:
        config, missing = load_config()
    except ValueError as e:
        print(f"config WhatsApp inválida: {e}", file=sys.stderr)
        return 1
    if missing:
        print(f"config WhatsApp incompleta: {missing}", file=sys.stderr)
        return 1

    try:
        if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
            send_cloud_api(config, message)
        else:
            send_evolution(config, message)
    except Exception as e:
        print(f"falha ao enviar: {e}", file=sys.stderr)
        return 1

    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
