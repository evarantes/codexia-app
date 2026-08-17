from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly 1 match, found {count}\nneedle={old[:140]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_first(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{path}: expected at least 1 match\nneedle={old[:140]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Backend: structured review-ready generation and preservation marker.
replace_first(
    "app/routers/youtube.py",
    "from app.services.ai_generator import AIContentGenerator\n",
    "from app.services.ai_generator import AIContentGenerator\nfrom app.services.story_review_editor import generate_review_ready_story_text\n",
)

replace_once(
    "app/routers/youtube.py",
    "    force_render_only: bool = False\n\nclass StoryTextGenerateRequest(BaseModel):",
    "    force_render_only: bool = False\n    editorial_reviewed: bool = False\n    editorial_review_ready: bool = False\n\nclass StoryTextGenerateRequest(BaseModel):",
)

old_endpoint = '''@router.post("/story/generate_text")\ndef generate_story_text(request: StoryTextGenerateRequest):\n    ai_service = AIContentGenerator()\n    kind = (request.kind or "story").strip().lower()\n    if kind not in {"story", "devotional", "prayer"}:\n        kind = "story"\n    try:\n        text = ai_service.generate_story_or_devotional_text(\n            instruction=request.instruction,\n            kind=kind,\n            duration_min_minutes=request.duration_min,\n            duration_max_minutes=request.duration_max,\n        )\n        return {"text": text, "kind": kind, "duration_min": request.duration_min, "duration_max": request.duration_max}\n    except Exception as e:\n        raise HTTPException(\n            status_code=503,\n            detail={\n                "error": "ai_text_generation_failed",\n                "message": str(e)[:900],\n                "kind": kind,\n            },\n        )\n'''
new_endpoint = '''@router.post("/story/generate_text")\ndef generate_story_text(request: StoryTextGenerateRequest):\n    ai_service = AIContentGenerator()\n    kind = (request.kind or "story").strip().lower()\n    if kind not in {"story", "devotional", "prayer"}:\n        kind = "story"\n    try:\n        package = generate_review_ready_story_text(\n            ai_service,\n            instruction=request.instruction,\n            kind=kind,\n            duration_min_minutes=request.duration_min,\n            duration_max_minutes=request.duration_max,\n        )\n        return {\n            **package,\n            "kind": kind,\n            "duration_min": request.duration_min,\n            "duration_max": request.duration_max,\n        }\n    except Exception as e:\n        raise HTTPException(\n            status_code=503,\n            detail={\n                "error": "ai_text_generation_failed",\n                "message": str(e)[:900],\n                "kind": kind,\n            },\n        )\n'''
replace_once("app/routers/youtube.py", old_endpoint, new_endpoint)

replace_once(
    "app/routers/youtube.py",
    "                script = _build_story_plan_from_text(request.story_content, minutes, kind_norm)\n            else:",
    "                script = _build_story_plan_from_text(request.story_content, minutes, kind_norm)\n                if isinstance(script, dict) and bool(getattr(request, 'editorial_reviewed', False)):\n                    script['editorial_reviewed'] = True\n                    script['editorial_review_ready'] = bool(getattr(request, 'editorial_review_ready', False))\n            else:",
)

# Narrative editor: once the human reviewed this exact narration, do not silently rewrite it again.
replace_once(
    "app/services/narrative_editor.py",
    "        revised, report = revise_plan_with_ai(self, working)\n        final_plan = revised if isinstance(revised, dict) else working\n",
    "        if bool(working.get('editorial_reviewed')):\n            report = {\n                'version': 3,\n                'enabled': True,\n                'mode': 'human_review_preserved',\n                'changed': False,\n                'fail_open': True,\n                'skip_reason': 'editorial_reviewed_before_video_generation',\n                'original': analyze_narrative_plan(working),\n            }\n            revised = None\n        else:\n            revised, report = revise_plan_with_ai(self, working)\n        final_plan = revised if isinstance(revised, dict) else working\n",
)

# Frontend: title visible/editable and values propagated into video request.
replace_once(
    "app/static/index.html",
    "                    ytStoryInstruction: '',\n                    ytStoryImproveInstruction: '',",
    "                    ytStoryInstruction: '',\n                    ytStoryTitle: '',\n                    ytStoryEditorialReviewReady: false,\n                    ytStoryImproveInstruction: '',",
)

old_ui = '''                    <div class="mb-4">\n                        <label class="block font-bold mb-2">Texto para narração</label>\n                        <textarea v-model="ytStoryContent" class="w-full border p-3 rounded" rows="10" placeholder="Cole aqui sua história/devocional, ou gere com IA acima."></textarea>\n                    </div>'''
new_ui = '''                    <div class="mb-4">\n                        <label class="block font-bold mb-2">Título do vídeo</label>\n                        <input v-model="ytStoryTitle" type="text" maxlength="120" class="w-full border p-3 rounded bg-white" placeholder="O título gerado pela IA aparecerá aqui e poderá ser editado.">\n                        <p class="text-xs text-gray-500 mt-1">Este título será preservado quando você gerar o vídeo.</p>\n                    </div>\n\n                    <div class="mb-4">\n                        <label class="block font-bold mb-2">Texto para narração</label>\n                        <textarea v-model="ytStoryContent" @input="ytStoryEditorialReviewReady = true" class="w-full border p-3 rounded" rows="10" placeholder="Cole aqui sua história/devocional, ou gere com IA acima."></textarea>\n                    </div>'''
replace_once("app/static/index.html", old_ui, new_ui)

replace_once(
    "app/static/index.html",
    "                        if (res.ok && data.text) {\n                            this.ytStoryContent = data.text;\n                            this.ytStoryGeneratedImages = [];\n                            this.ytStoryVideoDuration = this.ytStoryPredictedDurationMinutesValue || this.ytStoryVideoDuration;",
    "                        if (res.ok && data.text) {\n                            this.ytStoryContent = data.text;\n                            this.ytStoryTitle = String(data.title || '').trim();\n                            this.ytStoryEditorialReviewReady = Boolean(data.editorial_review_ready);\n                            this.ytStoryGeneratedImages = [];\n                            this.ytStoryVideoDuration = this.ytStoryPredictedDurationMinutesValue || this.ytStoryVideoDuration;",
)

replace_once(
    "app/static/index.html",
    "                            story_content: this.ytStoryContent,\n                            selected_images: selectedImages,",
    "                            story_content: this.ytStoryContent,\n                            override_title: String(this.ytStoryTitle || '').trim() || null,\n                            editorial_reviewed: true,\n                            editorial_review_ready: Boolean(this.ytStoryEditorialReviewReady),\n                            selected_images: selectedImages,",
)

# Financial simulation canonical payload should reflect title too, without affecting production behavior.
replace_once(
    "app/static/index.html",
    "                        story_content: this.ytStoryContent || this.ytStoryInstruction || '',\n                        duration: Number(this.ytStoryVideoDuration || this.ytVideoDuration || 8) || 8,",
    "                        story_content: this.ytStoryContent || this.ytStoryInstruction || '',\n                        override_title: String(this.ytStoryTitle || '').trim() || null,\n                        editorial_reviewed: true,\n                        editorial_review_ready: Boolean(this.ytStoryEditorialReviewReady),\n                        duration: Number(this.ytStoryVideoDuration || this.ytVideoDuration || 8) || 8,",
)

print("review-text editorial flow patch applied")
