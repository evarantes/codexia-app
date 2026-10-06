"""A recovered render must replace the artifact inspected by the final gate."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.unified_video_pipeline import UnifiedVideoPipelineService
from tests.test_director_final_quality_gate import _unified, _task_result


class RetryArtifactTests(unittest.TestCase):
    def setUp(self):
        self.service = UnifiedVideoPipelineService()
        self.uv = _unified()
        self.uv.result_json = '{}'
        self.uv.youtube_video_id = None
        self.uv.youtube_url = None
        self.uv.published_at = None
        self.uv.review_required = True
        self.uv.last_error = 'Previous render was 5:15'
        self.uv.video_path = '/old.mp4'
        self.uv.video_url = '/media/old.mp4'
        self.uv.video_duration_seconds = 315.0
        self.db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    def merge(self, result):
        self.service._merge_result_artifacts(self.db, self.uv, result)

    def validate(self, durations):
        with patch.object(self.service, '_find_any', return_value=self.uv), patch(
            'app.services.unified_video_pipeline.get_task',
            return_value={'result': _task_result()},
        ), patch.object(self.service, '_file_exists_or_url', return_value=True), patch(
            'app.services.unified_video_pipeline._file_size_bytes', return_value=262144,
        ), patch('app.services.unified_video_pipeline._ffprobe_streams') as probe, patch(
            'app.services.unified_video_pipeline.absolute_path_for_audio', side_effect=lambda p: p,
        ), patch('app.services.unified_video_pipeline.absolute_path_for_video', side_effect=lambda p: p):
            probe.side_effect = lambda p: {
                'has_video': True, 'has_audio': True, 'video_duration': durations[p],
            } if p in durations else {}
            validation, _ = self.service.transition_to_awaiting_review_if_valid(self.db, self.uv.task_id)
            return validation

    def test_retry_validates_new_overlong_video_instead_of_old_short_video(self):
        self.merge({'file_path': '/new.mp4', 'video_path': '/alternate.mp4', 'video_url': '/media/new.mp4'})
        result = self.validate({'/old.mp4': 315.0, '/new.mp4': 605.0})
        self.assertTrue(result.ok, result.details)
        self.assertEqual(result.details['mp4']['path'], '/new.mp4')
        self.assertEqual(self.uv.video_url, '/media/new.mp4')
        self.assertEqual(self.uv.video_duration_seconds, 605.0)
        self.assertIsNone(self.uv.last_error)

    def test_slightly_short_replacement_is_still_rejected(self):
        self.merge({'file_path': '/short.mp4'})
        result = self.validate({'/old.mp4': 605.0, '/short.mp4': 591.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.first_failed, 'duration_matches_request')

    def test_short_replacement_is_still_rejected(self):
        self.merge({'file_path': '/short.mp4'})
        result = self.validate({'/old.mp4': 600.0, '/short.mp4': 315.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.first_failed, 'duration_matches_request')

    def test_missing_replacement_does_not_fall_back_to_old_good_video(self):
        self.merge({'file_path': '/missing.mp4'})
        self.assertFalse(self.validate({'/old.mp4': 600.0}).ok)

    def test_url_only_replacement_does_not_keep_old_path(self):
        self.merge({'video_url': '/media/new.mp4'})
        self.assertEqual(self.uv.video_path, '/media/new.mp4')
        self.assertEqual(self.uv.video_url, '/media/new.mp4')
        self.assertIsNone(self.uv.video_duration_seconds)

    def test_progress_update_preserves_existing_artifacts(self):
        self.merge({'pipeline_stage': 'rendering', 'video_url': None})
        self.assertEqual(self.uv.video_path, '/old.mp4')
        self.assertEqual(self.uv.video_url, '/media/old.mp4')
        self.assertEqual(self.uv.video_duration_seconds, 315.0)

    def test_replacement_audio_updates_duration_and_path(self):
        self.uv.call_count_audio = 1
        self.uv.audio_duration_seconds = 310.0
        self.merge({'audio_generation': {'final_audio_path': '/new.mp3', 'final_audio_duration_sec': 586.0}})
        self.assertEqual(self.uv.audio_path, '/new.mp3')
        self.assertEqual(self.uv.audio_duration_seconds, 586.0)

    def test_same_path_render_discards_old_measurement(self):
        self.merge({'file_path': '/old.mp4'})
        self.assertIsNone(self.uv.video_duration_seconds)
        self.assertIsNone(self.uv.video_url)
        self.assertTrue(self.validate({'/old.mp4': 605.0}).ok)


if __name__ == '__main__':
    unittest.main()
