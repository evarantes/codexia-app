import pathlib
import sys
from typing import Dict, Iterable, List

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SQLALCHEMY_DATABASE_URL  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: F401,E402
from app.modules.ai_factory import models as ai_factory_models  # noqa: F401,E402
from app.modules.bible_video_factory import models as bible_video_factory_models  # noqa: F401,E402
from app.modules.humor_factory import models as humor_factory_models  # noqa: F401,E402


TASK_TABLES: Dict[str, Iterable[str]] = {
    "video_task_dedupe": {
        "idempotency_key",
        "request_hash",
        "task_id",
        "status",
        "request_payload_json",
        "result_json",
        "created_at",
        "updated_at",
        "expires_at",
        "completed_at",
    },
    "video_task_leases": {
        "task_id",
        "executor_id",
        "attempt_number",
        "created_at",
        "updated_at",
        "started_at",
        "heartbeat_at",
        "expires_at",
        "lease_expires_at",
    },
    "video_task_locks": {
        "lock_key",
        "owner_id",
        "created_at",
        "updated_at",
        "expires_at",
    },
}


def _print(msg: str) -> None:
    print(str(msg))


def _alembic_heads() -> List[str]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    return list(script.get_heads())


def _current_revisions(engine) -> List[str]:
    with engine.connect() as conn:
        inspector = inspect(conn)
        if "alembic_version" not in inspector.get_table_names():
            return []
        rows = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]


def _require_single_head(heads: List[str]) -> str:
    if not heads:
        raise RuntimeError("Alembic sem head definido no repositório.")
    if len(heads) > 1:
        raise RuntimeError(f"Alembic com múltiplos heads: {', '.join(heads)}")
    return heads[0]


def _check_task_tables(engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing_tables = [name for name in TASK_TABLES if name not in tables]
    if missing_tables:
        raise RuntimeError(
            "Tabelas críticas ausentes no PostgreSQL: " + ", ".join(sorted(missing_tables))
        )

    missing_columns = []
    for table_name, expected_columns in TASK_TABLES.items():
        current_columns = {col["name"] for col in inspector.get_columns(table_name)}
        for column_name in sorted(expected_columns):
            if column_name not in current_columns:
                missing_columns.append(f"{table_name}.{column_name}")
    if missing_columns:
        raise RuntimeError(
            "Colunas críticas ausentes no PostgreSQL: " + ", ".join(missing_columns)
        )


def _check_metadata_registration() -> None:
    metadata_tables = set(Base.metadata.tables.keys())
    expected_module_tables = {
        "codexia_ai_stories",
        "codexia_bible_video_series",
        "codexia_humor_projects",
    }
    missing = sorted(expected_module_tables - metadata_tables)
    if missing:
        raise RuntimeError(
            "Base.metadata não registrou todos os models modulares: " + ", ".join(missing)
        )


def main() -> int:
    try:
        _check_metadata_registration()
        engine = create_engine(SQLALCHEMY_DATABASE_URL)
        heads = _alembic_heads()
        head = _require_single_head(heads)
        current = _current_revisions(engine)
        if not current:
            raise RuntimeError("Tabela alembic_version ausente ou sem revisão aplicada.")
        if current != [head]:
            raise RuntimeError(
                f"Revisão atual divergente. current={current} head={head}"
            )
        _check_task_tables(engine)
        _print(f"[schema] Alembic head único: {head}")
        _print(f"[schema] Revisão atual do banco: {current[0]}")
        _print("[schema] Tabelas críticas de idempotência OK.")
        return 0
    except Exception as exc:
        _print(f"[schema] falha: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
