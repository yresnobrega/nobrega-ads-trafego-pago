#!/usr/bin/env python3
"""Monitor de status das contas Meta — dispara alerta imediato no WhatsApp.

Diferente do relatório periódico (3x/dia): roda a cada poucos minutos e só
manda mensagem quando o status de alguma conta MUDA (parou por pagamento,
restrição, revisão etc, ou voltou ao normal). Não repete a cada checagem —
usa um arquivo de estado pra saber o que já foi avisado, e reenvia um
lembrete só depois de um cooldown se o problema continuar sem resolução.
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_metrics import (  # noqa: E402
    ACCOUNT_STATUS_LABELS,
    fetch_account_status,
    fetch_campaign_meta,
    graph_get,
    read_token,
)
from send_whatsapp_report import load_config, mask_number, send_cloud_api, send_evolution  # noqa: E402
from check_daily_budget_cap import main as check_budget_caps  # noqa: E402

RESTRICTED_CAMPAIGN_STATUSES = {"DISAPPROVED", "WITH_ISSUES"}

OPERACAO = Path.home() / ".operacao-ia"
PERFIL = OPERACAO / "config" / "meta_perfil.json"
STATE_PATH = OPERACAO / "config" / "account_alert_state.json"
DASHBOARD_7D = OPERACAO / "dashboards" / "paid-traffic-7d.json"
LOG = OPERACAO / "logs" / "meta-account-alert.log"
BRT = timezone(timedelta(hours=-3))

REALERT_COOLDOWN_HOURS = 2

# Antes o monitor pulava a conta silenciosamente em qualquer falha de rede/API
# (DNS caindo, timeout etc) e só tentava de novo no próximo ciclo de 15min —
# sem retry imediato nem aviso se ficasse cego por horas (foi o que aconteceu
# em 2026-08-27 15h07: DNS caiu, as 15 contas falharam juntas e ninguém soube).
FETCH_RETRY_DELAY_SECONDS = 5
# Ciclos consecutivos falhando (cada ciclo já tenta 2x) antes de avisar que o
# monitor está sem conseguir checar — ~3 ciclos de 15min = 45min cego.
FETCH_FAIL_ALERT_THRESHOLD = 3

# Contas "Saldo disponível" (pré-pagas — cartão ou PIX) ficam sem entregar
# quando o saldo acaba, mesmo com account_status/disable_reason normais.
# O campo `balance` da Graph API NÃO é o saldo restante (confirmado ao vivo:
# não mudou depois de uma recarga real) — em vez disso rastreamos via
# /activities: funding_event_successful soma, ad_account_billing_charge
# subtrai. Precisa de uma âncora calibrada manualmente pra começar (ver
# calibrate_balance_anchor) — sem âncora, não alerta.
LOW_BALANCE_FLOOR_BRL = 100.0

# disable_reason que aparece com a conta ainda ATIVA (account_status == 1) —
# o Meta tentou cobrar e não conseguiu, mas ainda não parou de entregar.
# Aviso único (sem repetir) pra dar tempo de resolver antes que a conta pare
# de verdade (aí sim vira o alerta de "CONTA PARADA").
PAYMENT_WARNING_REASONS = {3, 4}

# account_status (não disable_reason) que significa "cobrança com problema,
# mas ainda pode estar entregando" — NÃO é parada de verdade:
# 9 = IN_GRACE_PERIOD (período de carência por pagamento)
# NÃO inclui 3 (UNSETTLED): testado ao vivo (conta Tudo pra Smartphone) e
# UNSETTLED de fato PARA de entregar — é "CONTA PARADA" mesmo, não aviso.
PAYMENT_WARNING_STATUSES = {9}

# Códigos de disable_reason mais comuns do Graph API. Códigos fora daqui
# ainda disparam alerta, só sem tradução amigável (mostra o número cru).
DISABLE_REASON_LABELS = {
    1: "violação de política de anúncios",
    2: "revisão de IP/rede suspeita",
    3: "problema de pagamento (cobrança recusada ou pendente)",
    4: "período de carência por pagamento",
    7: "conta fechada permanentemente",
}


def parse_meta_datetime(raw):
    """Python 3.9 fromisoformat() não aceita offset sem dois-pontos (+0000) — a
    Meta retorna nesse formato em event_time, então normaliza antes de parsear.
    """
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw.replace("Z", "+00:00"))
    return datetime.fromisoformat(normalized)


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


def resolve_account_ids(perfil):
    ids = perfil.get("ad_account_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids if i and "XXXXX" not in str(i)]
    single = perfil.get("ad_account_id")
    return [single] if single and "XXXXX" not in single else []


def load_avg_daily_spend():
    """spend/7 por conta a partir do dashboard 7d já coletado (evita mais chamadas à Graph API)."""
    if not DASHBOARD_7D.exists():
        return {}
    try:
        payload = json.loads(DASHBOARD_7D.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for acc in payload.get("accounts") or []:
        acc_id = acc.get("ad_account_id")
        spend = acc.get("spend")
        if acc_id and isinstance(spend, (int, float)):
            out[acc_id] = spend / 7
    return out


def is_prepay_account(funding_source_details):
    display = (funding_source_details or {}).get("display_string") or ""
    return "saldo" in display.lower()


def parse_prepay_balance(funding_source_details):
    """Extrai o valor de 'Saldo disponível (R$549,99 BRL)' — a Meta já manda
    o saldo real formatado nesse campo, então não precisa reconstruir nada
    via eventos incrementais. (A tentativa antiga de calibrar uma âncora
    manual e ir somando/subtraindo via /activities nunca foi implementada de
    verdade — o alerta de saldo baixo nunca disparava por causa disso, foi
    o que aconteceu com a Ribeiro.)
    """
    display = (funding_source_details or {}).get("display_string") or ""
    m = re.search(r"R\$\s*([\d.]+),(\d{2})", display)
    if not m:
        return None
    reais = m.group(1).replace(".", "")
    return float(reais) + float(m.group(2)) / 100


def check_low_balance(info, avg_daily_spend, acc):
    """Retorna (is_low, balance_brl, avg_daily) lendo o saldo direto da
    resposta da Meta — só se aplica a contas 'Saldo disponível' (pré-pagas)."""
    fsd = info.get("funding_source_details")
    if not is_prepay_account(fsd):
        return False, None, None
    balance_brl = parse_prepay_balance(fsd)
    if balance_brl is None:
        return False, None, None
    avg_daily = avg_daily_spend.get(acc)
    is_low = balance_brl < LOW_BALANCE_FLOOR_BRL
    return is_low, balance_brl, avg_daily


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
    account_ids = resolve_account_ids(perfil)
    if not account_ids:
        print("❌ Nenhuma conta configurada (ad_account_ids/ad_account_id).")
        return 1

    token = read_token()
    if not token:
        print("❌ Token Meta ausente em meta.env.")
        return 1

    state = load_state()
    avg_daily_spend = load_avg_daily_spend()
    now = datetime.now(BRT)
    now_iso = now.isoformat(timespec="seconds")

    health_degraded = []   # (acc, name_hint) que passaram a alertar "monitor cego" agora
    health_recovered = []  # (acc, name) que voltaram a ser checadas após alerta

    for acc in account_ids:
        prev = state.get(acc, {})
        fetch_health = prev.get("fetch_health", {})
        prev_warning = prev.get("payment_warning") or {}

        # Antes pulava a checagem inteira de conta já marcada com problema
        # (até 2h, pra economizar chamada de API) — só rechecava perto da
        # hora de um possível reenvio de alerta. Só que isso atrasava a
        # detecção de normalização por até 2h: a conta voltava a ATIVA na
        # Meta e o monitor nem olhava de novo pra perceber (aconteceu com a
        # Black Point BC — normalizou e só seria detectado ~1h20 depois).
        # Agora recheca toda conta em todo ciclo de 15min; o dedupe de
        # alerta (should_alert/cooldown mais abaixo) já evita spam.
        info = None
        last_error = None
        for attempt in (1, 2):
            try:
                info = fetch_account_status(acc, token)
                break
            except Exception as e:
                last_error = e
                if attempt == 1:
                    log(f"{acc}: falha ao checar status ({e}) — retry em {FETCH_RETRY_DELAY_SECONDS}s")
                    time.sleep(FETCH_RETRY_DELAY_SECONDS)

        if info is None:
            log(f"{acc}: falha ao checar status mesmo após retry ({last_error})")
            consecutive = fetch_health.get("consecutive_failures", 0) + 1
            last_alert_at = fetch_health.get("last_alert_at")
            was_alerted = bool(last_alert_at)
            should_flag = consecutive >= FETCH_FAIL_ALERT_THRESHOLD and (
                not was_alerted
                or (now - datetime.fromisoformat(last_alert_at)).total_seconds() > REALERT_COOLDOWN_HOURS * 3600
            )
            if should_flag:
                health_degraded.append((acc, prev.get("account_name") or acc))

            state[acc] = dict(prev)
            state[acc]["fetch_health"] = {
                "consecutive_failures": consecutive,
                "last_alert_at": last_alert_at,
            }
            continue

        if fetch_health.get("consecutive_failures", 0) >= FETCH_FAIL_ALERT_THRESHOLD:
            health_recovered.append((acc, info.get("account_name") or acc))

        code = info.get("account_status")
        disable_reason = info.get("disable_reason") or 0
        name = info.get("account_name") or acc
        status_label = info.get("account_status_label") or ACCOUNT_STATUS_LABELS.get(
            code, f"desconhecido ({code})"
        )
        # Conta ainda ATIVA mas com cobrança falhando (disable_reason 3/4) OU
        # com account_status UNSETTLED(3)/IN_GRACE_PERIOD(9) é um problema
        # DIFERENTE de conta já parada — vira aviso único (sem repetir), não
        # o alerta de "CONTA PARADA" (esse é só quando a conta de fato para
        # de entregar, não quando só tem cobrança pendente/em carência).
        is_payment_warning_status = code in PAYMENT_WARNING_STATUSES
        is_payment_warning = is_payment_warning_status or (code == 1 and disable_reason in PAYMENT_WARNING_REASONS)
        is_stopped = (code != 1) and not is_payment_warning_status

        was_warning = bool(prev_warning.get("active"))
        if is_payment_warning and not was_warning:
            reason_label = (
                DISABLE_REASON_LABELS.get(disable_reason, f"código {disable_reason}")
                if disable_reason
                else status_label
            )
            lines = [
                "⚠️ *AVISO — COBRANÇA NÃO PROCESSOU*",
                f"Conta: {name}",
                f"Motivo: {reason_label}",
                "",
                "A conta ainda está ATIVA, mas o Meta não conseguiu cobrar. Se não for "
                "resolvido, a conta para de entregar — sem novo aviso até lá.",
                "",
                "Atualize o pagamento: business.facebook.com/billing_hub",
                now_iso,
            ]
            send_alert("\n".join(lines))
        elif not is_payment_warning and was_warning and not is_stopped:
            send_alert(
                f"✅ *COBRANÇA NORMALIZADA*\n{name} — pagamento processado, sem risco de parar.\n{now_iso}"
            )
        payment_warning_state = {
            "active": is_payment_warning,
            "disable_reason": disable_reason if is_payment_warning else 0,
            "checked_at": now_iso,
        }

        is_problem = is_stopped

        was_problem = bool(prev.get("is_problem"))
        last_alert_at = prev.get("last_alert_at")

        if is_problem:
            should_alert = not was_problem
            if was_problem:
                if last_alert_at:
                    last_dt = datetime.fromisoformat(last_alert_at)
                    if (now - last_dt).total_seconds() > REALERT_COOLDOWN_HOURS * 3600:
                        should_alert = True
                else:
                    should_alert = True

            if should_alert:
                reason_label = DISABLE_REASON_LABELS.get(disable_reason)
                lines = [
                    "🚨 *ALERTA — CONTA META PARADA*",
                    f"Conta: {name}",
                    f"Status: {status_label}",
                ]
                if reason_label:
                    lines.append(f"Motivo provável: {reason_label} (código {disable_reason})")
                elif disable_reason:
                    lines.append(
                        f"Motivo (código Meta {disable_reason}) — confira detalhes no Gerenciador de Negócios"
                    )
                lines.append("")
                lines.append("Verifique: business.facebook.com/billing_hub")
                lines.append(now_iso)
                sent = send_alert("\n".join(lines))
                last_alert_at = now_iso if sent else last_alert_at

            state[acc] = {
                "is_problem": True,
                "account_status": code,
                "disable_reason": disable_reason,
                "last_alert_at": last_alert_at,
                "account_name": name,
            }
        else:
            if was_problem:
                send_alert(
                    f"✅ *CONTA NORMALIZADA*\n{name} voltou a ATIVA.\n{now_iso}"
                )
            state[acc] = {
                "is_problem": False,
                "account_status": code,
                "disable_reason": disable_reason,
                "last_alert_at": None,
                "account_name": name,
            }

        state[acc]["fetch_health"] = {"consecutive_failures": 0, "last_alert_at": None}
        state[acc]["payment_warning"] = payment_warning_state

        # Saldo baixo é independente do account_status/disable_reason — uma
        # conta "Saldo disponível" pode zerar e parar de entregar mesmo com
        # tudo "normal" nesses campos (foi o que aconteceu com a Ribeiro).
        is_low, balance_brl, avg_daily = check_low_balance(info, avg_daily_spend, acc)
        prev_low = prev.get("low_balance", {})
        was_low = bool(prev_low.get("is_low"))
        low_last_alert_at = prev_low.get("last_alert_at")

        if is_low:
            # Igual ao alerta de CONTA PARADA: avisa na transição pra "baixo"
            # e depois relembra a cada REALERT_COOLDOWN_HOURS enquanto continuar
            # baixo — sem isso a conta podia ficar dias sem saldo com um único
            # aviso perdido no meio do grupo (foi o que aconteceu com a Fênix).
            should_alert = not was_low
            if was_low:
                if low_last_alert_at:
                    last_dt = datetime.fromisoformat(low_last_alert_at)
                    if (now - last_dt).total_seconds() > REALERT_COOLDOWN_HOURS * 3600:
                        should_alert = True
                else:
                    should_alert = True

            if should_alert:
                lines = [
                    "🪫 *ALERTA — SALDO BAIXO*",
                    f"Conta: {name}",
                    f"Saldo disponível: R$ {balance_brl:.2f} (abaixo de R$ {LOW_BALANCE_FLOOR_BRL:.2f})",
                ]
                if avg_daily:
                    lines.append(f"Gasto médio diário (7d): R$ {avg_daily:.2f}")
                lines.append("")
                lines.append("Recarregue antes que a conta pare de entregar: business.facebook.com/billing_hub")
                lines.append(now_iso)
                sent = send_alert("\n".join(lines))
                low_last_alert_at = now_iso if sent else low_last_alert_at

            state[acc]["low_balance"] = {
                "is_low": True,
                "balance_brl": balance_brl,
                "last_alert_at": low_last_alert_at,
            }
        else:
            if was_low:
                send_alert(
                    f"✅ *SALDO NORMALIZADO*\n{name} — saldo recarregado.\n{now_iso}"
                )
            state[acc]["low_balance"] = {
                "is_low": False,
                "balance_brl": balance_brl,
                "last_alert_at": None,
            }

        # Campanha restringida/reprovada (DISAPPROVED, WITH_ISSUES) — trava
        # de entrega por política, independente do status da conta em si.
        # Lista todas as campanhas (chamada pesada, com paginação), então só
        # roda 1x/hora (no ciclo de minuto 0) em vez de nos 4 ciclos de 15min
        # — não é tão urgente quanto conta parada/saldo baixo.
        if now.minute == 0:
            try:
                camp_meta = fetch_campaign_meta(acc, token)
            except Exception as e:
                log(f"{acc}: falha ao checar status de campanhas ({e})")
                camp_meta = {}

            current_restricted = {
                cid: (c.get("name") or cid)
                for cid, c in camp_meta.items()
                if (c.get("effective_status") or "").upper() in RESTRICTED_CAMPAIGN_STATUSES
            }
            prev_restricted = prev.get("restricted_campaigns", {})
            prev_ids = set(prev_restricted.get("ids") or [])
            current_ids = set(current_restricted)
            new_restricted = current_ids - prev_ids
            recovered_restricted = prev_ids - current_ids
            restricted_last_alert_at = prev_restricted.get("last_alert_at")

            should_alert_restricted = bool(new_restricted)
            if not should_alert_restricted and current_ids and restricted_last_alert_at:
                last_dt = datetime.fromisoformat(restricted_last_alert_at)
                if (now - last_dt).total_seconds() > REALERT_COOLDOWN_HOURS * 3600:
                    should_alert_restricted = True

            if should_alert_restricted:
                lines = [
                    "🚫 *ALERTA — CAMPANHA RESTRINGIDA/REPROVADA*",
                    f"Conta: {name}",
                ]
                for cid in sorted(current_ids):
                    status = (camp_meta.get(cid, {}).get("effective_status") or "").upper()
                    lines.append(f"• {current_restricted[cid]} — {status}")
                lines.append("")
                lines.append("Verifique no Gerenciador de Anúncios (Qualidade/Revisão de política).")
                lines.append(now_iso)
                sent = send_alert("\n".join(lines))
                if sent:
                    restricted_last_alert_at = now_iso

            if recovered_restricted:
                prev_names = prev_restricted.get("names") or {}
                names = [prev_names.get(cid, cid) for cid in recovered_restricted]
                send_alert(
                    "✅ *CAMPANHA(S) NORMALIZADA(S)*\n"
                    f"Conta: {name}\n"
                    + "\n".join(f"• {n}" for n in names)
                    + f"\n{now_iso}"
                )

            state[acc]["restricted_campaigns"] = {
                "ids": sorted(current_ids),
                "names": current_restricted,
                "last_alert_at": restricted_last_alert_at,
            }
        elif "restricted_campaigns" in prev:
            state[acc]["restricted_campaigns"] = prev["restricted_campaigns"]

    # Uma mensagem só pro lote (evita 15 alertas separados se a rede cair
    # geral, como aconteceu em 2026-08-27) — cada conta guarda seu próprio
    # last_alert_at pra manter o cooldown individual daqui pra frente.
    if health_degraded:
        lines = [
            "⚠️ *MONITOR SEM CONSEGUIR CHECAR*",
            f"{len(health_degraded)} conta(s) sem checagem bem-sucedida há {FETCH_FAIL_ALERT_THRESHOLD}+ ciclos "
            f"(~{FETCH_FAIL_ALERT_THRESHOLD * 15}min), mesmo com retry:",
        ]
        for acc, name in health_degraded:
            lines.append(f"• {name}")
        lines.append("")
        lines.append("Pode ser instabilidade de rede/DNS local ou token expirado — confira a internet da máquina e o log.")
        lines.append(now_iso)
        sent = send_alert("\n".join(lines))
        if sent:
            for acc, _ in health_degraded:
                state[acc]["fetch_health"]["last_alert_at"] = now_iso

    if health_recovered:
        lines = ["✅ *MONITOR NORMALIZADO*", "Voltou a conseguir checar:"]
        for acc, name in health_recovered:
            lines.append(f"• {name}")
        lines.append(now_iso)
        send_alert("\n".join(lines))

    save_state(state)

    # Teto de gasto diário: antes só era checado 1x/dia (21:30). Rodar junto
    # aqui pega o estouro na hora (dentro da janela de 15min), em vez de
    # esperar o fim do dia — reusa o paid-traffic-1d.json já coletado pelo
    # fetch horário (sem chamada nova à Graph API) e o próprio state file de
    # budget_cap_alert_state.json dedupe entre esta checagem e a das 21:30.
    try:
        check_budget_caps()
    except Exception as e:
        log(f"check_daily_budget_cap falhou: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
