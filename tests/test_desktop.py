"""Portable resource paths, service ownership and desktop API behavior."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import sys

import httpx

import app_paths
import desktop_service
from desktop_service import ServiceController, transcribe_file
import server
import desktop_settings
from desktop import DesktopApplication


class PortablePathsTests(unittest.TestCase):
    def test_frozen_assets_and_logs_use_different_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app_paths.sys, 'frozen', True, create=True), patch.object(app_paths.sys, '_MEIPASS', str(root / 'bundle'), create=True), patch.dict(app_paths.os.environ, {'LOCALAPPDATA': str(root / 'user')}):
                self.assertEqual(app_paths.resource_dir(), root / 'bundle')
                self.assertEqual(app_paths.runtime_dir(), root / 'user' / 'WeChatASRBridge' / 'runtime')

    def test_legacy_port_settings_and_bad_settings_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(desktop_service.saved_port(root), 8765)
            desktop_service.save_port(8768, root)
            self.assertEqual(desktop_service.saved_port(root), 8768)
            self.assertEqual(json.loads((root / 'desktop-settings.json').read_text()), {'port': 8768})
            (root / 'desktop-settings.json').write_text('{bad json')
            self.assertEqual(desktop_service.saved_port(root), 8765)

    def test_server_rejects_shutdown_signal_outside_its_runtime(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server.uvicorn, 'Server') as launch:
            root = Path(directory)
            with patch.object(server, 'runtime_dir', return_value=root / 'runtime'):
                with self.assertRaises(SystemExit) as error:
                    server.main(['--shutdown-file', str(root / 'outside')])
            self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI')
class PersistentKeyTests(unittest.TestCase):
    def test_key_round_trip_is_encrypted_and_survives_port_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = 'unit-only-key-你好'
            desktop_settings.save_settings(18765, key, root)
            self.assertNotIn(key, (root / 'desktop-settings.json').read_text(encoding='utf-8'))
            self.assertEqual(desktop_settings.saved_api_key(root), key)
            desktop_settings.save_port(18766, root)
            self.assertEqual(desktop_settings.saved_port(root), 18766)
            self.assertEqual(desktop_settings.saved_api_key(root), key)
            desktop_settings.save_settings(18766, '', root)
            self.assertEqual(desktop_settings.saved_api_key(root, default='environment-key'), '')

    def test_encryption_failure_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop_settings.save_settings(18765, 'existing-test-key', root)
            before = (root / 'desktop-settings.json').read_bytes()
            with patch.object(desktop_settings, '_crypt', side_effect=OSError('failed')):
                with self.assertRaises(OSError):
                    desktop_settings.save_settings(18766, 'replacement-test-key', root)
            self.assertEqual((root / 'desktop-settings.json').read_bytes(), before)

    def test_corrupt_encrypted_key_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'desktop-settings.json').write_text(json.dumps({'port': 18765, 'api_key_dpapi': 'not valid base64'}))
            with self.assertRaisesRegex(ValueError, '无法解密'):
                desktop_settings.saved_api_key(root)


class ServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = ServiceController(18765, runtime=self.root)

    def metadata(self, **changes):
        value = {'pid': 123, 'port': 18765, 'shutdown_file': str(self.root / 'desktop-stop-18765-test'),
                 'executable': str(Path(desktop_service.sys.executable).resolve())}
        value.update(changes)
        self.controller.metadata_path.write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_existing_service_is_reused_without_launching_a_child(self):
        current = {'backend': desktop_service.APP_BACKEND, 'status': 'ok'}
        with patch.object(desktop_service, 'health', return_value=current), patch.object(desktop_service.subprocess, 'Popen') as launch:
            self.assertEqual(self.controller.start(), current)
            launch.assert_not_called()
        self.assertFalse(self.controller.owns_service())

    def test_unrelated_listener_is_never_replaced(self):
        with patch.object(desktop_service, 'health', return_value=None), patch.object(desktop_service, 'port_in_use', return_value=True), patch.object(desktop_service.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '端口'):
                self.controller.start()
            launch.assert_not_called()

    def test_forged_pid_or_path_cannot_signal_another_program(self):
        data = self.metadata()
        info = {'ExecutablePath': data['executable'], 'CommandLine': 'unrelated program'}
        with patch.object(desktop_service, 'process_info', return_value=info):
            with self.assertRaisesRegex(RuntimeError, '其他程序'):
                self.controller.stop()
        self.assertFalse(Path(data['shutdown_file']).exists())
        self.metadata(shutdown_file=str(self.root.parent / 'outside'))
        self.assertIsNone(self.controller._metadata())
        self.metadata(executable='relative.exe')
        self.assertIsNone(self.controller._metadata())

    def test_verified_child_gets_only_its_graceful_signal(self):
        data = self.metadata()
        info = {'ExecutablePath': data['executable'],
                'CommandLine': f'"{data["executable"]}" --serve --shutdown-file "{data["shutdown_file"]}"'}
        with patch.object(desktop_service, 'process_info', side_effect=[info, None]):
            self.controller.stop()
        self.assertTrue(Path(data['shutdown_file']).is_file())

    def test_desktop_key_reaches_child_without_command_or_metadata_leak(self):
        current = {'backend': desktop_service.APP_BACKEND, 'status': 'ok'}
        for key in ('unit-only-desktop-key', ''):
            with self.subTest(key_set=bool(key)), \
                    patch.dict(desktop_service.os.environ, {'WECHAT_API_KEY': 'inherited-key'}), \
                    patch.object(desktop_service, 'health', side_effect=[None, current]), \
                    patch.object(desktop_service, 'port_in_use', return_value=False), \
                    patch.object(desktop_service.subprocess, 'Popen') as launch:
                launch.return_value.pid = 123
                launch.return_value.poll.return_value = None
                capacities={'wechat':108,'wetype':33,'doubao':64}
                self.controller.start(api_key=key,concurrency=capacities)
                args, options = launch.call_args
                for provider,capacity in capacities.items():
                    index=args[0].index('--'+provider+'-concurrency')
                    self.assertEqual(args[0][index+1],str(capacity))
                self.assertEqual(options['env'].get('WECHAT_API_KEY', ''), key)
                self.assertNotIn('unit-only-desktop-key', ' '.join(args[0]))
                self.assertNotIn('unit-only-desktop-key', self.controller.metadata_path.read_text())
                self.assertEqual(desktop_service.os.environ['WECHAT_API_KEY'], 'inherited-key')


class DesktopLifetimeTests(unittest.TestCase):
    def application(self):
        app = DesktopApplication.__new__(DesktopApplication)
        app.root = Mock()
        app.root.after = Mock()
        app.settings_window = None
        app.operation = None
        app.closed = app.exit_requested = app.tray_pending = False
        app.tray_ready = True
        app.tray = Mock()
        app._update_buttons = Mock()
        app.status = Mock()
        app.detail = Mock()
        app._show_error = Mock()
        app._operation_error = Mock()
        return app

    def test_close_during_recognition_keeps_window_and_service_alive(self):
        app = self.application()
        app.operation = 'recognizing'
        app._close()
        app.root.withdraw.assert_called_once()
        app.root.destroy.assert_not_called()
        self.assertFalse(app.exit_requested)
        app.tray.stop.assert_not_called()

    def test_exit_waits_for_launch_worker_and_only_stops_owned_children(self):
        app = self.application()
        owned, external = Mock(), Mock()
        owned.owns_service.return_value = True
        external.owns_service.return_value = False
        app.controllers = {18765: owned, 18766: external}
        app._work = Mock(side_effect=lambda action, success, failure: success(action()))
        app.operation = 'starting'
        app._request_exit()
        app._work.assert_not_called()
        app.root.destroy.assert_not_called()
        app.operation = None
        app.root.after.call_args.args[1]()
        owned.stop.assert_called_once()
        external.stop.assert_not_called()
        app.tray.stop.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_shutdown_failure_keeps_application_recoverable(self):
        app = self.application()
        owned = Mock()
        owned.owns_service.return_value = True
        owned.stop.side_effect = RuntimeError('cleanup still pending')
        app.controllers = {18765: owned}
        def run(action, success, failure):
            try:
                result = action()
            except RuntimeError as error:
                failure(str(error))
            else:
                success(result)
        app._work = run
        app._request_exit()
        app.root.destroy.assert_not_called()
        app.root.deiconify.assert_called_once()
        self.assertFalse(app.exit_requested)
        self.assertIsNone(app.operation)

    def test_tray_start_failure_does_not_hide_window(self):
        app = self.application()
        app.tray_ready = False
        with patch('desktop.TrayIcon', side_effect=RuntimeError('tray unavailable')):
            app._close()
        app.root.withdraw.assert_not_called()
        app.root.destroy.assert_not_called()
        app._show_error.assert_called_once()


class DesktopTranscriptionTests(unittest.TestCase):
    def test_full_error_http_status_code_and_request_id_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'test.wav'
            audio.write_bytes(b'audio')
            payload = {'error': {'message': '原生调用失败', 'code': 'native_failure',
                                 'details': 'access violation at function encoder'},
                       'warnings': [{'code': 'unverified_wetype_version', 'message': '输入法版本已变化'}]}
            client = httpx.Client(transport=httpx.MockTransport(lambda request:
                httpx.Response(503, json=payload, headers={'x-request-id': 'req_visible'})))
            with patch.object(httpx, 'Client', return_value=client):
                with self.assertRaises(RuntimeError) as error:
                    transcribe_file(audio, model='wetype-voice')
            for value in ('HTTP 503', 'native_failure', 'req_visible', 'access violation', '输入法版本已变化'):
                self.assertIn(value, str(error.exception))

    def test_non_json_error_body_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'test.wav'
            audio.write_bytes(b'audio')
            client = httpx.Client(transport=httpx.MockTransport(lambda request:
                httpx.Response(502, text='upstream connection refused')))
            with patch.object(httpx, 'Client', return_value=client):
                with self.assertRaisesRegex(RuntimeError, 'upstream connection refused'):
                    transcribe_file(audio)

    def test_success_retains_warnings_without_changing_transcription_text(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'test.wav'
            audio.write_bytes(b'audio')
            warnings = [{'code': 'unverified_wetype_version', 'message': '继续尝试'}]
            client = httpx.Client(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json={'text': '识别文字', 'warnings': warnings})))
            with patch.object(httpx, 'Client', return_value=client):
                result = transcribe_file(audio)
            self.assertEqual(result, '识别文字')
            self.assertEqual(result.warnings, warnings)

    def test_manual_failure_appears_in_copyable_result_area(self):
        app = DesktopApplication.__new__(DesktopApplication)
        app.controller, app.api_key, app.auth_enabled, app.model = Mock(), Mock(), Mock(), Mock()
        app.audio_path = Path('test.wav')
        app.result_status, app.result, app.progress, app._update_buttons = Mock(), Mock(), Mock(), Mock()
        app.last_health = None
        message = 'HTTP 503\n[native_failure] 具体错误\n请求 ID：req_visible'
        def work(action, done, failed):
            failed(message)
        app._work = work
        app._recognize()
        app.result.insert.assert_called_once_with('1.0', message)
        self.assertEqual(app.request_diagnostics, message)
        self.assertIn('req_visible', app._diagnostic_text())

    def test_audio_response_and_authentication_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'sample.wav'
            audio.write_bytes(b'synthetic')
            def respond(request):
                self.assertEqual(request.url.path, '/v1/audio/transcriptions')
                self.assertEqual(request.headers['Authorization'], 'Bearer local-test')
                self.assertIn(b'synthetic', request.read())
                self.assertIn(b'name="model"\r\n\r\ndoubao-voice', request.read())
                return httpx.Response(200, json={'text': '测试文字'})
            client = httpx.Client(transport=httpx.MockTransport(respond))
            with patch.object(httpx, 'Client', return_value=client):
                self.assertEqual(transcribe_file(audio, 18765, 'local-test', model='doubao-voice'), '测试文字')
            client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={'error': {'message': 'bad key'}})))
            with patch.object(httpx, 'Client', return_value=client):
                with self.assertRaisesRegex(RuntimeError, '密钥'):
                    transcribe_file(audio)

    def test_oversized_audio_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'large.wav'
            with audio.open('wb') as output:
                output.truncate(desktop_service.MAX_UPLOAD + 1)
            with patch.object(httpx, 'Client') as client:
                with self.assertRaisesRegex(ValueError, '25 MiB'):
                    transcribe_file(audio)
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
