import gc
import json
import math
import os
import traceback
import uuid
from datetime import datetime
from typing import List, Optional

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

    def _fallback_jokes(self, theme: str, count: int) -> List[str]:
        base = [
            f"No tema {theme}, o português falou: 'vou só ali'. Voltou com pão, café e fofoca da rua inteira.",
            f"No culto {theme}, o irmão disse 'é rapidinho' e pregou até o relógio pedir oração.",
            f"Na igreja {theme}, pediram silêncio; até o bebê olhou pra mãe e cochichou 'amém'.",
            f"No ônibus do bairro {theme}, o cobrador perguntou se era cartão; a pessoa respondeu 'só tenho fé'.",
            f"No almoço {theme}, falaram 'come pouco'; quando viram, o prato já tava no segundo testemunho.",
            f"No grupo da família {theme}, mandaram bom dia às 5h. Às 7h já tinha receita, notícia e piada.",
            f"No trabalho {theme}, perguntaram se tava pronto. Resposta: 'tá quase, só faltam as partes importantes'.",
            f"No mercado {theme}, a lista era curta. Saiu com sacola cheia e uma promoção que nem precisava.",
            f"No futebol de bairro {theme}, o juiz era primo de todo mundo. Impedimento virou opinião.",
            f"No churrasco {theme}, disseram 'sem exagero'. A farofa já chegou com status de patrimônio.",
        ]
        out = []
        idx = 0
        while len(out) < count:
            out.append(base[idx % len(base)])
            idx += 1
        return out

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

    def _generate_ai_jokes(self, theme: str, count: int, catchphrase: str = "") -> List[str]:
        catchphrase_line = ""
        if (catchphrase or "").strip():
            catchphrase_line = (
                f'- Bordão do personagem: "{catchphrase.strip()}". '
                "Use de forma natural em parte das piadas, sem repetir em todas."
            )
        prompt = f"""
Você é roteirista de humor limpo para YouTube.
Crie {count} piadas curtas e inéditas sobre o tema: "{theme}".

Regras obrigatórias:
- Humor leve, sem baixaria, sem ofensa, sem palavrão.
- Linguagem natural em português do Brasil.
- Cada piada em 1 ou 2 frases curtas.
- Variar o gancho para manter retenção.
- Evitar repetição de estrutura.
{catchphrase_line}

Retorne APENAS JSON válido:
{{"jokes": ["piada 1", "piada 2", "..."]}}
"""
        try:
            text = self.ai._generate_text(prompt) or ""
            parsed = self._extract_json_array(text)
            if parsed:
                return parsed[:count]
        except Exception:
            pass
        return self._fallback_jokes(theme, count)

    def _generate_ai_jokes_by_themes(self, themes: List[str], count: int, catchphrase: str = "") -> List[str]:
        clean = [t.strip() for t in (themes or []) if t and t.strip()]
        if not clean:
            return self._fallback_jokes("humor geral", count)
        if len(clean) == 1:
            return self._generate_ai_jokes(clean[0], count, catchphrase=catchphrase)

        per_theme = max(4, math.ceil(count / len(clean)))
        buckets: List[List[str]] = []
        for t in clean:
            jokes = self._generate_ai_jokes(t, per_theme, catchphrase=catchphrase)
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

    def _resolve_avatar_path(self, channel: Optional[HumorChannel], video_gen: VideoGenerator, override_path: str = "") -> Optional[str]:
        ov = (override_path or "").strip()
        if ov:
            if os.path.exists(ov):
                return ov
            if ov.startswith("/static/"):
                from app.config import absolute_path_for_static

                abs_p = absolute_path_for_static(ov)
                if abs_p and os.path.exists(abs_p):
                    return abs_p
        if channel and channel.avatar_path:
            p = str(channel.avatar_path).strip()
            if p and os.path.exists(p):
                return p
            if p.startswith("/static/"):
                from app.config import absolute_path_for_static

                abs_p = absolute_path_for_static(p)
                if abs_p and os.path.exists(abs_p):
                    return abs_p
        fallback = video_gen._generate_fallback_background((1280, 720))
        return fallback if fallback and os.path.exists(fallback) else None

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
            estimated_secs_per_joke = 12
            needed_jokes = max(20, math.ceil(target_seconds / estimated_secs_per_joke))
            selected_themes = self._theme_list(project.theme)
            themes_label = ", ".join(selected_themes) if selected_themes else "humor geral"
            opening_message = (project.opening_message or "").strip()
            catchphrase_message = (project.catchphrase_message or "").strip()
            closing_message = (project.closing_message or "").strip()

            manual = self._parse_manual_jokes(project.manual_jokes_text)
            source = (project.joke_source or "ai").strip().lower()
            jokes = []
            if source == "manual":
                jokes = manual[:]
            elif source == "mixed":
                jokes = manual[:]
                missing = max(0, needed_jokes - len(jokes))
                if missing > 0:
                    jokes.extend(self._generate_ai_jokes_by_themes(selected_themes, missing, catchphrase=catchphrase_message))
            else:
                jokes = self._generate_ai_jokes_by_themes(selected_themes, needed_jokes, catchphrase=catchphrase_message)

            if len(jokes) < needed_jokes:
                # Completa ciclando sem baixar qualidade do ritmo
                src = jokes[:] if jokes else self._fallback_jokes(themes_label, needed_jokes)
                idx = 0
                while len(jokes) < needed_jokes:
                    jokes.append(src[idx % len(src)])
                    idx += 1

            project.jokes_json = json.dumps(jokes, ensure_ascii=False)
            self._set_progress(db, project, 12, f"{len(jokes)} piadas preparadas (temas: {themes_label}).")

            video_gen = VideoGenerator(output_dir=VIDEO_OUTPUT_DIR, ai_service=self.ai)
            avatar_path = self._resolve_avatar_path(channel, video_gen, override_path=(project.avatar_override_path or ""))
            if avatar_path and os.path.basename(avatar_path).startswith("fallback_local_"):
                temporary_avatar = avatar_path

            try:
                from moviepy.editor import AudioFileClip, VideoClip, concatenate_videoclips
            except Exception:
                from moviepy import AudioFileClip, VideoClip, concatenate_videoclips

            total_scenes = len(jokes) + (1 if opening_message else 0) + (1 if closing_message else 0)
            scene_counter = 0

            def add_talking_scene(text_on_screen: str, narration_text: str, label: str) -> bool:
                nonlocal scene_counter
                audio_path = video_gen.generate_audio(
                    narration_text,
                    voice_style="human",
                    voice_gender=(channel.default_voice_gender if channel else "male"),
                )
                if not audio_path or not os.path.exists(audio_path):
                    self._append_log(project, f"Aviso: sem áudio para {label}, pulando.")
                    db.commit()
                    return False

                temp_files.append(audio_path)
                audio_clip = AudioFileClip(audio_path)
                audio_clips.append(audio_clip)
                duration = max(4.0, float(audio_clip.duration or 0) + 0.35)

                base_frame = video_gen.create_text_image(
                    text=(text_on_screen or "")[:280],
                    size=(1280, 720),
                    bg_color=(35, 45, 70),
                    text_color=(248, 248, 248),
                    bg_image_path=avatar_path,
                )
                frame_open = self._mouth_variant(base_frame, mouth_open=True)
                frame_closed = self._mouth_variant(base_frame, mouth_open=False)

                def make_frame(t, fo=frame_open, fc=frame_closed):
                    return fo if int(t * 5.4) % 2 == 0 else fc

                clip = self._animated_clip(VideoClip, make_frame, duration)
                clip = self._clip_with_audio(clip, audio_clip)
                clip = self._clip_with_fps(clip, 24)
                clips.append(clip)

                scene_counter += 1
                pct = 12 + int((scene_counter / max(1, total_scenes)) * 70)
                self._set_progress(db, project, pct, f"Gerando cena {scene_counter}/{total_scenes}: {label}")
                return True

            if opening_message:
                intro_narration = opening_message
                if catchphrase_message and catchphrase_message.lower() not in intro_narration.lower():
                    intro_narration = f"{intro_narration} {catchphrase_message}"
                add_talking_scene(
                    text_on_screen=f"Abertura\n{opening_message}",
                    narration_text=intro_narration,
                    label="abertura",
                )

            for idx, joke in enumerate(jokes, start=1):
                narration = f"Piada {idx}. {joke} ... e já vem a próxima."
                if catchphrase_message and idx % 3 == 1:
                    narration = f"{catchphrase_message} {narration}"
                add_talking_scene(
                    text_on_screen=f"Piada {idx}/{len(jokes)}\n{joke}",
                    narration_text=narration,
                    label=f"piada {idx}",
                )

            if closing_message:
                outro_narration = closing_message
                if catchphrase_message and catchphrase_message.lower() not in outro_narration.lower():
                    outro_narration = f"{outro_narration} {catchphrase_message}"
                add_talking_scene(
                    text_on_screen=f"Encerramento\n{closing_message}",
                    narration_text=outro_narration,
                    label="fechamento",
                )

            if not clips:
                raise RuntimeError("Não foi possível gerar áudio/cenas para as piadas.")

            final_clip = concatenate_videoclips(clips, method="compose")
            os.makedirs(VIDEO_OUTPUT_DIR, exist_ok=True)
            filename = f"humor_project_{project.id}_{uuid.uuid4().hex[:8]}.mp4"
            output_path = os.path.join(VIDEO_OUTPUT_DIR, filename)
            self._set_progress(db, project, 90, "Renderizando vídeo final de humor...")
            final_clip.write_videofile(
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
                        "opening_message": opening_message,
                        "catchphrase_message": catchphrase_message,
                        "closing_message": closing_message,
                        "jokes": jokes,
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
