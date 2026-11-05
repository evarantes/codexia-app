import tempfile
import unittest
from pathlib import Path

from app.services.entre_nos_dois_timeline import build_measured_timeline
from app.services.entre_nos_dois_visual_plan import (
    VisualPlanError, build_visual_pilot_plan,
)
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class VisualPilotPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        plan = prepare_speech_manifest()
        segments = []
        for item in plan["segments"]:
            path = Path(self.tmp.name) / f"{item['index']:02d}.mp3"
            path.write_bytes(b"ID3" + b"0" * 2048)
            segments.append({"index": item["index"], "speaker": item["speaker"], "path": str(path)})
        audio = {
            "program": "entre-nos-dois",
            "project_id": 42,
            "approval_status": "awaiting_human_review",
            "segments": segments,
        }
        self.timeline = build_measured_timeline(audio, probe=lambda path: 6.0)
        self.references = {
            "Gabriel": "projects/42/hosts/gabriel/approved.png",
            "Helena": "projects/42/hosts/helena/approved.png",
        }

    def invoke(self, **kwargs):
        return build_visual_pilot_plan(
            timeline=kwargs.get("timeline", self.timeline),
            reference_keys=kwargs.get("reference_keys", self.references),
        )

    def test_shots_follow_the_verified_speaker_and_audio(self):
        result = self.invoke()
        self.assertEqual(len(result["scenes"]), 4)
        self.assertEqual([s["camera_focus"] for s in result["scenes"]],
                         ["Helena", "Gabriel", "Helena", "Gabriel"])
        self.assertTrue(all(s["lip_sync_required"] for s in result["scenes"]))
        self.assertTrue(all(s["caption_max_lines"] == 2 for s in result["scenes"]))
        self.assertFalse(result["video_generated"])
        self.assertFalse(result["auto_publish"])

    def test_rejects_cross_project_visual_asset(self):
        refs = dict(self.references, Helena="projects/99/hosts/helena.png")
        with self.assertRaises(VisualPlanError):
            self.invoke(reference_keys=refs)

    def test_rejects_voice_substitution(self):
        self.timeline["segments"][0]["voice_id"] = "other-id"
        with self.assertRaises(VisualPlanError):
            self.invoke()

    def test_rejects_overlapping_scenes(self):
        self.timeline["segments"][1]["start_seconds"] = 0.0
        with self.assertRaises(VisualPlanError):
            self.invoke()

    def test_rejects_unapproved_publication(self):
        self.timeline["auto_publish"] = True
        with self.assertRaises(VisualPlanError):
            self.invoke()


if __name__ == "__main__":
    unittest.main()
