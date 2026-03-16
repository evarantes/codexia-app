import os
import uuid
import openai
import requests
import google.generativeai as genai
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
        self.gemini_key = None
        self.deepseek_key = None
        self.groq_key = None
        self.anthropic_key = None
        self.mistral_key = None
        self.openrouter_key = None
        self.elevenlabs_key = None
        self.elevenlabs_voice_id = None
        self.elevenlabs_voice_name = None
        self.provider = "openai"
        self.hf_token = os.getenv("HUGGINGFACE_TOKEN") # Para MusicGen

        if settings:
            self.api_key = settings.openai_api_key
            self.gemini_key = settings.gemini_api_key
            self.deepseek_key = settings.deepseek_api_key
            self.groq_key = settings.groq_api_key
            self.anthropic_api_key = settings.anthropic_api_key
            self.mistral_key = settings.mistral_api_key
            self.openrouter_key = settings.openrouter_api_key
            self.elevenlabs_key = settings.elevenlabs_api_key
            self.elevenlabs_voice_id = getattr(settings, "elevenlabs_voice_id", None)
            self.elevenlabs_voice_name = getattr(settings, "elevenlabs_voice_name", None)
            self.provider = settings.ai_provider or "openai"
        
        # Fallback to env vars
        if not self.api_key: self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.gemini_key: self.gemini_key = os.getenv("GEMINI_API_KEY")
        if not self.deepseek_key: self.deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if not self.groq_key: self.groq_key = os.getenv("GROQ_API_KEY")
        if not self.anthropic_key: self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.mistral_key: self.mistral_key = os.getenv("MISTRAL_API_KEY")
        if not self.openrouter_key: self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not self.elevenlabs_key: self.elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

        # Configure Gemini
        if self.gemini_key:
            genai.configure(api_key=self.gemini_key)

    def _generate_text(self, prompt, system_prompt=None, temperature=0.7, json_mode=False):
        """Unified method to generate text using the configured provider"""
        self._load_config()
        
        providers_to_try = []
        
        # Determine priority list
        available_providers = []
        if self.api_key: available_providers.append("openai")
        if self.gemini_key: available_providers.append("gemini")
        if self.deepseek_key: available_providers.append("deepseek")
        if self.anthropic_key: available_providers.append("anthropic")
        if self.mistral_key: available_providers.append("mistral")
        if self.groq_key: available_providers.append("groq")
        if self.openrouter_key: available_providers.append("openrouter")

        providers_to_try = []
        
        if self.provider == "hybrid":
             # Priority: OpenAI -> DeepSeek -> Anthropic -> Mistral -> Gemini -> Groq -> OpenRouter
             preferred_order = ["openai", "deepseek", "anthropic", "mistral", "gemini", "groq", "openrouter"]
             providers_to_try = [p for p in preferred_order if p in available_providers]
             # Add any others not in preferred list but available
             for p in available_providers:
                 if p not in providers_to_try:
                     providers_to_try.append(p)
        else:
            # User selected specific provider. Try it first.
            if self.provider in available_providers:
                providers_to_try.append(self.provider)
            
            # Then fallback to ALL other available providers (AUTO-FALLBACK)
            for p in available_providers:
                if p != self.provider:
                    providers_to_try.append(p)
            
            # If the selected provider wasn't available (no key), we still try others.


        last_error = None

        for current_provider in providers_to_try:
            try:
                if current_provider == "mistral" and self.mistral_key:
                    import requests
                    headers = {
                        "Authorization": f"Bearer {self.mistral_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    }
                    
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": prompt})

                    data = {
                        "model": "mistral-small-latest",
                        "messages": messages,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"} if json_mode else None
                    }
                    
                    response = requests.post(
                        "https://api.mistral.ai/v1/chat/completions",
                        headers=headers,
                        json=data
                    )
                    
                    if response.status_code != 200:
                        raise Exception(f"Mistral Error {response.status_code}: {response.text}")
                        
                    return response.json()["choices"][0]["message"]["content"]

                elif current_provider == "openrouter" and self.openrouter_key:
                    # Use OpenAI Client compatible interface
                    client = openai.OpenAI(
                        api_key=self.openrouter_key, 
                        base_url="https://openrouter.ai/api/v1",
                        default_headers={"HTTP-Referer": "https://codexia.com", "X-Title": "Codexia"}
                    )
                    
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": prompt})

                    # OpenRouter auto-routes, but we can specify a cheap default like auto or specific
                    response = client.chat.completions.create(
                        model="openai/gpt-3.5-turbo", # OpenRouter supports mapping, or use "mistralai/mistral-7b-instruct"
                        messages=messages,
                        temperature=temperature,
                        response_format={"type": "json_object"} if json_mode else None
                    )
                    return response.choices[0].message.content

                elif current_provider == "anthropic" and self.anthropic_key:
                    import requests
                    headers = {
                        "x-api-key": self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    }
                    
                    system_msg = system_prompt if system_prompt else "You are a helpful assistant."
                    if json_mode:
                        system_msg += " Output ONLY valid JSON."

                    data = {
                        "model": "claude-3-haiku-20240307", # Cheap and fast
                        "max_tokens": 4000,
                        "system": system_msg,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature
                    }
                    
                    response = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers=headers,
                        json=data
                    )
                    
                    if response.status_code != 200:
                        raise Exception(f"Anthropic Error {response.status_code}: {response.text}")
                        
                    result = response.json()
                    return result["content"][0]["text"]

                elif current_provider == "gemini" and self.gemini_key:
                    # Lista de modelos para tentar em ordem de preferência/custo
                    models_to_try = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro', 'gemini-1.0-pro']
                    gemini_error = None
                    
                    for model_name in models_to_try:
                        try:
                            model = genai.GenerativeModel(model_name)
                            final_prompt = prompt
                            if system_prompt:
                                final_prompt = f"System Instruction: {system_prompt}\n\nUser Request: {prompt}"
                            if json_mode:
                                final_prompt += "\n\nIMPORTANT: Output ONLY valid JSON."
                            
                            response = model.generate_content(
                                final_prompt,
                                generation_config=genai.types.GenerationConfig(
                                    temperature=temperature,
                                    response_mime_type="application/json" if json_mode else "text/plain"
                                )
                            )
                            return response.text
                        except Exception as e:
                            print(f"Gemini model {model_name} failed: {e}")
                            gemini_error = e
                            continue # Tenta o próximo modelo da lista
                    
                    # Se todos os modelos Gemini falharem, lança erro para tentar próximo provedor
                    if gemini_error:
                        raise gemini_error

                elif current_provider == "openai" and self.api_key:
                    # Use OpenAI Client (v1.0+) explicitly to avoid global state issues
                    client = openai.OpenAI(api_key=self.api_key)
                    
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": prompt})

                    try:
                        response = client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=messages,
                            temperature=temperature,
                            response_format={"type": "json_object"} if json_mode else None
                        )
                        return response.choices[0].message.content
                    except Exception as e:
                        print(f"OpenAI Error: {e}")
                        raise e

                elif current_provider == "deepseek" and self.deepseek_key:
                    # Use OpenAI Client compatible interface
                    client = openai.OpenAI(api_key=self.deepseek_key, base_url="https://api.deepseek.com")
                    
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": prompt})

                    response = client.chat.completions.create(
                        model="deepseek-chat",
                        messages=messages,
                        temperature=temperature,
                        response_format={"type": "json_object"} if json_mode else None
                    )
                    return response.choices[0].message.content

                elif current_provider == "groq" and self.groq_key:
                    # Use OpenAI Client compatible interface
                    client = openai.OpenAI(api_key=self.groq_key, base_url="https://api.groq.com/openai/v1")
                    
                    messages = []
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": prompt})

                    # Groq supports Llama 3 8b/70b
                    response = client.chat.completions.create(
                        model="llama3-70b-8192",
                        messages=messages,
                        temperature=temperature,
                        response_format={"type": "json_object"} if json_mode else None
                    )
                    return response.choices[0].message.content

            except Exception as e:
                print(f"Erro no provedor {current_provider}: {e}")
                last_error = e
                continue # Try next provider
        
        # If we get here, all providers failed
        if last_error:
            print(f"CRITICAL: All AI providers failed. Last error: {last_error}")
            raise Exception("Todas as IAs configuradas estão indisponíveis ou sem saldo. Verifique suas chaves de API e tente novamente.")
        return None

    def generate_book_section(self, section_type, context_text, title, existing_content=None):
        """Generates specific book sections like synopsis, epigraph, preface. Can rewrite existing content."""
        self._load_config()
        # Verify if any key is available
        if not (self.api_key or self.gemini_key or self.deepseek_key or self.anthropic_key or self.mistral_key or self.groq_key or self.openrouter_key):
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
        
        if not (self.api_key or self.gemini_key or self.deepseek_key or self.anthropic_key or self.mistral_key or self.groq_key or self.openrouter_key):
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

        if not (self.api_key or self.gemini_key):
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

        # [REMOVED] StockService priority removed to ensure AI generation with custom text (Title/Author) as requested.
        # Previously, stock images were returned without any text overlay.
        
        # Mock response if no API key (usa chave do banco ou env)
        if not self.api_key:
            colors = ["1e293b", "4f46e5", "059669"]
            return [f"https://placehold.co/400x600/{color}/ffffff?text={title[:10]}...%0A{author}" for i, color in enumerate(colors[:n])]

        import json
        try:
            client = openai.OpenAI(api_key=self.api_key)
            # 1. Get Prompts from GPT to ensure variety and relevance to the central message
            prompt_gen_prompt = f"""
            Crie {n} descrições visuais artísticas e EXCLUSIVAS para a capa do livro '{title}'.
            Contexto/Mensagem Central: {context[:500]}
            Gênero/Estilo: Identifique pelo contexto.
            
            OBJETIVO: Despertar o desejo de leitura através de uma imagem impactante e simbólica.
            FOCO: Apenas a descrição da imagem (cenário, elementos, cores, estilo artístico). NÃO mencione texto ou tipografia aqui.
            
            Retorne apenas um JSON: {{ "prompts": ["descrição visual 1...", "descrição visual 2...", "descrição visual 3..."] }}
            """
            
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt_gen_prompt}],
                    temperature=0.8
                )
                content = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
                prompts_data = json.loads(content)
                prompts = prompts_data.get("prompts", []) if isinstance(prompts_data, dict) else []
            except Exception as e_prompt:
                print(f"Error generating cover prompts: {e_prompt}")
                prompts = [f"A conceptual artistic cover representing {context[:50]}"] * n
            
            # Ensure we have enough prompts
            while len(prompts) < n:
                prompts.append(prompts[0] if prompts else f"Artistic cover for {title}")

            # Título e autor devem aparecer exatamente como especificado na capa
            title_display = title.strip() if title else "Livro"
            author_display = author.strip() if author else ""
            subtitle_display = subtitle.strip() if subtitle else ""
            
            image_urls = []
            for p in prompts[:n]:
                # Construct a very specific prompt for DALL-E 3 to handle text
                dalle_prompt = f"""
                Create a rectangular, flat 2D digital artwork for a book cover.

                MANDATORY TEXT TO WRITE ON IMAGE:
                Title: "{title_display}"
                """
                
                if subtitle_display:
                    dalle_prompt += f'Subtitle: "{subtitle_display}"\n'
                
                if author_display:
                    dalle_prompt += f'Author Name: "{author_display}"\n'
                
                dalle_prompt += f"""
                
                VISUAL ART DESCRIPTION:
                {p}
                
                STRICT LAYOUT RULES:
                1. FORMAT: A flat digital image file. NO 3D MOCKUPS. NO SPINES. NO BACKGROUND SURFACE. NO TABLE.
                2. TEXT: Write the Title, Subtitle, and Author Name EXACTLY as provided above.
                3. HIERARCHY:
                   - Title: Big, bold, centered.
                   - Subtitle: Smaller, below the title.
                   - Author: Small, at the very bottom center.
                4. STYLE: High-quality illustration or graphic design. 
                5. Do NOT include any other text.
                """
                
                try:
                    img_res = client.images.generate(
                        model="dall-e-3",
                        prompt=dalle_prompt.strip()[:4000], # Ensure within limit
                        n=1,
                        size="1024x1792", # Vertical aspect ratio for books
                        quality="standard",
                        style="vivid"
                    )
                    image_urls.append(img_res.data[0].url)
                except Exception as e_img:
                    print(f"Error generating single image: {e_img}")
            
            # Fallback if DALL-E fails completely
            while len(image_urls) < n:
                fallback_color = "4f46e5"
                image_urls.append(f"https://placehold.co/400x600/{fallback_color}/ffffff?text={title_display}+Fail")
                
            return image_urls

        except Exception as e:
            print(f"Error generating covers: {e}")
            return [f"https://placehold.co/400x600?text=Error+{i+1}" for i in range(n)]

    def generate_music_placeholder(self, prompt: str):
        """Gera música a partir de um prompt (Placeholder)"""
        # Implementação futura com MusicGen/HuggingFace
        print(f"Solicitação de música recebida: {prompt}")
        return None

    def generate_video_script(self, book_title: str, synopsis: str, style: str = "drama"):
        self._load_config()
        
        # Se não tiver chave, retorna mock
        if not (self.api_key or self.gemini_key):
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
        if not (self.api_key or self.gemini_key):
            return {
                "title": "Short gerado",
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
        - Cenas: entre 3 e 5 cenas. Cada cena: "text" (frase narrada, curta) e "image_prompt" (descrição visual em inglês para gerar imagem com IA, sem texto na imagem).
        - Estilo: dinâmico, adequado para Shorts/Reels, gancho no início.

        Retorne APENAS este JSON (sem markdown, sem texto extra):
        {{
            "title": "Título do Short",
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
            return data
        except Exception as e:
            print(f"Erro ao gerar script de Short: {e}")
            return {
                "title": "Short inspirador",
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
        if not (self.api_key or self.gemini_key):
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
        if not (self.api_key or self.gemini_key):
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

    def enrich_scenes_with_image_prompts(self, plan: dict) -> dict:
        """
        Gera image_prompt profissionais com base na narração de cada cena, para a IA
        criar imagens próprias e montar o vídeo de forma profissional (YouTube Auto etc).
        Atualiza apenas cenas que não têm image_prompt ou têm um muito curto/genérico.
        """
        self._load_config()
        if not (self.api_key or self.gemini_key or self.deepseek_key or self.anthropic_key or self.groq_key or self.openrouter_key):
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

        prompt = f"""
        Você é um diretor de arte para vídeos narrados (YouTube, Shorts). Seu trabalho é criar descrições visuais para gerar imagens com IA (DALL-E/Flux) que ilustrem exatamente o que está sendo dito.

        Título do vídeo: {title}

        Narrações por cena:
        {scenes_desc}

        Para CADA cena acima, crie UMA descrição visual (image_prompt) em INGLÊS com as regras:
        - Representar fielmente a ideia e o clima da narração.
        - Estilo: ilustração digital autoral, concept art cinematográfica, composição artística exclusiva, alta definição.
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
                        scenes[scene_idx]["image_prompt"] = prompt_text[:500]
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
        
        if not (self.api_key or self.gemini_key):
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
        """Gera um banner para o canal do YouTube usando DALL-E 3"""
        self._load_config()
        
        # DALL-E requires OpenAI Key
        if not self.api_key:
            print("OpenAI Key missing for Image Generation. Skipping banner.")
            return None

        try:
            # Correct call for DALL-E 3
            response = openai.images.generate(
                model="dall-e-3",
                prompt=f"YouTube Channel Banner art, {prompt_text}, wide aspect ratio, professional design, minimal text, 4k resolution",
                size="1024x1024", 
                quality="standard",
                n=1,
            )
            return response.data[0].url
        except Exception as e:
            print(f"Error generating banner: {e}")
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
        
        prompt = f"""
        Atue como um Consultor de Monetização do YouTube.
        
        STATUS ATUAL:
        - Inscritos: {progress_data.get('subscribers')} (Meta: {progress_data.get('subscribers_target')})
        - Horas de Exibição Estimadas: {progress_data.get('estimated_watch_hours')} (Meta: {progress_data.get('watch_hours_target')})
        - Progresso Inscritos: {progress_data.get('subscribers_progress_pct')}%
        - Progresso Horas: {progress_data.get('watch_hours_progress_pct')}%
        
        MISSÃO:
        1. Analise o que falta para a monetização.
        2. Dê sugestões PRÁTICAS para acelerar o preenchimento das lacunas (ex: se faltam horas, sugerir lives ou vídeos longos; se faltam inscritos, sugerir shorts virais).
        
        Retorne APENAS um JSON válido:
        {{
            "summary": "Resumo da situação atual.",
            "gap_analysis": {{
                "subscribers_missing": 0,
                "watch_hours_missing": 0,
                "estimated_time_to_monetize": "Estimativa (ex: 3 meses)"
            }},
            "strategy_suggestion": "Sua principal estratégia para fechar o gap.",
            "weekly_actions": [
                "Ação prática 1",
                "Ação prática 2",
                "Ação prática 3"
            ]
        }}
        """
        
        try:
            content = self._generate_text(
                prompt,
                system_prompt="Você é um especialista em monetização do YouTube.",
                json_mode=True
            )
            
            if not content:
                raise Exception("Resposta vazia da IA")
                
            clean_content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_content)
            
        except Exception as e:
            print(f"Erro ao gerar insights de monetização: {e}")
            
            # Calculate missing values for fallback
            subs_target = progress_data.get('subscribers_target', 1000)
            subs_current = progress_data.get('subscribers', 0)
            subs_missing = max(0, subs_target - subs_current)
            
            hours_target = progress_data.get('watch_hours_target', 4000)
            hours_current = progress_data.get('estimated_watch_hours', 0)
            hours_missing = max(0, hours_target - hours_current)
            
            return {
                "summary": "Não foi possível gerar a análise detalhada da IA, mas aqui estão seus números.",
                "gap_analysis": {
                    "subscribers_missing": subs_missing,
                    "watch_hours_missing": hours_missing,
                    "estimated_time_to_monetize": "Calculando..."
                },
                "strategy_suggestion": "Continue postando conteúdo de qualidade com consistência.",
                "weekly_actions": [
                    "Verifique suas configurações de API da IA se o erro persistir.",
                    "Foque em Shorts para ganhar inscritos rapidamente.",
                    "Faça vídeos mais longos para aumentar as horas de exibição."
                ]
            }

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
        Gera URL de imagem por cadeia de provedores de IA.
        Retorna URL quando algum provedor aceita a solicitação.
        """
        self._load_config()
        raw_prompt = (prompt or "").strip()
        if not raw_prompt:
            return None

        width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        dalle_size = "1024x1792" if aspect_ratio == "9:16" else "1792x1024"

        quality_tokens = [
            "masterpiece", "unique artistic composition", "copyright free style",
            "cinematic lighting", "highly detailed", "sharp focus",
            "dramatic atmosphere", "award winning concept art", "digital painting",
            "hand-drawn illustration", "original artwork",
        ]
        negative_constraints = "no text, no watermarks, no signatures, no logos, no branded characters, no distorted faces, no blur"
        enhanced_prompt = raw_prompt
        if len(raw_prompt) < 240 and not any(token in raw_prompt.lower() for token in quality_tokens[:3]):
            enhanced_prompt = f"{raw_prompt}, {' '.join(quality_tokens[:7])}"

        provider_order = providers or ["openai_dalle3", "pollinations_flux", "pollinations_turbo", "pollinations"]
        provider_labels = {
            "openai_dalle3": "OpenAI DALL-E 3",
            "pollinations_flux": "Pollinations Flux",
            "pollinations_turbo": "Pollinations Turbo",
            "pollinations": "Pollinations Base",
        }

        def notify(message: str):
            if status_callback:
                try:
                    status_callback(message)
                except Exception:
                    pass

        for provider in provider_order:
            label = provider_labels.get(provider, provider)
            try:
                if provider == "openai_dalle3":
                    if not self.api_key:
                        notify(f"{label} sem chave configurada; tentando próximo provedor.")
                        continue
                    notify(f"Tentando {label}...")
                    full_prompt = (
                        f"{enhanced_prompt}. {negative_constraints}. "
                        "Original AI-generated illustration, cinematic concept art, highly detailed."
                    )
                    client = openai.OpenAI(api_key=self.api_key)
                    response = client.images.generate(
                        model="dall-e-3",
                        prompt=full_prompt,
                        size=dalle_size,
                        quality="hd",
                        n=1,
                        style="natural",
                    )
                    url = None
                    if getattr(response, "data", None):
                        item = response.data[0]
                        url = getattr(item, "url", None)
                    if url:
                        notify(f"{label} respondeu com sucesso.")
                        return url
                    notify(f"{label} não retornou URL válida; tentando próximo.")
                    continue

                if provider in {"pollinations_flux", "pollinations_turbo", "pollinations"}:
                    import urllib.parse
                    model = "flux" if provider == "pollinations_flux" else ("turbo" if provider == "pollinations_turbo" else "")
                    notify(f"Tentando {label}...")
                    pollinations_prompt = (
                        f"{enhanced_prompt} original ai illustration cinematic concept art "
                        "highly detailed digital painting no text no watermark"
                    )
                    safe_prompt = urllib.parse.quote(pollinations_prompt)
                    query = (
                        f"width={width}&height={height}&nologo=true&seed={uuid.uuid4()}&enhance=false"
                    )
                    if model:
                        query += f"&model={model}"
                    return f"https://image.pollinations.ai/prompt/{safe_prompt}?{query}"
            except Exception as e:
                reason = str(e).strip().replace("\n", " ")
                if len(reason) > 180:
                    reason = reason[:177] + "..."
                notify(f"{label} falhou ({reason}); tentando próximo provedor.")
                continue

        notify("Todos os provedores de imagem falharam nesta rodada.")
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
        """Gera áudio usando ElevenLabs (prioridade), OpenAI TTS ou fallback."""
        self._load_config()

        # 1. ElevenLabs (vozes ultra-realistas) - prioridade quando configurado
        if self.elevenlabs_key:
            audio_content = self._generate_audio_elevenlabs(text, voice)
            if audio_content:
                return audio_content

        # 2. OpenAI TTS (Human-like)
        if self.api_key:
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

        if not (self.api_key or self.gemini_key or self.deepseek_key or self.anthropic_key or self.groq_key or self.openrouter_key):
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

    def lyrics_to_music_prompt(self, lyrics: str, title: str = "", genre: str = ""):
        """Converte letra em prompt para geração de música instrumental (MusicGen)."""
        self._load_config()
        if not (self.api_key or self.gemini_key):
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
        lines = [l.strip() for l in lyrics.strip().split("\n") if l.strip()]
        if not lines:
            return [{"text": title or "Música", "image_prompt": "abstract music visual"}]
        scenes = []
        chunk = 3
        for i in range(0, len(lines), chunk):
            block = "\n".join(lines[i : i + chunk])
            if not block:
                continue
            if self.api_key or self.gemini_key:
                prompt = f"""Letra: "{block[:400]}". Título: {title or 'Música'}. Gere UM image_prompt em inglês para cena de clipe (visual artístico, sem texto na imagem). Uma frase."""
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
