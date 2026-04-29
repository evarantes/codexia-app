from __future__ import annotations

import base64
import os
import re
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE_DIR = Path("generated_assets/storyboard_images")
BASE_DIR.mkdir(parents=True, exist_ok=True)

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


def build_scene_prompts(text: str, quantity: int = 15) -> List[str]:
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

    model_candidates = ["gpt-4.1-mini", "gpt-4o-mini"]
    raw = ""
    last_err: Optional[Exception] = None
    for m in model_candidates:
        try:
            response = client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": "Você cria prompts visuais profissionais para geração de imagens."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
            )
            raw = (response.choices[0].message.content or "").strip()
            if raw:
                break
        except Exception as e:
            last_err = e
            raw = ""

    if not raw:
        if last_err:
            raise last_err
        return []

    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    prompts: List[str] = []
    for line in lines:
        clean = _clean_numbered_list_line(line)
        if not clean:
            continue
        prompts.append(clean)

    return prompts[:quantity]


def _image_model_candidates(preferred: str) -> List[str]:
    pref = (preferred or "").strip() or "gpt-image-1"
    out = [pref]
    if pref != "gpt-image-1":
        out.append("gpt-image-1")
    if pref != "gpt-image-2":
        out.append("gpt-image-2")
    if "dall-e-3" not in out:
        out.append("dall-e-3")
    uniq = []
    seen = set()
    for m in out:
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def generate_image(prompt: str, index: int, model: str = "gpt-image-1") -> Dict[str, Any]:
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

    last_err: Optional[Exception] = None
    for m in _image_model_candidates(model):
        try:
            if m == "dall-e-3":
                result = client.images.generate(
                    model="dall-e-3",
                    prompt=final_prompt,
                    size="1792x1024",
                    quality="hd",
                    n=1,
                    response_format="b64_json",
                )
            else:
                result = client.images.generate(
                    model=m,
                    prompt=final_prompt,
                    size="1536x1024",
                    quality="high",
                    n=1,
                    response_format="b64_json",
                )
            image_base64 = getattr(result.data[0], "b64_json", None) if result and getattr(result, "data", None) else None
            if not isinstance(image_base64, str) or not image_base64.strip():
                raise Exception("Resposta de imagem sem b64_json.")
            image_bytes = base64.b64decode(image_base64)
            filename = f"scene_{int(index):02d}_{uuid.uuid4().hex}.png"
            path = BASE_DIR / filename
            with open(path, "wb") as f:
                f.write(image_bytes)
            return {
                "scene": int(index),
                "prompt": (prompt or "").strip(),
                "file": str(path),
                "url": f"/generated_assets/storyboard_images/{filename}",
                "model_used": m,
            }
        except Exception as e:
            last_err = e
            continue
    raise last_err or Exception("Falha ao gerar imagem.")


def generate_storyboard_images(text: str, quantity: int = 15) -> Dict[str, Any]:
    qty = max(15, min(int(quantity or 15), 20))
    prompts = build_scene_prompts(text, qty)
    if len(prompts) < qty:
        while len(prompts) < qty:
            base = prompts[-1] if prompts else (text or "").strip()[:240]
            prompts.append(base)

    images: List[Dict[str, Any]] = []
    preferred_model = "gpt-image-1"
    for i, scene_prompt in enumerate(prompts[:qty], start=1):
        image = generate_image(scene_prompt, i, model=preferred_model)
        images.append(image)

    return {
        "success": True,
        "quantity": len(images),
        "images": images,
    }

