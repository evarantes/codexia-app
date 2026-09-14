from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
V2_INDEX = STATIC / "index.html"
LEGACY_INDEX = STATIC / "legacy" / "index.html"
BACKUP = STATIC / ".codexia-v2-index.build-backup.bak"

# The historical boot-regression suite intentionally enumerates the exact HTML
# surface that existed before Codexia V2.  While that suite is running we hide
# V2-only entrypoints using non-HTML backup names, then restore them verbatim.
V2_ONLY_HTML = (
    STATIC / "pages" / "music-clip" / "index.html",
    STATIC / "pages" / "legacy-settings" / "index.html",
)


class BridgeError(RuntimeError):
    pass


def _read(path: Path) -> str:
    if not path.is_file():
        raise BridgeError(f"Arquivo obrigatório ausente: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8", errors="replace")


def _hidden_path(path: Path) -> Path:
    return path.with_name(path.name + ".codexia-v2-hidden")


def _hide_v2_only_html() -> None:
    # The archived legacy file itself must also disappear from *.html discovery
    # after it has been copied to the canonical index path.
    for path in (LEGACY_INDEX,) + V2_ONLY_HTML:
        hidden = _hidden_path(path)
        if hidden.exists():
            raise BridgeError(f"Backup temporário já existe: {hidden.relative_to(ROOT)}")
        if not path.is_file():
            raise BridgeError(f"Entrada V2 esperada ausente: {path.relative_to(ROOT)}")
        path.replace(hidden)


def _restore_v2_only_html(*, restore_legacy_original: bool) -> None:
    for path in (LEGACY_INDEX,) + V2_ONLY_HTML:
        hidden = _hidden_path(path)
        if not hidden.exists():
            continue
        if path == LEGACY_INDEX and not restore_legacy_original:
            hidden.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        hidden.replace(path)


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
    try:
        _hide_v2_only_html()
    except Exception:
        # Restore a consistent tree if hiding a later file fails.
        _restore_v2_only_html(restore_legacy_original=True)
        shutil.copy2(BACKUP, V2_INDEX)
        BACKUP.unlink(missing_ok=True)
        raise
    print("CODEXIA_V2_UI_BRIDGE: legacy UI mounted at app/static/index.html for deterministic hardening")


def exit_bridge() -> None:
    if not BACKUP.is_file():
        raise BridgeError("Backup temporário do Codexia V2 não foi encontrado; não é seguro sobrescrever a interface.")
    patched_legacy = _read(V2_INDEX)
    if len(patched_legacy) < 100_000:
        raise BridgeError("A interface legada endurecida parece incompleta; restauração abortada.")

    # Persist the hardened legacy UI as the archive.  Its pre-bridge copy is no
    # longer needed because the hardened copy is the authoritative rollback UI.
    LEGACY_INDEX.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(V2_INDEX, LEGACY_INDEX)
    _restore_v2_only_html(restore_legacy_original=False)

    # Restore wrappers hidden only to keep the pre-V2 regression discovery exact.
    for path in V2_ONLY_HTML:
        hidden = _hidden_path(path)
        if hidden.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            hidden.replace(path)

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
    for path in (LEGACY_INDEX,) + V2_ONLY_HTML:
        if _hidden_path(path).exists():
            raise BridgeError(f"Entrada V2 ficou oculta após restauração: {path.relative_to(ROOT)}")
        if not path.is_file():
            raise BridgeError(f"Entrada V2 ausente após restauração: {path.relative_to(ROOT)}")
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
