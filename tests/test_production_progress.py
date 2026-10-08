import unittest
from datetime import datetime, timezone, timedelta
from app.services.production_progress import record_progress, activity_status, MeasuredRenderLogger


class ProgressTests(unittest.TestCase):
    def test_heartbeat_does_not_move_last_advance(self):
        now = datetime.now(timezone.utc)
        state = record_progress({}, 'render', 'Render vídeo: 10/100 quadros', now.isoformat())
        updated = record_progress(state, 'render', 'Renderizando arquivo final', (now + timedelta(minutes=5)).isoformat())
        self.assertEqual(updated['last_advance_at'], state['last_advance_at'])
        self.assertEqual(activity_status(updated, 'processing', now + timedelta(minutes=5))['state'], 'stalled')

    def test_real_frames_confirm_progress(self):
        now = datetime.now(timezone.utc)
        state = record_progress({}, 'render', 'Render vídeo: 10/100 quadros', now.isoformat())
        state = record_progress(state, 'render', 'Render vídeo: 20/100 quadros', (now + timedelta(seconds=30)).isoformat())
        self.assertEqual(state['render']['percent'], 20)
        self.assertEqual(activity_status(state, 'processing', now + timedelta(seconds=40))['state'], 'advancing')
        self.assertEqual(activity_status(state, 'processing', now + timedelta(minutes=4))['state'], 'stale')

    def test_legacy_task_is_unknown_not_active(self):
        self.assertEqual(activity_status({}, 'processing')['state'], 'unknown')

    def test_logger_measures_indices_not_total_changes(self):
        events = []
        logger = MeasuredRenderLogger(lambda p, msg: events.append(msg), '')
        logger(t__total=100)
        self.assertEqual(events, [])
        logger(t__index=10)
        self.assertEqual(events, ['Render vídeo: 10/100 quadros'])
