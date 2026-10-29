import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import importlib.util
_spec = importlib.util.spec_from_file_location('production_assets_under_test', Path(__file__).resolve().parents[1] / 'app/routers/production_assets.py')
_routes = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_routes)
router = _routes.router
from app.services.central_asset_transfer import CentralAssetTransfer, map_cached_references, _ACK

class CentralTransferTests(unittest.TestCase):
    def setUp(self):
        _ACK.clear()

    def test_archive_auth_integrity_and_task_scoped_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = FastAPI(); app.include_router(router)
            roots = {kind: root / kind for kind in ('image', 'audio', 'video', 'caption', 'script')}
            env = {'CODEXIA_ASSET_ARCHIVE_ROLE': 'archive', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test-token'}
            with patch.dict(os.environ, env), patch('app.services.central_asset_transfer.roots', return_value=roots), \
                 patch('app.services.central_asset_transfer.index_path', side_effect=lambda task: root / f'{task}.json'), \
                 patch('app.services.production_manifest.load_manifest', return_value={}):
                client = TestClient(app)
                url = '/internal/production-assets/task/image/one.png'
                body = b'valid bytes'; sha = hashlib.sha256(body).hexdigest()
                self.assertEqual(client.put(url, content=body).status_code, 401)
                headers = {'X-Codexia-Asset-Token': 'test-token', 'X-Content-SHA256': sha}
                self.assertEqual(client.put(url, content=b'bad', headers=headers).status_code, 422)
                self.assertFalse((roots['image'] / 'task' / 'one.png').exists())
                self.assertEqual(client.put(url, content=body, headers=headers).status_code, 200)
                self.assertEqual(client.get(url, headers=headers).content, body)
                self.assertEqual(client.get(url.replace('/task/', '/other/'), headers=headers).status_code, 404)
                bad_headers = dict(headers, **{'X-Content-SHA256': hashlib.sha256(b'changed').hexdigest()})
                conflict = client.put(url, content=b'changed', headers=bad_headers)
                self.assertEqual(conflict.status_code, 409)
                old_sha = hashlib.sha256(body).hexdigest()
                self.assertEqual(conflict.json()['detail']['existing_sha256'], old_sha)
                self.assertEqual((roots['image'] / 'task' / 'one.png').read_bytes(), body)
                replace_headers = dict(bad_headers, **{'X-Expected-Previous-SHA256': old_sha})
                self.assertEqual(client.put(url, content=b'changed', headers=replace_headers).status_code, 200)
                self.assertEqual((roots['image'] / 'task' / 'one.png').read_bytes(), b'changed')
                stale_headers = dict(headers, **{
                    'X-Content-SHA256': hashlib.sha256(b'other change').hexdigest(),
                    'X-Expected-Previous-SHA256': old_sha,
                })
                stale = client.put(url, content=b'other change', headers=stale_headers)
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(stale.json()['detail']['existing_sha256'], hashlib.sha256(b'changed').hexdigest())
                # Same filename from another production is an independent asset.
                other_body = b'other production content'
                other_headers = dict(headers, **{'X-Content-SHA256': hashlib.sha256(other_body).hexdigest()})
                other_url = url.replace('/task/', '/other/')
                self.assertEqual(client.put(other_url, content=other_body, headers=other_headers).status_code, 200)
                self.assertEqual(client.get(other_url, headers=other_headers).content, other_body)
                self.assertEqual((roots['image'] / 'task' / 'one.png').read_bytes(), b'changed')
                self.assertEqual((roots['image'] / 'other' / 'one.png').read_bytes(), other_body)
                # Retries for identical content succeed without conflict.
                changed_headers = dict(headers, **{'X-Content-SHA256': hashlib.sha256(b'changed').hexdigest()})
                self.assertEqual(client.put(url, content=b'changed', headers=changed_headers).status_code, 200)
                self.assertEqual(client.get(url, headers=headers).content, b'changed')

    def test_unconfirmed_upload_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'https://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            p = Path(directory) / 'one.png'; p.write_bytes(b'image')
            transfer = CentralAssetTransfer()
            response = Mock(status_code=200); response.json.return_value = {'sha256': 'wrong', 'size': 5}
            transfer.session.put = Mock(return_value=response)
            with self.assertRaisesRegex(RuntimeError, 'integridade'):
                transfer.publish('task', p, 'image')
            self.assertTrue(p.exists())
            self.assertFalse(_ACK)

    def test_publish_retries_name_conflict_with_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'https://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            p = Path(directory) / 'one.png'; p.write_bytes(b'new image')
            transfer = CentralAssetTransfer()
            old_sha = hashlib.sha256(b'old image').hexdigest()
            new_sha = hashlib.sha256(b'new image').hexdigest()
            conflict = Mock(status_code=409)
            conflict.json.return_value = {'detail': {'existing_sha256': old_sha}}
            success = Mock(status_code=200)
            success.json.return_value = {'sha256': new_sha, 'size': p.stat().st_size}
            transfer.session.put = Mock(side_effect=[conflict, success])

            transfer.publish('task', p, 'image')

            self.assertEqual(transfer.session.put.call_count, 2)
            self.assertNotIn('X-Expected-Previous-SHA256', transfer.session.put.call_args_list[0].kwargs['headers'])
            self.assertEqual(transfer.session.put.call_args_list[1].kwargs['headers']['X-Expected-Previous-SHA256'], old_sha)
            self.assertEqual(_ACK[(transfer.url, 'task', str(p), 'image', 'one.png')], (p.stat().st_size, p.stat().st_mtime_ns))

    def test_hydration_maps_nested_seed_audio_and_image_paths(self):
        mapping = {'one.png': '/cache/one.png', 'voice.mp3': '/cache/voice.mp3'}
        result = map_cached_references({'seeded_script': {'seed_audio_path': '/old/voice.mp3'},
                                       'selected_images': ['/old/one.png']}, mapping)
        self.assertEqual(result['seeded_script']['seed_audio_path'], '/cache/voice.mp3')
        self.assertEqual(result['selected_images'], ['/cache/one.png'])

    def test_worker_refuses_insecure_archive_endpoint(self):
        with patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'http://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
                CentralAssetTransfer()

    def test_cleanup_requires_central_confirmation_and_preserves_shared_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'https://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            root = Path(directory); taskdir = root / 'task'; taskdir.mkdir()
            own = root / 'one.png'; own.write_bytes(b'own')
            shared = root / 'shared.png'; shared.write_bytes(b'shared')
            other = root / 'other'; other.mkdir(); (other / 'manifest.json').write_text('{}')
            transfer = CentralAssetTransfer()
            response = Mock(); response.json.return_value = {'files': [{'sha256': hashlib.sha256(b'own').hexdigest()}, {'sha256': hashlib.sha256(b'shared').hexdigest()}]}
            transfer.session.get = Mock(return_value=response)
            with patch('app.services.production_manifest.manifest_dir', return_value=taskdir), \
                 patch('app.services.production_manifest._root_dir', return_value=root), \
                 patch('app.services.central_asset_transfer.roots', return_value={'image': root}), \
                 patch('app.services.production_manifest.load_manifest', return_value={'artifacts': [{'original_path': str(own)}, {'original_path': str(shared)}]}), \
                 patch('app.services.production_manifest._read_json', return_value={'artifacts': [{'original_path': str(shared)}]}):
                response.raise_for_status.side_effect = RuntimeError('archive unavailable')
                with self.assertRaises(RuntimeError):
                    transfer.clean_cache('task')
                self.assertTrue(own.exists())
                response.raise_for_status.side_effect = None
                transfer.clean_cache('task')
                self.assertFalse(own.exists())
                self.assertTrue(shared.exists())
