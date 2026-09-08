from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence


PTBR_NARRATION_PERFORMANCE_VERSION = 2
PTBR_NARRATION_PERFORMANCE_NAMESPACE = "ptbr-natural-performance-v2"
PTBR_EDGE_RATE = "-4%"
PTBR_EDGE_PITCH = "+0Hz"
PTBR_EDGE_VOLUME = "+0%"
PTBR_SENTENCE_PAUSE_MS = 260
PTBR_PARAGRAPH_PAUSE_MS = 520
PTBR_CLAUSE_PAUSE_MS = 160


class PtBrNarrationPerformanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PerformanceSegment:
    text: str
    pause_after_ms: int
    reason: str


def _collapse_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def _alignment_key(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(
        ch
        for ch in raw
        if unicodedata.category(ch) != "Mn" and ch.isalnum()
    )


def _sentence_units(paragraph: str) -> List[str]:
    paragraph = _collapse_spaces(paragraph)
    if not paragraph:
        return []
    units = [
        item.strip()
        for item in re.split(r"(?<=[.!?…])\s+", paragraph)
        if str(item or "").strip()
    ]
    return units or [paragraph]


def _split_long_unit(unit: str, *, max_chars: int = 300, max_words: int = 46) -> List[str]:
    text = _collapse_spaces(unit)
    if not text:
        return []
    if len(text) <= max_chars and len(text.split()) <= max_words:
        return [text]

    pieces = [piece.strip() for piece in re.split(r"(?<=[;:])\s+", text) if piece.strip()]
    if len(pieces) <= 1:
        pieces = [piece.strip() for piece in re.split(r"(?<=,)\s+", text) if piece.strip()]
    if len(pieces) <= 1:
        words = text.split()
        return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]

    out: List[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip() if current else piece
        if current and (len(candidate) > max_chars or len(candidate.split()) > max_words):
            out.append(current)
            current = piece
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def build_performance_segments(review_text: Any, spoken_text: Any) -> List[PerformanceSegment]:
    """Build sentence-sized pt-BR chunks without changing a lexical token.

    Pauses are represented as PCM time between synthesized chunks. They never
    become words such as ``pausa``/``respira`` and never require phonetic
    misspellings in the approved Portuguese source.
    """
    canonical = _collapse_spaces(spoken_text)
    if not canonical:
        return []

    raw = str(review_text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [
        _collapse_spaces(part)
        for part in re.split(r"\n\s*\n", raw)
        if _collapse_spaces(part)
    ]
    if not paragraphs:
        paragraphs = [canonical]

    segments: List[PerformanceSegment] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        sentences = _sentence_units(paragraph)
        for sentence_index, sentence in enumerate(sentences):
            chunks = _split_long_unit(sentence)
            for chunk_index, chunk in enumerate(chunks):
                is_last_chunk = chunk_index == len(chunks) - 1
                is_last_sentence = sentence_index == len(sentences) - 1
                is_last_paragraph = paragraph_index == len(paragraphs) - 1
                if not is_last_chunk:
                    pause_ms = PTBR_CLAUSE_PAUSE_MS
                    reason = "clause_breath"
                elif not is_last_sentence:
                    pause_ms = PTBR_SENTENCE_PAUSE_MS
                    reason = "sentence_breath"
                elif not is_last_paragraph:
                    pause_ms = PTBR_PARAGRAPH_PAUSE_MS
                    reason = "paragraph_breath"
                else:
                    pause_ms = 0
                    reason = "final"
                segments.append(PerformanceSegment(chunk, pause_ms, reason))

    flattened = _collapse_spaces(" ".join(item.text for item in segments))
    if flattened != canonical:
        raise PtBrNarrationPerformanceError(
            "O plano de performance alterou o texto canônico; a síntese foi bloqueada."
        )
    return segments


def align_canonical_word_boundaries(
    canonical_text: Any,
    boundaries: Sequence[Dict[str, Any]],
    *,
    max_group_tokens: int = 4,
) -> List[Dict[str, Any]]:
    """Map TTS timings to exact approved tokens, preserving accents/punctuation."""
    canonical_tokens = [token for token in _collapse_spaces(canonical_text).split(" ") if token]
    edge_tokens: List[Dict[str, Any]] = []
    for raw in boundaries or []:
        if not isinstance(raw, dict):
            continue
        token = str(raw.get("word") or raw.get("text") or "").strip()
        try:
            start = max(0.0, float(raw.get("start") or 0.0))
            end = max(start, float(raw.get("end") or 0.0))
        except Exception:
            continue
        if token and end > start:
            edge_tokens.append({"word": token, "start": start, "end": end})

    if not canonical_tokens or not edge_tokens:
        return []

    aligned: List[Dict[str, Any]] = []
    ci = 0
    ei = 0
    group_limit = max(1, int(max_group_tokens or 1))
    while ci < len(canonical_tokens) and ei < len(edge_tokens):
        best = None
        max_c = min(group_limit, len(canonical_tokens) - ci)
        max_e = min(group_limit, len(edge_tokens) - ei)
        for c_count in range(1, max_c + 1):
            c_key = "".join(_alignment_key(token) for token in canonical_tokens[ci:ci + c_count])
            if not c_key:
                continue
            for e_count in range(1, max_e + 1):
                e_key = "".join(_alignment_key(item["word"]) for item in edge_tokens[ei:ei + e_count])
                if not e_key or c_key != e_key:
                    continue
                score = (c_count + e_count, abs(c_count - e_count), c_count, e_count)
                if best is None or score < best[0]:
                    best = (score, c_count, e_count)
        if best is None:
            raise PtBrNarrationPerformanceError(
                "Os limites de palavra do TTS não correspondem exatamente ao texto aprovado; "
                "a legenda foi bloqueada para evitar dessincronização."
            )

        _, c_count, e_count = best
        canonical_group = canonical_tokens[ci:ci + c_count]
        edge_group = edge_tokens[ei:ei + e_count]
        aligned.append({
            "start": round(float(edge_group[0]["start"]), 4),
            "end": round(float(edge_group[-1]["end"]), 4),
            "word": " ".join(canonical_group),
        })
        ci += c_count
        ei += e_count

    if ci < len(canonical_tokens):
        residue = canonical_tokens[ci:]
        if aligned and all(not _alignment_key(token) for token in residue):
            aligned[-1]["word"] = f"{aligned[-1]['word']} {' '.join(residue)}".strip()
            ci = len(canonical_tokens)
    if ei < len(edge_tokens) and all(not _alignment_key(item["word"]) for item in edge_tokens[ei:]):
        ei = len(edge_tokens)

    if ci != len(canonical_tokens) or ei != len(edge_tokens):
        raise PtBrNarrationPerformanceError(
            "A contagem final de palavras do TTS divergiu do texto aprovado; a legenda foi bloqueada."
        )

    rebuilt = _collapse_spaces(" ".join(item["word"] for item in aligned))
    canonical = _collapse_spaces(canonical_text)
    if rebuilt != canonical:
        raise PtBrNarrationPerformanceError(
            "A legenda reconstruída não é idêntica ao texto aprovado."
        )
    return aligned


def _ffmpeg_decode_to_wav(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PtBrNarrationPerformanceError("FFmpeg não está disponível para montar as pausas naturais.")
    result = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ar", "24000", "-ac", "1",
            "-c:a", "pcm_s16le", str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 256:
        raise PtBrNarrationPerformanceError(
            "Falha ao preparar trecho de voz para a montagem natural: " + str(result.stderr or "")[-300:]
        )


def _encode_wav_to_mp3(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PtBrNarrationPerformanceError("FFmpeg não está disponível para finalizar a narração.")
    result = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ar", "24000", "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", "96k", str(target),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 512:
        raise PtBrNarrationPerformanceError(
            "Falha ao finalizar a narração natural: " + str(result.stderr or "")[-300:]
        )


async def synthesize_edge_ptbr_performance(
    *,
    spoken_text: str,
    review_text: str,
    voice: str,
    output_path: Path,
) -> Dict[str, Any]:
    """Synthesize pt-BR in breathing-sized chunks and return exact word timing."""
    try:
        import edge_tts
    except Exception as exc:
        raise PtBrNarrationPerformanceError("Edge TTS não está disponível no servidor.") from exc

    segments = build_performance_segments(review_text, spoken_text)
    if not segments:
        raise PtBrNarrationPerformanceError("O roteiro não possui texto falável para sintetizar.")
    if not shutil.which("ffmpeg"):
        raise PtBrNarrationPerformanceError("FFmpeg não está disponível para a síntese natural.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    global_timeline: List[Dict[str, Any]] = []
    cursor = 0.0
    pause_total = 0.0

    with tempfile.TemporaryDirectory(prefix="codexia-ptbr-tts-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        combined_wav = temp_dir / "combined.wav"
        wav_writer = wave.open(str(combined_wav), "wb")
        wav_writer.setnchannels(1)
        wav_writer.setsampwidth(2)
        wav_writer.setframerate(24000)
        try:
            for index, segment in enumerate(segments):
                segment_mp3 = temp_dir / f"segment-{index:03d}.mp3"
                segment_wav = temp_dir / f"segment-{index:03d}.wav"
                communicator = edge_tts.Communicate(
                    segment.text,
                    voice,
                    rate=PTBR_EDGE_RATE,
                    pitch=PTBR_EDGE_PITCH,
                    volume=PTBR_EDGE_VOLUME,
                )
                raw_boundaries: List[Dict[str, Any]] = []
                if hasattr(communicator, "stream"):
                    with segment_mp3.open("wb") as audio_file:
                        async for chunk in communicator.stream():
                            if not isinstance(chunk, dict):
                                continue
                            chunk_type = str(chunk.get("type") or "")
                            if chunk_type == "audio":
                                data = chunk.get("data")
                                if isinstance(data, (bytes, bytearray)):
                                    audio_file.write(data)
                            elif chunk_type == "WordBoundary":
                                try:
                                    start = max(0.0, float(chunk.get("offset") or 0) / 10_000_000.0)
                                    duration = max(0.0, float(chunk.get("duration") or 0) / 10_000_000.0)
                                except Exception:
                                    continue
                                token = str(chunk.get("text") or "").strip()
                                if token and duration > 0:
                                    raw_boundaries.append({
                                        "start": start,
                                        "end": start + duration,
                                        "word": token,
                                    })
                else:
                    await communicator.save(str(segment_mp3))

                if not segment_mp3.is_file() or segment_mp3.stat().st_size <= 512:
                    raise PtBrNarrationPerformanceError("O TTS retornou um trecho de áudio inválido.")
                if not raw_boundaries:
                    raise PtBrNarrationPerformanceError(
                        "O TTS não retornou limites de palavra; a narração foi bloqueada para não gerar legenda aproximada."
                    )

                local_timeline = align_canonical_word_boundaries(segment.text, raw_boundaries)
                _ffmpeg_decode_to_wav(segment_mp3, segment_wav)
                with wave.open(str(segment_wav), "rb") as reader:
                    if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() != 24000:
                        raise PtBrNarrationPerformanceError("Formato PCM inesperado na montagem da narração.")
                    frames = reader.readframes(reader.getnframes())
                    segment_duration = reader.getnframes() / float(reader.getframerate())
                wav_writer.writeframes(frames)

                for item in local_timeline:
                    local_start = min(segment_duration, max(0.0, float(item["start"])))
                    local_end = min(segment_duration, max(local_start, float(item["end"])))
                    if local_end <= local_start:
                        raise PtBrNarrationPerformanceError(
                            "O limite temporal de uma palavra caiu fora do áudio sintetizado."
                        )
                    global_timeline.append({
                        "start": round(cursor + local_start, 4),
                        "end": round(cursor + local_end, 4),
                        "word": item["word"],
                    })
                cursor += segment_duration

                pause_ms = int(segment.pause_after_ms or 0) if index < len(segments) - 1 else 0
                if pause_ms > 0:
                    silence_frames = int(round(24000 * (pause_ms / 1000.0)))
                    wav_writer.writeframes(b"\x00\x00" * silence_frames)
                    pause_sec = silence_frames / 24000.0
                    cursor += pause_sec
                    pause_total += pause_sec
        finally:
            wav_writer.close()

        final_timeline = align_canonical_word_boundaries(spoken_text, global_timeline)
        _encode_wav_to_mp3(combined_wav, output_path)

    return {
        "caption_timeline": final_timeline,
        "segment_count": len(segments),
        "pause_total_sec": round(pause_total, 3),
        "performance_version": PTBR_NARRATION_PERFORMANCE_VERSION,
        "performance_namespace": PTBR_NARRATION_PERFORMANCE_NAMESPACE,
        "rate": PTBR_EDGE_RATE,
        "pitch": PTBR_EDGE_PITCH,
        "volume": PTBR_EDGE_VOLUME,
        "caption_alignment_exact": True,
    }
