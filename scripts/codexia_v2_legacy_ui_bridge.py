from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
V2_INDEX = STATIC / "index.html"
LEGACY_INDEX = STATIC / "legacy" / "index.html"
BACKUP = STATIC / ".codexia-v2-index.build-backup.html"


class BridgeError(RuntimeError):
    pass


def _read(path: Path) -> str:
    if not path.is_file():
        raise BridgeError(f"Arquivo obrigatório ausente: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8", errors="replace")


def enter() -> None:
    if BACKUP.exists():
        raise BridgeError(
            "Backup temporário do Codexia V2 já existe. Execute o modo exit ou remova o artefato de uma execução interrompida."
        )
    v2 = _read(V2_INDEX)
    legacy = _read(LEGACY_INDEX)
    if "Meta Monetização" not in v2:
        raise BridgeError("app/static/index.html não parece ser o shell Codexia V2 esperado.")
    if len(legacy) < 100_000:
        raise BridgeError("app/static/legacy/index.html parece incompleto; hardening legado foi bloqueado por segurança.")
    shutil.copy2(V2_INDEX, BACKUP)
    shutil.copy2(LEGACY_INDEX, V2_INDEX)
    print("CODEXIA_V2_UI_BRIDGE: legacy UI mounted at app/static/index.html for deterministic hardening")


def exit_bridge() -> None:
    if not BACKUP.is_file():
        raise BridgeError("Backup temporário do Codexia V2 não foi encontrado; não é seguro sobrescrever a interface.")
    patched_legacy = _read(V2_INDEX)
    if len(patched_legacy) < 100_000:
        raise BridgeError("A interface legada endurecida parece incompleta; restauração abortada.")
    shutil.copy2(V2_INDEX, LEGACY_INDEX)
    shutil.copy2(BACKUP, V2_INDEX)
    BACKUP.unlink(missing_ok=True)
    restored = _read(V2_INDEX)
    if "Meta Monetização" not in restored:
        raise BridgeError("Falha ao restaurar o shell Codexia V2 após hardening legado.")
    print("CODEXIA_V2_UI_BRIDGE: patched legacy UI archived and Codexia V2 shell restored")


def check() -> None:
    v2 = _read(V2_INDEX)
    legacy = _read(LEGACY_INDEX)
    if "Meta Monetização" not in v2:
        raise BridgeError("Shell V2 ausente da interface principal.")
    if len(legacy) < 100_000:
        raise BridgeError("Interface legada arquivada está incompleta.")
    if BACKUP.exists():
        raise BridgeError("Backup temporário remanescente indica que o bridge não foi finalizado.")
    print("CODEXIA_V2_UI_BRIDGE: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compatibilidade entre hardening legado e o shell Codexia V2.")
    parser.add_argument("mode", choices=("enter", "exit", "check"))
    args = parser.parse_args()
    try:
        if args.mode == "enter":
            enter()
        elif args.mode == "exit":
            exit_bridge()
        else:
            check()
    except BridgeError as exc:
        print(f"CODEXIA_V2_UI_BRIDGE_ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
