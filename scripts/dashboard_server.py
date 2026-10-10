#!/usr/bin/env python3
"""Servidor estático do dashboard de tráfego pago, com HTTP Basic Auth.

Serve ~/.operacao-ia/dashboards/ — roda na VPS (fora do Docker, direto no
host) com uma URL fixa, sem depender do Mac estar ligado. Também expõe
GET /api/refresh pra coleta ao vivo de TODAS as janelas configuradas (a
VPS já tem o token do Meta).
Credenciais em ~/.operacao-ia/config/dashboard_auth.env (chmod 600).

Suporta múltiplos logins. O primeiro (DASHBOARD_USER/DASHBOARD_PASS) é o
login "dono" — enxerga a pasta inteira (todas as contas) e é o único que
pode disparar /api/refresh, exatamente como antes. Logins extras usam o
sufixo _2, _3, ...:
    DASHBOARD_USER_2=nay.rodrigues
    DASHBOARD_PASS_2=xxxx
    DASHBOARD_VIEW_2=nay
DASHBOARD_VIEW_N tem que bater com o nome da view em dashboard_views.json
(paid_traffic_dashboard.py gera o HTML filtrado em dashboards/views/<view>/)
— esse login só enxerga aquela subpasta, nunca a pasta raiz.
"""

import base64
import http.server
import json
import os
import socketserver
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

OPERACAO = Path.home() / ".operacao-ia"
DASH_DIR = OPERACAO / "dashboards"
VIEWS_DIR = DASH_DIR / "views"
META_DIR = OPERACAO / "scripts" / "meta"
AUTH_ENV = OPERACAO / "config" / "dashboard_auth.env"
PORT = int(os.environ.get("DASHBOARD_PORT", "8888"))

_refresh_lock = threading.Lock()


def _load_auth_env():
    values = {}
    if AUTH_ENV.exists():
        for line in AUTH_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _load_auth():
    """Retorna {"Basic <b64>": view} — view None = pasta raiz (acesso total)."""
    values = _load_auth_env()
    user = values.get("DASHBOARD_USER", "")
    password = values.get("DASHBOARD_PASS", "")
    if not user or not password:
        raise SystemExit(f"❌ Configure DASHBOARD_USER e DASHBOARD_PASS em {AUTH_ENV}")

    auth_map = {}
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    auth_map[expected] = None  # dono: sem view = pasta raiz inteira

    n = 2
    while True:
        u = values.get(f"DASHBOARD_USER_{n}", "")
        p = values.get(f"DASHBOARD_PASS_{n}", "")
        v = values.get(f"DASHBOARD_VIEW_{n}", "")
        if not u or not p:
            break
        if not v:
            print(f"⚠️  DASHBOARD_USER_{n} sem DASHBOARD_VIEW_{n} — ignorando esse login (login extra precisa de view restrita)")
        else:
            key = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
            auth_map[key] = v
        n += 1
    return auth_map


AUTH_MAP = _load_auth()


class AuthHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASH_DIR), **kwargs)

    def do_GET(self):
        if self.headers.get("Authorization") not in AUTH_MAP:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Trafego Pago"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Autenticacao necessaria.")
            return

        view = AUTH_MAP[self.headers.get("Authorization")]

        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            if view is not None:
                # Logins restritos são só leitura — atualizar puxa dado novo
                # de TODAS as contas, não só do subconjunto que essa view vê.
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Sem permissao para atualizar.")
                return
            self._handle_refresh()
            return

        self.directory = str(VIEWS_DIR / view) if view is not None else str(DASH_DIR)
        super().do_GET()

    def _handle_refresh(self):
        # Atualiza TODAS as janelas de uma vez (não só a aba aberta) — com o
        # retry automático do fetch_metrics.py, uma conta instável pode levar
        # a coleta inteira a alguns minutos, daí o timeout mais folgado.
        if not _refresh_lock.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "já tem um refresh rodando, aguarde"})
            return
        try:
            fetch = subprocess.run(
                ["python3", str(META_DIR / "fetch_metrics.py")],
                capture_output=True, text=True, timeout=480,
            )
            # Exit code 3 = fetch_metrics.py pulou por já ter outro rodando
            # (cron ou outro clique em Atualizar) — nada foi refeito, então
            # NÃO pode virar "ok": true (senão parece que atualizou quando
            # na real ficou com o dado antigo).
            if fetch.returncode == 3:
                self._json(409, {"ok": False, "error": "já tem uma coleta rodando (cron ou outro Atualizar) — aguarde um pouco e tente de novo"})
                return
            if fetch.returncode != 0:
                self._json(500, {"ok": False, "error": fetch.stdout[-2000:] + fetch.stderr[-1000:]})
                return

            subprocess.run(
                ["python3", str(META_DIR / "paid_traffic_dashboard.py")],
                capture_output=True, text=True, timeout=60,
            )
            self._json(200, {"ok": True})
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "coleta demorou demais (timeout)"})
        finally:
            _refresh_lock.release()

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # logs vao pro systemd journal via stdout normal do processo pai


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Sem isso, reiniciar o serviço logo após uma conexão cai em "Address
    # already in use" enquanto o socket antigo fica em TIME_WAIT (~60s).
    allow_reuse_address = True
    # ThreadingMixIn: sem isso, um /api/refresh em andamento (até 480s de
    # subprocess) bloqueia TODAS as outras requisições (dashboard, favicon)
    # até terminar, e o proxy na frente devolve 504 antes disso acontecer.
    # _refresh_lock (acima) já impede refreshes concorrentes entre si.
    daemon_threads = True


if __name__ == "__main__":
    with ReusableTCPServer(("0.0.0.0", PORT), AuthHandler) as httpd:
        print(f"Dashboard server em http://0.0.0.0:{PORT} (auth ativo, {len(AUTH_MAP)} login(s))")
        httpd.serve_forever()
