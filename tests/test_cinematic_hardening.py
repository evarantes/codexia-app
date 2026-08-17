from app.services.scene_director_active import direct_scene_plan, install_scene_director_active_patch


def test_scene_director_expands_long_narration_into_real_visual_beats(monkeypatch):
    monkeypatch.setenv("ENABLE_SCENE_DIRECTOR", "true")
    monkeypatch.setenv("ENABLE_REAL_VISUAL_BEATS", "true")
    monkeypatch.setenv("VIDEO_MAX_UNIQUE_SCENES", "48")
    text = (
        "Jesus permanece perto quando o medo cresce e a esperança parece distante. "
        "A presença dele nos chama a confiar mesmo quando não enxergamos a saída. "
        "Quando as forças diminuem, encontramos nele direção para continuar. "
        "A fé amadurece quando deixamos de olhar apenas para a tempestade e voltamos o coração para Cristo. "
        "Ele não é apenas uma lembrança religiosa, mas presença que consola, confronta e transforma. "
        "Por isso, caminhar com Jesus muda a maneira como enfrentamos cada novo dia."
    )
    plan = {"scenes": [{"text": text, "image_prompt": "Jesus consolando uma pessoa"}]}

    directed, report = direct_scene_plan(plan)

    assert len(directed["scenes"]) >= 4
    assert report["changes_scene_count"] is True
    assert report["real_visual_beats"]["after"] == len(directed["scenes"])
    prompts = [scene["image_prompt"] for scene in directed["scenes"]]
    assert len(set(prompts)) == len(prompts)
    assert all("newly generated visual concept" in prompt for prompt in prompts)


def test_scene_expansion_preserves_all_narration_words(monkeypatch):
    monkeypatch.setenv("ENABLE_SCENE_DIRECTOR", "true")
    monkeypatch.setenv("ENABLE_REAL_VISUAL_BEATS", "true")
    original = "Uma frase curta. " + " ".join(f"palavra{i}" for i in range(70)) + "."
    plan = {"scenes": [{"text": original, "image_prompt": "cena base"}]}
    directed, _ = direct_scene_plan(plan)
    combined = " ".join(scene.get("text", "") for scene in directed["scenes"])
    assert combined.split() == original.split()


def test_ptbr_guard_normalizes_language_and_jesus_pronunciation(monkeypatch):
    monkeypatch.setenv("ENABLE_PTBR_TTS_GUARD", "true")

    class FakeGenerator:
        def create_video_from_plan(self, plan, *args, **kwargs):
            return {"ok": True, "plan": plan}

        def generate_audio(self, text, lang="pt", voice_style=None, voice_gender=None):
            return {"text": text, "lang": lang}

        def _build_logo_overlay(self, logo_path, size, *, duration, position="top_center", opacity=0.92, width_ratio=0.18):
            return {"position": position, "opacity": opacity, "width_ratio": width_ratio}

        def _resolve_closing_background_image(self, branding, **kwargs):
            return {"path": "/tmp/last-scene.png", "source": "last_scene"}

    install_scene_director_active_patch(FakeGenerator)
    generator = FakeGenerator()
    result = generator.generate_audio("Jesus é fiel.", lang="pt-BR")

    assert result["lang"] == "pt"
    assert "Jêzus" in result["text"]


def test_opening_logo_moves_out_of_top_center(monkeypatch):
    monkeypatch.setenv("ENABLE_PREMIUM_OPENING_SAFE_ZONE", "true")

    class FakeGenerator:
        def create_video_from_plan(self, plan, *args, **kwargs):
            return {"ok": True}

        def _build_logo_overlay(self, logo_path, size, *, duration, position="top_center", opacity=0.92, width_ratio=0.18):
            return {"position": position, "opacity": opacity, "width_ratio": width_ratio}

    install_scene_director_active_patch(FakeGenerator)
    result = FakeGenerator()._build_logo_overlay("logo.png", (1280, 720), duration=2.0, position="top_center", width_ratio=0.18)
    assert result["position"] == "top_right"
    assert result["width_ratio"] <= 0.11


def test_endcard_never_reuses_last_scene_when_premium_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DEDICATED_PREMIUM_ENDCARD", "true")

    class FakeGenerator:
        def create_video_from_plan(self, plan, *args, **kwargs):
            return {"ok": True}

        def _resolve_closing_background_image(self, branding, **kwargs):
            return {"path": "/tmp/last-scene.png", "source": "last_scene"}

    install_scene_director_active_patch(FakeGenerator)
    result = FakeGenerator()._resolve_closing_background_image({})
    assert result["path"] is None
    assert result["source"] == "dedicated_generated_endcard"
