"""
Rotas para gerar música a partir de letra e clipe (vídeo) da música.
Com Suno API: música com voz cantada. Sem Suno: instrumental (MusicGen).
"""
import os
import uuid
import threading
import requests
import json
import re
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Dict, List, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db, SessionLocal
from app.services.suno_service import (
    get_suno_api_key,
    create_suno_task,
    poll_suno_task,
    download_suno_audio,
)
from app.services.ai_generator import AIContentGenerator
from app.routers.auth import get_current_user
from app.models import User, VideoTask, SavedMusic, SavedMusicShort, SystemNotification
from app.services.task_manager import create_task, update_task, get_task, request_cancel_task, is_task_cancel_requested, mark_task_deleted
from app.config import MUSIC_OUTPUT_DIR, MUSIC_URL_PREFIX, absolute_path_for_music, absolute_path_for_video, VIDEO_OUTPUT_DIR, VIDEO_URL_PREFIX, STATIC_DIR

router = APIRouter(prefix="/music", tags=["music"])


class GenerateMusicRequest(BaseModel):
    lyrics: str
    title: str = "Música"
    genre: str = ""
    vocal_gender: Optional[str] = None  # "m" ou "f" para Suno


class GenerateClipRequest(BaseModel):
    lyrics: Optional[str] = None
    title: str = "Música"
    music_filename: Optional[str] = None
    author_text: Optional[str] = None
    watermark_enabled: Optional[bool] = True
    sync_mode: Optional[str] = "auto"
    captions_enabled: Optional[bool] = True
    aspect_ratio: Optional[str] = "9:16"  # "9:16" (vertical) ou "16:9" (YouTube)
    auto_upload_youtube: Optional[bool] = False
    images_count: Optional[int] = None
    visual_style: Optional[str] = None
    spiritual_intensity: Optional[str] = None
    prompt_language: Optional[str] = None
    mode: Optional[str] = None
    model: Optional[str] = None


class GenerateLyricsRequest(BaseModel):
    theme: str
    message: str
    language: str = "pt-BR"
    style: str = ""
    genre: str = ""

class ImproveLyricsRequest(BaseModel):
    lyrics: str
    instruction: str
    language: str = "pt-BR"
    style: str = ""
    genre: str = ""

class GenerateLyricsImagesRequest(BaseModel):
    lyrics: str
    title: str = ""
    images_count: int = 8
    visual_style: str = "cinematic"
    spiritual_intensity: str = "epic"
    prompt_language: str = "auto"
    mode: str = "epic"
    size: str = "1024x1024"
    model: str = ""
    quality: str = "standard"


class SaveMusicRequest(BaseModel):
    title: str = "Música"
    lyrics: Optional[str] = None
    genre: Optional[str] = None
    vocal_gender: Optional[str] = None
    with_vocals: Optional[bool] = None
    music_url: Optional[str] = None
    music_filename: Optional[str] = None
    clip_url: Optional[str] = None
    clip_filename: Optional[str] = None


class PublishSavedClipRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None  # vírgula-separado


class GenerateSavedMusicShortsRequest(BaseModel):
    count: int = 1
    target_seconds: Optional[int] = 45


