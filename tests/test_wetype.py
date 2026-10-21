"""WeType protocol, native compatibility checks and ownership tests."""
from pathlib import Path
import struct
import gc
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from backend import BackendError
from wetype_backend import REQUIRED_MODULES, WetypeBackend
from native_compat import WETYPE_ENCODER_SIGNATURES, resolve_wetype_encoder
from wetype_protocol import ProtocolError, blob, fields, integer, opus_packets, request, response, value
import wetype_backend
import server
from fastapi.testclient import TestClient


def wire_response(voice_id, text='测试', *, ack=2, processing=0, end_flag=0, ret=0):
    return blob(1, blob(1, integer(1, ret)) + blob(2, voice_id.encode('ascii'))
                + blob(4, text.encode('utf-8')) + integer(3, 100202)
                + integer(5, end_flag) + integer(6, 100)
                + integer(12, processing) + integer(18, ack))


class WireTests(unittest.TestCase):
    def test_frame_split_preserves_every_byte_and_single_header(self):
        header = b'#!OPUS_RAW_V1\x02\x01\x00'
        stream = header + b''.join(struct.pack('<H', 3) + bytes([i % 256]) * 3 for i in range(751))
        packets, frames = opus_packets(stream)
        self.assertEqual(frames, 751)
        self.assertEqual(len(packets), 4)
        self.assertEqual(b''.join(packets), stream)
        self.assertTrue(packets[0].startswith(header))
        self.assertEqual(len(packets[-1]), 5)
        for invalid in (header, header + b'\x01', header + b'\x05\0abc', b'OggS'):
            with self.assertRaises(ProtocolError):
                opus_packets(invalid)

    def test_request_has_audio_end_sequence_and_no_rewrite(self):
        payload = value(fields(request('id', b'audio', 3, 400, end=True)), 1, 2, None)
        inner = fields(payload)
        self.assertEqual(value(inner, 4, 2, None), b'audio')
        self.assertEqual(value(inner, 6, 0, 0), 1)
        self.assertEqual(value(inner, 7, 0, 0), 3)
        self.assertEqual(value(inner, 11, 0, 0), 400)
        self.assertNotIn(22, inner)
        self.assertNotIn(23, inner)
        self.assertNotIn(24, inner)

    def test_response_rejects_malformed_wrong_session_and_business_error(self):
        self.assertEqual(response(wire_response('id'), 'id').text, '测试')
        for data in (b'', b'\0', b'\x0a\xff', b'\x08' + b'\xff' * 10,
                     wire_response('other'), wire_response('id', ret=123),
                     blob(1, blob(2, b'id') + integer(4, 1)),
                     blob(1, blob(2, b'id') + blob(4, b'\xff'))):
            with self.assertRaises(ProtocolError):
                response(data, 'id')


class EncoderCompatibilityTests(unittest.TestCase):
    def executable(self, rva=0x2000, *, duplicate=False, executable=True):
        code = b'\xcc' * 32
        expected = {}
        for original, signature in WETYPE_ENCODER_SIGNATURES.items():
            expected[original] = rva + len(code)
            code += signature + b'\xcc' * 32
        if duplicate:
            code += next(iter(WETYPE_ENCODER_SIGNATURES.values()))
        data = bytearray(0x200) + code
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 0x3c, 0x80)
        data[0x80:0x84] = b'PE\0\0'
        struct.pack_into('<HH', data, 0x84, 0x8664, 1)
        struct.pack_into('<H', data, 0x94, 0xf0)
        struct.pack_into('<IIII', data, 0x190, len(code), rva, len(code), 0x200)
        struct.pack_into('<I', data, 0x1ac, 0x20000000 if executable else 0)
        return bytes(data), expected

    def test_addresses_follow_relocated_code(self):
        for rva in (0x2000, 0x5000):
            data, expected = self.executable(rva)
            self.assertEqual(resolve_wetype_encoder(data), expected)

    def test_rejects_ambiguous_missing_and_nonexecutable_matches(self):
        for data in (self.executable(duplicate=True)[0], self.executable(executable=False)[0],
                     self.executable()[0][:-40], b'unknown executable'):
            with self.assertRaises(ValueError):
                resolve_wetype_encoder(data)


class WetypeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.bridge = WetypeBackend()
        self.session, self.agent = MagicMock(), MagicMock()
        self.session.is_detached = False
        self.bridge._session, self.bridge._script, self.bridge._pid = self.session, self.agent, 123
        self.agent.exports_sync.finalize.return_value = {'cleaned': True}
        self.agent.exports_sync.dispose.return_value = {'cleaned': True}

    def test_incompatible_code_does_not_load_native_agent(self):
        bridge = WetypeBackend()
        session, metadata = MagicMock(), MagicMock()
        session.create_script.return_value = metadata
        with tempfile.TemporaryDirectory() as directory:
            modules = []
            for name in REQUIRED_MODULES:
                path = Path(directory) / name
                path.write_bytes(b'unknown build')
                modules.append({'name': name, 'path': str(path)})
            metadata.exports_sync.modules.return_value = modules
            with patch.object(wetype_backend.frida, 'attach', return_value=session):
                with self.assertRaises(BackendError) as error:
                    bridge._ensure_agent(123)
        self.assertEqual(error.exception.code, 'wetype_encoder_unavailable')
        self.assertEqual(session.create_script.call_count, 1)
        metadata.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_changed_modules_warn_and_resolve_relocated_functions(self):
        data, expected = EncoderCompatibilityTests().executable(0x5000)
        bridge = WetypeBackend()
        with tempfile.TemporaryDirectory() as directory:
            modules = []
            for name in REQUIRED_MODULES:
                path = Path(directory) / name
                path.write_bytes(data if name.endswith('.exe') else b'changed transport')
                modules.append({'name': name, 'path': str(path)})
            bridge.verify_modules(modules)
            bridge.verify_modules(modules)
        self.assertEqual(bridge._encoder_addresses, expected)
        self.assertEqual(len(bridge.warnings), 3)
        self.assertTrue(all(w['code'] == 'unverified_wetype_version' for w in bridge.warnings))

    def test_cleanup_failure_retains_callback_and_stops_new_calls(self):
        self.agent.exports_sync.finalize.return_value = {'cleaned': False}
        self.agent.exports_sync.dispose.return_value = {'cleaned': False}
        with patch.object(wetype_backend, 'normalize_audio', return_value=(b'pcm', 1)), \
             patch.object(self.bridge, '_recognize', return_value=('测试', 1, 10, 'server_end_flag')) as recognize:
            for _ in range(2):
                with self.assertRaises(BackendError) as error:
                    self.bridge.transcribe(b'audio')
                self.assertEqual(error.exception.code, 'cleanup_failed')
            recognize.assert_called_once()
        with self.assertRaises(BackendError):
            self.bridge.close()
        self.agent.unload.assert_not_called()
        self.session.detach.assert_not_called()

    def test_protocol_failure_still_finalizes_and_agent_can_close(self):
        with patch.object(wetype_backend, 'normalize_audio', return_value=(b'pcm', 1)), \
             patch.object(self.bridge, '_recognize', side_effect=ProtocolError('wrong session')):
            with self.assertRaises(BackendError) as error:
                self.bridge.transcribe(b'audio')
        self.assertEqual(error.exception.code, 'wetype_protocol_error')
        self.agent.exports_sync.finalize.assert_called_once()
        self.assertEqual(self.bridge._active_requests, 0)
        self.bridge.close()
        self.agent.unload.assert_called_once()
        self.session.detach.assert_called_once()

    def test_acknowledged_stable_text_without_end_flag_is_not_complete(self):
        self.agent.exports_sync.encode.return_value = list(b'#!OPUS_RAW_V1\x02\x01\x00\x01\x00a')
        def answer(data):
            inner = fields(value(fields(bytes(data)), 1, 2, None))
            voice_id = value(inner, 2, 2, None).decode('ascii')
            return {'code': 0, 'data': list(wire_response(voice_id, '完整但未确认', ack=2))}
        self.agent.exports_sync.request.side_effect = answer
        # Start/deadline checks, then first poll is already out of time.
        with patch.object(wetype_backend.time, 'monotonic', side_effect=[0, 0, 0, 40]), \
             patch.object(wetype_backend.time, 'sleep'):
            with self.assertRaises(BackendError) as error:
                self.bridge._recognize(self.agent, b'\0' * 640, 1)
        self.assertEqual(error.exception.code, 'recognition_timeout')

    def test_server_end_flag_completes_without_heuristic(self):
        self.agent.exports_sync.encode.return_value = list(b'#!OPUS_RAW_V1\x02\x01\x00\x01\x00a')
        def answer(data):
            inner = fields(value(fields(bytes(data)), 1, 2, None))
            voice_id = value(inner, 2, 2, None).decode('ascii')
            return {'code': 0, 'data': list(wire_response(voice_id, end_flag=1))}
        self.agent.exports_sync.request.side_effect = answer
        self.assertEqual(self.bridge._recognize(self.agent, b'\0' * 640, 1)[3], 'server_end_flag')

    def test_incomplete_native_encoding_is_never_submitted(self):
        self.agent.exports_sync.encode.return_value = list(b'#!OPUS_RAW_V1\x02\x01\x00\x01\x00a')
        with self.assertRaises(ProtocolError):
            self.bridge._recognize(self.agent, b'\0' * 1280, 1)
        self.agent.exports_sync.request.assert_not_called()


class WetypeApiTests(unittest.TestCase):
    def setUp(self):
        # A previous native Tk fixture can leave cycles; collect them on the
        # owning main thread before TestClient starts an ASGI worker thread.
        gc.collect()
        server.configure_backend('wechat')

    def test_selected_provider_models_health_and_headers(self):
        state = server.backend, server.MODELS, server.MAX_CONCURRENCY, server.gate
        try:
            server.configure_backend('wetype')
            client = TestClient(server.app)
            names = [m['id'] for m in client.get('/v1/models').json()['data']]
            self.assertEqual(names, ['wetype-voice'])
            self.assertEqual(client.get('/health').json()['max_concurrency'], 4)
            result = wetype_backend.WetypeTranscription('测试', 1, 123, 50, 100, 'server_end_flag')
            with patch.object(server.backend, 'transcribe', return_value=result):
                reply = client.post('/v1/audio/transcriptions', data={'model': 'wetype-voice'},
                                    files={'file': ('test.wav', b'synthetic')})
                self.assertEqual(reply.json(), {'text': '测试'})
                self.assertEqual(reply.headers['x-asr-completion'], result.completion)
            server.configure_backend('wechat')
            self.assertEqual(server.MODELS, ('wechat-voice',))
            self.assertEqual(server.MAX_CONCURRENCY, 4)
        finally:
            server.backend, server.MODELS, server.MAX_CONCURRENCY, server.gate = state

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for callback ownership checks')
    def test_delayed_native_callbacks_and_timeouts(self):
        subprocess.run([shutil.which('node'), str(Path(__file__).with_name('verify_wetype_agent.cjs'))],
                       check=True, timeout=15)


if __name__ == '__main__':
    unittest.main()
