import os
import shutil
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


def _normalize_database_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    if url.lower().startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _int_env(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _build_postgres_url_from_parts() -> str:
    host = os.getenv("POSTGRES_HOST", "").strip()
    port = os.getenv("POSTGRES_PORT", "5432").strip()
    database = os.getenv("POSTGRES_DB", "").strip()
    user = os.getenv("POSTGRES_USER", "").strip()
    password = os.getenv("POSTGRES_PASSWORD", "").strip()
    sslmode = os.getenv("POSTGRES_SSLMODE", "").strip()

    if not all([host, database, user, password]):
        return ""

    base = (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(database)}"
    )
    return f"{base}?sslmode={sslmode}" if sslmode else base


def _resolve_sqlite_url() -> tuple[str, str]:
    # Sem PostgreSQL configurado: fallback para SQLite.
    # Em ambiente com volume, usa /data. Em local, usa arquivo local.
    data_dir = "/data"
    use_data_dir = False

    if os.name == "posix":
        if os.path.isdir(data_dir):
            use_data_dir = os.access(data_dir, os.W_OK)
        else:
            try:
                os.makedirs(data_dir, exist_ok=True)
                use_data_dir = True
            except Exception:
                use_data_dir = False

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


APP_ENV = os.getenv("APP_ENV", "production").lower()
IS_PRODUCTION = APP_ENV == "production"

database_url = _normalize_database_url(os.getenv("DATABASE_URL", ""))
if not database_url:
    database_url = _build_postgres_url_from_parts()

if database_url:
    SQLALCHEMY_DATABASE_URL = database_url
    if database_url.startswith("postgresql"):
        DATABASE_DISPLAY = "PostgreSQL"
    elif database_url.startswith("sqlite://"):
        DATABASE_DISPLAY = "SQLite (DATABASE_URL)"
    else:
        DATABASE_DISPLAY = "Custom DB URL"
else:
    SQLALCHEMY_DATABASE_URL, DATABASE_DISPLAY = _resolve_sqlite_url()

DB_IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
DB_IS_POSTGRES = SQLALCHEMY_DATABASE_URL.startswith("postgresql")

connect_args = {"check_same_thread": False} if DB_IS_SQLITE else {}
engine_kwargs = {}

if DB_IS_POSTGRES:
    # Pooling recomendado para producao em PostgreSQL.
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_recycle": _int_env("DB_POOL_RECYCLE", 1800),
            "pool_size": _int_env("DB_POOL_SIZE", 5),
            "max_overflow": _int_env("DB_MAX_OVERFLOW", 10),
        }
    )

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

if IS_PRODUCTION and not DB_IS_POSTGRES:
    print(
        "AVISO: APP_ENV=production sem PostgreSQL configurado. "
        "Recomendado definir DATABASE_URL para PostgreSQL."
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
