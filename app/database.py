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
        SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "").strip()
        
        # Verifica se /data existe e é gravável ou se estamos em Linux (container)
        use_data_dir = False
        if os.name == 'posix':
            def _can_write_dir(p: str) -> bool:
                try:
                    if not os.path.isdir(p):
                        return False
                    test_path = os.path.join(p, f".__writetest__{os.getpid()}")
                    with open(test_path, "wb") as f:
                        f.write(b"1")
                    try:
                        os.remove(test_path)
                    except Exception:
                        pass
                    return True
                except Exception:
                    return False

            if SQLITE_DB_PATH:
                if SQLITE_DB_PATH.startswith("/"):
                    SQLALCHEMY_DATABASE_URL = f"sqlite:////{SQLITE_DB_PATH.lstrip('/')}"
                else:
                    SQLALCHEMY_DATABASE_URL = f"sqlite:///{SQLITE_DB_PATH}"
                DATABASE_DISPLAY = f"SQLite (Custom: {SQLITE_DB_PATH})"
                use_data_dir = False
            else:
                if os.path.isdir(DATA_DIR):
                    if _can_write_dir(DATA_DIR):
                        use_data_dir = True
                else:
                    try:
                        os.makedirs(DATA_DIR, exist_ok=True)
                        if _can_write_dir(DATA_DIR):
                            use_data_dir = True
                    except Exception:
                        pass
        elif SQLITE_DB_PATH:
            SQLALCHEMY_DATABASE_URL = f"sqlite:///{SQLITE_DB_PATH}"
            DATABASE_DISPLAY = f"SQLite (Custom: {SQLITE_DB_PATH})"
            use_data_dir = False

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
