import os
import uuid
import base64
import openai
import requests
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from app.database import SessionLocal
from app.models import Settings
from sqlalchemy.exc import OperationalError, SQLAlchemyError

load_dotenv()

class AIContentGenerator:
    def __init__(self):
        self.api_key = None
        self.gemini_key = None
        self.deepseek_key = None
        self.groq_key = None
        self.anthropic_key = None
        self.mistral_key = None
        self.openrouter_key = None
        self.openrouter_model = None
        self.edenai_key = None
        self.leonardo_key = None
        self.leonardo_model_id = None
        self.elevenlabs_key = None
        self.elevenlabs_voice_id = None
        self.elevenlabs_voice_name = None
        self.provider = "openai"
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN")

    def _load_config(self):
        # Tenta carregar do banco primeiro, depois do .env
        db = SessionLocal()
        settings = None
        try:
            settings = db.query(Settings).order_by(Settings.id.desc()).first()
        except OperationalError as e:
            print(f"AVISO: Falha ao carregar Settings do banco (migração pendente?): {e}")
        except SQLAlchemyError as e:
            print(f"AVISO: Falha ao carregar Settings do banco (erro SQL): {e}")
        except Exception as e:
            print(f"AVISO: Falha ao carregar Settings do banco: {e}")
        finally:
            db.close()

        self.api_key = None
        self.gemini_key = None
        self.deepseek_key = None
        self.groq_key = None
        self.anthropic_key = None
        self.mistral_key = None
        self.openrouter_key = None
        self.openrouter_model = None
        self.edenai_key = None
        self.leonardo_key = None
        self.leonardo_model_id = None
        self.elevenlabs_key = None
        self.elevenlabs_voice_id = None
        self.elevenlabs_voice_name = None
        self.provider = "openrouter"
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN") # Para MusicGen

        if settings:
            self.api_key = settings.openai_api_key
            self.gemini_key = settings.gemini_api_key
            self.deepseek_key = settings.deepseek_api_key
            self.groq_key = settings.groq_api_key
            self.anthropic_key = settings.anthropic_api_key
            self.mistral_key = settings.mistral_api_key
            self.openrouter_key = settings.openrouter_api_key
            self.openrouter_model = getattr(settings, "openrouter_model", None)
            self.edenai_key = getattr(settings, "edenai_api_key", None)
            self.leonardo_key = getattr(settings, "leonardo_api_key", None)
            self.leonardo_model_id = getattr(settings, "leonardo_model_id", None)
            self.elevenlabs_key = settings.elevenlabs_api_key
            self.elevenlabs_voice_id = getattr(settings, "elevenlabs_voice_id", None)
            self.elevenlabs_voice_name = getattr(settings, "elevenlabs_voice_name", None)
            self.provider = settings.ai_provider or "openrouter"
        
        # Fallback to env vars
        if not self.api_key: self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.gemini_key: self.gemini_key = os.getenv("GEMINI_API_KEY")
        if not self.deepseek_key: self.deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if not self.groq_key: self.groq_key = os.getenv("GROQ_API_KEY")
        if not self.anthropic_key: self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.mistral_key: self.mistral_key = os.getenv("MISTRAL_API_KEY")
        if not self.openrouter_key: self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not self.openrouter_model: self.openrouter_model = os.getenv("OPENROUTER_MODEL")
        if not self.edenai_key: self.edenai_key = os.getenv("EDENAI_API_KEY")
        if not self.leonardo_key: self.leonardo_key = os.getenv("LEONARDO_API_KEY")
        if not self.leonardo_model_id: self.leonardo_model_id = os.getenv("LEONARDO_MODEL_ID")
        if not self.elevenlabs_key: self.elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

    def _generate_text(self, prompt, system_prompt=None, temperature=0.7, json_mode=False):
        """Gera texto via OpenRouter (gateway único para LLMs)."""
        self._load_config()
        if not self.openrouter_key:
            return "{}" if json_mode else "Conteúdo gerado por IA (Simulação - Sem Chave)"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        raw_model = (self.openrouter_model or "").strip()
        raw_model_norm = raw_model.lower()
        if not raw_model or raw_model_norm in {"auto", "automático", "automatico", "melhor", "best"}:
            model = "openrouter/auto"
        else:
            model = raw_model

        client = openai.OpenAI(
            api_key=self.openrouter_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"},
        )

        def _call(model_id: str, allow_json_mode: bool):
            kwargs = {
                "model": model_id,
                "messages": messages,
                "temperature": temperature,
            }
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content

        try:
            try:
                return _call(model, allow_json_mode=True)
            except Exception:
                return _call(model, allow_json_mode=False)
        except Exception as e:
            if model != "openrouter/auto":
                try:
                    try:
                        return _call("openrouter/auto", allow_json_mode=True)
                    except Exception:
                        return _call("openrouter/auto", allow_json_mode=False)
                except Exception:
                    raise e
            raise e

    def generate_book_section(self, section_type, context_text, title, existing_content=None):
        """Generates specific book sections like synopsis, epigraph, preface. Can rewrite existing content."""
        self._load_config()
        # Verify if any key is available
        if not self.openrouter_key:
             return "Conteúdo gerado por IA (Simulação - Sem Chave)"

        base_prompt = f"Escreva um texto para {section_type} do livro '{title}'. Contexto: {context_text}..."
        
        if existing_content and len(existing_content.strip()) > 50:
            # Rewrite mode
            base_prompt = f"""
            ATUE COMO UM EDITOR E REESCREVA a seção '{section_type}' do livro '{title}'.
            
            CONTEXTO E INSTRUÇÕES:
            {context_text}
            
            CONTEÚDO ORIGINAL (Use como base, mas aplique as instruções acima):
            {existing_content}
            
            IMPORTANTE:
            1. Mantenha a essência do conteúdo original, mas adapte conforme as novas instruções.
            2. Se as instruções pedirem para corrigir algo (ex: número de páginas, referências), FAÇA A CORREÇÃO.
            3. Retorne APENAS o novo texto reescrito.
            """

        prompts = {
            "synopsis": f"Escreva uma sinopse instigante para a quarta capa do livro '{title}'. Baseado neste contexto: {context_text}...",
            "epigraph": f"Sugira uma epígrafe (citação curta e profunda) que combine com o tema do livro '{title}'. Contexto: {context_text}...",
            "preface": f"Escreva um prefácio curto para o livro '{title}', introduzindo o tema e preparando o leitor. Contexto: {context_text}...",
            "dedication": f"Sugira uma dedicatória genérica e emocionante para o livro '{title}'.",
            "introduction": f"Escreva uma introdução envolvente para o livro '{title}', apresentando os conceitos principais. Contexto: {context_text}...",
            "epilogue": f"Escreva um epílogo conclusivo para o livro '{title}', amarrando as pontas soltas e oferecendo uma reflexão final. Contexto: {context_text}...",
            "conclusion": f"Escreva uma conclusão resumida para o livro '{title}', recapitulando os pontos principais. Contexto: {context_text}...",
            "chapter": f"Escreva o conteúdo completo para o capítulo '{title}'. Mantenha o estilo do livro. Contexto: {context_text}..."
        }
        
        # If rewriting, use base_prompt constructed above. Otherwise use specific prompt from dict or fallback.
        if existing_content and len(existing_content.strip()) > 50:
            prompt = base_prompt
        else:
            prompt = prompts.get(section_type, base_prompt)

        try:
            content = self._generate_text(prompt)
            if not content:
                return "Erro: Nenhuma IA configurada."
            return content
        except Exception as e:
            print(f"Erro ao gerar seção {section_type}: {e}")
            return f"Erro ao gerar {section_type}: {str(e)}"

    def generate_full_book_draft(self, title: str, idea: str, num_chapters: int, style: str = "didático", num_pages: int = 50):
        """Generates a full book structure and content based on an idea"""
        self._load_config()
        
        if not self.openrouter_key:
            # Mock response
            return {
                "dedication": "Aos sonhadores.",
                "acknowledgments": "Agradeço à IA.",
                "introduction": "Esta é uma introdução gerada automaticamente.",
                "preface": "Um prefácio curto.",
                "epigraph": "O conhecimento é poder.",
                "chapters": [
                    {"title": f"Capítulo {i+1}", "content": f"Conteúdo simulado do capítulo {i+1} sobre {idea}..."} 
                    for i in range(num_chapters)
                ],
                "cover_url": "https://placehold.co/400x600?text=Capa+Simulada"
            }

        # Estimate word count based on pages (approx 250-300 words per page)
        total_words = num_pages * 250
        words_per_chapter = max(300, int(total_words / max(1, num_chapters)))

        # 1. Generate Outline
        outline_prompt = f"""
        Atue como um autor best-seller. Crie o planejamento de um livro completo.
        Título: {title}
        Ideia Central: {idea}
        Número de Capítulos: {num_chapters}
        Estimativa de Páginas: {num_pages} (aprox. {total_words} palavras no total)
        Estilo: {style}

        Retorne APENAS um JSON com a seguinte estrutura:
        {{
            "dedication": "Sugestão de dedicatória",
            "epigraph": "Sugestão de epígrafe",
            "chapters": [
                {{"title": "Título do Cap 1", "summary": "Breve resumo do que abordar neste capítulo"}},
                {{"title": "Título do Cap 2", "summary": "Breve resumo do que abordar neste capítulo"}}
            ]
        }}
        """

        try:
            import json
            
            # Using unified generator
            content = self._generate_text(outline_prompt, json_mode=True)
            if not content:
                 raise Exception("Falha na geração do outline (resposta vazia)")
                 
            content = content.replace("```json", "").replace("```", "").strip()
            structure = json.loads(content)
            
            # 2. Generate Cover (Parallel if possible, but sequential here for simplicity)
            # We generate 1 suggestion
            try:
                cover_urls = self.generate_cover_options(title, idea, n=1)
                structure["cover_url"] = cover_urls[0] if cover_urls else None
            except Exception as e:
                print(f"Erro ao gerar capa: {e}")
                structure["cover_url"] = None

            # 3. Generate Content for each chapter
            # Note: For a real production app, this should be done in background or streamed.
            # Here we do it sequentially but keep it concise to avoid timeout.
            final_chapters = []
            
            for i, chap in enumerate(structure.get("chapters", [])):
                chap_title = chap.get("title", f"Capítulo {i+1}")
                chap_summary = chap.get("summary", "")
                
                content_prompt = f"""
                Escreva o conteúdo completo do Capítulo {i+1} de {len(structure.get("chapters", []))}: '{chap_title}' do livro '{title}'.
                Contexto do capítulo: {chap_summary}
                Estilo: {style}
                Meta de tamanho: Aprox. {words_per_chapter} palavras.
                
                IMPORTANTE: 
                1. NÃO repita o título "Capítulo {i+1}" ou o nome do capítulo no início do texto. Comece diretamente o conteúdo.
                2. Mantenha a coerência com os capítulos anteriores e posteriores.
                3. Escreva de forma envolvente, detalhada e bem estruturada. Use parágrafos claros.
                """
                
                chap_content = self._generate_text(content_prompt)
                
                final_chapters.append({
                    "title": chap_title,
                    "content": chap_content or "Conteúdo não gerado."
                })
            
            structure["chapters"] = final_chapters
            
            # Fill other sections if missing
            if "introduction" not in structure:
                structure["introduction"] = self.generate_book_section("introduction", idea, title)
            if "preface" not in structure:
                structure["preface"] = self.generate_book_section("preface", idea, title)
            if "acknowledgments" not in structure:
                structure["acknowledgments"] = self.generate_book_section("acknowledgments", idea, title)

            return structure

        except Exception as e:
            error_msg = str(e)
            print(f"Erro ao gerar livro: {error_msg}")
            
            # Tratamento amigável para erro de cota
            if "insufficient_quota" in error_msg or "429" in error_msg:
                raise Exception(
                    "Créditos da IA esgotados. Verifique sua cota na OpenAI ou Gemini."
                )
            
            raise e

    def analyze_manuscript_structure(self, text_sample):
        """Analyzes text to identify potential structure (chapters) and extracts content"""
        self._load_config()
        
        import re
        
        # Structure to hold results
        structure = {
            "dedication": "",
            "acknowledgments": "",
            "introduction": "",
            "preface": "",
            "epigraph": "",
            "chapters": []
        }

        # Regex patterns for section headers
        # Order matters: check for specific sections first
        patterns = [
            (r'(?i)^(?:dedicatória|dedication)\s*$', 'dedication'),
            (r'(?i)^(?:agradecimentos|acknowledgments)\s*$', 'acknowledgments'),
            (r'(?i)^(?:introdução|introduction)\s*$', 'introduction'),
            (r'(?i)^(?:prefácio|preface)\s*$', 'preface'),
            (r'(?i)^(?:epígrafe|epigraph)\s*$', 'epigraph'),
            # Broadest chapter matching:
            # 1. "Capítulo 1" or "Chapter 1" (standard), allowing leading symbols like emojis/bullets
            (r'(?i)^[\W_]*(?:cap[ií]tulo|chapter)\s+([0-9IVX]+)(?:[\s:-]+(.*))?', 'chapter')
        ]
        
        lines = text_sample.split('\n')
        
        current_section_type = None 
        current_content = []
        current_title = ""
        
        def save_section():
            nonlocal current_section_type, current_content, current_title
            
            content_str = "\n".join(current_content).strip()
            if not content_str:
                return

            if current_section_type == 'chapter':
                structure['chapters'].append({
                    "title": current_title,
                    "content": content_str
                })
            elif current_section_type in structure:
                structure[current_section_type] = content_str
            
            # Reset content but keep type until new header found (actually type resets on new header)
        
        skip_next = False
        
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
                
            line = line.strip()
            
            # Check for header
            is_header = False
            
            # Skip very long lines for header check (headers are usually short)
            if len(line) < 100 and line:
                for pattern, type_name in patterns:
                    match = re.match(pattern, line)
                    if match:
                        # Found a new header!
                        # 1. Save previous section
                        save_section()
                        
                        # 2. Start new section
                        current_section_type = type_name
                        current_content = []
                        is_header = True
                        
                        if type_name == 'chapter':
                            # Extract chapter title
                            chap_num = match.group(1)
                            title_suffix = match.group(2) if match.lastindex >= 2 else ""
                            
                            if title_suffix and title_suffix.strip():
                                clean_suffix = title_suffix.strip().lstrip(":-").strip()
                                current_title = f"Capítulo {chap_num}: {clean_suffix}"
                            elif i + 1 < len(lines) and len(lines[i+1].strip()) < 100 and lines[i+1].strip():
                                # Check next line for title
                                current_title = f"Capítulo {chap_num}: {lines[i+1].strip()}"
                                skip_next = True # Consume next line as title
                            else:
                                current_title = f"Capítulo {chap_num}"
                        else:
                            current_title = line.title()
                        
                        break
            
            if not is_header:
                current_content.append(line)
        
        # Save last section
        save_section()
        
        # Fallback: if no chapters found but we have content
        if not structure['chapters'] and not any([structure[k] for k in structure if k != 'chapters']):
             # If completely failed to find structure, return whole text as chapter 1
             structure['chapters'].append({"title": "Conteúdo Completo", "content": text_sample})

        return structure


    def generate_ad_copy(self, book_title: str, synopsis: str, style: str = "cliffhanger"):
        # Recarrega config a cada chamada para pegar atualizações
        self._load_config()

        if not self.openrouter_key:
            return self._mock_response(book_title, style)

        prompt = self._build_prompt(book_title, synopsis, style)
        
        try:
            return self._generate_text(prompt, system_prompt="Você é um especialista em copywriting para venda de livros. Crie textos persuasivos, emocionantes e com alto potencial de conversão.") or "Erro na geração."
        except Exception as e:
            print(f"Erro na IA: {e}")
            return self._mock_response(book_title, style, error=str(e))

    def generate_cover_options(self, title: str, context: str, author: str = "", subtitle: str = "", n: int = 3):
        self._load_config()

        print(f"DEBUG: Generating covers for '{title}' with context: {context[:100]}...")
        if not self.edenai_key:
            colors = ["1e293b", "4f46e5", "059669"]
            return [f"https://placehold.co/400x600/{color}/ffffff?text={title[:10]}...%0A{author}" for i, color in enumerate(colors[:n])]

        import json

        title_display = title.strip() if title else "Livro"
        author_display = author.strip() if author else ""
        subtitle_display = subtitle.strip() if subtitle else ""

        prompt_gen_prompt = f"""
        Crie {n} descrições visuais artísticas e EXCLUSIVAS para a capa do livro '{title_display}'.
        Contexto/Mensagem Central: {context[:500]}

        Retorne APENAS um JSON:
        {{
          "prompts": ["descrição 1", "descrição 2", "descrição 3"]
        }}
        """

        try:
            raw = self._generate_text(prompt_gen_prompt, json_mode=True) or "{}"
            raw = raw.replace("```json", "").replace("```", "").strip()
            prompts_data = json.loads(raw) if raw else {}
            prompts = prompts_data.get("prompts", []) if isinstance(prompts_data, dict) else []
        except Exception as e:
            print(f"Error generating cover prompts: {e}")
            prompts = []

        while len(prompts) < n:
            prompts.append(f"Ilustração conceitual para a capa do livro, tema: {context[:120]}")

        def edenai_image_url(text_prompt: str, resolution: str):
            url = "https://api.edenai.run/v2/image/generation/"
            headers = {"Authorization": f"Bearer {self.edenai_key}", "Content-Type": "application/json"}
            payload = {
                "providers": "openai/dall-e-3,stabilityai/stable-diffusion-xl-1024-v1-0",
                "text": text_prompt,
                "resolution": resolution,
                "num_images": 1,
                "response_as_dict": True,
                "attributes_as_list": False,
            }
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            if r.status_code >= 400:
                raise Exception(f"Eden AI HTTP {r.status_code}: {r.text[:240]}")
            data = r.json() or {}
            for provider in ["openai", "stabilityai"]:
                provider_payload = data.get(provider) or {}
                items = provider_payload.get("items") or []
                if items and isinstance(items[0], dict):
                    item0 = items[0]
                    for k in ["image_resource_url", "image_url", "url", "image"]:
                        v = item0.get(k)
                        if isinstance(v, str) and v.strip().startswith("http"):
                            return v.strip()
            return None

        image_urls = []
        for p in prompts[:n]:
            cover_prompt = (
                f"Capa de livro, arte digital 2D plana, sem mockup 3D. "
                f"Título: \"{title_display}\". "
                + (f"Subtítulo: \"{subtitle_display}\". " if subtitle_display else "")
                + (f"Autor: \"{author_display}\". " if author_display else "")
                + f"Descrição visual: {p}. "
                "Sem marcas d'água, sem texto extra."
            )
            try:
                url = edenai_image_url(cover_prompt, resolution="1024x1792")
                image_urls.append(url or "https://placehold.co/400x600?text=Capa+Indispon%C3%ADvel")
            except Exception as e_img:
                print(f"Error generating cover image: {e_img}")
                image_urls.append("https://placehold.co/400x600?text=Cover+Error")

        while len(image_urls) < n:
            image_urls.append("https://placehold.co/400x600?text=Cover+Error")

        return image_urls

    def generate_music_placeholder(self, prompt: str):
        """Gera música a partir de um prompt (Placeholder)"""
        # Implementação futura com MusicGen/HuggingFace
        print(f"Solicitação de música recebida: {prompt}")
        return None

    def generate_video_script(self, book_title: str, synopsis: str, style: str = "drama"):
        self._load_config()
        
        # Se não tiver chave, retorna mock
        if not self.openrouter_key:
            return {
                "title": f"Trailer: {book_title}",
                "scenes": [
                    {"text": f"Conheça a história de {book_title}", "image_prompt": "capa do livro misteriosa"},
                    {"text": "Um segredo que pode mudar tudo...", "image_prompt": "pessoa olhando para o horizonte com suspense"},
                    {"text": "Disponível agora!", "image_prompt": "livro em cima de uma mesa de madeira"}
                ],
                "music_mood": style
            }

        prompt = f"""
        Crie um Roteiro de Vídeo Curto (TikTok/Reels) para o livro '{book_title}'.
        Sinopse: '{synopsis}'.
        Estilo: {style}.
        
        Retorne APENAS um JSON válido com a seguinte estrutura, sem explicações adicionais:
        {{
            "title": "Título do Vídeo",
            "scenes": [
                {{"text": "Frase narrada da cena 1", "image_prompt": "Descrição visual artística e altamente detalhada da cena 1 em inglês, focada em criar uma ilustração digital única e original, sem texto na imagem"}},
                {{"text": "Frase narrada da cena 2", "image_prompt": "Descrição visual artística e altamente detalhada da cena 2 em inglês, focada em criar uma ilustração digital única e original, sem texto na imagem"}}
            ],
            "music_mood": "{style}"
        }}
        Máximo de 4 cenas.
        """

        try:
            content = self._generate_text(
                prompt, 
                system_prompt="Você é um roteirista de vídeo especialista em trailers de livros. Retorne apenas JSON.",
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            # Tenta limpar markdown se houver
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
                
            return json.loads(content.strip())
        except Exception as e:
            print(f"Erro ao gerar script de vídeo: {e}")
            return {
                "title": f"Trailer: {book_title}",
                "scenes": [
                    {"text": f"Descubra {book_title}", "image_prompt": "book cover artistic"},
                    {"text": "Uma história incrível espera por você", "image_prompt": "fantasy world landscape"},
                    {"text": "Leia agora!", "image_prompt": "person reading a book happily"}
                ],
                "music_mood": style
            }

    def generate_short_script_from_prompt(self, prompt: str):
        """Gera roteiro de YouTube Short (vertical, ~30-60s) a partir de um único prompt."""
        self._load_config()
        if not self.openrouter_key:
            return {
                "title": "Short gerado",
                "description": "Um short criado automaticamente com base na mensagem do vídeo. #shorts",
                "scenes": [
                    {"text": "Um momento que inspira.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Vale a pena persistir.", "image_prompt": "person overcoming challenge"},
                    {"text": "Inscreva-se para mais!", "image_prompt": "call to action minimal"}
                ],
                "music_mood": "drama"
            }
        system = (
            "Você é um roteirista de YouTube Shorts e Reels. Crie roteiros curtos, impactantes, "
            "com frases de efeito. Cada cena deve ter 1-2 frases no máximo (5-15 segundos de fala). "
            "Retorne APENAS um JSON válido, sem explicações."
        )
        user_prompt = f"""
        Crie um roteiro de YouTube Short (vídeo vertical, 30-60 segundos no total) com base neste pedido:

        "{prompt}"

        Regras:
        - Título: uma frase chamativa (máx. 60 caracteres).
        - Descrição: 2-4 linhas com CTA e hashtags relevantes (inclua #shorts).
        - Cenas: entre 3 e 5 cenas. Cada cena: "text" (frase narrada, curta) e "image_prompt" (descrição visual em inglês para gerar imagem com IA, sem texto na imagem).
        - Estilo: dinâmico, adequado para Shorts/Reels, gancho no início.

        Retorne APENAS este JSON (sem markdown, sem texto extra):
        {{
            "title": "Título do Short",
            "description": "Descrição do short com hashtags",
            "scenes": [
                {{"text": "Frase da cena 1", "image_prompt": "descrição visual artística da cena 1"}},
                {{"text": "Frase da cena 2", "image_prompt": "descrição visual artística da cena 2"}}
            ],
            "music_mood": "drama"
        }}
        """
        try:
            content = self._generate_text(
                user_prompt,
                system_prompt=system,
                json_mode=True
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            import json
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            data = json.loads(content.strip())
            if not data.get("scenes"):
                data["scenes"] = [
                    {"text": "Um momento inspirador.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Persista e conquiste.", "image_prompt": "person overcoming challenge"}
                ]
            desc = (data.get("description") or "").strip()
            if not desc:
                try:
                    desc_prompt = (
                        f"Crie uma descrição curta (2-4 linhas) para um YouTube Short com base neste pedido:\n\n"
                        f"\"{prompt}\"\n\n"
                        "Regras: inclua CTA (inscreva-se/curta/compartilhe), inclua hashtags relevantes e #shorts. "
                        "Retorne apenas o texto da descrição (sem aspas, sem markdown)."
                    )
                    gen_desc = (self._generate_text(desc_prompt, system_prompt="Você é um copywriter de YouTube. Retorne só o texto.", json_mode=False) or "").strip()
                    if gen_desc:
                        data["description"] = gen_desc[:1200]
                except Exception:
                    pass
            return data
        except Exception as e:
            print(f"Erro ao gerar script de Short: {e}")
            return {
                "title": "Short inspirador",
                "description": "Um short criado automaticamente com base na mensagem do vídeo. #shorts",
                "scenes": [
                    {"text": "Um momento que inspira.", "image_prompt": "cinematic inspiring scene"},
                    {"text": "Vale a pena persistir.", "image_prompt": "person overcoming challenge"},
                    {"text": "Inscreva-se para mais!", "image_prompt": "call to action minimal"}
                ],
                "music_mood": "drama"
            }

    def generate_motivational_script(self, topic, duration_minutes=5):
        """Gera um roteiro longo para vídeo motivacional"""
        self._load_config()
        if not self.openrouter_key:
            return self._mock_response(topic, "motivational_long", duration=duration_minutes)

        # Estimate word count: approx 150 words per minute
        target_word_count = duration_minutes * 150
        min_scenes = max(5, duration_minutes * 2) # At least 2 scenes per minute

        prompt = f"""
        Crie um Roteiro de Vídeo Motivacional Profundo de {duration_minutes} minutos sobre '{topic}'.
        Estilo: Inspirador, Estoico, Narrativa Poderosa.
        Meta de Palavras: Aproximadamente {target_word_count} palavras.
        
        O roteiro deve ser estruturado para manter a retenção e COBRIR O TEMPO SOLICITADO.
        Divida em pelo menos {min_scenes} cenas/partes para garantir dinamismo.
        Estrutura sugerida: Introdução, Problema, Virada, Desenvolvimento (longo), Solução/Mindset, Conclusão/CTA.
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "title": "Título Impactante (SEO Friendly)",
            "description": "Descrição otimizada para YouTube com hashtags",
            "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
            "scenes": [
                {{"text": "Texto EXATO da narração (sem 'Cena 1:', sem 'Narrador:', apenas o que será falado). Deve ser longo o suficiente...", "image_prompt": "Descrição visual..."}},
                ...
            ],
            "music_mood": "epic_cinematic"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um roteirista de vídeos motivacionais virais. Seus roteiros são longos, profundos e respeitam o tempo solicitado.",
                temperature=0.8,
                json_mode=True
            )
            
            import json
            if not content:
                raise Exception("Resposta vazia da IA")

            # Limpeza básica de markdown json
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao gerar roteiro motivacional: {e}")
            return self._mock_response(topic, "motivational_long", error=str(e), duration=duration_minutes)

    def generate_script_from_text(self, text, duration_minutes=5):
        """Estrutura um texto existente em formato de roteiro de vídeo"""
        self._load_config()
        if not self.openrouter_key:
            return self._mock_response("História do Usuário", "motivational_long")

        prompt = f"""
        Atue como um Editor de Vídeo Profissional.
        Eu tenho uma história/texto pronto e quero transformá-lo em um vídeo narrado de aproximadamente {duration_minutes} minutos.
        
        TEXTO ORIGINAL:
        "{text}"
        
        Sua tarefa:
        1. Divida este texto em cenas lógicas para narração. MANTENHA O SENTIDO ORIGINAL E A MAIORIA DO TEXTO, apenas ajuste para fluidez se necessário.
        2. Para cada cena, crie um 'image_prompt' visual, artístico e detalhado para gerar imagens com IA (DALL-E).
        3. Defina um título e descrição para o YouTube.
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "title": "Título Sugerido",
            "description": "Descrição para YouTube",
            "tags": ["tag1", "tag2"],
            "scenes": [
                {{"text": "Trecho da narração da cena 1...", "image_prompt": "Descrição visual detalhada em inglês..."}},
                {{"text": "Trecho da narração da cena 2...", "image_prompt": "Descrição visual detalhada em inglês..."}}
            ],
            "music_mood": "emotional_cinematic"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um editor de vídeo profissional. Retorne apenas JSON.",
                temperature=0.7,
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao estruturar roteiro do texto: {e}")
            return self._mock_response("História do Usuário", "motivational_long", error=str(e))

    def generate_story_or_devotional_text(
        self,
        instruction: str,
        kind: str = "story",
        duration_min_minutes: int = 10,
        duration_max_minutes: Optional[int] = None,
    ) -> str:
        self._load_config()
        if not self.openrouter_key:
            title = "História" if kind == "story" else "Devocional"
            return f"{title} (Simulação - Sem Chave)\n\n{instruction}".strip()

        safe_kind = "história" if kind == "story" else "devocional"
        min_m = max(1, int(duration_min_minutes or 1))
        max_m = int(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        min_words = min_m * 200
        max_words = max_m * 200

        prompt = f"""
        Escreva um(a) {safe_kind} ORIGINAL em português (pt-BR), para ser NARRADO em vídeo.

        INSTRUÇÕES DO USUÁRIO (respeite exatamente):
        {instruction}

        REGRAS:
        - Objetivo: texto para narração (sem marcações, sem JSON, sem listas, sem títulos de seção).
        - Duração alvo do vídeo: entre {min_m} e {max_m} minutos.
        - Tamanho alvo: entre {min_words} e {max_words} palavras (aprox. 200 palavras por minuto).
        - Escreva em parágrafos, com ritmo natural e envolvente.
        - Não inclua nomes de marcas, links, nem instruções técnicas.
        - Não escreva "Cena 1" / "Narrador:" / "Roteiro:".

        Retorne APENAS o texto final completo.
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um escritor e roteirista de narração. Entregue apenas o texto final em português, sem JSON.",
                temperature=0.8,
                json_mode=False,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            return content.strip()
        except Exception as e:
            print(f"Erro ao gerar {safe_kind}: {e}")
            title = "História" if kind == "story" else "Devocional"
            return f"{title} (Falha na IA)\n\n{instruction}".strip()

    def improve_story_or_devotional_text(
        self,
        original_text: str,
        instruction: str,
        kind: str = "story",
        duration_min_minutes: int = 10,
        duration_max_minutes: Optional[int] = None,
    ) -> str:
        self._load_config()
        if not self.openrouter_key:
            return (original_text or "").strip() or "Texto (Simulação - Sem Chave)"

        safe_kind = "história" if kind == "story" else "devocional"
        min_m = max(1, int(duration_min_minutes or 1))
        max_m = int(duration_max_minutes) if duration_max_minutes else min_m
        if max_m < min_m:
            max_m = min_m

        min_words = min_m * 200
        max_words = max_m * 200

        prompt = f"""
        Você é um editor profissional de textos para narração em vídeo.
        Reescreva e MELHORE o(a) {safe_kind} abaixo, mantendo o tema e o sentido, mas elevando:
        - Clareza e fluidez
        - Emoção e retenção
        - Coerência e ritmo de narração

        INSTRUÇÕES DO USUÁRIO (respeite exatamente):
        {instruction}

        Duração alvo do vídeo: entre {min_m} e {max_m} minutos.
        Tamanho alvo: entre {min_words} e {max_words} palavras (aprox. 150 palavras por minuto).

        TEXTO ORIGINAL:
        {original_text}

        REGRAS:
        - Retorne APENAS o texto final completo (sem explicações, sem JSON, sem listas).
        - Não inclua nomes de marcas, links, nem instruções técnicas.
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um editor de textos para narração. Entregue apenas o texto final em português, sem JSON.",
                temperature=0.7,
                json_mode=False,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            return content.strip()
        except Exception as e:
            print(f"Erro ao melhorar {safe_kind}: {e}")
            return (original_text or "").strip()

    def generate_story_image_prompts(self, story_text: str, n: int = 4, kind: str = "story") -> list:
        self._load_config()
        try:
            count = int(n or 1)
        except Exception:
            count = 1
        count = max(1, min(12, count))

        text = (story_text or "").strip()
        if not text:
            return []

        safe_kind = (kind or "story").strip().lower()
        if safe_kind not in {"story", "devotional"}:
            safe_kind = "story"

        if not self.openrouter_key:
            base = text.replace("\n", " ").strip()[:320]
            styles = [
                "cinematic lighting, shallow depth of field",
                "dramatic atmosphere, volumetric light",
                "soft warm light, film still composition",
                "moody color grading, high detail",
            ]
            prompts = []
            for i in range(count):
                prompts.append(
                    f"Photorealistic cinematic photography inspired by this {safe_kind} message: {base}. "
                    f"{styles[i % len(styles)]}. Realistic humans (no dolls), natural skin, pleasant mood, no horror, no monsters, no gore. No text, no watermark, no logo."
                )
            return prompts

        import json

        prompt = f"""
        Crie {count} prompts de imagem DISTINTOS em INGLÊS, para gerar imagens por IA,
        com base no texto abaixo (um(a) {('história' if safe_kind == 'story' else 'devocional')} para narração).

        TEXTO (resumo/ideia central):
        {text[:2200]}

        REGRAS:
        - Cada prompt deve ser uma descrição visual rica (sem texto na imagem).
        - Varie composição, ângulo de câmera, cenário e momento (para evitar imagens repetidas).
        - Não inclua nomes de marcas, logos, marcas d'água nem "text overlay".
        - Estilo preferido: fotografia cinematográfica fotorrealista, iluminação natural e agradável, clima esperançoso e sereno.
        - Pessoas: aparência humana realista, proporções naturais, expressão serena (evitar "doll-like", "uncanny", "creepy").
        - Proibido: terror, monstros, gore, sangue, mutilação, olhos deformados, rosto desfigurado, assustador, grotesco, distópico, apocalíptico, sombrio.
        - Retorne APENAS um JSON válido:
          {{ "prompts": ["...", "..."] }}
        """

        try:
            raw = self._generate_text(
                prompt,
                system_prompt="Você é um diretor de arte. Gere apenas JSON no formato solicitado.",
                temperature=0.6,
                json_mode=True,
            ) or "{}"
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw) if raw else {}
            prompts = data.get("prompts") if isinstance(data, dict) else None
            if not isinstance(prompts, list):
                prompts = []
        except Exception:
            prompts = []

        while len(prompts) < count:
            base = text.replace("\n", " ").strip()[:320]
            prompts.append(
                f"Photorealistic cinematic photography inspired by this {safe_kind} message: {base}. "
                "Realistic humans (no dolls), pleasant mood, no horror, no monsters, no gore. No text, no watermark, no logo."
            )

        clean = []
        for p in prompts[:count]:
            if isinstance(p, str) and p.strip():
                clean.append(p.strip()[:900])
        while len(clean) < count:
            clean.append(f"Photorealistic cinematic photography inspired by this {safe_kind} message. Pleasant mood, no horror, no monsters, no gore. No text.")
        return clean[:count]

    def _visual_global_style(self) -> str:
        return "Cinematic photo-realistic, 8K, divine chiaroscuro, god rays, golden glow, celestial white highlights, deep blue atmosphere, warm fire tones, vibrant natural colors, epic perspective, deeply emotional expressions, high detail, high-definition, divine light, golden illumination, heavenly atmosphere, reverent scene, cinematic lighting, epic composition, inspiring, uplifting, photorealistic art, holy presence"

    def _visual_global_negative(self) -> str:
        return "(terror, horror, scary, disturbing, gore, blood, zombie, dark spirits, creepy, unsettling, death, monstrosity, distorted faces, menacing, evil appearance, nightmares, intense fear, non-divine context, unholy)"

    def _normalize_for_rules(self, text: str) -> str:
        import unicodedata
        t = (text or "").strip().lower()
        t = unicodedata.normalize("NFKD", t)
        t = "".join(ch for ch in t if not unicodedata.combining(ch))
        return t

    def _visual_negative_for_text(self, text: str) -> str:
        base = self._visual_global_negative()
        norm = self._normalize_for_rules(text or "")
        is_ezekiel_bones = any(k in norm for k in ["ezequiel", "ezekiel", "vale de ossos secos", "ossos secos", "valley of dry bones", "dry bones"])
        extra_common = [
            "no explicit blood",
            "no excessive blood",
            "no gore",
            "no horror",
            "no macabre",
            "no terrifying symbols",
            "no demonic symbols",
            "no evil expression",
            "no pure malice",
            "no fully black eyes",
            "no unsettling atmosphere",
            "no creepy",
            "no skulls",
            "no skeletons",
            "no bones",
            "no corpse",
            "no decomposition",
            "no rot",
        ]
        if is_ezekiel_bones:
            extra_common = [x for x in extra_common if x not in {"no skulls", "no skeletons", "no bones"}]
            extra_common.append("if bones appear, focus on reconstitution and life, no decomposition")

        extra = ", ".join(extra_common)
        return f"{base}, {extra}".strip().strip(",")

    def _sanitize_and_contextualize_image_prompt(self, prompt: str) -> str:
        import re
        raw = (prompt or "").strip()
        if not raw:
            return raw

        t = raw
        t = re.sub(r"(?is)\bnegative\s*prompt\s*:\s*.*$", "", t).strip()
        t = re.sub(r"\s+", " ", t).strip()

        norm = self._normalize_for_rules(t)
        is_revelation = any(k in norm for k in [
            "apocalipse", "revelation", "joao", "john",
            "candeeiro", "candelabro", "lampstand", "seven lamp",
            "cabelos brancos", "white hair", "wool",
            "espada saindo da boca", "sword from his mouth", "sword from mouth",
            "olhos em chamas", "eyes like fire", "flames of fire", "eyes of fire",
            "vestes de gloria", "robe of glory", "glorious robe",
            "filho do homem", "son of man",
        ])

        banned_words = [
            "monster", "monstrosity", "demon", "demonic", "devil", "satanic",
            "horror", "terror", "scary", "creepy", "macabre", "disturbing", "unholy",
            "evil", "menacing", "nightmare", "grotesque",
        ]
        if any(w in norm for w in banned_words):
            for w in banned_words:
                if w in norm:
                    t = re.sub(rf"(?i)\b{re.escape(w)}\b", "", t)
            t = re.sub(r"\s+", " ", t).strip()

        if is_revelation:
            t = (
                f"Glorious cinematic sacred vision inspired by Revelation 1. {t}. "
                "Interpret all visionary symbols as divine glory: hair white like wool as majestic and luminous, "
                "eyes like flames as gentle radiant divine light (not frightening), "
                "and 'sword from the mouth' as a symbolic beam of light shaped like a blade (not literal metal, not grotesque). "
                "Include seven golden lampstands when relevant, golden illumination, heavenly atmosphere, god rays, "
                "epic composition, vibrant colors, uplifting and inspiring mood, modest biblical attire, wide shot."
            ).strip()
        else:
            if any(k in norm for k in ["olhos", "eyes"]) and any(k in norm for k in ["fogo", "fire", "flame"]):
                t = (
                    f"{t}. Interpret any 'eyes of fire' as gentle divine light reflecting like flames, holy and uplifting, not scary or menacing."
                ).strip()
            if any(k in norm for k in ["espada", "sword"]) and any(k in norm for k in ["boca", "mouth"]):
                t = (
                    f"{t}. Interpret any 'sword from the mouth' symbolically as a radiant beam of light shaped like a blade, not grotesque."
                ).strip()

        style = self._visual_global_style()
        t_low = t.lower()
        if "reverent" not in t_low and "holy" not in t_low and "heavenly" not in t_low:
            t = f"{t}. Reverent holy scene, uplifting, inspiring."
        if "cinematic" not in t_low and "photorealistic" not in t_low:
            t = f"{t}. {style}"
        else:
            t = f"{t}. {style}"

        t = (
            f"{t}. Do not depict horror, terror, monsters, demons, disturbing atmosphere, or evil appearance. "
            "Avoid close-up threatening faces; if faces appear, keep them serene, natural, and peaceful."
        )
        return re.sub(r"\s+", " ", t).strip()

    def generate_semantic_visual_prompts_from_lyrics(self, lyrics: str, caption_slots: list, title: str = "", options: Optional[Dict[str, Any]] = None) -> list:
        self._load_config()
        import re
        try:
            count = int(len(caption_slots or []))
        except Exception:
            count = 0
        count = max(1, min(60, count))

        raw_lyrics = (lyrics or "").strip()
        clean_lyrics = raw_lyrics
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*style\s*:[^\]]*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*break\s*:[^\]]*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"(?m)^\s*\[\s*rhythmic\s+clapping\s*\]\s*$", "", clean_lyrics).strip()
        clean_lyrics = re.sub(r"\n{3,}", "\n\n", clean_lyrics).strip()

        try:
            from app.services.openai_image_module import OpenAIImageModule
        except Exception:
            OpenAIImageModule = None

        opts = dict(options or {})
        if OpenAIImageModule is None:
            style = self._visual_global_style()
            out = []
            while len(out) < count:
                out.append(self._sanitize_and_contextualize_image_prompt(f"Photorealistic cinematic film still. {style}. Safe, uplifting, no text.")[:900])
            return out[:count]

        mod = OpenAIImageModule(ai_service=self)
        sections = mod.split_lyrics_into_sections(clean_lyrics or raw_lyrics)
        allocated = mod._allocate_scenes(sections, count)
        prompt_language = mod._coerce_prompt_language((opts.get("prompt_language") or "auto"), clean_lyrics or raw_lyrics)
        semantic = mod.interpretar_letra(clean_lyrics or raw_lyrics, allocated, prompt_language=prompt_language) or {}
        global_semantic = semantic if isinstance(semantic, dict) else {}
        scene_semantics = global_semantic.get("cenas") if isinstance(global_semantic, dict) else None
        if not isinstance(scene_semantics, list):
            scene_semantics = []

        safe_title = (title or "").strip()
        if safe_title and isinstance(global_semantic, dict) and not global_semantic.get("titulo"):
            global_semantic["titulo"] = safe_title

        prompts = []
        for i, s in enumerate(allocated[:count]):
            ss = scene_semantics[i] if i < len(scene_semantics) and isinstance(scene_semantics[i], dict) else {}
            if not ss.get("trecho_titulo"):
                ss["trecho_titulo"] = (s.get("title") or f"Trecho {i+1}").strip()
            if not ss.get("descricao_cena"):
                ss["descricao_cena"] = (s.get("text") or "").strip()[:900]
            dalle_prompt = mod.build_dalle_prompt(global_semantic, ss, opts, prompt_language)
            prompts.append(self._sanitize_and_contextualize_image_prompt(dalle_prompt)[:900])

        while len(prompts) < count:
            style = self._visual_global_style()
            prompts.append(self._sanitize_and_contextualize_image_prompt(f"Photorealistic cinematic film still. {style}. Safe, uplifting, no text.")[:900])
        return prompts[:count]

    def enrich_scenes_with_image_prompts(self, plan: dict) -> dict:
        """
        Gera image_prompt profissionais com base na narração de cada cena, para a IA
        criar imagens próprias e montar o vídeo de forma profissional (YouTube Auto etc).
        Atualiza apenas cenas que não têm image_prompt ou têm um muito curto/genérico.
        """
        self._load_config()
        if not self.openrouter_key:
            return plan

        scenes = plan.get("scenes") or []
        if not scenes or not isinstance(scenes, list):
            return plan

        # Identifica cenas que precisam de image_prompt (vazio ou curto < 40 chars)
        need_prompts = []
        for i, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            text = (scene.get("text") or "").strip()
            current = (scene.get("image_prompt") or "").strip()
            if text and (not current or len(current) < 40):
                need_prompts.append((i, text[:500]))

        if not need_prompts:
            return plan

        import json
        # Uma única chamada: gera um image_prompt detalhado por cena a partir da narração
        scenes_desc = "\n".join([f"Cena {idx+1} (narração): {text}" for idx, text in need_prompts])
        title = plan.get("title") or "Vídeo"

        style = self._visual_global_style()
        neg = self._visual_negative_for_text(scenes_desc)

        prompt = f"""
        Você é um diretor de arte e diretor de fotografia para vídeos narrados (YouTube, Shorts). Seu trabalho é criar descrições visuais para gerar imagens com IA que ilustrem exatamente o que está sendo dito, com continuidade narrativa.

        Título do vídeo: {title}

        Narrações por cena:
        {scenes_desc}

        Para CADA cena acima, crie UMA descrição visual (image_prompt) em INGLÊS com as regras:
        - Faça internamente uma leitura exegética: protagonista, cenário, ação/emoção principal; diferencie metáfora vs literal.
        - Interpretação contextual: jamais interpretar passagens bíblicas de forma literal e sombria; traduza descrições visionárias para glória divina, transcendência e luz cinematográfica, nunca para terror.
        - Representar fielmente a ideia e o clima da narração, evitando genericidade.
        - Estilo obrigatório: {style}.
        - Estética e tom: santidade, adoração, esperança; luz celestial (god rays), brilho dourado, contraste (chiaroscuro) para glória, não para medo; paleta dourado/branco celestial/azul profundo/tons quentes.
        - Pessoas: humanas realistas (evitar bonecos/uncanny), proporções naturais, expressão serena.
        - Paisagens: realistas, sem aparência de IA assustadora, cores naturais, clima agradável.
        - Bloqueio global obrigatório: {neg}.
        - Proibido: macabro, terror, gore, símbolos de horror, olhos totalmente pretos, expressões de pura maldade; vestes indecentes.
        - Se narrar morte/inferno/trevas: represente por sombras, desertos ou abismos distantes, com a luz vencendo as trevas.
        - Proibido: texto na imagem, marcas d'água, logos.
        - Uma frase detalhada (30-80 palavras): cenário, iluminação, atmosfera, composição.
        - PROIBIDO: foto de banco de imagens, logos, marcas, text, watermark, personagens famosos.
        - Se a narração for abstrata, use metáforas visuais claras que expressem o sentido da mensagem.

        Retorne APENAS um JSON válido com um array "image_prompts" na mesma ordem das cenas:
        {{ "image_prompts": ["descrição visual cena 1...", "descrição visual cena 2...", ...] }}
        """

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você gera apenas JSON com o array image_prompts. Cada item é uma descrição visual em inglês para gerar imagem com IA.",
                temperature=0.6,
                json_mode=True
            )
            if not content:
                return plan
            content = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(content)
            prompts_list = data.get("image_prompts") or []
            if not isinstance(prompts_list, list):
                return plan
            for k, (scene_idx, _) in enumerate(need_prompts):
                if k < len(prompts_list) and scene_idx < len(scenes):
                    prompt_text = (prompts_list[k] or "").strip()
                    if prompt_text and isinstance(scenes[scene_idx], dict):
                        scenes[scene_idx]["image_prompt"] = self._sanitize_and_contextualize_image_prompt(prompt_text)[:500]
            plan["scenes"] = scenes
        except Exception as e:
            print(f"Erro ao enriquecer image_prompts com IA: {e}")
        return plan

    def generate_visual_plan_for_music(self, title, concept, duration_seconds):
        """Generates a visual-only script synchronized with music duration"""
        self._load_config()
        
        # Calculate roughly how many scenes (approx 6-10 seconds per scene)
        num_scenes = max(5, duration_seconds // 8)
        
        prompt = f"""
        Create a visual script for a music video titled "{title}".
        Concept/Theme: {concept}
        Duration: {duration_seconds} seconds.
        
        Please generate {num_scenes} visual scenes that flow well with the music.
        The scenes should be highly descriptive and photorealistic.
        There is NO narration, just music.
        
        Rules for image_prompt:
        - Photorealistic, cinematic, 4k, professional photography, live-action style.
        - NO cartoon, illustration, or pixel art.
        
        Return valid JSON in this format:
        {{
            "scenes": [
                {{
                    "image_prompt": "Detailed description of the scene...",
                    "duration": 8,
                    "transition": "fade"
                }},
                ...
            ]
        }}
        """
        
        try:
            content = self._generate_text(prompt, system_prompt="You are a professional music video director. Return only JSON.", json_mode=True)
            if not content: return {"scenes": []}
            
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except Exception as e:
            print(f"Error generating visual plan for music: {e}")
            # Fallback
            return {
                "scenes": [
                    {
                        "image_prompt": f"Cinematic shot representing {title} - {concept}, photorealistic, 4k",
                        "duration": duration_seconds,
                        "transition": "fade"
                    }
                ]
            }

    def analyze_channel_strategy(self, stats, current_description):
        """Analisa estratégia do canal"""
        self._load_config()
        
        prompt = f"""
        Atue como um Especialista em Crescimento de YouTube (YouTube Strategist).
        Analise os dados deste canal:
        - Inscritos: {stats.get('subscribers')}
        - Views: {stats.get('views')}
        - Vídeos: {stats.get('videos')}
        - Descrição Atual: "{current_description}"
        
        Forneça um plano de ação curto e direto para alavancar este canal.
        Sugira um novo TÍTULO (Nome do Canal) otimizado e uma nova descrição otimizada.
        
        Retorne JSON:
        {{
            "analysis": "Sua análise...",
            "action_plan": ["Passo 1", "Passo 2", "Passo 3"],
            "title_suggestion": "Novo Nome Sugerido",
            "description_suggestion": "Nova descrição sugerida...",
            "banner_prompt": "Descrição visual para o banner do canal..."
        }}
        """
        
        if not self.openrouter_key:
            return {
                "analysis": "Simulação: O canal tem potencial mas precisa de consistência.",
                "action_plan": ["Postar 2x por semana", "Melhorar Thumbnails", "Focar em Shorts"],
                "title_suggestion": "Codexia - Livros & Mente",
                "description_suggestion": "Canal oficial sobre livros e desenvolvimento pessoal. Inscreva-se para transformar sua vida.",
                "banner_prompt": "Uma biblioteca mística com luz dourada, estilo digital art, alta qualidade, 4k"
            }
            
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um estrategista de YouTube. Retorne apenas JSON.",
                json_mode=True
            )
            
            import json
            if not content:
                 raise Exception("Resposta vazia da IA")

            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro na análise do canal: {e}")
            return {"error": str(e)}

    def generate_banner_image(self, prompt_text: str) -> str:
        prompt_text = (prompt_text or "").strip()
        if not prompt_text:
            return None
        return self.generate_image(
            f"{prompt_text}. YouTube Channel Banner, wide 16:9 aspect ratio, professional design, no text.",
            aspect_ratio="16:9",
        )

    def generate_monitor_report(self, stats):
        """Gera relatório curto de monitoramento"""
        self._load_config()
        
        prompt = f"""
        Analise o status atual do canal (Monitoramento em Tempo Real):
        - Inscritos: {stats.get('subscribers')}
        - Views: {stats.get('views')}
        - Vídeos: {stats.get('videos')}
        
        Forneça:
        1. Uma análise curta de 1 frase sobre o desempenho atual.
        2. Uma sugestão estratégica imediata (1 frase).
        
        Retorne JSON:
        {{
            "analysis": "...",
            "strategy": "..."
        }}
        """
        
        try:
            content = self._generate_text(prompt, json_mode=True)
            if not content:
                raise Exception("No content generated")
                
            import json
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        except Exception as e:
            print(f"Error generating monitor report: {e}")
            return {
                "analysis": "Monitoramento simulado (Erro IA): Canal estável.",
                "strategy": "Continue postando regularmente para aumentar engajamento."
            }

    def generate_auto_insights(self, stats, recent_videos):
        """
        Gera insights automáticos sobre o canal, analisando impacto por vídeo
        e sugerindo novos conteúdos baseados nos melhores desempenhos.
        """
        self._load_config()
        
        import json
        videos_json = json.dumps(recent_videos, indent=2, default=str)
        
        prompt = f"""
        Atue como um Especialista Sênior em YouTube Analytics e Estratégia de Conteúdo.
        
        DADOS DO CANAL:
        - Nome: {stats.get('title')}
        - Inscritos: {stats.get('subscribers')}
        - Total Views: {stats.get('views')}
        - Total Vídeos: {stats.get('videos')}
        
        VÍDEOS RECENTES (Performance):
        {videos_json}
        
        SUA MISSÃO:
        1. Analise a evolução de cada vídeo recente e seu impacto no canal (quais trouxeram mais views/engajamento).
        2. Identifique o vídeo de MELHOR resultado (o "Campeão").
        3. Gere listas de ideias de vídeos longos e shorts baseados no campeão.
        4. Gere um plano de conteúdo semanal AUTOMÁTICO focado em ALAVANCAR esse sucesso.
        
        Retorne APENAS um JSON válido com a seguinte estrutura:
        {{
            "summary": "Resumo geral da saúde do canal e tendências identificadas.",
            "video_impact_analysis": [
                {{"video_title": "Título do Vídeo", "impact": "Análise curta do impacto"}}
            ],
            "best_video": {{
                "title": "Título do Melhor Vídeo",
                "reason": "Por que foi o melhor"
            }},
            "long_video_ideas": [
                {{"title": "Título Ideia 1", "concept": "Conceito..."}},
                {{"title": "Título Ideia 2", "concept": "Conceito..."}}
            ],
            "shorts_ideas": [
                {{"title": "Título Short 1", "concept": "Conceito..."}},
                {{"title": "Título Short 2", "concept": "Conceito..."}}
            ],
            "weekly_plan": [
                {{
                    "day": "Segunda-feira",
                    "theme": "Continuação do Sucesso",
                    "videos": [
                        {{
                            "title": "Título Sugerido",
                            "concept": "Explicação do conceito",
                            "time": "18:00",
                            "type": "video",
                            "auto_post": true
                        }}
                    ]
                }},
                {{
                    "day": "Quarta-feira",
                    "theme": "Short Viral",
                    "videos": [
                        {{
                            "title": "Título do Short",
                            "concept": "Hook rápido",
                            "time": "12:00",
                            "type": "short",
                            "auto_post": true
                        }}
                    ]
                }}
            ]
        }}
        """
        
        try:
            content = self._generate_text(
                prompt, 
                system_prompt="Você é um estrategista de YouTube focado em dados e crescimento viral.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_content)
            
        except Exception as e:
            print(f"Erro ao gerar auto insights: {e}")
            # Mock fallback para não quebrar o frontend
            return {
                "summary": "Não foi possível gerar a análise detalhada neste momento.",
                "video_impact_analysis": [],
                "best_video": {"title": "N/A", "reason": "Erro na análise"},
                "long_video_ideas": [],
                "shorts_ideas": [],
                "weekly_plan": []
            }

    def generate_monetization_insights(self, progress_data):
        """
        Gera insights focados em atingir a monetização do YouTube.
        """
        self._load_config()

        subs = progress_data.get('subscribers', 0)
        subs_target = progress_data.get('subscribers_target', 1000)
        hours = progress_data.get('estimated_watch_hours', 0)
        hours_target = progress_data.get('watch_hours_target', 4000)
        subs_pct = progress_data.get('subscribers_progress_pct', 0)
        hours_pct = progress_data.get('watch_hours_progress_pct', 0)
        subs_missing = max(0, subs_target - subs)
        hours_missing = max(0, hours_target - hours)

        prompt = f"""Atue como um Consultor Expert de Monetização do YouTube (YPP).

DADOS DO CANAL:
- Inscritos atuais: {subs} / Meta: {subs_target} (progresso: {subs_pct}%, faltam: {subs_missing})
- Horas de exibição estimadas: {hours} / Meta: {hours_target} (progresso: {hours_pct}%, faltam: {hours_missing})

REGRAS IMPORTANTES:
- Os valores de subscribers_missing e watch_hours_missing DEVEM ser calculados exatamente: {subs_missing} e {hours_missing} respectivamente.
- Se o canal JÁ atingiu uma meta, indique 0 faltante e parabenize.
- A estimativa de tempo deve ser realista (baseada no ritmo atual de crescimento).
- As ações semanais devem ser ESPECÍFICAS e ACIONÁVEIS (não genéricas).
- A estratégia deve priorizar o gap MAIOR (se faltam mais horas, foque em watch time; se faltam mais inscritos, foque em crescimento).

Retorne APENAS JSON válido com esta estrutura EXATA:
{{
    "summary": "Análise detalhada da situação atual do canal em relação à monetização (2-3 frases).",
    "gap_analysis": {{
        "subscribers_missing": {subs_missing},
        "watch_hours_missing": {hours_missing},
        "estimated_time_to_monetize": "Estimativa realista baseada no ritmo atual (ex: 2-3 meses)"
    }},
    "strategy_suggestion": "Estratégia principal detalhada para fechar o gap mais crítico.",
    "weekly_actions": [
        "Ação específica 1 com detalhes de implementação",
        "Ação específica 2 com detalhes de implementação",
        "Ação específica 3 com detalhes de implementação",
        "Ação específica 4 com detalhes de implementação",
        "Ação específica 5 com detalhes de implementação"
    ]
}}"""

        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um consultor especialista em monetização do YouTube Partner Program. Responda sempre em português do Brasil com dados precisos.",
                json_mode=True
            )

            if not content:
                raise Exception("Resposta vazia da IA")

            clean_content = content.replace("```json", "").replace("```", "").strip()
            result = json.loads(clean_content)

            if "gap_analysis" in result:
                result["gap_analysis"]["subscribers_missing"] = subs_missing
                result["gap_analysis"]["watch_hours_missing"] = hours_missing

            return result

        except Exception as e:
            print(f"Erro ao gerar insights de monetização: {e}")

            if subs >= subs_target and hours >= hours_target:
                summary = f"Parabéns! Seu canal já atingiu os requisitos de monetização: {subs} inscritos e ~{hours} horas de exibição."
                strategy = "Mantenha a consistência e solicite a revisão do YPP se ainda não o fez."
                time_est = "Elegível agora!"
            elif subs_pct > hours_pct:
                summary = f"Seu canal tem {subs} inscritos ({subs_pct}%) e ~{hours} horas de exibição ({hours_pct}%). O gap principal são as horas de exibição."
                strategy = "Foque em vídeos longos (10-20 min) e lives para aumentar as horas de exibição."
                time_est = "Depende do ritmo de publicação"
            else:
                summary = f"Seu canal tem {subs} inscritos ({subs_pct}%) e ~{hours} horas de exibição ({hours_pct}%). O gap principal são os inscritos."
                strategy = "Foque em Shorts virais e colaborações para crescer a base de inscritos."
                time_est = "Depende do ritmo de publicação"

            return {
                "summary": summary,
                "gap_analysis": {
                    "subscribers_missing": subs_missing,
                    "watch_hours_missing": hours_missing,
                    "estimated_time_to_monetize": time_est
                },
                "strategy_suggestion": strategy,
                "weekly_actions": [
                    f"Publicar pelo menos 3 vídeos longos (10+ min) para aumentar horas de exibição" if hours_pct < subs_pct else "Publicar 5 Shorts por semana para ganhar inscritos",
                    "Responder todos os comentários para aumentar engajamento e retenção",
                    "Criar thumbnails chamativas com CTR acima de 5%",
                    "Analisar Analytics para identificar vídeos com maior retenção e replicar o formato",
                    "Promover o canal em comunidades relevantes e redes sociais"
                ]
            }

    def transcribe_audio_segments(self, audio_path: str, language: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        info = self.transcribe_audio_segments_detailed(audio_path=audio_path, language=language)
        segs = info.get("segments") if isinstance(info, dict) else None
        return segs if isinstance(segs, list) else None

    def transcribe_audio_segments_detailed(self, audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
        self._load_config()
        api_key = (self.api_key or "").strip() if self.api_key else ""
        if not api_key:
            return {"segments": None, "error": "missing_api_key"}
        if not audio_path or not os.path.exists(audio_path):
            return {"segments": None, "error": "file_not_found"}
        client = openai.OpenAI(api_key=api_key)
        def _extract_openai_error(e: Exception) -> Dict[str, Any]:
            info: Dict[str, Any] = {}
            status = getattr(e, "status_code", None)
            if status is None:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
            if status is not None:
                info["status"] = status

            body = getattr(e, "body", None)
            if body is None:
                resp = getattr(e, "response", None)
                try:
                    body = resp.json() if resp is not None else None
                except Exception:
                    body = None

            if isinstance(body, str) and body.strip():
                try:
                    import json
                    body = json.loads(body)
                except Exception:
                    pass

            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    if err.get("type") is not None:
                        info["type"] = err.get("type")
                    if err.get("code") is not None:
                        info["code"] = err.get("code")
                    if err.get("message") is not None:
                        info["message"] = err.get("message")

            if not info.get("message"):
                info["message"] = str(e)
            return info

        try:
            with open(audio_path, "rb") as f:
                kwargs: Dict[str, Any] = {
                    "model": "whisper-1",
                    "file": f,
                    "timestamp_granularities": ["word", "segment"],
                }
                if language:
                    kwargs["language"] = language
                try:
                    res = client.audio.transcriptions.create(**kwargs)
                except TypeError:
                    kwargs.pop("timestamp_granularities", None)
                    res = client.audio.transcriptions.create(**kwargs)
        except Exception as e:
            return {"segments": None, "error": _extract_openai_error(e)}

        segments = None
        if hasattr(res, "segments"):
            segments = getattr(res, "segments")
        elif isinstance(res, dict):
            segments = res.get("segments")
        if not isinstance(segments, list):
            return {"segments": None, "error": "no_segments"}

        out: List[Dict[str, Any]] = []
        for s in segments:
            start = None
            end = None
            text = None
            words_raw = None
            if isinstance(s, dict):
                start = s.get("start")
                end = s.get("end")
                text = s.get("text")
                words_raw = s.get("words")
            elif hasattr(s, "model_dump"):
                try:
                    d = s.model_dump()
                    if isinstance(d, dict):
                        start = d.get("start")
                        end = d.get("end")
                        text = d.get("text")
                        words_raw = d.get("words")
                except Exception:
                    start = getattr(s, "start", None)
                    end = getattr(s, "end", None)
                    text = getattr(s, "text", None)
                    words_raw = getattr(s, "words", None)
            else:
                start = getattr(s, "start", None)
                end = getattr(s, "end", None)
                text = getattr(s, "text", None)
                words_raw = getattr(s, "words", None)
            try:
                start_f = float(start)
                end_f = float(end)
            except Exception:
                continue
            t = str(text or "").strip()
            if not t:
                continue
            words_out: Optional[List[Dict[str, Any]]] = None
            if isinstance(words_raw, list) and words_raw:
                w_items: List[Dict[str, Any]] = []
                for w in words_raw:
                    if not isinstance(w, dict):
                        continue
                    try:
                        ws = float(w.get("start"))
                        we = float(w.get("end"))
                    except Exception:
                        continue
                    ww = str(w.get("word") or w.get("text") or "").strip()
                    if not ww or we <= ws:
                        continue
                    w_items.append({"start": ws, "end": we, "word": ww})
                if w_items:
                    words_out = w_items
            out.append({"start": start_f, "end": end_f, "text": t, "words": words_out})
        return {"segments": out or None, "error": None if out else "empty_segments"}

    def generate_hotmart_suggestions(self, book_data):
        """
        Analisa um livro e gera sugestões otimizadas para publicação na Hotmart:
        - Título otimizado para vendas
        - Descrição persuasiva
        - Preço sugerido baseado no mercado
        - Categoria adequada
        - Tags relevantes
        - Copy de vendas
        """
        self._load_config()
        import json
        
        prompt = f"""
        Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.
        
        LIVRO PARA ANÁLISE:
        - Título: {book_data.get('title', 'Sem título')}
        - Autor: {book_data.get('author', 'Desconhecido')}
        - Sinopse: {book_data.get('synopsis', 'Sem sinopse')}
        - Preço Atual: R$ {book_data.get('price', 0)}
        - Capítulos: {', '.join(book_data.get('chapters', [])) if book_data.get('chapters') else 'Não informado'}
        
        SUA MISSÃO:
        1. Analise o conteúdo do livro e sugira um TÍTULO otimizado para vendas (pode ser diferente do original, mas mantendo a essência).
        2. Crie uma DESCRIÇÃO persuasiva e otimizada para conversão (máximo 2000 caracteres).
        3. Sugira um PREÇO competitivo baseado no mercado brasileiro de produtos digitais similares.
        4. Identifique a CATEGORIA mais adequada na Hotmart (ex: Educação, Negócios, Desenvolvimento Pessoal, etc.).
        5. Liste 5-10 TAGS relevantes para SEO e descoberta.
        6. Crie um COPY DE VENDAS curto (2-3 parágrafos) destacando os principais benefícios.
        7. Sugira um SUBTÍTULO chamativo.
        
        Retorne APENAS um JSON válido:
        {{
            "optimized_title": "Título otimizado para vendas",
            "subtitle": "Subtítulo chamativo",
            "description": "Descrição completa e persuasiva do produto...",
            "sales_copy": "Copy de vendas destacando benefícios...",
            "suggested_price": 97.00,
            "category": "Educação",
            "tags": ["tag1", "tag2", "tag3"],
            "key_benefits": [
                "Benefício 1",
                "Benefício 2",
                "Benefício 3"
            ],
            "target_audience": "Descrição do público-alvo",
            "marketing_notes": "Observações importantes para marketing"
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            suggestions = json.loads(clean_content)
            
            return suggestions
            
        except Exception as e:
            print(f"Erro ao gerar sugestões Hotmart: {e}")
            
            # Fallback com dados básicos
            return {
                "optimized_title": book_data.get('title', 'Sem título'),
                "subtitle": f"Por {book_data.get('author', 'Autor')}",
                "description": book_data.get('synopsis', 'Sem descrição disponível.'),
                "sales_copy": f"Descubra {book_data.get('title', 'este livro')} e transforme sua vida.",
                "suggested_price": book_data.get('price', 97.00),
                "category": "Educação",
                "tags": ["livro", "digital", "educação"],
                "key_benefits": [
                    "Conteúdo de qualidade",
                    "Acesso imediato",
                    "Suporte ao cliente"
                ],
                "target_audience": "Pessoas interessadas em desenvolvimento pessoal",
                "marketing_notes": "Configure as sugestões manualmente se necessário."
            }

    def generate_hotmart_suggestions_sync(self, book_data, changed_field, new_value, current_form):
        """
        Regenera campos relacionados quando o usuário altera manualmente um campo.
        Mantém consistência entre título, descrição, copy de vendas, etc.
        """
        self._load_config()
        import json
        
        # Mapeia qual campo foi alterado e quais devem ser atualizados
        field_dependencies = {
            "name": ["sales_copy", "description", "subtitle"],  # Se título muda, atualiza copy, descrição e subtítulo
            "description": ["sales_copy", "key_benefits"],  # Se descrição muda, atualiza copy e benefícios
            "subtitle": ["sales_copy"],  # Se subtítulo muda, atualiza copy
            "price": [],  # Preço não afeta outros campos
            "category": ["tags"],  # Se categoria muda, pode atualizar tags
            "tags": []  # Tags não afetam outros campos
        }
        
        fields_to_update = field_dependencies.get(changed_field, [])
        
        if not fields_to_update:
            return {}  # Nenhum campo precisa ser atualizado
        
        prompt = f"""
        Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart.
        
        CONTEXTO DO LIVRO:
        - Título ATUAL (alterado pelo usuário): {current_form.get('name') or book_data.get('title')}
        - Autor: {book_data.get('author', 'Desconhecido')}
        - Descrição ATUAL: {current_form.get('description') or book_data.get('synopsis', '')}
        - Subtítulo ATUAL: {current_form.get('subtitle', '')}
        - Preço: R$ {current_form.get('price') or book_data.get('price', 0)}
        - Categoria: {current_form.get('category', '')}
        
        CAMPO ALTERADO:
        - Campo: {changed_field}
        - Novo Valor: {new_value}
        
        SUA MISSÃO:
        Atualize APENAS os seguintes campos para manter consistência com a alteração feita:
        {', '.join(fields_to_update)}
        
        IMPORTANTE:
        - Use o título "{current_form.get('name') or book_data.get('title')}" em TODOS os textos gerados
        - Mantenha o tom e estilo profissional
        - Garanta que todos os textos mencionem o título correto
        - Se o campo alterado foi o título, atualize o copy de vendas para usar o novo título
        
        Retorne APENAS um JSON válido com os campos atualizados:
        {{
            "sales_copy": "Novo copy de vendas usando o título correto...",
            "description": "Nova descrição se necessário...",
            "subtitle": "Novo subtítulo se necessário...",
            "key_benefits": ["Benefício 1", "Benefício 2", "Benefício 3"]
        }}
        
        Inclua APENAS os campos que estão na lista: {fields_to_update}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um especialista em marketing digital e vendas de produtos digitais na Hotmart. Mantenha consistência entre todos os textos.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            updated_fields = json.loads(clean_content)
            
            # Retorna apenas os campos que devem ser atualizados
            result = {}
            for field in fields_to_update:
                if field in updated_fields:
                    result[field] = updated_fields[field]
            
            return result
            
        except Exception as e:
            print(f"Erro ao sincronizar campos Hotmart: {e}")
            return {}

    def _build_prompt(self, title, synopsis, style):
        if style == "cliffhanger":
            return f"Crie um anúncio curto e misterioso para o livro '{title}'. Sinopse: {synopsis}. Termine com um gancho forte."
        elif style == "storytelling":
            return f"Conte uma história curta e emocionante baseada no livro '{title}'. Sinopse: {synopsis}. Foque na jornada do herói."
        else: # direct
            return f"Crie um anúncio de vendas direto e persuasivo para o livro '{title}'. Sinopse: {synopsis}. Liste 3 benefícios e faça uma oferta irresistível."

    def generate_content_plan(self, theme, duration_type="days", duration_value=7, start_date=None, videos_per_day=1, shorts_per_day=0, video_duration=5):
        """Gera plano de conteúdo personalizado"""
        self._load_config()
        
        from datetime import datetime, timedelta
        import json
        
        if not start_date:
            start_date_obj = datetime.now() + timedelta(days=1)
        else:
            try:
                start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            except:
                start_date_obj = datetime.now() + timedelta(days=1)
                
        total_days = int(duration_value)
        if duration_type == "weeks":
            total_days = total_days * 7
        elif duration_type == "months":
            total_days = total_days * 30
            
        # Limit total days to 30 for safety in this iteration to avoid timeouts/context limits
        if total_days > 31:
            total_days = 31

        prompt = f"""
        Crie um planejamento de conteúdo para um canal do YouTube sobre o tema '{theme}'.
        Período: {total_days} dias, começando em {start_date_obj.strftime('%d/%m/%Y')}.
        
        Para CADA dia ({total_days} dias), eu preciso EXATAMENTE de:
        1. {videos_per_day} Vídeo(s) Longo(s) (type="video") com duração de {video_duration} min.
        2. {shorts_per_day} Vídeo(s) Curto(s) (type="short") com duração de 1 min.
        
        IMPORTANTE: As datas devem ser sequenciais a partir de {start_date_obj.strftime('%Y-%m-%d')}.
        Respeite rigorosamente a quantidade de vídeos e shorts por dia solicitada.
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "plan": [
                {{
                    "date": "YYYY-MM-DD",
                    "theme_of_day": "Tema do dia",
                    "videos": [
                        {{
                            "title": "Título",
                            "concept": "Ideia do vídeo",
                            "time": "HH:MM",
                            "type": "video",
                            "duration": {video_duration}
                        }},
                        {{
                            "title": "Título do Short",
                            "concept": "Ideia do short",
                            "time": "HH:MM",
                            "type": "short",
                            "duration": 1
                        }}
                    ]
                }}
            ]
        }}
        """
        
        try:
            content = self._generate_text(prompt, json_mode=True)
            if not content:
                 raise Exception("Resposta vazia da IA ou nenhum provedor configurado")

            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
            
        except Exception as e:
            error_msg = str(e)
            print(f"Erro ao gerar plano: {error_msg}")
            
            # Mock fallback
            mock_plan = []
            for i in range(total_days):
                current_date = start_date_obj + timedelta(days=i)
                day_videos = []
                
                # Mock Videos
                for v in range(int(videos_per_day)):
                    hour = 8 + (v * 4) # 8, 12, 16...
                    if hour > 22: hour = 22
                    day_videos.append({
                        "title": f"Vídeo {v+1}: {theme} {i+1}", 
                        "concept": f"Conceito vídeo {v+1}", 
                        "time": f"{hour:02d}:00", 
                        "type": "video",
                        "duration": video_duration
                    })
                
                # Mock Shorts
                for s in range(int(shorts_per_day)):
                    hour = 10 + (s * 2) # 10, 12, 14...
                    if hour > 23: hour = 23
                    day_videos.append({
                        "title": f"Short {s+1}: {theme}", 
                        "concept": "Curiosidade rápida", 
                        "time": f"{hour:02d}:30", 
                        "type": "short",
                        "duration": 1
                    })

                mock_plan.append({
                    "day": i + 1,
                    "date": current_date.strftime('%Y-%m-%d'),
                    "theme_of_day": f"Tema do Dia {i+1}: {theme}",
                    "videos": day_videos
                })
            
            return {"plan": mock_plan}

    def _mock_response(self, title, style, error=None, duration=None, **kwargs):
        base_msg = f"⚠️ MODO SIMULAÇÃO (Vá em Configurações e adicione sua chave OpenAI)\n\n"
        if error:
            base_msg += f"Erro detectado: {error}\n\n"
            
        if style == "cliffhanger":
            return base_msg + f"🔥 [Simulação] O mistério de '{title}' vai te prender..."
        elif style == "storytelling":
            return base_msg + f"📖 [Simulação] Quando escrevi '{title}', eu queria..."
        elif style == "motivational_long":
            import json
            
            # Simple scaling of scenes based on duration if provided
            num_scenes = 3
            if duration:
                try:
                    num_scenes = max(3, int(duration) * 2)
                except:
                    pass
            
            scenes = []
            scenes.append({"text": f"Bem-vindo a este vídeo sobre {title}. A vida é cheia de desafios...", "image_prompt": "Mountain peak sunrise"})
            
            for i in range(num_scenes - 2):
                scenes.append({"text": f"O passo {i+1} é acreditar em si mesmo e nunca desistir, pois a persistência é a chave.", "image_prompt": f"Motivational scene {i+1} nature landscape"})
                
            scenes.append({"text": "Acredite em si mesmo e conquiste seus sonhos.", "image_prompt": "Lion looking at horizon"})

            return {
                "title": f"Motivação: {title} (Vídeo Épico)",
                "description": "Vídeo motivacional gerado automaticamente.",
                "scenes": scenes,
                "music_mood": "epic"
            }
        else:
            return base_msg + f"🎬 [Simulação] Roteiro para '{title}'..."

    def generate_image(self, prompt, aspect_ratio: str = "9:16", providers: list = None, status_callback=None):
        """
        Gera imagem usando APENAS OpenAI Images API.
        Se falhar, levanta exceção e não tenta nenhum outro provedor.
        """
        self._load_config()
        raw_prompt = (prompt or "").strip()
        if not raw_prompt:
            return None
        raw_prompt = self._sanitize_and_contextualize_image_prompt(raw_prompt)

        def notify(message: str):
            if status_callback:
                try:
                    status_callback(message)
                except Exception:
                    pass

        if not (self.api_key or "").strip():
            raise Exception("OpenAI não configurada (OPENAI_API_KEY ausente).")

        size = "1024x1024"

        neg = self._visual_negative_for_text(raw_prompt)
        full_prompt = (
            f"{raw_prompt}. "
            "Cinematic, epic, high quality, dramatic lighting, professional composition. "
            "Photorealistic cinematic photography. "
            "No text, no letters, no numbers, no captions, no subtitles, no signage, no watermarks, no logos. "
            f"Negative prompt: {neg}."
        ).strip()

        base_dir = Path("generated_assets/openai_images")
        base_dir.mkdir(parents=True, exist_ok=True)
        filename = f"img_{uuid.uuid4().hex}.png"
        out_path = base_dir / filename
        friendly_error = "Não foi possível gerar a imagem com OpenAI. Verifique a chave da API, saldo/créditos e modelo disponível."

        notify("Gerando imagem com OpenAI...")
        try:
            if hasattr(openai, "OpenAI"):
                client = openai.OpenAI(api_key=(self.api_key or "").strip())
                result = client.images.generate(
                    model="gpt-image-1",
                    prompt=full_prompt,
                    size=size,
                )
                item0 = result.data[0] if result and getattr(result, "data", None) else None
                image_base64 = getattr(item0, "b64_json", None) if item0 is not None else None
            else:
                raise Exception("SDK OpenAI desatualizado. Requer openai>=1.0.0.")
        except Exception as e:
            print("OPENAI IMAGE ERROR RAW:", repr(e))
            raise Exception(friendly_error)

        try:
            image_base64 = (image_base64 or "").strip() if isinstance(image_base64, str) else ""
            if not image_base64:
                raise Exception("OpenAI não retornou b64_json na imagem.")
            image_bytes = base64.b64decode(image_base64)
            with open(out_path, "wb") as f:
                f.write(image_bytes)
            if not out_path.exists() or out_path.stat().st_size < 1024:
                raise Exception("OpenAI não retornou bytes válidos para a imagem.")
            return f"/generated_assets/openai_images/{filename}"
        except Exception as e:
            print("OPENAI IMAGE ERROR RAW:", repr(e))
            raise Exception(friendly_error)

    def generate_audio(self, text, voice="onyx"):
        """Gera áudio usando Eden AI (ElevenLabs) com fallback opcional."""
        self._load_config()

        if self.edenai_key:
            audio_content = self._generate_audio_edenai_elevenlabs(text, voice)
            if audio_content:
                return audio_content

        if self.elevenlabs_key:
            audio_content = self._generate_audio_elevenlabs(text, voice)
            if audio_content:
                return audio_content

        return None

    def _generate_audio_edenai_elevenlabs(self, text: str, voice_hint: str = "onyx"):
        if not (self.edenai_key or "").strip() or not text or not text.strip():
            return None
        try:
            hint = (voice_hint or "").strip().lower()
            custom_voice_id = (self.elevenlabs_voice_id or "").strip()
            voice_id = None
            if hint in {"my_voice", "myvoice", "minha_voz", "minhavoz", "custom"} and custom_voice_id:
                voice_id = custom_voice_id

            headers = {"Authorization": f"Bearer {self.edenai_key.strip()}"}
            payload = {
                "providers": "elevenlabs",
                "text": text[:5000],
                "language": "pt-BR",
            }
            if voice_id:
                payload["voice_id"] = voice_id
                payload["voice"] = voice_id

            r = requests.post(
                "https://api.edenai.run/v2/audio/text_to_speech",
                headers=headers,
                json=payload,
                timeout=120,
            )
            if r.status_code >= 400:
                print(f"Eden AI TTS HTTP {r.status_code}: {(r.text or '')[:240]}")
                return None
            data = r.json() if (r.headers.get("content-type") or "").startswith("application/json") else {}

            def extract_url(obj):
                if isinstance(obj, dict):
                    for k in ("audio_resource_url", "audio_url", "url"):
                        v = obj.get(k)
                        if isinstance(v, str) and v.startswith("http"):
                            return v
                return None

            provider_payload = data.get("elevenlabs") if isinstance(data, dict) else None
            audio_url = extract_url(provider_payload) or extract_url(data)
            if not audio_url:
                return None

            rr = requests.get(audio_url, timeout=120)
            if rr.status_code >= 400 or not rr.content:
                return None
            return rr.content
        except Exception as e:
            print(f"Eden AI TTS error: {e}")
            return None

    def _generate_audio_elevenlabs(self, text: str, voice_hint: str = "onyx"):
        """Gera áudio usando ElevenLabs API (vozes ultra-realistas)."""
        if not self.elevenlabs_key or not text or not text.strip():
            return None
        try:
            # Permite customização por ambiente sem quebrar compatibilidade.
            env_voice_male = os.getenv("ELEVENLABS_VOICE_ID_MALE", "").strip()
            env_voice_female = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "").strip()
            env_voice_default = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

            # Mapeia hints para voice_id do ElevenLabs (defaults públicos estáveis).
            voice_map = {
                "nova": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
                "shimmer": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
                "onyx": env_voice_male or "VR6AewLTigWG4xSOukaG",
                "echo": env_voice_male or "VR6AewLTigWG4xSOukaG",
                "fable": env_voice_female or "EXAVITQu4vr4xnSDxMaL",
            }
            hint = (voice_hint or "").strip().lower()
            custom_voice_id = (self.elevenlabs_voice_id or "").strip()
            if hint in ["my_voice", "myvoice", "minha_voz", "minhavoz", "custom"] and custom_voice_id:
                voice_id = custom_voice_id
            else:
                voice_id = env_voice_default or voice_map.get(hint, env_voice_female or "EXAVITQu4vr4xnSDxMaL")
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {"xi-api-key": self.elevenlabs_key, "Content-Type": "application/json"}
            payload = {
                "text": text[:5000],
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.35,
                    "similarity_boost": 0.85,
                    "style": 0.35,
                    "use_speaker_boost": True,
                },
            }
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code == 200:
                return r.content
            print(f"ElevenLabs TTS HTTP {r.status_code}: {r.text[:240]}")
        except Exception as e:
            print(f"ElevenLabs TTS error: {e}")
        return None

    def generate_song_lyrics(self, theme: str, message: str, language: str = "pt-BR", style: str = "", genre: str = ""):
        self._load_config()
        lang = (language or "pt-BR").strip()
        theme = (theme or "").strip()
        message = (message or "").strip()
        style = (style or "").strip()
        genre = (genre or "").strip()

        if not theme or not message:
            return {"title": "Música", "lyrics": ""}

        if not self.openrouter_key:
            title = f"{theme.title()} - Recomeçar"
            lyrics = (
                f"Verso 1\n"
                f"No silêncio eu me encontrei\n"
                f"Quando tudo parecia não ter fim\n"
                f"Guardei no peito o que eu sonhei\n"
                f"E fiz da queda um novo sim\n\n"
                f"Pré-Refrão\n"
                f"Eu ouvi a vida me chamar\n"
                f"Pra levantar e continuar\n\n"
                f"Refrão\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n\n"
                f"Verso 2\n"
                f"Se o medo tenta me prender\n"
                f"Eu lembro quem eu decidi ser\n"
                f"Cada cicatriz me faz crescer\n"
                f"E a esperança volta a aparecer\n\n"
                f"Refrão\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n\n"
                f"Ponte\n"
                f"Eu não nasci pra desistir\n"
                f"Eu nasci pra renascer\n\n"
                f"Refrão Final\n"
                f"{message}\n"
                f"Eu vou seguir sem olhar pra trás\n"
                f"Se a tempestade vem, eu faço paz\n"
                f"{message}\n"
            )
            return {"title": title, "lyrics": lyrics}

        combined = f"{style} {genre}".strip().lower()
        combined = combined.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
        combined = combined.replace("é", "e").replace("ê", "e")
        combined = combined.replace("í", "i")
        combined = combined.replace("ó", "o").replace("ô", "o").replace("õ", "o")
        combined = combined.replace("ú", "u")

        extra_rules = ""
        if any(k in combined for k in ["pentecostal", "corinho", "corinho de fogo", "fogo no pe", "fogo no pe'"]):
            extra_rules = (
                "\nRegras específicas do estilo (Corinho / Pentecostal):\n"
                "- Linguagem de culto congregacional (igreja pequena), direta e simples.\n"
                "- Frases curtas e rítmicas, fáceis de cantar em grupo.\n"
                "- Refrão com chamada-e-resposta (com repetições) e energia alta.\n"
                "- Evite romantização/sofrência e gírias seculares.\n"
            )
        if any(k in combined for k in ["corinho tradicional", "culto de oracao", "culto de oração"]):
            extra_rules += (
                "- Puxada de 'marcha pentecostal' (rápida), com temática de oração, vitória, fogo e comunhão.\n"
                "- Evite totalmente sonoridade sertaneja/country (banjo, viola caipira, rodeio, sofrência).\n"
            )
        if "pentecostal raiz" in combined:
            extra_rules += (
                "- Clima 'raiz' e acústico (violão/pandeiro/bateria), mantendo simplicidade e impacto.\n"
                "- Evite elementos modernos/eletro e metálicos; foque no percussivo.\n"
            )

        prompt = f"""
Crie uma letra de música ORIGINAL baseada no tema e na mensagem.

Tema: {theme}
Mensagem: {message}
Idioma: {lang}
Estilo: {style or 'livre'}
Gênero: {genre or 'livre'}

Regras:
- Letra com estrutura clara: Verso 1, Pré-Refrão, Refrão, Verso 2, Refrão, Ponte, Refrão Final.
- Sem palavrões.
- Sem citar marcas, artistas ou músicas existentes.
- Sem usar markdown.
- Refrão deve repetir a mensagem de forma memorável.
{extra_rules}

Retorne APENAS um JSON válido no formato:
{{
  "title": "Título curto e memorável",
  "lyrics": "Letra completa com quebras de linha"
}}
"""
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um compositor profissional. Retorne somente JSON válido.",
                temperature=0.85,
                json_mode=True
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            import json
            clean = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean)
            title = (data.get("title") or "Música").strip()[:120]
            lyrics = (data.get("lyrics") or "").strip()
            if not lyrics:
                raise Exception("Letra vazia")
            return {"title": title, "lyrics": lyrics}
        except Exception as e:
            print(f"Erro ao gerar letra: {e}")
            title = f"{theme.title()} - Mensagem"
            lyrics = f"Verso 1\n{theme}\n\nRefrão\n{message}\n"
            return {"title": title, "lyrics": lyrics}

    def improve_song_lyrics(self, lyrics: str, instruction: str, language: str = "pt-BR", style: str = "", genre: str = ""):
        self._load_config()
        original = (lyrics or "").strip()
        req = (instruction or "").strip()
        lang = (language or "pt-BR").strip()
        style = (style or "").strip()
        genre = (genre or "").strip()

        if not original or not req:
            return {"lyrics": original}

        if not self.openrouter_key:
            return {"lyrics": original}

        combined = f"{style} {genre}".strip().lower()
        combined = combined.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
        combined = combined.replace("é", "e").replace("ê", "e")
        combined = combined.replace("í", "i")
        combined = combined.replace("ó", "o").replace("ô", "o").replace("õ", "o")
        combined = combined.replace("ú", "u")

        extra_rules = ""
        if any(k in combined for k in ["pentecostal", "corinho", "corinho de fogo", "fogo no pe", "fogo no pe'"]):
            extra_rules = (
                "\nRegras específicas do estilo (Corinho / Pentecostal):\n"
                "- Linguagem congregacional, simples e direta.\n"
                "- Frases curtas, rítmicas e repetíveis.\n"
                "- Refrão forte com chamada-e-resposta e energia alta.\n"
                "- Evite romantização/sofrência e gírias seculares.\n"
            )
        if any(k in combined for k in ["corinho tradicional", "culto de oracao", "culto de oração"]):
            extra_rules += (
                "- Puxada de marcha pentecostal, temática de oração/vitória.\n"
                "- Evite totalmente sonoridade sertaneja/country e linguagem associada.\n"
            )
        if "pentecostal raiz" in combined:
            extra_rules += (
                "- Clima 'raiz' e acústico (violão/pandeiro/bateria), simplicidade e impacto.\n"
            )

        import json
        prompt = f"""
Você é um revisor e compositor profissional de letras de música cristã em {lang}.

OBJETIVO:
Aplicar o pedido de melhoria do usuário na letra existente, mantendo o sentido, coerência, ritmo cantável e contexto teológico.

PEDIDO DO USUÁRIO (execute com precisão):
{req}

LETRA ORIGINAL:
{original[:5200]}

REGRAS:
- Preserve o tema, a mensagem e o contexto bíblico/teológico.
- Preserve a estrutura (Verso/Refrão/Ponte etc.) e as quebras de linha. Não transforme em prosa.
- Faça a escansão (métrica/ritmo das sílabas) internamente quando o pedido mencionar contagem de sílabas, sílabas tônicas, métrica ou "8 sílabas", e ajuste a(s) frase(s) para cumprir exatamente.
- Se o pedido mencionar rima rica/consoante, garanta rimas ricas nas linhas relevantes sem forçar palavras estranhas.
- Se o pedido mencionar substituir ou ajustar uma frase específica, altere apenas o mínimo necessário no restante da letra para manter fluidez e coerência.
- Não inclua marcas, links, nomes de artistas, nem explicações.
{extra_rules}

Retorne APENAS um JSON válido no formato:
{{ "lyrics": "Letra completa melhorada com quebras de linha" }}
"""
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um compositor e revisor. Retorne somente JSON válido no formato solicitado.",
                temperature=0.55,
                json_mode=True,
            )
            if not content:
                raise Exception("Resposta vazia da IA")
            clean = content.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean) if clean else {}
            improved = (data.get("lyrics") or "").strip() if isinstance(data, dict) else ""
            if not improved:
                raise Exception("Letra vazia")
            return {"lyrics": improved}
        except Exception as e:
            print(f"Erro ao melhorar letra: {e}")
            return {"lyrics": original}

    def lyrics_to_music_prompt(self, lyrics: str, title: str = "", genre: str = ""):
        """Converte letra em prompt para geração de música instrumental (MusicGen)."""
        self._load_config()
        if not self.openrouter_key:
            return f"Emotional instrumental music, {genre or 'pop ballad'}. Cinematic, no lyrics."
        prompt = f"""Com base nesta letra, crie UM prompt em inglês para música INSTRUMENTAL (sem voz). Uma frase curta (até 80 palavras).
Título: {title or 'Sem título'}
Gênero: {genre or 'qualquer'}
Letra: {lyrics[:1200]}
Retorne APENAS o prompt, sem aspas."""
        try:
            out = self._generate_text(prompt, system_prompt="You output only the music prompt.", temperature=0.7)
            return (out or "").strip()[:300] or f"Emotional instrumental, {genre or 'cinematic'}. No lyrics."
        except Exception as e:
            print(f"Erro ao gerar prompt de música: {e}")
            return f"Emotional instrumental music, {genre or 'pop'}. Cinematic, no lyrics."

    def lyrics_to_clip_scenes(self, lyrics: str, title: str = ""):
        """Converte letra em cenas (texto + image_prompt) para clipe."""
        self._load_config()
        import re
        lines = [l.strip() for l in (lyrics or "").strip().split("\n") if l.strip()]
        label_re = re.compile(r"^(verso|refr[aã]o|pr[eé]-?refr[aã]o|ponte|intro|outro|coro|bridge|chorus)\b", re.IGNORECASE)
        lines = [l for l in lines if not label_re.match(l)]
        if not lines:
            return [{"text": title or "Música", "image_prompt": "abstract music visual"}]
        scenes = []
        for block in lines:
            if self.openrouter_key:
                prompt = f"""Lyric line (Portuguese): "{block[:260]}". Song title: {title or 'Song'}. Create ONE image prompt in English for a photorealistic cinematic music video scene that matches the lyric literally. No text in image. One sentence."""
                try:
                    ip = self._generate_text(prompt, system_prompt="Output only the image prompt.", temperature=0.7)
                    image_prompt = (ip or "").strip()[:250] or "cinematic music video scene"
                except Exception:
                    image_prompt = "cinematic music video scene"
            else:
                image_prompt = "cinematic music video scene"
            scenes.append({"text": block, "image_prompt": image_prompt})
        return scenes if scenes else [{"text": title or "Música", "image_prompt": "abstract music visual"}]

    def generate_music(self, prompt):
        """Gera música usando Hugging Face (MusicGen)"""
        # Se não tiver token, tenta sem (pode falhar por rate limit)
        # URL atualizada conforme erro 410
        API_URL = "https://router.huggingface.co/models/facebook/musicgen-small"
        # Fallback URL antiga se necessário
        # API_URL = "https://api-inference.huggingface.co/models/facebook/musicgen-small"
        
        headers = {}
        if self.hf_token:
            headers["Authorization"] = f"Bearer {self.hf_token}"
        
        # Otimiza o prompt para música de fundo
        music_prompt = f"Background music, {prompt}. High quality, cinematic, ambient, no lyrics, loopable."
        
        try:
            payload = {"inputs": music_prompt}
            response = requests.post(API_URL, headers=headers, json=payload)
            
            if response.status_code == 200:
                return response.content
            else:
                print(f"Erro HF MusicGen: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            print(f"Erro ao gerar música: {e}")
            return None
