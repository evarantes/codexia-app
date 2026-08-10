"""use low cost OpenAI image default

Revision ID: d7e8f9a0b1c2
Revises: b1c2d3e4f5a6
Create Date: 2026-08-10 19:30:00.000000

Mantém a linhagem canônica do História/Devocional e migra apenas o antigo
valor padrão de imagens para o modelo econômico. Modelos personalizados não
são alterados.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE settings
        SET openai_image_model = 'gpt-image-1-mini'
        WHERE openai_image_model IS NULL
           OR TRIM(openai_image_model) = ''
           OR LOWER(TRIM(openai_image_model)) = 'gpt-image-1'
        """
    )
    op.execute(
        """
        UPDATE ai_capability_policies
        SET primary_model = 'gpt-image-1-mini',
            estimated_cost = 0.005,
            updated_at = CURRENT_TIMESTAMP
        WHERE LOWER(TRIM(primary_provider)) = 'openai'
          AND capability IN ('IMAGE_GENERATION', 'THUMBNAIL_GENERATION')
          AND (
              primary_model IS NULL
              OR TRIM(primary_model) = ''
              OR LOWER(TRIM(primary_model)) = 'gpt-image-1'
          )
        """
    )
    # Libera falhas genéricas antigas para uma única nova sondagem já protegida
    # pela classificação fatal e por max_retries=0.
    op.execute(
        """
        UPDATE ai_provider_circuit_breakers
        SET state = 'closed', consecutive_failures = 0,
            cooldown_until = NULL, half_open_remaining = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE LOWER(TRIM(provider)) = 'openai'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE settings
        SET openai_image_model = 'gpt-image-1'
        WHERE LOWER(TRIM(openai_image_model)) = 'gpt-image-1-mini'
        """
    )
    op.execute(
        """
        UPDATE ai_capability_policies
        SET primary_model = 'gpt-image-1',
            estimated_cost = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE LOWER(TRIM(primary_provider)) = 'openai'
          AND capability IN ('IMAGE_GENERATION', 'THUMBNAIL_GENERATION')
          AND LOWER(TRIM(primary_model)) = 'gpt-image-1-mini'
        """
    )
