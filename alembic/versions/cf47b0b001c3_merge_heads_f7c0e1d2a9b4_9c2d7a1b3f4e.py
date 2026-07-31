"""merge_heads_f7c0e1d2a9b4_9c2d7a1b3f4e

Revision ID: cf47b0b001c3
Revises: f7c0e1d2a9b4, 9c2d7a1b3f4e
Create Date: 2026-07-31 13:11:03.187335

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cf47b0b001c3'
down_revision: Union[str, None] = ('f7c0e1d2a9b4', '9c2d7a1b3f4e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
