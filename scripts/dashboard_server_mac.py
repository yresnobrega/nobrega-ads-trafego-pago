#!/usr/bin/env python3
"""Servidor estático do dashboard local (Mac), com endpoint de refresh ao vivo.

Diferente do servidor da VPS (dashboard_server.py, com Basic Auth pra acesso
externo): este roda só em 127.0.0.1, sem auth — só sua máquina acessa.
GET /api/refresh dispara uma coleta nova de TODAS as janelas configuradas +
regeneração do HTML + sincronização com a VPS, tudo síncrono.
"""

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
META_DIR = OPERACAO / "scripts" / "meta"
PORT = int(os.environ.get("PORT", "8888"))

_refresh_lock = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASH_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            self._handle_refresh()
            return
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
            # Exit code 3 = fetch_metrics.py pulou por já ter outro rodando —
            # nada foi refeito, não pode virar "ok": true.
            if fetch.returncode == 3:
                self._json(409, {"ok": False, "error": "já tem uma coleta rodando — aguarde um pouco e tente de novo"})
                return
            if fetch.returncode != 0:
                self._json(500, {"ok": False, "error": fetch.stdout[-2000:] + fetch.stderr[-1000:]})
                return

            subprocess.run(
                ["python3", str(META_DIR / "paid_traffic_dashboard.py")],
                capture_output=True, text=True, timeout=60,
            )

            # Sincroniza pra VPS na hora, pra não esperar o próximo fetch agendado.
            subprocess.run(
                ["rsync", "-az", "-e", "ssh -o ConnectTimeout=10",
                 f"{DASH_DIR}/", "vps-claude:.operacao-ia/dashboards/"],
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
        pass


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Sem isso, reiniciar o serviço logo após uma conexão cai em "Address
    # already in use" enquanto o socket antigo fica em TIME_WAIT (~60s).
    allow_reuse_address = True
    # ThreadingMixIn: sem isso, um /api/refresh em andamento (até 480s de
    # subprocess) bloqueia TODAS as outras requisições até terminar.
    # _refresh_lock (acima) já impede refreshes concorrentes entre si.
    daemon_threads = True


if __name__ == "__main__":
    with ReusableTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Dashboard server (Mac, com refresh ao vivo) em http://127.0.0.1:{PORT}")
        httpd.serve_forever()
