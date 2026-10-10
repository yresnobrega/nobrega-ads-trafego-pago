#!/usr/bin/env python3
"""
fetch_metrics.py — coleta métricas Meta ADS adaptadas ao perfil do aluno.

Como funciona:
- Lê ~/.operacao-ia/config/meta_perfil.json (KPIs, metas, janelas, conta)
- Lê access token do MCP oficial Meta em ~/.claude.json
- Para cada janela, chama Meta Graph API v21.0 (level=ad) — header Authorization
- Faz 2ª query para campaigns (effective_status + budget atual)
- Armazena raw counters (spend, leads, purchases, msgs, impressions, clicks, ...)
- KPIs derivados (CPL, CPA, ROAS, CTR, CPM, CPC, custo/msg) calculados em cada nível
  a partir dos raws — agregação correta (sum/sum), nunca média ponderada de razões
- Aplica decide() por ad usando limiares do perfil
- Grava ~/.operacao-ia/dashboards/paid-traffic-{N}d.json

Modos:
  --check   : só valida config + token presente, não chama API
  (default) : roda fetch — exit code != 0 se qualquer janela falhar
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

OPERACAO = Path.home() / ".operacao-ia"
PERFIL = OPERACAO / "config" / "meta_perfil.json"
META_ENV = OPERACAO / "config" / "meta.env"
DASH_DIR = OPERACAO / "dashboards"
LOG = OPERACAO / "logs" / "meta-fetch.log"
LOCK_FILE = OPERACAO / "config" / "fetch_metrics.lock"

GRAPH_BASE = "https://graph.facebook.com/v21.0"
BRT = timezone(timedelta(hours=-3))

# Erros transitórios da Meta ("Service temporarily unavailable", "unknown
# error") pegam sempre as mesmas 1-2 contas em runs consecutivos — testado
# isoladamente, a mesma query funciona de primeira minutos depois. Retry
# curto absorve isso sem precisar rodar tudo de novo na mão.
INSIGHTS_RETRY_ATTEMPTS = 3
INSIGHTS_RETRY_DELAY_SECONDS = 8

# Mapeamento KPI → lista de action_types aceitos (variantes Meta).
# Soma todos os tipos da lista (ex: lead = sum(lead, onsite_lead, leadgen.other)).
ACTION_TYPES = {
    "leads": [
        "lead",
        "onsite_conversion.lead_grouped",
        "leadgen.other",
        "offsite_conversion.fb_pixel_lead",
    ],
    "purchases": [
        "purchase",
        "onsite_conversion.purchase",
        "offsite_conversion.fb_pixel_purchase",
        "omni_purchase",
    ],
    # Só messaging_conversation_started_7d — é o único action_type que bate com
    # a coluna "Resultados" do Ads Manager pra campanhas de mensagem. Somar
    # messaging_first_reply junto (como era antes) infla a contagem em ~1,7x
    # e deixa o custo por mensagem artificialmente mais barato do que é de
    # verdade — bug encontrado comparando com o Ads Manager em 2026-09-19
    # (Premium Store: API dava 36 "mensagens" pra um adset que o Ads Manager
    # mostrava 21 resultados a R$4,40; sem o first_reply bate 21 exato).
    "messages": [
        "onsite_conversion.messaging_conversation_started_7d",
    ],
    "installs": [
        "mobile_app_install",
        "app_install",
    ],
}

PURCHASE_VALUE_TYPES = ACTION_TYPES["purchases"]

# Contadores genéricos usados só por campanhas de engajamento/visita (não têm
# lead/compra/mensagem pra medir) — pareados com OPTIMIZATION_GOAL_RESULT_KEY.
RESULT_ACTION_TYPES = {
    "post_engagement": ["post_engagement"],
    "page_engagement": ["page_engagement"],
    "link_click": ["link_click"],
    "landing_page_view": ["landing_page_view"],
}

# Ads Manager calcula "Custo por resultado" usando a action_type que bate com
# o optimization_goal do conjunto de anúncios — não um agregado genérico tipo
# post_engagement pra tudo (isso inflava o volume e mascarava o custo real).
# PROFILE_AND_PAGE_ENGAGEMENT não tem action_type próprio; page_engagement é
# o mais próximo disponível na Insights API.
OPTIMIZATION_GOAL_RESULT_KEY = {
    "POST_ENGAGEMENT": "post_engagement",
    "PAGE_ENGAGEMENT": "page_engagement",
    "PROFILE_AND_PAGE_ENGAGEMENT": "page_engagement",
    "LINK_CLICKS": "link_click",
    "LANDING_PAGE_VIEWS": "landing_page_view",
}


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(BRT).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}\n"
    try:
        with LOG.open("a") as f:
            f.write(line)
    except Exception:
        pass


def read_env(key):
    if not META_ENV.exists():
        return ""
    for line in META_ENV.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def read_token():
    """Lê access_token de meta.env (fonte de verdade) ou env var."""
    return read_env("META_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN") or None


def graph_get(path, params, token, *, page_url=None):
    """GET no Graph API com Authorization: Bearer (não na query string)."""
    headers = {
        "User-Agent": "zx-control-trafego-pago/0.1.1",
        "Authorization": f"Bearer {token}",
    }
    if page_url:
        url = page_url
        # Se URL paginada já carrega access_token na query (vinda do .next),
        # remove pra evitar leak duplicado/conflito.
        if "access_token=" in url:
            from urllib.parse import urlparse, urlencode as ue, parse_qsl, urlunparse
            u = urlparse(url)
            qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "access_token"]
            url = urlunparse(u._replace(query=ue(qs)))
    else:
        url = f"{GRAPH_BASE}/{path.lstrip('/')}?{urlencode(params)}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Graph API {e.code}: {body[:300]}")


def get_action_count(actions, action_type_list):
    if not actions:
        return 0.0
    total = 0.0
    for a in actions:
        if a.get("action_type") in action_type_list:
            try:
                total += float(a.get("value", 0) or 0)
            except (ValueError, TypeError):
                pass
    return total


def window_date_range(days, today):
    # days=0 ("Ontem") é 1 dia fixo e fechado — nunca muda ao longo do dia,
    # diferente de "Hoje" que vai enchendo em tempo real.
    if days == 0:
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    # days=1 ("Hoje") inclui o dia corrente — é monitoramento em tempo real,
    # equivalente ao date_preset "today" do Meta.
    # days>1 EXCLUI hoje (termina ontem), pra bater com o date_preset "last_Nd"
    # que o próprio Ads Manager usa — senão a janela de 30d, por exemplo, fica
    # sempre 1 dia deslocada e nunca bate com o que o aluno vê no painel do Meta.
    until = today
    if days > 1:
        until -= timedelta(days=1)
    since = until - timedelta(days=days - 1)
    return since, until


INSIGHTS_FIELDS = ",".join([
    "ad_id", "ad_name", "adset_id", "adset_name",
    "campaign_id", "campaign_name", "objective",
    "spend", "impressions", "clicks", "reach", "frequency",
    "cpm", "ctr", "cpc",
    "actions", "action_values",
    "date_start",
])


def fetch_insights_range(account_id, since_date, until_date, token):
    """Busca insights por ad com quebra diária (time_increment=1) num único
    intervalo largo o bastante pra cobrir todas as janelas configuradas —
    janela maior sempre contém os dias das menores, então 1 chamada (+
    paginação) substitui N chamadas separadas (uma por janela)."""
    params = {
        "level": "ad",
        "fields": INSIGHTS_FIELDS,
        "time_range": json.dumps({"since": since_date.isoformat(), "until": until_date.isoformat()}),
        "time_increment": 1,
        # SEM filtro de effective_status: status é o estado ATUAL do anúncio, não
        # o de quando ele gastou. Filtrar por ACTIVE/PAUSED escondia gasto real de
        # qualquer anúncio arquivado/excluído depois — já vimos janela de 30d
        # subestimar uma conta em quase 80% por causa disso.
        "limit": 500,
    }
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    rows = []
    page = graph_get(f"{account}/insights", params, token)
    rows.extend(page.get("data", []))
    while page.get("paging", {}).get("next"):
        page = graph_get(None, None, token, page_url=page["paging"]["next"])
        rows.extend(page.get("data", []))
    return rows


# Testado ao vivo em 2026-08-31: pra 2 contas específicas (as com mais
# histórico de anúncios), o pedido de 30d inteiro falha quase sempre (erro
# transitório da Meta — "unknown error"/"service unavailable"), mas o MESMO
# intervalo pedido em pedaços de até 14d responde rápido e sem erro toda vez.
# Não é coincidência de timing: 4/4 tentativas de 30d falharam, 4/4 de 14d
# funcionaram. Usado como fallback só quando o pedido do intervalo inteiro
# esgota as tentativas normais — assim as contas saudáveis continuam com 1
# chamada só, e só as problemáticas pagam o custo de chamadas extras.
INSIGHTS_CHUNK_DAYS = 14


def fetch_insights_range_chunked(account_id, since_date, until_date, token, chunk_days=INSIGHTS_CHUNK_DAYS):
    # Retry por pedaço: sem isso, 1 pedaço transitoriamente ruim (raro, mas
    # acontece) derruba o fallback inteiro de novo — o mesmo problema que
    # ele existe pra resolver. Cada pedaço é pequeno/rápido, então vale a
    # pena tentar de novo em vez de desistir da conta na primeira falha.
    rows = []
    chunk_start = since_date
    while chunk_start <= until_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), until_date)
        last_error = None
        for attempt in range(1, INSIGHTS_RETRY_ATTEMPTS + 1):
            try:
                rows.extend(fetch_insights_range(account_id, chunk_start, chunk_end, token))
                last_error = None
                break
            except Exception as e:
                last_error = e
                log(f"{account_id}: pedaço {chunk_start}..{chunk_end} ERROR (tentativa {attempt}/{INSIGHTS_RETRY_ATTEMPTS}) {e}")
                if attempt < INSIGHTS_RETRY_ATTEMPTS:
                    time.sleep(INSIGHTS_RETRY_DELAY_SECONDS)
        if last_error is not None:
            raise last_error
        chunk_start = chunk_end + timedelta(days=1)
    return rows


def filter_rows_for_window(rows, since_date, until_date):
    since_s, until_s = since_date.isoformat(), until_date.isoformat()
    return [r for r in rows if since_s <= (r.get("date_start") or "") <= until_s]


ACCOUNT_STATUS_LABELS = {
    1: "ATIVA",
    2: "DESABILITADA",
    # Meta code 3 = "UNSETTLED" — testado ao vivo: a conta de fato PARA de
    # entregar (é o mesmo "is_stopped" que dispara o alerta de CONTA PARADA
    # em check_account_alerts.py). Rótulos antigos ("NÃO CONFIGURADA",
    # "COBRANÇA PENDENTE") sugeriam algo mais brando do que realmente é.
    3: "CONTA PARADA",
    7: "PENDENTE DE REVISÃO",
    8: "PENDENTE DE FECHAMENTO",
    9: "EM PERÍODO DE CARÊNCIA",
    100: "PENDENTE DE ATIVAÇÃO",
    101: "EM FECHAMENTO",
}


def fetch_account_status(account_id, token):
    """Pega status da conta (ativa/desabilitada/etc) — necessário pro relatório saber se a conta está rodando.

    Também traz balance/spend_cap/funding_source_details: em contas "Saldo
    disponível" (pré-pagas, comuns em boleto/PIX no Brasil), `balance` é o
    saldo restante de fato — pode zerar e parar a entrega sem o
    account_status/disable_reason mudarem. Em contas de cartão, `balance` é
    valor já gasto e ainda não cobrado (não é saldo disponível).
    """
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    fields = "account_status,disable_reason,name,balance,spend_cap,amount_spent,funding_source_details"
    data = graph_get(account, {"fields": fields}, token)
    code = data.get("account_status")
    return {
        "account_status": code,
        "account_status_label": ACCOUNT_STATUS_LABELS.get(code, f"DESCONHECIDO ({code})"),
        "disable_reason": data.get("disable_reason"),
        "account_name": data.get("name"),
        "balance": data.get("balance"),
        "spend_cap": data.get("spend_cap"),
        "amount_spent": data.get("amount_spent"),
        "funding_source_details": data.get("funding_source_details"),
    }


def fetch_campaign_meta(account_id, token):
    """Pega effective_status + budget de cada campanha (necessário pro budget-optimizer)."""
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    fields = "id,name,effective_status,daily_budget,lifetime_budget,bid_strategy,status,created_time"
    params = {"fields": fields, "limit": 500}
    out = {}
    page = graph_get(f"{account}/campaigns", params, token)
    for c in page.get("data", []):
        out[c["id"]] = c
    while page.get("paging", {}).get("next"):
        page = graph_get(None, None, token, page_url=page["paging"]["next"])
        for c in page.get("data", []):
            out[c["id"]] = c
    return out


def fetch_adset_optimization_goals(account_id, token):
    """Pega optimization_goal de cada conjunto — diz qual action_type é o
    'resultado' real da campanha (o mesmo que o Gerenciador de Anúncios usa
    pra calcular 'Custo por resultado'), necessário pra campanhas de
    engajamento/visita que não têm lead/compra/mensagem pra medir."""
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    params = {"fields": "id,optimization_goal", "limit": 500}
    out = {}
    page = graph_get(f"{account}/adsets", params, token)
    for a in page.get("data", []):
        out[a["id"]] = a.get("optimization_goal")
    while page.get("paging", {}).get("next"):
        page = graph_get(None, None, token, page_url=page["paging"]["next"])
        for a in page.get("data", []):
            out[a["id"]] = a.get("optimization_goal")
    return out


def row_raws(row):
    """Extrai counters raw de uma linha de insight (ad)."""
    raws = {
        "spend": float(row.get("spend") or 0),
        "impressions": int(row.get("impressions") or 0),
        "clicks": int(row.get("clicks") or 0),
        "reach": int(row.get("reach") or 0),
        "leads": get_action_count(row.get("actions"), ACTION_TYPES["leads"]),
        "purchases": get_action_count(row.get("actions"), ACTION_TYPES["purchases"]),
        "purchase_value": get_action_count(row.get("action_values"), PURCHASE_VALUE_TYPES),
        "messages": get_action_count(row.get("actions"), ACTION_TYPES["messages"]),
        "installs": get_action_count(row.get("actions"), ACTION_TYPES["installs"]),
    }
    for key, types in RESULT_ACTION_TYPES.items():
        raws[key] = get_action_count(row.get("actions"), types)
    return raws


def merge_raws(target, src):
    for k, v in src.items():
        target[k] = target.get(k, 0) + v


def calc_kpi(raws, kpi_key):
    spend = raws.get("spend", 0)
    impressions = raws.get("impressions", 0)
    clicks = raws.get("clicks", 0)

    if kpi_key == "cpl":
        leads = raws.get("leads", 0)
        return (spend / leads) if leads > 0 else None
    if kpi_key == "cpa":
        purchases = raws.get("purchases", 0)
        return (spend / purchases) if purchases > 0 else None
    if kpi_key == "roas":
        return (raws.get("purchase_value", 0) / spend) if spend > 0 else None
    if kpi_key == "cost_per_msg":
        msgs = raws.get("messages", 0)
        return (spend / msgs) if msgs > 0 else None
    if kpi_key == "cost_per_install":
        installs = raws.get("installs", 0)
        return (spend / installs) if installs > 0 else None
    if kpi_key == "cpm":
        return (spend / impressions * 1000) if impressions > 0 else None
    if kpi_key == "ctr":
        return (clicks / impressions * 100) if impressions > 0 else None
    if kpi_key == "cpc":
        return (spend / clicks) if clicks > 0 else None
    if kpi_key == "frequency":
        reach = raws.get("reach", 0)
        return (impressions / reach) if reach > 0 else None
    return None


def decide(value, kpi_meta, spend):
    target = kpi_meta.get("target") or 0
    if target <= 0:
        return "KEEP", "target inválido"
    scale_at = kpi_meta.get("scale_at", 0.8)
    kill_at = kpi_meta.get("kill_at", 1.5)
    better = kpi_meta.get("better", "lower")
    min_spend = max(target * 1.2, 30)

    if value is None:
        if spend >= target * 3:
            return "KILL", f"sem conversões após R${spend:.2f} de gasto"
        return "KEEP-amostra", "amostra insuficiente"

    if spend < min_spend:
        return "KEEP-amostra", f"spend R${spend:.2f} < min R${min_spend:.2f}"

    if better == "lower":
        if value <= target * scale_at:
            return "SCALE", f"{kpi_meta['key']} {value:.2f} ≤ {target*scale_at:.2f} ({(target-value)/target*100:.0f}% abaixo da meta)"
        if value > target * kill_at:
            return "KILL", f"{kpi_meta['key']} {value:.2f} > {target*kill_at:.2f} ({(value-target)/target*100:.0f}% acima da meta)"
        return "KEEP", f"{kpi_meta['key']} {value:.2f} dentro da banda"
    else:
        if value >= target * (2 - scale_at):
            return "SCALE", f"{kpi_meta['key']} {value:.2f} ≥ {target*(2-scale_at):.2f} (acima da meta)"
        if value < target * (2 - kill_at):
            return "KILL", f"{kpi_meta['key']} {value:.2f} < {target*(2-kill_at):.2f} (muito abaixo)"
        return "KEEP", f"{kpi_meta['key']} {value:.2f} dentro da banda"


def status_color(value, kpi_meta):
    """Verde até a meta, vermelho a partir do limiar de kill_at, amarelo no
    meio — mesma banda que decide() usa pra SCALE/KEEP/KILL, então o card do
    KPI nunca discorda do veredito por campanha."""
    if value is None:
        return "gray"
    target = kpi_meta.get("target") or 0
    if target <= 0:
        return "gray"
    better = kpi_meta.get("better", "lower")
    kill_at = kpi_meta.get("kill_at", 1.5)
    if better == "lower":
        if value <= target: return "green"
        if value <= target * kill_at: return "yellow"
        return "red"
    else:
        if value >= target: return "green"
        if value >= target * (2 - kill_at): return "yellow"
        return "red"


DEFAULT_RESULT_KPI_META = {
    "key": "cost_per_result",
    "label": "Custo por resultado",
    "target": 0.5,
    "better": "lower",
    "format": "currency",
    "scale_at": 0.8,
    "kill_at": 1.5,
}

# Campanha de mensagem de verdade segue essa convenção de nome: tag "ENG|"
# (engajamento cujo CTA de verdade é mensagem — com ou sem canal explícito
# no nome, ex: "ENG| COMPRAMOS SEU IPHONE" tanto quanto "ENG|WHATSAPP|...")
# EXCETO quando também vem com "|POST|"/"|VP|" (engajamento puro/visita de
# perfil, sem CTA de mensagem apesar de também ser tag ENG). "whatsapp"
# sozinho no nome NÃO basta (ex: campanha de TRÁFEGO/SITE que só menciona
# "WhatsApp" na oferta não é medida por mensagem). Qualquer campanha fora
# desse critério (tráfego, alcance, site, |POST|, |VP|) mede pelo
# optimization_goal do conjunto — não faz sentido custo/mensagem pra quem
# não otimiza pra isso.
#
# "mensagem" é o mesmo tipo de campanha com convenção de nome de um gestor
# anterior (conta IMPORTS SOARES, ex: "[08/07] MENSAGEM - POCO X8 PRO MAX") —
# confirmado pelo aluno em 2026-09-21 que é equivalente a ENG|, então entra
# como gatilho alternativo com a mesma exceção de |POST|/|VP|.
NON_MESSAGE_ENGAGEMENT_TAGS = ("|post|", "|vp|")
MESSAGE_CAMPAIGN_TRIGGERS = ("eng|", "mensagem")


def is_message_campaign(name_lower):
    if any(tag in name_lower for tag in NON_MESSAGE_ENGAGEMENT_TAGS):
        return False
    return any(trigger in name_lower for trigger in MESSAGE_CAMPAIGN_TRIGGERS)


def aggregate(rows, perfil, campaign_meta, account_id=None, account_name=None, adset_goals=None):
    adset_goals = adset_goals or {}
    primary_kpi = perfil["primary_kpi"]
    kpi_meta_map = {k["key"]: k for k in perfil["kpis"]}
    primary_meta = kpi_meta_map[primary_kpi]
    result_meta = perfil.get("decide_result_kpi") or DEFAULT_RESULT_KPI_META

    # Agrupa por ad_id primeiro: a coleta consolidada quebra por dia
    # (time_increment=1), então o mesmo anúncio chega em várias linhas — sem
    # juntar antes, ele apareceria duplicado (1 vez por dia) com métricas e
    # decide() de só aquele dia, em vez do total da janela.
    by_ad_id = {}
    for row in rows:
        ad_id = row.get("ad_id")
        entry = by_ad_id.get(ad_id)
        if entry is None:
            entry = dict(row)
            entry["raws"] = row_raws(row)
            by_ad_id[ad_id] = entry
        else:
            merge_raws(entry["raws"], row_raws(row))
    ad_rows = list(by_ad_id.values())

    by_camp = {}
    for row in ad_rows:
        cid = row.get("campaign_id")
        asid = row.get("adset_id")
        raws = row["raws"]
        campaign_name = (row.get("campaign_name") or "")
        name_lower = campaign_name.lower()

        ad_metrics = {k["key"]: calc_kpi(raws, k["key"]) for k in perfil["kpis"]}

        if is_message_campaign(name_lower):
            # ENG|WHATSAPP / ENG|INSTAGRAM — campanha de engajamento cujo CTA
            # de verdade é mensagem; mede pelo KPI primário (custo por
            # mensagem), não pelo objective bruto (aparece como
            # OUTCOME_ENGAGEMENT).
            decide_v, decide_r = decide(ad_metrics.get(primary_kpi), primary_meta, raws["spend"])
        else:
            # Qualquer outro objetivo (tráfego, alcance, site, |POST|, |VP|)
            # — usa o optimization_goal do conjunto pra achar o "resultado"
            # real (igual ao Gerenciador de Anúncios), com fallback pra
            # validação por CTR quando o goal não tem action_type mapeado.
            goal = adset_goals.get(asid)
            result_key = OPTIMIZATION_GOAL_RESULT_KEY.get(goal)
            result_count = raws.get(result_key, 0) if result_key else 0
            cost_per_result = (raws["spend"] / result_count) if result_count > 0 else None

            if result_key:
                ad_metrics["cost_per_result"] = cost_per_result
                decide_v, decide_r = decide(cost_per_result, result_meta, raws["spend"])
            else:
                ctr = calc_kpi(raws, "ctr")
                decide_v = "VALIDATION"
                decide_r = (
                    f"validação de conteúdo — CTR {ctr:.2f}%" if ctr is not None
                    else "validação de conteúdo — sem cliques ainda"
                )

        ad = {
            "ad_id": row.get("ad_id"),
            "ad_name": row.get("ad_name", ""),
            "raws": raws,
            "spend": raws["spend"],
            "metrics": ad_metrics,
            "decide": decide_v,
            "decide_reason": decide_r,
        }

        camp = by_camp.setdefault(cid, {
            "campaign_id": cid,
            "name": row.get("campaign_name", ""),
            "objective": row.get("objective", ""),
            "raws": {},
            "adsets": {},
        })
        merge_raws(camp["raws"], raws)

        adset = camp["adsets"].setdefault(asid, {
            "adset_id": asid,
            "name": row.get("adset_name", ""),
            "raws": {},
            "ads": [],
        })
        merge_raws(adset["raws"], raws)
        adset["ads"].append(ad)

    campaigns = []
    account_raws = {}
    for cid, camp in by_camp.items():
        merge_raws(account_raws, camp["raws"])
        meta = campaign_meta.get(cid, {})
        adset_list = []
        all_decisions = []
        for asid, adset in camp["adsets"].items():
            adset["spend"] = adset["raws"]["spend"]
            adset["metrics"] = {k["key"]: calc_kpi(adset["raws"], k["key"]) for k in perfil["kpis"]}
            adset_list.append(adset)
            all_decisions.extend(a["decide"] for a in adset["ads"])

        camp["spend"] = camp["raws"]["spend"]
        camp["metrics"] = {k["key"]: calc_kpi(camp["raws"], k["key"]) for k in perfil["kpis"]}

        # Qual métrica decidiu o veredito desta campanha — mesma regra de
        # roteamento usada por ad (ENG|WHATSAPP/ENG|INSTAGRAM -> KPI
        # primário, qualquer outro objetivo -> custo por resultado), só que
        # agregada pro nível de campanha, pra o relatório poder mostrar
        # "quanto tá" ao lado do veredito.
        camp_name_lower = camp["name"].lower()
        if is_message_campaign(camp_name_lower):
            camp["decide_metric_key"] = primary_kpi
            camp["decide_metric_label"] = primary_meta.get("label")
        else:
            camp_result_key = None
            for adset in adset_list:
                goal = adset_goals.get(adset.get("adset_id"))
                rk = OPTIMIZATION_GOAL_RESULT_KEY.get(goal)
                if rk:
                    camp_result_key = rk
                    break
            if camp_result_key:
                count = camp["raws"].get(camp_result_key, 0)
                camp["metrics"]["cost_per_result"] = (
                    camp["raws"]["spend"] / count if count > 0 else None
                )
                camp["decide_metric_key"] = "cost_per_result"
                camp["decide_metric_label"] = result_meta.get("label")
            else:
                camp["decide_metric_key"] = None
                camp["decide_metric_label"] = None

        # decide() agregado: pior decisão entre os ads
        if "KILL" in all_decisions:
            camp["decide"] = "KILL"
        elif "SCALE" in all_decisions:
            camp["decide"] = "SCALE"
        elif "KEEP" in all_decisions:
            camp["decide"] = "KEEP"
        elif "KEEP-amostra" in all_decisions:
            camp["decide"] = "KEEP-amostra"
        else:
            camp["decide"] = "VALIDATION"
        camp["effective_status"] = meta.get("effective_status")
        camp["daily_budget"]    = int(meta.get("daily_budget") or 0) / 100 if meta.get("daily_budget") else None
        camp["lifetime_budget"] = int(meta.get("lifetime_budget") or 0) / 100 if meta.get("lifetime_budget") else None
        camp["bid_strategy"]    = meta.get("bid_strategy")
        camp["created_time"]    = meta.get("created_time")
        camp["account_id"] = account_id
        camp["account_name"] = account_name
        camp["adsets"] = adset_list
        campaigns.append(camp)

    return campaigns, account_raws


def compute_2day_alert_campaign_ids(daily_rows, perfil, campaign_meta, account_id, account_name, adset_goals, today):
    """Campanha com resultado ruim (decide=KILL) nos ÚLTIMOS 2 DIAS CORRIDOS
    seguidos (ontem E anteontem, os 2 únicos dias fechados/completos — hoje
    fica de fora por ainda estar em andamento) vira "Alerta" no dashboard,
    mais grave que um "Ruim" pontual de um dia só. Reaproveita aggregate()
    rodando ele isolado em cada um dos 2 dias (mesmo roteamento whatsapp/
    |POST|/|VP| que já existe, sem duplicar lógica) em vez de reconstruir o
    veredito diário na mão."""
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    bad_by_day = {}
    for day in (day_before, yesterday):
        day_rows = filter_rows_for_window(daily_rows, day, day)
        campaigns, _ = aggregate(day_rows, perfil, campaign_meta, account_id, account_name, adset_goals)
        bad_by_day[day] = {c["campaign_id"] for c in campaigns if c.get("decide") == "KILL"}
    return bad_by_day[day_before] & bad_by_day[yesterday]


def compute_kpis_summary(perfil, account_raws):
    kpis_summary = {}
    for k in perfil["kpis"]:
        v = calc_kpi(account_raws, k["key"])
        target = k.get("target") or 0
        delta = ((v - target) / target * 100) if (v is not None and target > 0) else None
        kpis_summary[k["key"]] = {
            "value": (round(v, 4) if v is not None else None),
            "target": target,
            "delta_pct": (round(delta, 1) if delta is not None else None),
            "status": status_color(v, k),
        }
    return kpis_summary


def resolve_account_ids(perfil):
    """Suporta 1 conta (ad_account_id, legado) ou N contas (ad_account_ids)."""
    ids = perfil.get("ad_account_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids if i and "XXXXX" not in str(i)]
    single = perfil.get("ad_account_id") or read_env("META_AD_ACCOUNT_ID")
    return [single] if single and "XXXXX" not in single else []




def parse_meta_timestamp(ts):
    """Parseia timestamp Meta tipo '2026-09-01T12:34:56+0000' (activities,
    last_fired_time) — usa strptime em vez de fromisoformat porque o offset
    sem dois-pontos ('+0000') só é aceito pelo fromisoformat a partir do
    Python 3.11."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def fetch_account_activities(account_id, token, since_date, until_date, limit=100, max_pages=5):
    """Log de atividades da conta (act_<id>/activities) — mudanças de
    orçamento, público, criativo, status etc. Usado pra checar se uma queda
    de performance coincide com uma edição manual recente, antes de culpar
    mercado/público (causa #4 do framework de diagnóstico)."""
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    since_ts = int(datetime.combine(since_date, datetime.min.time(), tzinfo=BRT).timestamp())
    until_ts = int(datetime.combine(until_date, datetime.max.time().replace(microsecond=0), tzinfo=BRT).timestamp())
    params = {
        "fields": "event_type,event_time,translated_event_type,object_name,object_type,extra_data",
        "since": since_ts,
        "until": until_ts,
        "limit": limit,
    }
    items = []
    page = graph_get(f"{account}/activities", params, token)
    items.extend(page.get("data", []))
    pages_fetched = 1
    while page.get("paging", {}).get("next") and pages_fetched < max_pages:
        page = graph_get(None, None, token, page_url=page["paging"]["next"])
        items.extend(page.get("data", []))
        pages_fetched += 1
    return items


