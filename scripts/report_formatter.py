#!/usr/bin/env python3
"""Formata o relatório Meta já coletado, sem rede e sem recalcular métricas."""

import argparse
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


WHATSAPP_LIMIT = 1200
STALE_AFTER = timedelta(hours=26)
STATUS_LABELS = {
    "green": "BOM",
    "yellow": "ATENÇÃO",
    "red": "RUIM",
    "gray": "SEM DADOS",
}
STATUS_EMOJIS = {
    "green": "🟢",
    "yellow": "🟡",
    "red": "🔴",
    "gray": "⚪",
}
STATUS_COLORS = {
    "green": "#18794e",
    "yellow": "#8a6100",
    "red": "#b42318",
    "gray": "#667085",
}
CAMPAIGN_STATUS_EMOJIS = {
    "ACTIVE": "🟢",
    "PAUSED": "⏸️",
    "PENDING_REVIEW": "🟡",
    "DISAPPROVED": "🔴",
    "IN_PROCESS": "🟡",
    "WITH_ISSUES": "🔴",
    "ARCHIVED": "⚪",
    "DELETED": "⚪",
}
# Nome como aparece no Business Manager -> como deve sair nos relatórios (mesmo texto do dash).
ACCOUNT_NAME_OVERRIDES = {
    "CA - HS Imports": "HS IMPORTS",
    "Riachi Iphones": "AURA",
    "Tudo para Smartphone": "TUDO PARA IPHONE",
}


def _display_account_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return name
    # Mesmo tratamento visual do dash: maiúsculo (CSS text-transform lá, .upper() aqui).
    return ACCOUNT_NAME_OVERRIDES.get(name, name).upper()
# decide() do coletor devolve 5 valores (SCALE/KILL/KEEP/KEEP-amostra/VALIDATION);
# o relatório expõe só 3 decisões em português, o que o aluno realmente decide fazer.
DECISION_LABELS_PT = {
    "SCALE": "Escalar",
    "KILL": "Pausar",
    "KEEP": "Manter",
    "KEEP-AMOSTRA": "Manter",
    "VALIDATION": "Manter",
}
DECISION_EMOJIS_PT = {
    "SCALE": "📈",
    "KILL": "🛑",
    "KEEP": "🟡",
}


def _operation_dir(operation_dir: Optional[Path] = None) -> Path:
    return Path(operation_dir) if operation_dir else Path.home() / ".operacao-ia"


