from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app/routers/youtube.py"
MARKER = "CODEXIA_STALE_FACTORY_LOCK_RECOVERY_V1"


class PatchError(RuntimeError):
    pass


HELPERS = r'''
# CODEXIA_STALE_FACTORY_LOCK_RECOVERY_V1
# O lock global do renderer vive no Redis por até 4h. Se o container que o
# adquiriu for substituído durante um deploy, o token pode sobreviver sem dono
# e bloquear a fila mesmo com 0 tarefas em execução. A recuperação abaixo é
# fail-closed: só apaga um lock antigo quando não existe nenhum executor/job
# vivo conhecido e o próprio TTL prova que não é um lock recém-adquirido.
_FACTORY_LOCK_RUNTIME_SECONDS = 4 * 60 * 60
_FACTORY_LOCK_STALE_MIN_AGE_SECONDS = 10 * 60
_FACTORY_LOCK_SAFE_DELETE_LUA = """
local ttl = redis.call('ttl', KEYS[1])
if ttl >= 0 and ttl <= tonumber(ARGV[1]) then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _factory_lock_has_live_work() -> bool:
    db = SessionLocal()
    try:
        # Fila principal/legada também compartilha o lock global.
        if db.query(Job).filter(Job.status == "processing").first() is not None:
            return True

        # Uma VideoTask pode estar ainda em pending durante a janela entre o
        # acquire do execution lease e a transição para processing. Por isso
        # consulte os três estados e use o lease como fonte de verdade.
        active_rows = (
            db.query(VideoTask)
            .filter(VideoTask.status.in_(["pending", "processing", "pause_requested"]))
            .order_by(VideoTask.updated_at.desc(), VideoTask.created_at.desc())
            .limit(150)
            .all()
        )
        for row in active_rows:
            if _task_executor_is_alive(str(row.id)):
                return True
    except Exception:
        # Em dúvida, preserve o lock.
        return True
    finally:
        try:
            db.close()
        except Exception:
            pass

    # Um job RQ ativo pode ser de shorts/rotina auxiliar e ainda compartilhar
    # o mesmo lock. Se houver qualquer job corrente, não faça autocorreção.
    if conn is not None and RQ_AVAILABLE and Worker is not None:
        try:
            try:
                workers = list(Worker.all(connection=conn))
            except TypeError:
                workers = list(Worker.all(conn))
            for worker in workers:
                current_job_id = None
                try:
                    current_job_id = worker.get_current_job_id()
                except Exception:
                    try:
                        current_job = worker.get_current_job()
                        current_job_id = getattr(current_job, "id", None) if current_job is not None else None
                    except Exception:
                        current_job_id = None
                if current_job_id:
                    return True
        except Exception:
            # Se não conseguimos provar que os workers estão ociosos, preserve.
            return True
    return False


def _recover_stale_factory_lock_if_safe() -> bool:
    if conn is None or _cancel_all_active():
        return False
    try:
        ttl = int(conn.ttl(FACTORY_LOCK_KEY))
    except Exception:
        return False

    # -2 = chave ausente; -1 = sem expiração. Ambos não devem ser apagados aqui.
    if ttl < 0:
        return False
    recoverable_ttl = int(_FACTORY_LOCK_RUNTIME_SECONDS - _FACTORY_LOCK_STALE_MIN_AGE_SECONDS)
    if ttl > recoverable_ttl:
        return False
    if _factory_lock_has_live_work():
        return False

    # Revalida o TTL de forma atômica no mesmo comando que remove a chave. Se
    # o lock tiver sido renovado/substituído entre as leituras, nada é apagado.
    try:
        deleted = int(conn.eval(
            _FACTORY_LOCK_SAFE_DELETE_LUA,
            1,
            FACTORY_LOCK_KEY,
            recoverable_ttl,
        ) or 0)
    except Exception:
        return False
    if deleted <= 0:
        return False
    try:
        print(
            "Lock órfão da fábrica removido com segurança; "
            f"ttl_restante={ttl}s, idade_minima={_FACTORY_LOCK_STALE_MIN_AGE_SECONDS}s"
        )
    except Exception:
        pass
    return True

'''


KICK_OLD = '''        if _is_video_factory_busy():
            return None'''
KICK_NEW = '''        if _is_video_factory_busy():
            # CODEXIA_STALE_FACTORY_LOCK_RECOVERY_V1
            # Um deploy pode matar o dono do Redis lock sem remover a chave.
            # Antes de deixar a fila parada por horas, tente a recuperação
            # fail-closed; só prossiga se o lock órfão foi realmente removido.
            if not _recover_stale_factory_lock_if_safe():
                return None'''


QUEUE_OLD = '''        factory_busy = bool(_is_video_factory_busy())
        task_ids = {str(row.id) for row in rows}'''
QUEUE_NEW = '''        # CODEXIA_STALE_FACTORY_LOCK_RECOVERY_V1
        # A UI consulta esta rota periodicamente. Se um lock antigo ficou órfão
        # após deploy, essa leitura também pode autocurar o lock e reativar a
        # mesma fila, sem criar nova tarefa nem alterar os ativos preservados.
        stale_factory_lock_recovered = _recover_stale_factory_lock_if_safe()
        factory_busy = bool(_is_video_factory_busy())
        if stale_factory_lock_recovered:
            _kick_story_video_task_queue_async()
        task_ids = {str(row.id) for row in rows}'''


def _insert_before_once(text: str, anchor: str, insertion: str, label: str) -> str:
    if MARKER in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"{label}: âncora esperada 1 vez, encontrada {count}")
    return text.replace(anchor, insertion + anchor, 1)


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: trecho esperado 1 vez, encontrado {count}")
    return text.replace(old, new, 1)


def patch_youtube(text: str) -> str:
    if MARKER not in text:
        text = _insert_before_once(
            text,
            "def _is_video_factory_busy() -> bool:\n",
            HELPERS,
            "helpers de lock órfão",
        )
    text = _replace_once(text, KICK_OLD, KICK_NEW, "autocura no kick da fila")
    text = _replace_once(text, QUEUE_OLD, QUEUE_NEW, "autocura na leitura da fila")
    return text


def apply() -> None:
    original = YOUTUBE.read_text(encoding="utf-8")
    transformed = patch_youtube(original)
    if patch_youtube(transformed) != transformed:
        raise PatchError("patch de stale factory lock não é idempotente")
    if transformed != original:
        YOUTUBE.write_text(transformed, encoding="utf-8")


def check() -> None:
    text = YOUTUBE.read_text(encoding="utf-8")
    required = (
        MARKER,
        "def _factory_lock_has_live_work()",
        "def _recover_stale_factory_lock_if_safe()",
        "_FACTORY_LOCK_STALE_MIN_AGE_SECONDS = 10 * 60",
        "db.query(Job).filter(Job.status == \"processing\")",
        "_task_executor_is_alive(str(row.id))",
        "worker.get_current_job_id()",
        "conn.eval(",
        "stale_factory_lock_recovered = _recover_stale_factory_lock_if_safe()",
        "if not _recover_stale_factory_lock_if_safe():",
    )
    missing = [token for token in required if token not in text]
    if missing:
        raise PatchError("autocura de lock órfão incompleta: " + ", ".join(missing))
    compile(text, str(YOUTUBE), "exec")


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
        print(f"ERRO STALE FACTORY LOCK RECOVERY: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
