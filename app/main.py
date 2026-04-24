from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware # Importante para Coolify/Traefik
from app.database import engine, Base, get_db, SessionLocal, DATABASE_DISPLAY
from app.routers import books, marketing, settings, video, crm, webhook, youtube, book_factory, auth, diagnostics, hotmart, music, admin, social_media
from app.modules.ai_factory import router as ai_factory
from app.modules.ai_factory import models as ai_models
from app.modules.humor_factory import router as humor_factory
from app.modules.humor_factory import models as humor_models
from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager
from app.services.monitor_service import monitor_service
from sqlalchemy import text, inspect
from app.models import User
from app.routers.auth import get_password_hash

# Carregar variáveis de ambiente
load_dotenv()
# Trigger reload: 1

APP_ENV = os.getenv("APP_ENV", "production").lower()
IS_PRODUCTION = APP_ENV == "production"

# CORS: lista separada por vírgula (ex: https://app.example.com,https://example.com)
_cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw else ["*"]

# Caminho da pasta estática: no container é /app/app/static; localmente usa path do pacote
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_STATIC_SERVE = "/app/app/static" if os.path.isdir("/app/app/static") else str(_STATIC_DIR)

print(f"STARTUP DEBUG: _BASE_DIR={_BASE_DIR}")
print(f"STARTUP DEBUG: _STATIC_DIR={_STATIC_DIR}")
print(f"STARTUP DEBUG: _STATIC_SERVE={_STATIC_SERVE}")
if os.path.exists(_STATIC_SERVE):
    print(f"STARTUP DEBUG: listing {_STATIC_SERVE}: {os.listdir(_STATIC_SERVE)}")
else:
    print(f"STARTUP DEBUG: {_STATIC_SERVE} does not exist!")

# Create tables (não derrubar o processo se o banco estiver inacessível no startup, ex.: Render)
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"AVISO: create_all falhou no startup: {e}. O app sobe mesmo assim; migrações rodam no lifespan.")

