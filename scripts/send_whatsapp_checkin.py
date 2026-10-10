#!/usr/bin/env python3
"""Check-in — 1 mensagem no WhatsApp com status de todas as contas.

Diferente do relatório de campanhas (send_whatsapp_reports_por_conta.py):
não lista campanha por campanha. Só responde 3 perguntas por conta:
1. Está ativa?
2. Já começou a gastar hoje?
3. Tem sinal de que vai parar por pagamento?

Lê o dashboard já gerado pelo com.nobregaads.meta-fetch (paid-traffic-1d.json) —
não chama a Graph API, só formata o que já foi coletado.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_account_alerts import DISABLE_REASON_LABELS  # noqa: E402
from report_formatter import WHATSAPP_LIMIT, format_number_br  # noqa: E402
from send_whatsapp_report import (  # noqa: E402
    CONFIG_PATH,
    _cloud_error_details,
    _safe_error_message,
    load_config,
    mask_number,
    send_cloud_api,
    send_evolution,
)
from urllib.error import HTTPError, URLError

OPERACAO = Path.home() / ".operacao-ia"
DASHBOARD_1D = OPERACAO / "dashboards" / "paid-traffic-1d.json"
BRT = timezone(timedelta(hours=-3))

# account_status == 9 (EM PERÍODO DE CARÊNCIA) e disable_reason 3/4 (pagamento
# recusado/pendente) são os sinais que a Meta expõe ANTES de desabilitar a
# conta de vez — é o "vai parar por pagamento" que o aluno pediu pra destacar.
PAYMENT_RISK_STATUS_CODES = {9}
PAYMENT_RISK_DISABLE_REASONS = {3, 4}


def classify(account: dict) -> str:
    code = account.get("account_status")
    reason = account.get("disable_reason") or 0
    if code == 1 and reason in (0, None):
        return "ok"
    if code in PAYMENT_RISK_STATUS_CODES or reason in PAYMENT_RISK_DISABLE_REASONS:
        return "payment_risk"
    return "other_problem"


def build_accounts_section(payload: dict, now: datetime) -> tuple[str, list]:
    accounts = payload.get("accounts") or []
    emoji = "🌙" if now.hour >= 19 else "☀️"
    lines = [f"{emoji} *CHECK-IN* | {now.strftime('%d/%m/%Y às %H:%M')}"]
    lines.append("")
    lines.append("*Contas:*")

    warnings = []
    for acc in accounts:
        name = acc.get("account_name") or acc.get("ad_account_id") or "conta sem nome"
        spend = acc.get("spend") or 0
        status_label = acc.get("account_status_label") or "desconhecido"
        state = classify(acc)

        if spend > 0:
            spend_txt = f"já gastando R$ {format_number_br(spend)}"
        else:
            spend_txt = "ainda sem gasto hoje"

        if state == "ok":
            emoji = "🟢"
        elif state == "payment_risk":
            emoji = "💳"
        else:
            emoji = "🔴"

        lines.append(f"{emoji} {name} — {status_label}, {spend_txt}")

        if state != "ok":
            reason = acc.get("disable_reason") or 0
            reason_label = DISABLE_REASON_LABELS.get(reason)
            detail = f"{status_label}"
            if reason_label:
                detail += f" — {reason_label} (código {reason})"
            elif reason:
                detail += f" — código Meta {reason}, confira no Gerenciador de Negócios"
            warnings.append((state, name, detail))

    return "\n".join(lines).rstrip(), warnings


def build_warnings_section(warnings: list) -> str:
    lines = []
    payment_risks = [w for w in warnings if w[0] == "payment_risk"]
    other_problems = [w for w in warnings if w[0] == "other_problem"]

    if payment_risks:
        lines.append("💳 *RISCO DE PARAR POR PAGAMENTO:*")
        for _, name, detail in payment_risks:
            lines.append(f"• {name}: {detail}")
        lines.append("")

    if other_problems:
        lines.append("⚠️ *OUTRAS CONTAS COM PROBLEMA:*")
        for _, name, detail in other_problems:
            lines.append(f"• {name}: {detail}")
        lines.append("")

    if not warnings:
        lines.append("✅ Nenhum sinal de risco de pagamento hoje.")

    return "\n".join(lines).rstrip()


def _truncate(text: str) -> str:
    if len(text) <= WHATSAPP_LIMIT:
        return text
    suffix = "\n… *lista cortada — confira o dashboard completo.*"
    return text[: WHATSAPP_LIMIT - len(suffix)].rstrip() + suffix


def build_messages(payload: dict, now: datetime) -> list:
    """1 mensagem só (contas + avisos) quando cabe no limite do WhatsApp.
    Quando não cabe, manda em 2 mensagens (contas / avisos) em vez de cortar
    informação no meio — cada uma só é truncada se ela sozinha ainda estourar."""
    accounts_msg, warnings = build_accounts_section(payload, now)
    warnings_msg = build_warnings_section(warnings)

    combined = accounts_msg + "\n\n" + warnings_msg
    if len(combined) <= WHATSAPP_LIMIT:
        return [combined]

    return [_truncate(accounts_msg), _truncate(warnings_msg)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Envia 1 check-in (status + gasto + risco de pagamento) no WhatsApp"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="imprime a mensagem sem conectar"
    )
    args = parser.parse_args()

    if not DASHBOARD_1D.exists():
        print(
            f"❌ {DASHBOARD_1D} não existe. Rode o coletor (com.nobregaads.meta-fetch) antes.",
            file=sys.stderr,
        )
        return 3
    try:
        payload = json.loads(DASHBOARD_1D.read_text())
    except (OSError, ValueError) as exc:
        print(f"❌ Não foi possível ler {DASHBOARD_1D}: {exc}", file=sys.stderr)
        return 3

    accounts = payload.get("accounts") or []
    if not accounts:
        print("❌ Nenhuma conta no dashboard de 1d.", file=sys.stderr)
        return 3

    now = datetime.now(BRT)
    try:
        generated_at = datetime.fromisoformat(payload.get("generated_at"))
    except (TypeError, ValueError):
        generated_at = None
    stale = generated_at is None or (now - generated_at) > timedelta(hours=26)
    messages = build_messages(payload, now)
    if stale:
        messages[0] = "⚠️ *DADO DEFASADO* (coleta com mais de 26h) — confira o coletor.\n\n" + messages[0]

    if args.dry_run:
        for i, msg in enumerate(messages, 1):
            if len(messages) > 1:
                print(f"--- mensagem {i}/{len(messages)} ---")
            print(msg)
        return 0

    try:
        config, missing = load_config()
    except (OSError, ValueError) as exc:
        print(f"❌ Configuração de WhatsApp inválida: {exc}", file=sys.stderr)
        print(f"   Corrija {CONFIG_PATH}. Nada foi enviado.", file=sys.stderr)
        return 2
    if missing:
        provider = config["REPORT_WHATSAPP_PROVIDER"]
        print(f"❌ Configuração incompleta para o provider {provider}. Chaves ausentes:", file=sys.stderr)
        for key in missing:
            print(f"   - {key}", file=sys.stderr)
        return 2

    masked = mask_number(str(config["REPORT_WHATSAPP_TO"]))
    for i, msg in enumerate(messages, 1):
        try:
            if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
                send_cloud_api(config, msg)
            else:
                send_evolution(config, msg)
        except HTTPError as exc:
            if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
                code, detail = _cloud_error_details(exc)
                print(
                    f"❌ WhatsApp Cloud API recusou o envio da mensagem {i}/{len(messages)} (HTTP {exc.code}"
                    + (f", código {code}" if code else "")
                    + ").",
                    file=sys.stderr,
                )
                if detail:
                    print("   " + _safe_error_message(detail, config), file=sys.stderr)
            else:
                print(
                    f"❌ Evolution API recusou o envio da mensagem {i}/{len(messages)} (HTTP {exc.code}).",
                    file=sys.stderr,
                )
            return 4
        except URLError:
            print(f"❌ Não foi possível conectar ao provider (mensagem {i}/{len(messages)}).", file=sys.stderr)
            return 4
        except (OSError, ValueError):
            print(f"❌ Falha local ao enviar a mensagem {i}/{len(messages)}. Nada foi exposto.", file=sys.stderr)
            return 4

    suffix = f" ({len(messages)} mensagens)" if len(messages) > 1 else ""
    print(f"✅ Check-in enviado no WhatsApp para {masked}{suffix}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
