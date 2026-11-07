import unittest

from app.services.entre_nos_dois import (
    HOSTS, PROGRAM_DISCLOSURE, TEST_DIALOGUE, build_test_plan, validate_dialogue
)


class EntreNosDoisTests(unittest.TestCase):
    def setUp(self):
        self.refs = {
            "Gabriel": "projects/42/hosts/gabriel/reference.png",
            "Helena": "projects/42/hosts/helena/reference.png",
        }
        self.voices = {"Gabriel": "ceOf6Sb1xKhC0ZVxHIzb", "Helena": "EMGk4qRFrRTnkdPoTzu3"}

    def test_stable_host_names(self):
        self.assertEqual([h.name for h in HOSTS], ["Gabriel", "Helena"])

    def test_test_plan_requires_review_and_disclosure(self):
        plan = build_test_plan(project_id=42, reference_keys=self.refs, voice_ids=self.voices)
        self.assertEqual(plan["target_duration_seconds"], 30)
        self.assertEqual(plan["caption_max_lines"], 2)
        self.assertTrue(plan["requires_human_approval"])
        self.assertFalse(plan["auto_publish"])
        self.assertEqual(plan["disclosure"], PROGRAM_DISCLOSURE)
        self.assertEqual(sum(x["target_seconds"] for x in plan["dialogue"]), 30)

    def test_missing_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            build_test_plan(project_id=42, reference_keys={"Gabriel": self.refs["Gabriel"]}, voice_ids=self.voices)

    def test_project_isolation(self):
        changed = dict(self.refs, Helena="projects/77/hosts/helena/reference.png")
        with self.assertRaises(ValueError):
            build_test_plan(project_id=42, reference_keys=changed, voice_ids=self.voices)

    def test_rejects_shared_reference(self):
        with self.assertRaises(ValueError):
            build_test_plan(project_id=42, reference_keys={"Gabriel": self.refs["Gabriel"], "Helena": self.refs["Gabriel"]}, voice_ids=self.voices)

    def test_wrong_approved_voice_is_rejected(self):
        with self.assertRaises(ValueError):
            build_test_plan(project_id=42, reference_keys=self.refs, voice_ids={"Gabriel": "wrong", "Helena": self.voices["Helena"]})

    def test_missing_approved_voice(self):
        with self.assertRaises(ValueError):
            build_test_plan(project_id=42, reference_keys=self.refs, voice_ids={"Gabriel": self.voices["Gabriel"]})

    def test_dialogue_duration_mismatch(self):
        with self.assertRaises(ValueError):
            validate_dialogue(TEST_DIALOGUE, target_seconds=31)


if __name__ == "__main__":
    unittest.main()
