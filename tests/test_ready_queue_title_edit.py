from __future__ import annotations

import unittest
from pathlib import Path

from scripts.apply_ready_queue_title_edit import patch_index


ROOT = Path(__file__).resolve().parents[1]


class ReadyQueueTitleEditTests(unittest.TestCase):
    def test_patch_adds_edit_button_and_save_method(self):
        raw = '''
<tr v-for="video in readyVideos">
                                        <td class="p-3 font-medium">{{ video.title }}</td>
</tr>
<script>
                async saveScheduledVideo(video) {
                    return video;
                },
</script>
'''
        patched = patch_index(raw)
        self.assertIn("CODEXIA_READY_QUEUE_TITLE_EDIT_V1", patched)
        self.assertIn("editReadyScheduledTitle(video)", patched)
        self.assertIn("JSON.stringify({ title })", patched)
        self.assertIn("máximo 100 caracteres", patched)
        self.assertEqual(patch_index(patched), patched)

    def test_runtime_backend_already_persists_title_for_scheduled_video(self):
        router = (ROOT / "app/routers/youtube.py").read_text(encoding="utf-8")
        self.assertIn('@router.put("/schedule/{video_id}")', router)
        self.assertIn('if "title" in data:', router)
        self.assertIn('video.title = data["title"]', router)

    def test_runtime_ui_contains_title_edit_after_hardening(self):
        index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("CODEXIA_READY_QUEUE_TITLE_EDIT_V1", index)
        self.assertIn("Título salvo. Este será o título usado em Publicar agora.", index)


if __name__ == "__main__":
    unittest.main()
