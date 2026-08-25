from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
INDEX = ROOT / "app/static/index.html"
MARKER = "CODEXIA_RETRY_PLAN_CONFIRMATION_STABILITY_V1"


class PatchError(RuntimeError):
    pass


BACKEND_OLD = "optimization_plan, optimization_materials = _intelligent_retry_visual_materials(task_id, payload)"
BACKEND_NEW = """# CODEXIA_RETRY_PLAN_CONFIRMATION_STABILITY_V1
        # GET /retry-plan e POST /retry precisam assinar exatamente o mesmo
        # estado persistido. O payload local do retry já pode ter sido enriquecido
        # por guards de recuperação e não deve participar da validação do hash.
        optimization_plan, optimization_materials = _intelligent_retry_visual_materials(task_id)"""

PLAN_ERROR_OLD = "throw new Error(planData.detail || planData.message || 'Falha ao analisar alternativas de recuperação.');"
PLAN_ERROR_NEW = """const planDetail = planData && planData.detail;
                            const planDetailMessage = (planDetail && typeof planDetail === 'object')
                                ? (planDetail.message || planDetail.code || JSON.stringify(planDetail))
                                : planDetail;
                            throw new Error(planDetailMessage || planData.message || 'Falha ao analisar alternativas de recuperação.');"""

RETRY_ERROR_OLD = "throw new Error(data.detail || data.message || 'Falha ao recolocar a tarefa na fila.');"
RETRY_ERROR_NEW = """const retryDetail = data && data.detail;
                            const retryDetailMessage = (retryDetail && typeof retryDetail === 'object')
                                ? (retryDetail.message || retryDetail.code || JSON.stringify(retryDetail))
                                : retryDetail;
                            throw new Error(retryDetailMessage || data.message || 'Falha ao recolocar a tarefa na fila.');"""

OPEN_RECOVERABLE_OLD = """                    await this.pollStoryTask(taskId);
                    await this.diagnoseStoryTask();"""
OPEN_RECOVERABLE_NEW = """                    await this.pollStoryTask(taskId);
                    // pollStoryTask limpa o task_id quando encontra CANCELLED.
                    // Para diagnóstico de um item arquivado, restaure explicitamente
                    // a referência antes de chamar a rota read-only de diagnóstico.
                    this.ytStoryTaskId = taskId;
                    try { localStorage.setItem('ytStoryTaskId', taskId); } catch (e) {}
                    await this.diagnoseStoryTask();"""


def _replace_once_or_already(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho, encontrado {count}")
    return text.replace(old, new, 1)


def _replace_all_or_already(text: str, old: str, new: str, label: str) -> str:
    """Substitui todas as cópias equivalentes sem enfraquecer o fail-closed.

    Alguns hardenings anteriores podem materializar o mesmo handler de retry-plan
    em mais de um fluxo da UI durante o build. Todas essas ocorrências precisam
    exibir o detail estruturado corretamente. Zero ocorrências continua sendo erro.
    """
    if old not in text:
        if new in text:
            return text
        raise PatchError(f"{label}: esperado ao menos 1 trecho, encontrado 0")
    return text.replace(old, new)


def patch_youtube(text: str) -> str:
    if BACKEND_NEW in text:
        return text
    return _replace_once_or_already(
        text,
        BACKEND_OLD,
        BACKEND_NEW,
        "hash estável do plano de retry",
    )


def patch_index(text: str) -> str:
    text = _replace_all_or_already(
        text,
        PLAN_ERROR_OLD,
        PLAN_ERROR_NEW,
        "mensagem estruturada do retry-plan",
    )
    text = _replace_once_or_already(
        text,
        RETRY_ERROR_OLD,
        RETRY_ERROR_NEW,
        "mensagem estruturada do retry",
    )
    text = _replace_once_or_already(
        text,
        OPEN_RECOVERABLE_OLD,
        OPEN_RECOVERABLE_NEW,
        "diagnóstico de tarefa cancelada",
    )
    if MARKER not in text:
        text = text.rstrip() + f"\n<!-- {MARKER} -->\n"
    return text


def apply() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    patched_youtube = patch_youtube(youtube)
    if patched_youtube != youtube:
        YOUTUBE.write_text(patched_youtube, encoding="utf-8")

    index = INDEX.read_text(encoding="utf-8")
    patched_index = patch_index(index)
    if patched_index != index:
        INDEX.write_text(patched_index, encoding="utf-8")


def check() -> None:
    youtube = YOUTUBE.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")

    if BACKEND_OLD in youtube:
        raise PatchError("POST /retry ainda recalcula o hash com payload mutável")
    required_youtube = (
        MARKER,
        "optimization_plan, optimization_materials = _intelligent_retry_visual_materials(task_id)",
    )
    missing = [token for token in required_youtube if token not in youtube]
    if missing:
        raise PatchError("backend de confirmação incompleto: " + ", ".join(missing))

    required_index = (
        MARKER,
        "const retryDetail = data && data.detail;",
        "const planDetail = planData && planData.detail;",
        "this.ytStoryTaskId = taskId;",
        "await this.diagnoseStoryTask();",
    )
    missing = [token for token in required_index if token not in index]
    if missing:
        raise PatchError("UI de retry/diagnóstico incompleta: " + ", ".join(missing))
    if PLAN_ERROR_OLD in index:
        raise PatchError("UI ainda contém handler legado de retry-plan")

    compile(youtube, str(YOUTUBE), "exec")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply()
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO RETRY PLAN CONFIRMATION STABILITY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
