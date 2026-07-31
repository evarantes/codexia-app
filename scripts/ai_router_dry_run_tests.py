import os
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ai_router import AIRouter, AICapability, AIOperationInProgress
from app.database import SessionLocal
from sqlalchemy import text


COUNTERS = {
    "text_total_calls": 0,
    "text_in_progress": 0,
    "text_cache_total_calls": 0,
    "text_cache_in_progress": 0,
    "trans_total_calls": 0,
    "trans_in_progress": 0,
    "trans_cache_total_calls": 0,
    "trans_cache_in_progress": 0,
}

DEFAULT_N = int(os.getenv("AI_ROUTER_TEST_N", "100") or "100")
DEFAULT_MAX_WORKERS = int(os.getenv("AI_ROUTER_TEST_MAX_WORKERS", "5") or "5")
DEFAULT_MAX_ROUNDS = int(os.getenv("AI_ROUTER_TEST_MAX_ROUNDS", "200") or "200")

def _warmup_schema(router: AIRouter) -> None:
    db = SessionLocal()
    try:
        router._ensure_schema(db)
        router.guardian.ensure_schema(db)
        db.commit()
    finally:
        db.close()


def _make_silence_wav(path: str, duration_sec: float = 0.5, sample_rate: int = 16000):
    frames = int(duration_sec * sample_rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frames)


def _retry_on_in_progress(fn, counter_key: str, max_wait_sec: float = 5.0):
    started = time.time()
    while True:
        try:
            return fn()
        except AIOperationInProgress:
            COUNTERS[counter_key] = int(COUNTERS.get(counter_key, 0) or 0) + 1
            if time.time() - started > max_wait_sec:
                raise
            time.sleep(0.1)

def _run_concurrent_with_rounds(*, n: int, max_workers: int, fn_builder, total_counter_key: str, in_progress_counter_key: str) -> list:
    pending = list(range(int(n)))
    out = [None] * int(n)
    rounds = 0
    while pending:
        rounds += 1
        if rounds > DEFAULT_MAX_ROUNDS:
            raise RuntimeError("timeout_rounds")
        with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
            futs = {ex.submit(fn_builder(i)): i for i in pending}
            new_pending = []
            for f in as_completed(futs):
                idx = futs[f]
                try:
                    out[idx] = f.result()
                except AIOperationInProgress:
                    COUNTERS[in_progress_counter_key] = int(COUNTERS.get(in_progress_counter_key, 0) or 0) + 1
                    new_pending.append(idx)
                except Exception:
                    raise
        pending = new_pending
        if pending:
            time.sleep(0.1)
    return out


def test_text(router: AIRouter):
    print("TEST: text 100 identical (idempotency)", flush=True)
    prompt = "Teste de roteamento e cache (dry-run)."
    capability = AICapability.SCRIPT_GENERATION

    def call():
        COUNTERS["text_total_calls"] = int(COUNTERS.get("text_total_calls", 0) or 0) + 1
        return router.generate_text(
            user_id=None,
            task_id="dry-run-task-1",
            video_id=None,
            capability=capability,
            prompt=prompt,
            system_prompt="Retorne um texto curto.",
            temperature=0.1,
            json_mode=False,
        )

    results = _run_concurrent_with_rounds(
        n=DEFAULT_N,
        max_workers=DEFAULT_MAX_WORKERS,
        fn_builder=lambda _i: call,
        total_counter_key="text_total_calls",
        in_progress_counter_key="text_in_progress",
    )
    assert all(isinstance(r, str) and r for r in results)

    idempotency_unique = _count_runs(scope_id="dry-run-task-1", capability=capability)
    COUNTERS["text_idempotency_unique_runs"] = int(idempotency_unique)
    COUNTERS["text_idempotency_blocked"] = int(COUNTERS["text_total_calls"]) - int(idempotency_unique)
    print("OK: text 100 identical", flush=True)


