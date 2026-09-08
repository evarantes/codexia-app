import sys
import tempfile
import types
import unittest
import json
from pathlib import Path

from app.services.narration_core import NARRATION_CORE_NAMESPACE, NARRATION_CORE_VERSION
from app.services.narrative_structure_standard import (
    CHANNEL_PRESENTATION_TEXT,
    DEFAULT_NARRATED_CTA_TEXT,
    compose_canonical_narration,
)
from app.services.youtube_narration_gate import (
    YouTubeNarrationGateError,
    YouTubeNarrationGateService,
)


class _FakeCommunicate:
    calls = 0
    texts = []
    kwargs = []

    def __init__(self, text, voice, **kwargs):
        self.text = text
        self.voice = voice
        type(self).texts.append(text)
        type(self).kwargs.append(kwargs)

    async def save(self, path):
        type(self).calls += 1
        Path(path).write_bytes(b"ID3" + b"a" * 2048)


class _FakeStreamCommunicate:
    def __init__(self, text, voice, **kwargs):
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": b"ID3" + b"b" * 2048}
        yield {"type": "WordBoundary", "offset": 0, "duration": 4_000_000, "text": "Seja"}
        yield {"type": "WordBoundary", "offset": 4_000_000, "duration": 5_000_000, "text": "muito"}


def _canonical_script(topic: str = "Jesus permanece conosco") -> str:
    return compose_canonical_narration({
        "hook": f"{topic}, e esta verdade abre uma pergunta importante.",
        "development": "A mensagem avança com contexto, clareza e uma tensão real.",
        "central_truth": "A verdade central mostra que Deus continua presente e fiel.",
        "transformation": "Essa verdade transforma o medo em confiança para continuar.",
        "application": "Hoje podemos aplicar a mensagem em uma escolha concreta de fé.",
        "climax": "A esperança alcança seu ponto mais forte quando decidimos permanecer.",
        "reflection": "A reflexão retorna à pergunta inicial e conclui a mensagem com serenidade.",
    })


class YouTubeNarrationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = YouTubeNarrationGateService(self.tmp.name)
        self.old_edge = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=_FakeCommunicate)
        _FakeCommunicate.calls = 0
        _FakeCommunicate.texts = []
        _FakeCommunicate.kwargs = []
        self.service._duration = lambda _path: 123.4

    def tearDown(self):
        self.tmp.cleanup()
        if self.old_edge is None:
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = self.old_edge

    def test_blocks_pure_technical_payload_before_tts(self):
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.generate(
                text='{"image_prompt": "Jesus"}',
                user_id=7,
                voice="auto",
                voice_gender="female",
            )
        self.assertEqual(ctx.exception.code, "NARRATION_CORE_BLOCKED")
        self.assertEqual(_FakeCommunicate.calls, 0)

    def test_mixed_production_script_sends_only_spoken_sentence(self):
        canonical = _canonical_script("Jesus permanece conosco mesmo nos dias mais difíceis")
        raw = canonical + """

PROMPT VISUAL: Jesus caminhando por uma estrada, iluminação cinematográfica, 16:9.

DURAÇÃO: 8 segundos.

MOVIMENTO DE CÂMERA: travelling lento.

TEXTO NA TELA: Deus não esqueceu de você.
"""
        result = self.service.generate(text=raw, user_id=7)
        self.assertEqual(_FakeCommunicate.calls, 1)
        self.assertEqual(_FakeCommunicate.texts, [canonical])
        self.assertIn("\n\n", _FakeCommunicate.texts[0])
        self.assertEqual(_FakeCommunicate.kwargs[0]["rate"], "-5%")
        self.assertEqual(_FakeCommunicate.kwargs[0]["pitch"], "-1Hz")
        self.assertEqual(result["spoken_text_sent_to_tts"], _FakeCommunicate.texts[0])
        self.assertTrue(result["prosody"]["paragraph_pauses_preserved"])
        self.assertGreaterEqual(result["removed_technical_blocks"], 4)
        self.assertTrue(result["narration_contract"]["valid"])

    def test_generates_once_and_reuses_identical_audio(self):
        text = _canonical_script("Esta é uma narração limpa, completa e pronta para o vídeo")
        first = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        second = self.service.generate(text=text, user_id=7, voice="auto", voice_gender="female")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["preview_id"], second["preview_id"])
        self.assertEqual(first["narration_core_version"], NARRATION_CORE_VERSION)
        self.assertEqual(first["narration_core_namespace"], NARRATION_CORE_NAMESPACE)
        self.assertEqual(_FakeCommunicate.calls, 1)

    def test_preserves_edge_word_boundaries_as_caption_timing_authority(self):
        sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=_FakeStreamCommunicate)
        result = self.service.generate(
            text=_canonical_script("A esperança permanece nos dias difíceis"),
            user_id=8,
        )

        self.assertEqual(result["caption_timing_source"], "edge_tts_word_boundaries")
        self.assertEqual(
            result["caption_timeline"],
            [
                {"start": 0.0, "end": 0.4, "word": "Seja"},
                {"start": 0.4, "end": 0.9, "word": "muito"},
            ],
        )

    def test_approval_returns_reuse_audio_and_rejects_changed_text(self):
        text = _canonical_script("Confie em Deus e permaneça firme até o fim")
        preview = self.service.generate(text=text, user_id=9)
        approved = self.service.approve(
            preview_id=preview["preview_id"],
            expected_text=preview["review_script_text"],
            user_id=9,
        )
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["reuse_audio_from"]["source"], "youtube_narration_core_v1_approved")
        self.assertEqual(approved["reuse_audio_from"]["narration_core_version"], NARRATION_CORE_VERSION)
        self.assertTrue(Path(approved["reuse_audio_from"]["output_path"]).is_file())
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.approve(
                preview_id=preview["preview_id"],
                expected_text="Confie em Deus, mas este texto mudou.",
                user_id=9,
            )
        self.assertEqual(ctx.exception.code, "TEXT_CHANGED_AFTER_PREVIEW")

    def test_approval_preserves_multisentence_cta_paragraph_without_false_change(self):
        text = compose_canonical_narration(
            {
                "hook": "Imagine um amor que conhece cada detalhe dos seus dias.",
                "development": "Essa intimidade profunda revela a relação oferecida por Jesus.",
                "central_truth": "A verdade central é que sua presença permanece conosco.",
                "transformation": "Essa certeza transforma a solidão em companhia constante.",
                "application": "Hoje podemos caminhar em paz e confiar em cada passo.",
                "climax": "Jesus está aqui agora, conduzindo nossa jornada com amor eterno.",
                "reflection": "Reflita sobre como você percebe a presença de Jesus em sua vida.",
            },
            cta_text=(
                "Se gostou, curta este vídeo e inscreva-se no canal. "
                "Ative o sininho e compartilhe esta mensagem com quem precisa. "
                "Deixe seu comentário sobre o que mais tocou seu coração."
            ),
        )
        preview = self.service.generate(text=text, user_id=16)

        approved = self.service.approve(
            preview_id=preview["preview_id"],
            expected_text=preview["review_script_text"],
            user_id=16,
            production_job_id=preview["production_job_id"],
        )

        self.assertTrue(approved["approved"])
        self.assertEqual(approved["text_sha256"], preview["text_sha256"])

        changed_text = preview["review_script_text"].replace(
            "solidão em companhia constante",
            "solidão em uma espera constante",
            1,
        )
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.approve(
                preview_id=preview["preview_id"],
                expected_text=changed_text,
                user_id=16,
                production_job_id=preview["production_job_id"],
            )
        self.assertEqual(ctx.exception.code, "TEXT_CHANGED_AFTER_PREVIEW")

    def test_rejects_preview_whose_stored_global_narrative_contract_is_missing(self):
        preview = self.service.generate(
            text=_canonical_script("Jesus nos ensina a perseverar"),
            user_id=12,
        )
        meta_path = self.service._user_dir(12) / f"{preview['preview_id']}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("narration_contract", None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.approve(
                preview_id=preview["preview_id"],
                expected_text=preview["review_script_text"],
                user_id=12,
            )
        self.assertEqual(ctx.exception.code, "NARRATIVE_CONTRACT_INCOMPLETE")

    def test_job_approval_freezes_the_exact_mp3_in_its_own_folder(self):
        text = _canonical_script("Jesus nos chama a caminhar com fé e esperança")
        preview = self.service.generate(text=text, user_id=11, theme="Esperança")
        job_id = preview["production_job_id"]

        approved = self.service.approve(
            preview_id=preview["preview_id"],
            expected_text=preview["review_script_text"],
            user_id=11,
            production_job_id=job_id,
        )

        approved_path = Path(approved["reuse_audio_from"]["output_path"])
        self.assertEqual(approved["production_job_id"], job_id)
        self.assertEqual(approved["production_job_status"], "narration_approved")
        self.assertEqual(approved_path.name, "approved_narration.mp3")
        self.assertTrue(approved_path.is_file())
        validated = self.service.job_store.validated_approved_audio(user_id=11, job_id=job_id)
        self.assertEqual(validated["audio_path"].resolve(), approved_path.resolve())
        self.assertTrue(validated["job"]["tts_locked"])

    def test_blocks_incomplete_narrative_arc_before_tts(self):
        with self.assertRaises(YouTubeNarrationGateError) as ctx:
            self.service.generate(
                text="Jesus nos chama a caminhar com fé e esperança.",
                user_id=15,
            )
        self.assertEqual(ctx.exception.code, "NARRATIVE_CONTRACT_INCOMPLETE")
        self.assertEqual(_FakeCommunicate.calls, 0)


if __name__ == "__main__":
    unittest.main()
