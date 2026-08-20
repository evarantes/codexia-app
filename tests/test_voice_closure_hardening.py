from __future__ import annotations

import unittest

from app.services.channel_excellence_guard import prepare_spoken_text
from app.services.story_review_editor import _editorial_quality_issues


class VoiceClosureHardeningTests(unittest.TestCase):
    def test_jesus_keeps_official_spelling_for_tts(self):
        spoken = prepare_spoken_text("Jesus Cristo é o centro. JESUS permanece fiel.")
        self.assertIn("Jesus Cristo", spoken)
        self.assertNotIn("Jêzus", spoken)
        self.assertEqual(spoken.count("Jesus"), 2)

    def test_incomplete_ending_is_rejected_before_paid_media(self):
        text = (
            "A fé muda a forma como atravessamos o medo. Deus não promete ausência de luta, "
            "mas presença no caminho. Quando a ansiedade cresce, lembramos que não caminhamos sozinhos e"
        )
        issues = _editorial_quality_issues("Fé em meio ao medo", text, "como manter a fé em meio ao medo")
        self.assertIn("unfinished_final_sentence", issues)
        self.assertIn("dangling_final_connector", issues)

    def test_complete_ending_passes_structural_closure_checks(self):
        text = (
            "O medo pode até bater à porta, mas não precisa governar o coração. A fé nos lembra que Deus "
            "continua presente e que cada passo pode ser dado com confiança. Quando a insegurança voltar, "
            "recorde a verdade central desta mensagem: a fé não elimina toda luta, mas nos impede de lutar "
            "sozinhos. Hoje, escolha confiar novamente. Em Deus, o medo não terá a palavra final."
        )
        issues = _editorial_quality_issues("Fé em meio ao medo", text, "como manter a fé em meio ao medo")
        self.assertNotIn("closing_too_short", issues)
        self.assertNotIn("weak_final_sentence", issues)
        self.assertNotIn("unfinished_final_sentence", issues)
        self.assertNotIn("dangling_final_connector", issues)


if __name__ == "__main__":
    unittest.main()
