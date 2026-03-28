import gc
import json
import math
import os
import random
import traceback
import uuid
import wave
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

from app.config import VIDEO_OUTPUT_DIR
from app.database import SessionLocal
from app.models import ScheduledVideo
from app.modules.humor_factory.models import HumorChannel, HumorProject
from app.services.ai_generator import AIContentGenerator
from app.services.video_generator import VideoGenerator
from app.services.youtube_service import YouTubeService


class HumorFactoryService:
    def __init__(self):
        self.ai = AIContentGenerator()

    def _append_log(self, project: HumorProject, message: str):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        project.logs = (project.logs or "") + f"[{stamp}] {message}\n"

    def _set_progress(self, db, project: HumorProject, progress: int, message: Optional[str] = None):
        p = max(0, min(100, int(progress)))
        if p > (project.progress or 0):
            project.progress = p
        if message:
            project.status_message = message[:240]
            self._append_log(project, message)
        project.updated_at = datetime.now()
        db.commit()

    def _parse_manual_jokes(self, raw: Optional[str]) -> List[str]:
        lines = [ln.strip(" -\t\r\n") for ln in str(raw or "").splitlines()]
        jokes = [ln for ln in lines if ln and len(ln) >= 8]
        return jokes

    def _parse_manual_script_blocks(self, raw: Optional[str]) -> List[str]:
        text = str(raw or "").replace("\r\n", "\n").strip()
        if not text:
            return []
        blocks = [blk.strip() for blk in text.split("\n\n") if blk and blk.strip()]
        if not blocks:
            lines = [ln.strip() for ln in text.split("\n") if ln and ln.strip()]
            if lines:
                blocks = lines
        clean_blocks = []
        for block in blocks:
            normalized = " ".join(block.split()).strip()
            if len(normalized) >= 20:
                clean_blocks.append(normalized[:1200])
        return clean_blocks

    def _fallback_jokes(self, theme: str, count: int) -> List[str]:
        base = [
            f"No tema {theme}, eu sentei no boteco e falei que ia pedir só um café. Quando vi, já tava ouvindo a história da cidade inteira e saí com dois convites de casamento.",
            f"Na pegada {theme}, meu amigo prometeu que era só um culto curtinho. Começou com aleluia, terminou com o relógio pedindo oração de libertação.",
            f"No bairro {theme}, peguei o ônibus lotado e o cobrador me olhou com cara de coach: 'hoje você vence'. Eu venci foi o medo de cair na curva.",
            f"Na família {theme}, falaram que o almoço era simples. Simples nada: teve fila da sobremesa, testemunho da tia e até reprise de treta de 2014.",
            f"No trabalho {theme}, o chefe perguntou se estava pronto e eu disse que sim, no coração. Na prática, faltavam só pequenos detalhes... tipo tudo.",
            f"Na escola do tema {theme}, o professor perguntou quem estudou. A sala inteira ficou em silêncio, até a caneta caiu de vergonha.",
            f"No mercado {theme}, entrei só pra comprar pão e saí com promoção de panela, tapete e um pepino que nem sei fazer.",
            f"No churrasco {theme}, o primo jurou que ia assar pouco. Três horas depois ele tava discutindo com a brasa como se fosse final de campeonato.",
            f"No futebol {theme}, o juiz era tão conhecido da galera que cada falta virava reunião de condomínio no meio do campo.",
            f"No culto jovem {theme}, falaram pra desligar o celular. Cinco minutos depois, até o pastor pediu o Wi-Fi pra passar o louvor novo.",
        ]
        out = []
        idx = 0
        while len(out) < count:
            out.append(base[idx % len(base)])
            idx += 1
        return out

    def _normalize_standup_joke(self, text: str) -> str:
        raw = " ".join(str(text or "").replace("\n", " ").split()).strip()
        if not raw:
            return ""
        raw = raw.replace("—", "-")
        raw = raw.replace("Piada:", "").replace("piada:", "")
        raw = raw.replace("Stand-up:", "").replace("stand-up:", "")
        raw = raw.strip(" -")
        raw = raw.lstrip("0123456789).:- ")
        if raw.lower().startswith("por que ") or raw.lower().startswith("porque "):
            raw = f"Vou te contar uma: {raw}"
        return raw[:420]

    def _build_standup_narration(self, joke_text: str, catchphrase: str = "") -> str:
        base = self._normalize_standup_joke(joke_text)
        if not base:
            return ""
        if catchphrase:
            phrase = self._normalize_standup_joke(catchphrase)
            if phrase and phrase.lower() not in base.lower():
                base = f"{phrase}. {base}"
        # Risos sempre no fechamento de cada bloco.
        end = base.rstrip()
        if not end.endswith((".", "!", "?")):
            end += "."
        return f"{end} Hahaha!"

    def _theme_list(self, value: Optional[str]) -> List[str]:
        raw = str(value or "").strip()
        if not raw:
            return []
        parts = [x.strip() for x in raw.split("|")]
        seen = set()
        out = []
        for p in parts:
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        return out

    def _extract_json_array(self, text: str) -> Optional[List[str]]:
        raw = (text or "").strip()
        if not raw:
            return None
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(clean)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
            if isinstance(data, dict) and isinstance(data.get("jokes"), list):
                return [str(x).strip() for x in data["jokes"] if str(x).strip()]
        except Exception:
            pass
        start = clean.find("[")
        end = clean.rfind("]")
        if start >= 0 and end > start:
            snippet = clean[start : end + 1]
            try:
                data = json.loads(snippet)
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                return None
        return None

    def _parse_json_list(self, raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
        return []

    def _generate_ai_jokes(self, theme: str, count: int, catchphrase: str = "", catchphrases: Optional[List[str]] = None) -> List[str]:
        catchphrase_line = ""
        if (catchphrase or "").strip():
            catchphrase_line = (
                f'- Bordão do personagem: "{catchphrase.strip()}". '
                "Use de forma natural em parte das piadas, sem repetir em todas."
            )
        elif catchphrases:
            pool = [x.strip() for x in (catchphrases or []) if x and x.strip()]
            if pool:
                sample = pool[:6]
                sample_txt = "; ".join(f"\"{x}\"" for x in sample)
                catchphrase_line = (
                    f"- Use ocasionalmente um destes bordões do personagem: {sample_txt}. "
                    "Intercale de forma natural sem repetir em todas."
                )
        prompt = f"""
Você é roteirista de humor limpo para YouTube.
Crie {count} blocos curtos e inéditos de stand-up sobre o tema: "{theme}".

Regras obrigatórias:
- Humor leve, sem baixaria, sem ofensa, sem palavrão.
- Linguagem natural em português do Brasil.
- Formato de conversa/história curta de palco (2 a 4 frases por bloco).
- NÃO usar formato de pergunta e resposta do tipo "por que ... porque ...".
- NÃO numerar, NÃO usar "Piada 1", "Piada 2", nem títulos.
- Variar contexto e desfecho para manter retenção.
{catchphrase_line}

Retorne APENAS JSON válido:
{{"jokes": ["bloco de stand-up 1", "bloco de stand-up 2", "..."]}}
"""
        try:
            text = self.ai._generate_text(prompt) or ""
            parsed = self._extract_json_array(text)
            if parsed:
                return [self._normalize_standup_joke(x) for x in parsed[:count] if self._normalize_standup_joke(x)]
        except Exception:
            pass
        return [self._normalize_standup_joke(x) for x in self._fallback_jokes(theme, count)]

    def _generate_ai_jokes_by_themes(self, themes: List[str], count: int, catchphrase: str = "", catchphrases: Optional[List[str]] = None) -> List[str]:
        clean = [t.strip() for t in (themes or []) if t and t.strip()]
        if not clean:
            return self._fallback_jokes("humor geral", count)
        if len(clean) == 1:
            return self._generate_ai_jokes(clean[0], count, catchphrase=catchphrase, catchphrases=catchphrases)

        per_theme = max(4, math.ceil(count / len(clean)))
        buckets: List[List[str]] = []
        for t in clean:
            jokes = self._generate_ai_jokes(t, per_theme, catchphrase=catchphrase, catchphrases=catchphrases)
            if not jokes:
                jokes = self._fallback_jokes(t, per_theme)
            buckets.append(jokes)

        # Intercala para aumentar variedade no vídeo.
        mixed: List[str] = []
        i = 0
        while len(mixed) < count:
            added = False
            for bucket in buckets:
                if i < len(bucket):
                    mixed.append(bucket[i])
                    added = True
                    if len(mixed) >= count:
                        break
            if not added:
                break
            i += 1

        if len(mixed) < count:
            extra = self._fallback_jokes(", ".join(clean), count - len(mixed))
            mixed.extend(extra)
        return mixed[:count]

    def _avatar_candidates(self, raw_path: str) -> List[str]:
        p = str(raw_path or "").strip()
        if not p:
            return []
        candidates = [p]
        if p.startswith("/static/"):
            from app.config import absolute_path_for_static

            abs_p = absolute_path_for_static(p)
            if abs_p:
                candidates.append(abs_p)
        if p.startswith("app/static/"):
            candidates.append(os.path.abspath(p))
        if not os.path.isabs(p):
            candidates.append(os.path.abspath(p))

        basename = os.path.basename(p)
        if basename:
            candidates.append(os.path.abspath(os.path.join("app/static/generated/humor_project_avatars", basename)))
            candidates.append(os.path.abspath(os.path.join("app/static/generated/humor_avatars", basename)))
        # Dedupe preservando ordem
        out = []
        seen = set()
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _resolve_avatar_path(
        self, channel: Optional[HumorChannel], video_gen: VideoGenerator, override_path: str = ""
    ) -> Tuple[Optional[str], str]:
        ov = (override_path or "").strip()
        if ov:
            for candidate in self._avatar_candidates(ov):
                if os.path.exists(candidate):
                    return candidate, "override"
            return None, "missing_override"

        if channel and channel.avatar_path:
            p = str(channel.avatar_path).strip()
            for candidate in self._avatar_candidates(p):
                if os.path.exists(candidate):
                    return candidate, "channel"

        fallback = video_gen._generate_fallback_background((1280, 720))
        if fallback and os.path.exists(fallback):
            return fallback, "fallback"
        return None, "none"

    def _create_applause_sfx(self, output_dir: str, duration_sec: float = 2.8, sample_rate: int = 22050) -> Optional[str]:
        """
        Gera um efeito simples de aplausos (ruído em rajadas) para fechamento.
        Evita depender de assets externos no servidor.
        """
        try:
            total = int(max(1.0, duration_sec) * sample_rate)
            t = np.linspace(0, duration_sec, total, endpoint=False)
            signal = np.zeros_like(t)
            # Rajadas curtas simulando palmas
            burst_every = 0.11
            n_bursts = max(1, int(duration_sec / burst_every))
            for i in range(n_bursts):
                center = int((i * burst_every + random.uniform(0.0, 0.05)) * sample_rate)
                if center >= total:
                    break
                span = int(0.035 * sample_rate)
                start = max(0, center - span // 2)
                end = min(total, center + span // 2)
                if end <= start:
                    continue
                burst = np.random.uniform(-1.0, 1.0, end - start) * np.hanning(end - start)
                signal[start:end] += burst * random.uniform(0.45, 0.9)

            # Camada ambiente baixa para soar menos "digital"
            signal += np.random.uniform(-1.0, 1.0, total) * 0.03
            signal = np.clip(signal, -1.0, 1.0)
            pcm = (signal * 32767).astype(np.int16)

            os.makedirs(output_dir, exist_ok=True)
            path = os.path.join(output_dir, f"applause_{uuid.uuid4().hex[:8]}.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm.tobytes())
            return path
        except Exception:
            return None

    def _mouth_variant(self, base_frame: np.ndarray, mouth_open: bool) -> np.ndarray:
        img = Image.fromarray(base_frame.copy())
        draw = ImageDraw.Draw(img, "RGBA")
        w, h = img.size
        cx = int(w * 0.5)
        cy = int(h * 0.77)
        if mouth_open:
            draw.ellipse((cx - 26, cy - 12, cx + 26, cy + 16), fill=(120, 15, 15, 190), outline=(30, 0, 0, 210), width=2)
        else:
            draw.line((cx - 24, cy + 2, cx + 24, cy + 2), fill=(40, 0, 0, 210), width=4)
        return np.array(img)

    def _clip_with_audio(self, clip, audio_clip):
        if hasattr(clip, "with_audio"):
            return clip.with_audio(audio_clip)
        return clip.set_audio(audio_clip)

    def _clip_with_fps(self, clip, fps: int):
        if hasattr(clip, "with_fps"):
            return clip.with_fps(fps)
        return clip.set_fps(fps)

    def _animated_clip(self, VideoClipClass, make_frame_fn, duration: float):
        # Compatibilidade MoviePy 1.x e 2.x
        try:
            return VideoClipClass(frame_function=make_frame_fn, duration=duration)
        except TypeError:
            pass
        try:
            return VideoClipClass(make_frame=make_frame_fn, duration=duration)
        except TypeError:
            pass
        try:
            return VideoClipClass(make_frame_fn, duration=duration)
        except TypeError:
            return VideoClipClass(make_frame_fn)

    def _write_videofile_compat(self, clip, output_path: str, **kwargs):
        """
        Compatibilidade de parâmetros do write_videofile entre MoviePy 1.x/2.x.
        Algumas builds rejeitam kwargs como `verbose`, `preset` ou `threads`.
        """
        attempts = [
            dict(kwargs),
            {k: v for k, v in kwargs.items() if k != "verbose"},
            {k: v for k, v in kwargs.items() if k not in {"verbose", "preset", "threads"}},
            {k: v for k, v in kwargs.items() if k not in {"verbose", "preset", "threads", "logger"}},
        ]
        last_exc = None
        for opts in attempts:
            try:
                return clip.write_videofile(output_path, **opts)
            except TypeError as exc:
                last_exc = exc
                if "unexpected keyword argument" not in str(exc):
                    raise
        if last_exc:
            raise last_exc

    def _build_title(self, project: HumorProject) -> str:
        if (project.title or "").strip():
            return project.title.strip()
        stamp = datetime.now().strftime("%d/%m")
        return f"Fábrica de Humor: {project.theme} - especial {stamp}"

    def generate_project_video(self, project_id: int):
        db = SessionLocal()
        project = None
        clips = []
        audio_clips = []
        temp_files = []
        temporary_avatar = None
        final_clip = None
        try:
            project = db.query(HumorProject).filter(HumorProject.id == project_id).first()
            if not project:
                return

            project.status = "generating"
            self._set_progress(db, project, 5, "Iniciando produção do vídeo de humor...")

            channel = None
            if project.channel_id:
                channel = db.query(HumorChannel).filter(HumorChannel.id == project.channel_id).first()
            if not channel:
                channel = db.query(HumorChannel).filter(HumorChannel.is_active == True).order_by(HumorChannel.id.desc()).first()  # noqa: E712

            target_minutes = max(10, int(project.target_minutes or 10))
            target_seconds = target_minutes * 60
            estimated_secs_per_joke = 18
            needed_jokes = max(18, math.ceil(target_seconds / estimated_secs_per_joke))
            selected_themes = self._theme_list(project.theme)
            themes_label = ", ".join(selected_themes) if selected_themes else "humor geral"
            opening_message = (project.opening_message or "").strip()
            catchphrase_message = (project.catchphrase_message or "").strip()
            catchphrase_gallery = self._parse_json_list(project.catchphrases_json)
            closing_message = (project.closing_message or "").strip()
            if not catchphrase_gallery and channel:
                catchphrase_gallery = self._parse_json_list(getattr(channel, "catchphrases_json", None))

            # Compatibilidade: bordão único legado entra na galeria.
            if catchphrase_message and catchphrase_message not in catchphrase_gallery:
                catchphrase_gallery.append(catchphrase_message)

            manual = self._parse_manual_jokes(project.manual_jokes_text)
            script_blocks = self._parse_manual_script_blocks(getattr(project, "manual_script_text", None))
            source = (project.joke_source or "ai").strip().lower()
            jokes = []
            if source == "script":
                jokes = script_blocks[:]
            elif source == "manual":
                jokes = manual[:]
            elif source == "mixed":
                jokes = manual[:]
                missing = max(0, needed_jokes - len(jokes))
                if missing > 0:
                    jokes.extend(
                        self._generate_ai_jokes_by_themes(
                            selected_themes,
                            missing,
                            catchphrase=catchphrase_message,
                            catchphrases=catchphrase_gallery,
                        )
                    )
            else:
                jokes = self._generate_ai_jokes_by_themes(
                    selected_themes,
                    needed_jokes,
                    catchphrase=catchphrase_message,
                    catchphrases=catchphrase_gallery,
                )

            if source != "script" and len(jokes) < needed_jokes:
                # Completa ciclando sem baixar qualidade do ritmo
                src = jokes[:] if jokes else self._fallback_jokes(themes_label, needed_jokes)
                idx = 0
                while len(jokes) < needed_jokes:
                    jokes.append(src[idx % len(src)])
                    idx += 1

            jokes = [self._normalize_standup_joke(x) for x in jokes]
            jokes = [x for x in jokes if x]
            if source == "script":
                jokes = script_blocks[:]
                jokes = [x for x in jokes if x]
            if source == "script" and not jokes:
                raise RuntimeError("Informe um roteiro para gerar o vídeo na Fábrica de Humor.")
            project.jokes_json = json.dumps(jokes, ensure_ascii=False)
            mode_label = "blocos de roteiro" if source == "script" else "blocos de stand-up"
            self._set_progress(db, project, 12, f"{len(jokes)} {mode_label} preparados (temas: {themes_label}).")

            video_gen = VideoGenerator(output_dir=VIDEO_OUTPUT_DIR, ai_service=self.ai)
            avatar_path, avatar_source = self._resolve_avatar_path(
                channel, video_gen, override_path=(project.avatar_override_path or "")
            )
            if avatar_source == "missing_override":
                raise RuntimeError("A imagem fixa anexada para este vídeo não foi encontrada. Reenvie o avatar do projeto.")
            if avatar_path and os.path.basename(avatar_path).startswith("fallback_local_"):
                temporary_avatar = avatar_path
            self._append_log(project, f"Avatar em uso: {avatar_source} ({avatar_path or 'sem arquivo'}).")
            try:
                self.ai._load_config()
            except Exception:
                pass
            tts_provider = "ElevenLabs" if getattr(self.ai, "elevenlabs_key", None) else (
                "OpenAI" if getattr(self.ai, "api_key", None) else "EdgeTTS/gTTS"
            )
            self._append_log(project, f"TTS prioritário detectado: {tts_provider}.")
            db.commit()

            try:
                from moviepy.editor import AudioFileClip, VideoClip, concatenate_videoclips
            except Exception:
                from moviepy import AudioFileClip, VideoClip, concatenate_videoclips

            total_scenes = len(jokes) + 3  # abertura + fechamento + aplausos
            scene_counter = 0
            used_catchphrases = set()
            clean_pool = [self._normalize_standup_joke(x) for x in catchphrase_gallery]
            clean_pool = [x for x in clean_pool if x]

            def pick_catchphrase(force: bool = False) -> str:
                if not clean_pool:
                    return ""
                available = [x for x in clean_pool if x.lower() not in used_catchphrases]
                source = available if available else clean_pool
                pick = random.choice(source)
                if force or available:
                    used_catchphrases.add(pick.lower())
                return pick

            approx_scene_seconds = max(10, int(target_seconds / max(1, len(jokes))))
            catchphrase_every_jokes = max(1, int(round(180 / max(10, approx_scene_seconds))))
            if source != "script":
                self._append_log(project, f"Bordão intermediário configurado para ~1x a cada {catchphrase_every_jokes} blocos.")
            db.commit()

            def add_audio_scene(text_on_screen: str, audio_path: str, label: str, animate_mouth: bool = True) -> bool:
                nonlocal scene_counter
                if not audio_path or not os.path.exists(audio_path):
                    self._append_log(project, f"Aviso: sem áudio para {label}, pulando.")
                    db.commit()
                    return False

                audio_clip = AudioFileClip(audio_path)
                audio_clips.append(audio_clip)
                duration = max(2.0, float(audio_clip.duration or 0) + 0.2)

                base_frame = video_gen.create_text_image(
                    text=(text_on_screen or "")[:280],
                    size=(1280, 720),
                    bg_color=(35, 45, 70),
                    text_color=(248, 248, 248),
                    bg_image_path=avatar_path,
                )
                frame_open = self._mouth_variant(base_frame, mouth_open=True)
                frame_closed = self._mouth_variant(base_frame, mouth_open=False)

                def make_frame(t, fo=frame_open, fc=frame_closed, mouth=animate_mouth):
                    if not mouth:
                        return fc
                    return fo if int(t * 5.4) % 2 == 0 else fc

                clip = self._animated_clip(VideoClip, make_frame, duration)
                clip = self._clip_with_audio(clip, audio_clip)
                clip = self._clip_with_fps(clip, 24)
                clips.append(clip)

                scene_counter += 1
                pct = 12 + int((scene_counter / max(1, total_scenes)) * 70)
                self._set_progress(db, project, pct, f"Gerando cena {scene_counter}/{total_scenes}: {label}")
                return True

            def add_talking_scene(text_on_screen: str, narration_text: str, label: str) -> bool:
                audio_path = video_gen.generate_audio(
                    narration_text,
                    voice_style="human",
                    voice_gender=(channel.default_voice_gender if channel else "male"),
                )
                temp_files.append(audio_path)
                return add_audio_scene(text_on_screen=text_on_screen, audio_path=audio_path, label=label, animate_mouth=True)

            intro_text = self._normalize_standup_joke(opening_message) or "Boa noite, minha gente! Chega mais, porque hoje tem história boa."
            opening_catchphrase = pick_catchphrase(force=True) or self._normalize_standup_joke(catchphrase_message)
            intro_narration = intro_text
            if opening_catchphrase and opening_catchphrase.lower() not in intro_narration.lower():
                intro_narration = f"{opening_catchphrase}. {intro_narration}"
            add_talking_scene(
                text_on_screen=f"Abertura\n{intro_text}",
                narration_text=intro_narration,
                label="abertura",
            )

            for idx, joke in enumerate(jokes, start=1):
                if source == "script":
                    narration = joke.strip()
                    text_on_screen = narration[:280]
                    add_talking_scene(
                        text_on_screen=text_on_screen,
                        narration_text=narration,
                        label=f"roteiro {idx}",
                    )
                else:
                    mid_catchphrase = ""
                    if idx % catchphrase_every_jokes == 0:
                        mid_catchphrase = pick_catchphrase()
                    narration = self._build_standup_narration(joke, catchphrase=mid_catchphrase)
                    add_talking_scene(
                        text_on_screen=joke,
                        narration_text=narration,
                        label=f"stand-up {idx}",
                    )

            closing_text = self._normalize_standup_joke(closing_message) or "Valeu demais pela companhia, vocês são incríveis."
            closing_catchphrase = pick_catchphrase(force=True) or opening_catchphrase
            outro_narration = closing_text
            if closing_catchphrase and closing_catchphrase.lower() not in outro_narration.lower():
                outro_narration = f"{outro_narration} {closing_catchphrase}."
            add_talking_scene(
                text_on_screen=f"Encerramento\n{closing_text}",
                narration_text=outro_narration,
                label="fechamento",
            )

            applause_path = self._create_applause_sfx(VIDEO_OUTPUT_DIR)
            if applause_path and os.path.exists(applause_path):
                temp_files.append(applause_path)
                add_audio_scene(
                    text_on_screen="Aplausos da plateia",
                    audio_path=applause_path,
                    label="aplausos finais",
                    animate_mouth=False,
                )
            else:
                add_talking_scene(
                    text_on_screen="Aplausos da plateia",
                    narration_text="Muito obrigado, minha gente! Aplausos da plateia!",
                    label="aplausos finais",
                )

            if not clips:
                raise RuntimeError("Não foi possível gerar áudio/cenas para as piadas.")

            final_clip = concatenate_videoclips(clips, method="compose")
            os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
            filename = f"humor_project_{project.id}_{uuid.uuid4().hex[:8]}.mp4"
            output_path = os.path.join(VIDEO_OUTPUT_DIR, filename)
            self._set_progress(db, project, 90, "Renderizando vídeo final de humor...")
            self._write_videofile_compat(
                final_clip,
                output_path,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                threads=4,
                preset="ultrafast",
                verbose=False,
                logger=None,
            )

            scheduled = ScheduledVideo(
                theme=project.theme,
                title=self._build_title(project),
                description=(
                    f"Projeto Fábrica de Humor - temas: {themes_label}. "
                    "Conteúdo limpo e familiar, sem baixaria."
                    + (f" Bordão: {catchphrase_message}" if catchphrase_message else "")
                ),
                scheduled_for=datetime.now(),
                status="completed",
                video_type="video",
                script_data=json.dumps(
                    {
                        "source": "humor_factory",
                        "humor_project_id": project.id,
                        "joke_source": source,
                        "opening_message": opening_message,
                        "catchphrase_message": catchphrase_message,
                        "catchphrases": catchphrase_gallery,
                        "closing_message": closing_message,
                        "jokes": jokes,
                        "manual_script_text": getattr(project, "manual_script_text", None),
                    },
                    ensure_ascii=False,
                ),
                video_url=output_path,
                progress=100,
                auto_post=False,
                voice_style="human",
                voice_gender=(channel.default_voice_gender if channel else "male"),
            )
            db.add(scheduled)
            db.commit()
            db.refresh(scheduled)

            project.video_path = output_path
            project.scheduled_video_id = scheduled.id
            project.status = "review"
            self._set_progress(db, project, 100, "Vídeo pronto para revisão na Fábrica de Humor.")

        except Exception as e:
            if project:
                project.status = "failed"
                project.status_message = f"Falha na geração: {str(e)[:220]}"
                self._append_log(project, f"ERRO: {e}")
                self._append_log(project, traceback.format_exc()[-2500:])
                db.commit()
        finally:
            if final_clip is not None:
                try:
                    final_clip.close()
                except Exception:
                    pass
            for c in clips:
                try:
                    c.close()
                except Exception:
                    pass
            for a in audio_clips:
                try:
                    a.close()
                except Exception:
                    pass
            for p in temp_files:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            if temporary_avatar and os.path.exists(temporary_avatar):
                try:
                    os.remove(temporary_avatar)
                except Exception:
                    pass
            db.close()
            gc.collect()

    def publish_project(self, project_id: int):
        db = SessionLocal()
        try:
            project = db.query(HumorProject).filter(HumorProject.id == project_id).first()
            if not project:
                raise ValueError("Projeto de humor não encontrado.")
            if not project.scheduled_video_id:
                raise ValueError("Projeto sem vídeo em revisão.")

            scheduled = db.query(ScheduledVideo).filter(ScheduledVideo.id == project.scheduled_video_id).first()
            if not scheduled:
                raise ValueError("Item de revisão não encontrado.")

            path = (scheduled.video_url or project.video_path or "").strip()
            if not path:
                raise ValueError("Arquivo de vídeo não definido.")
            if not os.path.exists(path):
                from app.config import absolute_path_for_video

                path = absolute_path_for_video(path)
            if not path or not os.path.exists(path):
                raise ValueError("Arquivo de vídeo não encontrado no servidor.")

            project.status = "publishing"
            project.status_message = "Publicando no YouTube..."
            self._append_log(project, "Iniciando upload para o YouTube.")
            db.commit()

            yt = YouTubeService()
            upload_result = yt.upload_video(
                file_path=path,
                title=scheduled.title or self._build_title(project),
                description=scheduled.description or "Vídeo gerado pela Fábrica de Humor Codexia.",
                tags=["humor", "piadas", "humor limpo", "familia"],
            )

            is_error = False
            youtube_id = None
            if isinstance(upload_result, dict):
                if upload_result.get("error"):
                    is_error = True
                else:
                    youtube_id = upload_result.get("id")
                    if not youtube_id:
                        is_error = True
            else:
                youtube_id = str(upload_result or "").strip() or None
                if not youtube_id:
                    is_error = True

            if is_error or not youtube_id:
                msg = (upload_result.get("error") if isinstance(upload_result, dict) else str(upload_result)) or "Falha ao publicar no YouTube."
                project.status = "review"
                project.status_message = f"Falha ao publicar: {msg[:180]}"
                self._append_log(project, f"Falha ao publicar: {msg}")
                db.commit()
                raise ValueError(msg)

            scheduled.youtube_video_id = youtube_id
            scheduled.uploaded_at = datetime.now()
            scheduled.status = "published"
            project.youtube_video_id = youtube_id
            project.status = "published"
            project.status_message = "Publicado com sucesso no YouTube."
            self._append_log(project, f"Publicado com sucesso. YouTube ID: {youtube_id}")
            db.commit()
            return {"status": "published", "youtube_video_id": youtube_id}
        finally:
            db.close()
