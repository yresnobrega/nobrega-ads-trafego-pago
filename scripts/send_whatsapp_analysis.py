#!/usr/bin/env python3
"""Envia texto livre (ex: a análise do meta-performance-analyzer) pro destino
de WhatsApp configurado em meta.env — inclusive grupo (@g.us). Reaproveita a
mesma configuração e os mesmos emissores (Cloud API / Evolution) do
relatório diário, sem duplicar lógica de envio nem de config."""
import argparse
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_formatter import WHATSAPP_LIMIT
from send_whatsapp_report import (
    CONFIG_PATH,
    _cloud_error_details,
    _safe_error_message,
    load_config,
    mask_number,
    send_cloud_api,
    send_evolution,
)


def _read_message(args) -> str:
    if args.message is not None:
        return args.message
    if args.message_file is not None:
        return Path(args.message_file).read_text(encoding="utf-8")
    data = sys.stdin.read()
    if not data.strip():
        print("❌ Nenhuma mensagem informada (use --message, --message-file ou stdin).", file=sys.stderr)
        sys.exit(2)
    return data


# Nunca corta conteúdo pra caber — se passar do limite, divide em várias
# mensagens enviadas em sequência (pedido explícito do aluno em 2026-09-22:
# "nunca envie pela metade"). Cada pedaço reserva espaço pro marcador
# "(i/N)" no fim, pra continuar dentro do WHATSAPP_LIMIT testado.
_PART_MARKER_MARGIN = 12


def _split_on(text: str, sep: str, limit: int) -> list[str]:
    """Quebra `text` em pedaços <= limit, preferindo cortar em `sep`."""
    if len(text) <= limit:
        return [text] if text else []
    parts = text.split(sep)
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = (current + sep + part) if current else part
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(part) <= limit:
            current = part
        else:
            # pedaço isolado ainda maior que o limite — corte duro como
            # último recurso (não há separador natural menor pra usar).
            for i in range(0, len(part), limit):
                chunks.append(part[i : i + limit])
    if current:
        chunks.append(current)
    return chunks


def split_message(message: str, limit: int = WHATSAPP_LIMIT) -> list[str]:
    """Divide `message` em pedaços que cabem no limite do WhatsApp, sem
    truncar conteúdo. Tenta manter blocos coesos: primeiro por parágrafo
    (linha em branco), depois por linha, só quebrando no meio de uma linha
    como último recurso."""
    message = message.strip()
    if not message:
        return []
    if len(message) <= limit:
        return [message]

    effective_limit = limit - _PART_MARKER_MARGIN
    chunks: list[str] = []
    for block in _split_on(message, "\n\n", effective_limit):
        if len(block) <= effective_limit:
            chunks.append(block)
        else:
            chunks.extend(_split_on(block, "\n", effective_limit))

    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"{c}\n\n_({i}/{total})_" for i, c in enumerate(chunks, start=1)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Envia uma mensagem de texto livre pro WhatsApp (destino de meta.env) "
                    "— usado pra disparar análises pro grupo de relatórios."
    )
    parser.add_argument("--message", help="texto da mensagem")
    parser.add_argument("--message-file", help="arquivo com o texto da mensagem")
    parser.add_argument("--dry-run", action="store_true", help="mostra a mensagem exata sem enviar")
    args = parser.parse_args()

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

    parts = split_message(_read_message(args))
    masked = mask_number(str(config["REPORT_WHATSAPP_TO"]))

    if not parts:
        print("❌ Nenhuma mensagem informada (use --message, --message-file ou stdin).", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Destino: {masked}")
        print(f"Partes: {len(parts)} (limite {WHATSAPP_LIMIT} caracteres cada)")
        for i, part in enumerate(parts, start=1):
            print(f"--- parte {i}/{len(parts)} ({len(part)} chars) ---")
            print(part)
        return 0

    for i, part in enumerate(parts, start=1):
        try:
            if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
                send_cloud_api(config, part)
            else:
                send_evolution(config, part)
        except HTTPError as exc:
            if config["REPORT_WHATSAPP_PROVIDER"] == "cloud_api":
                code, detail = _cloud_error_details(exc)
                print(
                    f"❌ WhatsApp Cloud API recusou o envio (HTTP {exc.code}"
                    + (f", código {code}" if code else "") + f") na parte {i}/{len(parts)}.",
                    file=sys.stderr,
                )
                if detail:
                    print("   " + _safe_error_message(detail, config), file=sys.stderr)
            else:
                print(f"❌ Evolution API recusou o envio (HTTP {exc.code}) na parte {i}/{len(parts)}.", file=sys.stderr)
            return 4
        except URLError:
            print(f"❌ Não foi possível conectar ao provider (parte {i}/{len(parts)}).", file=sys.stderr)
            return 4
        except (OSError, ValueError):
            print(f"❌ Falha local ao enviar (parte {i}/{len(parts)}). Nada foi exposto.", file=sys.stderr)
            return 4
        if i < len(parts):
            time.sleep(2)  # mantém ordem de chegada no WhatsApp

    if len(parts) > 1:
        print(f"✅ Mensagem enviada em {len(parts)} partes no WhatsApp para {masked}.")
    else:
        print(f"✅ Mensagem enviada no WhatsApp para {masked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
