"""
Alembic env: usa PostgreSQL como banco oficial do projeto.
Registra metadata dos models do app para gerar migrações.
"""
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context
from dotenv import load_dotenv

load_dotenv()

# Carrega app.database respeitando a politica central de banco.
from app.database import SQLALCHEMY_DATABASE_URL
from app.database import Base

# Importa todos os models declarativos para registrar em Base.metadata.
# Sem isso, o autogenerate do Alembic ignora tabelas modulares `codexia_*`
# e passa a divergir do schema real usado pela aplicacao.
from app import models  # noqa: F401
from app.modules.ai_factory import models as ai_factory_models  # noqa: F401
from app.modules.bible_video_factory import models as bible_video_factory_models  # noqa: F401
from app.modules.humor_factory import models as humor_factory_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def get_url():
    return SQLALCHEMY_DATABASE_URL

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
