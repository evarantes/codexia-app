import unittest
from types import SimpleNamespace
from unittest.mock import patch
import requests
from app.services.ai_generator import AIContentGenerator


class ElevenLabsFailureTests(unittest.TestCase):
    def generator(self):
        obj = AIContentGenerator.__new__(AIContentGenerator)
        obj.elevenlabs_key = 'secret-test-key'
        obj._resolve_elevenlabs_voice_selection = lambda hint: {'voice_id_used': 'voice'}
        return obj

    def test_http_failures_are_specific_and_not_empty_response(self):
        for code, hint in [('invalid_api_key', 'chave'), ('voice_not_found', 'Voice ID'),
                           ('quota_exceeded', 'cota'), ('too_many_concurrent_requests', 'simultâneas')]:
            response = SimpleNamespace(status_code=401, json=lambda: {'detail': {'status': code, 'message': 'secret-test-key'}})
            with self.subTest(code=code), patch('app.services.ai_generator.requests.post', return_value=response):
                with self.assertRaisesRegex(RuntimeError, hint) as error:
                    self.generator()._generate_audio_elevenlabs('Texto de teste')
                self.assertIn(code, str(error.exception))
                self.assertNotIn('secret-test-key', str(error.exception))

    def test_local_length_failure_does_not_call_provider(self):
        with patch('app.services.ai_generator.requests.post') as request:
            with self.assertRaises(ValueError):
                self.generator()._generate_audio_elevenlabs('a' * 10001)
            request.assert_not_called()

    def test_timeout_does_not_claim_provider_returned_empty(self):
        with patch('app.services.ai_generator.requests.post', side_effect=requests.Timeout()):
            with self.assertRaisesRegex(RuntimeError, 'tempo de resposta'):
                self.generator()._generate_audio_elevenlabs('Teste')

    def test_valid_audio_is_preserved(self):
        with patch('app.services.ai_generator.requests.post', return_value=SimpleNamespace(status_code=200, content=b'audio')):
            self.assertEqual(self.generator()._generate_audio_elevenlabs('Teste'), b'audio')

    def test_unknown_error_redacts_secret(self):
        response = SimpleNamespace(status_code=403, json=lambda: {'detail': {'status': 'other', 'message': 'bad secret-test-key'}})
        with patch('app.services.ai_generator.requests.post', return_value=response):
            with self.assertRaises(RuntimeError) as error:
                self.generator()._generate_audio_elevenlabs('Teste')
        self.assertNotIn('secret-test-key', str(error.exception))

    def test_multilingual_v2_accepts_text_above_old_5000_limit(self):
        with patch('app.services.ai_generator.requests.post', return_value=SimpleNamespace(status_code=200, content=b'audio')) as request:
            self.assertEqual(self.generator()._generate_audio_elevenlabs('a' * 6000), b'audio')
            self.assertEqual(len(request.call_args.kwargs['json']['text']), 6000)
