import os
import shutil
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Se DATABASE_URL for Postgres, usar direto e NÃO mexer com /data
_raw = os.getenv("DATABASE_URL", "").strip()
if _raw and (_raw.lower().startswith("postgres://") or _raw.lower().startswith("postgresql://")):
    SQLALCHEMY_DATABASE_URL = _raw.replace("postgres://", "postgresql://", 1) if _raw.startswith("postgres://") else _raw
    DATABASE_DISPLAY = "PostgreSQL"
else:
    if _raw:
        # Outro DATABASE_URL explícito (ex: sqlite custom)
        SQLALCHEMY_DATABASE_URL = _raw
        DATABASE_DISPLAY = f"SQLite/Outro ({_raw})"
    else:
        # Sem DATABASE_URL: SQLite em /data (volume persistente no Coolify)
        DATA_DIR = "/data"
        DB_PATH = "/data/vibraface.db"
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists("/app/vibraface.db") and not os.path.exists(DB_PATH):
            shutil.copy2("/app/vibraface.db", DB_PATH)
        SQLALCHEMY_DATABASE_URL = "sqlite:////data/vibraface.db"
        DATABASE_DISPLAY = "SQLite (/data/vibraface.db)"

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
