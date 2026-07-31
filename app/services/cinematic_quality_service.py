import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.ai_router import AICapability


class CinematicQualityService:
    DIMENSION_LABELS = {
        "text_audio_alignment": "Texto x Áudio",
        "grammar": "Gramática",
        "orthography": "Ortografia",
        "fluency": "Fluidez",
        "naturality": "Naturalidade",
        "pronunciation": "Pronúncia",
        "caption_quality": "Legendas",
        "synchronization": "Sincronização",
        "rhythm": "Ritmo",
        "render_quality": "Render",
        "visual_quality": "Qualidade Visual",
        "biblical_coherence": "Coerência Bíblica",
        "narrative_continuity": "Continuidade Narrativa",
        "clarity": "Clareza",
        "emotional_impact": "Impacto Emocional",
        "interest": "Interesse",
        "predicted_retention": "Retenção Prevista",
        "hook_strength": "Gancho",
        "conclusion_strength": "Conclusão",
    }
    MINIMUM_DIMENSION_SCORES = {
        "text_audio_alignment": 92,
        "grammar": 88,
        "orthography": 94,
        "fluency": 86,
        "naturality": 85,
        "pronunciation": 86,
        "caption_quality": 92,
        "synchronization": 92,
        "rhythm": 84,
        "render_quality": 86,
        "visual_quality": 80,
        "biblical_coherence": 90,
        "narrative_continuity": 85,
        "clarity": 86,
        "emotional_impact": 82,
        "interest": 84,
        "predicted_retention": 82,
        "hook_strength": 84,
        "conclusion_strength": 84,
    }
    DIMENSION_WEIGHTS = {
        "text_audio_alignment": 0.08,
        "grammar": 0.06,
        "orthography": 0.06,
        "fluency": 0.06,
        "naturality": 0.07,
        "pronunciation": 0.07,
        "caption_quality": 0.08,
        "synchronization": 0.10,
        "rhythm": 0.06,
        "render_quality": 0.05,
        "visual_quality": 0.07,
        "biblical_coherence": 0.07,
        "narrative_continuity": 0.05,
        "clarity": 0.04,
        "emotional_impact": 0.05,
        "interest": 0.04,
        "predicted_retention": 0.03,
        "hook_strength": 0.03,
        "conclusion_strength": 0.03,
    }
    MINIMUM_OVERALL_SCORE = 90
    MINIMUM_TECHNICAL_SCORE = 90
    MINIMUM_EDITORIAL_SCORE = 90
    MAX_AUTO_RECOVERY_ATTEMPTS_PER_STAGE = 3

    DEFAULT_PRONUNCIATION_MAP = {
        "yhwh": "iavé",
        "yhvh": "iavé",
        "yeshua": "ieshua",
        "yeshua hamashiach": "ieshua hamashíarr",
        "melquisedeque": "melquisedeque",
        "mefibosete": "mefibosete",
        "nabucodonosor": "nabucodonosor",
        "quiriate-jearim": "quiriate-jearim",
        "quiriate jearim": "quiriate-jearim",
        "bate-seba": "bate-seba",
        "ezequias": "ezequias",
        "zorobabel": "zorobabel",
        "genesaré": "guenezaré",
        "getsemani": "getsêmani",
        "getsêmani": "getsêmani",
        "joquebede": "joquebede",
        "issacar": "issacar",
        "naftali": "naftali",
        "mael": "maél",
        "seol": "sheol",
        "jerusalem": "jerusalém",
        "galileia": "galileia",
    }

    def __init__(self, ai_service: Optional[Any] = None):
        self.ai_service = ai_service
        self._service_dir = Path(__file__).resolve().parent
        self._pronunciation_dictionary_path = self._service_dir / "cinematic_pronunciation_dictionary.json"
        self._quality_learning_path = self._service_dir / "cinematic_quality_learning.json"
        self._ensure_support_files()

    def _ensure_support_files(self) -> None:
        if not self._pronunciation_dictionary_path.exists():
            self._write_json_file(
                self._pronunciation_dictionary_path,
                {
                    "updated_at": None,
                    "entries": [
                        {"token": key, "spoken_form": value, "source": "default"}
                        for key, value in sorted(self.DEFAULT_PRONUNCIATION_MAP.items())
                    ],
                },
            )
        if not self._quality_learning_path.exists():
            self._write_json_file(
                self._quality_learning_path,
                {
                    "updated_at": None,
                    "videos": [],
                    "repairs": [],
                    "winning_patterns": [],
                    "recovery_history": [],
                },
            )

    def _read_json_file(self, path: Path, fallback: Any) -> Any:
        try:
            if not path.exists():
                return fallback
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return fallback

    def _write_json_file(self, path: Path, payload: Any) -> None:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _normalize_text(self, value: Any) -> str:
        text = str(value or "")
        text = unicodedata.normalize("NFC", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def _compact_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", self._normalize_text(value)).strip()

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _fold(self, value: Any) -> str:
        text = self._compact_text(value)
        if not text:
            return ""
        normalized = unicodedata.normalize("NFKD", text)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return normalized.lower().strip()

    def _count_words(self, value: Any) -> int:
        return len(re.findall(r"\w+", self._compact_text(value), flags=re.UNICODE))

    def _normalize_category_key(self, value: Any) -> str:
        folded = self._fold(value)
        mapping = {
            "texto": "text_audio_alignment",
            "texto x audio": "text_audio_alignment",
            "texto canonico": "text_audio_alignment",
            "text_audio_alignment": "text_audio_alignment",
            "text audio alignment": "text_audio_alignment",
            "gramatica": "grammar",
            "grammar": "grammar",
            "ortografia": "orthography",
            "orthography": "orthography",
            "fluidez": "fluency",
            "fluency": "fluency",
            "naturalidade": "naturality",
            "naturality": "naturality",
            "pronuncia": "pronunciation",
            "pronunciation": "pronunciation",
            "legendas": "caption_quality",
            "legenda": "caption_quality",
            "caption quality": "caption_quality",
            "caption_quality": "caption_quality",
            "sincronizacao": "synchronization",
            "synchronization": "synchronization",
            "coerencia entre fala e imagem": "synchronization",
            "coerencia entre audio e imagem": "synchronization",
            "ritmo": "rhythm",
            "rhythm": "rhythm",
            "transicoes": "rhythm",
            "transicoes visuais": "rhythm",
            "permanencia das cenas": "rhythm",
            "render": "render_quality",
            "render quality": "render_quality",
            "render_quality": "render_quality",
            "qualidade visual": "visual_quality",
            "visual quality": "visual_quality",
            "visual_quality": "visual_quality",
            "repeticao de imagens": "visual_quality",
            "coerencia biblica": "biblical_coherence",
            "biblical coherence": "biblical_coherence",
            "biblical_coherence": "biblical_coherence",
            "continuidade narrativa": "narrative_continuity",
            "narrative continuity": "narrative_continuity",
            "narrative_continuity": "narrative_continuity",
            "clareza": "clarity",
            "clarity": "clarity",
            "impacto emocional": "emotional_impact",
            "emotional impact": "emotional_impact",
            "emotional_impact": "emotional_impact",
            "interesse": "interest",
            "interest": "interest",
            "retencao prevista": "predicted_retention",
            "retenção prevista": "predicted_retention",
            "predicted retention": "predicted_retention",
            "predicted_retention": "predicted_retention",
            "gancho": "hook_strength",
            "hook": "hook_strength",
            "hook_strength": "hook_strength",
            "conclusao": "conclusion_strength",
            "conclusão": "conclusion_strength",
            "conclusion": "conclusion_strength",
            "conclusion_strength": "conclusion_strength",
        }
        return mapping.get(folded, folded)

    def _normalize_stage_key(self, value: Any, category: str = "") -> str:
        folded = self._fold(value)
        if folded in {"editorial_tts_render", "pronunciation_tts_render", "captions_render", "thumbnail_publish"}:
            return folded
        if any(token in folded for token in ["audio", "narracao", "narração", "tts"]):
            if category in {"pronunciation", "rhythm", "text_audio_alignment"}:
                return "pronunciation_tts_render"
        if any(token in folded for token in ["editorial", "texto", "revisor", "roteiro"]):
            return "editorial_tts_render"
        if any(token in folded for token in ["pronunc", "tts"]):
            return "pronunciation_tts_render"
        if any(token in folded for token in ["sincron", "ritmo", "legend", "pos-producao", "pos producao", "posproducao", "render", "pós-produção"]):
            return "captions_render"
        if "thumbnail" in folded:
            return "thumbnail_publish"
        if category in {"text_audio_alignment", "grammar", "orthography", "fluency", "naturality", "biblical_coherence", "narrative_continuity", "clarity", "emotional_impact", "interest", "predicted_retention", "hook_strength", "conclusion_strength"}:
            return "editorial_tts_render"
        if category == "pronunciation":
            return "pronunciation_tts_render"
        if category in {"caption_quality", "synchronization", "rhythm", "render_quality", "visual_quality"}:
            return "captions_render"
        return ""

    def _heuristic_editorial_baseline(self, report: Dict[str, Any]) -> Dict[str, int]:
        text_integrity = report.get("text_integrity") if isinstance(report.get("text_integrity"), dict) else {}
        narration_plan = report.get("narration_plan") if isinstance(report.get("narration_plan"), dict) else {}
        source_text = (
            text_integrity.get("approved_final_text")
            or text_integrity.get("official_transcript_text")
            or narration_plan.get("full_text")
            or ""
        )
        normalized_text = self._compact_text(source_text)
        sentences = [
            sentence.strip()
            for sentence in re.findall(r"[^.!?…]+[.!?…]?", normalized_text)
            if sentence and sentence.strip()
        ]
        word_counts = [self._count_words(sentence) for sentence in sentences if self._count_words(sentence) > 0]
        avg_sentence_words = (sum(word_counts) / len(word_counts)) if word_counts else 0.0
        long_sentences = sum(1 for count in word_counts if count >= 24)
        very_long_sentences = sum(1 for count in word_counts if count >= 32)
        comma_count = normalized_text.count(",")
        repeated_phrases = max(0, len(re.findall(r"\b(\w+(?:\s+\w+){1,3})\b(?=.*\b\1\b)", self._fold(normalized_text))))

        grammar = 95 - (very_long_sentences * 2) - long_sentences
        orthography = 96 - min(4, repeated_phrases)
        fluency = 93 - (very_long_sentences * 2) - max(0, long_sentences - 1)
        naturality = 91 - max(0, comma_count - max(2, len(sentences) * 2))
        clarity = 92 - (very_long_sentences * 2) - max(0, long_sentences - 2)
        biblical = 94
        pronunciation = 86

        if avg_sentence_words and avg_sentence_words <= 18:
            fluency += 1
            naturality += 1
            clarity += 1

        return {
            "grammar_score": max(82, min(98, int(round(grammar)))),
            "orthography_score": max(88, min(99, int(round(orthography)))),
            "fluency_score": max(84, min(97, int(round(fluency)))),
            "naturality_score": max(84, min(96, int(round(naturality)))),
            "pronunciation_score": max(84, min(92, int(round(pronunciation)))),
            "biblical_coherence_score": max(88, min(98, int(round(biblical)))),
            "clarity_score": max(84, min(97, int(round(clarity)))),
        }

    def _safe_json_loads(self, raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        candidates = [text]
        if "```json" in text:
            candidates.append(text.split("```json", 1)[1].rsplit("```", 1)[0].strip())
        if "```" in text:
            candidates.append(text.split("```", 1)[1].rsplit("```", 1)[0].strip())
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None

    def _call_ai_json(
        self,
        prompt: str,
        *,
        system_prompt: str,
        temperature: float = 0.2,
        provider_choice: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        generator = self.ai_service
        provider = self._compact_text(provider_choice)
        if provider in {"Disabled", "Local"}:
            return None
        if generator is None:
            return None
        try:
            if hasattr(generator, "_load_config"):
                generator._load_config()
        except Exception:
            pass
        try:
            if hasattr(generator, "ai_router"):
                raw = generator.ai_router.generate_text(
                    user_id=None,
                    task_id=None,
                    video_id=None,
                    capability=AICapability.EDITORIAL_REVIEW,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    json_mode=True,
                )
            elif hasattr(generator, "_generate_text"):
                raw = generator._generate_text(
                    prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    json_mode=True,
                    capability=AICapability.EDITORIAL_REVIEW,
                )
            else:
                return None
        except Exception:
            return None
        return self._safe_json_loads(str(raw or ""))

    def _pronunciation_dictionary_map(self) -> Dict[str, Dict[str, str]]:
        raw = self._read_json_file(self._pronunciation_dictionary_path, {"entries": []})
        entries = raw.get("entries") if isinstance(raw, dict) else []
        resolved: Dict[str, Dict[str, str]] = {}
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                token = self._fold(item.get("token"))
                spoken_form = self._compact_text(item.get("spoken_form"))
                if token and spoken_form:
                    resolved[token] = {
                        "token": self._compact_text(item.get("token")),
                        "spoken_form": spoken_form,
                        "source": self._compact_text(item.get("source")) or "dictionary",
                    }
        return resolved

    def register_pronunciation_entries(self, entries: List[Dict[str, Any]], source: str = "runtime") -> List[Dict[str, str]]:
        current = self._read_json_file(self._pronunciation_dictionary_path, {"updated_at": None, "entries": []})
        existing = current.get("entries") if isinstance(current, dict) else []
        merged: Dict[str, Dict[str, str]] = {}
        if isinstance(existing, list):
            for item in existing:
                if not isinstance(item, dict):
                    continue
                token = self._fold(item.get("token"))
                spoken_form = self._compact_text(item.get("spoken_form"))
                if token and spoken_form:
                    merged[token] = {
                        "token": self._compact_text(item.get("token")),
                        "spoken_form": spoken_form,
                        "source": self._compact_text(item.get("source")) or "dictionary",
                    }
        applied: List[Dict[str, str]] = []
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            token = self._compact_text(item.get("token") or item.get("from"))
            spoken_form = self._compact_text(item.get("spoken_form") or item.get("to"))
            folded = self._fold(token)
            if not folded or not spoken_form:
                continue
            merged[folded] = {
                "token": token,
                "spoken_form": spoken_form,
                "source": source,
            }
            applied.append({"token": token, "spoken_form": spoken_form})
        if applied:
            self._write_json_file(
                self._pronunciation_dictionary_path,
                {
                    "updated_at": datetime.utcnow().isoformat(),
                    "entries": sorted(merged.values(), key=lambda item: self._fold(item.get("token"))),
                },
            )
        return applied

    def normalize_pronunciation_for_tts(self, text: str) -> Dict[str, Any]:
        normalized = self._compact_text(text)
        replacements: List[Dict[str, str]] = []
        if not normalized:
            return {
                "source_text": "",
                "normalized_text": "",
                "replacements": [],
                "replacement_count": 0,
                "dictionary_size": 0,
            }

        dictionary_map = self._pronunciation_dictionary_map()
        for token_key, meta in dictionary_map.items():
            source = self._compact_text(meta.get("token"))
            target = self._compact_text(meta.get("spoken_form"))
            if not source or not target:
                continue
            pattern = re.compile(rf"(?iu)\b{re.escape(source)}\b")
            if pattern.search(normalized):
                normalized = pattern.sub(target, normalized)
                replacements.append(
                    {
                        "from": source,
                        "to": target,
                        "source": self._compact_text(meta.get("source")) or "dictionary",
                    }
                )

        normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return {
            "source_text": self._compact_text(text),
            "normalized_text": normalized,
            "replacements": replacements,
            "replacement_count": len(replacements),
            "dictionary_size": len(dictionary_map),
        }

    def _build_issue(
        self,
        *,
        category: str,
        problem: str,
        justification: str,
        suggestion: str,
        stage: str,
        severity: str = "medium",
        scene_index: Optional[int] = None,
        excerpt: str = "",
    ) -> Dict[str, Any]:
        return {
            "category": category,
            "problem": self._compact_text(problem),
            "justification": self._compact_text(justification),
            "suggestion": self._compact_text(suggestion),
            "stage": stage,
            "severity": severity,
            "scene_index": scene_index,
            "excerpt": self._compact_text(excerpt),
        }

    def _fallback_narration_critic_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        opening_text = self._compact_text(payload.get("opening_text"))
        reflection_text = self._compact_text(payload.get("reflection_text"))
        cta_text = self._compact_text(payload.get("cta_text"))
        scene_texts = [
            self._compact_text(item)
            for item in (payload.get("scene_texts") or [])
            if self._compact_text(item)
        ]
        issues: List[Dict[str, Any]] = []
        all_fragments = [opening_text, *scene_texts, reflection_text, cta_text]
        repeated_counter: Dict[str, int] = {}
        for idx, fragment in enumerate(scene_texts):
            words = self._count_words(fragment)
            if words >= 34:
                issues.append(
                    self._build_issue(
                        category="naturality",
                        problem="Frase longa demais para fala natural.",
                        justification="Trechos muito longos tendem a soar artificiais no TTS.",
                        suggestion="Dividir a ideia em duas frases curtas e mais conversacionais.",
                        stage="editorial_tts_render",
                        severity="medium",
                        scene_index=idx,
                        excerpt=fragment[:180],
                    )
                )
            if re.search(r",\s*,|;\s*;", fragment):
                issues.append(
                    self._build_issue(
                        category="grammar",
                        problem="Pontuação inconsistente no trecho.",
                        justification="Pontuação instável prejudica naturalidade e pausas do TTS.",
                        suggestion="Normalizar vírgulas e remover duplicidades.",
                        stage="editorial_tts_render",
                        severity="medium",
                        scene_index=idx,
                        excerpt=fragment[:180],
                    )
                )
            folded = self._fold(fragment)
            if folded:
                repeated_counter[folded] = repeated_counter.get(folded, 0) + 1
        for fragment, count in repeated_counter.items():
            if count > 1:
                issues.append(
                    self._build_issue(
                        category="narrative_continuity",
                        problem="Trecho repetido entre cenas.",
                        justification="Repetição excessiva torna o vídeo cansativo e reduz impacto.",
                        suggestion="Manter a ideia central, mas variar a formulação das cenas repetidas.",
                        stage="editorial_tts_render",
                        severity="high",
                        excerpt=fragment[:180],
                    )
                )
        for fragment in all_fragments:
            if re.search(r"(?iu)\b(?:melquisedeque|mefibosete|nabucodonosor|quiriate-jearim|bate-seba|ezequias|zorobabel)\b", fragment):
                issues.append(
                    self._build_issue(
                        category="pronunciation",
                        problem="Nome bíblico sensível para pronúncia detectado.",
                        justification="Esse tipo de nome costuma exigir forma falada estável para manter qualidade.",
                        suggestion="Aplicar dicionário persistente de pronúncia antes do TTS.",
                        stage="pronunciation_tts_render",
                        severity="medium",
                        excerpt=fragment[:180],
                    )
                )
                break
        return {
            "provider": "heuristic",
            "summary": (
                "A IA Crítica heurística avaliou o pacote de narração e sinalizou pontos objetivos "
                "de naturalidade, repetição e pronúncia."
            ),
            "issues": issues,
        }

    def criticize_narration_package(self, payload: Dict[str, Any], provider_choice: Optional[str] = None) -> Dict[str, Any]:
        sanitized = {
            "opening_text": self._compact_text(payload.get("opening_text")),
            "scene_texts": [
                self._compact_text(item)
                for item in (payload.get("scene_texts") or [])
                if self._compact_text(item)
            ],
            "reflection_text": self._compact_text(payload.get("reflection_text")),
            "cta_text": self._compact_text(payload.get("cta_text")),
            "channel_name": self._compact_text(payload.get("channel_name")),
            "title": self._compact_text(payload.get("title")),
        }
        if not sanitized["scene_texts"]:
            return self._fallback_narration_critic_report(sanitized)
        if self._compact_text(provider_choice) in {"Local", "Disabled"}:
            report = self._fallback_narration_critic_report(sanitized)
            report["provider_used"] = "Local"
            report["model_used"] = "heuristic"
            return report
        prompt = (
            "Analise este pacote de narração em português do Brasil para um vídeo bíblico. "
            "Você é a IA Crítica e NÃO corrige nada. Apenas encontra defeitos.\n"
            "Procure por: erros ortográficos, erros gramaticais, frases artificiais, repetições, "
            "baixa naturalidade, problemas de pronúncia, risco teológico, clareza fraca, "
            "continuidade ruim e impacto emocional baixo.\n"
            "Retorne somente JSON neste formato:\n"
            "{\n"
            '  "summary": "...",\n'
            '  "issues": [\n'
            "    {\n"
            '      "category": "grammar|orthography|fluency|naturality|pronunciation|biblical_coherence|clarity|narrative_continuity|emotional_impact",\n'
            '      "problem": "...",\n'
            '      "justification": "...",\n'
            '      "suggestion": "...",\n'
            '      "stage": "editorial_tts_render|pronunciation_tts_render",\n'
            '      "severity": "low|medium|high",\n'
            '      "scene_index": 0,\n'
            '      "excerpt": "..."\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"{json.dumps(sanitized, ensure_ascii=False)}"
        )
        reviewed = self._call_ai_json(
            prompt,
            system_prompt=(
                "Você é uma IA Crítica de produção audiovisual bíblica. "
                "Nunca reescreva o texto. Apenas aponte defeitos objetivos em JSON."
            ),
            temperature=0.1,
            provider_choice=provider_choice,
        )
        if not isinstance(reviewed, dict):
            report = self._fallback_narration_critic_report(sanitized)
            report["provider_used"] = "Local"
            report["model_used"] = "heuristic"
            return report
        issues: List[Dict[str, Any]] = []
        for item in reviewed.get("issues") or []:
            if not isinstance(item, dict):
                continue
            issues.append(
                self._build_issue(
                    category=self._compact_text(item.get("category")) or "clarity",
                    problem=item.get("problem") or "Problema não especificado.",
                    justification=item.get("justification") or "",
                    suggestion=item.get("suggestion") or "",
                    stage=self._compact_text(item.get("stage")) or "editorial_tts_render",
                    severity=self._compact_text(item.get("severity")) or "medium",
                    scene_index=item.get("scene_index") if isinstance(item.get("scene_index"), int) else None,
                    excerpt=item.get("excerpt") or "",
                )
            )
        return {
            "provider": "ai",
            "provider_used": self._compact_text(provider_choice) or "AI",
            "model_used": (
                (os.getenv("EDITORIAL_INTELLIGENCE_OPENAI_MODEL") or "gpt-4o-mini").strip()
                if self._compact_text(provider_choice) == "OpenAI"
                else (
                    (os.getenv("EDITORIAL_INTELLIGENCE_GEMINI_MODEL") or "google/gemini-2.5-flash-lite").strip()
                    if self._compact_text(provider_choice) == "Gemini"
                    else (
                        (os.getenv("EDITORIAL_INTELLIGENCE_CLAUDE_MODEL") or "anthropic/claude-3.5-haiku").strip()
                        if self._compact_text(provider_choice) == "Claude"
                        else ""
                    )
                )
            ),
            "summary": self._compact_text(reviewed.get("summary")) or "A IA Crítica concluiu a revisão textual.",
            "issues": issues,
        }

    def _fallback_editor_report(self, payload: Dict[str, Any], critic_report: Dict[str, Any]) -> Dict[str, Any]:
        scene_texts = list(payload.get("scene_texts") or [])
        opening_text = self._compact_text(payload.get("opening_text"))
        reflection_text = self._compact_text(payload.get("reflection_text"))
        cta_text = self._compact_text(payload.get("cta_text"))
        actions: List[Dict[str, Any]] = []
        for issue in critic_report.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            category = self._compact_text(issue.get("category"))
            scene_index = issue.get("scene_index") if isinstance(issue.get("scene_index"), int) else None
            if category in {"grammar", "orthography", "fluency", "naturality", "clarity", "emotional_impact"} and scene_index is not None:
                original = self._compact_text(scene_texts[scene_index]) if scene_index < len(scene_texts) else ""
                if original:
                    edited = re.sub(r"\s*,\s*,+", ", ", original)
                    edited = re.sub(r"\s+", " ", edited).strip()
                    if self._count_words(edited) > 26:
                        edited = edited.replace(", e ", ". E ", 1)
                    scene_texts[scene_index] = edited
                    actions.append(
                        {
                            "stage": "editorial_tts_render",
                            "category": category,
                            "scene_index": scene_index,
                            "action": "rewrote_scene_text",
                        }
                    )
        pronunciation_updates = [
            {
                "token": item.get("excerpt"),
                "spoken_form": item.get("excerpt"),
            }
            for item in (critic_report.get("issues") or [])
            if isinstance(item, dict)
            and self._compact_text(item.get("category")) == "pronunciation"
            and self._compact_text(item.get("excerpt"))
        ]
        return {
            "provider": "heuristic",
            "summary": "A IA Editora aplicou correções conservadoras apenas nos trechos sinalizados.",
            "opening_text": opening_text,
            "scene_texts": scene_texts,
            "reflection_text": reflection_text,
            "cta_text": cta_text,
            "actions": actions,
            "pronunciation_dictionary_updates": pronunciation_updates,
        }

    def edit_narration_package(self, payload: Dict[str, Any], critic_report: Dict[str, Any], provider_choice: Optional[str] = None) -> Dict[str, Any]:
        sanitized = {
            "opening_text": self._compact_text(payload.get("opening_text")),
            "scene_texts": [
                self._compact_text(item)
                for item in (payload.get("scene_texts") or [])
                if self._compact_text(item)
            ],
            "reflection_text": self._compact_text(payload.get("reflection_text")),
            "cta_text": self._compact_text(payload.get("cta_text")),
            "channel_name": self._compact_text(payload.get("channel_name")),
            "title": self._compact_text(payload.get("title")),
        }
        if not sanitized["scene_texts"]:
            return self._fallback_editor_report(sanitized, critic_report)
        if self._compact_text(provider_choice) in {"Local", "Disabled"}:
            reviewed = self._fallback_editor_report(sanitized, critic_report)
            reviewed["provider_used"] = "Local"
            reviewed["model_used"] = "heuristic"
            return reviewed
        issues = critic_report.get("issues") if isinstance(critic_report, dict) else []
        prompt = (
            "Você é a IA Editora de um pipeline cinematográfico bíblico.\n"
            "Receberá o roteiro e o relatório da IA Crítica.\n"
            "Corrija SOMENTE os problemas apontados. Não altere partes aprovadas. "
            "Mantenha o mesmo número de itens em scene_texts.\n"
            "Retorne JSON neste formato:\n"
            "{\n"
            '  "summary": "...",\n'
            '  "opening_text": "...",\n'
            '  "scene_texts": ["..."],\n'
            '  "reflection_text": "...",\n'
            '  "cta_text": "...",\n'
            '  "actions": [{"stage": "...", "category": "...", "scene_index": 0, "action": "..."}],\n'
            '  "pronunciation_dictionary_updates": [{"token": "...", "spoken_form": "..."}]\n'
            "}\n\n"
            f"ROTEIRO:\n{json.dumps(sanitized, ensure_ascii=False)}\n\n"
            f"RELATORIO_CRITICO:\n{json.dumps({'issues': issues}, ensure_ascii=False)}"
        )
        reviewed = self._call_ai_json(
            prompt,
            system_prompt=(
                "Você é um editor audiovisual sênior. Faça correções cirúrgicas e preserve o que já está aprovado. "
                "Retorne somente JSON válido."
            ),
            temperature=0.15,
            provider_choice=provider_choice,
        )
        if not isinstance(reviewed, dict):
            reviewed = self._fallback_editor_report(sanitized, critic_report)

        scene_texts = reviewed.get("scene_texts")
        if not isinstance(scene_texts, list) or len(scene_texts) != len(sanitized["scene_texts"]):
            scene_texts = sanitized["scene_texts"]
        pronunciation_updates = []
        for item in reviewed.get("pronunciation_dictionary_updates") or []:
            if not isinstance(item, dict):
                continue
            token = self._compact_text(item.get("token"))
            spoken_form = self._compact_text(item.get("spoken_form"))
            if token and spoken_form:
                pronunciation_updates.append({"token": token, "spoken_form": spoken_form})
        applied_updates = self.register_pronunciation_entries(pronunciation_updates, source="editor")
        return {
            "provider": self._compact_text(reviewed.get("provider")) or ("ai" if isinstance(reviewed, dict) else "heuristic"),
            "provider_used": self._compact_text(provider_choice) or self._compact_text(reviewed.get("provider_used")) or "Local",
            "model_used": self._compact_text(reviewed.get("model_used") or critic_report.get("model_used")),
            "summary": self._compact_text(reviewed.get("summary")) or "A IA Editora concluiu as correções direcionadas.",
            "opening_text": self._compact_text(reviewed.get("opening_text") or sanitized["opening_text"]),
            "scene_texts": [self._compact_text(item) for item in scene_texts],
            "reflection_text": self._compact_text(reviewed.get("reflection_text") or sanitized["reflection_text"]),
            "cta_text": self._compact_text(reviewed.get("cta_text") or sanitized["cta_text"]),
            "actions": [
                {
                    "stage": self._compact_text(item.get("stage")) or "editorial_tts_render",
                    "category": self._compact_text(item.get("category")) or "clarity",
                    "scene_index": item.get("scene_index") if isinstance(item.get("scene_index"), int) else None,
                    "action": self._compact_text(item.get("action")) or "updated_text",
                }
                for item in (reviewed.get("actions") or [])
                if isinstance(item, dict)
            ],
            "pronunciation_dictionary_updates": applied_updates,
        }

    def review_narration_package(self, payload: Dict[str, Any], provider_choice: Optional[str] = None) -> Dict[str, Any]:
        critic_report = self.criticize_narration_package(payload, provider_choice=provider_choice)
        editor_report = self.edit_narration_package(payload, critic_report, provider_choice=provider_choice)
        result = {
            "provider": self._compact_text(editor_report.get("provider")) or "heuristic",
            "provider_used": self._compact_text(editor_report.get("provider_used") or critic_report.get("provider_used") or provider_choice) or "Local",
            "model_used": self._compact_text(editor_report.get("model_used") or critic_report.get("model_used")) or "heuristic",
            "opening_text": self._compact_text(editor_report.get("opening_text") or payload.get("opening_text")),
            "scene_texts": [
                self._compact_text(item)
                for item in (editor_report.get("scene_texts") or payload.get("scene_texts") or [])
                if self._compact_text(item)
            ],
            "reflection_text": self._compact_text(editor_report.get("reflection_text") or payload.get("reflection_text")),
            "cta_text": self._compact_text(editor_report.get("cta_text") or payload.get("cta_text")),
            "issues": [item.get("problem") for item in (critic_report.get("issues") or []) if isinstance(item, dict)],
            "critic_report": critic_report,
            "editor_report": editor_report,
            "corrections_applied": bool(editor_report.get("actions")),
        }
        category_scores = {
            "grammar": 96,
            "orthography": 98,
            "fluency": 92,
            "naturality": 91,
            "pronunciation": 90,
            "biblical_coherence": 96,
            "clarity": 92,
            "narrative_continuity": 91,
            "emotional_impact": 88,
        }
        for issue in critic_report.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            category = self._compact_text(issue.get("category"))
            severity = self._compact_text(issue.get("severity")) or "medium"
            penalty = 4 if severity == "low" else 7 if severity == "medium" else 12
            if category in category_scores:
                category_scores[category] = max(58, category_scores[category] - penalty)
        result["grammar_score"] = category_scores["grammar"]
        result["fluency_score"] = category_scores["fluency"]
        result["pronunciation_score"] = min(100, category_scores["pronunciation"] + min(4, len(editor_report.get("pronunciation_dictionary_updates") or [])))
        result["biblical_coherence_score"] = category_scores["biblical_coherence"]
        result["naturality_score"] = category_scores["naturality"]
        result["clarity_score"] = category_scores["clarity"]
        result["orthography_score"] = category_scores["orthography"]
        return result

    def _dimension_record(self, key: str, score: int, justification: str, suggestions: List[str]) -> Dict[str, Any]:
        return {
            "label": self.DIMENSION_LABELS.get(key, key),
            "score": max(0, min(100, int(score))),
            "justification": self._compact_text(justification),
            "suggestions": [self._compact_text(item) for item in (suggestions or []) if self._compact_text(item)],
        }

    def _average_score(self, values: List[Any], default: int = 0) -> int:
        numbers: List[float] = []
        for item in values or []:
            try:
                numbers.append(float(item))
            except Exception:
                continue
        if not numbers:
            return int(default)
        return int(round(sum(numbers) / max(1, len(numbers))))

    def critique_audio(
        self,
        audio_payload: Optional[Dict[str, Any]],
        official_transcription: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        audio = audio_payload if isinstance(audio_payload, dict) else {}
        transcription = official_transcription if isinstance(official_transcription, dict) else {}
        comparison = transcription.get("comparison") if isinstance(transcription.get("comparison"), dict) else {}
        metrics = transcription.get("audio_metrics") if isinstance(transcription.get("audio_metrics"), dict) else {}
        issues: List[Dict[str, Any]] = []

        similarity = self._safe_float(comparison.get("similarity_ratio"), 0.0)
        within_tolerance = bool(comparison.get("within_tolerance"))
        words_per_minute = self._safe_float(metrics.get("words_per_minute"), 0.0)
        pauses_over_limit = self._safe_int(metrics.get("pauses_over_limit"), 0)
        longest_pause = self._safe_float(metrics.get("longest_pause_sec"), 0.0)
        fallback_used = bool(audio.get("fallback_used"))

        if not within_tolerance:
            issues.append(
                self._build_issue(
                    category="text_audio_alignment",
                    problem="A transcrição oficial do áudio divergiu do texto final aprovado.",
                    justification=f"Similaridade palavra a palavra abaixo da tolerância configurada ({similarity:.3f}).",
                    suggestion="Corrigir apenas texto/pronúncia sensível e regenerar somente o áudio antes do render.",
                    stage="editorial_tts_render",
                    severity="high",
                )
            )
        if fallback_used:
            issues.append(
                self._build_issue(
                    category="pronunciation",
                    problem="A geração de áudio caiu em provider de fallback.",
                    justification="Fallback costuma reduzir estabilidade de pronúncia, entonação e naturalidade.",
                    suggestion="Tentar novamente o TTS premium antes de seguir para o render.",
                    stage="pronunciation_tts_render",
                    severity="high",
                )
            )
        if words_per_minute and (words_per_minute < 112 or words_per_minute > 182):
            issues.append(
                self._build_issue(
                    category="rhythm",
                    problem="Velocidade de fala fora da faixa confortável.",
                    justification=f"O áudio foi estimado em {words_per_minute:.1f} palavras por minuto.",
                    suggestion="Ajustar ritmo da narração e regenerar apenas o áudio.",
                    stage="pronunciation_tts_render",
                    severity="medium",
                )
            )
        if pauses_over_limit >= 2 or longest_pause >= 1.8:
            issues.append(
                self._build_issue(
                    category="rhythm",
                    problem="Pausas longas demais foram detectadas na narração.",
                    justification=(
                        f"Foram detectadas {pauses_over_limit} pausas acima do limite, "
                        f"com maior pausa de {longest_pause:.2f}s."
                    ),
                    suggestion="Suavizar pausas e manter a cadência antes de renderizar o vídeo.",
                    stage="pronunciation_tts_render",
                    severity="medium",
                )
            )

        blocking_issues = [
            item.get("problem")
            for item in issues
            if isinstance(item, dict) and self._compact_text(item.get("severity")) == "high"
        ]
        approved = not blocking_issues
        return {
            "provider": "heuristic",
            "summary": (
                "A IA Crítica de Áudio aprovou a narração para seguir ao render."
                if approved
                else "A IA Crítica de Áudio reteve a narração para ajuste antes do render."
            ),
            "approved_for_render": approved,
            "issues": issues,
            "blocking_issues": blocking_issues,
            "metrics": metrics,
            "transcription_similarity": round(similarity, 4),
        }

    def director_review(
        self,
        render_report: Optional[Dict[str, Any]],
        quality_report: Optional[Dict[str, Any]],
        critic_review: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        report = render_report if isinstance(render_report, dict) else {}
        quality = quality_report if isinstance(quality_report, dict) else {}
        critic = critic_review if isinstance(critic_review, dict) else {}
        weak_dimensions = [
            key for key, value in ((quality.get("dimensions") or {}).items() if isinstance(quality.get("dimensions"), dict) else [])
            if self._safe_int(value, 0) < 90
        ]
        suggestions: List[Dict[str, Any]] = []
        for key in weak_dimensions[:6]:
            if key in {"rhythm", "synchronization"}:
                suggestions.append({"priority": "alta", "suggestion": "Reduzir o tempo de cena e recalcular a sincronização pelo áudio real.", "dimension": key})
            elif key in {"visual_quality", "render_quality"}:
                suggestions.append({"priority": "alta", "suggestion": "Trocar imagens repetidas e refinar a finalização visual antes da publicação.", "dimension": key})
            elif key in {"emotional_impact", "interest", "predicted_retention"}:
                suggestions.append({"priority": "media", "suggestion": "Aumentar suspense, pausa dramática e progressão emocional do vídeo.", "dimension": key})
            elif key in {"hook_strength", "conclusion_strength"}:
                suggestions.append({"priority": "media", "suggestion": "Reforçar a abertura e a conclusão para melhorar retenção prevista.", "dimension": key})
            else:
                suggestions.append({"priority": "media", "suggestion": "Refinar apenas a etapa correspondente ao problema detectado e reavaliar.", "dimension": key})
        for issue in (critic.get("issues") or []):
            if isinstance(issue, dict) and len(suggestions) < 8:
                suggestions.append(
                    {
                        "priority": "alta" if self._compact_text(issue.get("severity")) == "high" else "media",
                        "suggestion": self._compact_text(issue.get("suggestion")) or self._compact_text(issue.get("problem")),
                        "dimension": self._normalize_category_key(issue.get("category")),
                    }
                )
        return {
            "provider": "heuristic",
            "summary": "A IA Diretora consolidou as melhores ações para aumentar a qualidade percebida do vídeo.",
            "suggestions": suggestions[:8],
            "target_quality_score": self.MINIMUM_OVERALL_SCORE,
            "current_quality_score": self._safe_int(quality.get("quality_score_final") or quality.get("overall_score"), 0),
            "scene_count": self._safe_int(len(report.get("scene_visuals") or []), 0),
        }

    def build_detailed_quality_report(
        self,
        render_report: Optional[Dict[str, Any]],
        editorial_review: Optional[Dict[str, Any]] = None,
        critic_review: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        report = render_report if isinstance(render_report, dict) else {}
        editorial = editorial_review if isinstance(editorial_review, dict) else {}
        critic = critic_review if isinstance(critic_review, dict) else {}
        sync = report.get("sync_validation") if isinstance(report.get("sync_validation"), dict) else {}
        visual = report.get("visual_plan") if isinstance(report.get("visual_plan"), dict) else {}
        audio = report.get("audio_generation") if isinstance(report.get("audio_generation"), dict) else {}
        text_integrity = report.get("text_integrity") if isinstance(report.get("text_integrity"), dict) else {}
        pronunciation = report.get("pronunciation_normalization") if isinstance(report.get("pronunciation_normalization"), dict) else {}
        official_transcription = report.get("official_audio_transcription") if isinstance(report.get("official_audio_transcription"), dict) else {}
        audio_critic = report.get("audio_critic_review") if isinstance(report.get("audio_critic_review"), dict) else {}
        critic_issues = [item for item in (critic.get("issues") or []) if isinstance(item, dict)]
        heuristic_editorial = self._heuristic_editorial_baseline(report)

        def issue_count(*categories: str) -> int:
            valid = {self._normalize_category_key(item) for item in categories if self._compact_text(item)}
            return sum(1 for issue in critic_issues if self._normalize_category_key(issue.get("category")) in valid)

        transcript_similarity = self._safe_float(text_integrity.get("official_transcript_similarity_ratio"), self._safe_float((official_transcription.get("comparison") or {}).get("similarity_ratio"), 0.0))
        captions_match_official = bool(text_integrity.get("captions_match_official_transcript"))
        approved_text_matches_official = bool(text_integrity.get("approved_text_matches_official_transcript"))
        caption_timeline_source = self._compact_text(sync.get("timeline_source") or sync.get("caption_timeline_source"))
        scene_visuals = report.get("scene_visuals") if isinstance(report.get("scene_visuals"), list) else []
        unique_image_paths = {
            self._compact_text(item.get("image_path"))
            for item in scene_visuals
            if isinstance(item, dict) and self._compact_text(item.get("image_path"))
        }
        repeated_visuals = max(0, len(scene_visuals) - len(unique_image_paths))
        text_audio_alignment_score = 76
        if approved_text_matches_official:
            text_audio_alignment_score = 100
        elif transcript_similarity > 0:
            text_audio_alignment_score = max(40, min(98, int(round(transcript_similarity * 100))))
        text_audio_alignment_score = max(35, text_audio_alignment_score - (issue_count("text_audio_alignment") * 10))

        grammar_score = int(editorial.get("grammar_score") or heuristic_editorial.get("grammar_score") or 94)
        orthography_score = int(editorial.get("orthography_score") or heuristic_editorial.get("orthography_score") or max(grammar_score, 94))
        fluency_score = int(editorial.get("fluency_score") or heuristic_editorial.get("fluency_score") or 92)
        naturality_score = int(editorial.get("naturality_score") or heuristic_editorial.get("naturality_score") or max(88, fluency_score - 1))
        pronunciation_score = int(editorial.get("pronunciation_score") or heuristic_editorial.get("pronunciation_score") or 86)
        biblical_score = int(editorial.get("biblical_coherence_score") or heuristic_editorial.get("biblical_coherence_score") or 94)
        clarity_score = int(editorial.get("clarity_score") or heuristic_editorial.get("clarity_score") or 92)
        continuity_score = 90 if report.get("story_completed") else 72
        emotional_score = 88 if self._safe_float((report.get("duration_plan") or {}).get("actual_audio_duration_sec"), 0.0) >= 40 else 80

        grammar_score = max(55, grammar_score - (issue_count("grammar") * 5))
        orthography_score = max(60, orthography_score - (issue_count("orthography") * 6))
        fluency_score = max(55, fluency_score - (issue_count("fluency") * 5))
        naturality_score = max(55, naturality_score - (issue_count("naturality") * 5))
        biblical_score = max(55, biblical_score - (issue_count("biblical_coherence") * 8))
        clarity_score = max(55, clarity_score - (issue_count("clarity") * 5))
        continuity_score = max(55, continuity_score - (issue_count("narrative_continuity") * 7))
        emotional_score = max(55, emotional_score - (issue_count("emotional_impact") * 5))

        if audio.get("provider_used") in {"elevenlabs", "openai_tts"}:
            pronunciation_score = min(100, pronunciation_score + 4)
        if audio.get("fallback_used"):
            pronunciation_score = max(55, pronunciation_score - 10)
        pronunciation_score = min(100, pronunciation_score + min(4, self._safe_int(pronunciation.get("replacement_count"), 0)))
        pronunciation_score = max(45, pronunciation_score - (len(audio_critic.get("issues") or []) * 2))

        caption_score = 72
        if captions_match_official and caption_timeline_source == "official_audio_transcript":
            caption_score = 100
        elif captions_match_official:
            caption_score = 94
        elif transcript_similarity > 0:
            caption_score = max(40, min(96, int(round(transcript_similarity * 96))))
        if not sync.get("captions_synced_with_audio"):
            caption_score = max(35, caption_score - 18)
        if caption_timeline_source != "official_audio_transcript":
            caption_score = max(35, caption_score - 14)

        sync_score = 72
        audio_caption_diff = self._safe_float(sync.get("audio_caption_diff_sec"), 0.0)
        audio_video_diff = self._safe_float(sync.get("audio_video_diff_sec"), 0.0)
        worst_sync_diff = max(audio_caption_diff, audio_video_diff)
        canonical_sync_verified = bool(
            sync.get("captions_synced_with_audio")
            and sync.get("video_synced_with_audio")
            and caption_timeline_source == "official_audio_transcript"
        )
        if canonical_sync_verified:
            sync_score = 100
        elif sync.get("captions_synced_with_audio") and sync.get("video_synced_with_audio"):
            sync_score = 96
        else:
            sync_score = max(45, min(96, int(round(96 - (worst_sync_diff * 28)))))
        risky_blocks = self._safe_int(((sync.get("caption_block_sync") or {}).get("blocks_with_risk_of_drift")), 0)
        if not canonical_sync_verified:
            sync_score = max(40, sync_score - min(18, risky_blocks * 2))

        rhythm_score = 88
        average_image_duration = self._safe_float(visual.get("average_image_duration_sec"), 0.0)
        if average_image_duration and average_image_duration < 2.2:
            rhythm_score -= 10
        elif average_image_duration and average_image_duration > 18.0:
            rhythm_score -= 10
        elif average_image_duration and average_image_duration > 16.5:
            rhythm_score -= 6
        if risky_blocks > 0 and not canonical_sync_verified:
            rhythm_score -= min(10, risky_blocks * 2)
        rhythm_score = max(50, min(100, rhythm_score))

        render_score = 82
        if report.get("story_completed") and report.get("end_screen_rendered"):
            render_score += 10
        if not report.get("unexpected_extra_video_created"):
            render_score += 4
        if not report.get("plain_background_detected_at_end"):
            render_score += 4
        render_score = max(45, min(100, render_score))

        visual_score = 78
        generated_images = self._safe_int(visual.get("generated_image_count"), 0)
        reused_images = self._safe_int(visual.get("reused_image_count"), 0)
        if generated_images > 0:
            visual_score += min(12, generated_images)
        if not report.get("plain_background_detected_at_end"):
            visual_score += 6
        if report.get("unexpected_extra_video_created"):
            visual_score -= 20
        if reused_images > generated_images and generated_images > 0:
            visual_score -= 4
        if repeated_visuals > 0:
            visual_score -= min(12, repeated_visuals * 2)
        visual_score = max(45, min(100, visual_score))

        interest_score = max(50, min(100, self._average_score([naturality_score, rhythm_score, visual_score, emotional_score], 82) - (issue_count("interest") * 5)))
        predicted_retention_score = max(50, min(100, self._average_score([interest_score, hook_strength := 90 if bool((report.get("narration_plan") or {}).get("opening_text")) else 72, conclusion_strength := 90 if bool((report.get("narration_plan") or {}).get("closing_text")) or bool(report.get("end_screen_rendered")) else 74, rhythm_score], 82) - (issue_count("predicted_retention") * 5)))
        hook_strength = max(55, min(100, hook_strength - (issue_count("hook_strength") * 6)))
        conclusion_strength = max(55, min(100, conclusion_strength - (issue_count("conclusion_strength") * 6)))

        blocking_issues: List[str] = []
        if not approved_text_matches_official:
            blocking_issues.append("áudio final divergiu do texto aprovado")
        if not sync.get("captions_synced_with_audio"):
            blocking_issues.append("legendas fora do áudio final")
        if not sync.get("video_synced_with_audio"):
            blocking_issues.append("vídeo fora da narração final")
        if not captions_match_official:
            blocking_issues.append("legendas divergiram da transcrição oficial")
        if audio.get("fallback_used"):
            blocking_issues.append("TTS caiu em fallback")
        if issue_count("biblical_coherence") > 0:
            blocking_issues.append("revisão bíblica encontrou inconsistências")
        if not bool(audio_critic.get("approved_for_render", True)):
            blocking_issues.append("IA crítica de áudio reteve a narração")

        detailed_dimensions = {
            "text_audio_alignment": self._dimension_record(
                "text_audio_alignment",
                text_audio_alignment_score,
                "Compara o texto final aprovado com a transcrição oficial do áudio final.",
                ["Corrigir divergências entre texto e áudio antes do render.", "Regenerar apenas TTS quando a equivalência ficar abaixo da tolerância."],
            ),
            "grammar": self._dimension_record(
                "grammar",
                grammar_score,
                "Avalia concordância, estrutura frasal e pontuação do texto final narrado.",
                ["Revisar frases com pontuação excessiva.", "Eliminar construções quebradas ou ambíguas."],
            ),
            "orthography": self._dimension_record(
                "orthography",
                orthography_score,
                "Mede a ausência de erros ortográficos e grafia inconsistente.",
                ["Corrigir grafias divergentes.", "Padronizar nomes próprios e referências."],
            ),
            "fluency": self._dimension_record(
                "fluency",
                fluency_score,
                "Mede o encadeamento natural das ideias para leitura em voz alta.",
                ["Enxugar conectivos excessivos.", "Quebrar períodos muito longos."],
            ),
            "naturality": self._dimension_record(
                "naturality",
                naturality_score,
                "Mede o quanto o texto soa humano e conversacional quando narrado.",
                ["Trocar formulações artificiais por fala natural.", "Reduzir repetições mecânicas."],
            ),
            "pronunciation": self._dimension_record(
                "pronunciation",
                pronunciation_score,
                "Mede a estabilidade da fala para nomes bíblicos, siglas e termos sensíveis.",
                ["Atualizar dicionário persistente de pronúncia.", "Regerar apenas TTS quando a fala soar artificial."],
            ),
            "caption_quality": self._dimension_record(
                "caption_quality",
                caption_score,
                "Mede se as legendas nasceram da transcrição oficial do áudio final e permanecem equivalentes ao narrado.",
                ["Usar somente a transcrição oficial do áudio como base das legendas.", "Refazer somente as legendas quando houver divergência textual."],
            ),
            "synchronization": self._dimension_record(
                "synchronization",
                sync_score,
                "Compara duração do áudio, das legendas e do vídeo final.",
                ["Refazer alinhamento a partir do áudio final.", "Regerar somente legendas e render quando houver drift."],
            ),
            "rhythm": self._dimension_record(
                "rhythm",
                rhythm_score,
                "Avalia o ritmo visual e narrativo do vídeo final.",
                ["Ajustar duração das cenas pelo áudio real.", "Evitar trocas rápidas ou cenas excessivamente longas."],
            ),
            "render_quality": self._dimension_record(
                "render_quality",
                render_score,
                "Avalia acabamento final do render, presença de encerramento e consistência da saída final.",
                ["Executar somente pós-produção quando a finalização ficar abaixo do padrão.", "Garantir encerramento visual completo e sem cortes abruptos."],
            ),
            "visual_quality": self._dimension_record(
                "visual_quality",
                visual_score,
                "Mede coerência visual, variedade e acabamento do vídeo.",
                ["Reutilizar imagens só quando fizer sentido narrativo.", "Melhorar fundos ou cenas visualmente fracas."],
            ),
            "biblical_coherence": self._dimension_record(
                "biblical_coherence",
                biblical_score,
                "Avalia fidelidade ao sentido bíblico e coerência teológica.",
                ["Corrigir inferências soltas.", "Preservar contexto e sentido das referências citadas."],
            ),
            "narrative_continuity": self._dimension_record(
                "narrative_continuity",
                continuity_score,
                "Mede continuidade entre cenas, progressão e ausência de rupturas bruscas.",
                ["Eliminar repetições de cena.", "Conectar melhor transições narrativas."],
            ),
            "clarity": self._dimension_record(
                "clarity",
                clarity_score,
                "Avalia clareza de entendimento para o espectador.",
                ["Trocar frases ambíguas por declarações diretas.", "Simplificar trechos densos sem perder profundidade."],
            ),
            "emotional_impact": self._dimension_record(
                "emotional_impact",
                emotional_score,
                "Mede presença de emoção, tensão, alívio ou reverência na experiência final.",
                ["Reforçar o arco emocional.", "Ajustar linguagem para maior envolvimento do espectador."],
            ),
            "interest": self._dimension_record(
                "interest",
                interest_score,
                "Estima o quanto o vídeo mantém curiosidade e desejo de continuar assistindo.",
                ["Criar mais progressão dramática entre cenas.", "Variar linguagem e enquadramento emocional."],
            ),
            "predicted_retention": self._dimension_record(
                "predicted_retention",
                predicted_retention_score,
                "Estima retenção com base em ritmo, abertura, conclusão e cadência narrativa.",
                ["Aprimorar quebra de padrão e progressão das cenas.", "Fortalecer transições e payoff final."],
            ),
            "hook_strength": self._dimension_record(
                "hook_strength",
                hook_strength,
                "Mede a força da abertura em prender atenção nos primeiros segundos.",
                ["Abrir com conflito, tensão ou pergunta forte.", "Evitar início morno na locução inicial."],
            ),
            "conclusion_strength": self._dimension_record(
                "conclusion_strength",
                conclusion_strength,
                "Mede a força do fechamento emocional e da conclusão do vídeo.",
                ["Conectar a conclusão ao arco emocional.", "Finalizar com fechamento memorável e coerente."],
            ),
        }

        weighted_total = 0.0
        weight_total = 0.0
        numeric_dimensions: Dict[str, int] = {}
        for key, record in detailed_dimensions.items():
            numeric_dimensions[key] = int(record.get("score") or 0)
            weight = float(self.DIMENSION_WEIGHTS.get(key, 0.0))
            weighted_total += float(record.get("score") or 0) * weight
            weight_total += weight
        if weight_total <= 0:
            weight_total = 1.0
        weighted_total = weighted_total / weight_total
        quality_score_final = int(round(max(0.0, min(100.0, weighted_total))))
        technical_dimensions = {
            "orthography": numeric_dimensions.get("orthography", 0),
            "grammar": numeric_dimensions.get("grammar", 0),
            "pronunciation": numeric_dimensions.get("pronunciation", 0),
            "caption_quality": numeric_dimensions.get("caption_quality", 0),
            "synchronization": numeric_dimensions.get("synchronization", 0),
            "biblical_coherence": numeric_dimensions.get("biblical_coherence", 0),
            "render_quality": numeric_dimensions.get("render_quality", 0),
            "visual_quality": numeric_dimensions.get("visual_quality", 0),
            "text_audio_alignment": numeric_dimensions.get("text_audio_alignment", 0),
        }
        editorial_dimensions = {
            "naturality": numeric_dimensions.get("naturality", 0),
            "fluency": numeric_dimensions.get("fluency", 0),
            "rhythm": numeric_dimensions.get("rhythm", 0),
            "emotional_impact": numeric_dimensions.get("emotional_impact", 0),
            "interest": numeric_dimensions.get("interest", 0),
            "predicted_retention": numeric_dimensions.get("predicted_retention", 0),
            "hook_strength": numeric_dimensions.get("hook_strength", 0),
            "conclusion_strength": numeric_dimensions.get("conclusion_strength", 0),
        }
        technical_score = self._average_score(list(technical_dimensions.values()), quality_score_final)
        editorial_score = self._average_score(list(editorial_dimensions.values()), quality_score_final)
        below_minimum = [
            key
            for key, minimum in self.MINIMUM_DIMENSION_SCORES.items()
            if int(numeric_dimensions.get(key) or 0) < int(minimum)
        ]
        approved = bool(
            quality_score_final >= self.MINIMUM_OVERALL_SCORE
            and technical_score >= self.MINIMUM_TECHNICAL_SCORE
            and editorial_score >= self.MINIMUM_EDITORIAL_SCORE
            and not below_minimum
            and not blocking_issues
        )
        return {
            "dimensions": numeric_dimensions,
            "detailed_dimensions": detailed_dimensions,
            "technical_score": technical_score,
            "editorial_score": editorial_score,
            "technical_dimensions": technical_dimensions,
            "editorial_dimensions": editorial_dimensions,
            "quality_score_final": quality_score_final,
            "overall_score": quality_score_final,
            "minimum_overall_score": self.MINIMUM_OVERALL_SCORE,
            "minimum_technical_score": self.MINIMUM_TECHNICAL_SCORE,
            "minimum_editorial_score": self.MINIMUM_EDITORIAL_SCORE,
            "minimum_dimension_scores": dict(self.MINIMUM_DIMENSION_SCORES),
            "below_minimum_dimensions": below_minimum,
            "blocking_issues": blocking_issues,
            "approved_for_publication": approved,
        }

    def score_render_quality(
        self,
        render_report: Optional[Dict[str, Any]],
        editorial_review: Optional[Dict[str, Any]] = None,
        critic_review: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.build_detailed_quality_report(
            render_report=render_report,
            editorial_review=editorial_review,
            critic_review=critic_review,
        )

    def critique_video(
        self,
        render_report: Optional[Dict[str, Any]],
        quality_report: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        report = render_report if isinstance(render_report, dict) else {}
        quality = quality_report if isinstance(quality_report, dict) else {}
        prompt = {
            "title": ((report.get("original_script") or {}).get("title") if isinstance(report.get("original_script"), dict) else None),
            "quality_report": quality,
            "sync_validation": report.get("sync_validation"),
            "audio_generation": {
                "provider_used": ((report.get("audio_generation") or {}).get("provider_used") if isinstance(report.get("audio_generation"), dict) else None),
                "fallback_used": ((report.get("audio_generation") or {}).get("fallback_used") if isinstance(report.get("audio_generation"), dict) else None),
            },
            "visual_plan": report.get("visual_plan"),
            "scene_visuals": report.get("scene_visuals"),
        }
        reviewed = self._call_ai_json(
            (
                "Você é a IA Crítica de Vídeo pós-render de um pipeline cinematográfico bíblico.\n"
                "Você NÃO corrige. Apenas encontra defeitos no resultado final.\n"
                "Avalie especificamente: transições, ritmo visual, repetição de imagens, permanência das cenas, "
                "coerência entre fala e imagem, impacto emocional e acabamento de pós-produção.\n"
                "Retorne JSON neste formato:\n"
                "{\n"
                '  "summary": "...",\n'
                '  "issues": [{"category": "...", "problem": "...", "justification": "...", "suggestion": "...", "stage": "...", "severity": "low|medium|high"}],\n'
                '  "strengths": ["..."],\n'
                '  "recommended_action": "publish|hold"\n'
                "}\n\n"
                f"{json.dumps(prompt, ensure_ascii=False)}"
            ),
            system_prompt=(
                "Você é um diretor crítico severo, focado em qualidade cinematográfica, sincronização, pronúncia, ritmo e fidelidade bíblica. "
                "Retorne somente JSON válido."
            ),
            temperature=0.1,
        )
        if isinstance(reviewed, dict):
            issues = []
            for item in reviewed.get("issues") or []:
                if not isinstance(item, dict):
                    continue
                issues.append(
                    self._build_issue(
                        category=item.get("category") or "visual_quality",
                        problem=item.get("problem") or "Problema não especificado.",
                        justification=item.get("justification") or "",
                        suggestion=item.get("suggestion") or "",
                        stage=item.get("stage") or "captions_render",
                        severity=item.get("severity") or "medium",
                    )
                )
            approved = bool(
                quality.get("approved_for_publication")
                and self._compact_text(reviewed.get("recommended_action")) != "hold"
                and not issues
            )
            return {
                "provider": "ai",
                "approved_for_publication": approved,
                "summary": self._compact_text(reviewed.get("summary")) or "A IA Crítica concluiu a revisão pós-render.",
                "issues": issues,
                "blocking_issues": [item.get("problem") for item in issues],
                "strengths": [
                    self._compact_text(item)
                    for item in (reviewed.get("strengths") or [])
                    if self._compact_text(item)
                ],
                "recommended_action": self._compact_text(reviewed.get("recommended_action")) or ("publish" if approved else "hold"),
            }

        issues = []
        if not quality.get("approved_for_publication"):
            for label in quality.get("below_minimum_dimensions") or []:
                stage = "captions_render"
                if label in {"text_audio_alignment", "grammar", "orthography", "fluency", "naturality", "clarity", "biblical_coherence", "narrative_continuity", "emotional_impact", "interest", "predicted_retention", "hook_strength", "conclusion_strength"}:
                    stage = "editorial_tts_render"
                elif label == "pronunciation":
                    stage = "pronunciation_tts_render"
                elif label in {"caption_quality", "synchronization", "rhythm", "render_quality"}:
                    stage = "captions_render"
                elif label == "visual_quality":
                    stage = "thumbnail_publish"
                issues.append(
                    self._build_issue(
                        category=label,
                        problem=f"Dimensão {self.DIMENSION_LABELS.get(label, label)} abaixo da meta mínima.",
                        justification="O score detalhado final reteve a publicação automática.",
                        suggestion="Executar apenas a etapa defeituosa e reavaliar antes de publicar.",
                        stage=stage,
                        severity="high",
                    )
                )
        return {
            "provider": "heuristic",
            "approved_for_publication": bool(quality.get("approved_for_publication")) and not issues,
            "summary": (
                "A IA Crítica heurística aprovou a publicação."
                if bool(quality.get("approved_for_publication")) and not issues
                else "A IA Crítica heurística reteve a publicação por qualidade abaixo do padrão mínimo."
            ),
            "issues": issues,
            "blocking_issues": [item.get("problem") for item in issues],
            "strengths": [
                "sincronização validada" if ((quality.get("dimensions") or {}).get("synchronization", 0) >= 92) else "",
                "áudio premium utilizado" if ((report.get("audio_generation") or {}).get("provider_used") in {"elevenlabs", "openai_tts"}) else "",
            ],
            "recommended_action": "publish" if bool(quality.get("approved_for_publication")) and not issues else "hold",
        }

    def plan_auto_recovery(
        self,
        quality_report: Optional[Dict[str, Any]],
        critic_review: Optional[Dict[str, Any]],
        attempt_state: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        quality = quality_report if isinstance(quality_report, dict) else {}
        critic = critic_review if isinstance(critic_review, dict) else {}
        attempts = attempt_state if isinstance(attempt_state, dict) else {}
        if bool(quality.get("approved_for_publication")) and bool(critic.get("approved_for_publication", True)):
            return {
                "action": "publish",
                "stage": None,
                "reason": "",
                "supported": True,
                "attempts_for_stage": 0,
                "max_attempts": self.MAX_AUTO_RECOVERY_ATTEMPTS_PER_STAGE,
            }

        stage_priority = [
            ("editorial_tts_render", {"text_audio_alignment"}),
            ("editorial_tts_render", {"orthography"}),
            ("editorial_tts_render", {"grammar"}),
            ("editorial_tts_render", {"naturality"}),
            ("pronunciation_tts_render", {"pronunciation"}),
            ("editorial_tts_render", {"clarity"}),
            ("captions_render", {"rhythm"}),
            ("captions_render", {"synchronization"}),
            ("captions_render", {"caption_quality"}),
            ("captions_render", {"render_quality"}),
            ("editorial_tts_render", {"fluency", "biblical_coherence", "narrative_continuity", "emotional_impact", "interest", "predicted_retention", "hook_strength", "conclusion_strength"}),
            ("captions_render", {"visual_quality"}),
        ]
        problematic = {self._normalize_category_key(item) for item in (quality.get("below_minimum_dimensions") or [])}
        for stage, categories in stage_priority:
            if problematic.intersection(categories):
                current_attempts = int(attempts.get(stage) or 0)
                return {
                    "action": "recover",
                    "stage": stage,
                    "reason": ", ".join(sorted(problematic.intersection(categories))),
                    "supported": True,
                    "attempts_for_stage": current_attempts,
                    "max_attempts": self.MAX_AUTO_RECOVERY_ATTEMPTS_PER_STAGE,
                }
        for issue in critic.get("issues") or []:
            if isinstance(issue, dict):
                category = self._normalize_category_key(issue.get("category"))
                stage = self._normalize_stage_key(issue.get("stage"), category=category)
                if stage:
                    current_attempts = int(attempts.get(stage) or 0)
                    return {
                        "action": "recover",
                        "stage": stage,
                        "reason": self._compact_text(issue.get("problem")),
                        "supported": stage in {"editorial_tts_render", "pronunciation_tts_render", "captions_render", "thumbnail_publish"},
                        "attempts_for_stage": current_attempts,
                        "max_attempts": self.MAX_AUTO_RECOVERY_ATTEMPTS_PER_STAGE,
                    }
        return {
            "action": "hold",
            "stage": None,
            "reason": "Problema abaixo da qualidade mínima sem estratégia de autorecuperação suportada.",
            "supported": False,
            "attempts_for_stage": 0,
            "max_attempts": self.MAX_AUTO_RECOVERY_ATTEMPTS_PER_STAGE,
        }

    def record_learning(
        self,
        *,
        title: str,
        quality_report: Optional[Dict[str, Any]],
        critic_review: Optional[Dict[str, Any]],
        recovery_actions: Optional[List[Dict[str, Any]]] = None,
        render_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        learning = self._read_json_file(self._quality_learning_path, {"videos": [], "repairs": [], "winning_patterns": [], "recovery_history": []})
        videos = learning.get("videos") if isinstance(learning, dict) else []
        repairs = learning.get("repairs") if isinstance(learning, dict) else []
        recovery_history = learning.get("recovery_history") if isinstance(learning, dict) else []
        if not isinstance(videos, list):
            videos = []
        if not isinstance(repairs, list):
            repairs = []
        if not isinstance(recovery_history, list):
            recovery_history = []
        quality = quality_report if isinstance(quality_report, dict) else {}
        critic = critic_review if isinstance(critic_review, dict) else {}
        report = render_report if isinstance(render_report, dict) else {}
        entry = {
            "title": self._compact_text(title),
            "recorded_at": datetime.utcnow().isoformat(),
            "quality_score_final": int(quality.get("quality_score_final") or quality.get("overall_score") or 0),
            "technical_score": int(quality.get("technical_score") or 0),
            "editorial_score": int(quality.get("editorial_score") or 0),
            "below_minimum_dimensions": list(quality.get("below_minimum_dimensions") or []),
            "blocking_issues": list(quality.get("blocking_issues") or []),
            "critic_blocking_issues": [item.get("problem") for item in (critic.get("issues") or []) if isinstance(item, dict)],
            "render_time_sec": self._safe_float(report.get("execution_time_sec"), 0.0),
            "provider_used": self._compact_text(((report.get("audio_generation") or {}).get("provider_used")) if isinstance(report.get("audio_generation"), dict) else ""),
            "fallback_used": bool(((report.get("audio_generation") or {}).get("fallback_used")) if isinstance(report.get("audio_generation"), dict) else False),
        }
        videos.append(entry)
        for action in recovery_actions or []:
            if isinstance(action, dict):
                action_copy = dict(action)
                repairs.append(action_copy)
                recovery_history.append(
                    {
                        "title": self._compact_text(title),
                        "recorded_at": entry["recorded_at"],
                        "problem": self._compact_text(action_copy.get("reason")),
                        "solution_applied": self._compact_text(action_copy.get("stage")),
                        "result": "success" if self._compact_text(action_copy.get("status")) == "completed" else "failed",
                        "worked": self._compact_text(action_copy.get("status")) == "completed" and self._safe_int(action_copy.get("after_score"), 0) > self._safe_int(action_copy.get("before_score"), 0),
                        "score_before": self._safe_int(action_copy.get("before_score"), 0),
                        "score_after": self._safe_int(action_copy.get("after_score"), 0),
                        "time_spent_sec": self._safe_float(action_copy.get("time_spent_sec"), 0.0),
                        "attempt": self._safe_int(action_copy.get("attempt"), 0),
                        "error": self._compact_text(action_copy.get("error")),
                    }
                )
        learning["videos"] = videos[-80:]
        learning["repairs"] = repairs[-120:]
        learning["recovery_history"] = recovery_history[-180:]
        learning["updated_at"] = datetime.utcnow().isoformat()
        self._write_json_file(self._quality_learning_path, learning)
        return entry