def summarize_activity(a):
    extra = {}
    raw_extra = a.get("extra_data")
    if raw_extra:
        try:
            extra = json.loads(raw_extra)
        except Exception:
            extra = {}
    return {
        "event_time": a.get("event_time"),
        "event_type": a.get("event_type"),
        "translated_event_type": a.get("translated_event_type"),
        "object_name": a.get("object_name"),
        "object_type": a.get("object_type"),
        "extra_data": extra,
    }


def filter_activities_for_window(items, since_date, until_date):
    out = []
    for it in items:
        dt = parse_meta_timestamp(it.get("event_time"))
        if dt is None:
            continue
        d = dt.astimezone(BRT).date()
        if since_date <= d <= until_date:
            out.append(it)
    return out


# Janela de "saudável" pro pixel: fire nas últimas 48h. Acima disso e até 7
# dias vira "degradado" (ainda dispara, mas devagar demais pra confiar 100%
# no dado de conversão); acima de 7 dias ou nunca disparou vira "não
# saudável" — mesmo corte usado como sinal primário de tracking quebrado no
# framework de diagnóstico (descartar antes das outras 5 causas).
PIXEL_HEALTHY_HOURS = 48
PIXEL_DEGRADED_HOURS = 168


def fetch_pixel_health(account_id, token):
    """Localiza os pixels vinculados à conta e classifica saúde pelo
    last_fired_time — não é um diagnóstico completo de CAPI/matching, é um
    sinal rápido e barato (1 chamada) pra descartar tracking obviamente
    quebrado antes de confiar em qualquer veredito de custo por resultado."""
    account = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
    data = graph_get(
        f"{account}/adspixels",
        {"fields": "id,name,last_fired_time,creation_time", "limit": 10},
        token,
    )
    pixels_raw = data.get("data", [])
    now = datetime.now(timezone.utc)
    order = {"UNHEALTHY": 3, "DESCONHECIDO": 2, "DEGRADED": 1, "HEALTHY": 0}
    pixels_out = []
    for p in pixels_raw:
        last_fired = p.get("last_fired_time")
        dt = parse_meta_timestamp(last_fired)
        hours_since_fire = None
        if dt is None:
            status = "UNHEALTHY" if last_fired is None else "DESCONHECIDO"
        else:
            hours_since_fire = (now - dt).total_seconds() / 3600
            if hours_since_fire <= PIXEL_HEALTHY_HOURS:
                status = "HEALTHY"
            elif hours_since_fire <= PIXEL_DEGRADED_HOURS:
                status = "DEGRADED"
            else:
                status = "UNHEALTHY"
        pixels_out.append({
            "pixel_id": p.get("id"),
            "name": p.get("name"),
            "last_fired_time": last_fired,
            "hours_since_last_fire": round(hours_since_fire, 1) if hours_since_fire is not None else None,
            "status": status,
        })
    # Pixel que NUNCA disparou (last_fired_time nulo) é, na prática, quase
    # sempre um dataset órfão/abandonado — não é o pixel em uso de verdade.
    # Se a conta tem outro pixel com histórico real (já disparou alguma vez,
    # mesmo que há tempos), o status geral ignora os órfãos e reflete só os
    # que já provaram estar em uso — senão um pixel morto nunca-usado derruba
    # o status de um pixel saudável só porque a conta acumulou dataset velho.
    # Se TODOS os pixels da conta nunca dispararam, não há pixel "real" pra
    # confiar — aí sim UNHEALTHY reflete a conta inteira.
    ever_fired = [p for p in pixels_out if p["last_fired_time"] is not None]
    scoring_pool = ever_fired if ever_fired else pixels_out
    overall_status = "SEM_PIXEL" if not pixels_out else max(scoring_pool, key=lambda x: order.get(x["status"], 2))["status"]
    return {"overall_status": overall_status, "pixels": pixels_out}


