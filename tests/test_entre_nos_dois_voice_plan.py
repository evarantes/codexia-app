import unittest

from app.services.entre_nos_dois import DialogueTurn
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class SpeechManifestTests(unittest.TestCase):
    def test_official_voices_are_preserved(self):
        manifest = prepare_speech_manifest()
        self.assertEqual(manifest["approved_voices"]["Gabriel"], "ceOf6Sb1xKhC0ZVxHIzb")
        self.assertEqual(manifest["approved_voices"]["Helena"], "EMGk4qRFrRTnkdPoTzu3")
        self.assertEqual(manifest["target_seconds"], 30)
        self.assertEqual([s["speaker"] for s in manifest["segments"]],
                         ["Helena", "Gabriel", "Helena", "Gabriel"])
        self.assertTrue(manifest["requires_explicit_cost_approval"])
        self.assertTrue(manifest["requires_human_audio_review"])
        self.assertFalse(manifest["auto_publish"])
        self.assertTrue(all(s["audio_path"] is None for s in manifest["segments"]))

    def test_budget_guard(self):
        with self.assertRaises(ValueError):
            prepare_speech_manifest(max_characters=10)

    def test_unapproved_speaker(self):
        with self.assertRaises(ValueError):
            prepare_speech_manifest(
                (DialogueTurn("Narrador", "Texto", 30),)
            )

    def test_duration_guard(self):
        with self.assertRaises(ValueError):
            prepare_speech_manifest(target_seconds=29)


if __name__ == "__main__":
    unittest.main()
