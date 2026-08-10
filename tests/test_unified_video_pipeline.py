import os
import shutil
import tempfile
import unittest
import time
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ANTES de importar app.database: forçar SQLite de desenvolvimento para não tentar conectar PostgreSQL
# task_manager usa SessionLocal do app.database global; com isso ele também usará SQLite temporário.
_TEMP_DB_ROOT = tempfile.mkdtemp(prefix="codexia-db-")
_TEMP_DB_FILE = os.path.join(_TEMP_DB_ROOT, "main.sqlite3")
os.environ["APP_ENV"] = "development"
os.environ["ENABLE_SQLITE_DEV"] = "true"
os.environ["SQLITE_DB_PATH"] = _TEMP_DB_FILE
os.environ.pop("DATABASE_URL", None)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session as _SASession

from app.database import Base
from app.models import UnifiedVideo, UnifiedVideoStatus

# --- ANTES de qualquer teste: garantir engine GLOBAL (SessionLocal) tem todas as tabelas de task_manager ---
import app.services.task_manager as _tm_bootstrap
from app.database import SessionLocal as _SL_Bootstrap
_tm_bootstrap._task_schema_ready = False
_sdb_boot = _SL_Bootstrap()
try:
    try:
        from app.models import VideoTask as _VT
        _VT.__table__.create(bind=_sdb_boot.bind, checkfirst=True)
    except Exception:
        pass
    from sqlalchemy import inspect as _si_boot, text as _t_boot
    _insp = _si_boot(_sdb_boot.bind)
    _required_tables = [
        "video_tasks",
        _tm_bootstrap._TASK_DEDUPE_TABLE,
        _tm_bootstrap._TASK_LEASE_TABLE,
        _tm_bootstrap._TASK_LOCK_TABLE,
    ]
    _missing = [tn for tn in _required_tables if not _insp.has_table(tn)]
    if _missing:
        _now_sql = _tm_bootstrap._utcnow()
        for tn in _missing:
            try:
                if tn == "video_tasks":
                    _sdb_boot.execute(_t_boot("""
                    CREATE TABLE IF NOT EXISTS video_tasks (
                        id VARCHAR(64) PRIMARY KEY,
                        user_id INTEGER,
                        status VARCHAR(32) DEFAULT 'pending',
                        progress INTEGER DEFAULT 0,
                        message TEXT,
                        result_json TEXT,
                        payload_json TEXT,
                        idempotency_key VARCHAR(255),
                        request_hash VARCHAR(64),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )"""))
                elif tn == _tm_bootstrap._TASK_DEDUPE_TABLE:
                    _sdb_boot.execute(_t_boot(f"""
                    CREATE TABLE IF NOT EXISTS {tn} (
                        idempotency_key VARCHAR(255) PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        task_id VARCHAR(64),
                        status VARCHAR(32) NOT NULL DEFAULT 'pending',
                        request_payload_json TEXT,
                        result_json TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME,
                        completed_at DATETIME
                    )"""))
                elif tn == _tm_bootstrap._TASK_LEASE_TABLE:
                    _sdb_boot.execute(_t_boot(f"""
                    CREATE TABLE IF NOT EXISTS {tn} (
                        task_id VARCHAR(64) PRIMARY KEY,
                        executor_id VARCHAR(255) NOT NULL,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME,
                        lease_expires_at DATETIME
                    )"""))
                elif tn == _tm_bootstrap._TASK_LOCK_TABLE:
                    _sdb_boot.execute(_t_boot(f"""
                    CREATE TABLE IF NOT EXISTS {tn} (
                        lock_key VARCHAR(255) PRIMARY KEY,
                        owner_id VARCHAR(255) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NOT NULL
                    )"""))
            except Exception:
                pass
        _sdb_boot.commit()
except Exception:
    _sdb_boot.rollback()
finally:
    try:
        _sdb_boot.close()
    except Exception:
        pass
_tm_bootstrap._task_schema_ready = False
try:
    _tm_bootstrap._ensure_task_support_tables()
except Exception:
    pass


