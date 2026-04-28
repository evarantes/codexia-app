import re
import os
import json
import base64
from typing import Any, Dict, List, Optional

import openai


class OpenAIImageModule:
    def __init__(self, ai_service):
        self.ai_service = ai_service

    def split_lyrics_into_sections(self, lyrics: str) -> List[Dict[str, str]]:
        text = (lyrics or "").strip().replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return []

        lines = [ln.rstrip() for ln in text.split("\n")]
        sections: List[Dict[str, str]] = []
        current_title: Optional[str] = None
        current_lines: List[str] = []

        def is_heading(line: str) -> Optional[str]:
            t = (line or "").strip()
            if not t:
                return None
            low = t.lower()
            low = re.sub(r"[\[\]():\-–—]+", " ", low).strip()
            low = re.sub(r"\s+", " ", low)
            if re.match(r"^(verso|verse)\b", low):
                return "Verso"
            if re.match(r"^(pre[\s\-]?refr[aã]o|pr[eé][\s\-]?refr[aã]o|pre[\s\-]?chorus)\b", low):
                return "Pré-refrão"
            if re.match(r"^(refr[aã]o|chorus)\b", low):
                return "Refrão"
            if re.match(r"^(ponte|bridge)\b", low):
                return "Ponte"
            if re.match(r"^(intro|introdu[cç][aã]o)\b", low):
                return "Intro"
            if re.match(r"^(outro|final|coda)\b", low):
                return "Outro"
            return None

        def flush():
            nonlocal current_title, current_lines
            body = "\n".join([x for x in current_lines if x is not None]).strip()
            if body:
                sections.append(
                    {
                        "title": current_title or "Trecho",
                        "text": body,
                    }
                )
            current_title = None
            current_lines = []

        for ln in lines:
            h = is_heading(ln)
            if h:
                flush()
                current_title = h
                continue
            current_lines.append(ln)

        flush()
        if not sections:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
            for p in paragraphs:
                sections.append({"title": "Trecho", "text": p})
        return sections

    def _coerce_prompt_language(self, prompt_language: str, lyrics: str) -> str:
        lang = (prompt_language or "auto").strip().lower()
        if lang in {"pt", "pt-br", "pt_br", "portugues", "português"}:
            return "pt-BR"
        if lang in {"en", "english", "ing", "ingles", "inglês"}:
            return "en"
        if lang != "auto":
            return "en"
        t = (lyrics or "").lower()
        pt_hits = sum(1 for w in [" não ", "ção", "ções", "senhor", "glória", "vitória", "coração", "espírito", "igreja", "jesus"] if w in f" {t} ")
        return "pt-BR" if pt_hits >= 2 else "en"

    def interpretar_letra(self, lyrics: str, sections: List[Dict[str, str]], prompt_language: str = "auto") -> Dict[str, Any]:
        prompt_lang = self._coerce_prompt_language(prompt_language, lyrics)
        sections_desc = "\n".join([f"- {s.get('title')}: {s.get('text','')[:600]}" for s in (sections or [])][:24])
        safe_rules = (
            "Regra CRÍTICA: nunca interprete expressões bíblicas de forma literal e sombria. "
            "Se aparecer 'olhos como fogo', interprete como autoridade divina, santidade e poder (luz radiante), nunca como terror. "
            "Se aparecer 'espada', interprete como autoridade da Palavra (luz simbólica), nunca como arma grotesca. "
            "Se aparecer 'morte', interprete como vitória sobre a morte, esperança e redenção."
        )
        output_language = "Português (pt-BR)" if prompt_lang == "pt-BR" else "English (en)"
        prompt = f"""
Você é um especialista em interpretação bíblica e diretor de arte cinematográfico. Seu objetivo é transformar uma LETRA COMPLETA em um resumo semântico e cenas visuais coerentes.

LEIA A LETRA COMPLETA antes de decidir qualquer coisa.

{safe_rules}

LETRA COMPLETA:
{(lyrics or '').strip()[:6500]}

TRECHOS (use para quebrar por cenas; 1 cena por trecho por padrão):
{sections_desc}

Saída obrigatória: retorne APENAS um JSON válido com esta estrutura:
{{
  "tema": "",
  "emocao": "",
  "ambiente": "",
  "personagens": [],
  "estilo_visual": "",
  "descricao_cena": "",
  "cenas": [
    {{
      "trecho_titulo": "",
      "descricao_cena": "",
      "emocao": "",
      "ambiente": "",
      "personagens": []
    }}
  ],
  "idioma_prompt": "{output_language}"
}}

Regras para os campos:
- "tema": 3-8 palavras.
- "emocao": 2-6 palavras (ex: reverência, esperança, poder, glória).
- "ambiente": 3-12 palavras (ex: visão celestial, ilha rochosa, trono, templo, céu).
- "personagens": lista curta (ex: "Jesus glorificado", "João", "multidão em adoração").
- "estilo_visual": descreva em 1 frase a estética sacra e cinematográfica (glow dourado, raios de luz, cores vibrantes).
- "descricao_cena": 1 frase resumindo a cena central da música como um todo, sem terror.
- "cenas": gere um item por trecho, na mesma ordem do input; mantenha consistência de ambiente/personagens quando aplicável.
"""
        try:
            raw = self.ai_service._generate_text(
                prompt,
                system_prompt="Return only valid JSON. Never output horror interpretations for biblical symbolism.",
                temperature=0.35,
                json_mode=True,
            )
            raw = (raw or "").replace("```json", "").replace("```", "").strip()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
            if "cenas" not in data or not isinstance(data.get("cenas"), list):
                data["cenas"] = []
            if data:
                return data
        except Exception:
            pass

        norm = f" {(lyrics or '').lower()} "
        theme = "adoração" if any(k in norm for k in [" adora", " worship", " louvor", " exaltar"]) else (
            "vitória" if any(k in norm for k in [" vitória", " vencer", " conquer", " triunfo", " triumph"]) else (
                "redenção" if any(k in norm for k in [" reden", " salva", " resgate", " redemption", " salvation"]) else "fé e esperança"
            )
        )
        emotion = "reverência e esperança" if any(k in norm for k in [" rever", " esperança", " hope", " temor"]) else "glória e alegria"
        ambience = "visão celestial" if any(k in norm for k in [" céu", " heaven", " trono", " throne", " anjo", " angel"]) else (
            "ilha rochosa e visão espiritual" if any(k in norm for k in [" ilha", " patmos", " island"]) else "atmosfera espiritual"
        )
        characters: List[str] = []
        if any(k in norm for k in [" jesus", " jesús", " christ", " filho do homem", " son of man", " senhor", " lord"]):
            characters.append("Jesus glorificado")
        if any(k in norm for k in [" joão", " joao", " john"]):
            characters.append("João")
        if any(k in norm for k in [" igreja", " congreg", " multid", " crowd", " povo", " saints"]):
            characters.append("multidão em adoração")
        if not characters:
            characters = ["figuras bíblicas reverentes"]

        cenas = []
        for s in (sections or []):
            cenas.append(
                {
                    "trecho_titulo": (s.get("title") or "Trecho").strip(),
                    "descricao_cena": (s.get("text") or "").strip()[:800],
                    "emocao": emotion,
                    "ambiente": ambience,
                    "personagens": characters,
                }
            )
        return {
            "tema": theme,
            "emocao": emotion,
            "ambiente": ambience,
            "personagens": characters,
            "estilo_visual": "Cinematográfico sacro, luz dourada celestial, raios de luz (god rays), cores vibrantes, reverente e inspirador.",
            "descricao_cena": "Representação reverente e cinematográfica do sentido espiritual da música, com luz divina vencendo qualquer sombra.",
            "cenas": cenas,
            "idioma_prompt": "Português (pt-BR)" if prompt_lang == "pt-BR" else "English (en)",
        }

    def _style_tokens(self, visual_style: str, spiritual_intensity: str, mode: str) -> Dict[str, str]:
        style = (visual_style or "cinematic").strip().lower()
        intensity = (spiritual_intensity or "epic").strip().lower()
        mode_low = (mode or "").strip().lower()

        if mode_low in {"devocional", "devotional"}:
            intensity = "leve"
        if mode_low in {"epico", "épico", "epic"}:
            intensity = "epico"

        style_line = "photorealistic cinematic art" if style in {"cinematic", "cinematografico", "cinematográfico"} else (
            "photorealistic" if style in {"realista", "realistic"} else "sacred contemporary art, high-quality illustration"
        )

        if intensity in {"leve", "light"}:
            intensity_line = "soft divine light, gentle god rays, intimate worship atmosphere, warm golden glow"
        elif intensity in {"forte", "strong"}:
            intensity_line = "strong divine light, pronounced god rays, luminous golden illumination, powerful holy presence"
        else:
            intensity_line = "epic divine light, intense god rays, grand heavenly atmosphere, throne-room scale, blazing golden illumination"

        mode_line = "devotional, soft, inspiring, peaceful" if mode_low in {"devocional", "devotional"} else "epic, majestic, triumphant, awe-inspiring"
        return {"style": style_line, "intensity": intensity_line, "mode": mode_line}

    def build_dalle_prompt(
        self,
        global_semantic: Dict[str, Any],
        scene_semantic: Dict[str, Any],
        options: Dict[str, Any],
        prompt_language: str,
    ) -> str:
        tokens = self._style_tokens(
            options.get("visual_style") or "cinematic",
            options.get("spiritual_intensity") or "epic",
            options.get("mode") or "epic",
        )
        theme = (global_semantic.get("tema") or "").strip()
        emotion = (scene_semantic.get("emocao") or global_semantic.get("emocao") or "").strip()
        ambience = (scene_semantic.get("ambiente") or global_semantic.get("ambiente") or "").strip()
        characters = scene_semantic.get("personagens") or global_semantic.get("personagens") or []
        if not isinstance(characters, list):
            characters = []
        scene_desc = (scene_semantic.get("descricao_cena") or global_semantic.get("descricao_cena") or "").strip()
        trecho = (scene_semantic.get("trecho_titulo") or "").strip()

        if prompt_language == "pt-BR":
            return "\n".join(
                [
                    "Cinematic, epic, high quality, 4k, dramatic lighting.",
                    f"Scene: {scene_desc or 'Cena bíblica reverente baseada no contexto completo da música.'}",
                    f"Theme: {theme}" if theme else "Theme: adoração e esperança",
                    f"Environment: {ambience}" if ambience else "Environment: atmosfera celestial e espiritual",
                    f"Characters: {', '.join([str(c) for c in characters if str(c).strip()][:6])}" if characters else "Characters: figuras bíblicas reverentes",
                    "Style:",
                    f"- {tokens['style']}",
                    "- Bíblico",
                    "- Glorioso",
                    "- Luz dourada celestial",
                    "- Atmosfera espiritual",
                    "- Não sombrio, não terror",
                    f"- {tokens['intensity']}",
                    f"- {tokens['mode']}",
                    "Emotion:",
                    f"- {emotion}" if emotion else "- Reverência",
                    "- Poder",
                    "- Esperança",
                    "Important:",
                    "- Evitar horror",
                    "- Evitar distorções",
                    "- Representar santidade e autoridade",
                    "- Sem texto, sem watermark, sem logos",
                    f"Section: {trecho}" if trecho else "",
                ]
            ).strip()

        return "\n".join(
            [
                "Cinematic, epic, high quality, 4k, dramatic lighting.",
                f"Scene: {scene_desc or 'Reverent biblical scene based on the full meaning of the song, not isolated words.'}",
                f"Theme: {theme}" if theme else "Theme: worship and hope",
                f"Environment: {ambience}" if ambience else "Environment: heavenly spiritual atmosphere",
                f"Characters: {', '.join([str(c) for c in characters if str(c).strip()][:6])}" if characters else "Characters: reverent biblical figures",
                "Style:",
                f"- {tokens['style']}",
                "- Biblical",
                "- Glorious",
                "- Golden heavenly light",
                "- Spiritual atmosphere",
                "- Not dark, not horror",
                f"- {tokens['intensity']}",
                f"- {tokens['mode']}",
                "Emotion:",
                f"- {emotion}" if emotion else "- Reverence",
                "- Power",
                "- Hope",
                "Important:",
                "- Avoid horror",
                "- Avoid distortions",
                "- Represent holiness and divine authority",
                "- No text, no watermark, no logos",
                f"Section: {trecho}" if trecho else "",
            ]
        ).strip()

    def _allocate_scenes(self, sections: List[Dict[str, str]], count: int) -> List[Dict[str, str]]:
        secs = [s for s in (sections or []) if (s.get("text") or "").strip()]
        if not secs:
            return []
        target = max(1, min(40, int(count or 1)))
        if target <= len(secs):
            return secs[:target]

        out: List[Dict[str, str]] = []
        remaining = target
        for s in secs:
            if remaining <= 0:
                break
            txt = (s.get("text") or "").strip()
            parts = [p.strip() for p in re.split(r"\n\s*\n+", txt) if p.strip()]
            if len(parts) <= 1:
                out.append(s)
                remaining -= 1
                continue
            take = min(len(parts), max(1, remaining))
            for i in range(take):
                out.append({"title": s.get("title") or "Trecho", "text": parts[i]})
                remaining -= 1
                if remaining <= 0:
                    break
        while remaining > 0:
            out.append(out[-1])
            remaining -= 1
        return out[:target]

    def generate_images_from_lyrics(self, lyrics: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = dict(options or {})
        sections = self.split_lyrics_into_sections(lyrics)
        images_count = int(opts.get("images_count") or 1)
        allocated = self._allocate_scenes(sections, images_count)
        prompt_language = self._coerce_prompt_language(opts.get("prompt_language") or "auto", lyrics)

        semantic = self.interpretar_letra(lyrics, allocated, prompt_language=prompt_language) or {}
        global_semantic = semantic if isinstance(semantic, dict) else {}
        scene_semantics = global_semantic.get("cenas") if isinstance(global_semantic, dict) else None
        if not isinstance(scene_semantics, list):
            scene_semantics = []

        model = (opts.get("model") or "").strip() or (os.getenv("OPENAI_IMAGE_MODEL") or "").strip() or "gpt-image-1"
        size = (opts.get("size") or "1024x1024").strip() or "1024x1024"
        quality = (opts.get("quality") or "").strip() or "standard"
        response_format = (opts.get("response_format") or "").strip() or ("b64_json" if model == "gpt-image-1" else "url")

        def _openai_generate_image(prompt: str) -> Optional[str]:
            api_key = (getattr(self.ai_service, "api_key", "") or "").strip()
            if not api_key:
                return None
            try:
                if hasattr(openai, "OpenAI"):
                    client = openai.OpenAI(api_key=api_key)
                    resp = client.images.generate(
                        model=model,
                        prompt=prompt,
                        size=size,
                        quality=quality,
                        n=1,
                        response_format=response_format,
                    )
                    if resp and getattr(resp, "data", None) and resp.data:
                        item0 = resp.data[0]
                        url = getattr(item0, "url", None)
                        if isinstance(url, str) and url.strip():
                            return url.strip()
                        b64 = getattr(item0, "b64_json", None)
                        if isinstance(b64, str) and b64.strip():
                            return f"data:image/png;base64,{b64.strip()}"
                else:
                    openai.api_key = api_key
                    resp = openai.Image.create(
                        prompt=prompt,
                        size=size,
                        n=1,
                    )
                    if isinstance(resp, dict) and resp.get("data"):
                        url = resp["data"][0].get("url")
                        if isinstance(url, str) and url.strip():
                            return url.strip()
            except Exception:
                pass
            if response_format != "url":
                try:
                    if hasattr(openai, "OpenAI"):
                        client = openai.OpenAI(api_key=api_key)
                        resp = client.images.generate(
                            model=model,
                            prompt=prompt,
                            size=size,
                            quality=quality,
                            n=1,
                            response_format="url",
                        )
                        if resp and getattr(resp, "data", None) and resp.data:
                            url = getattr(resp.data[0], "url", None)
                            if isinstance(url, str) and url.strip():
                                return url.strip()
                except Exception:
                    pass
            if response_format != "b64_json":
                try:
                    if hasattr(openai, "OpenAI"):
                        client = openai.OpenAI(api_key=api_key)
                        resp = client.images.generate(
                            model=model,
                            prompt=prompt,
                            size=size,
                            quality=quality,
                            n=1,
                            response_format="b64_json",
                        )
                        if resp and getattr(resp, "data", None) and resp.data:
                            b64 = getattr(resp.data[0], "b64_json", None)
                            if isinstance(b64, str) and b64.strip():
                                return f"data:image/png;base64,{b64.strip()}"
                except Exception:
                    pass
            return None

        items: List[Dict[str, Any]] = []
        for idx, s in enumerate(allocated):
            ss = scene_semantics[idx] if idx < len(scene_semantics) and isinstance(scene_semantics[idx], dict) else {}
            if isinstance(ss, dict):
                ss = dict(ss)
            else:
                ss = {}
            if not ss.get("trecho_titulo"):
                ss["trecho_titulo"] = (s.get("title") or "Trecho").strip()
            if not ss.get("descricao_cena"):
                ss["descricao_cena"] = (s.get("text") or "").strip()[:800]
            dalle_prompt = self.build_dalle_prompt(global_semantic, ss, opts, prompt_language)
            dalle_prompt = self.ai_service._sanitize_and_contextualize_image_prompt(dalle_prompt)
            image_url = _openai_generate_image(dalle_prompt)
            items.append(
                {
                    "index": idx + 1,
                    "section_title": ss.get("trecho_titulo") or (s.get("title") or "Trecho"),
                    "section_text": (s.get("text") or "").strip()[:1200],
                    "semantic": {
                        "tema": global_semantic.get("tema") if isinstance(global_semantic, dict) else "",
                        "emocao": ss.get("emocao") or global_semantic.get("emocao") if isinstance(global_semantic, dict) else "",
                        "ambiente": ss.get("ambiente") or global_semantic.get("ambiente") if isinstance(global_semantic, dict) else "",
                        "personagens": ss.get("personagens") or global_semantic.get("personagens") if isinstance(global_semantic, dict) else [],
                    },
                    "prompt_language": prompt_language,
                    "prompt": dalle_prompt,
                    "image_url": image_url,
                }
            )

        return {
            "summary": {
                "tema": global_semantic.get("tema") if isinstance(global_semantic, dict) else "",
                "emocao": global_semantic.get("emocao") if isinstance(global_semantic, dict) else "",
                "ambiente": global_semantic.get("ambiente") if isinstance(global_semantic, dict) else "",
                "personagens": global_semantic.get("personagens") if isinstance(global_semantic, dict) else [],
                "estilo_visual": global_semantic.get("estilo_visual") if isinstance(global_semantic, dict) else "",
                "descricao_cena": global_semantic.get("descricao_cena") if isinstance(global_semantic, dict) else "",
            },
            "items": items,
        }