def _parse_bool(v) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def _sanitize_title(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return "Música"
    t = re.sub(r"\s*[-–—|:]\s*$", "", t).strip()
    t = re.sub(r"(\s*[-–—|:]?\s*E\.?MA\.?\s*)$", "", t, flags=re.IGNORECASE).strip()
    return t or "Música"


@router.post("/lyrics")
def generate_lyrics(request: GenerateLyricsRequest, user: User = Depends(get_current_user)):
    if not request.theme or not request.theme.strip():
        raise HTTPException(status_code=400, detail="Informe o tema.")
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Informe a mensagem.")
    try:
        ai = AIContentGenerator()
        result = ai.generate_song_lyrics(
            theme=request.theme.strip(),
            message=request.message.strip(),
            language=(request.language or "pt-BR").strip(),
            style=(request.style or "").strip(),
            genre=(request.genre or "").strip(),
        )
        if not result or not isinstance(result, dict) or not result.get("lyrics"):
            raise HTTPException(status_code=503, detail="Não foi possível gerar a letra agora.")
        return {
            "title": result.get("title") or "Música",
            "lyrics": result.get("lyrics"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar letra: {str(e)}")

@router.post("/lyrics/improve")
def improve_lyrics(request: ImproveLyricsRequest, user: User = Depends(get_current_user)):
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Informe a letra atual.")
    if not request.instruction or not request.instruction.strip():
        raise HTTPException(status_code=400, detail="Informe o que você quer melhorar.")
    try:
        ai = AIContentGenerator()
        result = ai.improve_song_lyrics(
            lyrics=request.lyrics.strip(),
            instruction=request.instruction.strip(),
            language=(request.language or "pt-BR").strip(),
            style=(request.style or "").strip(),
            genre=(request.genre or "").strip(),
        )
        improved = (result or {}).get("lyrics") if isinstance(result, dict) else None
        improved = (improved or "").strip()
        if not improved:
            raise HTTPException(status_code=503, detail="Não foi possível melhorar a letra agora.")
        return {"lyrics": improved}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao melhorar letra: {str(e)}")

@router.post("/lyrics/images/openai")
def generate_images_from_lyrics_openai(request: GenerateLyricsImagesRequest, user: User = Depends(get_current_user)):
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Informe a letra.")
    try:
        ai = AIContentGenerator()
        ai._load_config()
        if not (getattr(ai, "api_key", "") or "").strip():
            raise HTTPException(status_code=400, detail="Configure a OpenAI API Key em Configurações.")
        from app.services.openai_image_module import OpenAIImageModule

        mod = OpenAIImageModule(ai_service=ai)
        result = mod.generate_images_from_lyrics(
            request.lyrics.strip(),
            options={
                "images_count": max(1, min(40, int(request.images_count or 1))),
                "visual_style": (request.visual_style or "cinematic").strip(),
                "spiritual_intensity": (request.spiritual_intensity or "epic").strip(),
                "prompt_language": (request.prompt_language or "auto").strip(),
                "mode": (request.mode or "epic").strip(),
                "size": (request.size or "1024x1024").strip(),
                "model": (request.model or "").strip(),
                "quality": (request.quality or "standard").strip(),
            },
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar imagens: {str(e)}")


@router.post("/generate")
def generate_music_from_lyrics(request: GenerateMusicRequest, user: User = Depends(get_current_user)):
    """
    Gera música a partir da letra. Se Suno API Key estiver em Configurações: música com voz cantada.
    Senão: instrumental (MusicGen / Hugging Face).
    """
    if not request.lyrics or not request.lyrics.strip():
        raise HTTPException(status_code=400, detail="Envie a letra da música.")
    try:
        api_key = get_suno_api_key()
        if api_key:
            task_id = create_task(user_id=user.id)
            update_task(
                task_id,
                status="processing",
                progress=5,
                message="Enviando para o Suno...",
                result={"provider": "suno", "title": request.title or "Música"},
            )

            lyrics = request.lyrics.strip()
            title = request.title or "Música"
            style = request.genre or "Pop"
            vocal_gender = request.vocal_gender

            created = create_suno_task(
                api_key=api_key,
                lyrics=lyrics,
                title=title,
                style=style,
                vocal_gender=vocal_gender,
            )
            if not created.get("success"):
                update_task(task_id, status="failed", progress=100, message=created.get("error") or "Suno falhou.")
                raise HTTPException(status_code=503, detail=created.get("error", "Suno falhou."))
            suno_task_id = str(created.get("task_id") or "")
            update_task(
                task_id,
                status="processing",
                progress=12,
                message="Suno processando...",
                result={"provider": "suno", "suno_task_id": suno_task_id, "title": title},
            )

            def _run():
                try:
                    update_task(task_id, status="processing", progress=20, message="Aguardando áudio do Suno...")
                    polled = poll_suno_task(api_key=api_key, task_id=suno_task_id, max_wait_seconds=12 * 60, step_seconds=6)
                    if not polled.get("success"):
                        update_task(task_id, status="failed", progress=100, message=polled.get("error") or "Suno falhou.")
                        return
                    audio_url = str(polled.get("audio_url") or "")
                    if not audio_url:
                        update_task(task_id, status="failed", progress=100, message="Suno não retornou URL do áudio.")
                        return
                    update_task(task_id, status="processing", progress=85, message="Baixando áudio...")
                    dl = download_suno_audio(audio_url)
                    if not dl.get("success"):
                        update_task(task_id, status="failed", progress=100, message=dl.get("error") or "Falha ao baixar áudio.")
                        return
                    update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        message="Música com voz cantada gerada (Suno).",
                        result={
                            "provider": "suno",
                            "suno_task_id": suno_task_id,
                            "audio_url": audio_url,
                            "music_url": dl.get("music_url"),
                            "music_filename": dl.get("music_filename"),
                            "with_vocals": True,
                            "title": title,
                        },
                    )
                except Exception as e:
                    update_task(task_id, status="failed", progress=100, message=str(e))

            threading.Thread(target=_run, daemon=True).start()
            return {
                "task_id": task_id,
                "message": "Geração iniciada (Suno). Aguarde a finalização.",
                "with_vocals": True,
            }

        # 2. Fallback: instrumental (MusicGen)
        ai = AIContentGenerator()
        music_prompt = ai.lyrics_to_music_prompt(request.lyrics, request.title, request.genre)
        raw_audio = ai.generate_music(music_prompt)
        if not raw_audio:
            raise HTTPException(
                status_code=503,
                detail="Não foi possível gerar a música. Configure a Suno API Key em Configurações para voz cantada, ou o token Hugging Face para instrumental."
            )
        music_dir = MUSIC_OUTPUT_DIR
        os.makedirs(music_dir, exist_ok=True)
        filename = f"song_{uuid.uuid4().hex[:10]}.wav"
        path = os.path.join(music_dir, filename)
        with open(path, "wb") as f:
            f.write(raw_audio)
        return {
            "music_url": f"{MUSIC_URL_PREFIX}/{filename}",
            "music_filename": filename,
            "message": "Música instrumental gerada. Para voz cantada, configure a Suno API Key em Configurações.",
            "with_vocals": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar música: {str(e)}")

@router.get("/task/{task_id}")
def get_music_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para acessar esta tarefa.")
    data = get_task(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    return data


@router.post("/task/{task_id}/cancel")
def cancel_music_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para cancelar esta tarefa.")
    cur = request_cancel_task(task_id, message="Cancelado pelo usuário.")
    if cur:
        return cur
    return {"task_id": task_id, "status": "cancelled", "progress": int(row.progress or 0), "message": "Cancelado pelo usuário.", "result": None}


@router.delete("/task/{task_id}")
def delete_music_task(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para excluir esta tarefa.")

    payload = None
    if row.result_json:
        try:
            payload = json.loads(row.result_json)
        except Exception:
            payload = None

    def _extract_video_url(obj):
        if not obj:
            return None
        if isinstance(obj, dict):
            for k in ("video_url", "clip_url", "videoUrl", "clipUrl"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    video_url = _extract_video_url(payload)
    if not video_url:
        video_url = _extract_video_url(get_task(task_id) or {})

    deleted_file = False
    deleted_path = None
    filename = None

    if video_url and isinstance(video_url, str):
        clean = video_url.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
        filename = os.path.basename(clean) if clean else None
        try:
            abs_path = absolute_path_for_video(clean)
        except Exception:
            abs_path = ""

        def _is_allowed_path(p: str) -> bool:
            if not p or not isinstance(p, str):
                return False
            try:
                ap = os.path.normcase(os.path.abspath(p))
                roots = []
                for r in [VIDEO_OUTPUT_DIR, str(STATIC_DIR / "videos"), os.path.join("/data", "media", "videos")]:
                    if not r:
                        continue
                    roots.append(os.path.normcase(os.path.abspath(str(r))))
                return any(ap.startswith(rt) for rt in roots)
            except Exception:
                return False

        if abs_path and os.path.exists(abs_path) and _is_allowed_path(abs_path):
            try:
                os.remove(abs_path)
                deleted_file = True
                deleted_path = abs_path
            except Exception:
                deleted_file = False

    if deleted_file and filename:
        try:
            items = db.query(SavedMusic).filter(
                SavedMusic.user_id == user.id,
                or_(
                    SavedMusic.clip_filename == filename,
                    SavedMusic.clip_url.like(f"%{filename}%"),
                ),
            ).all()
            for it in items:
                it.clip_url = None
                it.clip_filename = None
            db.commit()
        except Exception:
            db.rollback()

    try:
        mark_task_deleted(task_id)
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Falha ao excluir tarefa.")

    return {
        "deleted_task": True,
        "deleted_file": deleted_file,
        "deleted_path": deleted_path,
        "filename": filename,
    }


@router.post("/saved")
def save_music_item(request: SaveMusicRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    has_any = bool((request.lyrics and request.lyrics.strip()) or (request.music_url and request.music_url.strip()) or (request.clip_url and request.clip_url.strip()))
    if not has_any:
        raise HTTPException(status_code=400, detail="Informe ao menos a letra, a música ou o clipe para salvar.")
    item = SavedMusic(
        user_id=user.id,
        title=(request.title or "Música")[:200],
        lyrics=(request.lyrics.strip() if request.lyrics and request.lyrics.strip() else None),
        genre=(request.genre.strip() if request.genre and request.genre.strip() else None),
        vocal_gender=(request.vocal_gender.strip() if request.vocal_gender and request.vocal_gender.strip() else None),
        with_vocals=bool(request.with_vocals) if request.with_vocals is not None else False,
        music_url=(request.music_url.strip() if request.music_url and request.music_url.strip() else None),
        music_filename=(request.music_filename.strip() if request.music_filename and request.music_filename.strip() else None),
        clip_url=(request.clip_url.strip() if request.clip_url and request.clip_url.strip() else None),
        clip_filename=(request.clip_filename.strip() if request.clip_filename and request.clip_filename.strip() else None),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "title": item.title,
        "lyrics": item.lyrics,
        "genre": item.genre,
        "vocal_gender": item.vocal_gender,
        "with_vocals": item.with_vocals,
        "music_url": item.music_url,
        "music_filename": item.music_filename,
        "clip_url": item.clip_url,
        "clip_filename": item.clip_filename,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.post("/import")
async def import_music(
    title: str = Form("Música"),
    source_url: Optional[str] = Form(None),
    lyrics: Optional[str] = Form(None),
    genre: Optional[str] = Form(None),
    vocal_gender: Optional[str] = Form(None),
    with_vocals: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    src = (source_url or "").strip()
    if not file and not src:
        raise HTTPException(status_code=400, detail="Envie um arquivo de áudio ou informe uma URL.")

    allowed_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    max_bytes = 200 * 1024 * 1024
    min_bytes = 50 * 1024

    os.makedirs(MUSIC_OUTPUT_DIR, exist_ok=True)

    ext = ""
    original_name = ""
    if file and file.filename:
        original_name = str(file.filename)
        ext = os.path.splitext(original_name)[1].lower()
    if not ext and src:
        try:
            clean = src.split("?", 1)[0].split("#", 1)[0]
            ext = os.path.splitext(clean)[1].lower()
        except Exception:
            ext = ""
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Formato inválido. Envie mp3, wav, m4a, aac ou ogg.")

    filename = f"import_{uuid.uuid4().hex[:12]}{ext}"
    path = os.path.join(MUSIC_OUTPUT_DIR, filename)
    total = 0

    try:
        if file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=400, detail="Arquivo muito grande (limite 200MB).")
                with open(path, "ab") as f:
                    f.write(chunk)
        else:
            if not (src.startswith("http://") or src.startswith("https://")):
                raise HTTPException(status_code=400, detail="URL inválida.")
            r = requests.get(src, stream=True, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Falha ao baixar a URL (HTTP {r.status_code}).")
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=400, detail="Arquivo muito grande (limite 200MB).")
                    f.write(chunk)

        if total < min_bytes:
            raise HTTPException(status_code=400, detail="Arquivo muito pequeno; verifique se o áudio é válido.")

        item = SavedMusic(
            user_id=user.id,
            title=(title or "Música")[:200],
            lyrics=(lyrics.strip() if lyrics and lyrics.strip() else None),
            genre=(genre.strip() if genre and genre.strip() else None),
            vocal_gender=(vocal_gender.strip() if vocal_gender and vocal_gender.strip() else None),
            with_vocals=bool(_parse_bool(with_vocals)) if _parse_bool(with_vocals) is not None else False,
            music_url=f"{MUSIC_URL_PREFIX}/{filename}",
            music_filename=filename,
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        return {
            "id": item.id,
            "title": item.title,
            "lyrics": item.lyrics,
            "genre": item.genre,
            "vocal_gender": item.vocal_gender,
            "with_vocals": item.with_vocals,
            "music_url": item.music_url,
            "music_filename": item.music_filename,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "original_filename": original_name or None,
            "bytes": total,
        }
    except HTTPException:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Erro ao importar música: {str(e)}")


@router.get("/saved")
def list_saved_music(limit: int = 50, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        lim = int(limit)
    except Exception:
        lim = 50
    lim = max(1, min(lim, 200))
    q = db.query(SavedMusic).filter(SavedMusic.user_id == user.id).order_by(SavedMusic.created_at.desc()).limit(lim)
    items = q.all()
    return {
        "items": [
            {
                "id": i.id,
                "title": i.title,
                "lyrics": i.lyrics,
                "genre": i.genre,
                "vocal_gender": i.vocal_gender,
                "with_vocals": i.with_vocals,
                "music_url": i.music_url,
                "music_filename": i.music_filename,
                "clip_url": i.clip_url,
                "clip_filename": i.clip_filename,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in items
        ]
    }


@router.get("/saved/{item_id}")
def get_saved_music(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para acessar este item.")
    return {
        "id": item.id,
        "title": item.title,
        "lyrics": item.lyrics,
        "genre": item.genre,
        "vocal_gender": item.vocal_gender,
        "with_vocals": item.with_vocals,
        "music_url": item.music_url,
        "music_filename": item.music_filename,
        "clip_url": item.clip_url,
        "clip_filename": item.clip_filename,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/saved/{item_id}/watch", response_class=HTMLResponse)
def watch_saved_music(item_id: int):
    safe_id = int(item_id)
    html = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Biblioteca • Codexia</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50">
  <div class="max-w-4xl mx-auto p-4">
    <div class="flex items-start justify-between gap-4">
      <div class="min-w-0">
        <h1 id="title" class="text-2xl font-bold truncate">Carregando...</h1>
        <div id="meta" class="text-sm text-gray-600"></div>
      </div>
      <div class="flex gap-2 flex-wrap justify-end">
        <a class="px-3 py-2 rounded bg-indigo-600 text-white hover:bg-indigo-700 text-sm font-semibold" href="/static/index.html#use_saved_music={safe_id}">Usar</a>
        <a class="px-3 py-2 rounded bg-white border hover:bg-gray-50 text-sm font-semibold" href="/static/index.html#">Voltar</a>
      </div>
    </div>

    <div id="content" class="mt-4 space-y-4"></div>
    <div id="error" class="mt-4 hidden bg-red-50 border border-red-200 text-red-800 rounded p-3 text-sm"></div>
  </div>

  <script>
    (function() {{
      const itemId = {safe_id};
      const token = localStorage.getItem("access_token");
      const titleEl = document.getElementById("title");
      const metaEl = document.getElementById("meta");
      const contentEl = document.getElementById("content");
      const errorEl = document.getElementById("error");

      function showError(msg) {{
        errorEl.textContent = String(msg || "Erro ao carregar.");
        errorEl.classList.remove("hidden");
      }}

      function addBlock(label, node) {{
        const wrap = document.createElement("div");
        wrap.className = "bg-white border rounded p-3";
        if (label) {{
          const h = document.createElement("div");
          h.className = "text-sm font-semibold mb-2";
          h.textContent = label;
          wrap.appendChild(h);
        }}
        wrap.appendChild(node);
        contentEl.appendChild(wrap);
      }}

      if (!token) {{
        titleEl.textContent = "Não autenticado";
        showError("Faça login para assistir este item.");
        const a = document.createElement("a");
        a.href = "/static/login.html";
        a.className = "inline-block mt-2 px-3 py-2 rounded bg-gray-900 text-white hover:bg-black text-sm font-semibold";
        a.textContent = "Ir para login";
        contentEl.appendChild(a);
        return;
      }}

      fetch(`/music/saved/${{encodeURIComponent(String(itemId))}}`, {{
        headers: {{ "Authorization": `Bearer ${{token}}` }}
      }})
      .then(async (res) => {{
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) {{
          throw new Error(data.detail || data.message || "Falha ao carregar item.");
        }}
        return data;
      }})
      .then((it) => {{
        const t = String(it.title || "Música");
        titleEl.textContent = t;
        const created = it.created_at ? new Date(it.created_at).toLocaleString() : "";
        const genre = it.genre ? String(it.genre) : "";
        metaEl.textContent = [created, genre].filter(Boolean).join(" • ");

        const clipUrl = it.clip_url ? String(it.clip_url) : "";
        const musicUrl = it.music_url ? String(it.music_url) : "";
        const lyrics = it.lyrics ? String(it.lyrics) : "";

        if (clipUrl) {{
          const v = document.createElement("video");
          v.src = clipUrl;
          v.controls = true;
          v.className = "w-full h-full object-contain";
          const wrap = document.createElement("div");
          wrap.className = "bg-black rounded overflow-hidden";
          wrap.appendChild(v);
          contentEl.appendChild(wrap);
        }}
        if (musicUrl) {{
          const a = document.createElement("audio");
          a.src = musicUrl;
          a.controls = true;
          a.className = "w-full";
          addBlock("Música", a);
        }}
        if (lyrics) {{
          const pre = document.createElement("pre");
          pre.className = "whitespace-pre-wrap text-sm text-gray-800";
          pre.textContent = lyrics;
          addBlock("Letra", pre);
        }}

        if (!clipUrl && !musicUrl && !lyrics) {{
          const d = document.createElement("div");
          d.className = "bg-white border rounded p-3 text-gray-600";
          d.textContent = "Nada para exibir.";
          contentEl.appendChild(d);
        }}
      }})
      .catch((e) => {{
        titleEl.textContent = "Erro";
        showError(e && e.message ? e.message : String(e));
      }});
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@router.delete("/saved/{item_id}")
def delete_saved_music(item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para excluir este item.")
    db.delete(item)
    db.commit()
    return {"success": True}


@router.post("/saved/{item_id}/publish_youtube")
def publish_saved_clip_youtube(item_id: int, request: PublishSavedClipRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para publicar este item.")
    if not item.clip_url:
        raise HTTPException(status_code=400, detail="Este item não possui clipe para publicar.")

    from app.config import absolute_path_for_video
    abs_video_path = absolute_path_for_video(item.clip_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(status_code=503, detail="Arquivo do clipe não encontrado no servidor.")

    title = (request.title or item.title or "Clipe")[:100]
    description = (request.description or item.lyrics or "").strip()[:4000] or "Clipe gerado automaticamente por Codexia."
    tags = []
    if request.tags and request.tags.strip():
        tags = [t.strip() for t in str(request.tags).split(",") if t.strip()][:20]

    from app.services.youtube_service import YouTubeService
    service = YouTubeService()
    upload_result = service.upload_video(abs_video_path, title=title, description=description, tags=tags)

    youtube_id = None
    if isinstance(upload_result, dict):
        youtube_id = upload_result.get("id") or upload_result.get("videoId") or upload_result.get("youtube_video_id")
        if upload_result.get("error") or upload_result.get("status") == "not_connected":
            raise HTTPException(status_code=502, detail=upload_result.get("error") or "Canal não conectado ao YouTube.")
    elif upload_result:
        youtube_id = str(upload_result)

    youtube_url = (f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else None)
    try:
        db.add(SystemNotification(
            user_id=user.id,
            kind="music_clip_published",
            title="Publicado no YouTube",
            message=f"O clipe '{title}' foi publicado no YouTube.",
            payload_json=json.dumps({"item_id": item.id, "youtube_video_id": youtube_id, "youtube_url": youtube_url}, ensure_ascii=False),
            status="new",
        ))
        db.commit()
    except Exception:
        db.rollback()

    return {"status": "published", "youtube_video_id": youtube_id, "youtube_url": youtube_url, "upload_result": upload_result}


@router.get("/shorts")
def list_music_shorts(limit: int = 50, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        lim = int(limit)
    except Exception:
        lim = 50
    lim = max(1, min(lim, 200))
    rows = (
        db.query(SavedMusicShort)
        .filter(SavedMusicShort.user_id == user.id)
        .order_by(SavedMusicShort.created_at.desc())
        .limit(lim)
        .all()
    )
    parent_ids = [int(r.parent_saved_music_id) for r in rows if getattr(r, "parent_saved_music_id", None)]
    parents = {}
    if parent_ids:
        for p in db.query(SavedMusic).filter(SavedMusic.id.in_(parent_ids)).all():
            parents[int(p.id)] = p
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "clip_url": r.clip_url,
                "clip_filename": r.clip_filename,
                "parent_saved_music_id": r.parent_saved_music_id,
                "parent_title": (parents.get(int(r.parent_saved_music_id)).title if parents.get(int(r.parent_saved_music_id)) else None),
                "start_sec": r.start_sec,
                "end_sec": r.end_sec,
                "youtube_video_id": r.youtube_video_id,
                "uploaded_at": (r.uploaded_at.isoformat() if getattr(r, "uploaded_at", None) else None),
                "created_at": (r.created_at.isoformat() if getattr(r, "created_at", None) else None),
            }
            for r in rows
        ]
    }


@router.post("/saved/{item_id}/shorts/task")
def generate_music_shorts_task(item_id: int, request: GenerateSavedMusicShortsRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(SavedMusic).filter(SavedMusic.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    if item.user_id and item.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para gerar shorts deste item.")
    if not item.clip_url:
        raise HTTPException(status_code=400, detail="Este item não possui clipe.")

    abs_video_path = absolute_path_for_video(item.clip_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(status_code=503, detail="Arquivo do clipe não encontrado no servidor.")

    try:
        count = int(getattr(request, "count", 1) or 1)
    except Exception:
        count = 1
    count = max(1, min(3, count))
    try:
        target_seconds = int(getattr(request, "target_seconds", 45) or 45)
    except Exception:
        target_seconds = 45
    target_seconds = max(20, min(58, target_seconds))

    task_id = create_task(user_id=user.id)
    update_task(task_id, status="processing", progress=0, message="Iniciando geração de shorts...", result={"kind": "music_shorts", "item_id": item.id})

    def _run():
        try:
            try:
                from moviepy.editor import VideoFileClip
            except ImportError:
                from moviepy import VideoFileClip
            try:
                import numpy as np
            except Exception:
                np = None
            import math

            def _subclip(obj, start_t: float, end_t: float):
                if hasattr(obj, "subclip"):
                    return obj.subclip(start_t, end_t)
                if hasattr(obj, "subclipped"):
                    return obj.subclipped(start_t, end_t)
                raise AttributeError("Clip sem subclip/subclipped")

            def _crop(obj, **kwargs):
                if hasattr(obj, "crop"):
                    return obj.crop(**kwargs)
                if hasattr(obj, "cropped"):
                    return obj.cropped(**kwargs)
                raise AttributeError("Clip sem crop/cropped")

            def _resize(obj, size):
                if hasattr(obj, "resize"):
                    return obj.resize(size)
                if hasattr(obj, "resized"):
                    return obj.resized(size)
                raise AttributeError("Clip sem resize/resized")

            clip = VideoFileClip(abs_video_path)
            duration = float(getattr(clip, "duration", 0) or 0)
            if duration <= 2:
                raise Exception("Vídeo muito curto para gerar shorts.")

            scan_step = 3
            one_sec = 1.0
            steps = list(range(0, max(1, int(duration - one_sec)), scan_step))
            rms = []

            for idx, t in enumerate(steps):
                if is_task_cancel_requested(task_id):
                    raise Exception("cancelled_by_user")
                if idx % 8 == 0:
                    update_task(task_id, status="processing", progress=min(35, int((idx / max(1, len(steps))) * 35)), message="Analisando áudio para encontrar trechos fortes...")
                a = None
                try:
                    if clip.audio:
                        a = _subclip(clip.audio, float(t), float(min(duration, t + one_sec))).to_soundarray(fps=8000)
                except Exception:
                    a = None
                if a is None:
                    rms.append(0.0)
                    continue
                try:
                    if np is not None:
                        arr = np.asarray(a, dtype=np.float32)
                        v = float(np.sqrt(np.mean(arr * arr)))
                    else:
                        s = 0.0
                        n = 0
                        for row in a:
                            try:
                                for ch in row:
                                    fv = float(ch)
                                    s += fv * fv
                                    n += 1
                            except Exception:
                                fv = float(row)
                                s += fv * fv
                                n += 1
                        v = float(math.sqrt(s / max(1, n)))
                except Exception:
                    v = 0.0
                rms.append(v)

            win_steps = max(1, int(target_seconds / float(scan_step)))
            scores = []
            if len(rms) < win_steps:
                scores = [float(sum(rms))]
            else:
                cur = float(sum(rms[:win_steps]))
                scores = [cur]
                for i in range(1, len(rms) - win_steps + 1):
                    cur = cur - float(rms[i - 1]) + float(rms[i + win_steps - 1])
                    scores.append(cur)

            picks = []
            used = set()
            for _ in range(count):
                best_i = None
                best_v = None
                for i, sc in enumerate(scores):
                    if i in used:
                        continue
                    if best_v is None or sc > best_v:
                        best_v = sc
                        best_i = i
                if best_i is None:
                    break
                picks.append(best_i)
                gap = max(1, int((target_seconds / float(scan_step)) * 0.8))
                for j in range(max(0, best_i - gap), min(len(scores), best_i + gap + 1)):
                    used.add(j)

            if not picks:
                picks = [max(0, int((duration / 2) / float(scan_step)))]

            out_dir = str(VIDEO_OUTPUT_DIR or os.path.join(str(STATIC_DIR), "videos"))
            os.makedirs(out_dir, exist_ok=True)

            created_ids = []
            db2 = SessionLocal()
            try:
                for idx, start_i in enumerate(picks[:count]):
                    if is_task_cancel_requested(task_id):
                        raise Exception("cancelled_by_user")
                    start = float(start_i * scan_step)
                    end = float(min(duration, start + float(target_seconds)))
                    if end - start < 5:
                        continue
                    update_task(task_id, status="processing", progress=40 + int((idx / max(1, count)) * 50), message=f"Renderizando short {idx+1}/{count}...")
                    sub = _subclip(clip, start, end)
                    w = int(getattr(sub, "w", 0) or 0)
                    h = int(getattr(sub, "h", 0) or 0)
                    if w > 0 and h > 0:
                        target_ar = 9.0 / 16.0
                        ar = float(w) / float(h)
                        if ar > target_ar:
                            new_w = int(float(h) * target_ar)
                            x1 = int((w - new_w) / 2)
                            try:
                                sub = _crop(sub, x1=x1, y1=0, x2=x1 + new_w, y2=h)
                            except Exception:
                                pass
                        elif ar < target_ar:
                            new_h = int(float(w) / target_ar)
                            y1 = int((h - new_h) / 2)
                            try:
                                sub = _crop(sub, x1=0, y1=y1, x2=w, y2=y1 + new_h)
                            except Exception:
                                pass
                    try:
                        sub = _resize(sub, (720, 1280))
                    except Exception:
                        pass

                    filename = f"music_short_{int(item.id)}_{uuid.uuid4().hex}.mp4"
                    out_path = os.path.join(out_dir, filename)
                    sub.write_videofile(
                        out_path,
                        fps=24,
                        codec="libx264",
                        audio_codec="aac",
                        threads=1,
                        ffmpeg_params=["-preset", "ultrafast", "-movflags", "+faststart", "-pix_fmt", "yuv420p"],
                    )
                    rel_url = f"{VIDEO_URL_PREFIX}/{filename}"
                    row = SavedMusicShort(
                        user_id=user.id,
                        parent_saved_music_id=int(item.id),
                        title=f"{_sanitize_title(item.title)} (Short {idx+1})"[:200],
                        clip_url=rel_url,
                        clip_filename=filename,
                        start_sec=float(start),
                        end_sec=float(end),
                    )
                    db2.add(row)
                    db2.flush()
                    if row.id:
                        created_ids.append(int(row.id))
                    try:
                        sub.close()
                    except Exception:
                        pass

                db2.commit()
            except Exception:
                db2.rollback()
                raise
            finally:
                try:
                    db2.close()
                except Exception:
                    pass
            try:
                clip.close()
            except Exception:
                pass

            update_task(task_id, status="completed", progress=100, message="Shorts gerados com sucesso.", result={"kind": "music_shorts", "item_id": item.id, "created_ids": created_ids, "count": len(created_ids)})
        except Exception as e:
            if str(e) == "cancelled_by_user":
                update_task(task_id, status="cancelled", progress=0, message="Cancelado pelo usuário.", result={"kind": "music_shorts", "item_id": item.id})
            else:
                try:
                    import traceback as _traceback
                    tb = _traceback.format_exc()
                except Exception:
                    tb = ""
                msg = str(e) or repr(e)
                et = getattr(type(e), "__name__", "Exception")
                detail = f"{et}: {msg}".strip()
                if tb:
                    tail = tb.strip()
                    if len(tail) > 900:
                        tail = tail[-900:]
                    detail = (detail + "\n" + tail).strip()
                update_task(
                    task_id,
                    status="failed",
                    progress=100,
                    message=detail[:1600],
                    result={"kind": "music_shorts", "item_id": item.id, "error_type": et, "error": msg},
                )

    threading.Thread(target=_run, daemon=True).start()
    return {"message": "Processo iniciado", "task_id": task_id}


@router.delete("/shorts/{short_id}")
def delete_music_short(short_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(SavedMusicShort).filter(SavedMusicShort.id == short_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Short não encontrado.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para excluir este short.")
    try:
        abs_path = absolute_path_for_video(row.clip_url)
        if abs_path and os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except Exception:
                pass
    except Exception:
        pass
    db.delete(row)
    db.commit()
    return {"success": True}


@router.post("/shorts/{short_id}/publish_youtube")
def publish_music_short_youtube(short_id: int, request: PublishSavedClipRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(SavedMusicShort).filter(SavedMusicShort.id == short_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Short não encontrado.")
    if row.user_id and row.user_id != user.id and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Sem permissão para publicar este short.")

    abs_video_path = absolute_path_for_video(row.clip_url)
    if not abs_video_path or not os.path.exists(abs_video_path):
        raise HTTPException(status_code=503, detail="Arquivo do short não encontrado no servidor.")

    title = (request.title or row.title or "Short")[:100]
    description = (request.description or "").strip()[:4000] or "#shorts"
    if "#shorts" not in description.lower():
        description = (description + "\n\n#shorts").strip()[:4000]
    tags = []
    if request.tags and request.tags.strip():
        tags = [t.strip() for t in str(request.tags).split(",") if t.strip()][:20]
    if not tags:
        tags = ["shorts", "música"]

    from app.services.youtube_service import YouTubeService
    service = YouTubeService()
    upload_result = service.upload_video(abs_video_path, title=title, description=description, tags=tags)

    youtube_id = None
    if isinstance(upload_result, dict):
        youtube_id = upload_result.get("id") or upload_result.get("videoId") or upload_result.get("youtube_video_id")
        if upload_result.get("error") or upload_result.get("status") == "not_connected":
            raise HTTPException(status_code=502, detail=upload_result.get("error") or "Canal não conectado ao YouTube.")
    elif upload_result:
        youtube_id = str(upload_result)

    youtube_url = (f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else None)
    try:
        row.youtube_video_id = youtube_id
        row.uploaded_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()

    return {"status": "published", "youtube_video_id": youtube_id, "youtube_url": youtube_url, "upload_result": upload_result}


@router.post("/clip")
def generate_music_clip(request: GenerateClipRequest, user: User = Depends(get_current_user)):
    """
    Gera clipe (vídeo) da música: cenas baseadas na letra + áudio da música gerada.
    """
    music_filename = request.music_filename
    if not music_filename:
        music_dir = MUSIC_OUTPUT_DIR
        if os.path.exists(music_dir):
            songs = [
                f
                for f in os.listdir(music_dir)
                if (f.startswith("song_") or f.startswith("import_"))
                and (f.endswith(".wav") or f.endswith(".mp3") or f.endswith(".m4a") or f.endswith(".aac") or f.endswith(".ogg"))
            ]
            songs.sort(key=lambda f: os.path.getmtime(os.path.join(music_dir, f)), reverse=True)
            if songs:
                music_filename = songs[0]
    if not music_filename:
        raise HTTPException(
            status_code=400,
            detail="Gere a música primeiro (botão 'Gerar Música') ou informe music_filename."
        )
    music_path = absolute_path_for_music(music_filename)
    if not os.path.exists(music_path):
        raise HTTPException(status_code=404, detail="Arquivo de música não encontrado. Gere a música novamente.")
    try:
        from app.services.video_generator import VideoGenerator
        ai = AIContentGenerator()
        video_gen = VideoGenerator(ai_service=ai)
        author = (request.author_text.strip() if request.author_text and request.author_text.strip() else "E.MA")
        watermark_enabled = bool(request.watermark_enabled) if request.watermark_enabled is not None else True
        sync_mode = (request.sync_mode or "auto").strip().lower()
        captions_enabled = bool(request.captions_enabled) if request.captions_enabled is not None else True
        aspect_ratio = (request.aspect_ratio or "9:16").strip()
        if aspect_ratio not in ("9:16", "16:9"):
            aspect_ratio = "9:16"
        safe_title = _sanitize_title(request.title)
        result = video_gen.create_music_video(
            music_path,
            title=safe_title,
            aspect_ratio=aspect_ratio,
            lyrics=(request.lyrics.strip() if request.lyrics and request.lyrics.strip() else None),
            author_text=author,
            watermark_enabled=watermark_enabled,
            sync_mode=sync_mode,
            captions_enabled=captions_enabled,
            image_options={
                "images_count": request.images_count,
                "visual_style": request.visual_style,
                "spiritual_intensity": request.spiritual_intensity,
                "prompt_language": request.prompt_language,
                "mode": request.mode,
                "model": request.model,
            },
        )
        warning = (result.get("warning") if isinstance(result, dict) else None)
        msg = "Clipe gerado com sucesso."
        if isinstance(warning, str) and warning.strip():
            msg = f"{msg} {warning.strip()}"
        return {"video_url": result["video_url"], "message": msg}
    except Exception as e:
        msg = str(e)
        if msg.startswith("Para sincronização perfeita,"):
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=f"Erro ao gerar clipe: {msg}")


@router.post("/clip/task")
def generate_music_clip_task(request: GenerateClipRequest, user: User = Depends(get_current_user)):
    music_filename = request.music_filename
    if not music_filename:
        music_dir = MUSIC_OUTPUT_DIR
        if os.path.exists(music_dir):
            songs = [
                f
                for f in os.listdir(music_dir)
                if (f.startswith("song_") or f.startswith("import_"))
                and (f.endswith(".wav") or f.endswith(".mp3") or f.endswith(".m4a") or f.endswith(".aac") or f.endswith(".ogg"))
            ]
            songs.sort(key=lambda f: os.path.getmtime(os.path.join(music_dir, f)), reverse=True)
            if songs:
                music_filename = songs[0]
    if not music_filename:
        raise HTTPException(status_code=400, detail="Gere a música primeiro (botão 'Gerar Música') ou informe music_filename.")
    music_path = absolute_path_for_music(music_filename)
    if not os.path.exists(music_path):
        raise HTTPException(status_code=404, detail="Arquivo de música não encontrado. Gere a música novamente.")

    task_id = create_task(user_id=user.id)
    sync_mode = (request.sync_mode or "auto").strip().lower()
    watermark_enabled = bool(request.watermark_enabled) if request.watermark_enabled is not None else True
    author = (request.author_text.strip() if request.author_text and request.author_text.strip() else "E.MA")
    captions_enabled = bool(request.captions_enabled) if request.captions_enabled is not None else True
    aspect_ratio = (request.aspect_ratio or "9:16").strip()
    if aspect_ratio not in ("9:16", "16:9"):
        aspect_ratio = "9:16"
    auto_upload_youtube = bool(request.auto_upload_youtube) if request.auto_upload_youtube is not None else False
    safe_title = _sanitize_title(request.title)
    payload = {
        "kind": "music_clip",
        "title": safe_title,
        "music_filename": music_filename,
        "sync_mode": sync_mode,
        "captions_enabled": captions_enabled,
        "aspect_ratio": aspect_ratio,
        "auto_upload_youtube": auto_upload_youtube,
        "watermark_enabled": watermark_enabled,
        "author_text": author,
        "requested_at": datetime.utcnow().isoformat(),
    }
    update_task(task_id, status="processing", progress=1, message="Iniciando geração do clipe...", result=payload)

    def _run():
        try:
            from app.services.video_generator import VideoGenerator
            from app.services.image_storyboard_service import generate_storyboard_images
            ai = AIContentGenerator()
            video_gen = VideoGenerator(ai_service=ai)

            def _progress(p: int, m: str):
                if is_task_cancel_requested(task_id):
                    raise Exception("cancelled_by_user")
                update_task(
                    task_id,
                    status="processing",
                    progress=max(0, min(99, int(p or 0))),
                    message=str(m or "Processando..."),
                    result=payload,
                )

            def _extract_openai_error(e: Exception) -> Dict[str, Any]:
                status = getattr(e, "status_code", None)
                if status is None:
                    resp_obj = getattr(e, "response", None)
                    status = getattr(resp_obj, "status_code", None)
                body = getattr(e, "body", None)
                if body is None:
                    resp_obj = getattr(e, "response", None)
                    try:
                        body = resp_obj.json() if resp_obj is not None else None
                    except Exception:
                        body = None
                if isinstance(body, str) and body.strip():
                    try:
                        body = json.loads(body)
                    except Exception:
                        pass
                err_type = None
                err_code = None
                err_message = None
                if isinstance(body, dict):
                    err = body.get("error")
                    if isinstance(err, dict):
                        err_type = err.get("type")
                        err_code = err.get("code")
                        err_message = err.get("message")
                if not err_message:
                    err_message = str(e)
                return {"status": status, "type": err_type, "code": err_code, "message": err_message}

            if is_task_cancel_requested(task_id):
                update_task(task_id, status="cancelled", progress=0, message="Cancelado pelo usuário.", result=payload)
                return

            desired = request.images_count if request.images_count is not None else 15
            try:
                storyboard_qty = int(desired or 15)
            except Exception:
                storyboard_qty = 15
            storyboard_qty = max(15, min(storyboard_qty, 20))

            lyrics_text = (request.lyrics.strip() if request.lyrics and request.lyrics.strip() else "")
            if not lyrics_text:
                raise Exception("Informe a letra para gerar o storyboard do clipe.")

            _progress(8, f"Gerando imagens (OpenAI) 1/{storyboard_qty}...")
            try:
                ai._load_config()
                api_key = (ai.api_key or "").strip() if getattr(ai, "api_key", None) else ""
                if not api_key:
                    raise Exception("OpenAI não configurada (OPENAI_API_KEY ausente).")
                storyboard = generate_storyboard_images(lyrics_text, quantity=storyboard_qty, api_key=api_key) or {}
            except Exception as e:
                print("OPENAI IMAGE ERROR FULL:", repr(e))
                print("OPENAI IMAGE ERROR DICT:", getattr(e, "__dict__", {}))
                info = _extract_openai_error(e)
                msg = (
                    "Erro OpenAI:\n"
                    f"status: {info.get('status')}\n"
                    f"type: {info.get('type')}\n"
                    f"code: {info.get('code')}\n"
                    f"message: {info.get('message')}"
                )
                update_task(task_id, status="failed", progress=100, message=msg[:900], result={**payload, "error": info})
                return

            images = storyboard.get("images") if isinstance(storyboard, dict) else None
            if not isinstance(images, list) or not images:
                raise Exception("Falha ao gerar storyboard (sem imagens).")
            pre_generated = []
            for it in images:
                if isinstance(it, dict):
                    u = (it.get("url") or "").strip()
                    if u:
                        pre_generated.append(u)
            if not pre_generated:
                raise Exception("Falha ao gerar storyboard (sem URLs).")
            if len(pre_generated) > 20:
                pre_generated = pre_generated[:20]
            while len(pre_generated) < 15:
                pre_generated.append(pre_generated[-1])

            _progress(22, f"Imagens geradas ({len(pre_generated)}). Montando clipe...")
            result = video_gen.create_music_video(
                music_path,
                title=safe_title,
                aspect_ratio=aspect_ratio,
                lyrics=lyrics_text,
                author_text=author,
                watermark_enabled=watermark_enabled,
                sync_mode="auto",
                captions_enabled=captions_enabled,
                progress_callback=_progress,
                image_options={
                    "force_storyboard": True,
                    "storyboard_quantity": len(pre_generated),
                    "pre_generated_images": pre_generated,
                },
            )
            video_url = result.get("video_url") if isinstance(result, dict) else None
            if not video_url:
                raise Exception("Falha ao gerar o clipe (sem URL).")

            youtube_info = None
            if auto_upload_youtube:
                try:
                    from app.services.youtube_service import YouTubeService
                    from app.config import absolute_path_for_video

                    update_task(task_id, status="processing", progress=96, message="Iniciando upload para o YouTube...")
                    abs_video_path = absolute_path_for_video(video_url)
                    service = YouTubeService()
                    upload_result = service.upload_video(
                        abs_video_path,
                        title=safe_title,
                        description=(request.lyrics or "").strip()[:4000] or "Clipe gerado automaticamente por Codexia.",
                        tags=["música", "gospel"] if "16:9" == aspect_ratio else ["shorts", "música"],
                    )
                    youtube_id = None
                    if isinstance(upload_result, dict):
                        youtube_id = upload_result.get("id") or upload_result.get("videoId") or upload_result.get("youtube_video_id")
                    elif upload_result:
                        youtube_id = str(upload_result)
                    youtube_url = (f"https://www.youtube.com/watch?v={youtube_id}" if youtube_id else None)
                    youtube_info = {
                        "upload_result": upload_result,
                        "youtube_video_id": youtube_id,
                        "youtube_url": youtube_url,
                        "published": bool(youtube_id),
                        "status": "published" if youtube_id else "failed",
                    }
                except Exception as e:
                    youtube_info = {"error": str(e), "published": False, "status": "failed"}

            warning = (result.get("warning") if isinstance(result, dict) else None)
            update_task(
                task_id,
                status="completed",
                progress=100,
                message=(
                    ("Clipe gerado e publicado no YouTube." if (youtube_info and youtube_info.get("published")) else "Clipe gerado com sucesso.")
                    + (f" {warning.strip()}" if isinstance(warning, str) and warning.strip() else "")
                ),
                result={**payload, "video_url": video_url, "youtube": youtube_info, "warning": warning},
            )
            db2 = SessionLocal()
            try:
                note_payload = {"task_id": task_id, "video_url": video_url}
                if youtube_info:
                    note_payload["youtube"] = youtube_info
                db2.add(SystemNotification(
                    user_id=user.id,
                    kind="music_clip_ready",
                    title="Clipe pronto",
                    message=f"Seu clipe '{safe_title[:80]}' foi concluído.",
                    payload_json=json.dumps(note_payload, ensure_ascii=False),
                    status="new",
                ))
                db2.commit()
            except Exception:
                db2.rollback()
            finally:
                db2.close()
        except Exception as e:
            if is_task_cancel_requested(task_id) or str(e) == "cancelled_by_user":
                current = get_task(task_id) or {}
                try:
                    p = int(current.get("progress") or 0)
                except Exception:
                    p = 0
                update_task(task_id, status="cancelled", progress=max(0, min(99, p)), message="Cancelado pelo usuário.", result=payload)
            else:
                print("OPENAI IMAGE ERROR FULL:", repr(e))
                print("OPENAI IMAGE ERROR DICT:", getattr(e, "__dict__", {}))
                info = None
                try:
                    info = _extract_openai_error(e)
                except Exception:
                    info = None
                if isinstance(info, dict) and any(info.get(k) for k in ("status", "type", "code", "message")):
                    msg = (
                        "Erro OpenAI:\n"
                        f"status: {info.get('status')}\n"
                        f"type: {info.get('type')}\n"
                        f"code: {info.get('code')}\n"
                        f"message: {info.get('message')}"
                    )
                    update_task(task_id, status="failed", progress=100, message=msg[:900], result={**payload, "error": info})
                else:
                    msg = str(e)
                    update_task(task_id, status="failed", progress=100, message=msg[:900], result=payload)
            db2 = SessionLocal()
            try:
                db2.add(SystemNotification(
                    user_id=user.id,
                    kind="music_clip_failed",
                    title="Falha ao gerar clipe",
                    message=(msg or "Erro desconhecido")[:500],
                    payload_json=json.dumps({"task_id": task_id}, ensure_ascii=False),
                    status="new",
                ))
                db2.commit()
            except Exception:
                db2.rollback()
            finally:
                db2.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"message": "Processo iniciado", "task_id": task_id}


@router.get("/tasks")
def list_my_music_tasks(limit: int = 20, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    lim = max(1, min(100, int(limit or 20)))
    rows = (
        db.query(VideoTask)
        .filter(VideoTask.user_id == user.id)
        .order_by(VideoTask.updated_at.desc().nullslast(), VideoTask.created_at.desc().nullslast())
        .limit(lim)
        .all()
    )
    items = []
    for r in rows:
        payload2 = None
        if r.result_json:
            try:
                payload2 = json.loads(r.result_json)
            except Exception:
                payload2 = r.result_json
        items.append({
            "task_id": r.id,
            "status": r.status,
            "progress": int(r.progress or 0),
            "message": r.message,
            "result": payload2,
            "created_at": (r.created_at.isoformat() if getattr(r, "created_at", None) else None),
            "updated_at": (r.updated_at.isoformat() if getattr(r, "updated_at", None) else None),
        })
    return {"count": len(items), "items": items}


@router.get("/notifications")
def list_my_music_notifications(limit: int = 20, status: Optional[str] = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    lim = max(1, min(100, int(limit or 20)))
    q = db.query(SystemNotification).filter(SystemNotification.user_id == user.id)
    if status:
        q = q.filter(SystemNotification.status == str(status).strip())
    rows = q.order_by(SystemNotification.created_at.desc()).limit(lim).all()
    items = []
    for n in rows:
        payload2 = None
        if n.payload_json:
            try:
                payload2 = json.loads(n.payload_json)
            except Exception:
                payload2 = n.payload_json
        items.append({
            "id": n.id,
            "kind": n.kind,
            "title": n.title,
            "message": n.message,
            "status": n.status,
            "created_at": (n.created_at.isoformat() if n.created_at else None),
            "read_at": (n.read_at.isoformat() if n.read_at else None),
            "payload": payload2,
        })
    return {"count": len(items), "items": items}


@router.post("/notifications/{notification_id}/read")
def mark_music_notification_read(notification_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    n = db.query(SystemNotification).filter(SystemNotification.id == notification_id, SystemNotification.user_id == user.id).first()
    if not n:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")
    n.status = "read"
    n.read_at = datetime.utcnow()
    db.commit()
    return {"status": "read", "id": n.id}
