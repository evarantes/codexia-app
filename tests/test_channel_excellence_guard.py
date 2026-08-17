from __future__ import annotations

import os
import unittest

from app.services.channel_excellence_guard import (
    install_channel_excellence_guard_patch,
    premium_endcard_lines,
    prepare_spoken_text,
)


class _DummyGenerator:
    def __init__(self):
        self.audio_text = None
        self.received_plan = None

    def generate_audio(self, text, *args, **kwargs):
        self.audio_text = text
        return "/tmp/audio.mp3"

    def create_video_from_plan(self, plan, *args, **kwargs):
        self.received_plan = plan
        return {"file_path": "/tmp/fake.mp4"}

    def _resolve_contextual_closing(self, plan=None):
        return {"kind": "custom", "lines": ["Esta é uma reflexão final longa demais para uma tela pequena e deve ser reduzida para ficar elegante e legível no celular."]}


class ChannelExcellenceGuardTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ENABLE_CHANNEL_EXCELLENCE_GUARD", None)

    def test_spoken_text_repairs_pelo_contrario_and_dangling_opening(self):
        text = prepare_spoken_text("Uma mensagem de... Pelo contrário, Deus continua presente.")
        self.assertNotIn("Uma mensagem de", text)
        self.assertIn("muito pelo contrário", text.lower())

    def test_endcard_is_limited_to_two_mobile_lines(self):
        lines = premium_endcard_lines(
            "Mesmo quando tudo parece silencioso, Deus continua perto de você. Leve esta esperança para o seu dia e continue caminhando pela fé."
        )
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 50 for line in lines))

    def test_patch_sanitizes_audio_and_enforces_short_endcard(self):
        cls = type("ExcellenceGenerator", (_DummyGenerator,), {})
        install_channel_excellence_guard_patch(cls)
        instance = cls()
        instance.generate_audio("Pelo contrário, continue firme.")
        self.assertIn("muito pelo contrário", instance.audio_text.lower())

        instance.create_video_from_plan({
            "title": "Teste",
            "final_message": "Jesus continua presente mesmo quando você não percebe. Leve esta esperança com você hoje.",
            "scenes": [{"text": "Mensagem"}],
        })
        self.assertIsInstance(instance.received_plan["final_message"], list)
        self.assertLessEqual(len(instance.received_plan["final_message"]), 2)
        self.assertEqual(instance.received_plan["endcard_cta_text"], "Inscreva-se e acompanhe novas mensagens.")

    def test_explicit_disable_preserves_original_audio(self):
        os.environ["ENABLE_CHANNEL_EXCELLENCE_GUARD"] = "false"
        cls = type("DisabledExcellenceGenerator", (_DummyGenerator,), {})
        install_channel_excellence_guard_patch(cls)
        instance = cls()
        instance.generate_audio("Pelo contrário, continue.")
        self.assertEqual(instance.audio_text, "Pelo contrário, continue.")


if __name__ == "__main__":
    unittest.main()
