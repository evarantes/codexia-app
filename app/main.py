from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.database import engine, Base, get_db
from app.routers import books, marketing, settings, video, crm, webhook, youtube, book_factory, auth
from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager
from app.services.monitor_service import monitor_service
from sqlalchemy import text, inspect
from app.models import User
from app.routers.auth import get_password_hash
from sqlalchemy.orm import Session

# Carregar variáveis de ambiente
load_dotenv()

# Create tables
Base.metadata.create_all(bind=engine)

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

            # Check for ScheduledVideo new columns
            if "scheduled_videos" in inspector.get_table_names():
                sv_columns = [c["name"] for c in inspector.get_columns("scheduled_videos")]
                with engine.connect() as conn:
                    if "progress" not in sv_columns:
                        print("Migrating: Adding progress to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN progress INTEGER DEFAULT 0"))
                    if "publish_at" not in sv_columns:
                        print("Migrating: Adding publish_at to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN publish_at DATETIME"))
                    if "auto_post" not in sv_columns:
                        print("Migrating: Adding auto_post to scheduled_videos...")
                        # Use FALSE for compatibility with both SQLite and PostgreSQL
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN auto_post BOOLEAN DEFAULT FALSE"))
                    if "youtube_video_id" not in sv_columns:
                        print("Migrating: Adding youtube_video_id to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN youtube_video_id TEXT"))
                    if "uploaded_at" not in sv_columns:
                        print("Migrating: Adding uploaded_at to scheduled_videos...")
                        conn.execute(text("ALTER TABLE scheduled_videos ADD COLUMN uploaded_at DATETIME"))
                    
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
                    conn.commit()


    except Exception as e:
        print(f"Migration warning: {e}")

def create_default_user():
    db = Session(bind=engine)
    try:
        user = db.query(User).filter(User.email == "evarantes2@gmail.com").first()
        if not user:
            print("Creating default user: evarantes2@gmail.com")
            hashed_password = get_password_hash("123456")
            user = User(
                email="evarantes2@gmail.com", 
                hashed_password=hashed_password,
                must_change_password=True
            )
            db.add(user)
            db.commit()
            print("Default user created.")
        else:
            print("Default user already exists.")
    except Exception as e:
        print(f"Error creating default user: {e}")
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"Iniciando aplicação... DATABASE_URL: {os.getenv('DATABASE_URL', 'Not Set (Using SQLite)')}")
    
    # Run auto-migrations
    run_migrations(engine)
    
    # Create default user
    create_default_user()
    
    # Start Monitor Service
    monitor_service.start()
    
    # RECOVERY: Reset any stuck 'processing' videos to 'queued'
    # This handles cases where the server crashed (OOM) during processing
    try:
        from app.models import ScheduledVideo
        db = Session(bind=engine)
        stuck_videos = db.query(ScheduledVideo).filter(ScheduledVideo.status == "processing").all()
        if stuck_videos:
            print(f"Startup Recovery: Found {len(stuck_videos)} stuck videos. Resetting to 'queued'.")
            for vid in stuck_videos:
                vid.status = "queued"
            db.commit()
        db.close()
    except Exception as e:
        print(f"Startup Recovery Error: {e}")
    
    yield
    # Shutdown
    print("Desligando aplicação...")
    monitor_service.stop()

app = FastAPI(
    title="Codexia API", 
    description="Sua fábrica de conteúdo movida a inteligência",
    lifespan=lifespan
)

# Montar arquivos estáticos
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/")
async def read_root():
    return FileResponse('app/static/index.html')

@app.get("/login.html")
async def read_login():
    return FileResponse('app/static/login.html')

@app.get("/reset-password.html")
async def read_reset_password():
    return FileResponse('app/static/reset-password.html')

# Routers
app.include_router(auth.router)
app.include_router(books.router)
app.include_router(marketing.router)
app.include_router(settings.router)
app.include_router(video.router)
app.include_router(crm.router)
app.include_router(webhook.router)
app.include_router(youtube.router)
app.include_router(book_factory.router)

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
    db = Session(bind=engine)
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


