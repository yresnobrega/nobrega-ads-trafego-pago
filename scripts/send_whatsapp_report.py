#!/usr/bin/env python3
"""Envia o relatório compartilhado pela Cloud API ou Evolution API."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from report_formatter import assemble_report, render_whatsapp


CONFIG_PATH = Path.home() / ".operacao-ia" / "config" / "meta.env"
CLOUD_ENDPOINT = "https://graph.facebook.com/v21.0/{phone_number_id}/messages"
PROVIDERS = ("cloud_api", "evolution")
PROVIDER_KEYS = {
    "cloud_api": (
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_TOKEN",
        "REPORT_WHATSAPP_TO",
    ),
    "evolution": (
        "EVOLUTION_URL",
        "EVOLUTION_API_KEY",
        "EVOLUTION_INSTANCE",
        "REPORT_WHATSAPP_TO",
    ),
}


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _config_value(file_values: Dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key, file_values.get(key, default)).strip()


def _normalize_number(value: str) -> str:
    # Grupo de WhatsApp: JID no formato <id>@g.us, não é número de telefone —
    # passa direto, sem validar quantidade de dígitos.
    if value.strip().endswith("@g.us"):
        return value.strip()
    number = re.sub(r"\D", "", value)
    if not 8 <= len(number) <= 15:
        raise ValueError(
            "REPORT_WHATSAPP_TO deve ter de 8 a 15 dígitos (incluindo o DDI) "
            "ou ser um ID de grupo terminando em @g.us"
        )
    return number


def mask_number(value: str) -> str:
    if value.strip().endswith("@g.us"):
        return value.strip()
    digits = re.sub(r"\D", "", value)
    return "****" + digits[-4:] if digits else "****"


def load_config() -> Tuple[Optional[Dict[str, object]], List[str]]:
    values = _read_env_file(CONFIG_PATH)
    provider = _config_value(values, "REPORT_WHATSAPP_PROVIDER", "cloud_api").lower()
    if provider not in PROVIDERS:
        raise ValueError(
            "REPORT_WHATSAPP_PROVIDER inválido. Valores aceitos: cloud_api, evolution"
        )
    resolved = {
        key: _config_value(values, key) for key in PROVIDER_KEYS[provider]
    }
    missing = [key for key in PROVIDER_KEYS[provider] if not resolved[key]]
    if missing:
        return {"REPORT_WHATSAPP_PROVIDER": provider}, missing

    resolved["REPORT_WHATSAPP_TO"] = _normalize_number(
        resolved["REPORT_WHATSAPP_TO"]
    )
    if provider == "cloud_api":
        if not resolved["WHATSAPP_PHONE_NUMBER_ID"].isdigit():
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID deve conter somente dígitos")
        if any(char.isspace() for char in resolved["WHATSAPP_TOKEN"]):
            raise ValueError("WHATSAPP_TOKEN não pode conter espaços")
    else:
        parsed = urlparse(resolved["EVOLUTION_URL"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("EVOLUTION_URL precisa começar com http:// ou https://")
        if parsed.username or parsed.password:
            raise ValueError("EVOLUTION_URL não deve conter usuário ou senha")
        if any(char.isspace() for char in resolved["EVOLUTION_API_KEY"]):
            raise ValueError("EVOLUTION_API_KEY não pode conter espaços")

    window_raw = _config_value(values, "REPORT_WINDOW")
    window = None
    if window_raw:
        try:
            window = int(window_raw)
            if window < 1:
                raise ValueError
        except ValueError:
            raise ValueError("REPORT_WINDOW deve ser um número inteiro maior que zero")
    return {
        "REPORT_WHATSAPP_PROVIDER": provider,
        "REPORT_WINDOW": window,
        **resolved,
    }, []


def _request_json(url: str, headers: Dict[str, str], payload: Dict[str, object]) -> None:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        response.read()


def _group_mention_jids(base: str, api_key: str, group_jid: str) -> List[str]:
    # evolution-go 0.7.2 monta o mentionAll com o JID @lid do participante em
    # vez do @s.whatsapp.net — WhatsApp não resolve a menção nesse formato e
    # a notificação nunca dispara (bug aberto: evolution-foundation/evolution-go PR #137).
    # Contornamos resolvendo os JIDs certos aqui e mandando via mentionedJid.
    request = Request(
        f"{base}/group/info",
        data=json.dumps({"groupJid": group_jid}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "apikey": api_key},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    participants = body.get("data", {}).get("Participants", [])
    return [p["PhoneNumber"] for p in participants if p.get("PhoneNumber")]


def send_cloud_api(config: Dict[str, object], message: str) -> None:
    url = CLOUD_ENDPOINT.format(
        phone_number_id=quote(str(config["WHATSAPP_PHONE_NUMBER_ID"]), safe="")
    )
    _request_json(
        url,
        {"Authorization": f"Bearer {config['WHATSAPP_TOKEN']}"},
        {
            "messaging_product": "whatsapp",
            "to": config["REPORT_WHATSAPP_TO"],
            "type": "text",
            "text": {"body": message},
        },
    )


def send_evolution(config: Dict[str, object], message: str) -> None:
    # evolution-go (evoapicloud/evolution-go): instance é implícita no apikey
    # (token por instância), não vai na URL como no Evolution API (Node.js).
    base = str(config["EVOLUTION_URL"]).rstrip("/")
    api_key = str(config["EVOLUTION_API_KEY"])
    destination = str(config["REPORT_WHATSAPP_TO"])
    payload: Dict[str, object] = {"number": destination, "text": message}
    if destination.endswith("@g.us"):
        # Sem isso, quem tem o grupo configurado como "somente @menções" não
        # recebe notificação — a mensagem só aparece ao abrir o WhatsApp.
        try:
            mentioned = _group_mention_jids(base, api_key, destination)
        except (HTTPError, URLError, OSError, ValueError, KeyError):
            mentioned = []
        if mentioned:
            payload["mentionedJid"] = mentioned
    _request_json(
        f"{base}/send/text",
        {"apikey": api_key},
        payload,
    )


def _cloud_error_details(error: HTTPError) -> Tuple[Optional[int], str]:
    try:
        body = json.loads(error.read().decode("utf-8", errors="replace"))
        detail = body.get("error") or {}
        return detail.get("code"), str(detail.get("message") or "")
    except (ValueError, AttributeError):
        return None, ""


def _safe_error_message(message: str, config: Dict[str, object]) -> str:
    safe = message
    for key in (
        "WHATSAPP_TOKEN",
        "EVOLUTION_API_KEY",
        "REPORT_WHATSAPP_TO",
        "WHATSAPP_PHONE_NUMBER_ID",
    ):
        value = str(config.get(key) or "")
        if value:
            safe = safe.replace(value, "[oculto]")
    # A resposta de terceiros pode ecoar outro número; nunca o logamos completo.
    safe = re.sub(r"\d{8,}", "[número oculto]", safe)
    return safe[:240]


def main() -> int:
    parser = argparse.ArgumentParser(description="Envia o relatório Meta no WhatsApp")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="imprime a mensagem exata sem conectar",
    )
    args = parser.parse_args()

    try:
        config, missing = load_config()
    except (OSError, ValueError) as exc:
        print(f"❌ Configuração de WhatsApp inválida: {exc}", file=sys.stderr)
        print(f"   Corrija {CONFIG_PATH}. Nada foi enviado.", file=sys.stderr)
        return 2
    if missing:
        provider = config["REPORT_WHATSAPP_PROVIDER"]
        print(
            f"❌ Configuração incompleta para o provider {provider}. Chaves ausentes:",
            file=sys.stderr,
        )
        for key in missing:
            print(f"   - {key}", file=sys.stderr)
        print(
            f"   Preencha {CONFIG_PATH} ou defina as chaves no ambiente. "
            "Nada foi enviado.",
            file=sys.stderr,
        )
        return 2

    try:
        report = assemble_report(window=config.get("REPORT_WINDOW"))
        message = render_whatsapp(report)
    except (OSError, ValueError) as exc:
        print(f"❌ Não foi possível montar o relatório: {exc}", file=sys.stderr)
        return 3

    masked = mask_number(str(config["REPORT_WHATSAPP_TO"]))
    if args.dry_run:
        print("=== DRY RUN: nenhuma conexão será aberta ===")
        print(f"Provider: {config['REPORT_WHATSAPP_PROVIDER']}")
        print(f"Destino: {masked}")
        print("--- mensagem ---")
        print(message)
        return 0

    try:
        if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
            send_cloud_api(config, message)
        else:
            send_evolution(config, message)
    except HTTPError as exc:
        if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
            code, detail = _cloud_error_details(exc)
            print(
                f"❌ WhatsApp Cloud API recusou o envio (HTTP {exc.code}"
                + (f", código {code}" if code else "")
                + ").",
                file=sys.stderr,
            )
            if detail:
                print(
                    "   " + _safe_error_message(detail, config),
                    file=sys.stderr,
                )
            # Texto livre é bloqueado pela Meta após 24h; traduzimos o erro para
            # não parecer falha de token ou defeito do agendamento.
            if code == 131047:
                print(
                    "   A janela de atendimento de 24h está fechada. Para texto livre, "
                    "mande um “oi” ao número da Cloud API e tente de novo; para envio "
                    "diário sem esse passo, use um template aprovado pela Meta.",
                    file=sys.stderr,
                )
            else:
                print(
                    "   Se já passaram 24h desde a última mensagem do destinatário, "
                    "texto livre não é entregue. Reabra a janela com um “oi” ou use "
                    "um template aprovado pela Meta.",
                    file=sys.stderr,
                )
        else:
            print(
                f"❌ Evolution API recusou o envio (HTTP {exc.code}).",
                file=sys.stderr,
            )
            print(
                "   Confira EVOLUTION_URL, EVOLUTION_INSTANCE e a conexão da instância.",
                file=sys.stderr,
            )
        return 4
    except URLError as exc:
        print(
            f"❌ Não foi possível conectar ao provider para o destino {masked}.",
            file=sys.stderr,
        )
        print(
            "   Confira a URL, a internet e se o serviço está disponível.",
            file=sys.stderr,
        )
        return 5
    except (OSError, ValueError):
        print(
            f"❌ Falha local ao enviar para o destino {masked}. Nada foi exposto.",
            file=sys.stderr,
        )
        return 5

    print(f"✅ Relatório enviado no WhatsApp para {masked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
