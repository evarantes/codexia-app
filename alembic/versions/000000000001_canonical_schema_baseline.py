"""canonical schema baseline

Cria o schema inicial completo para instalações novas. Os bancos existentes
que já estão em qualquer revisão descendente não executam novamente esta
baseline; ela apenas passa a ser a raiz explícita da linhagem Alembic.

Revision ID: 000000000001
Revises:
Create Date: 2026-08-09 17:45:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "000000000001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # alembic/env.py registra todos os models modulares em Base.metadata antes
    # de executar a migration. checkfirst mantém a baseline não destrutiva para
    # instalações antigas que possuam tabelas, mas ainda não alembic_version.
    from app.database import Base

    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Baseline de adoção: nunca apagar tabelas ou dados de negócio.
    pass
