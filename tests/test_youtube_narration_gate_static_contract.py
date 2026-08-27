import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.apply_youtube_narration_gate as gate_patch


class YouTubeNarrationGateStaticContractTests(unittest.TestCase):
    def test_patch_is_idempotent_and_injects_only_one_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            js = root / "youtube_narration_gate.js"
            index.write_text("<html><body><main>Codexia</main></body></html>", encoding="utf-8")
            js.write_text(
                "Gerar primeiro o áudio da narração\n"
                "Avançar para geração do vídeo com este áudio\n"
                "/youtube/narration-lab/production-preview\n"
                "reuse_audio_from\n"
                "approved_narration_text_sha256\n",
                encoding="utf-8",
            )
            with patch.object(gate_patch, "INDEX", index), patch("pathlib.Path", wraps=Path):
                # apply() only depends on INDEX; validate the deterministic injection itself.
                self.assertTrue(gate_patch.apply())
                self.assertFalse(gate_patch.apply())
            self.assertEqual(index.read_text(encoding="utf-8").count(gate_patch.TAG), 1)


if __name__ == "__main__":
    unittest.main()
