from __future__ import annotations

import math

from app.services.cinematic_director import CinematicDirector
from app.services.cinematic_duration_contract import _duration_tolerance_ratio, _voice_wpm


class ContractAwareCinematicDirector(CinematicDirector):
    """Claude director that knows the exact narration clock before drafting."""

    def _user_prompt(self, *, theme: str, content_type: str, duration_minutes: int, budget_brl: float) -> str:
        base = super()._user_prompt(
            theme=theme,
            content_type=content_type,
            duration_minutes=duration_minutes,
            budget_brl=budget_brl,
        )
        duration = max(1, int(duration_minutes or 1))
        wpm = _voice_wpm()
        tolerance = _duration_tolerance_ratio()
        target_words = int(round(duration * wpm))
        min_words = int(math.floor(target_words * (1.0 - tolerance)))
        max_words = int(math.ceil(target_words * (1.0 + tolerance)))
        return (
            base
            + f"""

CONTRATO DE DURAÇÃO — TEM PRIORIDADE SOBRE QUALQUER REGRA GENÉRICA:
- Duração contratada: {duration} minutos ({duration * 60} segundos).
- Ritmo usado pelo Codexia para validar a locução: {wpm} palavras por minuto.
- Alvo: {target_words} palavras narradas.
- Faixa aceita antes de liberar a produção: {min_words} a {max_words} palavras.
- A soma dos campos "narration" de TODAS as cenas deve ficar dentro dessa faixa.
- O campo "full_script" deve ser EXATAMENTE a concatenação, em ordem, dos campos "narration" das cenas. Não crie um segundo roteiro mais longo escondido em full_script.
- Distribua os "target_seconds" de forma coerente e faça a soma se aproximar de {duration * 60} segundos.
- Se houver conflito entre quantidade de cenas e duração, preserve a qualidade da mensagem e ajuste o tamanho das narrações; não use repetição ou enchimento.
- O Codexia medirá isso automaticamente. Um plano fora da faixa será devolvido a você e NÃO será considerado aprovado.
""".rstrip()
        )