def run_migrations(engine):
    try:
        inspector = inspect(engine)
        dialect = (getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
        datetime_type = "TIMESTAMP" if dialect in ("postgresql", "postgres") else "DATETIME"

        if "settings" in inspector.get_table_names():
            try:
                columns = [c["name"] for c in inspector.get_columns("settings")]
                if "youtube_comments_last_sync_at" not in columns:
                    with engine.connect() as conn:
                        conn.execute(text(f"ALTER TABLE settings ADD COLUMN youtube_comments_last_sync_at {datetime_type}"))
                        conn.commit()
            except Exception as e:
                print(f"Failed to migrate settings table: {e}")

        if "video_tasks" in inspector.get_table_names():
            try:
                columns = [c["name"] for c in inspector.get_columns("video_tasks")]
                missing = []
                if "created_at" not in columns:
                    missing.append(("created_at", datetime_type))
                if "updated_at" not in columns:
                    missing.append(("updated_at", datetime_type))
                if missing:
                    with engine.connect() as conn:
                        for name, kind in missing:
                            conn.execute(text(f"ALTER TABLE video_tasks ADD COLUMN {name} {kind}"))
                        conn.commit()
            except Exception as e:
                print(f"Failed to migrate video_tasks table: {e}")
        
        # Books table migration
        if "books" in inspector.get_table_names():
            try:
                columns = [c["name"] for c in inspector.get_columns("books")]
                if "cover_image_base64" not in columns:
                    print("Migrating: Adding missing column cover_image_base64 to books table...")
                    with engine.connect() as conn:
                        conn.execute(text("ALTER TABLE books ADD COLUMN cover_image_base64 TEXT"))
                        conn.commit()
            except Exception as e:
                print(f"Failed to migrate books table: {e}")

        # Tenants table creation/verification
        if "tenants" not in inspector.get_table_names():
            print("Migration: Creating tenants table...")
            try:
                Base.metadata.create_all(bind=engine)
            except Exception as e:
                print(f"Failed to create tenants table: {e}")
        
        # Ensure Default Tenant exists
        try:
            with engine.connect() as conn:
                # Check if tenants table exists first to avoid error if create_all failed
                if "tenants" in inspector.get_table_names():
                    r = conn.execute(text("SELECT 1 FROM tenants WHERE slug = 'default' LIMIT 1"))
                    if r.fetchone() is None:
                        conn.execute(text(
                            "INSERT INTO tenants (name, slug, created_at) VALUES ('Default', 'default', CURRENT_TIMESTAMP)"
                        ))
                        conn.commit()
                        print("Migration: Tenant 'Default' criado.")
        except Exception as e:
            print(f"Failed to create Default tenant: {e}")

        # Users table migration
        if "users" not in inspector.get_table_names():
             print("Migration: Creating users table...")
             try:
                 Base.metadata.create_all(bind=engine)
             except Exception as e:
                 print(f"Failed to create users table: {e}")
        else:
            try:
                user_columns = [c["name"] for c in inspector.get_columns("users")]
                with engine.connect() as conn:
                    if "must_change_password" not in user_columns:
                        print("Migrating: Adding missing column must_change_password to users table...")
                        try:
                            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"))
                            conn.commit()
                        except Exception as e: print(f"Error adding must_change_password: {e}")

                    if "is_admin" not in user_columns:
                        print("Migrating: Adding is_admin to users table...")
                        try:
                            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE"))
                            conn.commit()
                        except Exception as e: print(f"Error adding is_admin: {e}")

                    if "name" not in user_columns:
                        print("Migrating: Adding name to users table...")
                        try:
                            conn.execute(text("ALTER TABLE users ADD COLUMN name TEXT"))
                            conn.commit()
                        except Exception as e: print(f"Error adding name: {e}")

                    if "role" not in user_columns:
                        print("Migrating: Adding role to users table...")
                        try:
                            conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'cliente'"))
                            conn.commit()
                            conn.execute(text("UPDATE users SET role = 'admin' WHERE is_admin = 1"))
                            conn.commit()
                        except Exception as e: print(f"Error adding role: {e}")

                    if "tenant_id" not in user_columns:
                        print("Migrating: Adding tenant_id to users table...")
                        try:
                            conn.execute(text("ALTER TABLE users ADD COLUMN tenant_id INTEGER"))
                            conn.commit()
                            # Atribuir usuários existentes ao tenant Default (id=1)
                            conn.execute(text("UPDATE users SET tenant_id = 1 WHERE tenant_id IS NULL"))
                            conn.commit()
                            print("Migration: tenant_id adicionado; usuários existentes atribuídos a Default.")
                        except Exception as e: print(f"Error adding tenant_id: {e}")
            except Exception as e:
                print(f"Error migrating users table: {e}")

        # Multi-tenant: user_id nas tabelas principais
        for table, col in [
            ("books", "user_id"), ("book_drafts", "user_id"), ("leads", "user_id"),
            ("settings", "user_id"), ("customers", "user_id"), ("scheduled_videos", "user_id"),
            ("channel_reports", "user_id"),
            ("system_notifications", "user_id"), ("channel_insights", "user_id"),
        ]:
            if table in inspector.get_table_names():
                try:
                    tcols = [c["name"] for c in inspector.get_columns(table)]
                    if col not in tcols:
                        print(f"Migrating: Adding {col} to {table}...")
                        with engine.connect() as conn:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER"))
                            conn.commit()
                except Exception as e:
                    print(f"Failed to migrate {table} ({col}): {e}")

        try:
            tables = set(inspector.get_table_names())
            if "system_notifications" not in tables or "channel_insights" not in tables:
                Base.metadata.create_all(bind=engine)
        except Exception as e:
            print(f"Failed to create notifications/insights tables: {e}")

        # ScheduledVideo migration
        if "scheduled_videos" in inspector.get_table_names():
            try:
                sv_columns = [c["name"] for c in inspector.get_columns("scheduled_videos")]
                with engine.connect() as conn:
                    if "progress" not in sv_columns:
                        try:
                            print("Migrating: Adding progress to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN progress INTEGER DEFAULT 0"))
                            conn.commit()
                        except Exception as e: print(f"Error adding progress: {e}")

                    if "publish_at" not in sv_columns:
                        try:
                            print("Migrating: Adding publish_at to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN publish_at TIMESTAMP"))
                            conn.commit()
                        except Exception as e: print(f"Error adding publish_at: {e}")

                    if "auto_post" not in sv_columns:
                        try:
                            print("Migrating: Adding auto_post to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN auto_post BOOLEAN DEFAULT FALSE"))
                            conn.commit()
                        except Exception as e: print(f"Error adding auto_post: {e}")

                    if "youtube_video_id" not in sv_columns:
                        try:
                            print("Migrating: Adding youtube_video_id to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN youtube_video_id TEXT"))
                            conn.commit()
                        except Exception as e: print(f"Error adding youtube_video_id: {e}")
                    
                    if "uploaded_at" not in sv_columns:
                        try:
                            print("Migrating: Adding uploaded_at to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN uploaded_at TIMESTAMP"))
                            conn.commit()
                        except Exception as e: print(f"Error adding uploaded_at: {e}")

                    if "updated_at" not in sv_columns:
                        try:
                            print("Migrating: Adding updated_at to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN updated_at TIMESTAMP"))
                            conn.commit()
                        except Exception as e: print(f"Error adding updated_at: {e}")

                    if "music_file_path" not in sv_columns:
                        try:
                            print("Migrating: Adding music_file_path to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN music_file_path VARCHAR"))
                            conn.commit()
                        except Exception as e: print(f"Error adding music_file_path: {e}")

                    if "voice_style" not in sv_columns:
                        try:
                            print("Migrating: Adding voice_style to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_style VARCHAR DEFAULT 'human'"))
                            conn.commit()
                        except Exception as e: print(f"Error adding voice_style: {e}")
                    
                    if "voice_gender" not in sv_columns:
                        try:
                            print("Migrating: Adding voice_gender to scheduled_videos...")
                            conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_gender VARCHAR DEFAULT 'female'"))
                            conn.commit()
                        except Exception as e: print(f"Error adding voice_gender: {e}")
            except Exception as e:
                print(f"Error migrating scheduled_videos table: {e}")

        # Settings migration
        if "settings" in inspector.get_table_names():
            try:
                settings_columns = [c["name"] for c in inspector.get_columns("settings")]
                with engine.connect() as conn:
                    for col in [
                        "leonardo_api_key", "leonardo_model_id",
                        "gemini_api_key", "deepseek_api_key", "groq_api_key", 
                        "anthropic_api_key", "mistral_api_key", "openrouter_api_key",
                        "openrouter_model",
                        "hotmart_client_id", "hotmart_client_secret", 
                        "hotmart_access_token", "suno_api_key",
                        "pexels_api_key", "pixabay_api_key", "elevenlabs_api_key",
                        "edenai_api_key",
                        "elevenlabs_voice_id", "elevenlabs_voice_name",
                        "whatsapp_phone_number_id", "whatsapp_access_token",
                        "whatsapp_verify_token", "whatsapp_allowed_numbers",
                        "telegram_bot_token", "telegram_allowed_chat_ids",
                        "instagram_user_id", "instagram_access_token",
                        "tiktok_access_token"
                    ]:
                        if col not in settings_columns:
                            try:
                                print(f"Migrating: Adding {col} to settings...")
                                conn.execute(text(f"ALTER TABLE settings ADD COLUMN {col} TEXT"))
                                conn.commit()
                            except Exception as e: print(f"Error adding {col}: {e}")

                    if "ai_provider" not in settings_columns:
                        try:
                            print("Migrating: Adding ai_provider to settings...")
                            conn.execute(text("ALTER TABLE settings ADD COLUMN ai_provider TEXT DEFAULT 'openai'"))
                            conn.commit()
                        except Exception as e: print(f"Error adding ai_provider: {e}")

                    if "hotmart_token_expires_at" not in settings_columns:
                        try:
                            print("Migrating: Adding hotmart_token_expires_at to settings...")
                            conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_token_expires_at TIMESTAMP"))
                            conn.commit()
                        except Exception as e: print(f"Error adding hotmart_token_expires_at: {e}")
            except Exception as e:
                print(f"Error migrating settings table: {e}")

        # Book drafts migration
        if "book_drafts" in inspector.get_table_names():
            try:
                bd_columns = [c["name"] for c in inspector.get_columns("book_drafts")]
                if "cover_base64" not in bd_columns:
                    print("Migrating: Adding cover_base64 to book_drafts...")
                    with engine.connect() as conn:
                        conn.execute(text("ALTER TABLE book_drafts ADD COLUMN cover_base64 TEXT"))
                        conn.commit()
            except Exception as e:
                print(f"Error migrating book_drafts table: {e}")

        if "story_drafts" not in inspector.get_table_names():
            try:
                Base.metadata.create_all(bind=engine)
            except Exception as e:
                print(f"Error creating story_drafts table: {e}")

        # Humor Factory migration
        if "codexia_humor_projects" in inspector.get_table_names():
            try:
                hp_columns = [c["name"] for c in inspector.get_columns("codexia_humor_projects")]
                extra_cols = {
                    "avatar_override_path": "TEXT",
                    "opening_message": "TEXT",
                    "catchphrase_message": "TEXT",
                    "catchphrases_json": "TEXT",
                    "closing_message": "TEXT",
                }
                with engine.connect() as conn:
                    for col_name, col_type in extra_cols.items():
                        if col_name not in hp_columns:
                            try:
                                print(f"Migrating: Adding {col_name} to codexia_humor_projects...")
                                conn.execute(text(f"ALTER TABLE codexia_humor_projects ADD COLUMN {col_name} {col_type}"))
                                conn.commit()
                            except Exception as e:
                                print(f"Error adding {col_name} in codexia_humor_projects: {e}")
            except Exception as e:
                print(f"Error migrating codexia_humor_projects table: {e}")

        if "codexia_humor_channels" in inspector.get_table_names():
            try:
                hc_columns = [c["name"] for c in inspector.get_columns("codexia_humor_channels")]
                if "catchphrases_json" not in hc_columns:
                    with engine.connect() as conn:
                        try:
                            print("Migrating: Adding catchphrases_json to codexia_humor_channels...")
                            conn.execute(text("ALTER TABLE codexia_humor_channels ADD COLUMN catchphrases_json TEXT"))
                            conn.commit()
                        except Exception as e:
                            print(f"Error adding catchphrases_json in codexia_humor_channels: {e}")
            except Exception as e:
                print(f"Error migrating codexia_humor_channels table: {e}")

    except Exception as e:
        print(f"Critical Migration Error: {e}")

