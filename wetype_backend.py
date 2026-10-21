"""Experimental WeType backend using its initialized local business transport."""
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import time
import threading
import uuid

import frida

from app_paths import resource_dir
from backend import BackendError, Transcription, WechatBackend, normalize_audio
from wetype_protocol import ProtocolError, opus_packets, request, response
from native_compat import resolve_wetype_encoder
from diagnostics import exception_detail

REQUIRED_MODULES = ('wetype_update.exe', 'wetap_business.dll', 'ime_net.dll')
REFERENCE_SHA256 = {
    'wetype_update.exe': '580ce85a6108b5883afe6f25502fe4f0ef221148372de782f49c6ab15e63a201',
    'wetap_business.dll': '4c948dec0ff9a473b98316bd9f02031c4a67e9efcd75b516edccc4dd583b2b33',
    'ime_net.dll': '69f25af3a87662c5e8060324cf13fda91c42ce44b6a3515c5b8c04ac5f9cbb02',
}
log = logging.getLogger(__name__)


def discover_pid():
    override = os.environ.get('WETYPE_PID')
    if override:
        try:
            pid = int(override)
            if pid > 0:
                return pid
        except ValueError:
            pass
        raise BackendError('WETYPE_PID 必须为正整数。', code='wetype_unavailable')
    candidates = [p.pid for p in frida.get_local_device().enumerate_processes()
                  if p.name.lower() == 'wetype_update.exe']
    if len(candidates) != 1:
        raise BackendError('请先启动微信输入法；多个输入法进程时用 WETYPE_PID 指定。', code='wetype_unavailable')
    return candidates[0]


@dataclass
class WetypeTranscription(Transcription):
    completion: str


