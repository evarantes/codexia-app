from __future__ import annotations

from pathlib import Path
import re

YOUTUBE = Path("app/routers/youtube.py")
TEST = Path("tests/test_cx33_retry_dispatch_regression.py")

text = YOUTUBE.read_text(encoding="utf-8")
pattern = re.compile(r"def _rq_workers_online\(\) -> bool:\n.*?\n(?=def _is_video_factory_busy\(\) -> bool:)", re.S)
replacement = '''def _rq_workers_online() -> bool:\n    \"\"\"Retorna True quando o RQ registra worker ativo para a fila de vídeo.\n\n    Não use um corte fixo curto em ``last_heartbeat``. Enquanto o worker está\n    ocioso, o RQ pode manter o registro válido por vários minutos entre ciclos\n    de manutenção. O próprio registro/TTL do RQ é a fonte de verdade para\n    monitoramento; em produção continuamos fail-closed se nenhum worker estiver\n    registrado.\n    \"\"\"\n    if not conn or not RQ_AVAILABLE or Worker is None:\n        return False\n    try:\n        if rq_queue is not None:\n            try:\n                return Worker.count(queue=rq_queue) > 0\n            except TypeError:\n                pass\n            except Exception:\n                pass\n\n        try:\n            return Worker.count(connection=conn) > 0\n        except TypeError:\n            try:\n                return Worker.count(conn) > 0\n            except Exception:\n                pass\n        except Exception:\n            pass\n\n        # Compatibilidade defensiva com versões/classes customizadas de RQ.\n        try:\n            workers = list(Worker.all(connection=conn))\n        except TypeError:\n            workers = list(Worker.all(conn))\n        return bool(workers)\n    except Exception:\n        return False\n\n'''
new_text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f"expected one _rq_workers_online block, got {count}")
YOUTUBE.write_text(new_text, encoding="utf-8")

text = TEST.read_text(encoding="utf-8")
old = '''    def test_rq_worker_online_accepts_timezone_aware_heartbeat(self):\n        class FakeWorker:\n            @classmethod\n            def all(cls, *args, **kwargs):\n                return [_WorkerRow(datetime.now(timezone.utc))]\n\n        with ExitStack() as stack:\n            stack.enter_context(patch.object(youtube, \"conn\", object()))\n            stack.enter_context(patch.object(youtube, \"RQ_AVAILABLE\", True))\n            stack.enter_context(patch.object(youtube, \"Worker\", FakeWorker))\n            self.assertTrue(youtube._rq_workers_online())\n'''
new = '''    def test_rq_worker_online_uses_rq_registration_even_with_idle_heartbeat(self):\n        class FakeWorker:\n            @classmethod\n            def count(cls, *args, **kwargs):\n                return 1\n\n            @classmethod\n            def all(cls, *args, **kwargs):\n                return [_WorkerRow(datetime.now(timezone.utc))]\n\n        with ExitStack() as stack:\n            stack.enter_context(patch.object(youtube, \"conn\", object()))\n            stack.enter_context(patch.object(youtube, \"RQ_AVAILABLE\", True))\n            stack.enter_context(patch.object(youtube, \"Worker\", FakeWorker))\n            self.assertTrue(youtube._rq_workers_online())\n\n    def test_rq_worker_offline_when_rq_has_no_registered_workers(self):\n        class FakeWorker:\n            @classmethod\n            def count(cls, *args, **kwargs):\n                return 0\n\n        with ExitStack() as stack:\n            stack.enter_context(patch.object(youtube, \"conn\", object()))\n            stack.enter_context(patch.object(youtube, \"RQ_AVAILABLE\", True))\n            stack.enter_context(patch.object(youtube, \"Worker\", FakeWorker))\n            self.assertFalse(youtube._rq_workers_online())\n'''
if old not in text:
    raise SystemExit("target test block not found")
TEST.write_text(text.replace(old, new, 1), encoding="utf-8")
