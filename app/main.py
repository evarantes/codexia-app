from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.database import engine, Base, get_db, SessionLocal, DATABASE_DISPLAY
from app.routers import books, marketing, settings, video, crm, webhook, youtube, book_factory, auth, diagnostics, hotmart, music, admin
from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager
from app.services.monitor_service import monitor_service
from sqlalchemy import text, inspect
from app.models import User
from app.routers.auth import get_password_hash

# Carregar variáveis de ambiente
load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production").lower()
IS_PRODUCTION = APP_ENV == "production"

# CORS: lista separada por vírgula (ex: https://app.example.com,https://example.com)
_cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw else ["*"]

# Caminho da pasta estática: no container é /app/app/static; localmente usa path do pacote
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_STATIC_SERVE = "/app/app/static" if os.path.isdir("/app/app/static") else str(_STATIC_DIR)

# Create tables (não derrubar o processo se o banco estiver inacessível no startup, ex.: Render)
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"AVISO: create_all falhou no startup: {e}. O app sobe mesmo assim; migrações rodam no lifespan.")

def run_migrations(engine):
    try:
        inspector = inspect(engine)
        if "books" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("books")]
            if "cover_image_base64" not in columns:
                print("Migrating: Adding missing column cover_image_base64 to books table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE books ADD COLUMN cover_image_base64 TEXT"))
                    conn.commit()
            else:
                print("Migration: Column cover_image_base64 already exists.")
        
        # Check if users table exists (create_all should handle, but just in case)
        if "tenants" not in inspector.get_table_names():
            print("Migration: Creating tenants table...")
            Base.metadata.create_all(bind=engine)
        # Garantir tenant Default existe
        with engine.connect() as conn:
            r = conn.execute(text("SELECT 1 FROM tenants WHERE slug = 'default' LIMIT 1"))
            if r.fetchone() is None:
                conn.execute(text(
                    "INSERT INTO tenants (name, slug, created_at) VALUES ('Default', 'default', CURRENT_TIMESTAMP)"
                ))
                conn.commit()
                print("Migration: Tenant 'Default' criado.")
        if "users" not in inspector.get_table_names():
             print("Migration: Creating users table...")
             Base.metadata.create_all(bind=engine)
        else:
            # Check for must_change_password column
            user_columns = [c["name"] for c in inspector.get_columns("users")]
            if "must_change_password" not in user_columns:
                print("Migrating: Adding missing column must_change_password to users table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"))
                    conn.commit()
            if "is_admin" not in user_columns:
                print("Migrating: Adding is_admin to users table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE"))
                    conn.commit()
            if "name" not in user_columns:
                print("Migrating: Adding name to users table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN name TEXT"))
                    conn.commit()
            if "role" not in user_columns:
                print("Migrating: Adding role to users table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'cliente'"))
                    conn.commit()
                with engine.connect() as conn:
                    conn.execute(text("UPDATE users SET role = 'admin' WHERE is_admin = 1"))
                    conn.commit()
            if "tenant_id" not in user_columns:
                print("Migrating: Adding tenant_id to users table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN tenant_id INTEGER"))
                    conn.commit()
                # Atribuir usuários existentes ao tenant Default (id=1)
                with engine.connect() as conn:
                    conn.execute(text("UPDATE users SET tenant_id = 1 WHERE tenant_id IS NULL"))
                    conn.commit()
                print("Migration: tenant_id adicionado; usuários existentes atribuídos a Default.")

            # Multi-tenant: user_id nas tabelas principais
            for table, col in [
                ("books", "user_id"), ("book_drafts", "user_id"), ("leads", "user_id"),
                ("settings", "user_id"), ("customers", "user_id"), ("scheduled_videos", "user_id"),
                ("channel_reports", "user_id"),
            ]:
                if table in inspector.get_table_names():
                    tcols = [c["name"] for c in inspector.get_columns(table)]
                    if col not in tcols:
                        print(f"Migrating: Adding {col} to {table}...")
                        with engine.connect() as conn:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER"))
                            conn.commit()

            # Check for ScheduledVideo new columns
            if "scheduled_videos" in inspector.get_table_names():
                sv_columns = [c["name"] for c in inspector.get_columns("scheduled_videos")]
                with engine.connect() as conn:
                    if "progress" not in sv_columns:
                        print("Migrating: Adding progress to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN progress INTEGER DEFAULT 0"))
                    if "publish_at" not in sv_columns:
                        print("Migrating: Adding publish_at to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN publish_at TIMESTAMP"))
                    if "auto_post" not in sv_columns:
                        print("Migrating: Adding auto_post to scheduled_videos...")
                        # Use FALSE for compatibility with both SQLite and PostgreSQL
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN auto_post BOOLEAN DEFAULT FALSE"))
                    if "youtube_video_id" not in sv_columns:
                        print("Migrating: Adding youtube_video_id to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN youtube_video_id TEXT"))
                    if "uploaded_at" not in sv_columns:
                        print("Migrating: Adding uploaded_at to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN uploaded_at TIMESTAMP"))
                    if "updated_at" not in sv_columns:
                        print("Migrating: Adding updated_at to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN updated_at TIMESTAMP"))

                    if "voice_style" not in sv_columns:
                        print("Migrating: Adding voice_style to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_style VARCHAR DEFAULT 'human'"))
                    
                    if "voice_gender" not in sv_columns:
                        print("Migrating: Adding voice_gender to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN voice_gender VARCHAR DEFAULT 'female'"))
                        
                    conn.commit()

            # Check for Settings new columns
            if "settings" in inspector.get_table_names():
                settings_columns = [c["name"] for c in inspector.get_columns("settings")]
                with engine.connect() as conn:
                    if "gemini_api_key" not in settings_columns:
                        print("Migrating: Adding gemini_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN gemini_api_key TEXT"))
                
                    if "deepseek_api_key" not in settings_columns:
                        print("Migrating: Adding deepseek_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN deepseek_api_key TEXT"))
                
                    if "groq_api_key" not in settings_columns:
                        print("Migrating: Adding groq_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN groq_api_key TEXT"))
                    
                    if "anthropic_api_key" not in settings_columns:
                        print("Migrating: Adding anthropic_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN anthropic_api_key TEXT"))

                    if "mistral_api_key" not in settings_columns:
                        print("Migrating: Adding mistral_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN mistral_api_key TEXT"))

                    if "openrouter_api_key" not in settings_columns:
                        print("Migrating: Adding openrouter_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN openrouter_api_key TEXT"))

                    if "ai_provider" not in settings_columns:
                        print("Migrating: Adding ai_provider to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN ai_provider TEXT DEFAULT 'openai'"))
                    
                    # Hotmart Integration
                    if "hotmart_client_id" not in settings_columns:
                        print("Migrating: Adding hotmart_client_id to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_client_id TEXT"))
                    if "hotmart_client_secret" not in settings_columns:
                        print("Migrating: Adding hotmart_client_secret to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_client_secret TEXT"))
                    if "hotmart_access_token" not in settings_columns:
                        print("Migrating: Adding hotmart_access_token to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_access_token TEXT"))
                    if "hotmart_token_expires_at" not in settings_columns:
                        print("Migrating: Adding hotmart_token_expires_at to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN hotmart_token_expires_at TIMESTAMP"))
                    if "suno_api_key" not in settings_columns:
                        print("Migrating: Adding suno_api_key to settings...")
                        conn.execute(text("ALTER TABLE settings ADD COLUMN suno_api_key TEXT"))
                    conn.commit()

            # Book drafts: cover_base64 para persistir capa em ambiente efêmero
            if "book_drafts" in inspector.get_table_names():
                bd_columns = [c["name"] for c in inspector.get_columns("book_drafts")]
                if "cover_base64" not in bd_columns:
                    print("Migrating: Adding cover_base64 to book_drafts...")
                    with engine.connect() as conn:
                        conn.execute(text("ALTER TABLE book_drafts ADD COLUMN cover_base64 TEXT"))
                        conn.commit()


    except Exception as e:
        print(f"Migration warning: {e}")

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
            return  # Usuário já existe — não alterar senha
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
            from app.models import ScheduledVideo
            db = SessionLocal()
            try:
                stuck_videos = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").all()
                if stuck_videos:
                    print(f"Startup Recovery: Found {len(stuck_videos)} stuck videos. Resetting to 'queued'.")
                    for vid in stuck_videos:
                        vid.status = "queued"
                        vid.progress = 0
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
    if isinstance(exc, (HTTPException, RequestValidationError)):
        raise exc  # deixa o handler padrão do FastAPI tratar 4xx/422
    if IS_PRODUCTION:
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    return JSONResponse(status_code=500, content={"detail": str(exc)})


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

