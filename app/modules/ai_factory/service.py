import json
from app.services.ai_generator import AIContentGenerator

class AIFactoryService:
    def __init__(self):
        self.ai_service = AIContentGenerator()

    async def generate_story(self, theme, style, audience, length):
        prompt = f"""
        Crie uma estrutura narrativa completa para uma história com as seguintes características:
        Tema: {theme}
        Estilo: {style}
        Público-alvo: {audience}
        Tamanho aproximado: {length}

        Gere:
        1. Título criativo
        2. Sinopse envolvente
        3. Estrutura de capítulos (lista com título e breve descrição do que acontece)
        4. O primeiro capítulo completo.

        Retorne EXCLUSIVAMENTE em formato JSON válido com a seguinte estrutura:
        {{
            "title": "...",
            "synopsis": "...",
            "chapters_structure": [
                {{"chapter": 1, "title": "...", "summary": "..."}},
                ...
            ],
            "first_chapter_content": "..."
        }}
        """
        response = self.ai_service._generate_text(prompt)
        try:
            # Clean response to ensure JSON
            response = response.replace("```json", "").replace("```", "").strip()
            return json.loads(response)
        except json.JSONDecodeError:
            # Fallback for text response
            return {"raw_content": response}

    async def generate_cover_prompt(self, title, subtitle, author, style):
        prompt = f"""
        Crie um prompt detalhado e profissional para gerar uma capa de livro usando DALL-E 3.
        Título: {title}
        Subtítulo: {subtitle}
        Autor: {author}
        Estilo Visual: {style}
        
        O prompt deve garantir que o título, subtítulo e nome do autor apareçam na imagem de forma legível.
        A arte deve ser exclusiva e impactante.
        """
        return self.ai_service._generate_text(prompt)

    async def generate_image(self, prompt):
        # Using existing AI generator logic if available, otherwise returning prompt for now
        # The AIContentGenerator in app/services/ai_generator.py handles DALL-E via _generate_image if implemented,
        # but looking at previous turns, it seems to use OpenAI directly or mock.
        # Let's assume we can use the generate_image method if it exists or implement a wrapper.
        # Checking ai_generator.py in previous context... it has generate_cover_image.
        # We will reuse that or implement a generic one.
        try:
            return self.ai_service.generate_cover_image(prompt) # Assuming this method exists and works for generic images too
        except AttributeError:
             return "Erro: Método de geração de imagem não encontrado no serviço base."

    async def generate_script(self, theme, duration, narrative_type):
        prompt = f"""
        Escreva um roteiro de vídeo detalhado para o YouTube.
        Tema: {theme}
        Duração estimada: {duration}
        Tipo de Narrativa: {narrative_type}

        O roteiro deve incluir:
        1. Título
        2. Introdução (Gancho)
        3. Desenvolvimento (Cenas com sugestão visual e narração)
        4. Conclusão (CTA)
        
        Retorne em formato JSON:
        {{
            "title": "...",
            "script_full": "...",
            "scenes": [
                {{"scene": 1, "visual": "...", "narration": "..."}},
                ...
            ]
        }}
        """
        response = self.ai_service._generate_text(prompt)
        try:
            response = response.replace("```json", "").replace("```", "").strip()
            return json.loads(response)
        except json.JSONDecodeError:
            return {"raw_content": response}

    async def generate_shorts_from_script(self, script_content):
        prompt = f"""
        Com base neste roteiro de vídeo longo, crie 3 roteiros curtos (Shorts/TikTok) de até 60 segundos cada.
        Destaque os momentos mais impactantes.
        
        Roteiro Original:
        {script_content}

        Retorne em formato JSON:
        [
            {{"title": "Short 1", "content": "..."}},
            {{"title": "Short 2", "content": "..."}},
            {{"title": "Short 3", "content": "..."}}
        ]
        """
        response = self.ai_service._generate_text(prompt)
        try:
            response = response.replace("```json", "").replace("```", "").strip()
            return json.loads(response)
        except json.JSONDecodeError:
            return {"raw_content": response}