def test_text_cache(router: AIRouter):
    print("TEST: text cache 100 (cross-task)", flush=True)
    prompt = "Teste de roteamento e cache (dry-run)."
    capability = AICapability.SCRIPT_GENERATION

    def warm():
        COUNTERS["text_cache_total_calls"] = int(COUNTERS.get("text_cache_total_calls", 0) or 0) + 1
        return router.generate_text(
            user_id=None,
            task_id="dry-run-cache-text-warm",
            video_id=None,
            capability=capability,
            prompt=prompt,
            system_prompt="Retorne um texto curto.",
            temperature=0.1,
            json_mode=False,
        )

    _ = _retry_on_in_progress(warm, "text_cache_in_progress")

    def call(i: int):
        COUNTERS["text_cache_total_calls"] = int(COUNTERS.get("text_cache_total_calls", 0) or 0) + 1
        return router.generate_text(
            user_id=None,
            task_id=f"dry-run-cache-text-{i:03d}",
            video_id=None,
            capability=capability,
            prompt=prompt,
            system_prompt="Retorne um texto curto.",
            temperature=0.1,
            json_mode=False,
        )

    results = _run_concurrent_with_rounds(
        n=DEFAULT_N,
        max_workers=DEFAULT_MAX_WORKERS,
        fn_builder=lambda i: (lambda: call(i)),
        total_counter_key="text_cache_total_calls",
        in_progress_counter_key="text_cache_in_progress",
    )
    assert all(isinstance(r, str) and r for r in results)
    print("OK: text cache 100", flush=True)


def test_transcription(router: AIRouter):
    print("TEST: transcription 100 identical (idempotency)", flush=True)
    with tempfile.TemporaryDirectory() as td:
        audio_path = os.path.join(td, "silence.wav")
        _make_silence_wav(audio_path)

        def call():
            COUNTERS["trans_total_calls"] = int(COUNTERS.get("trans_total_calls", 0) or 0) + 1
            return router.transcribe_audio(
                user_id=None,
                task_id="dry-run-task-2",
                video_id=None,
                audio_path=audio_path,
                language="pt",
            )

        results = _run_concurrent_with_rounds(
            n=DEFAULT_N,
            max_workers=DEFAULT_MAX_WORKERS,
            fn_builder=lambda _i: call,
            total_counter_key="trans_total_calls",
            in_progress_counter_key="trans_in_progress",
        )
        assert all(isinstance(r, dict) for r in results)

        idempotency_unique = _count_runs(scope_id="dry-run-task-2", capability=AICapability.TRANSCRIPTION)
        COUNTERS["trans_idempotency_unique_runs"] = int(idempotency_unique)
        COUNTERS["trans_idempotency_blocked"] = int(COUNTERS["trans_total_calls"]) - int(idempotency_unique)
        print("OK: transcription 100 identical", flush=True)


def test_transcription_cache(router: AIRouter):
    print("TEST: transcription cache 100 (cross-task)", flush=True)
    with tempfile.TemporaryDirectory() as td:
        audio_path = os.path.join(td, "silence.wav")
        _make_silence_wav(audio_path)

        def warm():
            COUNTERS["trans_cache_total_calls"] = int(COUNTERS.get("trans_cache_total_calls", 0) or 0) + 1
            return router.transcribe_audio(
                user_id=None,
                task_id="dry-run-cache-trans-warm",
                video_id=None,
                audio_path=audio_path,
                language="pt",
            )

        _ = _retry_on_in_progress(warm, "trans_cache_in_progress")

        def call(i: int):
            COUNTERS["trans_cache_total_calls"] = int(COUNTERS.get("trans_cache_total_calls", 0) or 0) + 1
            return router.transcribe_audio(
                user_id=None,
                task_id=f"dry-run-cache-trans-{i:03d}",
                video_id=None,
                audio_path=audio_path,
                language="pt",
            )

        results = _run_concurrent_with_rounds(
            n=DEFAULT_N,
            max_workers=DEFAULT_MAX_WORKERS,
            fn_builder=lambda i: (lambda: call(i)),
            total_counter_key="trans_cache_total_calls",
            in_progress_counter_key="trans_cache_in_progress",
        )
        assert all(isinstance(r, dict) for r in results)
        print("OK: transcription cache 100", flush=True)


def _ensure_cb_settings():
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT id FROM settings ORDER BY id DESC LIMIT 1")).fetchone()
        if not row:
            db.execute(text("INSERT INTO settings (user_id, is_active) VALUES (NULL, TRUE)"))
        db.execute(text(
            "DELETE FROM ai_capability_policies WHERE user_id IS NULL AND capability = :cap"
        ), {"cap": AICapability.TEXT_GENERATION})
        db.execute(text(
            """
            UPDATE settings
            SET ai_cb_failure_threshold = 2,
                ai_cb_cooldown_seconds = 2,
                ai_cb_half_open_max_attempts = 1,
                openai_allow_text = FALSE,
                openai_allow_transcription = FALSE,
                openai_allow_images = TRUE,
                openai_allow_thumbnail = TRUE
            WHERE id = (SELECT id FROM settings ORDER BY id DESC LIMIT 1)
            """
        ))
        db.execute(text(
            """
            DELETE FROM ai_provider_circuit_breakers
            WHERE provider IN ('gemini', 'openrouter', 'openai', 'groq')
            """
        ))
        db.commit()
    finally:
        db.close()


