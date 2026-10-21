"""Adapt independent anonymous Doubao sessions to the local audio API."""
from dataclasses import dataclass
import threading
import time

from backend import BackendError, Transcription, normalize_audio
from experiments.doubao_asr.client import ASRError, transcribe_pcm


@dataclass
class DoubaoTranscription(Transcription):
    completion: str = 'final_and_session_finished'


class DoubaoBackend:
    backend_id = 'doubao-web-anonymous'
    model_owner = 'doubao-web'
    display_name = '豆包语音'
    agent_connected = False
    agent_generation = 0
    last_pid = None

    def __init__(self):
        self.lock = threading.Condition()
        self.active = 0
        self.closing = False
        self.healthy = True
        self.last_error = self.last_success = self.last_completion = None
        self.last_error_detail = None
        self.warnings = []

    def transcribe(self, data):
        pcm, duration = normalize_audio(data)
        with self.lock:
            if self.closing:
                raise BackendError('服务正在停止。', code='server_stopping')
            self.active += 1
        try:
            result = transcribe_pcm(pcm, duration=duration)
            with self.lock:
                self.last_error = None
                self.last_error_detail = None
                self.last_success = time.time()
                self.last_completion = 'final_and_session_finished'
            return DoubaoTranscription(result.text, duration, 0, len(pcm) // 640, len(pcm))
        except ASRError as error:
            code = 'recognition_timeout' if '超时' in str(error) else 'doubao_unavailable'
            with self.lock:
                self.last_error = code
            raise BackendError(str(error), 504 if code == 'recognition_timeout' else 502, code) from error
        finally:
            with self.lock:
                self.active -= 1
                self.lock.notify_all()

    def close(self):
        with self.lock:
            self.closing = True
            while self.active:
                self.lock.wait()
