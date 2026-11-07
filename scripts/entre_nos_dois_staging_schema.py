"""Create/verify only the three tables needed by the isolated Studio.

NEVER run on production. This is a disposable staging schema, not an Alembic
replacement for the complete Codexia application.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def ensure_isolated_database() -> None:
    if os.getenv("APP_ENV", "").strip().lower() != "staging":
        raise RuntimeError("Apenas APP_ENV=staging é permitido.")
    parsed = urlsplit(os.getenv("DATABASE_URL", ""))
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname != "studio-db" or parsed.path != "/codexia_staging":
        raise RuntimeError("Banco exclusivo studio-db/codexia_staging obrigatório.")


def main() -> int:
    ensure_isolated_database()
    from sqlalchemy import inspect
    from app.database import Base, engine
    from app.models import Tenant, User, Settings

    tables = (Tenant.__table__, User.__table__, Settings.__table__)
    Base.metadata.create_all(bind=engine, tables=list(tables), checkfirst=True)
    inspector = inspect(engine)
    for table in tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        expected = {c.name for c in table.columns}
        missing = expected - existing
        if missing:
            raise RuntimeError(f"Esquema de homologação incompatível: {table.name}.")
    print("Studio staging: esquema isolado de autenticação e configurações validado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