def test_circuit_breaker(router: AIRouter):
    print("TEST: circuit breaker transitions", flush=True)
    _ensure_cb_settings()
    os.environ["AI_ROUTER_SIMULATE_FAILURE_PROVIDERS"] = "gemini"
    marker = str(int(time.time() * 1000))
    prompt = f"Teste CB {marker}"

    def call(task_id: str):
        return router.generate_text(
            user_id=None,
            task_id=task_id,
            video_id=None,
            capability=AICapability.TEXT_GENERATION,
            prompt=prompt,
            system_prompt="Retorne um texto curto.",
            temperature=0.0,
            json_mode=False,
        )

    call("dry-run-cb-1")
    call("dry-run-cb-2")
    _ = call("dry-run-cb-3")
    time.sleep(2.2)
    os.environ.pop("AI_ROUTER_SIMULATE_FAILURE_PROVIDERS", None)
    _ = call("dry-run-cb-4")

    db = SessionLocal()
    try:
        rows = db.execute(text(
            """
            SELECT provider, status, COUNT(*)
            FROM ai_operation_runs
            WHERE scope_id LIKE 'dry-run-cb-%' AND capability = :cap
            GROUP BY provider, status
            """
        ), {"cap": AICapability.TEXT_GENERATION}).fetchall()
        summary = {(r[0], r[1]): int(r[2]) for r in rows}
        assert summary.get(("gemini", "failed"), 0) >= 2
        assert summary.get(("openrouter", "completed"), 0) >= 1
        cb = db.execute(text("SELECT state, consecutive_failures FROM ai_provider_circuit_breakers WHERE provider = 'gemini'")).fetchone()
        assert cb is not None
        assert str(cb[0] or "").lower() == "closed"
        assert int(cb[1] or 0) == 0
    finally:
        db.close()
    print("OK: circuit breaker transitions", flush=True)


def test_openai_block(router: AIRouter):
    print("TEST: OpenAI block (text)", flush=True)
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM ai_capability_policies WHERE user_id IS NULL AND capability = :cap"), {"cap": AICapability.TEXT_GENERATION})
        db.execute(text(
            """
            INSERT INTO ai_capability_policies (
                user_id, capability, primary_provider, primary_model, fallback_enabled, fallback_provider, fallback_model,
                cache_enabled, estimated_cost, max_cost, is_active, created_at, updated_at
            ) VALUES (
                NULL, :capability, 'openai', 'gpt-4o-mini', FALSE, NULL, NULL,
                TRUE, 0, NULL, TRUE, NOW(), NOW()
            )
            """
        ), {"capability": AICapability.TEXT_GENERATION})
        db.commit()
    finally:
        db.close()

    blocked = False
    try:
        router.generate_text(
            user_id=None,
            task_id="dry-run-openai-block-1",
            video_id=None,
            capability=AICapability.TEXT_GENERATION,
            prompt="Teste bloqueio OpenAI texto",
            system_prompt="Retorne um texto curto.",
            temperature=0.0,
            json_mode=False,
        )
    except Exception:
        blocked = True
    assert blocked is True
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM ai_capability_policies WHERE user_id IS NULL AND capability = :cap"), {"cap": AICapability.TEXT_GENERATION})
        db.commit()
    finally:
        db.close()
    print("OK: OpenAI block (text)", flush=True)


def _count_runs(*, scope_id: str, capability: str) -> int:
    db = SessionLocal()
    try:
        val = db.execute(text(
            "SELECT COUNT(*) FROM ai_operation_runs WHERE scope_id = :sid AND capability = :cap"
        ), {"sid": scope_id, "cap": capability}).scalar() or 0
        return int(val)
    finally:
        db.close()


def _assert_idempotency_counts():
    db = SessionLocal()
    try:
        t1 = db.execute(text(
            "SELECT COUNT(*) FROM ai_operation_runs WHERE scope_id = 'dry-run-task-1' AND capability = :cap"
        ), {"cap": AICapability.SCRIPT_GENERATION}).scalar() or 0
        t2 = db.execute(text(
            "SELECT COUNT(*) FROM ai_operation_runs WHERE scope_id = 'dry-run-task-2' AND capability = :cap"
        ), {"cap": AICapability.TRANSCRIPTION}).scalar() or 0
        assert int(t1) == 1
        assert int(t2) == 1
    finally:
        db.close()


