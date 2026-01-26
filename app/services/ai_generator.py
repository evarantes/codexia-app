import os
import openai
import requests
from dotenv import load_dotenv
from app.database import SessionLocal
from app.models import Settings

load_dotenv()

class AIContentGenerator:
    def __init__(self):
        self._load_config()

    def _load_config(self):
        # Tenta carregar do banco primeiro, depois do .env
        db = SessionLocal()
        settings = db.query(Settings).first()
        db.close()

        self.api_key = None
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN") # Para MusicGen

        if settings and settings.openai_api_key:
            self.api_key = settings.openai_api_key
        
        if not self.api_key:
            self.api_key = os.getenv("OPENAI_API_KEY")

        if self.api_key and self.api_key != "sua_chave_openai_aqui":
            openai.api_key = self.api_key
        else:
            self.api_key = None
            print("⚠️ AVISO: Chave da OpenAI não configurada.")

    def generate_book_section(self, section_type, context_text, title):
        """Generates specific book sections like synopsis, epigraph, preface"""
        self._load_config()
        if not self.api_key:
            return "Conteúdo gerado por IA (Simulação)"

        prompts = {
            "synopsis": f"Escreva uma sinopse instigante para a quarta capa do livro '{title}'. Baseado neste contexto: {context_text[:1000]}...",
            "epigraph": f"Sugira uma epígrafe (citação curta e profunda) que combine com o tema do livro '{title}'. Contexto: {context_text[:500]}...",
            "preface": f"Escreva um prefácio curto para o livro '{title}', introduzindo o tema e preparando o leitor. Contexto: {context_text[:1000]}...",
            "dedication": f"Sugira uma dedicatória genérica e emocionante para o livro '{title}'."
        }
        
        prompt = prompts.get(section_type, f"Escreva um texto para {section_type} do livro '{title}'.")

        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Erro ao gerar seção {section_type}: {e}")
            return f"Erro ao gerar {section_type}."

    def generate_full_book_draft(self, title: str, idea: str, num_chapters: int, style: str = "didático", num_pages: int = 50):
        """Generates a full book structure and content based on an idea"""
        self._load_config()
        
        if not self.api_key:
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
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": outline_prompt}],
                temperature=0.7
            )
            import json
            content = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
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
            
            for chap in structure.get("chapters", []):
                chap_title = chap.get("title", "Capítulo")
                chap_summary = chap.get("summary", "")
                
                content_prompt = f"""
                Escreva o conteúdo completo do capítulo '{chap_title}' do livro '{title}'.
                Contexto do capítulo: {chap_summary}
                Estilo: {style}
                Meta de tamanho: Aprox. {words_per_chapter} palavras.
                
                Escreva de forma envolvente, detalhada e bem estruturada. Use parágrafos claros.
                """
                
                res_chap = openai.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": content_prompt}],
                    temperature=0.7
                )
                chap_content = res_chap.choices[0].message.content
                
                final_chapters.append({
                    "title": chap_title,
                    "content": chap_content
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
            print(f"Erro ao gerar livro: {e}")
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

        if not self.api_key:
            return self._mock_response(book_title, style)

        prompt = self._build_prompt(book_title, synopsis, style)
        
        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Você é um especialista em copywriting para venda de livros. Crie textos persuasivos, emocionantes e com alto potencial de conversão."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=500
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Erro na OpenAI: {e}")
            return self._mock_response(book_title, style, error=str(e))

    def generate_cover_options(self, title: str, context: str, author: str = "", subtitle: str = "", n: int = 3):
        self._load_config()
        
        # Mock response if no API key
        if not self.api_key:
            # Return distinct placeholder colors/text
            colors = ["1e293b", "4f46e5", "059669"]
            return [f"https://placehold.co/400x600/{color}/ffffff?text={title[:10]}...%0A{author}" for i, color in enumerate(colors[:n])]

        try:
            # 1. Get Prompts from GPT to ensure variety
            # We ask for visual descriptions ONLY, we will handle text placement in the final prompt
            prompt_gen_prompt = f"""
            Crie {n} descrições visuais artísticas e detalhadas para a capa do livro '{title}'.
            Contexto/Sinopse: {context[:500]}
            Gênero/Estilo: Identifique pelo contexto.
            
            FOCO: Apenas a descrição da imagem (cenário, elementos, cores, estilo artístico). NÃO descreva onde o texto fica.
            
            Retorne apenas um JSON: {{ "prompts": ["descrição visual 1...", "descrição visual 2...", "descrição visual 3..."] }}
            """
            
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt_gen_prompt}],
                temperature=0.7
            )
            import json
            content = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
            prompts = json.loads(content).get("prompts", [])
            
            image_urls = []
            # 2. Generate Images with DALL-E 3 (Better text support)
            for p in prompts[:n]:
                 # Construct a specific prompt for DALL-E 3 to handle text
                 dalle_prompt = f"""
                 A professional book cover design.
                 Title: "{title}"
                 Author: "{author}"
                 Subtitle: "{subtitle}"
                 
                 Visual Art: {p}
                 
                 Layout instructions:
                 - The Title "{title}" must be clearly visible, large, and elegant.
                 - The Author name "{author}" should be legible, usually at the bottom or top.
                 - The Subtitle "{subtitle}" (if present) should be smaller and balanced.
                 - Ensure the text does not blend into the background. Use contrasting colors for text.
                 - High quality, cinematic lighting, 8k resolution.
                 """
                 
                 try:
                     img_res = openai.images.generate(
                         model="dall-e-3",
                         prompt=dalle_prompt.strip(),
                         n=1,
                         size="1024x1792", # Vertical format for books supported by DALL-E 3
                         quality="standard",
                         style="vivid"
                     )
                     image_urls.append(img_res.data[0].url)
                 except Exception as e_img:
                     print(f"Error generating single image: {e_img}")
                     # Fallback or retry?
            
            # Fill if failed to get enough
            while len(image_urls) < n:
                 image_urls.append(f"https://placehold.co/400x600?text=Falha+na+Geracao")

            return image_urls

        except Exception as e:
            print(f"Error generating covers: {e}")
            return [f"https://placehold.co/400x600?text=Error+{i+1}" for i in range(n)]

    def generate_video_script(self, book_title: str, synopsis: str, style: str = "drama"):
        self._load_config()
        
        # Se não tiver chave, retorna mock
        if not self.api_key:
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
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Você é um roteirista de vídeo especialista em trailers de livros. Retorne apenas JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=600
            )
            import json
            content = response.choices[0].message.content
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

    def generate_motivational_script(self, topic, duration_minutes=5):
        """Gera um roteiro longo para vídeo motivacional"""
        self._load_config()
        if not self.api_key:
            return self._mock_response(topic, "motivational_long")

        prompt = f"""
        Crie um Roteiro de Vídeo Motivacional Profundo de {duration_minutes} minutos sobre '{topic}'.
        Estilo: Inspirador, Estoico, Narrativa Poderosa.
        
        O roteiro deve ser estruturado para manter a retenção.
        Divida em 5 partes principais (Introdução, Problema, Virada, Solução/Mindset, Conclusão/CTA).
        
        Retorne APENAS um JSON válido com a estrutura:
        {{
            "title": "Título Impactante (SEO Friendly)",
            "description": "Descrição otimizada para YouTube com hashtags",
            "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
            "scenes": [
                {{"text": "Texto EXATO da narração (sem 'Cena 1:', sem 'Narrador:', apenas o que será falado)...", "image_prompt": "Descrição visual..."}},
                ...
            ],
            "music_mood": "epic_cinematic"
        }}
        """
        
        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo-16k", # Modelo com janela maior para texto longo
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8
            )
            import json
            content = response.choices[0].message.content
            # Limpeza básica de markdown json
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao gerar roteiro motivacional: {e}")
            return self._mock_response(topic, "motivational_long", error=str(e))

    def generate_script_from_text(self, text, duration_minutes=5):
        """Estrutura um texto existente em formato de roteiro de vídeo"""
        self._load_config()
        if not self.api_key:
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
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo-16k",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            import json
            content = response.choices[0].message.content
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao estruturar roteiro do texto: {e}")
            return self._mock_response("História do Usuário", "motivational_long", error=str(e))

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
            "new_title": "Novo Nome Sugerido",
            "new_description": "Nova descrição sugerida...",
            "banner_prompt": "Descrição visual para o banner do canal..."
        }}
        """
        
        if not self.api_key:
            return {
                "analysis": "Simulação: O canal tem potencial mas precisa de consistência.",
                "action_plan": ["Postar 2x por semana", "Melhorar Thumbnails", "Focar em Shorts"],
                "new_title": "Codexia - Livros & Mente",
                "new_description": "Canal oficial sobre livros e desenvolvimento pessoal. Inscreva-se para transformar sua vida.",
                "banner_prompt": "Uma biblioteca mística com luz dourada, estilo digital art, alta qualidade, 4k"
            }
            
        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}]
            )
            import json
            content = response.choices[0].message.content
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            return {"error": str(e)}

    def generate_banner_image(self, prompt_text: str) -> str:
        """Gera um banner para o canal do YouTube usando DALL-E 3"""
        self._load_config()
        if not self.api_key:
            return None

        try:
            response = openai.chat.completions.create(
                model="dall-e-3",
                prompt=f"YouTube Channel Banner, 16:9 aspect ratio, professional, high quality. Theme: {prompt_text}",
                size="1024x1024", # DALL-E 3 standard, YouTube will crop/resize
                quality="standard",
                n=1,
            )
            # Nota: DALL-E 3 gera 1024x1024 ou 1024x1792. Para banner YouTube ideal é 2560x1440.
            # Vamos usar o link gerado. O YouTube Service terá que lidar com o upload.
            # DALL-E 3 API (via images.generate, not chat.completions - correção abaixo)
            return response.data[0].url
        except Exception as e:
            # Fallback para correção da chamada
            try:
                response = openai.images.generate(
                    model="dall-e-3",
                    prompt=f"YouTube Channel Banner art, {prompt_text}, wide aspect ratio, professional design, minimal text",
                    size="1024x1024", 
                    quality="standard",
                    n=1,
                )
                return response.data[0].url
            except Exception as e2:
                print(f"Erro ao gerar banner: {e2}")
                return None

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
        
        if not self.api_key:
            return {
                "analysis": "Monitoramento simulado: Canal estável.",
                "strategy": "Continue postando regularmente para aumentar engajamento."
            }
            
        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}]
            )
            import json
            content = response.choices[0].message.content
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            return {"analysis": "Erro na análise.", "strategy": "Verifique logs."}

    def _build_prompt(self, title, synopsis, style):
        if style == "cliffhanger":
            return f"Crie um anúncio curto e misterioso para o livro '{title}'. Sinopse: {synopsis}. Termine com um gancho forte."
        elif style == "storytelling":
            return f"Conte uma história curta e emocionante baseada no livro '{title}'. Sinopse: {synopsis}. Foque na jornada do herói."
        else: # direct
            return f"Crie um anúncio de vendas direto e persuasivo para o livro '{title}'. Sinopse: {synopsis}. Liste 3 benefícios e faça uma oferta irresistível."

    def generate_content_plan(self, theme, duration_type="days", duration_value=7, start_date=None, videos_per_day=1, video_duration=5):
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
        
        Para CADA dia ({total_days} dias), eu preciso de:
        1. {videos_per_day} Vídeo(s) Longo(s) com Título, Ideia Central e Horário sugerido.
        
        IMPORTANTE: As datas devem ser sequenciais a partir de {start_date_obj.strftime('%Y-%m-%d')}.
        
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
                        }}
                    ]
                }}
            ]
        }}
        """
        
        if not self.api_key:
             # Mock response
             mock_plan = []
             for i in range(total_days):
                 current_date = start_date_obj + timedelta(days=i)
                 mock_plan.append({
                     "day": i + 1,
                     "date": current_date.strftime('%Y-%m-%d'),
                     "theme_of_day": f"Tema do Dia {i+1}: {theme}",
                     "videos": [
                         {"title": f"Manhã: {theme} {i+1}", "concept": "Conceito manhã", "time": "08:00", "type": "video"},
                         {"title": f"Tarde: {theme} {i+1}", "concept": "Conceito tarde", "time": "14:00", "type": "video"},
                         {"title": f"Noite: {theme} {i+1}", "concept": "Conceito noite", "time": "20:00", "type": "video"},
                         {"title": f"Short 1: {theme}", "concept": "Curiosidade", "time": "10:00", "type": "short"},
                         {"title": f"Short 2: {theme}", "concept": "Dica", "time": "18:00", "type": "short"}
                     ]
                 })
             return mock_plan
        
        try:
            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            content = response.choices[0].message.content
            content = content.replace("```json", "").replace("```", "")
            return json.loads(content)
        except Exception as e:
            print(f"Erro ao gerar plano: {e}")
            return []

    def _mock_response(self, title, style, error=None):
        base_msg = f"⚠️ MODO SIMULAÇÃO (Vá em Configurações e adicione sua chave OpenAI)\n\n"
        if error:
            base_msg += f"Erro detectado: {error}\n\n"
            
        if style == "cliffhanger":
            return base_msg + f"🔥 [Simulação] O mistério de '{title}' vai te prender..."
        elif style == "storytelling":
            return base_msg + f"📖 [Simulação] Quando escrevi '{title}', eu queria..."
        elif style == "motivational_long":
            import json
            return {
                "title": f"Motivação: {title} (Vídeo Épico)",
                "description": "Vídeo motivacional gerado automaticamente.",
                "scenes": [
                    {"text": "A vida é cheia de desafios... mas você pode superá-los.", "image_prompt": "Mountain peak sunrise"},
                    {"text": "Não desista agora, o sucesso está logo ali.", "image_prompt": "Runner crossing finish line"},
                    {"text": "Acredite em si mesmo.", "image_prompt": "Lion looking at horizon"}
                ],
                "music_mood": "epic"
            }
        else:
            return base_msg + f"🎬 [Simulação] Roteiro para '{title}'..."

    def generate_image(self, prompt):
        self._load_config()
        if not self.api_key:
            return None
            
        try:
            # Enforcing original, artistic creation via prompt engineering
            full_prompt = f"{prompt}. Vertical aspect ratio 9:16. Original digital art, unique composition, cinematic lighting, 8k resolution, highly detailed. No text, copyright free style."
            
            response = openai.images.generate(
                model="dall-e-3",
                prompt=full_prompt,
                size="1024x1792",
                quality="standard",
                n=1,
            )
            return response.data[0].url
        except Exception as e:
            print(f"Erro ao gerar imagem: {e}")
            return None

    def generate_banner_image(self, prompt):
        self._load_config()
        if not self.api_key:
            return None
            
        try:
            full_prompt = f"{prompt}. Horizontal YouTube Channel Banner, 16:9 aspect ratio. Professional digital art, high quality, 4k. No text."
            
            response = openai.images.generate(
                model="dall-e-3",
                prompt=full_prompt,
                size="1792x1024", # Horizontal
                quality="standard",
                n=1,
            )
            return response.data[0].url
        except Exception as e:
            print(f"Erro ao gerar banner: {e}")
            return None

    def generate_audio(self, text, voice="onyx"):
        """Gera áudio usando OpenAI TTS (Human-like)"""
        self._load_config()
        if not self.api_key:
            return None
            
        try:
            response = openai.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text
            )
            return response.content
        except Exception as e:
            print(f"Erro ao gerar áudio OpenAI: {e}")
            return None

    def generate_music(self, prompt):
        """Gera música usando Hugging Face (MusicGen)"""
        # Se não tiver token, tenta sem (pode falhar por rate limit)
        # URL da API de inferência (gratuita com limitações)
        API_URL = "https://api-inference.huggingface.co/models/facebook/musicgen-small"
        
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