def format_number_br(value: Any, decimals: int = 2) -> str:
    """Formata número com ponto de milhar e vírgula decimal."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    rendered = f"{number:,.{decimals}f}"
    return rendered.replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_metric(value: Any, format_name: str, currency: str = "BRL") -> str:
    if value is None:
        return "—"
    if format_name == "currency":
        prefix = "R$ " if currency == "BRL" else f"{currency} "
        return prefix + format_number_br(value)
    if format_name == "percent":
        return format_number_br(value) + "%"
    return format_number_br(value)


def describe_kpi_direction(value: Any, target: Any, better: str) -> str:
    """Explica a direção sem inverter KPIs em que um valor maior é melhor."""
    try:
        current = float(value)
        goal = float(target)
    except (TypeError, ValueError):
        return "sem comparação com a meta"
    if goal <= 0:
        return "meta inválida"
    if abs(current - goal) < 1e-12:
        return "na meta"
    relation = "acima" if current > goal else "abaixo"
    favorable = (better == "higher" and current > goal) or (
        better == "lower" and current < goal
    )
    quality = "direção favorável" if favorable else "direção desfavorável"
    delta = abs((current - goal) / goal * 100)
    return f"{format_number_br(delta, 1)}% {relation} da meta; {quality}"


# Mesma faixa usada no dashboard (docs/paid-traffic-dashboard-template.html):
# MENSAGEM avalia por custo/mensagem, ENGAJAMENTO/VISITA/INTERAÇÃO por custo/resultado.
def classify_campaign_quality(decide_metric_key: Any, value: Any) -> tuple:
    """Retorna (rótulo, é_alerta) a partir do custo relevante pro objetivo da campanha."""
    if value is None:
        return None, False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, False
    if decide_metric_key == "cost_per_msg":
        if v < 3:
            return "Excelente", False
        if v <= 5:
            return "Bom", False
        return "Alerta", True
    if decide_metric_key == "cost_per_result":
        if v < 0.5:
            return "Ótimo", False
        return "Alerta", True
    return None, False


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("a raiz do JSON precisa ser um objeto")
    return data


def _profile_window(profile: Dict[str, Any], requested: Optional[int]) -> int:
    if requested is not None:
        if requested < 1:
            raise ValueError("--window precisa ser maior que zero")
        return requested
    windows = profile.get("windows") or [4, 7, 14, 30]
    valid = [int(item) for item in windows if isinstance(item, int) and item > 0]
    return min(valid) if valid else 7


def _parse_generated_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _display_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return "data desconhecida"
    return value.strftime("%d/%m/%Y às %H:%M")


def _iter_ads(campaign: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Aceita o contrato atual (ads em adsets) e o formato direto legado."""
    for ad in campaign.get("ads") or []:
        if isinstance(ad, dict):
            yield ad
    for adset in campaign.get("adsets") or []:
        if not isinstance(adset, dict):
            continue
        for ad in adset.get("ads") or []:
            if isinstance(ad, dict):
                yield ad


def _empty_report(window: int, source: Path, reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "warning": (
            "DADOS INDISPONÍVEIS: "
            + reason
            + " O relatório não inventou números. Verifique o coletor de métricas."
        ),
        "stale": False,
        "account": "não informada",
        "account_name": "não informada",
        "account_status_label": "desconhecido",
        "account_status_emoji": "⚪",
        "window": window,
        "generated_display": "coleta indisponível",
        "spend": None,
        "currency": "BRL",
        "kpis": [],
        "campaigns": [],
        "active_campaigns": [],
        "scale_ads": [],
        "kill_ads": [],
        "keep_ads": [],
        "source": str(source),
        "footer": (
            f"Fonte esperada: {source}. Para conferir na hora: "
            "python3 ~/.operacao-ia/scripts/meta/report_formatter.py --format text"
        ),
    }


