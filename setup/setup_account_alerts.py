#!/usr/bin/env python3
"""Instalador opt-in — alerta imediato no WhatsApp quando uma conta Meta para
(pagamento recusado, restrição, revisão, fechamento etc). Roda a cada 15min,
separado do relatório periódico (com.zxlab.meta-report-whatsapp)."""

import filecmp
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
OPERACAO = Path.home() / ".operacao-ia"
SCRIPT_DST_DIR = OPERACAO / "scripts" / "meta"
META_ENV = OPERACAO / "config" / "meta.env"
LOG_DIR = OPERACAO / "logs"
PLIST_NAME = "com.zxlab.meta-account-alert.plist"
PLIST_SRC = REPO_ROOT / "launchagents" / PLIST_NAME
PLIST_DST = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
SCRIPT_NAMES = ("check_account_alerts.py", "fetch_metrics.py", "send_whatsapp_report.py", "report_formatter.py")


def _copy_script(name: str) -> Tuple[bool, Path]:
    src = REPO_ROOT / "scripts" / name
    dst = SCRIPT_DST_DIR / name
    if not src.is_file():
        raise FileNotFoundError(f"Arquivo ausente no repositório: {src}")
    if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
        dst.chmod(0o700)
        return False, dst
    shutil.copy2(src, dst)
    dst.chmod(0o700)
    return True, dst


def _install_plist() -> bool:
    if not PLIST_SRC.is_file():
        print(f"❌ Plist ausente no repositório: {PLIST_SRC}")
        return False
    try:
        PLIST_DST.parent.mkdir(parents=True, exist_ok=True)
        content = (
            PLIST_SRC.read_text(encoding="utf-8")
            .replace("{HOME}", str(Path.home()))
            .replace("{PYTHON}", sys.executable)
        )
        PLIST_DST.write_text(content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(PLIST_DST)], capture_output=True)
        result = subprocess.run(
            ["launchctl", "load", str(PLIST_DST)],
            capture_output=True,
            text=True,
        )
    except (OSError, FileNotFoundError) as exc:
        print(f"❌ Não foi possível instalar o LaunchAgent: {exc}")
        return False
    if result.returncode != 0:
        print(f"❌ launchctl não carregou {PLIST_NAME}: {result.stderr.strip()}")
        return False
    print(f"✅ LaunchAgent instalado/atualizado: {PLIST_DST}")
    return True


def main() -> int:
    if platform.system() != "Darwin":
        print(f"❌ Este agendamento usa LaunchAgent do macOS; sistema: {platform.system()}.")
        return 1

    SCRIPT_DST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for name in SCRIPT_NAMES:
            changed, dst = _copy_script(name)
            prefix = "✅ Instalado/atualizado" if changed else "⏭️  Já estava idêntico"
            print(f"{prefix}: {dst}")
    except (OSError, FileNotFoundError) as exc:
        print(f"❌ Não foi possível copiar os scripts: {exc}")
        return 1

    if META_ENV.exists():
        try:
            os.chmod(META_ENV, 0o600)
        except OSError as exc:
            print(f"❌ Não foi possível aplicar permissão 600 em {META_ENV}: {exc}")
            return 1
        print(f"✅ Permissão 600 confirmada em {META_ENV}")

    print("\n🔎 Rodando uma checagem agora (sem esperar o agendamento)...")
    check = subprocess.run(
        [sys.executable, str(SCRIPT_DST_DIR / "check_account_alerts.py")],
        check=False,
    )
    if check.returncode != 0:
        print(
            f"\n❌ A checagem falhou. Corrija o problema indicado acima e rode este instalador de novo."
        )
        return check.returncode

    if not _install_plist():
        return 1
    print("✅ Monitor de status de conta ativo — checa a cada 15min e avisa no WhatsApp na hora que mudar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
