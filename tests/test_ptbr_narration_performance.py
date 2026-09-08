import pytest

from app.services.ptbr_narration_performance import (
    PTBR_NARRATION_PERFORMANCE_NAMESPACE,
    PTBR_NARRATION_PERFORMANCE_VERSION,
    PTBR_PARAGRAPH_PAUSE_MS,
    PTBR_SENTENCE_PAUSE_MS,
    PtBrNarrationPerformanceError,
    align_canonical_word_boundaries,
    build_performance_segments,
)


def test_alignment_preserves_exact_accents_and_punctuation():
    canonical = "Você não está só, Jesus caminha ao seu lado em cada passo."
    boundaries = [
        {"word": "Voce", "start": 0.10, "end": 0.30},
        {"word": "nao", "start": 0.31, "end": 0.50},
        {"word": "esta", "start": 0.51, "end": 0.70},
        {"word": "so", "start": 0.71, "end": 0.90},
        {"word": "Jesus", "start": 1.00, "end": 1.20},
        {"word": "caminha", "start": 1.21, "end": 1.50},
        {"word": "ao", "start": 1.51, "end": 1.60},
        {"word": "seu", "start": 1.61, "end": 1.72},
        {"word": "lado", "start": 1.73, "end": 1.90},
        {"word": "em", "start": 1.91, "end": 2.00},
        {"word": "cada", "start": 2.01, "end": 2.15},
        {"word": "passo", "start": 2.16, "end": 2.40},
    ]

    aligned = align_canonical_word_boundaries(canonical, boundaries)

    assert " ".join(item["word"] for item in aligned) == canonical
    assert aligned[0] == {"start": 0.1, "end": 0.3, "word": "Você"}
    assert aligned[3]["word"] == "só,"
    assert aligned[-1]["word"] == "passo."


def test_alignment_handles_hyphen_tokenization_without_time_redistribution():
    aligned = align_canonical_word_boundaries(
        "uma palavra-chave aqui",
        [
            {"word": "uma", "start": 0.0, "end": 0.1},
            {"word": "palavra", "start": 0.1, "end": 0.2},
            {"word": "chave", "start": 0.2, "end": 0.3},
            {"word": "aqui", "start": 0.3, "end": 0.4},
        ],
    )

    assert aligned[1] == {"start": 0.1, "end": 0.3, "word": "palavra-chave"}
    assert " ".join(item["word"] for item in aligned) == "uma palavra-chave aqui"


def test_alignment_fails_closed_when_tts_words_do_not_match():
    with pytest.raises(PtBrNarrationPerformanceError):
        align_canonical_word_boundaries(
            "Jesus caminha conosco.",
            [
                {"word": "Jesus", "start": 0.0, "end": 0.2},
                {"word": "corre", "start": 0.2, "end": 0.4},
                {"word": "conosco", "start": 0.4, "end": 0.7},
            ],
        )


def test_breath_plan_preserves_canonical_portuguese_exactly():
    review = (
        "Você não está só. Deus permanece perto de você.\n\n"
        "Permita que essa verdade reescreva seus dias: caminhe com fé."
    )
    spoken = (
        "Você não está só. Deus permanece perto de você. "
        "Permita que essa verdade reescreva seus dias: caminhe com fé."
    )

    segments = build_performance_segments(review, spoken)

    assert " ".join(item.text for item in segments) == spoken
    assert segments[0].text == "Você não está só."
    assert segments[0].pause_after_ms == PTBR_SENTENCE_PAUSE_MS
    assert segments[1].text == "Deus permanece perto de você."
    assert segments[1].pause_after_ms == PTBR_PARAGRAPH_PAUSE_MS
    assert segments[-1].pause_after_ms == 0
    assert "Você" in segments[0].text
    assert "fé." in segments[-1].text


def test_performance_contract_is_versioned():
    assert PTBR_NARRATION_PERFORMANCE_VERSION == 2
    assert PTBR_NARRATION_PERFORMANCE_NAMESPACE == "ptbr-natural-performance-v2"
