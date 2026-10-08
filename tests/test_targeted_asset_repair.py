import unittest
from app.services.targeted_asset_repair import repair_payload

class TargetedRepairTests(unittest.TestCase):
    def setUp(self):
        self.result = {'script': {'scenes': [{'text': 'Original'}], 'selected_images': ['a.png']}}
        self.plan = {'existing_image_paths': ['a.png'], 'audio_path': 'voice.mp3', 'expected_image_count': 20}
    def test_images_only_preserves_script_audio_and_caps_missing_calls(self):
        patched = repair_payload({'duration': 10}, self.result, self.plan, 'images')
        self.assertEqual(patched['seeded_script']['scenes'], self.result['script']['scenes'])
        self.assertEqual(patched['seeded_script']['seed_audio_path'], 'voice.mp3')
        self.assertEqual(patched['repair_image_budget']['max_new_image_calls'], 19)
        self.assertFalse(patched['repair_regenerate_audio'])
        self.assertFalse(patched['auto_publish'])
    def test_narration_preserves_images_without_image_budget(self):
        patched = repair_payload({}, self.result, self.plan, 'narration')
        self.assertTrue(patched['repair_regenerate_audio'])
        self.assertNotIn('seed_audio_path', patched['seeded_script'])
        self.assertFalse(patched['repair_complete_visuals'])
        self.assertNotIn('repair_image_budget', patched)
    def test_caption_and_render_reuse_audio_and_images(self):
        for asset in ['captions', 'narration_caption_sync', 'render']:
            patched = repair_payload({}, self.result, self.plan, asset)
            self.assertEqual(patched['seeded_script']['seed_audio_path'], 'voice.mp3')
            self.assertFalse(patched['repair_complete_visuals'])
            self.assertFalse(patched['repair_regenerate_audio'])
    def test_missing_unselected_asset_blocks_instead_of_regenerating(self):
        with self.assertRaisesRegex(ValueError, 'Narração'):
            repair_payload({}, self.result, {}, 'images')