class WetypeBackend(WechatBackend):
    backend_id = 'wetype-3.0.0.34-experimental'
    model_owner = 'wetype-local'
    display_name = '微信输入法'

    def __init__(self):
        super().__init__()
        self.last_completion = None
        self._request_context = threading.local()
        self._encoder_addresses = None

    def verify_modules(self, modules):
        paths = {entry['name'].lower(): Path(entry['path']) for entry in modules}
        if not set(REQUIRED_MODULES) <= paths.keys():
            missing = ', '.join(sorted(set(REQUIRED_MODULES) - paths.keys()))
            raise BackendError('输入法未加载必要的语音网络组件：' + missing, code='wetype_unavailable')
        try:
            fingerprint = tuple((name, str(paths[name]), paths[name].stat().st_size,
                                 paths[name].stat().st_mtime_ns) for name in REQUIRED_MODULES)
            if self._fingerprint == fingerprint:
                return
            self.clear_warnings('unverified_wetype_version')
            for name, expected in REFERENCE_SHA256.items():
                actual = hashlib.sha256(paths[name].read_bytes()).hexdigest()
                if actual != expected:
                    self.warn('unverified_wetype_version',
                              f'输入法模块 {name} 与参考版本 3.0.0.34 不同，将继续自动定位编码函数；'
                              f'当前文件：{paths[name]}；SHA-256：{actual}。')
            try:
                self._encoder_addresses = resolve_wetype_encoder(paths['wetype_update.exe'].read_bytes())
            except ValueError as error:
                raise BackendError('无法定位输入法编码函数：' + str(error),
                                   code='wetype_encoder_unavailable', details=exception_detail(error)) from error
            self._fingerprint = fingerprint
        except OSError as error:
            raise BackendError('无法读取微信输入法模块：' + str(error), code='wetype_unavailable',
                               details=exception_detail(error)) from error

    def _ensure_agent(self, pid):
        if pid == self._unsafe_pid:
            raise BackendError('输入法连接意外断开，请退出输入法后重新启动。', code='agent_disconnected')
        if self.agent_connected and self._pid == pid:
            return self._script
        self._close_agent()
        session = frida.attach(pid)
        generation = self._generation + 1
        session.on('detached', lambda reason, crash=None: self._agent_detached(session, pid, generation, reason, crash))
        script = None
        try:
            metadata = session.create_script('rpc.exports.modules = () => Process.enumerateModules().map(m => ({name:m.name,path:m.path}));')
            metadata.load()
            try:
                self.verify_modules(metadata.exports_sync.modules())
            finally:
                metadata.unload()
            source = (resource_dir() / 'wetype_agent.js').read_text(encoding='utf-8')
            for original, resolved in self._encoder_addresses.items():
                source = source.replace(hex(original), hex(resolved))
            script = session.create_script(source)
            script.on('message', lambda message, data: self._agent_message(pid, generation, message, data))
            script.load()
        except Exception:
            if script:
                script.unload()
            session.detach()
            raise
        self._session, self._script, self._pid = session, script, pid
        self._generation += 1
        self.last_pid = pid
        log.info('WeType agent attached: pid=%s generation=%s', pid, self._generation)
        return script

    def _recognize(self, script, pcm, duration):
        encoded = bytes(script.exports_sync.encode(list(pcm)))
        packets, frames = opus_packets(encoded)
        if frames != len(pcm) // 640:
            raise ProtocolError('编码后的帧数与输入音频不一致。')
        owner = getattr(self._request_context, 'owner', None)
        voice_id = owner or 'wt-api-' + uuid.uuid4().hex
        total_bytes = 0
        deadline = time.monotonic() + max(30, duration * 1.5 + 15)

        def submit(audio, sequence, end):
            if time.monotonic() > deadline:
                raise BackendError('输入法未在时限内完成识别。', 504, 'recognition_timeout')
            payload = list(request(voice_id, audio, sequence, total_bytes, end=end))
            reply = script.exports_sync.request(payload, owner) if owner else script.exports_sync.request(payload)
            if reply['code'] != 0:
                raise BackendError(f"输入法语音网络请求失败：原生错误码 {reply['code']}，"
                                   f"任务 ID {reply.get('taskId', 'unknown')}。",
                                   502, 'wetype_transport_error', details=reply.get('error'))
            return response(bytes(reply['data']), voice_id)

        for sequence, packet in enumerate(packets, 1):
            total_bytes += len(packet)
            submit(packet, sequence, False)
        end_sequence = len(packets) + 1
        current = submit(b'', end_sequence, True)
        while True:
            if current.end_flag:
                return current.text, frames, len(encoded), 'server_end_flag'
            now = time.monotonic()
            if now >= deadline:
                raise BackendError('输入法未确认识别结束，未交付可能不完整的文字。', 504, 'recognition_timeout')
            time.sleep(max(0.1, min(current.interval_ms / 1000, 1.0)))
            current = submit(b'', 0, True)

    def transcribe(self, data):
        owner = 'wt-api-' + uuid.uuid4().hex
        script = session = None
        registered = False
        failure = None
        cleanup = {'cleaned': True}
        try:
            pcm, duration = normalize_audio(data)
            with self.lock:
                pid = self._pid if self.agent_connected and not os.environ.get('WETYPE_PID') else discover_pid()
                while self._active_requests and (self._pid != pid or self._session.is_detached):
                    if self._closing:
                        raise BackendError('服务正在停止。', code='server_stopping')
                    self.lock.wait()
                if self._closing:
                    raise BackendError('服务正在停止。', code='server_stopping')
                if self._unsafe_pid is not None and pid != self._unsafe_pid:
                    self._unsafe_pid = None
                    self.healthy = True
                    self.last_error = None
                if pid == self._unsafe_pid:
                    raise BackendError('输入法连接意外断开，请退出输入法后重新启动。', code='agent_disconnected')
                if self._session is not None and self._session.is_detached:
                    self._close_agent()
                    self.healthy = True
                if not self.healthy:
                    raise BackendError('输入法原生回调尚未清理，已暂停后续调用。', code='cleanup_failed')
                script = self._ensure_agent(pid)
                session = self._session
                self._active_requests += 1
                registered = True
            self._request_context.owner = owner
            text, frames, encoded_bytes, completion = self._recognize(script, pcm, duration)
        except Exception as error:
            failure = error
        finally:
            if registered:
                if not session.is_detached:
                    try:
                        cleanup = script.exports_sync.finalize(owner)
                    except Exception as error:
                        cleanup = {'cleaned': session.is_detached, 'error': exception_detail(error)}
                with self.lock:
                    if not cleanup.get('cleaned'):
                        self.healthy = False
                        self.last_error = 'cleanup_failed'
                    self._active_requests -= 1
                    if self._active_requests == 0 and session.is_detached:
                        self._close_agent()
                        if self._unsafe_pid != pid:
                            self.healthy = True
                    self.lock.notify_all()
            self._request_context.owner = None
        if not cleanup.get('cleaned'):
            raise BackendError('输入法原生回调清理未完成，已暂停后续调用。', 503, 'cleanup_failed',
                               details={'cleanup': cleanup, 'failure': exception_detail(failure) if failure else None})
        if failure:
            with self.lock:
                if self.healthy:
                    self.last_error = failure.code if isinstance(failure, BackendError) else type(failure).__name__
            if isinstance(failure, BackendError):
                raise failure
            if isinstance(failure, ProtocolError):
                raise BackendError(str(failure), 502, 'wetype_protocol_error') from failure
            raise BackendError('输入法语音调用失败：' + str(failure), code='wetype_unavailable',
                               details=exception_detail(failure)) from failure
        with self.lock:
            if self.healthy:
                self.last_error = None
                self.last_error_detail = None
            self.last_success = time.time()
            self.last_completion = completion
        log.info('WeType recognition completed: seconds=%.3f completion=%s', duration, completion)
        return WetypeTranscription(text, duration, pid, frames, encoded_bytes, completion)
