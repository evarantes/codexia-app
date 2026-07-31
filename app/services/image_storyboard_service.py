from __future__ import annotations

import base64
import os
import re
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from app.services.ai_router import AIRouter, AICapability

BASE_DIR = Path("generated_assets/storyboard_images")
BASE_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = Path("generated_assets/thumbnails")
THUMB_DIR.mkdir(parents=True, exist_ok=True)
PAID_AI_DISABLE_FLAG = Path(__file__).resolve().parents[2] / "artifacts" / "financial_guardian" / "disable_paid_ai.flag"

ROUTER = AIRouter()

SYSTEM_STYLE = """
Você é um diretor de arte cinematográfico cristão.
Leia o texto completo e transforme em cenas visuais coerentes para vídeo.
Não interprete palavras isoladas.
Não crie imagens de terror, demoníacas, grotescas ou assustadoras.
Quando aparecer fogo, representar como presença divina, unção, glória e poder de Deus.
NUNCA colocar texto, legendas, letras, números ou palavras dentro das imagens.
Estilo: cinematográfico, realista, épico, luz divina, atmosfera espiritual, composição profissional, 4K.
""".strip()


def _clean_numbered_list_line(line: str) -> str:
    s = (line or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\s*\d+\s*[\.\-\)]\s*", "", s).strip()
    s = re.sub(r"^\s*[-•]\s*", "", s).strip()
    return s


def _paid_ai_disabled() -> bool:
    if PAID_AI_DISABLE_FLAG.exists():
        return True
    for name in (
        "CODEXIA_DISABLE_PAID_AI",
        "DISABLE_PAID_AI",
        "NO_PAID_AI",
        "FINANCIAL_GUARDIAN_NO_PAID_MODE",
    ):
        raw = str(os.getenv(name) or "").strip().lower()
        if raw in {"1", "true", "yes", "sim", "on", "enabled", "enable"}:
            return True
    return False


def build_scene_prompts(text: str, quantity: int = 15, api_key: Optional[str] = None) -> List[str]:
    quantity = max(15, min(int(quantity or 15), 20))
    full_text = (text or "").strip()

    prompt = f"""
{SYSTEM_STYLE}

Crie exatamente {quantity} prompts de imagem para um vídeo baseado neste texto:

{full_text}

Regras:
- Cada prompt deve representar uma cena diferente.
- As cenas devem ter sequência lógica: começo, desenvolvimento, clímax e final.
- Não usar texto dentro da imagem.
- Não usar legenda.
- Não usar arte infantil/cartoon.
- Manter coerência bíblica, espiritual e cinematográfica.
- Responder apenas em lista numerada, um prompt por linha.
""".strip()

    if _paid_ai_disabled() and str(os.getenv("AI_COST_DRY_RUN") or "").strip().lower() not in {"1", "true", "yes", "sim", "on"}:
        raise RuntimeError("Modo sem consumo pago ativo; storyboard e thumbnail desabilitados.")
    raw = ROUTER.generate_text(
        user_id=None,
        task_id=None,
        video_id=None,
        capability=AICapability.TEXT_GENERATION,
        prompt=prompt,
        system_prompt="Você cria prompts visuais profissionais para geração de imagens.",
        temperature=0.7,
        json_mode=False,
    ).strip()
    if not raw:
        return []

    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    prompts: List[str] = []
    for line in lines:
        clean = _clean_numbered_list_line(line)
        if not clean:
            continue
        prompts.append(clean)

    return prompts[:quantity]


def generate_image(prompt: str, index: int, model: str = "gpt-image-1", api_key: Optional[str] = None) -> Dict[str, Any]:
    final_prompt = f"""
{SYSTEM_STYLE}

Cena {index}:
{(prompt or "").strip()}

Reforço obrigatório:
- sem texto na imagem
- sem legenda
- sem palavras
- sem números
- sem símbolos escritos
- imagem cinematográfica horizontal para vídeo
""".strip()

    try:
        if _paid_ai_disabled() and str(os.getenv("AI_COST_DRY_RUN") or "").strip().lower() not in {"1", "true", "yes", "sim", "on"}:
            raise RuntimeError("Modo sem consumo pago ativo; storyboard e thumbnail desabilitados.")
        path = ROUTER.generate_image(
            user_id=None,
            task_id=None,
            video_id=None,
            capability=AICapability.IMAGE_GENERATION,
            prompt=final_prompt,
            output_dir=str(BASE_DIR),
        )
        filename = os.path.basename(str(path))
        return {
            "scene": int(index),
            "prompt": (prompt or "").strip(),
            "file": str(path),
            "url": f"/generated_assets/storyboard_images/{filename}",
            "model_used": model,
        }
    except Exception as e:
        print("OPENAI IMAGE ERROR FULL:", repr(e))
        print("OPENAI IMAGE ERROR DICT:", getattr(e, "__dict__", {}))
        raise