def _print_report():
    db = SessionLocal()
    try:
        total_runs = int(db.execute(text("SELECT COUNT(*) FROM ai_operation_runs")).scalar() or 0)
        by_status = db.execute(text("SELECT status, COUNT(*) FROM ai_operation_runs GROUP BY status")).fetchall()
        by_provider = db.execute(text("SELECT provider, COUNT(*) FROM ai_operation_runs GROUP BY provider")).fetchall()
        cache_rows = int(db.execute(text("SELECT COUNT(*) FROM ai_operation_cache")).scalar() or 0)
        openai_blocked = int(db.execute(text(
            "SELECT COUNT(*) FROM codexia_financial_audit_events WHERE event_type = 'OPENAI_CAPABILITY_BLOCKED'"
        )).scalar() or 0)
        circuit_events = int(db.execute(text(
            "SELECT COUNT(*) FROM codexia_financial_audit_events WHERE event_type LIKE 'AI_PROVIDER_CIRCUIT_%'"
        )).scalar() or 0)
        ai_ops = int(db.execute(text(
            "SELECT COUNT(*) FROM codexia_financial_audit_events WHERE event_type = 'AI_OPERATION'"
        )).scalar() or 0)
        cache_hits = int(db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM codexia_financial_audit_events
                WHERE event_type = 'AI_OPERATION'
                  AND (details_json LIKE :p1 OR details_json LIKE :p2)
                """
            ),
            {"p1": '%"cache_hit": true%', "p2": '%"cache_hit":true%'},
        ).scalar() or 0)
        cb_rows = int(db.execute(text("SELECT COUNT(*) FROM ai_provider_circuit_breakers")).scalar() or 0)
        print({
            "dry_run": True,
            "total_operation_runs": total_runs,
            "runs_by_status": {r[0]: int(r[1]) for r in by_status},
            "runs_by_provider": {r[0]: int(r[1]) for r in by_provider},
            "cache_rows": cache_rows,
            "cache_hit_events": cache_hits,
            "circuit_breaker_rows": cb_rows,
            "audit_ai_operation_events": ai_ops,
            "audit_openai_blocked_events": openai_blocked,
            "audit_circuit_events": circuit_events,
            "text_100_identical": {
                "total_calls": int(COUNTERS.get("text_total_calls", 0) or 0),
                "unique_operation_runs": int(COUNTERS.get("text_idempotency_unique_runs", 0) or 0),
                "blocked_or_reused": int(COUNTERS.get("text_idempotency_blocked", 0) or 0),
                "in_progress_retries": int(COUNTERS.get("text_in_progress", 0) or 0),
            },
            "text_cache_100": {
                "total_calls": int(COUNTERS.get("text_cache_total_calls", 0) or 0),
                "in_progress_retries": int(COUNTERS.get("text_cache_in_progress", 0) or 0),
            },
            "transcription_100_identical": {
                "total_calls": int(COUNTERS.get("trans_total_calls", 0) or 0),
                "unique_operation_runs": int(COUNTERS.get("trans_idempotency_unique_runs", 0) or 0),
                "blocked_or_reused": int(COUNTERS.get("trans_idempotency_blocked", 0) or 0),
                "in_progress_retries": int(COUNTERS.get("trans_in_progress", 0) or 0),
            },
            "transcription_cache_100": {
                "total_calls": int(COUNTERS.get("trans_cache_total_calls", 0) or 0),
                "in_progress_retries": int(COUNTERS.get("trans_cache_in_progress", 0) or 0),
            },
            "paid_calls_expected": 0,
        })
    finally:
        db.close()


def main():
    os.environ["AI_COST_DRY_RUN"] = "1"
    router = AIRouter()
    _warmup_schema(router)
    test_text(router)
    test_text_cache(router)
    test_transcription(router)
    test_transcription_cache(router)
    _assert_idempotency_counts()
    test_circuit_breaker(router)
    test_openai_block(router)

    router2 = AIRouter()
    _ = router2.generate_text(
        user_id=None,
        task_id="dry-run-task-1",
        video_id=None,
        capability=AICapability.SCRIPT_GENERATION,
        prompt="Teste de roteamento e cache (dry-run).",
        system_prompt="Retorne um texto curto.",
        temperature=0.1,
        json_mode=False,
    )
    _assert_idempotency_counts()
    _print_report()


if __name__ == "__main__":
    main()
