import os
import shutil
from typing import Any, Dict

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

APP_ENV = (os.getenv("APP_ENV", "production") or "production").strip().lower()
ENABLE_SQLITE_DEV = (os.getenv("ENABLE_SQLITE_DEV", "") or "").strip().lower() in {"1", "true", "yes", "on"}
DATABASE_POLICY_ERROR = "DATABASE_URL não configurada. O Codexia requer PostgreSQL para este ambiente."
POSTGRES_REQUIRED_ENVS = {"production", "homologation", "staging", "functional_validation", "validation"}
SQLITE_DEV_MODE = APP_ENV not in POSTGRES_REQUIRED_ENVS and (ENABLE_SQLITE_DEV or APP_ENV in {"development", "local"})


def _can_write_dir(path: str) -> bool:
    try:
        if not os.path.isdir(path):
            return False
        test_path = os.path.join(path, f".__writetest__{os.getpid()}")
        with open(test_path, "wb") as f:
            f.write(b"1")
        try:
            os.remove(test_path)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _raise_database_policy_error(extra: str = "") -> None:
    message = DATABASE_POLICY_ERROR
    if extra:
        message = f"{message} {extra}".strip()
    raise RuntimeError(message)


def _resolve_sqlite_dev_url():
    # SQLite é permitido apenas em modo local explícito.
    data_dir = "/data"
    sqlite_db_path = os.getenv("SQLITE_DB_PATH", "").strip()
    use_data_dir = False

    if os.name == "posix":
        if sqlite_db_path:
            if sqlite_db_path.startswith("/"):
                return f"sqlite:////{sqlite_db_path.lstrip('/')}", f"SQLite (Custom: {sqlite_db_path})"
            return f"sqlite:///{sqlite_db_path}", f"SQLite (Custom: {sqlite_db_path})"
        if os.path.isdir(data_dir):
            if _can_write_dir(data_dir):
                use_data_dir = True
        else:
            try:
                os.makedirs(data_dir, exist_ok=True)
                if _can_write_dir(data_dir):
                    use_data_dir = True
            except Exception:
                pass
    elif sqlite_db_path:
        return f"sqlite:///{sqlite_db_path}", f"SQLite (Custom: {sqlite_db_path})"

    if use_data_dir:
        db_path = "/data/vibraface.db"
        if os.path.exists("/app/vibraface.db") and not os.path.exists(db_path):
            try:
                shutil.copy2("/app/vibraface.db", db_path)
            except Exception:
                pass
        return "sqlite:////data/vibraface.db", "SQLite (/data/vibraface.db)"

    db_path = "vibraface.db"
    return f"sqlite:///{db_path}", f"SQLite (Local: {db_path})"


# PostgreSQL é obrigatório fora do modo local explícito.
_raw = os.getenv("DATABASE_URL", "").strip()
if _raw and (_raw.lower().startswith("postgres://") or _raw.lower().startswith("postgresql://")):
    SQLALCHEMY_DATABASE_URL = _raw.replace("postgres://", "postgresql://", 1) if _raw.startswith("postgres://") else _raw
    DATABASE_DISPLAY = "PostgreSQL"
elif _raw:
    if not _raw.lower().startswith("sqlite://"):
        _raise_database_policy_error("DATABASE_URL inválida. Informe uma URL PostgreSQL válida.")
    if not SQLITE_DEV_MODE:
        _raise_database_policy_error()
    SQLALCHEMY_DATABASE_URL = _raw
    DATABASE_DISPLAY = f"SQLite (Explicit URL: {_raw})"
else:
    if not SQLITE_DEV_MODE:
        _raise_database_policy_error()
    SQLALCHEMY_DATABASE_URL, DATABASE_DISPLAY = _resolve_sqlite_dev_url()

# SQLite requires specific args, PostgreSQL does not
connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}

_is_postgres = SQLALCHEMY_DATABASE_URL.startswith("postgresql://")

engine_kwargs: Dict[str, Any] = {}
if _is_postgres:
    # Conservador: elimina vazamento primeiro (pool_pre_ping + recycle + timeout),
    # evitando QueuePool limit sem aumentar cegamente limites.
    # Aumentar pool_size/overflow apenas se, com rollback+close adequados, ainda houver gargalo.
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": 600,          # reconecta conexões após 10 min
        "pool_timeout": 30,           # espera no máximo 30s por conexão
        "pool_size": 5,               # conexões em repouso (padrão)
        "max_overflow": 15,           # pico permitido acima do pool (15, era 10)
    }

create_engine_args: Dict[str, Any] = {"connect_args": connect_args}
create_engine_args.update(engine_kwargs)

engine = create_engine(SQLALCHEMY_DATABASE_URL, **create_engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency padrão do FastAPI: abre sessão, sempre fecha no finally.
    Em caso de exceção, rola back a transação para não contaminar a conexão no pool."""
    db = SessionLocal()
    try:
        try:
            yield db
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
    finally:
        try:
            db.close()
        except Exception:
            pass

