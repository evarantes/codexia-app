"""
Serviço do Canal de Piadas.
- Geração de piadas por IA (baseado no tema)
- Inserção manual de piadas
- Temas: português, religiosa, gospel, família, etc.
"""
import json
from app.services.ai_generator import AIContentGenerator
from app.models import JOKES_THEMES


class JokesService:
    def __init__(self):
        self.ai = AIContentGenerator()

    def get_themes(self):
        """Retorna lista de temas disponíveis."""
        return [{"id": t[0], "label": t[1]} for t in JOKES_THEMES]

    def generate_jokes(
        self,
        theme: str,
        quantity: int = 15,
        duration_min: int = 10,
    ) -> list[dict]:
        """
        Gera piadas exclusivas via IA com base no tema.
        Piadas curtas, sem baixaria, adequadas ao tema.
        """
        theme_label = next((t[1] for t in JOKES_THEMES if t[0] == theme), theme)

        prompt = f"""
Você é um comediante de stand-up que conta piadas leves e engraçadas, SEM baixaria, SEM palavrões, SEM conteúdo ofensivo.
Tema: {theme_label}

Gere exatamente {quantity} piadas CURTAS (cada uma com 1-3 frases no máximo).
Cada piada deve ser engraçada e prender a atenção. Formato: pode ser pergunta-resposta ou narrativa curta.

IMPORTANTE: O texto será NARRADO por um avatar em vídeo. Escreva de forma natural para fala.
Cada piada deve durar entre 15 e 45 segundos quando narrada.

Retorne EXCLUSIVAMENTE um JSON válido com esta estrutura:
{{
    "jokes": [
        {{
            "punchline": "Texto completo da piada para narrar (inclui setup + punchline se houver)",
            "setup": "Opcional: parte inicial separada se quiser pausa dramática"
        }}
    ]
}}
"""
        try:
            response = self.ai._generate_text(prompt, json_mode=True)
            if not response:
                return []
            clean = response.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean)
            jokes = data.get("jokes", [])
            if not isinstance(jokes, list):
                return []
            result = []
            for i, j in enumerate(jokes[:quantity]):
                punchline = (j.get("punchline") or j.get("text") or "").strip()
                if not punchline:
                    continue
                result.append({
                    "idx": i + 1,
                    "punchline": punchline[:2000],
                    "setup": (j.get("setup") or "").strip() or None,
                    "source": "ai",
                    "theme": theme,
                })
            return result
        except Exception as e:
            raise ValueError(f"Erro ao gerar piadas: {e}")

    def parse_manual_jokes(self, text: str, theme: str = "geral") -> list[dict]:
        """
        Parseia texto com piadas manuais (uma por linha ou separadas por ---).
        """
        lines = []
        for block in text.split("---"):
            block = block.strip()
            if not block:
                continue
            for line in block.split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)

        result = []
        for i, punchline in enumerate(lines, start=1):
            if len(punchline) > 10:
                result.append({
                    "idx": i,
                    "punchline": punchline[:2000],
                    "setup": None,
                    "source": "manual",
                    "theme": theme,
                })
        return result
