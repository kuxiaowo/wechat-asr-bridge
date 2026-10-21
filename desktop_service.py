"""Desktop service ownership, safe process launch and HTTP client helpers."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from app_paths import resource_dir, runtime_dir
from desktop_settings import saved_port, save_port, saved_api_key, save_settings
from bridge_config import BRIDGE_ID, validate_concurrency

APP_BACKEND = BRIDGE_ID
VERSION = '0.3.3'
MAX_UPLOAD = 25 * 1024 * 1024
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def health(port, timeout=2):
    # Bypass HTTP proxies: this application only talks to its loopback service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) and data.get('backend') == APP_BACKEND else None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def process_info(pid):
    command = (
        '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; '
        f'Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}" | '
        'Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'
    )
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, timeout=10, creationflags=NO_WINDOW)
    output = result.stdout.decode('utf-8-sig').strip()
    return json.loads(output) if output else None


class ServiceController:
    def __init__(self, port=8765, *, runtime=None, executable=None):
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError('端口必须介于 1 和 65535。')
        self.runtime = Path(runtime) if runtime is not None else runtime_dir()
        self.executable = Path(executable) if executable is not None else Path(sys.executable)
        self.frozen = executable is not None or getattr(sys, 'frozen', False)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.runtime / f'desktop-service-{self.port}.json'
        self.log_path = self.runtime / f'desktop-service-{self.port}.log'
        self._process = None
        self._lock = threading.Lock()

    def _metadata(self):
        try:
            data = json.loads(self.metadata_path.read_text(encoding='utf-8'))
            signal = Path(data['shutdown_file']).resolve()
            if (data['port'] != self.port or not signal.is_relative_to(self.runtime.resolve())
                    or not signal.name.startswith(f'desktop-stop-{self.port}-')
                    or not isinstance(data['pid'], int) or data['pid'] <= 0
                    or not isinstance(data.get('executable'), str)
                    or not Path(data['executable']).is_absolute()):
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def owns_service(self):
        # Metadata is only a hint. Validate the live PID before signaling a stop.
        with self._lock:
            metadata = self._metadata()
            if not metadata:
                return False
            try:
                info = process_info(metadata['pid'])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return False
            if not info or not info.get('CommandLine') or not info.get('ExecutablePath'):
                return False
            executable_matches = os.path.normcase(str(Path(info['ExecutablePath']).resolve())) == os.path.normcase(str(Path(metadata['executable']).resolve()))
            command = info['CommandLine']
            return (executable_matches and '--serve' in command and '--shutdown-file' in command
                    and metadata['shutdown_file'] in command)

    def start(self, api_key=None, concurrency=None):
        capacities = validate_concurrency(concurrency)
        with self._lock:
            current = health(self.port)
            if current:
                return current
            if port_in_use(self.port):
                raise RuntimeError(f'端口 {self.port} 已被其他程序占用，请更换端口。')
            signal = self.runtime / f'desktop-stop-{self.port}-{uuid.uuid4().hex}'
            command = [str(self.executable)]
            if not self.frozen:
                command.append(str(resource_dir() / 'desktop.py'))
            command += ['--serve', '--port', str(self.port), '--shutdown-file', str(signal)]
            command += ['--backend', 'all']
            for key, capacity in capacities.items():
                command += [f'--{key}-concurrency', str(capacity)]
            environment = os.environ.copy()
            if api_key is not None:
                if api_key:
                    environment['WECHAT_API_KEY'] = api_key
                else:
                    environment.pop('WECHAT_API_KEY', None)
            with self.log_path.open('a', encoding='utf-8') as log:
                self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                                 stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                                 env=environment)
            metadata = {'pid': self._process.pid, 'port': self.port, 'shutdown_file': str(signal),
                        'executable': str(self.executable.resolve()), 'version': VERSION}
            temporary = self.metadata_path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.metadata_path)
            for _ in range(100):
                if self._process.poll() is not None:
                    raise RuntimeError('服务启动失败，请打开运行日志查看原因。')
                current = health(self.port, timeout=0.5)
                if current:
                    return current
                time.sleep(0.2)
            # Do not kill a slow child: it may be initializing native resources.
            raise RuntimeError('服务启动时间较长，请稍后刷新状态或查看运行日志。')

    def stop(self):
        if not self.owns_service():
            raise RuntimeError('此服务由其他程序启动；请使用它原来的停止方式。')
        metadata = self._metadata()
        Path(metadata['shutdown_file']).touch()
        for _ in range(180):
            if self._process is not None and self._process.pid == metadata['pid']:
                if self._process.poll() is not None:
                    return
            elif not process_info(metadata['pid']):
                return
            time.sleep(1)
        raise RuntimeError('正在等待识别和会话清理完成；服务未被强行终止。')


class TranscriptionText(str):
    def __new__(cls, text, *, warnings=None, request_id=None):
        result = super().__new__(cls, text)
        result.warnings = warnings or []
        result.request_id = request_id
        return result


def transcribe_file(path, port=8765, api_key='wechat-local', model='wechat-voice'):
    import httpx
    path = Path(path)
    if not path.is_file():
        raise ValueError('请选择一个存在的音频文件。')
    if path.stat().st_size > MAX_UPLOAD:
        raise ValueError('音频文件超过 25 MiB，请选择较小的文件。')
    try:
        with httpx.Client(timeout=180, trust_env=False) as client, path.open('rb') as audio:
            response = client.post(f'http://127.0.0.1:{port}/v1/audio/transcriptions',
                                   data={'model': model},
                                   files={'file': (path.name, audio, 'application/octet-stream')},
                                   headers={'Authorization': 'Bearer ' + (api_key or 'wechat-local')})
    except httpx.TimeoutException as error:
        raise RuntimeError('等待识别超时；已提交的请求仍可能完成。\n' + str(error)) from error
    except httpx.RequestError as error:
        raise RuntimeError(f'无法连接本机服务 http://127.0.0.1:{port}。\n{type(error).__name__}: {error}') from error
    request_id = response.headers.get('x-request-id')
    context = f'HTTP {response.status_code} · model={model}'
    if request_id:
        context += '\n请求 ID：' + request_id
    try:
        data = response.json()
    except ValueError as error:
        raise RuntimeError(context + '\n服务返回非 JSON 响应：\n' + response.text[:8000]) from error
    if response.status_code != 200:
        fault = data.get('error') if isinstance(data, dict) else None
        if isinstance(fault, dict):
            message = f"[{fault.get('code', 'unknown')}] {fault.get('message', '服务没有提供错误说明。')}"
            if fault.get('details') is not None:
                message += '\n详细原因：\n' + (fault['details'] if isinstance(fault['details'], str)
                                              else json.dumps(fault['details'], ensure_ascii=False, indent=2))
        else:
            message = json.dumps(data, ensure_ascii=False, indent=2)
        if response.status_code == 401:
            message = '接口密钥不匹配，请填写启动该服务时设置的 API Key。\n' + message
        for warning in data.get('warnings', []) if isinstance(data, dict) else []:
            message += f"\n警告 [{warning.get('code', 'warning')}]：{warning.get('message', '')}"
        raise RuntimeError(context + '\n' + message)
    if not isinstance(data, dict) or not isinstance(data.get('text'), str):
        raise RuntimeError(context + '\n成功响应缺少 text 字符串：\n' + json.dumps(data, ensure_ascii=False, indent=2))
    return TranscriptionText(data['text'], warnings=data.get('warnings', []), request_id=request_id)
