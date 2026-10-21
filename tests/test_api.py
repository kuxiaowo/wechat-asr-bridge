"""Contract and audio-validation checks; no live Weixin injection."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import wave
import subprocess

import imageio_ffmpeg

from fastapi.testclient import TestClient
from backend import BackendError, Transcription, normalize_audio, WechatBackend
import backend
import server


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        server.configure_backend('wechat')
        self.client = TestClient(server.app)
        self.audio = Path(__file__).with_name('fixtures').joinpath('test.wav').read_bytes()

    def post(self, fields=None, audio=None):
        return self.client.post('/v1/audio/transcriptions',
            data=fields or {'model': 'wechat-voice'},
            files={'file': ('audio.wav', self.audio if audio is None else audio, 'audio/wav')})

    def test_sdk_contract_and_formats(self):
        value = Transcription('你好，接口测试', 6.28, 123, 314, 10136)
        with patch.object(server.backend, 'transcribe', return_value=value):
            r = self.post()
            self.assertEqual(r.json(), {'text': value.text})
            self.assertEqual(r.headers['x-wechat-session-cleaned'], 'true')
            self.assertTrue(r.headers['x-request-id'].startswith('req_'))
            text = self.post({'model': 'wechat-voice', 'response_format': 'text'})
            self.assertEqual(text.text, value.text)
            self.assertIn('text/plain', text.headers['content-type'])

    def test_unsupported_features_are_explicit_errors(self):
        with patch.object(server.backend, 'transcribe') as call:
            for extra in ({'response_format': 'srt'}, {'stream': 'true'}, {'language': 'en'},
                          {'prompt': 'test'}, {'temperature': '1'}, {'include[]': 'logprobs'}):
                r = self.post({'model': 'wechat-voice', **extra})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()['error']['type'], 'invalid_request_error')
            call.assert_not_called()

    def test_authentication_and_backend_unavailable(self):
        with patch.dict(os.environ, {'WECHAT_API_KEY': 'test-only-key'}):
            self.assertEqual(self.client.get('/v1/models').status_code, 401)
            r = self.client.get('/v1/models', headers={'Authorization': 'Bearer test-only-key'})
            self.assertEqual(r.status_code, 200)
            self.assertEqual([x['id'] for x in r.json()['data']], ['wechat-voice'])
        with patch.object(server.backend, 'transcribe', side_effect=BackendError('请登录微信')):
            r = self.post()
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()['error']['code'], 'wechat_unavailable')

    def test_missing_and_invalid_input(self):
        self.assertEqual(self.client.post('/v1/audio/transcriptions', json={}).status_code, 400)
        self.assertEqual(self.post({'model': 'unsupported'}).status_code, 400)
        self.assertEqual(self.client.get('/v1/models/missing').status_code, 404)

    def test_audio_duration_is_not_silently_truncated(self):
        out = io.BytesIO()
        with wave.open(out, 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * 40 * 16000)
        pcm, seconds = normalize_audio(out.getvalue())
        self.assertEqual(seconds, 40)
        self.assertEqual(pcm, b'\x01\x00' * 40 * 16000)
        # Exercise the ffmpeg path with an actual compressed long input.
        compressed = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
             '-i', 'pipe:0', '-f', 'mp3', 'pipe:1'],
            input=out.getvalue(), capture_output=True, check=True, timeout=20,
        ).stdout
        decoded, seconds = normalize_audio(compressed)
        self.assertGreater(seconds, 39)
        self.assertGreater(len(decoded), 39 * 32000)
        with self.assertRaises(BackendError):
            normalize_audio(b'not an audio file')
        pcm, seconds = normalize_audio(self.audio)
        self.assertAlmostEqual(seconds, 6.28)
        self.assertEqual(len(pcm) % 640, 0)

    def test_unknown_dll_warns_but_loads_agent(self):
        bridge = WechatBackend()
        session, metadata, agent = MagicMock(), MagicMock(), MagicMock()
        session.is_detached = False
        session.create_script.side_effect = [metadata, agent]
        agent.exports_sync.recognize.return_value = {'done': True, 'events': [
            {'event': 'final_result', 'text': '测试'}], 'frames': 1, 'bytes': 20}
        agent.exports_sync.finalize.return_value = {'cleaned': True}
        agent.exports_sync.dispose.return_value = {'cleaned': True}
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / 'Weixin.dll'
            dll.write_bytes(b'unsupported-build')
            metadata.exports_sync.info.return_value = {'path': str(dll)}
            with patch.object(backend, 'discover_pid', return_value=123), patch.object(backend.frida, 'attach', return_value=session):
                self.assertEqual(bridge.transcribe(self.audio).text, '测试')
                self.assertFalse(bridge.verify_dll(dll))
                self.assertEqual(len(bridge.warnings), 1)
                self.assertEqual(bridge.warnings[0]['code'], 'unverified_wechat_version')
                bridge.close()
        self.assertEqual(session.create_script.call_count, 2)
        metadata.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_warnings_survive_json_text_and_failure_responses(self):
        server.backend.warn('unverified_wechat_version', '版本不同，继续尝试。')
        with patch.object(server.backend, 'transcribe', return_value=Transcription('测试', 1, 0, 1, 1)):
            result = self.post()
            warning = result.json()['warnings'][0]
            self.assertEqual(warning['message'], '版本不同，继续尝试。')
            self.assertEqual(json.loads(result.headers['x-asr-warnings']), [warning])
            plain = self.post({'model': 'wechat-voice', 'response_format': 'text'})
            self.assertEqual(plain.text, '测试')
            self.assertEqual(json.loads(plain.headers['x-asr-warnings']), [warning])
        fault = BackendError('原生调用失败：具体异常', 502, 'native_failure', details={'native_code': 42})
        with patch.object(server.backend, 'transcribe', side_effect=fault):
            result = self.post()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['warnings'], [warning])
        self.assertEqual(result.json()['error']['details'], {'native_code': 42})
        self.assertEqual(result.json()['error']['request_id'], result.headers['x-request-id'])
        state = self.client.get('/health').json()
        self.assertEqual(state['last_error_detail']['code'], 'native_failure')
        self.assertIn('具体异常', state['last_error_detail']['message'])
        self.assertEqual(state['warnings'], [warning])

    def test_unexpected_exception_is_visible_and_correlated(self):
        client = TestClient(server.app, raise_server_exceptions=False)
        with patch.object(server.backend, 'transcribe', side_effect=RuntimeError('specific native failure')):
            result = client.post('/v1/audio/transcriptions', data={'model': 'wechat-voice'},
                                 files={'file': ('x.wav', self.audio)})
        self.assertEqual(result.status_code, 500)
        error = result.json()['error']
        self.assertIn('specific native failure', error['message'])
        self.assertIn('RuntimeError: specific native failure', error['details'])
        self.assertEqual(error['request_id'], result.headers['x-request-id'])

    def test_cleanup_failure_stops_future_native_calls(self):
        bridge = WechatBackend()
        session, metadata, agent = MagicMock(), MagicMock(), MagicMock()
        session.is_detached = False
        session.create_script.side_effect = [metadata, agent]
        metadata.exports_sync.info.return_value = {'path': 'test.dll'}
        agent.exports_sync.recognize.return_value = {
            'done': True, 'events': [{'event': 'final_result', 'text': '测试'}],
            'frames': 1, 'bytes': 20,
        }
        agent.exports_sync.finalize.return_value = {'cleaned': False}
        with patch.object(backend, 'discover_pid', return_value=123), patch.object(backend.frida, 'attach', return_value=session) as attach, patch.object(bridge, 'verify_dll'):
            with self.assertRaises(BackendError) as error:
                bridge.transcribe(self.audio)
            self.assertEqual(error.exception.code, 'cleanup_failed')
            self.assertFalse(bridge.healthy)
            with self.assertRaises(BackendError):
                bridge.transcribe(self.audio)
            attach.assert_called_once()
        session.detach.assert_not_called()
        agent.unload.assert_not_called()
        agent.exports_sync.dispose.return_value = {'cleaned': False}
        with self.assertRaises(BackendError):
            bridge.close()
        session.detach.assert_not_called()
        agent.unload.assert_not_called()

    def test_agent_is_reused_and_closed_only_on_shutdown(self):
        bridge = WechatBackend()
        session, metadata, agent = MagicMock(), MagicMock(), MagicMock()
        session.is_detached = False
        session.create_script.side_effect = [metadata, agent]
        metadata.exports_sync.info.return_value = {'path': 'test.dll'}
        agent.exports_sync.recognize.return_value = {
            'done': True, 'events': [{'event': 'final_result', 'text': '测试'}],
            'frames': 1, 'bytes': 20,
        }
        agent.exports_sync.finalize.return_value = {'cleaned': True}
        agent.exports_sync.dispose.return_value = {'cleaned': True}
        with patch.object(backend, 'discover_pid', return_value=123), patch.object(backend.frida, 'attach', return_value=session) as attach, patch.object(bridge, 'verify_dll'):
            bridge.transcribe(self.audio)
            bridge.transcribe(self.audio)
            attach.assert_called_once_with(123)
            self.assertEqual(session.create_script.call_count, 2)
            self.assertEqual(agent.exports_sync.recognize.call_count, 2)
            session.detach.assert_not_called()
            agent.unload.assert_not_called()
            bridge.close()
        agent.exports_sync.dispose.assert_called_once()
        agent.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_target_exit_does_not_permanently_block_new_process(self):
        bridge = WechatBackend()
        sessions = [MagicMock(), MagicMock()]
        agents = []
        for session in sessions:
            metadata, agent = MagicMock(), MagicMock()
            session.is_detached = False
            session.create_script.side_effect = [metadata, agent]
            metadata.exports_sync.info.return_value = {'path': 'test.dll'}
            agent.exports_sync.recognize.return_value = {
                'done': True, 'events': [{'event': 'final_result', 'text': '测试'}],
                'frames': 1, 'bytes': 20,
            }
            agent.exports_sync.finalize.return_value = {'cleaned': True}
            agent.exports_sync.dispose.return_value = {'cleaned': True}
            agents.append(agent)
        def exited(*args):
            sessions[0].is_detached = True
            raise RuntimeError('process terminated')
        agents[0].exports_sync.recognize.side_effect = exited
        with patch.object(backend, 'discover_pid', side_effect=[123, 456]), patch.object(backend.frida, 'attach', side_effect=sessions) as attach, patch.object(bridge, 'verify_dll'):
            with self.assertRaises(BackendError):
                bridge.transcribe(self.audio)
            self.assertTrue(bridge.healthy)
            result = bridge.transcribe(self.audio)
            self.assertEqual(result.pid, 456)
            self.assertEqual(attach.call_count, 2)
            bridge.close()
        agents[0].exports_sync.finalize.assert_not_called()
        agents[0].exports_sync.dispose.assert_not_called()

    def test_discovery_failure_is_visible_in_health_state(self):
        bridge = WechatBackend()
        with patch.object(backend, 'discover_pid', side_effect=BackendError('微信进程不存在')):
            with self.assertRaises(BackendError):
                bridge.transcribe(self.audio)
        self.assertEqual(bridge.last_error, '微信进程不存在')

    def test_uncertain_cleanup_can_recover_after_target_exits(self):
        bridge = WechatBackend()
        old_session = MagicMock()
        old_session.is_detached = True
        bridge._session = old_session
        bridge._script = MagicMock()
        bridge._pid = 123
        bridge.healthy = False
        bridge.last_error = 'cleanup_failed'
        session, metadata, agent = MagicMock(), MagicMock(), MagicMock()
        session.is_detached = False
        session.create_script.side_effect = [metadata, agent]
        metadata.exports_sync.info.return_value = {'path': 'test.dll'}
        agent.exports_sync.recognize.return_value = {
            'done': True, 'events': [{'event': 'final_result', 'text': '测试'}],
            'frames': 1, 'bytes': 20,
        }
        agent.exports_sync.finalize.return_value = {'cleaned': True}
        agent.exports_sync.dispose.return_value = {'cleaned': True}
        with patch.object(backend, 'discover_pid', return_value=456), patch.object(backend.frida, 'attach', return_value=session), patch.object(bridge, 'verify_dll'):
            self.assertEqual(bridge.transcribe(self.audio).pid, 456)
            self.assertTrue(bridge.healthy)
            self.assertIsNone(bridge.last_error)
            bridge.close()
        old_session.detach.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
