import tempfile
import unittest
from pathlib import Path

from app.services.entre_nos_dois_timeline import build_measured_timeline
from app.services.entre_nos_dois_lipsync_gate import (
    LipSyncGateError, prepare_lip_sync_jobs,
)
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class LipSyncGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        plan = prepare_speech_manifest()
        chunks = []
        for segment in plan["segments"]:
            p = Path(self.tmp.name) / f'{segment["index"]}.mp3'
            p.write_bytes(b"ID3" + b"0" * 2048)
            chunks.append({"index": segment["index"], "speaker": segment["speaker"], "path": str(p)})
        self.timeline = build_measured_timeline({
            "program": "entre-nos-dois", "project_id": 42,
            "approval_status": "awaiting_human_review", "segments": chunks,
        }, probe=lambda path: 5.0)
        self.refs = {
            "Gabriel": "projects/42/hosts/gabriel.png",
            "Helena": "projects/42/hosts/helena.png",
        }

    def call_gate(self, **kwargs):
        return prepare_lip_sync_jobs(timeline=self.timeline, reference_keys=self.refs, **kwargs)

    def test_no_credits_and_provider_means_no_generation(self):
        result = self.call_gate()
        self.assertFalse(result["jobs_ready"])
        self.assertFalse(result["billable_calls_performed"])

    def test_jobs_keep_voice_and_visual_identity(self):
        result = self.call_gate(approved_provider="lipsync_image_audio",
                                approved_max_credits=100, provider_estimate_credits=15)
        self.assertTrue(result["jobs_ready"])
        self.assertEqual([x["character"] for x in result["jobs"]],
                         ["Helena", "Gabriel", "Helena", "Gabriel"])
        self.assertTrue(all(x["provider_job_id"] is None for x in result["jobs"]))
        self.assertFalse(result["auto_publish"])

    def test_rejects_unknown_provider(self):
        with self.assertRaises(LipSyncGateError):
            self.call_gate(approved_provider="act_two",
                           approved_max_credits=100, provider_estimate_credits=15)

    def test_rejects_over_budget(self):
        with self.assertRaises(LipSyncGateError):
            self.call_gate(approved_provider="lipsync_image_audio",
                           approved_max_credits=10, provider_estimate_credits=15)

    def test_rejects_missing_audio(self):
        Path(self.timeline["segments"][0]["audio_path"]).unlink()
        with self.assertRaises(LipSyncGateError):
            self.call_gate(approved_provider="lipsync_image_audio",
                           approved_max_credits=100, provider_estimate_credits=15)


if __name__ == "__main__":
    unittest.main()
