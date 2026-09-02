from pathlib import Path


def test_youtube_auto_exposes_global_narrative_standard_badge():
    index_path = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
    text = index_path.read_text(encoding="utf-8")

    assert "Padrão Global Codexia — ATIVO" in text
    assert "motor narrativo global orienta os vídeos narrados compatíveis em todo o sistema" in text
    assert "Escopo:</strong> todo o Codexia" in text
    assert "Shorts e música mantêm estrutura própria" in text
