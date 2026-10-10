#!/usr/bin/env python3
"""
paid_traffic_dashboard.py — gera HTML do dashboard a partir dos JSONs.

Lê ~/.operacao-ia/dashboards/paid-traffic-{N}d.json e o template,
emite paid-traffic-dashboard-{N}d.html injetando JSON_DATA inline.

Também gera, pra cada view configurada em dashboard_views.json (ex: um
login de Basic Auth extra que só deve ver um subconjunto de contas), uma
versão filtrada em views/<view>/paid-traffic-dashboard-{N}d.html — com
accounts/campaigns restritos às contas da view e raws_total/kpis_summary
recalculados só sobre esse subconjunto (dashboard_server.py aponta cada
login pra sua própria pasta).
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_metrics import merge_raws, compute_kpis_summary  # noqa: E402

OPERACAO = Path.home() / ".operacao-ia"
DASH_DIR = OPERACAO / "dashboards"
VIEWS_DIR = DASH_DIR / "views"
META_DIR = OPERACAO / "scripts" / "meta"
PERFIL = OPERACAO / "config" / "meta_perfil.json"
VIEWS_CONFIG = OPERACAO / "config" / "dashboard_views.json"
TEMPLATE_LOCAL = META_DIR / "paid-traffic-dashboard-template.html"
TEMPLATE_REPO  = Path(__file__).resolve().parent.parent / "docs" / "paid-traffic-dashboard-template.html"

# Assets estáticos (logo etc.) que o template referencia por caminho relativo
# (ex: src="nobrega_ads_logo_claro.svg") — cada view roda numa subpasta
# própria (dashboard_server.py aponta o self.directory pra lá), então sem
# copiar o arquivo pra dentro dela a imagem quebra (404) pra quem usa login
# restrito, mesmo com o arquivo existindo normalmente na pasta raiz.
STATIC_ASSET_EXTS = {".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp"}


def load_budget_caps():
    if not PERFIL.exists():
        return {}
    try:
        return json.loads(PERFIL.read_text()).get("budget_caps") or {}
    except (OSError, ValueError):
        return {}


def load_perfil():
    if not PERFIL.exists():
        return None
    try:
        return json.loads(PERFIL.read_text())
    except (OSError, ValueError):
        return None


def load_views():
    """{"nay": {"label": "Nay Rodrigues", "accounts": ["Koble Brasil", ...]}, ...}"""
    if not VIEWS_CONFIG.exists():
        return {}
    try:
        return json.loads(VIEWS_CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def format_generated_at(raw):
    """ISO cru (2026-08-30T13:36:41-03:00) é denso demais no header — separa
    data e hora igual ao resto do produto (report_formatter.py usa o mesmo
    "às" entre elas)."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%d/%m/%Y às %H:%M")
    except ValueError:
        return raw


def find_template():
    if TEMPLATE_LOCAL.exists():
        return TEMPLATE_LOCAL
    if TEMPLATE_REPO.exists():
        return TEMPLATE_REPO
    return None


def render_html(template, data, win_list_js):
    days = data["window_days"]
    title_days = "Ontem" if days == 0 else ("Hoje" if days == 1 else f"{days}d")
    return template.replace(
        "/*__JSON_DATA__*/",
        f"window.PAID_TRAFFIC_DATA = {json.dumps(data, ensure_ascii=False)};"
    ).replace(
        "<!--__GENERATED_AT__-->",
        f"Atualizado: {format_generated_at(data.get('generated_at', ''))}"
    ).replace(
        "<!--__TITLE__-->",
        f"Tráfego Pago — {title_days}"
    ).replace(
        "/*__WIN_LIST__*/[4, 7, 14, 30]/*__END_WIN_LIST__*/",
        win_list_js
    )


