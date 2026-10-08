import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app.services.complete_repair_images import complete_repair_images

class CompleteImagesTests(unittest.TestCase):
    def test_14_to_48_completes_before_return_and_checkpoints_every_file(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for n in range(48):
                p=Path(directory)/f'{n}.png';p.write_bytes(b'image');paths.append(str(p))
            renderer=Mock();renderer._resolve_input_image_path.side_effect=lambda p:p
            generated=iter(paths[14:])
            def generate(*args, **kw):
                kw['paid_call_guard']()
                return next(generated)
            renderer._ensure_image_for_scene.side_effect=generate
            script={'scenes':[{'text':'Preserved'}], 'selected_images':paths[:14],
                    '_partial_image_recovery':{'enabled':True,'existing_image_count':14,'expected_image_count':48,'missing_image_count':34,'max_new_image_calls':34}}
            counts=[]
            actual=complete_repair_images(script,renderer,'16:9',lambda p,t,m:counts.append(len(p)),lambda:None)
            self.assertEqual(len(actual),48)
            self.assertEqual(renderer._ensure_image_for_scene.call_count,34)
            self.assertEqual(counts,[14]+list(range(14,49)))
            self.assertNotIn('_partial_image_recovery',script)
            self.assertFalse(script['repair_complete_visuals'])
    def test_provider_failure_never_returns_partial_set_for_render(self):
        renderer=Mock();renderer._ensure_image_for_scene.side_effect=RuntimeError('quota')
        script={'scenes':[{'text':'scene'}], 'selected_images':[], '_partial_image_recovery':{'enabled':True,'expected_image_count':1,'missing_image_count':1,'max_new_image_calls':1}}
        with self.assertRaisesRegex(RuntimeError,'quota'):
            complete_repair_images(script,renderer,'16:9',lambda *a:None,lambda:None)

    def test_worker_recovers_stale_api_paths_before_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'preserved.png';p.write_bytes(b'image')
            renderer=Mock();renderer._resolve_input_image_path.side_effect=lambda path:path
            script={'scenes':[{'text':'scene'}], 'selected_images':['/old/api/image.png'],
                    '_partial_image_recovery':{'enabled':True,'existing_image_count':1,'expected_image_count':1,'missing_image_count':0,'max_new_image_calls':0}}
            with patch('app.services.production_manifest.resolve_recovery_image_paths',return_value={'paths':[str(p)]}) as resolve:
                self.assertEqual(complete_repair_images(script,renderer,'16:9',lambda *a:None,lambda:None,task_id='task'),[str(p)])
            resolve.assert_called_once_with('task',['/old/api/image.png'],expected_count=1)
            renderer._ensure_image_for_scene.assert_not_called()
