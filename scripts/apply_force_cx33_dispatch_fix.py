from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE = ROOT / "app" / "routers" / "youtube.py"
TESTS = ROOT / "tests" / "test_cx33_retry_dispatch_regression.py"

text = YOUTUBE.read_text(encoding="utf-8")

old_enqueue = "    if use_rq and worker_online:\n"
new_enqueue = (
    "    # Um worker dedicado registrado sempre tem prioridade. Uma variável antiga\n"
    "    # USE_RQ_FOR_VIDEO_GENERATION=false não pode desviar produção pesada para\n"
    "    # o app principal quando o CX33 está disponível.\n"
    "    if worker_online:\n"
)
if old_enqueue in text:
    text = text.replace(old_enqueue, new_enqueue, 1)
elif "if worker_online:" not in text:
    raise SystemExit("Não encontrei o gate de enqueue esperado")

old_inline = '''    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")\n    if allow_inline_raw is None or not str(allow_inline_raw).strip():\n        allow_inline = True\n    else:\n        allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}\n'''
new_inline = '''    allow_inline_raw = os.getenv("ALLOW_INLINE_VIDEO_GENERATION")\n    # Fail-closed por padrão: execução pesada local só existe quando um ambiente\n    # de desenvolvimento/homologação habilita explicitamente esta variável.\n    if allow_inline_raw is None or not str(allow_inline_raw).strip():\n        allow_inline = False\n    else:\n        allow_inline = str(allow_inline_raw).strip().lower() in {"1", "true", "yes", "on"}\n'''
count = text.count(old_inline)
if count:
    text = text.replace(old_inline, new_inline)
elif "allow_inline = False" not in text:
    raise SystemExit("Não encontrei o fallback inline esperado")

YOUTUBE.write_text(text, encoding="utf-8")

suite = TESTS.read_text(encoding="utf-8")
marker = "    def test_diagnostic_panel_renders_checks_and_task_message(self):\n"
addition = r'''    def test_registered_cx33_wins_even_when_legacy_use_rq_flag_is_false(self):
        enqueued = []
        updates = []

        class Queue:
            def enqueue(self, *args, **kwargs):
                enqueued.append((args, kwargs))

        class ForbiddenThread:
            def __init__(self, *args, **kwargs):
                raise AssertionError("worker CX33 online não pode cair para thread local")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "APP_ENV": "development",
                "USE_RQ_FOR_VIDEO_GENERATION": "false",
            }, clear=True))
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "rq_queue", Queue()))
            stack.enter_context(patch.object(youtube, "_rq_workers_online", lambda: True))
            stack.enter_context(patch.object(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True}))
            stack.enter_context(patch.object(youtube, "_requires_isolated_video_process", lambda payload, task_id: False))
            stack.enter_context(patch.object(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload))
            stack.enter_context(patch.object(youtube, "get_task", lambda task_id: {"progress": 1}))
            stack.enter_context(patch.object(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs)))
            stack.enter_context(patch.object(youtube.threading, "Thread", ForbiddenThread))

            youtube._dispatch_video_generation_task({"duration": 1}, "fresh-task")

        self.assertEqual(len(enqueued), 1)
        self.assertTrue(updates)
        self.assertEqual(updates[-1]["status"], "processing")
        self.assertIn("CX33", updates[-1]["message"])

    def test_inline_video_execution_is_opt_in_when_worker_is_offline(self):
        updates = []

        class ForbiddenThread:
            def __init__(self, *args, **kwargs):
                raise AssertionError("execução local deve ser opt-in")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "APP_ENV": "development",
                "USE_RQ_FOR_VIDEO_GENERATION": "false",
            }, clear=True))
            stack.enter_context(patch.object(youtube, "conn", object()))
            stack.enter_context(patch.object(youtube, "_rq_workers_online", lambda: False))
            stack.enter_context(patch.object(youtube, "_series_resource_preflight", lambda payload, task_id: {"allowed": True}))
            stack.enter_context(patch.object(youtube, "_requires_isolated_video_process", lambda payload, task_id: False))
            stack.enter_context(patch.object(youtube, "_maybe_enable_render_only_flags", lambda payload, task_id: payload))
            stack.enter_context(patch.object(youtube, "get_task", lambda task_id: {"progress": 23}))
            stack.enter_context(patch.object(youtube, "update_task", lambda task_id, **kwargs: updates.append(kwargs)))
            stack.enter_context(patch.object(youtube.threading, "Thread", ForbiddenThread))

            youtube._dispatch_video_generation_task({"duration": 1}, "offline-task")

        self.assertTrue(updates)
        self.assertEqual(updates[-1]["status"], "pending")
        self.assertEqual(updates[-1]["progress"], 23)
        self.assertIn("execução local desativada", updates[-1]["message"])

'''
if "test_registered_cx33_wins_even_when_legacy_use_rq_flag_is_false" not in suite:
    if marker not in suite:
        raise SystemExit("Marcador de inserção dos testes não encontrado")
    suite = suite.replace(marker, addition + marker, 1)
TESTS.write_text(suite, encoding="utf-8")

print("CX33 dispatch patch aplicado com sucesso")