def generate_storyboard_images(text: str, quantity: int = 15, api_key: Optional[str] = None) -> Dict[str, Any]:
    qty = max(15, min(int(quantity or 15), 20))
    prompts = build_scene_prompts(text, qty, api_key=api_key)
    if len(prompts) < qty:
        while len(prompts) < qty:
            base = prompts[-1] if prompts else (text or "").strip()[:240]
            prompts.append(base)

    images: List[Dict[str, Any]] = []
    preferred_model = "gpt-image-1"
    for i, scene_prompt in enumerate(prompts[:qty], start=1):
        image = generate_image(scene_prompt, i, model=preferred_model, api_key=api_key)
        images.append(image)

    return {
        "success": True,
        "quantity": len(images),
        "images": images,
    }


def _pick_font(size: int):
    try:
        from PIL import ImageFont
    except Exception:
        return None
    candidates = [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\impact.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _wrap_text(text: str, max_chars: int) -> List[str]:
    s = " ".join((text or "").strip().split())
    if not s:
        return []
    words = s.split(" ")
    lines: List[str] = []
    buf = ""
    for w in words:
        cand = (buf + " " + w).strip() if buf else w
        if len(cand) <= max_chars:
            buf = cand
            continue
        if buf:
            lines.append(buf)
        buf = w
    if buf:
        lines.append(buf)
    return lines


def overlay_thumbnail_text(image_file: str, text: str) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        raise RuntimeError(f"Pillow não disponível: {e}")

    src = (image_file or "").strip()
    if not src or not os.path.exists(src):
        raise RuntimeError("Imagem base não encontrada para thumbnail.")

    txt = " ".join((text or "").strip().split())
    if not txt:
        raise RuntimeError("Texto da thumbnail vazio.")

    img = Image.open(src).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    max_lines = 2
    max_chars = 14 if w >= 1000 else 12
    lines = _wrap_text(txt.upper(), max_chars=max_chars)[:max_lines]
    if not lines:
        raise RuntimeError("Não foi possível preparar o texto da thumbnail.")

    font_size = int(h * 0.12)
    font = _pick_font(font_size)
    while font_size > 24:
        font = _pick_font(font_size)
        if not font:
            break
        widths = []
        heights = []
        for ln in lines:
            bb = draw.textbbox((0, 0), ln, font=font, stroke_width=8)
            widths.append(bb[2] - bb[0])
            heights.append(bb[3] - bb[1])
        if widths and max(widths) <= int(w * 0.92) and sum(heights) <= int(h * 0.32):
            break
        font_size = int(font_size * 0.9)

    font = _pick_font(font_size) or font
    stroke_w = max(6, int(font_size * 0.12))
    fill = (255, 234, 64)
    stroke_fill = (0, 0, 0)

    bbs = [draw.textbbox((0, 0), ln, font=font, stroke_width=stroke_w) for ln in lines]
    line_heights = [(bb[3] - bb[1]) for bb in bbs]
    total_h = sum(line_heights) + int(font_size * 0.10) * (len(lines) - 1)
    y = int(h * 0.70) - int(total_h / 2)

    for i, ln in enumerate(lines):
        bb = bbs[i]
        tw = bb[2] - bb[0]
        th = bb[3] - bb[1]
        x = int((w - tw) / 2)
        draw.text((x, y), ln, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=stroke_fill)
        y += th + int(font_size * 0.10)

    filename = f"thumb_{uuid.uuid4().hex}.png"
    out_path = THUMB_DIR / filename
    img.save(out_path, format="PNG", optimize=True)
    return {"file": str(out_path), "url": f"/generated_assets/thumbnails/{filename}"}


def generate_thumbnail_with_text(
    idea: str,
    text: str,
    image_prompt: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    theme = (idea or "").strip()
    t = (text or "").strip()
    if not theme or not t:
        raise RuntimeError("Ideia e texto da thumbnail são obrigatórios.")

    base_prompt = (image_prompt or "").strip()
    if not base_prompt:
        base_prompt = (
            "Epic Christian Digital Art, cinematic, dramatic, epic lighting, god rays, "
            "ancient biblical atmosphere, high detail, realistic, no horror, no gore, no demons, "
            "no text, no letters, no watermark."
        )
    final_prompt = f"{base_prompt}\n\nTheme: {theme}\n\nNo text in the image."
    if _paid_ai_disabled() and str(os.getenv("AI_COST_DRY_RUN") or "").strip().lower() not in {"1", "true", "yes", "sim", "on"}:
        raise RuntimeError("Modo sem consumo pago ativo; storyboard e thumbnail desabilitados.")
    base_path = ROUTER.generate_image(
        user_id=None,
        task_id=None,
        video_id=None,
        capability=AICapability.THUMBNAIL_GENERATION,
        prompt=final_prompt[:6000],
        output_dir=str(THUMB_DIR),
    )
    base_filename = os.path.basename(str(base_path))

    out = overlay_thumbnail_text(str(base_path), t)
    out["base_file"] = str(base_path)
    out["base_url"] = f"/generated_assets/thumbnails/{base_filename}"
    out["text"] = t
    out["image_prompt_used"] = base_prompt
    return out