def raws_for_range(rows, since_date, until_date):
    filtered = filter_rows_for_window(rows, since_date, until_date)
    total = {}
    for r in filtered:
        merge_raws(total, row_raws(r))
    return total


def compute_period_over_period(perfil, days, since_date, overall_since, account_ids, fetch_errors, daily_rows):
    """Compara o período atual com o equivalente imediatamente anterior (ex:
    últimos 7d vs os 7d antes desses), reaproveitando os daily rows já
    coletados — só é possível quando o histórico buscado (overall_since)
    cobre 2x a janela; pra janela de 30d isso exigiria dobrar a coleta, então
    fica marcado como indisponível em vez de gerar uma chamada extra."""
    if days < 3:
        return {"available": False, "reason": "comparação de período não se aplica a janelas de 0-1 dia"}
    prev_until = since_date - timedelta(days=1)
    prev_since = prev_until - timedelta(days=days - 1)
    if prev_since < overall_since:
        return {
            "available": False,
            "reason": (
                f"janela de {days}d precisaria de {days * 2}d de histórico pra comparar "
                f"com o período anterior; só foi coletado a partir de {overall_since.isoformat()}"
            ),
        }
    combined_prev = {}
    for acc in account_ids:
        if acc in fetch_errors:
            continue
        merge_raws(combined_prev, raws_for_range(daily_rows.get(acc, []), prev_since, prev_until))
    prev_kpis_summary = compute_kpis_summary(perfil, combined_prev)
    return {
        "available": True,
        "prev_since": prev_since.isoformat(),
        "prev_until": prev_until.isoformat(),
        "prev_spend_total": round(combined_prev.get("spend", 0), 2),
        "prev_kpis_summary": prev_kpis_summary,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Só valida config")
    parser.add_argument("--window", type=int, help="busca só essa janela (dias), em vez de todas do perfil")
    args = parser.parse_args()

    if not PERFIL.exists():
        print("❌ ~/.operacao-ia/config/meta_perfil.json não existe. Rode E3.")
        return 1

    perfil = json.loads(PERFIL.read_text())
    account_ids = resolve_account_ids(perfil)
    if not account_ids:
        print("❌ Nenhuma conta configurada. Rode E2 (OAuth) ou preencha ad_account_ids no perfil.")
        return 1

    token = read_token()
    if not token:
        meta_env_status = "existe" if META_ENV.exists() else "não existe"
        print("❌ Token Meta inválido ou ausente.")
        print()
        print("Diagnóstico:")
        print(f"  - meta.env: {meta_env_status} ({META_ENV})")
        print(f"  - META_ACCESS_TOKEN preenchido: não")
        print()
        print("Pra resolver, rode:")
        print("  python3 setup/setup_meta_oauth.py --renew")
        print()
        print("Ou gere token manual:")
        print("  1. Abra https://business.facebook.com/settings/system-users")
        print("  2. Selecione system user, clique 'Gerar token'")
        print("  3. Permissões: ads_management, ads_read, business_management")
        print("  4. Cole no terminal quando pedir.")
        return 1

    if args.check:
        print(f"✅ OK {len(account_ids)} conta(s) ({', '.join(account_ids)}), {len(perfil['kpis'])} KPIs, janelas {perfil.get('windows', [])}")
        return 0

    # Evita 2 execuções simultâneas (cron horário + clique em "Atualizar" no
    # dashboard coincidindo) — já aconteceu de dobrar as chamadas à API bem
    # na hora que uma conta instável falhava, piorando a chance de erro.
    # O lock some sozinho quando o processo termina (flock morre com o fd).
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️  Outro fetch_metrics.py já está rodando — pulando essa execução pra não duplicar chamadas à API.")
        log("SKIP já tem um fetch_metrics.py rodando")
        # Exit code distinto de 0 (sucesso) e 1/2 (falha real) — quem chama
        # (dashboard_server.py) precisa saber que NADA foi atualizado, senão
        # trata "pulei" como "atualizei com sucesso" e mostra dado velho sem
        # avisar ninguém.
        return 3

    DASH_DIR.mkdir(parents=True, exist_ok=True)
    # args.window is not None (não "if args.window"): 0 ("Ontem") é um valor
    # válido e falsy em Python — "if args.window" trataria --window 0 como se
    # nada tivesse sido passado e cairia pras janelas do perfil por engano.
    windows = [args.window] if args.window is not None else perfil.get("windows", [4, 7, 14, 30])
    log(f"START fetch accounts={account_ids} windows={windows}")

    # Intervalo (today/janela máxima) só pro log de atividades — calculado
    # aqui porque a busca de activities roda dentro do loop de metadados por
    # conta, logo abaixo; today/max_window/overall_since equivalentes são
    # recalculados mais adiante pro fetch de insights — mesmo resultado,
    # sem custo real na duplicação.
    _activities_today = datetime.now(BRT).date()
    _activities_max_window = max(windows + [2]) if windows else 30
    _activities_since, _ = window_date_range(_activities_max_window, _activities_today)
    _activities_until = _activities_today

    # Metadados por conta, buscados uma vez (status + effective_status/budget das campanhas)
    account_aliases = perfil.get("account_aliases", {})
    account_meta = {}
    for acc in account_ids:
        try:
            status_info = fetch_account_status(acc, token)
        except Exception as e:
            print(f"⚠️  Não foi possível obter status da conta {acc}: {e}")
            log(f"account_status ERROR {acc} {e}")
            status_info = {
                "account_status": None,
                "account_status_label": "DESCONHECIDO",
                "disable_reason": None,
                "account_name": None,
            }
        if acc in account_aliases:
            # Nome exibido no relatório pode diferir do nome real da conta no
            # Meta (ex: conta ainda registrada no nome pessoal do dono).
            status_info["account_name"] = account_aliases[acc]
        try:
            campaign_meta = fetch_campaign_meta(acc, token)
        except Exception as e:
            print(f"❌ Falha ao listar campanhas da conta {acc}: {e}")
            log(f"campaign_meta ERROR {acc} {e}")
            return 2
        try:
            adset_goals = fetch_adset_optimization_goals(acc, token)
        except Exception as e:
            # Não bloqueia o fetch — só perde o "custo por resultado" de
            # campanhas de engajamento/visita, que cai pra validação por CTR.
            print(f"⚠️  Não foi possível obter optimization_goal da conta {acc}: {e}")
            log(f"adset_goals ERROR {acc} {e}")
            adset_goals = {}
        try:
            activities_raw = fetch_account_activities(acc, token, _activities_since, _activities_until)
        except Exception as e:
            # Não bloqueia o fetch — só perde o log de mudanças recentes
            # (orçamento/público/criativo) usado no diagnóstico de causa.
            print(f"⚠️  Não foi possível obter log de atividades da conta {acc}: {e}")
            log(f"activities ERROR {acc} {e}")
            activities_raw = []
        try:
            pixel_health = fetch_pixel_health(acc, token)
        except Exception as e:
            # Não bloqueia o fetch — só perde o diagnóstico de saúde do pixel/CAPI.
            print(f"⚠️  Não foi possível obter saúde do pixel da conta {acc}: {e}")
            log(f"pixel_health ERROR {acc} {e}")
            pixel_health = {"overall_status": "DESCONHECIDO", "pixels": []}
        account_meta[acc] = {
            "status_info": status_info,
            "campaign_meta": campaign_meta,
            "adset_goals": adset_goals,
            "activities_raw": activities_raw,
            "pixel_health": pixel_health,
        }

    # Coleta consolidada: 1 chamada (+ paginação) por conta cobrindo a maior
    # janela configurada, com quebra diária — troca N chamadas por janela por
    # 1 só, já que a janela de 30d contém os dias de todas as menores. Também
    # isola falhas por conta em vez de por janela (antes, 1 conta falhando
    # derrubava a janela inteira pras outras 18 contas juntas).
    today = datetime.now(BRT).date()
    # +[2] garante no mínimo 2 dias corridos de histórico mesmo quando só uma
    # janela pequena é pedida (--window 0/1) — precisa disso pro alerta de
    # "ruim 2 dias seguidos" (compute_2day_alert_campaign_ids) mais abaixo.
    max_window = max(windows + [2]) if windows else 30
    overall_since, _ = window_date_range(max_window, today)
    overall_until = today
    daily_rows = {}
    fetch_errors = {}
    for acc in account_ids:
        last_error = None
        for attempt in range(1, INSIGHTS_RETRY_ATTEMPTS + 1):
            try:
                daily_rows[acc] = fetch_insights_range(acc, overall_since, overall_until, token)
                last_error = None
                break
            except Exception as e:
                last_error = e
                log(f"insights_range ERROR {acc} (tentativa {attempt}/{INSIGHTS_RETRY_ATTEMPTS}) {e}")
                if attempt < INSIGHTS_RETRY_ATTEMPTS:
                    time.sleep(INSIGHTS_RETRY_DELAY_SECONDS)
        if last_error is not None:
            # Pedido do intervalo INTEIRO esgotou as tentativas — antes de
            # desistir da conta, tenta o mesmo intervalo quebrado em pedaços
            # menores (ver fetch_insights_range_chunked). Contas com muito
            # histórico às vezes travam só no pedido grande de uma vez; o
            # mesmo período em pedaços de 14d passa sem erro quase sempre.
            log(f"{acc}: intervalo inteiro falhou {INSIGHTS_RETRY_ATTEMPTS}x — tentando em pedaços de {INSIGHTS_CHUNK_DAYS}d")
            try:
                daily_rows[acc] = fetch_insights_range_chunked(acc, overall_since, overall_until, token)
                last_error = None
                log(f"{acc}: fallback em pedaços funcionou")
            except Exception as e2:
                last_error = e2
        if last_error is not None:
            print(f"⚠️  Falha ao coletar insights consolidados da conta {acc}: {last_error}")
            fetch_errors[acc] = str(last_error)

    # Campanhas com decide=KILL nos últimos 2 dias corridos seguidos — vira
    # "Alerta" (mais grave que "Ruim" pontual) em todas as janelas, já que
    # reflete dias de calendário reais, não o recorte da janela sendo vista.
    alert_2d_by_account = {}
    for acc in account_ids:
        if acc in fetch_errors:
            continue
        meta = account_meta[acc]
        account_name = meta["status_info"].get("account_name") or acc
        alert_2d_by_account[acc] = compute_2day_alert_campaign_ids(
            daily_rows[acc], perfil, meta["campaign_meta"], acc, account_name, meta.get("adset_goals"), today
        )

    failures = 1 if fetch_errors else 0
    for days in windows:
        try:
            log(f"fetch window {days}d")
            since_date, until_date = window_date_range(days, today)
            all_campaigns = []
            combined_raws = {}
            accounts_out = []
            total_rows = 0
            for acc in account_ids:
                if acc in fetch_errors:
                    continue
                meta = account_meta[acc]
                status_info = meta["status_info"]
                account_name = status_info.get("account_name") or acc
                rows = filter_rows_for_window(daily_rows[acc], since_date, until_date)
                # rows tem 1 linha por (anúncio, dia) — conta anúncios únicos,
                # não linhas, senão o número infla com o tamanho da janela.
                total_rows += len({r.get("ad_id") for r in rows})
                campaigns, raws = aggregate(
                    rows, perfil, meta["campaign_meta"], acc, account_name, meta.get("adset_goals")
                )
                alert_2d_ids = alert_2d_by_account.get(acc, set())
                for camp in campaigns:
                    camp["alert_2d"] = camp["campaign_id"] in alert_2d_ids
                all_campaigns.extend(campaigns)
                merge_raws(combined_raws, raws)
                activities_window = filter_activities_for_window(meta.get("activities_raw", []), since_date, until_date)
                recent_activities = sorted(
                    (summarize_activity(a) for a in activities_window),
                    key=lambda a: a.get("event_time") or "",
                    reverse=True,
                )[:20]
                accounts_out.append({
                    "ad_account_id": acc,
                    "account_name": account_name,
                    "account_status": status_info.get("account_status"),
                    "account_status_label": status_info.get("account_status_label"),
                    "disable_reason": status_info.get("disable_reason"),
                    "spend": round(raws.get("spend", 0), 2),
                    "recent_activities": recent_activities,
                    "pixel_health": meta.get("pixel_health", {"overall_status": "DESCONHECIDO", "pixels": []}),
                })
            all_campaigns.sort(key=lambda c: c.get("spend", 0), reverse=True)
            kpis_summary = compute_kpis_summary(perfil, combined_raws)
            period_over_period = compute_period_over_period(
                perfil, days, since_date, overall_since, account_ids, fetch_errors, daily_rows
            )
            out = {
                "generated_at": datetime.now(BRT).isoformat(timespec="seconds"),
                "window_days": days,
                "ad_account_id": account_ids[0] if len(account_ids) == 1 else ", ".join(account_ids),
                "account_status": account_meta[account_ids[0]]["status_info"],
                "accounts": accounts_out,
                "currency": "BRL",
                "perfil_kpis": [k["key"] for k in perfil["kpis"]],
                "primary_kpi": perfil["primary_kpi"],
                "kpis_summary": kpis_summary,
                "period_over_period": period_over_period,
                "spend_total": round(combined_raws.get("spend", 0), 2),
                "raws_total": combined_raws,
                "campaigns": all_campaigns,
            }
            outfile = DASH_DIR / f"paid-traffic-{days}d.json"
            outfile.write_text(json.dumps(out, indent=2, ensure_ascii=False))
            print(f"✅ {outfile.name} — {len(accounts_out)}/{len(account_ids)} conta(s), {total_rows} ads, R${combined_raws.get('spend', 0):.2f}")
            log(f"  -> {outfile.name} OK")
        except Exception as e:
            failures += 1
            print(f"❌ janela {days}d falhou: {e}")
            log(f"  -> ERROR {e}")

    log(f"END failures={failures}")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