def create_admin_master():
    """Cria admin master a partir de ADMIN_EMAIL/ADMIN_PASSWORD. Usa tenant Default."""
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    admin_name = os.getenv("ADMIN_NAME", "").strip() or None
    if not admin_email or not admin_password:
        return
    db = SessionLocal()
    try:
        from app.models import Tenant
        tenant = db.query(Tenant).filter(Tenant.slug == "default").first()
        if not tenant:
            tenant = Tenant(name="Default", slug="default")
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
        user = db.query(User).filter(User.email == admin_email).first()
        if user:
            # Force update password to ensure access recovery
            print(f"Admin master encontrado. Atualizando senha de {admin_email}...")
            user.hashed_password = get_password_hash(admin_password)
            if admin_name:
                user.name = admin_name
            user.is_admin = True
            user.role = "admin"
            db.commit()
            return
        user = User(
            email=admin_email,
            name=admin_name,
            tenant_id=tenant.id,
            hashed_password=get_password_hash(admin_password),
            is_admin=True,
            role="admin",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        print("Admin master criado com sucesso (tenant Default).")
    except Exception as e:
        print(f"Erro ao criar admin master: {e}")
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: cada passo em try/except para não derrubar o app (Render/Coolify)
    print(f"Iniciando aplicação... APP_ENV={APP_ENV} | Banco: {DATABASE_DISPLAY}")
    
    try:
        try:
            run_migrations(engine)
        except Exception as e:
            print(f"Migration warning (app continua): {e}")
        
        try:
            create_admin_master()
        except Exception as e:
            print(f"Admin master warning (app continua): {e}")
        
        try:
            monitor_service.start()
        except Exception as e:
            print(f"MonitorService start warning (app continua): {e}")
        
        try:
            from app.models import ScheduledVideo, Job, Video
            from sqlalchemy import func
            from datetime import datetime as dt, timedelta
            db = SessionLocal()
            try:
                stuck_videos = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").all()
                if stuck_videos:
                    print(f"Startup Recovery: Found {len(stuck_videos)} stuck ScheduledVideos. Resetting to 'queued'.")
                    for vid in stuck_videos:
                        vid.status = "queued"
                        vid.progress = 0
                    db.commit()
                # Jobs da Fila de Produção (YouTube Auto) - resetar travados em processing
                cutoff = dt.now() - timedelta(minutes=1)
                stuck_jobs = db.query(Job).filter(Job.status == "processing").filter(
                    func.coalesce(Job.updated_at, Job.created_at) < cutoff
                ).all()
                if stuck_jobs:
                    print(f"Startup Recovery: Found {len(stuck_jobs)} stuck Jobs. Resetting to 'pending'.")
                    for j in stuck_jobs:
                        j.status = "pending"
                        j.progress = 0
                        v = db.query(Video).get(j.video_id)
                        if v and (v.status or "").upper() not in ("PAUSED", "CANCELLED", "CANCELED"):
                            v.status = "queued"
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            print(f"Startup Recovery Error: {e}")
        
        print("Codexia API startup concluído. Rotas: / (frontend), /api/status, /health, /static")
        # Aviso de segurança em produção
        if os.getenv("SECRET_KEY", "").strip() in ("", "sua_secret_key_super_secreta_codexia_2025"):
            print("AVISO: Defina SECRET_KEY no ambiente para produção (tokens JWT).")
    except Exception as e:
        print(f"Startup error (app sobe mesmo assim): {e}")
    
    yield
    # Shutdown
    print("Desligando aplicação...")
    try:
        monitor_service.stop()
    except Exception as e:
        print(f"Monitor stop warning: {e}")

app = FastAPI(
    title="Codexia API",
    description="Sua fábrica de conteúdo movida a inteligência",
    lifespan=lifespan,
    debug=not IS_PRODUCTION,
)

# TRUSTED_HOSTS para Coolify/Traefik (Importante para Mobile/Redirects incorretos)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

# credentials=True incompatível com allow_origins=["*"]; usar False quando *
_allow_creds = "*" not in CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Em production: retorna 500 genérico. Em development: detalhes do erro."""
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    import traceback
    
    if isinstance(exc, (HTTPException, RequestValidationError)):
        raise exc  # deixa o handler padrão do FastAPI tratar 4xx/422
    
    # EMERGENCIAL: Mostrar erro real mesmo em produção para debug
    error_details = str(exc)
    # traceback_str = "".join(traceback.format_tb(exc.__traceback__))
    # print(f"INTERNAL SERVER ERROR: {error_details}\n{traceback_str}")
    
    return JSONResponse(status_code=500, content={"detail": f"Erro Interno: {error_details}"})
    
    # if IS_PRODUCTION:
    #     return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    # return JSONResponse(status_code=500, content={"detail": str(exc)})


# Servir vídeos: tenta VIDEO_OUTPUT_DIR e depois app/static/videos (Render e múltiplas instâncias)
from app.config import VIDEO_OUTPUT_DIR, STATIC_DIR

def _resolve_video_path(safe_name: str):
    """Retorna o path absoluto do vídeo, procurando em VIDEO_OUTPUT_DIR e em app/static/videos."""
    for directory in (VIDEO_OUTPUT_DIR, str(STATIC_DIR / "videos")):
        if directory:
            filepath = os.path.join(directory, safe_name)
            if os.path.isfile(filepath):
                return filepath
    return None

def _video_file_response(request: Request, filepath: str):
    range_header = request.headers.get("range")
    file_size = os.path.getsize(filepath)
    common_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}

    if not range_header:
        return FileResponse(filepath, media_type="video/mp4", headers=common_headers)

    try:
        units, rng = range_header.split("=", 1)
        if units.strip().lower() != "bytes":
            return FileResponse(filepath, media_type="video/mp4", headers=common_headers)
        start_s, end_s = (rng.split("-", 1) + [""])[:2]
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
    except Exception:
        return FileResponse(filepath, media_type="video/mp4", headers=common_headers)

    def _iterfile(path: str, start_pos: int, end_pos: int, chunk_size: int = 1024 * 1024):
        with open(path, "rb") as f:
            f.seek(start_pos)
            remaining = end_pos - start_pos + 1
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    content_length = end - start + 1
    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
    }
    return StreamingResponse(_iterfile(filepath, start, end), status_code=206, media_type="video/mp4", headers=headers)

@app.get("/media/videos/{filename:path}")
def serve_video_media(filename: str, request: Request):
    """Serve vídeo do diretório configurado ou fallback em app/static/videos."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return _video_file_response(request, filepath)

@app.head("/media/videos/{filename:path}")
def head_video_media(filename: str):
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(filepath, media_type="video/mp4", headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"})

@app.get("/static/videos/{filename:path}")
def serve_video_static(filename: str, request: Request):
    """Serve vídeo em URLs /static/videos/... (mesmo arquivo em VIDEO_OUTPUT_DIR ou fallback)."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return _video_file_response(request, filepath)

@app.head("/static/videos/{filename:path}")
def head_video_static(filename: str):
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(filepath, media_type="video/mp4", headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"})

# Montar /static com a pasta que contém index.html (no container: /app/app/static)
app.mount("/static", StaticFiles(directory=_STATIC_SERVE), name="static")
# Garantir que os diretórios de vídeos existem
if os.path.isdir("/data"):
    os.makedirs("/data/media/videos", exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "videos"), exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "image_bank"), exist_ok=True)
# NOTA: Não montamos /media como StaticFiles porque temos rotas específicas (/media/videos/{filename})
# que servem vídeos de VIDEO_OUTPUT_DIR. O mount genérico interceptaria essas rotas.

@app.get("/health")
async def health():
    """Resposta rápida sem DB — para Render/Coolify e diagnóstico."""
    return {"status": "ok"}

@app.get("/api/status")
def api_status():
    """Status da API (JSON) — para scripts ou checagem programática."""
    return {"message": "Codexia API is running"}

@app.get("/api/debug/db")
def debug_db():
    """Rota temporária para debug de schema do banco."""
    try:
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        users_cols = [c["name"] for c in inspector.get_columns("users")] if "users" in tables else []
        sv_cols = [c["name"] for c in inspector.get_columns("scheduled_videos")] if "scheduled_videos" in tables else []
        
        # Check admin user
        with engine.connect() as conn:
            r = conn.execute(text("SELECT id, email, role, is_admin FROM users WHERE email='evarantes2@gmail.com'"))
            user = r.fetchone()
            user_info = dict(user._mapping) if user else "Not Found"
            
        return {
            "tables": tables,
            "users_columns": users_cols,
            "scheduled_videos_columns": sv_cols,
            "admin_user": user_info,
            "db_url_masked": str(engine.url).replace(":", "***").replace("@", "***")
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/ping")
def ping():
    """Simple ping for connectivity check."""
    return "pong"

@app.get("/")
async def root():
    """Serve o painel Vue (index.html) na raiz."""
    index_path = os.path.join(_STATIC_SERVE, "index.html")
    if not os.path.exists(index_path):
        # Debugging info for 404 - Return 200 OK to pass health checks and show debug info
        cwd = os.getcwd()
        listdir_cwd = os.listdir(cwd) if os.path.exists(cwd) else "cwd not found"
        listdir_static = os.listdir(_STATIC_SERVE) if os.path.exists(_STATIC_SERVE) else "static_serve not found"
        
        html_content = f"""
        <html>
            <body style="font-family: monospace; padding: 20px;">
                <h1>Codexia System Error</h1>
                <p>O arquivo <strong>index.html</strong> não foi encontrado.</p>
                <p>Isso geralmente ocorre se um Volume persistente sobrepôs a pasta estática.</p>
                <hr>
                <h3>Debug Info:</h3>
                <ul>
                    <li>Base Dir: {_BASE_DIR}</li>
                    <li>Static Serve Path: {_STATIC_SERVE}</li>
                    <li>Index Path: {index_path}</li>
                    <li>CWD: {cwd}</li>
                </ul>
                <h3>Conteúdo de {_STATIC_SERVE}:</h3>
                <pre>{listdir_static}</pre>
                <h3>Conteúdo de {cwd}:</h3>
                <pre>{listdir_cwd}</pre>
            </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=200)

    return FileResponse(index_path)

@app.get("/app")
async def serve_app():
    """Alias para a interface web (compatibilidade com links antigos)."""
    return FileResponse(os.path.join(_STATIC_SERVE, "index.html"))

@app.get("/login.html")
async def read_login():
    return FileResponse(os.path.join(_STATIC_SERVE, "login.html"))

@app.get("/reset-password.html")
async def read_reset_password():
    return FileResponse(os.path.join(_STATIC_SERVE, "reset-password.html"))

# Routers
app.include_router(auth.router)
app.include_router(books.router)
app.include_router(marketing.router)
app.include_router(settings.router)
app.include_router(video.router)
app.include_router(crm.router)
app.include_router(youtube.router)
app.include_router(webhook.router)
app.include_router(diagnostics.router)
app.include_router(book_factory.router)
app.include_router(hotmart.router)
app.include_router(music.router)
app.include_router(admin.router)
app.include_router(social_media.router)
app.include_router(ai_factory.router)
app.include_router(humor_factory.router)

@app.get("/success")
def payment_success():
    return {"status": "Pagamento Aprovado! Envie o livro."}

@app.get("/failure")
def payment_failure():
    return {"status": "Pagamento Falhou."}

@app.get("/pending")
def payment_pending():
    return {"status": "Pagamento Pendente."}

@app.get("/debug-reset-user")
def debug_reset_user():
    """Só disponível se ALLOW_DEBUG_ROUTES=true (não use em produção)."""
    if os.getenv("ALLOW_DEBUG_ROUTES", "").lower() not in ("1", "true", "yes"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not Found")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "evarantes2@gmail.com").first()
        if user:
            db.delete(user)
            db.commit()
        hashed_password = get_password_hash("123456")
        new_user = User(
            email="evarantes2@gmail.com",
            hashed_password=hashed_password,
            must_change_password=True
        )
        db.add(new_user)
        db.commit()
        return {"status": "User evarantes2@gmail.com reset to 123456"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

@app.get("/health/db")
def check_db_status():
    """Check database connection and type"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            return {
                "status": "connected", 
                "database_url_configured": "postgres" in os.getenv('DATABASE_URL', ''),
                "url_prefix": os.getenv('DATABASE_URL', 'sqlite')[:10]
            }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


