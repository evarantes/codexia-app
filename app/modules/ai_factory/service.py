import json
import re
from typing import List

from app.services.ai_generator import AIContentGenerator


class AIFactoryService:
    def __init__(self):
        self.ai_service = AIContentGenerator()

    def _clean_json_response(self, response):
        if response is None:
            return ""
        if isinstance(response, (dict, list)):
            return response
        cleaned = str(response).replace("```json", "").replace("```", "").strip()
        return cleaned

    def _parse_json_response(self, response, fallback_key="raw_content"):
        if isinstance(response, (dict, list)):
            return response
        cleaned = self._clean_json_response(response)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {fallback_key: cleaned}

    def _slugify_tag(self, value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9]+", "", (value or "").title())
        return normalized[:24] or "Humor"

    def _parse_manual_jokes(self, manual_jokes: str) -> List[str]:
        text = (manual_jokes or "").replace("\r", "\n").strip()
        if not text:
            return []

        if "\n\n" in text:
            raw_parts = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
        else:
            raw_parts = [line.strip() for line in text.split("\n") if line.strip()]

        jokes = []
        seen = set()
        for part in raw_parts:
            cleaned = re.sub(r"^\s*(?:[-*•]+|\d+[.)-]?)\s*", "", part).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned.lower() not in seen:
                jokes.append(cleaned)
                seen.add(cleaned.lower())
        return jokes

    def _ensure_family_friendly(self, jokes: List[str]):
        blocked_terms = [
            "caralho", "porra", "puta", "puta que pariu", "buceta", "foder", "foda",
            "piranha", "arrombado", "merda", "viado", "otario", "otário", "desgraca",
            "desgraça", "sexo", "transar", "pelado", "pelada", "safadeza", "safado",
        ]
        for joke in jokes:
            lowered = joke.lower()
            hits = [term for term in blocked_terms if term in lowered]
            if hits:
                raise ValueError(
                    "Foram encontrados termos inadequados para o canal sem baixaria. "
                    f"Ajuste a piada: '{joke[:100]}'"
                )

    def _build_transition(self, idx: int, total: int) -> str:
        if idx >= total:
            return "Fechamento com chamada para o proximo video e convite para comentar a favorita."
        transitions = [
            "Agora segura essa proxima porque ela vem ainda mais rapida.",
            "Tem mais uma chegando, entao nao sai dai.",
            "Se essa te pegou de surpresa, a seguinte vai te ganhar tambem.",
            "Respira e bora para a proxima, porque o ritmo nao para.",
            "Essa foi boa, mas a fila de piadas ainda esta so comecando.",
        ]
        return transitions[(idx - 1) % len(transitions)]

    def _build_manual_segment(self, joke_text: str, idx: int, total: int, category: str):
        lead_options = [
            f"Piada {idx}: olha essa do tema {category.lower()} sem exagerar na dose.",
            f"Seguinte, agora vem uma curtinha de {category.lower()} para manter o clima leve.",
            f"Bora para mais uma de {category.lower()} com o avatar firme na resenha.",
            f"Presta atencao nessa proxima do bloco {category.lower()}.",
        ]
        return {
            "position": idx,
            "lead_in": lead_options[(idx - 1) % len(lead_options)],
            "joke": joke_text,
            "transition": self._build_transition(idx, total),
            "theme_angle": category,
            "estimated_seconds": 26,
        }

    def _fallback_ai_segments(self, theme: str, category: str, tone: str, count: int, start_at: int = 1):
        themed_openers = [
            f"No clima de {theme.lower()},",
            f"Aqui no bloco de {category.lower()},",
            f"Pra quem curte humor {tone.lower()},",
            "Segura essa porque o avatar veio inspirado,",
        ]
        punchlines = [
            "o sujeito estudou tanto portugues que na prova escreveu 'sujeito oculto' e escondeu o nome.",
            "o irmao do louvor chegou tao cedo que o microfone ainda estava em jejum.",
            "o aluno disse que tirou nota alta em redacao porque colocou virgula onde sentiu paz.",
            "o portugues abriu a geladeira devagarinho para nao assustar o frio.",
            "o camarada foi tao organizado que colocou 'rir' na agenda e chegou antes da gargalhada.",
            "a senhora pediu paciencia ao ceu e recebeu um grupo de familia no horario do almoco.",
            "o rapaz foi contar uma piada curta e demorou tanto na introducao que a plateia riu da paciencia.",
            "o vizinho ficou tao gospel que ate o cafe da manha entrou no tom de gratidao.",
            "o professor perguntou por que a frase estava sem ponto final e o aluno respondeu que ainda tinha fe.",
            "o amigo disse que era humor refinado, mas refinou tanto que perdeu a piada e ficou so o brilho.",
        ]

        segments = []
        for offset in range(count):
            idx = start_at + offset
            opener = themed_openers[offset % len(themed_openers)]
            punchline = punchlines[offset % len(punchlines)]
            segments.append(
                {
                    "position": idx,
                    "lead_in": f"{opener} vai mais uma para manter o publico esperando a proxima.",
                    "joke": punchline,
                    "transition": self._build_transition(idx, start_at + count - 1),
                    "theme_angle": category,
                    "estimated_seconds": 26,
                }
            )
        return segments

    def _normalize_ai_segments(self, payload, category: str, start_at: int = 1):
        items = payload.get("jokes", []) if isinstance(payload, dict) else []
        segments = []
        for offset, item in enumerate(items, start=start_at):
            if not isinstance(item, dict):
                continue
            joke_text = (item.get("joke") or item.get("punchline") or "").strip()
            if not joke_text:
                continue
            segments.append(
                {
                    "position": offset,
                    "lead_in": (item.get("lead_in") or f"Mais uma piada curta de {category.lower()} para o publico.").strip(),
                    "joke": joke_text,
                    "transition": (item.get("transition") or self._build_transition(offset, start_at + len(items) - 1)).strip(),
                    "theme_angle": (item.get("theme_angle") or category).strip(),
                    "estimated_seconds": int(item.get("estimated_seconds") or 26),
                }
            )
        return segments

    def _generate_ai_jokes(self, theme: str, category: str, tone: str, count: int, manual_seed: List[str] = None):
        manual_seed = manual_seed or []
        prompt = f"""
        Crie {count} piadas curtas e EXCLUSIVAS em portugues do Brasil para um video longo de YouTube.

        Regras obrigatorias:
        - Humor familiar, sem baixaria, sem sexo, sem palavroes, sem humilhacao.
        - Tom: {tone}
        - Tema principal: {theme}
        - Categoria/nicho: {category}
        - Cada piada deve caber em no maximo 2 frases curtas.
        - O objetivo e manter o ouvinte querendo ouvir a proxima.
        - Se houver referencia religiosa, mantenha respeito e leveza.
        - Nao repita estrutura, trocadilho ou punchline.

        {"Piadas manuais de referencia para NAO copiar literalmente, apenas complementar o repertorio: " + json.dumps(manual_seed, ensure_ascii=False) if manual_seed else ""}

        Retorne EXCLUSIVAMENTE um JSON valido neste formato:
        {{
          "jokes": [
            {{
              "lead_in": "frase curta que introduz a piada",
              "joke": "texto principal da piada",
              "transition": "gancho curto para a proxima piada",
              "theme_angle": "{category}",
              "estimated_seconds": 26
            }}
          ]
        }}
        """
        try:
            response = self.ai_service._generate_text(prompt, temperature=0.9, json_mode=True)
            parsed = self._parse_json_response(response)
            segments = self._normalize_ai_segments(parsed, category)
            if segments:
                return segments[:count]
        except Exception:
            pass
        return self._fallback_ai_segments(theme, category, tone, count)

    def _compose_opening(self, channel_name: str, theme: str, category: str):
        return (
            f"Bem-vindo ao canal {channel_name}. Hoje o nosso avatar fixo traz uma sequencia de "
            f"piadas de {category.lower()} com tema {theme.lower()}, tudo leve, rapido e sem baixaria. "
            "Fica ate o fim porque a proxima sempre promete ser melhor."
        )

    def _compose_closing(self):
        return (
            "Se alguma piada te ganhou, comenta a favorita, envia para um amigo e volta no proximo video "
            "porque o avatar ja fica de plantao para a proxima sequencia."
        )

    def _compose_script_text(self, opening_hook: str, segments: List[dict], closing_cta: str):
        lines = [opening_hook, ""]
        for segment in segments:
            lines.append(f"Bloco {segment['position']}")
            lines.append(segment["lead_in"])
            lines.append(segment["joke"])
            lines.append(segment["transition"])
            lines.append("")
        lines.append(closing_cta)
        return "\n".join(lines).strip()

    async def generate_story(self, theme, style, audience, length):
        prompt = f"""
        Crie uma estrutura narrativa completa para uma historia com as seguintes caracteristicas:
        Tema: {theme}
        Estilo: {style}
        Publico-alvo: {audience}
        Tamanho aproximado: {length}

        Gere:
        1. Titulo criativo
        2. Sinopse envolvente
        3. Estrutura de capitulos (lista com titulo e breve descricao do que acontece)
        4. O primeiro capitulo completo.

        Retorne EXCLUSIVAMENTE em formato JSON valido com a seguinte estrutura:
        {{
            "title": "...",
            "synopsis": "...",
            "chapters_structure": [
                {{"chapter": 1, "title": "...", "summary": "..."}}
            ],
            "first_chapter_content": "..."
        }}
        """
        response = self.ai_service._generate_text(prompt)
        return self._parse_json_response(response)

    async def generate_cover_prompt(self, title, subtitle, author, style):
        prompt = f"""
        Crie um prompt detalhado e profissional para gerar uma capa de livro usando DALL-E 3.
        Titulo: {title}
        Subtitulo: {subtitle}
        Autor: {author}
        Estilo Visual: {style}

        O prompt deve garantir que o titulo, subtitulo e nome do autor aparecam na imagem de forma legivel.
        A arte deve ser exclusiva e impactante.
        """
        return self.ai_service._generate_text(prompt)

    async def generate_image(self, prompt):
        return self.ai_service.generate_image(prompt)

    async def generate_script(self, theme, duration, narrative_type):
        prompt = f"""
        Escreva um roteiro de video detalhado para o YouTube.
        Tema: {theme}
        Duracao estimada: {duration}
        Tipo de Narrativa: {narrative_type}

        O roteiro deve incluir:
        1. Titulo
        2. Introducao (Gancho)
        3. Desenvolvimento (Cenas com sugestao visual e narracao)
        4. Conclusao (CTA)

        Retorne em formato JSON:
        {{
            "title": "...",
            "script_full": "...",
            "scenes": [
                {{"scene": 1, "visual": "...", "narration": "..."}}
            ]
        }}
        """
        response = self.ai_service._generate_text(prompt)
        return self._parse_json_response(response)

    async def generate_shorts_from_script(self, script_content):
        prompt = f"""
        Com base neste roteiro de video longo, crie 3 roteiros curtos (Shorts/TikTok) de ate 60 segundos cada.
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
        return self._parse_json_response(response)

    async def generate_joke_channel_package(
        self,
        channel_name: str,
        theme: str,
        category: str,
        tone: str,
        duration_minutes: int,
        jokes_count: int,
        source_mode: str,
        manual_jokes: str,
        avatar_name: str,
        avatar_style: str,
        avatar_description: str,
        auto_publish: bool,
    ):
        safe_duration = max(int(duration_minutes or 10), 10)
        target_jokes = max(int(jokes_count or 24), safe_duration * 2)
        source_mode = (source_mode or "ai").strip().lower()
        if source_mode not in {"ai", "manual", "mixed"}:
            source_mode = "ai"

        manual_list = self._parse_manual_jokes(manual_jokes)
        if source_mode in {"manual", "mixed"} and not manual_list:
            raise ValueError("Informe pelo menos uma piada manual ou selecione o modo IA exclusiva.")

        self._ensure_family_friendly(manual_list)

        if source_mode == "manual" and len(manual_list) < target_jokes:
            raise ValueError(
                f"No modo manual, informe pelo menos {target_jokes} piadas para atingir a meta de {safe_duration} minutos "
                "ou escolha o modo misto para completar com a IA."
            )

        manual_segments = []
        if source_mode in {"manual", "mixed"}:
            used_manual = manual_list if source_mode == "manual" else manual_list[:target_jokes]
            manual_segments = [
                self._build_manual_segment(joke_text, idx, len(used_manual), category)
                for idx, joke_text in enumerate(used_manual, start=1)
            ]

        remaining = max(target_jokes - len(manual_segments), 0)
        ai_segments = []
        if source_mode in {"ai", "mixed"} and remaining > 0:
            ai_segments = self._generate_ai_jokes(theme, category, tone, remaining, manual_seed=manual_list)
            for idx, segment in enumerate(ai_segments, start=len(manual_segments) + 1):
                segment["position"] = idx

        segments = (manual_segments + ai_segments)[:target_jokes]
        if not segments:
            raise ValueError("Nao foi possivel montar o roteiro de piadas com os dados informados.")
        for idx, segment in enumerate(segments, start=1):
            segment["position"] = idx
            segment["transition"] = self._build_transition(idx, len(segments))

        opening_hook = self._compose_opening(channel_name, theme, category)
        closing_cta = self._compose_closing()
        script_text = self._compose_script_text(opening_hook, segments, closing_cta)

        avatar_visual_prompt = (
            f"Friendly fixed storyteller avatar named {avatar_name}, {avatar_style}, {avatar_description}. "
            "Medium shot, warm studio lighting, family friendly comedy vibe, consistent clothes, "
            "subtle mouth movement for lip sync, almost no body movement, expressive face, clean background, "
            "vertical composition for YouTube shorts poster style, no text."
        ).strip()
        avatar_image_url = self.ai_service.generate_image(avatar_visual_prompt, aspect_ratio="9:16")

        estimated_duration_minutes = round((30 + len(segments) * 26 + 18) / 60, 1)
        recommended_title = (
            f"{channel_name}: {len(segments)} piadas curtas de {category.lower()} "
            f"para rir sem baixaria | {safe_duration}+ min"
        )
        recommended_description = (
            f"Uma maratona de piadas curtas sobre {theme.lower()} no estilo {tone.lower()}, "
            "com avatar fixo, sincronia labial leve e foco total em humor familiar. "
            "O video foi preparado para revisao antes da automacao de publicacao."
        )

        notes = []
        if source_mode == "mixed":
            notes.append("Pacote montado em modo misto: piadas manuais + complementos exclusivos da IA.")
        if auto_publish:
            notes.append("Aprovacao final pode seguir direto para automacao quando o pipeline de render/publicacao estiver conectado.")

        return {
            "project_summary": (
                f"Canal de piadas '{channel_name}' com avatar fixo '{avatar_name}', tema {theme} "
                f"e foco em {category.lower()} para videos longos de humor leve."
            ),
            "channel_positioning": {
                "channel_name": channel_name,
                "theme": theme,
                "category": category,
                "tone": tone,
                "source_mode": source_mode,
            },
            "avatar": {
                "name": avatar_name,
                "style": avatar_style,
                "description": avatar_description,
                "visual_prompt": avatar_visual_prompt,
                "image_url": avatar_image_url,
                "movement_direction": "Avatar fixo em quadro medio com sincronizacao labial e gestos minimos.",
            },
            "production_blueprint": {
                "target_duration_minutes": safe_duration,
                "estimated_duration_minutes": estimated_duration_minutes,
                "jokes_target_count": target_jokes,
                "approved_for_review": True,
                "render_direction": "Cena unica com avatar recorrente, cortes suaves e boca sincronizada com a locucao.",
            },
            "opening_hook": opening_hook,
            "segments": segments,
            "closing_cta": closing_cta,
            "script_full": script_text,
            "review_checklist": [
                "Conferir se todas as piadas estao adequadas para publico familiar.",
                "Validar consistencia visual do avatar entre a imagem e o roteiro.",
                "Confirmar ritmo: cada bloco deve deixar curiosidade para a proxima piada.",
                "Checar se o video final ficou com 10 minutos ou mais antes de aprovar.",
                "Liberar automacao de publicacao apenas apos a revisao manual.",
            ],
            "publication": {
                "recommended_title": recommended_title,
                "recommended_description": recommended_description,
                "hashtags": [
                    f"#{self._slugify_tag(channel_name)}",
                    f"#{self._slugify_tag(category)}",
                    "#PiadasSemBaixaria",
                    "#HumorLeve",
                    "#CanalDePiadas",
                ],
                "auto_publish": bool(auto_publish),
            },
            "notes": notes,
        }
