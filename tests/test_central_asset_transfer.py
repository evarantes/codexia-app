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

    def test_archive_rejects_relay_and_scopes_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = FastAPI(); app.include_router(router)
            roots = {kind: root / kind for kind in ('image', 'audio', 'video', 'caption', 'script')}
            env = {'CODEXIA_ASSET_ARCHIVE_ROLE': 'archive', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test-token'}
            task_file = roots['image'] / 'task' / 'one.png'
            other_file = roots['image'] / 'other' / 'one.png'
            task_file.parent.mkdir(parents=True)
            other_file.parent.mkdir(parents=True)
            task_file.write_bytes(b'task image')
            other_file.write_bytes(b'other task image')
            indexed = {
                'task': [{'kind': 'image', 'filename': 'one.png', 'path': str(task_file)}],
                'other': [{'kind': 'image', 'filename': 'one.png', 'path': str(other_file)}],
            }
            with patch.dict(os.environ, env), patch('app.services.central_asset_transfer.roots', return_value=roots), \
                 patch('app.services.central_asset_transfer.index_path', side_effect=lambda task: root / f'{task}.json'), \
                 patch('app.services.production_manifest._read_json', side_effect=lambda path: {'files': indexed.get(Path(path).stem, [])}), \
                 patch('app.services.production_manifest.load_manifest', return_value={}):
                client = TestClient(app)
                url = '/internal/production-assets/task/image/one.png'
                headers = {'X-Codexia-Asset-Token': 'test-token'}
                self.assertEqual(client.put(url, content=b'valid bytes').status_code, 401)
                # The main server is metadata/control-plane only. Old worker
                # clients are rejected before the media body is relayed here.
                self.assertEqual(client.put(url, content=b'valid bytes', headers=headers).status_code, 410)
                self.assertEqual(client.get(url, headers=headers).content, b'task image')
                other_url = url.replace('/task/', '/other/')
                self.assertEqual(client.get(other_url, headers=headers).content, b'other task image')
                self.assertEqual(client.get(url.replace('/task/', '/missing/'), headers=headers).status_code, 404)

    def test_unconfirmed_upload_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'https://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            p = Path(directory) / 'one.png'; p.write_bytes(b'image')
            transfer = CentralAssetTransfer()
            index_response = Mock(status_code=200); index_response.json.return_value = {'files': []}
            session_response = Mock(status_code=200)
            session_response.json.return_value = {
                'upload_url': 'https://www.googleapis.com/upload/drive/v3/files?upload_id=test',
                'drive_account_id': 1,
            }
            query_response = Mock(status_code=308, headers={})
            upload_response = Mock(status_code=201)
            upload_response.json.return_value = {
                'id': 'drive-file', 'size': str(p.stat().st_size), 'md5Checksum': 'wrong',
            }
            transfer.session.get = Mock(return_value=index_response)
            transfer.session.post = Mock(return_value=session_response)
            transfer.session.put = Mock()
            with patch('app.services.central_asset_transfer.requests.put', side_effect=[query_response, upload_response]):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    transfer.publish('task', p, 'image')
            self.assertTrue(p.exists())
            self.assertFalse(_ACK)
            transfer.session.put.assert_not_called()

    def test_publish_retries_name_conflict_with_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CODEXIA_ASSET_ARCHIVE_URL': 'https://archive.example', 'CODEXIA_ASSET_TRANSFER_TOKEN': 'test'}):
            p = Path(directory) / 'one.png'; p.write_bytes(b'new image')
            transfer = CentralAssetTransfer()
            index_response = Mock(status_code=200); index_response.json.return_value = {'files': []}
            old_sha = hashlib.sha256(b'old image').hexdigest()
            new_sha = hashlib.sha256(b'new image').hexdigest()
            conflict = Mock(status_code=409)
            conflict.json.return_value = {'detail': {'existing_sha256': old_sha}}
            session_response = Mock(status_code=200)
            session_response.json.return_value = {
                'upload_url': 'https://www.googleapis.com/upload/drive/v3/files?upload_id=test',
                'drive_account_id': 1,
            }
            registered = Mock(status_code=200)
            registered.json.return_value = {'entry': {'sha256': new_sha, 'size': p.stat().st_size}}
            query_response = Mock(status_code=308, headers={})
            upload_response = Mock(status_code=201)
            upload_response.json.return_value = {
                'id': 'drive-file', 'size': str(p.stat().st_size),
                'md5Checksum': hashlib.md5(b'new image').hexdigest(),
            }
            transfer.session.get = Mock(return_value=index_response)
            transfer.session.post = Mock(side_effect=[conflict, session_response, registered])
            transfer.session.put = Mock()

            with patch('app.services.central_asset_transfer.requests.put', side_effect=[query_response, upload_response]):
                transfer.publish('task', p, 'image')

            self.assertEqual(transfer.session.post.call_count, 3)
            self.assertEqual(transfer.session.post.call_args_list[0].kwargs['json']['expected_previous_sha256'], '')
            self.assertEqual(transfer.session.post.call_args_list[1].kwargs['json']['expected_previous_sha256'], old_sha)
            self.assertEqual(transfer.session.post.call_args_list[0].args[0].rsplit('/', 1)[-1], 'upload-session')
            transfer.session.put.assert_not_called()
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
            response = Mock(); response.json.return_value = {'files': [
                {'kind': 'image', 'filename': 'one.png', 'drive_file_id': 'own-drive-id',
                 'sha256': hashlib.sha256(b'own').hexdigest(), 'size': len(b'own')},
                {'kind': 'image', 'filename': 'shared.png', 'drive_file_id': 'shared-drive-id',
                 'sha256': hashlib.sha256(b'shared').hexdigest(), 'size': len(b'shared')},
            ]}
            transfer.session.get = Mock(return_value=response)
            with patch('app.services.production_manifest.manifest_dir', return_value=taskdir), \
                 patch('app.services.production_manifest._root_dir', return_value=root), \
                 patch('app.services.central_asset_transfer.roots', return_value={'image': root}), \
                 patch('app.services.production_manifest.load_manifest', return_value={'artifacts': [
                     {'kind': 'image', 'original_path': str(own)},
                     {'kind': 'image', 'original_path': str(shared)},
                 ]}), \
                 patch('app.services.production_manifest._read_json', return_value={'artifacts': [{'original_path': str(shared)}]}):
                response.raise_for_status.side_effect = RuntimeError('archive unavailable')
                with self.assertRaises(RuntimeError):
                    transfer.clean_cache('task')
                self.assertTrue(own.exists())
                response.raise_for_status.side_effect = None
                transfer.clean_cache('task')
                self.assertFalse(own.exists())
                self.assertTrue(shared.exists())
