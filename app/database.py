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
        # Sem DATABASE_URL: SQLite
        # Tenta usar /data (persistente no Coolify) se possível/existir
        # Caso contrário (local/dev), usa diretório local
        DATA_DIR = "/data"
        
        # Verifica se /data existe e é gravável ou se estamos em Linux (container)
        use_data_dir = False
        if os.name == 'posix':
            if os.path.isdir(DATA_DIR):
                if os.access(DATA_DIR, os.W_OK):
                    use_data_dir = True
            else:
                # Tenta criar /data se tiver permissão (root)
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    use_data_dir = True
                except Exception:
                    pass

        if use_data_dir:
            DB_PATH = "/data/vibraface.db"
            if os.path.exists("/app/vibraface.db") and not os.path.exists(DB_PATH):
                try:
                    shutil.copy2("/app/vibraface.db", DB_PATH)
                except:
                    pass
            SQLALCHEMY_DATABASE_URL = "sqlite:////data/vibraface.db"
            DATABASE_DISPLAY = "SQLite (/data/vibraface.db)"
        else:
            # Fallback para local (desenvolvimento ou sem volume)
            DB_PATH = "vibraface.db"
            SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
            DATABASE_DISPLAY = f"SQLite (Local: {DB_PATH})"

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