def assemble_report(
    window: Optional[int] = None,
    *,
    operation_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Monta uma única estrutura usada pelos três renderizadores."""
    base = _operation_dir(operation_dir)
    profile_path = base / "config" / "meta_perfil.json"
    profile: Dict[str, Any] = {}
    profile_error = ""
    try:
        profile = _read_json(profile_path)
    except FileNotFoundError:
        profile_error = f"{profile_path} não existe."
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        profile_error = f"{profile_path} não pôde ser lido ({exc})."

    try:
        selected_window = _profile_window(profile, window)
    except ValueError:
        raise
    source = base / "dashboards" / f"paid-traffic-{selected_window}d.json"
    if profile_error:
        return _empty_report(selected_window, source, profile_error)

    try:
        payload = _read_json(source)
    except FileNotFoundError:
        return _empty_report(
            selected_window,
            source,
            f"{source} não existe. Rode o coletor antes do relatório.",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _empty_report(
            selected_window,
            source,
            f"{source} está inválido ou ilegível ({exc}).",
        )

    generated_error = ""
    try:
        generated_at = _parse_generated_at(payload.get("generated_at"))
    except ValueError as exc:
        generated_at = None
        generated_error = f"generated_at inválido ({exc})"

    reference_now = now or datetime.now(timezone.utc)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    age = (
        reference_now.astimezone(timezone.utc)
        - generated_at.astimezone(timezone.utc)
        if generated_at
        else None
    )
    # 26h tolera pequenos atrasos do job diário sem esconder falha do coletor.
    stale = generated_at is None or (age is not None and age > STALE_AFTER)
    warning = ""
    if stale:
        detail = generated_error or "a última coleta tem mais de 26 horas"
        warning = (
            "DADO DEFASADO: "
            + detail
            + ". O coletor pode ter falhado; confira antes de decidir."
        )

    kpi_meta = {
        item.get("key"): item
        for item in profile.get("kpis") or []
        if isinstance(item, dict) and item.get("key")
    }
    kpis = []
    for key, summary in (payload.get("kpis_summary") or {}).items():
        if not isinstance(summary, dict):
            continue
        meta = kpi_meta.get(key, {})
        status = str(summary.get("status") or "gray").lower()
        if status not in STATUS_LABELS:
            status = "gray"
        value = summary.get("value")
        target = summary.get("target", meta.get("target"))
        format_name = str(meta.get("format") or "ratio")
        better = str(meta.get("better") or "lower")
        kpis.append(
            {
                "key": key,
                "label": meta.get("label") or key.upper(),
                "value": value,
                "value_display": format_metric(
                    value, format_name, str(payload.get("currency") or "BRL")
                ),
                "target_display": format_metric(
                    target, format_name, str(payload.get("currency") or "BRL")
                ),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "direction": describe_kpi_direction(value, target, better),
            }
        )

    def _account_emoji(code: Any) -> str:
        return "🟢" if code == 1 else ("🔴" if code is not None else "⚪")

    raw_accounts = payload.get("accounts")
    if isinstance(raw_accounts, list) and raw_accounts:
        accounts = [
            {
                "id": a.get("ad_account_id") or "—",
                "name": _display_account_name(a.get("account_name")) or a.get("ad_account_id") or "conta sem nome",
                "status_label": a.get("account_status_label") or "desconhecido",
                "status_emoji": _account_emoji(a.get("account_status")),
            }
            for a in raw_accounts
            if isinstance(a, dict)
        ]
    else:
        # Fallback para dashboards antigos (formato de 1 conta só, sem "accounts").
        legacy_status = payload.get("account_status") or {}
        accounts = [
            {
                "id": payload.get("ad_account_id") or profile.get("ad_account_id") or "—",
                "name": _display_account_name(legacy_status.get("account_name"))
                or payload.get("ad_account_id")
                or "conta sem nome",
                "status_label": legacy_status.get("account_status_label") or "desconhecido",
                "status_emoji": _account_emoji(legacy_status.get("account_status")),
            }
        ]
    multi_account = len(accounts) > 1
    account_name_by_id = {a["id"]: a["name"] for a in accounts}

    # Mantidos por compatibilidade com quem já lê "account_status_label" direto.
    account_status_label = accounts[0]["status_label"] if len(accounts) == 1 else (
        f"{sum(1 for a in accounts if a['status_emoji'] == '🟢')}/{len(accounts)} ativas"
    )
    account_status_emoji = accounts[0]["status_emoji"] if len(accounts) == 1 else (
        "🟢" if all(a["status_emoji"] == "🟢" for a in accounts) else "🟡"
    )

    primary = str(payload.get("primary_kpi") or profile.get("primary_kpi") or "")
    primary_meta = kpi_meta.get(primary, {})
    primary_format = str(primary_meta.get("format") or "ratio")
    primary_label = str(primary_meta.get("label") or primary.upper() or "KPI")
    currency = str(payload.get("currency") or "BRL")
    raw_campaigns = [
        item for item in payload.get("campaigns") or [] if isinstance(item, dict)
    ]
    raw_campaigns.sort(key=lambda item: _safe_float(item.get("spend")), reverse=True)
    campaigns = []
    active_campaigns = []
    scale_ads = []
    kill_ads = []
    keep_ads = []
    seen_ads = set()
    for campaign in raw_campaigns:
        # Campanhas |POST|/|VP| decidem por custo por resultado, não pelo
        # KPI primário da conta (custo por mensagem não existe pra elas).
        # "decide_metric_key" ausente = campanha nunca avaliada (não WhatsApp,
        # sem KPI mapeado) -> None explícito != chave ausente do fetch antigo.
        has_metric_key = "decide_metric_key" in campaign
        decide_metric_key = campaign.get("decide_metric_key") if has_metric_key else primary
        decide_metric_label = (
            (campaign.get("decide_metric_label") or "Métrica") if has_metric_key else primary_label
        )
        metric_value = (campaign.get("metrics") or {}).get(decide_metric_key)
        eff_status = str(campaign.get("effective_status") or "").upper()
        camp_account_id = campaign.get("account_id")
        camp_raws = campaign.get("raws") or {}
        impressions = _safe_float(camp_raws.get("impressions"))
        clicks = _safe_float(camp_raws.get("clicks"))
        ctr_value = (clicks / impressions * 100) if impressions > 0 else None
        conversions = (
            _safe_float(camp_raws.get("leads"))
            + _safe_float(camp_raws.get("purchases"))
            + _safe_float(camp_raws.get("messages"))
            + _safe_float(camp_raws.get("installs"))
        )
        camp_spend = _safe_float(campaign.get("spend"))
        cpa_value = (camp_spend / conversions) if conversions > 0 else None
        cpm_value = (campaign.get("metrics") or {}).get("cpm")
        camp_decide_key = str(campaign.get("decide") or "").upper()
        camp_decision = DECISION_LABELS_PT.get(camp_decide_key, "Manter")
        camp_decision_emoji = DECISION_EMOJIS_PT.get(camp_decide_key, "🟡")
        quality_label, quality_alert = classify_campaign_quality(decide_metric_key, metric_value)
        campaign_row = {
            "name": campaign.get("name") or "Campanha sem nome",
            "account_id": camp_account_id,
            "account_name": _display_account_name(campaign.get("account_name"))
            or account_name_by_id.get(camp_account_id)
            or (accounts[0]["name"] if len(accounts) == 1 else None),
            "status": eff_status or "DESCONHECIDO",
            "status_emoji": CAMPAIGN_STATUS_EMOJIS.get(eff_status, "⚪"),
            "spend": format_metric(campaign.get("spend"), "currency", currency),
            "spend_value": camp_spend,
            "cpm_display": format_metric(cpm_value, "currency", currency),
            "ctr_display": format_metric(ctr_value, "percent"),
            "cpa_display": format_metric(cpa_value, "currency", currency),
            "decision": camp_decision,
            "decision_emoji": camp_decision_emoji,
            "primary_label": decide_metric_label,
            "primary_value": format_metric(metric_value, primary_format, currency),
            "quality_label": quality_label,
            "quality_alert": quality_alert,
        }
        campaigns.append(campaign_row)
        if eff_status == "ACTIVE":
            active_campaigns.append(campaign_row)
        for ad in _iter_ads(campaign):
            identity = ad.get("ad_id") or id(ad)
            if identity in seen_ads:
                continue
            seen_ads.add(identity)
            decision = str(ad.get("decide") or "").upper()
            item = {
                "campaign": campaign.get("name") or "Campanha sem nome",
                "account_name": _display_account_name(campaign.get("account_name"))
                or account_name_by_id.get(campaign.get("account_id"))
                or (accounts[0]["name"] if len(accounts) == 1 else None),
                "name": ad.get("ad_name") or "Anúncio sem nome",
                "reason": ad.get("decide_reason") or "motivo não informado",
                "spend_value": _safe_float(ad.get("spend")),
            }
            if decision == "SCALE":
                scale_ads.append(item)
            elif decision == "KILL":
                kill_ads.append(item)
            else:
                keep_ads.append(item)
    scale_ads.sort(key=lambda item: item["spend_value"], reverse=True)
    kill_ads.sort(key=lambda item: item["spend_value"], reverse=True)
    keep_ads.sort(key=lambda item: item["spend_value"], reverse=True)

    return {
        "available": True,
        "warning": warning,
        "stale": stale,
        "account": payload.get("ad_account_id") or profile.get("ad_account_id") or "não informada",
        "account_name": accounts[0]["name"] if len(accounts) == 1 else None,
        "accounts": accounts,
        "multi_account": multi_account,
        "account_status_label": account_status_label,
        "account_status_emoji": account_status_emoji,
        "window": payload.get("window_days") or selected_window,
        "generated_display": _display_datetime(generated_at),
        "spend": format_metric(
            payload.get("spend_total"),
            "currency",
            str(payload.get("currency") or "BRL"),
        ),
        "currency": payload.get("currency") or "BRL",
        "kpis": kpis,
        "campaigns": campaigns,
        "active_campaigns": active_campaigns,
        "scale_ads": scale_ads,
        "kill_ads": kill_ads,
        "keep_ads": keep_ads,
        "source": str(source),
        "footer": "",
    }


def render_text(report: Optional[Dict[str, Any]] = None, **assemble_kwargs: Any) -> str:
    report = report or assemble_report(**assemble_kwargs)
    lines: List[str] = []
    if report.get("warning"):
        lines.extend([f"⚠ {report['warning']}", ""])
    account_display = report.get("account_name") or report["account"]
    lines.append(
        "RELATÓRIO META ADS — conta "
        f"{account_display} — {report['window']} dias — {report['generated_display']}"
    )
    if report.get("available"):
        lines.append("")
        if report.get("multi_account"):
            lines.append("Contas:")
            for a in report.get("accounts") or []:
                lines.append(f"  - {a['name']} ({a['id']}): {a['status_label']}")
        else:
            lines.append(f"Status da conta: {report.get('account_status_label', 'desconhecido')}")
        lines.extend([f"Gasto total: {report['spend']}", "", "KPIs"])
        if report["kpis"]:
            for item in report["kpis"]:
                lines.append(
                    f"- {item['label']}: {item['value_display']} | meta "
                    f"{item['target_display']} | {item['status_label']} "
                    f"({item['direction']})"
                )
        else:
            lines.append("- Nenhum KPI disponível no JSON.")

        lines.extend(["", "Campanhas ativas"])
        active_campaigns = report.get("active_campaigns") or []
        if active_campaigns:
            for item in active_campaigns:
                prefix = f"[{item['account_name']}] " if report.get("multi_account") and item.get("account_name") else ""
                lines.append(
                    f"- {prefix}{item['name']}: Gasto {item['spend']} | "
                    f"CPM {item['cpm_display']} | CTR {item['ctr_display']} | "
                    f"CPA {item['cpa_display']} | Decisão: {item['decision_emoji']} {item['decision']}"
                )
        else:
            lines.append("- Nenhuma campanha ativa no momento.")
    if report.get("footer"):
        lines.extend(["", report["footer"]])
    return "\n".join(lines)


def render_html(report: Optional[Dict[str, Any]] = None, **assemble_kwargs: Any) -> str:
    report = report or assemble_report(**assemble_kwargs)

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    parts = [
        '<!doctype html><html><body style="font-family:Arial,sans-serif;'
        'color:#252525;line-height:1.45;max-width:760px;margin:0 auto;padding:20px">'
    ]
    if report.get("warning"):
        parts.append(
            '<div style="background:#fff4e5;border:1px solid #f5a623;'
            'padding:12px;margin-bottom:16px"><strong>⚠ '
            + esc(report["warning"])
            + "</strong></div>"
        )
    account_display = report.get("account_name") or report["account"]
    parts.append(
        "<h2 style=\"margin:0 0 8px\">Relatório Meta Ads</h2>"
        f"<p style=\"margin:0 0 18px\">Conta <strong>{esc(account_display)}</strong>"
        f" · {esc(report['window'])} dias · {esc(report['generated_display'])}</p>"
    )
    if report.get("available"):
        if report.get("multi_account"):
            parts.append("<h3>Contas</h3><ul>")
            for a in report.get("accounts") or []:
                parts.append(
                    f"<li>{esc(a['status_emoji'])} <strong>{esc(a['name'])}</strong> "
                    f"({esc(a['id'])}) — {esc(a['status_label'])}</li>"
                )
            parts.append("</ul>")
        else:
            parts.append(
                f"<p style=\"margin:0 0 8px\">Status da conta: <strong>"
                f"{esc(report.get('account_status_emoji', '⚪'))} "
                f"{esc(report.get('account_status_label', 'desconhecido'))}"
                "</strong></p>"
            )
        parts.append(f"<h3>Gasto total: {esc(report['spend'])}</h3>")
        parts.append(
            '<h3>KPIs</h3><table style="border-collapse:collapse;width:100%">'
            "<thead><tr><th align=\"left\">KPI</th><th align=\"left\">Valor</th>"
            "<th align=\"left\">Meta</th><th align=\"left\">Status</th>"
            "<th align=\"left\">Leitura</th></tr></thead><tbody>"
        )
        for item in report["kpis"]:
            color = STATUS_COLORS[item["status"]]
            parts.append(
                "<tr>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['label'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['value_display'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['target_display'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd;color:{color};"
                f"font-weight:bold\">{esc(item['status_label'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['direction'])}</td>"
                "</tr>"
            )
        if not report["kpis"]:
            parts.append('<tr><td colspan="5">Nenhum KPI disponível no JSON.</td></tr>')
        parts.append("</tbody></table>")

        account_col = "<th align=\"left\">Conta</th>" if report.get("multi_account") else ""
        active_campaigns = report.get("active_campaigns") or []
        parts.append(
            '<h3>Campanhas ativas</h3><table style="border-collapse:collapse;width:100%">'
            f"<thead><tr>{account_col}<th align=\"left\">Campanha</th>"
            "<th align=\"left\">Gasto</th><th align=\"left\">CPM</th>"
            "<th align=\"left\">CTR</th><th align=\"left\">CPA</th>"
            "<th align=\"left\">Decisão</th></tr></thead><tbody>"
        )
        for item in active_campaigns:
            account_cell = (
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item.get('account_name') or '—')}</td>"
                if report.get("multi_account") else ""
            )
            parts.append(
                "<tr>"
                f"{account_cell}"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['name'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['spend'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['cpm_display'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['ctr_display'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['cpa_display'])}</td>"
                f"<td style=\"padding:8px;border-top:1px solid #ddd\">{esc(item['decision_emoji'])} {esc(item['decision'])}</td></tr>"
            )
        if not active_campaigns:
            colspan = 7 if report.get("multi_account") else 6
            parts.append(f'<tr><td colspan="{colspan}">Nenhuma campanha ativa no momento.</td></tr>')
        parts.append("</tbody></table>")
    if report.get("footer"):
        parts.append(
            f'<p style="font-size:12px;color:#667085;border-top:1px solid #ddd;'
            f'padding-top:12px">{esc(report["footer"])}</p>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def _shorten(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _whatsapp_candidate(
    report: Dict[str, Any],
    *,
    campaign_limit: int,
    name_limit: int,
    summarized: bool,
) -> str:
    lines: List[str] = []
    if report.get("warning"):
        lines.append("⚠️ *" + _shorten(report["warning"], 210) + "*")
    account_display = report.get("account_name") or report["account"]
    lines.append(
        f"*RELATÓRIO META* | {_shorten(account_display, 30)} | {report['window']}d | "
        f"{report['generated_display']}"
    )
    if report.get("available"):
        if report.get("multi_account"):
            lines.append("*Contas:*")
            for a in report.get("accounts") or []:
                lines.append(f"{a['status_emoji']} {_shorten(a['name'], 28)}: {a['status_label']}")
        else:
            lines.append(
                f"*Conta:* {report.get('account_status_emoji', '⚪')} "
                f"{report.get('account_status_label', 'desconhecido')}"
            )
        lines.append(f"*Gasto:* {report['spend']}")
        lines.append("")
        lines.append("*KPIs*")
        for item in report["kpis"]:
            lines.append(
                f"{STATUS_EMOJIS[item['status']]} *{item['label']}:* "
                f"{item['value_display']} (meta {item['target_display']})"
            )
        lines.append("")
        active_campaigns = report.get("active_campaigns") or []
        alert_count = sum(1 for item in active_campaigns if item.get("quality_alert"))
        if alert_count:
            lines.append(f"⚠️ *{alert_count} campanha(s) fora do padrão de custo*")
        lines.append("*Campanhas ativas*")
        for item in active_campaigns[:campaign_limit]:
            acc_prefix = f"[{_shorten(item['account_name'], 14)}] " if report.get("multi_account") and item.get("account_name") else ""
            metric_part = (
                f"{item['primary_label']} {item['primary_value']} | "
                if item.get("primary_label") and item.get("primary_value")
                else ""
            )
            quality_part = f"⚠️ {item['quality_label']} | " if item.get("quality_alert") else ""
            lines.append(
                f"• {acc_prefix}{_shorten(item['name'], name_limit)} — "
                f"{item['spend']} | {metric_part}{quality_part}"
                f"CPM {item['cpm_display']} | CTR {item['ctr_display']} | "
                f"CPA {item['cpa_display']} | {item['decision_emoji']} {item['decision']}"
            )
        if not active_campaigns:
            lines.append("• Nenhuma campanha ativa no momento.")
    if report.get("footer"):
        lines.extend(["", _shorten(report["footer"], 150)])
    if summarized:
        lines.append("… *Relatório resumido para caber no WhatsApp.*")
    return "\n".join(lines)


def render_whatsapp(
    report: Optional[Dict[str, Any]] = None, **assemble_kwargs: Any
) -> str:
    report = report or assemble_report(**assemble_kwargs)
    total_active = len(report.get("active_campaigns") or [])
    options = [
        (total_active, 40),
        (10, 34),
        (6, 26),
        (3, 20),
        (1, 16),
    ]
    for campaign_limit, name_limit in options:
        summarized = campaign_limit < total_active or name_limit < 40
        candidate = _whatsapp_candidate(
            report,
            campaign_limit=campaign_limit,
            name_limit=name_limit,
            summarized=summarized,
        )
        if len(candidate) <= WHATSAPP_LIMIT:
            return candidate
    # Última barreira: a API recebe sempre menos de 1200 caracteres.
    suffix = "\n… *Relatório resumido para caber no WhatsApp.*"
    raw = _whatsapp_candidate(
        report,
        campaign_limit=1,
        name_limit=16,
        summarized=False,
    )
    return raw[: WHATSAPP_LIMIT - len(suffix)].rstrip() + suffix


def accounts_in_report(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Lista as contas do relatório multi-conta (1 item se for de conta única)."""
    return list(report.get("accounts") or [])


def scope_report_to_account(report: Dict[str, Any], account_id: str) -> Dict[str, Any]:
    """Recorta um relatório (possivelmente multi-conta) para uma única conta.

    Cada aluno/cliente tem seu próprio decide() hoje só na configuração global do
    perfil; contas sem o mesmo objetivo tendem a cair em VALIDATION/KEEP-amostra
    (sempre "Manter") até terem metas próprias — isso é esperado, não um bug.
    """
    account = next(
        (a for a in report.get("accounts") or [] if a.get("id") == account_id), None
    )
    active_campaigns = [
        c
        for c in report.get("active_campaigns") or []
        if c.get("account_id") == account_id
    ]
    spend_value = sum(c.get("spend_value", 0.0) for c in active_campaigns)
    return {
        "available": report.get("available", False),
        "warning": report.get("warning", ""),
        "account": account_id,
        "account_name": (account or {}).get("name") or account_id,
        "status_label": (account or {}).get("status_label") or "desconhecido",
        "status_emoji": (account or {}).get("status_emoji") or "⚪",
        "window": report.get("window"),
        "generated_display": report.get("generated_display"),
        "spend": format_metric(spend_value, "currency", str(report.get("currency") or "BRL")),
        "active_campaigns": active_campaigns,
        "footer": report.get("footer", ""),
    }


def render_account_whatsapp(scoped: Dict[str, Any]) -> str:
    """Mensagem enxuta de 1 conta: nome, campanhas ativas e veredito em pt-BR."""

    def _build(campaign_limit: int, name_limit: int, summarized: bool) -> str:
        lines: List[str] = []
        if scoped.get("warning"):
            lines.append("⚠️ *" + _shorten(scoped["warning"], 200) + "*")
        lines.append(f"*RELATÓRIO {_shorten(scoped['account_name'], 34)}*")
        lines.append(
            f"{scoped['window']}d | {scoped['generated_display']} | "
            f"{scoped['status_emoji']} {scoped['status_label']}"
        )
        lines.append(f"*Gasto do dia:* {scoped['spend']}")
        lines.append("")
        active_campaigns = scoped.get("active_campaigns") or []
        alert_count = sum(1 for item in active_campaigns if item.get("quality_alert"))
        if alert_count:
            lines.append(f"⚠️ *{alert_count} campanha(s) fora do padrão de custo*")
        lines.append("*Campanhas ativas*")
        for item in active_campaigns[:campaign_limit]:
            metric_part = (
                f"{item['primary_label']} {item['primary_value']} | "
                if item.get("primary_label") and item.get("primary_value")
                else ""
            )
            quality_part = f"⚠️ {item['quality_label']} | " if item.get("quality_alert") else ""
            lines.append(
                f"• {_shorten(item['name'], name_limit)} — {item['spend']} | "
                f"{metric_part}{quality_part}"
                f"CPM {item['cpm_display']} | CTR {item['ctr_display']} | "
                f"{item['decision_emoji']} {item['decision']}"
            )
        if not active_campaigns:
            lines.append("• Nenhuma campanha ativa no momento.")
        if scoped.get("footer"):
            lines.extend(["", _shorten(scoped["footer"], 150)])
        if summarized:
            lines.append("… *Relatório resumido para caber no WhatsApp.*")
        return "\n".join(lines)

    if not scoped.get("available"):
        return _build(0, 16, False)

    total_active = len(scoped.get("active_campaigns") or [])
    options = [
        (total_active, 40),
        (10, 34),
        (6, 26),
        (3, 20),
        (1, 16),
    ]
    for campaign_limit, name_limit in options:
        summarized = campaign_limit < total_active or name_limit < 40
        candidate = _build(campaign_limit, name_limit, summarized)
        if len(candidate) <= WHATSAPP_LIMIT:
            return candidate
    suffix = "\n… *Relatório resumido para caber no WhatsApp.*"
    raw = _build(1, 16, False)
    return raw[: WHATSAPP_LIMIT - len(suffix)].rstrip() + suffix


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspeciona o relatório Meta sem enviar")
    parser.add_argument("--window", type=int, help="janela do JSON em dias")
    parser.add_argument(
        "--format",
        choices=("text", "html", "whatsapp"),
        default="text",
        help="formato de saída (default: text)",
    )
    args = parser.parse_args()
    try:
        report = assemble_report(window=args.window)
        renderer = {
            "text": render_text,
            "html": render_html,
            "whatsapp": render_whatsapp,
        }[args.format]
        print(renderer(report))
        return 0
    except (OSError, ValueError) as exc:
        print(f"❌ Não foi possível montar o relatório: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
