import os
import shutil
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Use DATABASE_URL env var if available (PostgreSQL/Cloud); else SQLite em /data (persistência no Docker)
if os.getenv("DATABASE_URL", "").strip():
    SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL").strip()
    # Fix for Render/Heroku providing postgres:// instead of postgresql://
    if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    # SQLite: persistir em /data no Docker (volume); criar pasta e migrar se necessário
    DATA_DIR = "/data"
    DB_PATH = "/data/vibraface.db"
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists("/app/vibraface.db") and not os.path.exists(DB_PATH):
        shutil.copy2("/app/vibraface.db", DB_PATH)
    SQLALCHEMY_DATABASE_URL = "sqlite:////data/vibraface.db"

# SQLite requires specific args, PostgreSQL does not
connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args=connect_args
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