@app.get("/media/videos/{filename:path}", response_class=FileResponse)
def serve_video_media(filename: str):
    """Serve vídeo do diretório configurado ou fallback em app/static/videos."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(filepath, media_type="video/mp4")

@app.get("/static/videos/{filename:path}", response_class=FileResponse)
def serve_video_static(filename: str):
    """Serve vídeo em URLs /static/videos/... (mesmo arquivo em VIDEO_OUTPUT_DIR ou fallback)."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=404, detail="Not Found")
    filepath = _resolve_video_path(safe_name)
    if not filepath:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(filepath, media_type="video/mp4")

# Montar /static com a pasta que contém index.html (no container: /app/app/static)
app.mount("/static", StaticFiles(directory=_STATIC_SERVE), name="static")
# Garantir que os diretórios de vídeos existem
if os.path.isdir("/data"):
    os.makedirs("/data/media/videos", exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "videos"), exist_ok=True)
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

@app.get("/ping")
def ping():
    """Simple ping for connectivity check."""
    return "pong"

@app.get("/")
async def root():
    """Serve o painel Vue (index.html) na raiz."""
    index_path = os.path.join(_STATIC_SERVE, "index.html")
    if not os.path.exists(index_path):
        return JSONResponse(
            status_code=404, 
            content={
                "error": "index.html not found", 
                "path": str(index_path), 
                "cwd": os.getcwd(),
                "static_serve": _STATIC_SERVE
            }
        )
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