def build_filtered_data(data, view_cfg, perfil):
    """Restringe accounts/campaigns às contas da view e recalcula os
    agregados (raws_total/spend_total/kpis_summary) só sobre esse
    subconjunto — sem isso o card de KPI da view filtrada mostraria a soma
    de TODAS as contas, vazando o resto da operação pro login restrito."""
    names = set(view_cfg["accounts"])
    accounts = [a for a in data.get("accounts", []) if a.get("account_name") in names]
    campaigns = [c for c in data.get("campaigns", []) if c.get("account_name") in names]

    combined_raws = {}
    for c in campaigns:
        merge_raws(combined_raws, c.get("raws", {}))

    filtered = dict(data)
    filtered["accounts"] = accounts
    filtered["campaigns"] = campaigns
    filtered["raws_total"] = combined_raws
    filtered["spend_total"] = round(combined_raws.get("spend", 0), 2)
    filtered["ad_account_id"] = ", ".join(a["ad_account_id"] for a in accounts)
    # Header "Visão geral — contas de X" no template usa isso pra não dar a
    # entender que a view filtrada mostra a operação inteira.
    filtered["view_label"] = view_cfg.get("label") or ""
    if perfil:
        filtered["kpis_summary"] = compute_kpis_summary(perfil, combined_raws)
    return filtered


def copy_static_assets(view_dir):
    for f in DASH_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in STATIC_ASSET_EXTS:
            dest = view_dir / f.name
            if not dest.exists() or dest.stat().st_mtime < f.stat().st_mtime:
                shutil.copy2(f, dest)


def build_index_html(default_url):
    # Index é só um redirect pra janela padrão — evita manter um visual
    # separado do template principal (isso já causou o dashboard em "/"
    # ficar desatualizado/com tema diferente das páginas de janela).
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0; url={default_url}">'
        '<title>Nobrega Ads — Tráfego Pago</title>'
        '<style>body{background:#F6F4EF;color:#12141C;font-family:Inter,system-ui,sans-serif;'
        'padding:40px;text-align:center}'
        'a{color:#FF4405;font-weight:600}</style></head><body>'
        f'<p>Redirecionando para <a href="{default_url}">{default_url}</a>…</p>'
        '</body></html>'
    )


def main():
    tpl = find_template()
    if not tpl:
        print(f"❌ Template HTML não encontrado em {TEMPLATE_LOCAL} ou {TEMPLATE_REPO}")
        sys.exit(1)

    if not DASH_DIR.exists():
        print(f"❌ {DASH_DIR} não existe — rode fetch_metrics.py primeiro")
        sys.exit(1)

    jsons = sorted(DASH_DIR.glob("paid-traffic-*d.json"))
    if not jsons:
        print(f"❌ Nenhum JSON em {DASH_DIR}")
        sys.exit(1)

    template = tpl.read_text()
    all_days = sorted({
        json.loads(jf.read_text())["window_days"]
        for jf in jsons
        if jf.stat().st_size > 0
    })
    win_list_js = json.dumps(all_days)
    budget_caps = load_budget_caps()
    views = {
        name: cfg for name, cfg in load_views().items()
        if cfg.get("accounts")
    }
    perfil = load_perfil() if views else None

    for view_name in views:
        view_dir = VIEWS_DIR / view_name
        view_dir.mkdir(parents=True, exist_ok=True)
        copy_static_assets(view_dir)

    generated = 0
    for jf in jsons:
        try:
            data = json.loads(jf.read_text())
        except Exception as e:
            print(f"⚠️  Pulando {jf.name}: {e}")
            continue
        data["budget_caps"] = budget_caps
        days = data["window_days"]

        html = render_html(template, data, win_list_js)
        out = DASH_DIR / f"paid-traffic-dashboard-{days}d.html"
        out.write_text(html)
        print(f"✅ {out.name}")
        generated += 1

        for view_name, view_cfg in views.items():
            filtered = build_filtered_data(data, view_cfg, perfil)
            filtered_html = render_html(template, filtered, win_list_js)
            view_out = VIEWS_DIR / view_name / f"paid-traffic-dashboard-{days}d.html"
            view_out.write_text(filtered_html)
            print(f"✅ views/{view_name}/{view_out.name}")

    default_day = 7 if 7 in all_days else (all_days[0] if all_days else 1)
    default_url = f"paid-traffic-dashboard-{default_day}d.html"
    (DASH_DIR / "index.html").write_text(build_index_html(default_url))

    for view_name in views:
        view_dir = VIEWS_DIR / view_name
        if view_dir.exists():
            (view_dir / "index.html").write_text(build_index_html(default_url))

    print(f"\n{generated} dashboards gerados em {DASH_DIR}")
    if views:
        print(f"Views filtradas geradas: {', '.join(views)}")


if __name__ == "__main__":
    main()
