from __future__ import annotations

import unittest

from app.services.ai_generator import AIContentGenerator
from app.services.narration_contract_guard import NarrationContractError, validate_narration_text
from app.services.video_generator import VideoGenerator


class GlobalNarrationContractTests(unittest.TestCase):
    def test_plain_narration_is_accepted(self):
        text = "Jesus permanece conosco mesmo nos dias difíceis. Continue firme na esperança."
        self.assertEqual(validate_narration_text(text), text)

    def test_json_payload_is_blocked_by_provider_boundary(self):
        ai = AIContentGenerator.__new__(AIContentGenerator)
        with self.assertRaises(NarrationContractError):
            ai._assert_tts_text_not_truncated(
                '{"scene": 1, "duration": 12, "text": "Nunca narre este JSON."}',
                provider="OpenAI TTS",
                max_chars=4096,
            )

    def test_python_code_is_blocked_by_video_tts_before_provider(self):
        video = VideoGenerator.__new__(VideoGenerator)
        with self.assertRaises(NarrationContractError):
            video.generate_audio(
                "def render_video():\n    return {'status': 'processing', 'progress': 89}",
                segment_label="narração principal",
            )

    def test_serialized_technical_payload_is_blocked(self):
        bad = "status: processing\nprogress: 89\noutput_path: /data/videos/final.mp4"
        with self.assertRaises(NarrationContractError):
            validate_narration_text(bad, require_terminal_sentence=False)


if __name__ == "__main__":
    unittest.main()