def _make_minimal_mp4(path: str, size_bytes: int = 200 * 1024) -> str:
    """Cria um arquivo MP4 falso mas com assinatura/alguns bytes para validação de tamanho."""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        # 8 bytes ftyp ISO base media (MP4 start signature) + padding
        f.write(b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41")
        remaining = max(0, int(size_bytes) - f.tell())
        if remaining:
            # Preenche o resto com zeros para chegar ao tamanho alvo.
            block = b"\x00" * min(remaining, 65536)
            written = 0
            while written < remaining:
                chunk = block if (remaining - written) >= len(block) else (b"\x00" * (remaining - written))
                f.write(chunk)
                written += len(chunk)
    return path


def _make_wav_audio(path: str, size_bytes: int = 80 * 1024) -> str:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        # WAV header: 44 bytes válidos (RIFF/WAVE/fmt/data) para min 1kHz 8bit mono
        # 44 bytes + N bytes = tamanho final
        riff_chunk_size = max(36, int(size_bytes) - 8)
        data_size = max(0, riff_chunk_size - 36)
        f.write(b"RIFF")
        f.write(riff_chunk_size.to_bytes(4, "little", signed=False))
        f.write(b"WAVEfmt ")
        f.write((16).to_bytes(4, "little"))  # fmt chunk size
        f.write((1).to_bytes(2, "little"))  # PCM
        f.write((1).to_bytes(2, "little"))  # mono
        f.write((8000).to_bytes(4, "little"))  # sample rate
        f.write((8000).to_bytes(4, "little"))  # byte rate
        f.write((1).to_bytes(2, "little"))  # block align
        f.write((8).to_bytes(2, "little"))  # bits/sample
        f.write(b"data")
        f.write(data_size.to_bytes(4, "little"))
        written = 0
        block = b"\x00" * min(max(0, data_size), 65536)
        while written < data_size:
            chunk = block if (data_size - written) >= len(block) else (b"\x00" * (data_size - written))
            f.write(chunk)
            written += len(chunk)
    return path


def _make_image(path: str, size_bytes: int = 40 * 1024) -> str:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        # Minimal PNG: 8-byte magic + IHDR 1x1 + IDAT tiny + IEND
        f.write(b"\x89PNG\r\n\x1a\n")
        remaining = max(0, int(size_bytes) - 8)
        if remaining:
            block = b"\x00" * min(remaining, 65536)
            written = 0
            while written < remaining:
                chunk = block if (remaining - written) >= len(block) else (b"\x00" * (remaining - written))
                f.write(chunk)
                written += len(chunk)
    return path


class UnifiedVideoPipelineContractTests(unittest.TestCase):
    """Contrato + idempotência + validação de artefatos do UnifiedVideoPipelineService.

    Nota: por default os testes usam banco SQLite em diretório temporário.
    Para o executor assíncrono / HTTP real, usamos mocks.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="unified-pipe-"))
        self.db_path = self.temp_dir / "unified.sqlite"
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(self.engine)

        # O pipeline, as tarefas e os locks devem usar o mesmo banco. Outros
        # módulos da suíte também substituem SessionLocal em seus testes; fazer
        # o vínculo explicitamente aqui evita depender da ordem de descoberta.
        import app.services.task_manager as _isolated_tm
        self._original_task_session_local = _isolated_tm.SessionLocal
        _isolated_tm.SessionLocal = self.Session
        _isolated_tm._task_schema_ready = False
        _isolated_tm._ensure_task_support_tables()

        # ----- task_manager: garantir tabelas de apoio (video_tasks, video_task_dedupe, leases, locks) no engine GLOBAL do app.
        # Como claim_video_task usa SessionLocal() global internamente; precisamos garantir que
        # a SessionLocal crie/veja as tabelas.
        try:
            import app.services.task_manager as _tm
            # Force re-check global.
            _tm._task_schema_ready = False
            from app.database import engine as _main_engine, SessionLocal as _SL
            _eng = _main_engine
            # Rodar DDL direto na engine global para garantir tabelas existem lá.
            from sqlalchemy import inspect as _si, text as _txt
            insp_main = _si(_eng)
            has_all_ok = all(
                insp_main.has_table(tn) for tn in ("video_tasks", _tm._TASK_DEDUPE_TABLE, _tm._TASK_LEASE_TABLE, _tm._TASK_LOCK_TABLE)
            )
            if not has_all_ok:
                # Criar via SQLite create_all no engine global.
                _sess = _SL()
                try:
                    try:
                        from app.models import VideoTask
                        VideoTask.__table__.create(bind=_eng, checkfirst=True)
                    except Exception:
                        pass
                    try:
                        _sess.execute(_txt(f"""
                        CREATE TABLE IF NOT EXISTS {_tm._TASK_DEDUPE_TABLE} (
                            idempotency_key VARCHAR(255) PRIMARY KEY,
                            request_hash TEXT NOT NULL,
                            task_id VARCHAR(64),
                            status VARCHAR(32) NOT NULL DEFAULT 'pending',
                            request_payload_json TEXT,
                            result_json TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            expires_at DATETIME,
                            completed_at DATETIME
                        )"""))
                        _sess.execute(_txt(f"""
                        CREATE TABLE IF NOT EXISTS {_tm._TASK_LEASE_TABLE} (
                            task_id VARCHAR(64) PRIMARY KEY,
                            executor_id VARCHAR(255) NOT NULL,
                            attempt_number INTEGER NOT NULL DEFAULT 1,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            expires_at DATETIME,
                            lease_expires_at DATETIME
                        )"""))
                        _sess.execute(_txt(f"""
                        CREATE TABLE IF NOT EXISTS {_tm._TASK_LOCK_TABLE} (
                            lock_key VARCHAR(255) PRIMARY KEY,
                            owner_id VARCHAR(255) NOT NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            expires_at DATETIME NOT NULL
                        )"""))
                        _sess.commit()
                    except Exception:
                        _sess.rollback()
                finally:
                    _sess.close()
            _tm._task_schema_ready = False  # forçar revalidação
            _tm._ensure_task_support_tables()
        except Exception:
            import traceback
            traceback.print_exc()

        # Criar também no engine do teste (garante se precisar)
        try:
            import app.services.task_manager as _tm
            from sqlalchemy import inspect as _si, text as _txt
            _it = _si(self.engine)
            if not _it.has_table("video_tasks"):
                try:
                    from app.models import VideoTask
                    VideoTask.__table__.create(bind=self.engine, checkfirst=True)
                except Exception:
                    pass
            for tn in ("video_tasks", _tm._TASK_DEDUPE_TABLE, _tm._TASK_LEASE_TABLE, _tm._TASK_LOCK_TABLE):
                if not _it.has_table(tn):
                    try:
                        if tn == _tm._TASK_DEDUPE_TABLE:
                            self.engine.execute(_txt(f"""
                            CREATE TABLE IF NOT EXISTS {tn} (
                                idempotency_key VARCHAR(255) PRIMARY KEY,
                                request_hash TEXT NOT NULL,
                                task_id VARCHAR(64),
                                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                                request_payload_json TEXT,
                                result_json TEXT,
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                expires_at DATETIME,
                                completed_at DATETIME
                            )"""))
                        elif tn == _tm._TASK_LEASE_TABLE:
                            self.engine.execute(_txt(f"""
                            CREATE TABLE IF NOT EXISTS {tn} (
                                task_id VARCHAR(64) PRIMARY KEY,
                                executor_id VARCHAR(255) NOT NULL,
                                attempt_number INTEGER NOT NULL DEFAULT 1,
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                expires_at DATETIME,
                                lease_expires_at DATETIME
                            )"""))
                        elif tn == _tm._TASK_LOCK_TABLE:
                            self.engine.execute(_txt(f"""
                            CREATE TABLE IF NOT EXISTS {tn} (
                                lock_key VARCHAR(255) PRIMARY KEY,
                                owner_id VARCHAR(255) NOT NULL,
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                expires_at DATETIME NOT NULL
                            )"""))
                        else:
                            self.engine.execute(_txt(f"""
                            CREATE TABLE IF NOT EXISTS {tn} (id VARCHAR(64) PRIMARY KEY, status VARCHAR(32))
                            """))
                    except Exception:
                        pass
        except Exception:
            pass

        self.media_root = self.temp_dir / "media"
        self.videos_dir = self.media_root / "videos"
        self.audio_dir = self.media_root / "audio"
        self.images_dir = self.media_root / "images"
        for p in (self.videos_dir, self.audio_dir, self.images_dir):
            p.mkdir(parents=True, exist_ok=True)

        from app import config as _cfg
        self._orig_paths = {
            "UNIFIED_VIDEO_DIR": str(getattr(_cfg, "UNIFIED_VIDEO_DIR", "")),
            "UNIFIED_AUDIO_DIR": str(getattr(_cfg, "UNIFIED_AUDIO_DIR", "")),
            "UNIFIED_IMAGES_DIR": str(getattr(_cfg, "UNIFIED_IMAGES_DIR", "")),
            "VIDEO_OUTPUT_DIR": str(getattr(_cfg, "VIDEO_OUTPUT_DIR", "")),
            "AUDIO_OUTPUT_DIR": str(getattr(_cfg, "AUDIO_OUTPUT_DIR", "")),
            "IMAGES_OUTPUT_DIR": str(getattr(_cfg, "IMAGES_OUTPUT_DIR", "")),
        }
        _cfg.UNIFIED_VIDEO_DIR = str(self.videos_dir)
        _cfg.UNIFIED_AUDIO_DIR = str(self.audio_dir)
        _cfg.UNIFIED_IMAGES_DIR = str(self.images_dir)
        _cfg.VIDEO_OUTPUT_DIR = str(self.videos_dir)
        _cfg.AUDIO_OUTPUT_DIR = str(self.audio_dir)
        _cfg.IMAGES_OUTPUT_DIR = str(self.images_dir)

        self.db: _SASession = self.Session()

    def tearDown(self):
        self.db.close()
        try:
            import app.services.task_manager as _tm
            _tm.SessionLocal = self._original_task_session_local
            _tm._task_schema_ready = False
        except Exception:
            pass
        self.engine.dispose()
        try:
            from app import config as _cfg
            for k, v in self._orig_paths.items():
                if v != "" and hasattr(_cfg, k):
                    setattr(_cfg, k, v)
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _pipeline(self):
        from app.services.unified_video_pipeline import unified_video_pipeline, UnifiedVideoPipelineService
        svc = unified_video_pipeline()
        svc.ensure_schema(self.db)
        return svc

    def _minimal_request(self, *, ik: Optional[str] = None, module: str = "story", sid: str = "src:1", duration: int = 3, image_count: int = 6) -> Any:
        from app.services.unified_video_pipeline import UnifiedVideoRequest
        return UnifiedVideoRequest(
            source_module=module,
            source_id=sid,
            idempotency_key=ik or f"{module}:{sid}:{hash(ik or sid)}",
            content_type="devotional",
            topic="Como vencer o medo pela fé",
            duration_minutes=duration,
            aspect_ratio="16:9",
            image_count=image_count,
            visibility="unlisted",
            review_required=True,
            auto_publish=False,
        )

    # ------------------------------------------------------------------ #
    # 1. Um clique gera exatamente 1 tarefa                                #
    # ------------------------------------------------------------------ #
    def test_01_one_click_one_task(self):
        pipe = self._pipeline()
        req = self._minimal_request(ik="test:one-click:1", module="story", sid="story:777")
        r1 = pipe.submit_or_reuse(self.db, request=req)
        self.assertTrue(r1.created_new or r1.reused_existing or r1.reused_completed)
        self.assertTrue(r1.task_id, msg="task_id deve ser retornado")
        self.assertTrue(r1.idempotency_key)

        rows = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == "test:one-click:1").all()
        self.assertEqual(len(rows), 1, f"Esperava 1 UnifiedVideo, encontrei {len(rows)}")

    # ------------------------------------------------------------------ #
    # 2. Dois cliques com mesma idempotency_key => 1 tarefa (não duplica) #
    # ------------------------------------------------------------------ #
    def test_02_two_cliques_same_ik_one_task(self):
        pipe = self._pipeline()
        ik = "test:two-clicks:same-ik"
        req1 = self._minimal_request(ik=ik, module="story", sid="story:1001")
        r1 = pipe.submit_or_reuse(self.db, request=req1)
        # Segundo submit com a MESMA idempotency key
        req2 = self._minimal_request(ik=ik, module="story", sid="story:1001")
        r2 = pipe.submit_or_reuse(self.db, request=req2)

        self.assertEqual(r1.task_id, r2.task_id, f"task IDs divergem: {r1.task_id} vs {r2.task_id}")
        rows = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).all()
        self.assertEqual(len(rows), 1, f"dois submits mesmo ik gerou {len(rows)} linhas")
        self.assertTrue(r2.reused_existing or r2.reused_completed or not r2.created_new)

    def test_02b_simultaneous_clicks_create_one_task_one_row_and_one_kick(self):
        """Prova a idempotência sob concorrência, não apenas sequencialmente."""
        pipe = self._pipeline()
        # A chave deve ser igual entre as threads deste teste, mas exclusiva
        # entre execuções para que um banco temporário já usado não transforme
        # a primeira chamada desta rodada em um reaproveitamento legítimo.
        ik = f"test:simultaneous-clicks:{uuid.uuid4().hex}"
        kick_count = 0
        kick_guard = threading.Lock()

        def kick_once():
            nonlocal kick_count
            with kick_guard:
                kick_count += 1

        def submit_once(_index: int):
            db = self.Session()
            try:
                req = self._minimal_request(ik=ik, module="story", sid="story:concurrent")
                return pipe.submit_or_reuse(db, request=req, kick_queue_callback=kick_once)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(submit_once, range(6)))

        task_ids = {str(result.task_id) for result in results}
        self.assertEqual(len(task_ids), 1, task_ids)
        self.assertEqual(kick_count, 1, "Somente a primeira solicitação pode iniciar o worker.")
        self.db.expire_all()
        rows = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(1 for result in results if result.created_new), 1)

    def test_02c_discarded_failure_allows_new_task_and_reopens_canonical_row(self):
        from app.services.task_manager import request_cancel_task

        pipe = self._pipeline()
        ik = f"test:discard-then-new:{uuid.uuid4().hex}"
        req = self._minimal_request(ik=ik, module="story", sid="story:discard-old")
        first = pipe.submit_or_reuse(self.db, request=req)
        pipe.transition_status(
            self.db,
            str(first.task_id),
            status=UnifiedVideoStatus.FAILED,
            progress=20,
            message="Falha antiga",
        )
        discarded = request_cancel_task(str(first.task_id), message="Descartada no teste")
        self.assertIsNotNone(discarded)

        second = pipe.submit_or_reuse(
            self.db,
            request=self._minimal_request(ik=ik, module="story", sid="story:discard-old"),
        )
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertTrue(second.created_new)
        self.db.expire_all()
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        self.assertEqual(uv.task_id, second.task_id)
        self.assertEqual(uv.status, UnifiedVideoStatus.QUEUED)
        self.assertEqual(uv.progress, 0)
        self.assertIsNone(uv.last_error)

    # ------------------------------------------------------------------ #
    # 3. Retry sem force_regenerate NÃO cria novo MP4 se já existe        #
    # ------------------------------------------------------------------ #
    def test_03_retry_no_new_mp4_if_exists(self):
        pipe = self._pipeline()
        ik = "test:reuse-mp4:v1"
        req = self._minimal_request(ik=ik, module="story", sid="story:3001", image_count=2)
        r1 = pipe.submit_or_reuse(self.db, request=req)
        uv1 = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        fake_video = str(self.videos_dir / f"reuse-{uv1.id}.mp4")
        fake_audio = str(self.audio_dir / f"reuse-{uv1.id}.wav")
        _make_minimal_mp4(fake_video, size_bytes=300 * 1024)
        _make_wav_audio(fake_audio, size_bytes=100 * 1024)
        uv1.video_path = fake_video
        uv1.video_size_bytes = os.path.getsize(fake_video)
        uv1.video_duration_seconds = 42.5
        uv1.audio_path = fake_audio
        uv1.audio_size_bytes = os.path.getsize(fake_audio)
        uv1.audio_duration_seconds = 41.8
        uv1.script_json = json.dumps({"title": "X", "text": "Y"})
        uv1.storyboard_json = json.dumps({"scenes": [{"text": "a"}, {"text": "b"}]})
        imgs = []
        for i in range(3):
            p = str(self.images_dir / f"reuse-{uv1.id}-{i}.png")
            _make_image(p, size_bytes=50 * 1024)
            imgs.append(p)
        uv1.images_json = json.dumps({"paths": imgs})
        uv1.status = UnifiedVideoStatus.AWAITING_REVIEW
        self.db.commit()

        # Novo submit MESMA ik SEM force_regenerate
        req2 = self._minimal_request(ik=ik, module="story", sid="story:3001", image_count=2)
        r2 = pipe.submit_or_reuse(self.db, request=req2)
        self.assertTrue(r2.reused_completed or r2.reused_existing, msg="deve reuso sem force_regenerate")
        self.assertEqual(r1.task_id, r2.task_id)
        self.assertEqual(os.path.getsize(fake_video), 300 * 1024, msg="MP4 original não deve ser tocado")

        # Com force_regenerate deve invalidar e resetar status para queued/processing
        req3 = self._minimal_request(ik=ik, module="story", sid="story:3001", image_count=2)
        req3.force_regenerate = True
        r3 = pipe.submit_or_reuse(self.db, request=req3)
        self.assertIsNotNone(r3.task_id)

    # ------------------------------------------------------------------ #
    # 4. Scheduler (chamada concorrente simulada) não duplica             #
    # ------------------------------------------------------------------ #
    def test_04_scheduler_no_duplicate_task(self):
        pipe = self._pipeline()
        ik = "test:scheduler:v1"
        req = self._minimal_request(ik=ik, module="youtube_series", sid="episode:42")
        # Chamadas sequenciais simulando scheduler rodando duas vezes
        results: List[Any] = []
        for _ in range(3):
            pipe2 = self._pipeline()
            r = pipe2.submit_or_reuse(self.db, request=req)
            results.append(r.task_id)
        self.assertEqual(len(set(results)), 1, msg=f"scheduler gerou múltiplas tasks: {set(results)}")
        rows = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).count()
        self.assertEqual(rows, 1)

    # ------------------------------------------------------------------ #
    # 5. Fluxo series constrói UnifiedVideoRequest corretamente via bridge #
    # ------------------------------------------------------------------ #
    def test_05_series_uses_unified_pipeline(self):
        pipe = self._pipeline()
        ik = "test:series:bridge:1"
        from app.services.unified_video_pipeline import UnifiedVideoRequest
        req = UnifiedVideoRequest(
            source_module="youtube_series",
            source_id="episode:99",
            idempotency_key=ik,
            content_type="devotional",
            topic="Salvação pela fé",
            duration_minutes=5,
            aspect_ratio="16:9",
            image_count=8,
            visibility="unlisted",
            review_required=True,
            auto_publish=False,
            force_regenerate=False,
            force_reuse_assets=False,
        )
        r = pipe.submit_or_reuse(self.db, request=req)
        self.assertTrue(r.task_id)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        self.assertEqual(uv.source_module, "youtube_series")
        self.assertEqual(uv.source_id, "episode:99")

    # ------------------------------------------------------------------ #
    # 6. Fluxo manual (story) chama o mesmo pipeline — submit_or_reuse    #
    # ------------------------------------------------------------------ #
    def test_06_story_module_uses_same_pipeline(self):
        pipe = self._pipeline()
        ik = "test:story-module:same-pipe"
        req = self._minimal_request(ik=ik, module="story", sid="manual:xyz")
        r = pipe.submit_or_reuse(self.db, request=req)
        self.assertTrue(r.task_id)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        self.assertEqual(uv.source_module, "story")

    # ------------------------------------------------------------------ #
    # 7. Sem MP4 válido (>100KB) NÃO passa para awaiting_review            #
    # ------------------------------------------------------------------ #
    def test_07_no_valid_mp4_no_awaiting_review(self):
        pipe = self._pipeline()
        ik = "test:no-mp4:v1"
        req = self._minimal_request(ik=ik, module="story", sid="bad:1", image_count=2)
        r = pipe.submit_or_reuse(self.db, request=req)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        # monta cenário parcial: áudio e imagens OK, mas MP4 pequeno
        audio_p = str(self.audio_dir / f"bad-{uv.id}.wav")
        _make_wav_audio(audio_p, size_bytes=120 * 1024)
        imgs = []
        for i in range(3):
            p = str(self.images_dir / f"bad-{uv.id}-{i}.png")
            _make_image(p, size_bytes=50 * 1024)
            imgs.append(p)
        small_mp4 = str(self.videos_dir / f"bad-{uv.id}.mp4")
        _make_minimal_mp4(small_mp4, size_bytes=10 * 1024)  # abaixo do threshold
        uv.script_json = json.dumps({"title": "X", "text": "Y"})
        uv.storyboard_json = json.dumps({"scenes": [{"text": "a"}] * 3})
        uv.images_json = json.dumps({"paths": imgs})
        uv.audio_path = audio_p
        uv.audio_size_bytes = os.path.getsize(audio_p)
        uv.video_path = small_mp4
        uv.video_size_bytes = os.path.getsize(small_mp4)
        self.db.commit()

        # Chama validação central.
        validation, updated_uv = pipe.transition_to_awaiting_review_if_valid(self.db, ik, probe_local_paths=True, probe_http=False)
        self.assertFalse(validation.ok, msg=f"MP4 pequeno ({uv.video_size_bytes} bytes) não deveria passar. Detalhes: {validation.details}")
        self.assertEqual(str(updated_uv.status), UnifiedVideoStatus.FAILED, f"deve ir para failed mas foi {updated_uv.status}")
        # O primeiro check que falhou deve estar relacionado com MP4 (não existe ou tamanho < 100KB)
        joined_lower = (
            str(validation.first_failed or "").lower()
            + " "
            + (json.dumps(validation.details) if isinstance(validation.details, dict) else str(validation.details)).lower()
        )
        self.assertTrue(
            ("mp4" in joined_lower) and ("size_bytes" in joined_lower or "larger" in joined_lower or "100" in joined_lower or "exists" in joined_lower),
            f"Esperava checagem MP4 falha, mas first_failed={validation.first_failed}; joined={joined_lower[:1000]}",
        )

    # ------------------------------------------------------------------ #
    # 8. Sem áudio NÃO passa para awaiting_review                          #
    # ------------------------------------------------------------------ #
    def test_08_no_audio_no_awaiting_review(self):
        pipe = self._pipeline()
        ik = "test:no-audio:v1"
        req = self._minimal_request(ik=ik, module="story", sid="badaudio:1", image_count=2)
        r = pipe.submit_or_reuse(self.db, request=req)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        mp4 = str(self.videos_dir / f"badaudio-{uv.id}.mp4")
        _make_minimal_mp4(mp4, size_bytes=500 * 1024)
        imgs = []
        for i in range(3):
            p = str(self.images_dir / f"badaudio-{uv.id}-{i}.png")
            _make_image(p, size_bytes=50 * 1024)
            imgs.append(p)
        uv.script_json = json.dumps({"title": "X", "text": "Y"})
        uv.storyboard_json = json.dumps({"scenes": [{"text": "a"}] * 3})
        uv.images_json = json.dumps({"paths": imgs})
        uv.video_path = mp4
        uv.video_size_bytes = os.path.getsize(mp4)
        # SEM áudio
        self.db.commit()

        validation, updated_uv = pipe.transition_to_awaiting_review_if_valid(self.db, ik, probe_local_paths=True, probe_http=False)
        self.assertFalse(validation.ok, msg=f"sem áudio não deveria passar. first_failed={validation.first_failed} details={validation.details}")
        self.assertEqual(str(updated_uv.status), UnifiedVideoStatus.FAILED)
        self.assertIn("audio", (str(validation.first_failed) or "").lower() + " " + (json.dumps(validation.details) if isinstance(validation.details, dict) else str(validation.details)).lower())

    # ------------------------------------------------------------------ #
    # 9. Upload NÃO ocorre duas vezes (youtube_video_id presente)         #
    # ------------------------------------------------------------------ #
    def test_09_upload_happens_at_most_once(self):
        pipe = self._pipeline()
        ik = "test:single-upload:v1"
        req = self._minimal_request(ik=ik, module="youtube_series", sid="episode:555")
        r = pipe.submit_or_reuse(self.db, request=req)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        mp4 = str(self.videos_dir / f"up-{uv.id}.mp4")
        _make_minimal_mp4(mp4, size_bytes=600 * 1024)
        uv.video_path = mp4
        uv.youtube_video_id = "yt_already_uploaded_123"
        uv.youtube_url = "https://youtu.be/yt_already_uploaded_123"
        uv.status = UnifiedVideoStatus.APPROVED
        # Force: garantir que unified_video tenha force_regenerate=False para acionar a proteção
        uv.force_regenerate = False
        self.db.commit()

        calls: List[Any] = []

        def fake_upload(path, meta):
            calls.append((path, meta))
            raise AssertionError("upload callable não deveria ter sido chamado quando youtube_video_id já existe")

        res = pipe.publish_if_ready(self.db, ik, upload_callable=fake_upload, upload_metadata={"title": "t"})
        self.assertEqual(len(calls), 0, f"upload callable invocado {len(calls)} vezes com calls={calls}")
        self.assertTrue(bool(res.get("already_uploaded")), f"expected already_uploaded=True, got {res}")
        self.assertEqual(str(res.get("youtube_video_id") or ""), "yt_already_uploaded_123")

    # ------------------------------------------------------------------ #
    # 10. Providers e custo ficam registrados corretamente via merge      #
    # ------------------------------------------------------------------ #
    def test_10_providers_and_cost_recorded(self):
        pipe = self._pipeline()
        ik = "test:providers-cost:v1"
        req = self._minimal_request(ik=ik, module="story", sid="p:1")
        r = pipe.submit_or_reuse(self.db, request=req)
        uv = self.db.query(UnifiedVideo).filter(UnifiedVideo.idempotency_key == ik).one()
        # api_report_{k} keys + render_report.audio_generation.call_count cobre call_count_audio também
        result_dict = {
            "script": {"title": "t", "text": "body"},
            "api_report_script": {"provider": "openai", "model": "gpt-4o-mini", "calls": 2},
            "api_report_images": {"provider": "openai", "model": "gpt-image-1", "calls": 8},
            "api_report_audio": {"provider": "elevenlabs", "model": "nova-v2", "calls": 1},
            "render_report": {
                "audio_generation": {
                    "provider": "elevenlabs",
                    "model": "nova-v2",
                    "call_count": 1,
                }
            },
            "financial_guardian": {"estimated_cost": 0.42, "actual_cost": 0.38},
        }
        pipe.transition_status(
            self.db,
            ik,
            status="processing_script",
            progress=10,
            merge_result=result_dict,
        )
        self.db.refresh(uv)
        self.assertEqual(uv.text_provider, "openai")
        self.assertEqual(uv.text_model, "gpt-4o-mini")
        self.assertEqual(uv.image_provider, "openai")
        self.assertEqual(uv.image_model, "gpt-image-1")
        # voice provider/model pode vir de api_report_audio OU render_report.audio_generation
        self.assertIn(str(uv.voice_provider or ""), {"elevenlabs"})
        self.assertIn(str(uv.voice_model or ""), {"nova-v2"})
        self.assertEqual(int(uv.call_count_text or 0), 2)
        self.assertEqual(int(uv.call_count_image or 0), 8)
        # call_count_audio pode ser 1 via api_report_audio OU render_report
        self.assertIn(int(uv.call_count_audio or 0), {1})
        self.assertAlmostEqual(float(uv.estimated_cost or 0.0), 0.42, delta=1e-6)
        self.assertAlmostEqual(float(uv.actual_cost or 0.0), 0.38, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
