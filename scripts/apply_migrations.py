"""Aplica o head Alembic canônico antes de iniciar API ou worker."""
from __future__ import annotations

import pathlib
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SQLALCHEMY_DATABASE_URL  # noqa: E402


_POSTGRES_LOCK_ID = 2_026_080_901


def main() -> int:
    engine = create_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool)
    lock_connection = None
    try:
        dialect = str(engine.dialect.name or "").lower()
        if dialect == "postgresql":
            lock_connection = engine.connect()
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _POSTGRES_LOCK_ID},
            )

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        command.upgrade(config, "head")
        print("[alembic] upgrade head concluído.")
        return 0
    except Exception as exc:
        print(f"[alembic] falha: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if lock_connection is not None:
            try:
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _POSTGRES_LOCK_ID},
                )
            except Exception:
                pass
            lock_connection.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
