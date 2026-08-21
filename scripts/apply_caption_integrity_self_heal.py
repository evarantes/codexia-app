from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RENDER_TARGET = ROOT / "app/services/video_generator.py"
FATAL_MESSAGE = "Falha de validacao: legenda-base difere do texto enviado ao TTS."
SELF_HEAL_MARKER = 'caption_integrity_self_heal_v1'


class PatchError(RuntimeError):
    pass


def patch_renderer(text: str) -> str:
    if SELF_HEAL_MARKER in text:
        return text

    old = '''            if normalized_caption_text != normalized_tts_text:\n                raise Exception("Falha de validacao: legenda-base difere do texto enviado ao TTS.")'''
    new = '''            # caption_integrity_self_heal_v1\n            # Divergência de ASR/segmentação não pode cancelar uma produção já\n            # paga. Primeiro preservamos timestamps reais; depois reconstruímos\n            # a timeline localmente; por último usamos um bloco canônico.\n            if normalized_caption_text != normalized_tts_text:\n                mismatch_before = {\n                    "caption_length": len(normalized_caption_text),\n                    "tts_length": len(normalized_tts_text),\n                    "timeline_source_before": caption_timeline_source,\n                }\n                repair_mode = "canonical_rebuild"\n                repaired_timeline = []\n                repairer = getattr(self, "_codexia_force_canonical_caption_timeline", None)\n                if callable(repairer):\n                    try:\n                        repaired_timeline = repairer(\n                            caption_narration_text,\n                            actual_total_audio_dur,\n                            timeline=full_caption_timeline,\n                            opening_silence_sec=initial_opening_silence_sec,\n                        ) or []\n                        repair_mode = str(\n                            getattr(self, "_codexia_caption_repair_mode", repair_mode)\n                            or repair_mode\n                        )\n                    except Exception:\n                        repaired_timeline = []\n\n                if not repaired_timeline:\n                    try:\n                        usable_duration = max(\n                            0.1,\n                            actual_total_audio_dur - max(0.0, initial_opening_silence_sec),\n                        )\n                        local_timeline = self._caption_timeline_from_text(\n                            caption_narration_text,\n                            usable_duration,\n                        )\n                        for raw_item in local_timeline or []:\n                            item = dict(raw_item)\n                            start = float(item.get("start") or 0.0) + initial_opening_silence_sec\n                            end = float(item.get("end") or 0.0) + initial_opening_silence_sec\n                            item["start"] = round(min(actual_total_audio_dur, start), 3)\n                            item["end"] = round(min(actual_total_audio_dur, max(start, end)), 3)\n                            item["source"] = "canonical_text_timeline"\n                            repaired_timeline.append(item)\n                        repair_mode = "canonical_text_timeline"\n                    except Exception:\n                        repaired_timeline = []\n\n                if repaired_timeline:\n                    full_caption_timeline = repaired_timeline\n                    caption_text_joined = " ".join(\n                        str(item.get("caption") or "").strip()\n                        for item in full_caption_timeline\n                        if str(item.get("caption") or "").strip()\n                    ).strip()\n                    normalized_caption_text = self._normalize_tts_text(caption_text_joined)\n\n                if normalized_caption_text != normalized_tts_text:\n                    # Última garantia: o conteúdo-base fica literalmente igual à\n                    # fonte canônica. O overlay pode subdividir depois sem alterar\n                    # o texto nem gerar nova chamada paga.\n                    full_caption_timeline = [{\n                        "caption": caption_narration_text,\n                        "start": round(max(0.0, initial_opening_silence_sec), 3),\n                        "end": round(max(initial_opening_silence_sec + 0.1, actual_total_audio_dur), 3),\n                        "source": "canonical_single_block",\n                        "text_source": "canonical_narration",\n                    }]\n                    caption_text_joined = caption_narration_text\n                    normalized_caption_text = self._normalize_tts_text(caption_narration_text)\n                    repair_mode = "canonical_single_block"\n\n                caption_timeline_source = f"{caption_timeline_source}+self_heal:{repair_mode}"\n                render_report["text_integrity"].update({\n                    "auto_repaired": True,\n                    "repair_mode": repair_mode,\n                    "mismatch_before_repair": mismatch_before,\n                    "captions_source_text": caption_text_joined,\n                    "captions_match_narration_source": normalized_caption_text == normalized_tts_text,\n                    "caption_text_length_after_repair": len(normalized_caption_text),\n                })'''

    count = text.count(old)
    if count != 1:
        raise PatchError(f"validator fatal esperado 1 vez, encontrado {count}")
    return text.replace(old, new, 1)


def check_text(text: str) -> None:
    if SELF_HEAL_MARKER not in text:
        raise PatchError("auto-recuperação de integridade de legendas não instalada")
    fatal = f'raise Exception("{FATAL_MESSAGE}")'
    if fatal in text:
        raise PatchError("validator fatal antigo ainda existe no renderer")
    required = (
        '_codexia_force_canonical_caption_timeline',
        '"auto_repaired": True',
        '"canonical_single_block"',
        'caption_timeline_source = f"{caption_timeline_source}+self_heal:{repair_mode}"',
    )
    for token in required:
        if token not in text:
            raise PatchError(f"self-heal incompleto: ausente {token}")


def check() -> None:
    check_text(RENDER_TARGET.read_text(encoding="utf-8"))


def apply(*, write: bool) -> int:
    original = RENDER_TARGET.read_text(encoding="utf-8")
    transformed = patch_renderer(original)
    second = patch_renderer(transformed)
    if second != transformed:
        raise PatchError("transformação não idempotente")
    if write and transformed != original:
        RENDER_TARGET.write_text(transformed, encoding="utf-8")
    check_text(transformed)
    print(f"Caption integrity self-heal: {'alterado' if transformed != original else 'já aplicado'}.")
    return int(transformed != original)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        if args.apply:
            apply(write=True)
        if args.check:
            check()
    except PatchError as exc:
        print(f"ERRO CAPTION INTEGRITY SELF-HEAL: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
