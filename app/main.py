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
    
    monitor_service.start()
    yield
    # Shutdown
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
