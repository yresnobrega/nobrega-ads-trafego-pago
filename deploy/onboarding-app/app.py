#!/usr/bin/env python3
"""Serve o formulário de onboarding e envia cópia do briefing por e-mail.

GET  /                  -> onboarding.html
POST /api/enviar-copia  -> {cardTitle, cardDescription} -> e-mail via SMTP
                            (Hostinger) para contato@nobregaads.com

Credenciais SMTP em variáveis de ambiente (ver docker-compose/Easypanel):
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_TO
"""
import http.server
import json
import os
import smtplib
import socketserver
from email.mime.text import MIMEText
from pathlib import Path

HTML_PATH = Path(__file__).resolve().parent / "onboarding.html"
PORT = int(os.environ.get("PORT", "80"))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO = os.environ.get("SMTP_TO", "contato@nobregaads.com")

MAX_BODY_BYTES = 200_000  # briefing é texto curto; corta qualquer payload anormal


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", ""):
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/enviar-copia":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"ok": False, "error": "payload inválido"})
            return
        raw = self.rfile.read(length)

        try:
            payload = json.loads(raw)
            title = str(payload.get("cardTitle") or "Novo briefing")
            description = str(payload.get("cardDescription") or "")
        except (json.JSONDecodeError, AttributeError):
            self._json(400, {"ok": False, "error": "JSON inválido"})
            return

        if not SMTP_USER or not SMTP_PASS:
            self._json(500, {"ok": False, "error": "SMTP não configurado no servidor"})
            return

        try:
            msg = MIMEText(description, "plain", "utf-8")
            msg["Subject"] = title
            msg["From"] = SMTP_USER
            msg["To"] = SMTP_TO
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, [SMTP_TO], msg.as_string())
        except Exception as e:
            self._json(502, {"ok": False, "error": f"falha ao enviar e-mail: {e}"})
            return

        self._json(200, {"ok": True})

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Onboarding server em http://0.0.0.0:{PORT}")
        httpd.serve_forever()
