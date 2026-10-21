"""Local ASR bridge with independent WeChat, WeType and Doubao routes."""
import argparse
import hmac
import json
import logging
import os
import sys
import time
import uuid
import threading
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
import uvicorn

from backend import BackendError, MAX_BYTES, WechatBackend
from app_paths import runtime_dir, resource_dir
from bridge_config import BRIDGE_ID, PROVIDERS, validate_concurrency
from bridge_pool import BackendPool
from diagnostics import exception_detail

log = logging.getLogger(__name__)

MODELS = ('wechat-voice',)
MODEL_CREATED = int(time.time())
app = FastAPI(title='ASR Bridge', version='0.3.3', docs_url=None, redoc_url=None)
backend = WechatBackend()
MAX_CONCURRENCY = 4
gate = anyio.Semaphore(MAX_CONCURRENCY)
worker_limit = anyio.CapacityLimiter(MAX_CONCURRENCY)
pools = None


@app.get('/docs', include_in_schema=False)
async def documentation():
    return FileResponse(resource_dir() / 'docs' / 'api.html', media_type='text/html')


def warning_headers(warnings):
    return {'x-asr-warnings': json.dumps(warnings, ensure_ascii=True, separators=(',', ':'))} if warnings else {}


def error_response(message, status=400, code='invalid_request', param=None, *, details=None,
                   request_id=None, warnings=None):
    kind = 'server_error' if status >= 500 else ('authentication_error' if status == 401 else 'invalid_request_error')
    error = {'message': message, 'type': kind, 'param': param, 'code': code}
    if details is not None:
        error['details'] = details
    if request_id:
        error['request_id'] = request_id
    body = {'error': error}
    if warnings:
        body['warnings'] = warnings
    headers = warning_headers(warnings)
    if request_id:
        headers['x-request-id'] = request_id
    return JSONResponse(body, status_code=status, headers=headers)


@app.middleware('http')
async def metadata_auth(request, call_next):
    request.state.request_id = f'req_{uuid.uuid4().hex}'
    key = os.environ.get('WECHAT_API_KEY')
    if key and request.url.path.startswith('/v1/'):
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + key).encode()):
            response = error_response('API Key 不正确。', 401, 'invalid_api_key')
            response.headers['x-request-id'] = request.state.request_id
            return response
    response = await call_next(request)
    response.headers['x-request-id'] = request.state.request_id
    return response


@app.exception_handler(BackendError)
async def backend_error(request, error):
    return report_error(request, error)


def report_error(request, error):
    request_id = getattr(request.state, 'request_id', None)
    selected = getattr(request.state, 'asr_backend', None)
    warnings = list(getattr(selected, 'warnings', []))
    if selected is not None:
        with selected.lock:
            selected.last_error = str(error)
            selected.last_error_detail = {'message': str(error), 'code': error.code,
                                          'details': error.details, 'request_id': request_id}
    log.error('ASR request failed: request_id=%s code=%s message=%s details=%s',
              request_id, error.code, error, error.details)
    return error_response(str(error), error.status, error.code, error.param, details=error.details,
                          request_id=request_id, warnings=warnings)


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return error_response(str(error.detail), error.status_code, 'invalid_request')


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return error_response('请求参数不正确。', 400, 'invalid_request')


@app.exception_handler(Exception)
async def unexpected_error(request, error):
    log.error('Unhandled ASR error: request_id=%s', getattr(request.state, 'request_id', None),
              exc_info=(type(error), error, error.__traceback__))
    return report_error(request, BackendError('内部调用失败：' + str(error), 500, 'internal_error',
                                             details=exception_detail(error)))


def model_object(name):
    selected = backend
    if pools is not None:
        key = next(k for k, p in PROVIDERS.items() if p['model'] == name)
        selected = pools[key].backend
    return {'id': name, 'object': 'model', 'created': MODEL_CREATED,
            'owned_by': getattr(selected, 'model_owner', 'wechat-local')}


@app.get('/v1/models')
async def models():
    return {'object': 'list', 'data': [model_object(name) for name in MODELS]}


@app.get('/v1/models/{name}')
async def model(name: str):
    if name not in MODELS:
        return error_response('不支持的模型。', 404, 'model_not_found', 'model')
    return model_object(name)


@app.get('/health')
async def health():
    # Read-only; does not inject or submit an audio request.
    if pools is not None:
        states = [pool.snapshot() for pool in pools.values()]
        return {'backend': BRIDGE_ID, 'status': 'ok' if all(s['status'] == 'ok' for s in states) else 'degraded',
                'active_requests': sum(s['active_requests'] for s in states),
                'queued_requests': sum(s['queued_requests'] for s in states),
                'max_concurrency': sum(s['max_concurrency'] for s in states),
                'busy': any(s['active_requests'] for s in states), 'pools': states,
                'formats': ['json', 'text'], 'max_audio_seconds': None,
                'last_error': next((s['last_error'] for s in states if s['last_error']), None),
                'last_error_detail': next((s['last_error_detail'] for s in states if s['last_error_detail']), None),
                'warnings': [warning for state in states for warning in state['warnings']],
                'note': '健康检查不验证客户端登录或上游服务当前可用性'}
    return {'status': 'ok' if backend.healthy else 'degraded', 'busy': gate.value < MAX_CONCURRENCY,
            'active_requests': MAX_CONCURRENCY - gate.value, 'max_concurrency': MAX_CONCURRENCY,
            'queued_requests': gate.statistics().tasks_waiting,
            'last_pid': backend.last_pid, 'last_success': backend.last_success,
            'agent_connected': backend.agent_connected,
            'agent_generation': backend.agent_generation,
            'backend': getattr(backend, 'backend_id', 'wechat-4.1.13.65'), 'formats': ['json', 'text'],
            'max_audio_seconds': None, 'last_error': backend.last_error,
            'last_error_detail': getattr(backend, 'last_error_detail', None),
            'warnings': list(getattr(backend, 'warnings', [])),
            'completion': getattr(backend, 'last_completion', None),
            'note': '健康检查不等于原生识别能力验证'}


@app.post('/v1/audio/transcriptions', openapi_extra={
    'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
        'type': 'object', 'required': ['file', 'model'], 'properties': {
            'file': {'type': 'string', 'format': 'binary'},
            'model': {'type': 'string', 'description': '必须填写 /v1/models 返回的模型名称', 'enum': [p['model'] for p in PROVIDERS.values()]},
            'response_format': {'type': 'string', 'enum': ['json', 'text'], 'default': 'json'},
            'temperature': {'type': 'number', 'enum': [0], 'default': 0},
            'stream': {'type': 'boolean', 'enum': [False], 'default': False},
        }}}}},
})
async def transcriptions(request: Request):
    if not request.headers.get('content-type', '').startswith('multipart/form-data'):
        return error_response('请使用 multipart/form-data 上传音频。')
    # Reject obviously oversized requests before multipart parsing.
    length = request.headers.get('content-length')
    if length:
        try:
            if int(length) > MAX_BYTES + 1024 * 1024:
                return error_response('上传文件过大。', 413, 'file_too_large', 'file')
        except ValueError:
            return error_response('Content-Length 不正确。')
    async with request.form(max_files=1, max_fields=20, max_part_size=MAX_BYTES) as form:
        audio = form.get('file')
        if not isinstance(audio, UploadFile):
            return error_response('缺少 file 音频文件。', param='file')
        model = form.get('model')
        if model not in MODELS:
            return error_response('model 必须为 ' + ' 或 '.join(MODELS) + '。', param='model')
        fmt = form.get('response_format', 'json')
        if fmt not in ('json', 'text'):
            return error_response('当前后端仅支持 json 和 text，无逐词时间戳或说话人分离。', param='response_format')
        allowed = {'file', 'model', 'response_format', 'stream', 'temperature'}
        for key in form:
            if key not in allowed:
                return error_response(f'当前后端尚不支持参数 {key}。', param=key)
        if str(form.get('stream', 'false')).lower() not in ('false', '0', ''):
            return error_response('当前后端不支持流式响应。', param='stream')
        if 'temperature' in form:
            try:
                if float(form['temperature']) != 0:
                    return error_response('当前后端不支持调整 temperature。', param='temperature')
            except (ValueError, TypeError):
                return error_response('temperature 不正确。', param='temperature')
        data = await audio.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return error_response('音频文件超过 25 MiB。', 413, 'file_too_large', 'file')
        selected, selected_gate, selected_workers = backend, gate, worker_limit
        if pools is not None:
            key = next(k for k, p in PROVIDERS.items() if p['model'] == model)
            pool = pools[key]
            selected, selected_gate, selected_workers = pool.backend, pool.admit(), pool.workers
        request.state.asr_backend = selected
        async with selected_gate:
            with anyio.CancelScope(shield=True):
                result = await anyio.to_thread.run_sync(selected.transcribe, data, abandon_on_cancel=False, limiter=selected_workers)
        headers = {'x-wechat-audio-seconds': str(result.duration), 'x-wechat-session-cleaned': 'true'}
        headers['x-asr-backend'] = getattr(selected, 'backend_id', 'wechat-4.1.13.65')
        warnings = list(getattr(selected, 'warnings', []))
        headers.update(warning_headers(warnings))
        if hasattr(result, 'completion'):
            headers['x-asr-completion'] = result.completion
        if fmt == 'text':
            return PlainTextResponse(result.text, headers=headers)
        body = {'text': result.text}
        if warnings:
            body['warnings'] = warnings
        return JSONResponse(body, headers=headers)


def configure_backend(name, concurrency=None):
    global backend, MODELS, MAX_CONCURRENCY, gate, pools, worker_limit
    capacities = validate_concurrency(concurrency)
    pools = None
    if name == 'all':
        from wetype_backend import WetypeBackend
        from doubao_backend import DoubaoBackend
        backends = {'wechat': WechatBackend(), 'wetype': WetypeBackend(), 'doubao': DoubaoBackend()}
        pools = {key: BackendPool(key, value, capacities[key]) for key, value in backends.items()}
        backend = backends['wechat']
        MODELS = tuple(p['model'] for p in PROVIDERS.values())
        MAX_CONCURRENCY = gate = None
    elif name == 'wetype':
        from wetype_backend import WetypeBackend
        backend = WetypeBackend()
        MODELS = ('wetype-voice',)
        MAX_CONCURRENCY = capacities['wetype']
        gate = anyio.Semaphore(MAX_CONCURRENCY)
    elif name == 'wechat':
        backend = WechatBackend()
        MODELS = ('wechat-voice',)
        MAX_CONCURRENCY = capacities['wechat']
        gate = anyio.Semaphore(MAX_CONCURRENCY)
    elif name == 'doubao':
        from doubao_backend import DoubaoBackend
        backend = DoubaoBackend()
        MODELS = ('doubao-voice',)
        MAX_CONCURRENCY = capacities['doubao']
        gate = anyio.Semaphore(MAX_CONCURRENCY)
    else:
        raise ValueError('Unknown backend')
    worker_limit = anyio.CapacityLimiter(MAX_CONCURRENCY) if MAX_CONCURRENCY is not None else None


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--backend', choices=('all', 'wechat', 'wetype', 'doubao'), default='all')
    for key, settings in PROVIDERS.items():
        parser.add_argument(f'--{key}-concurrency', type=int, default=settings['default'])
    parser.add_argument('--pid', type=int, help='Optional fixed native provider process PID')
    parser.add_argument('--shutdown-file', type=Path, help='Local graceful shutdown signal file')
    args = parser.parse_args(argv)
    try:
        configure_backend(args.backend, {key: getattr(args, key + '_concurrency') for key in PROVIDERS})
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if args.pid:
        os.environ['WETYPE_PID' if args.backend == 'wetype' else 'WECHAT_PID'] = str(args.pid)
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not os.environ.get('WECHAT_API_KEY'):
        parser.error('非本机监听地址需要先设置 WECHAT_API_KEY。')
    # Concurrent requests share one agent; separate workers cannot coordinate
    # native callback ownership or graceful shutdown in the same Weixin process.
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    if args.shutdown_file and not args.shutdown_file.resolve().is_relative_to(runtime.resolve()):
        parser.error('--shutdown-file 必须位于服务运行目录内。')
    controller = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, workers=1, log_level='info'))
    finished = threading.Event()
    def watch_shutdown():
        while not finished.wait(0.25):
            if args.shutdown_file.exists():
                controller.should_exit = True
                return
    if args.shutdown_file:
        threading.Thread(target=watch_shutdown, daemon=True).start()
    try:
        controller.run()
    finally:
        finished.set()
        targets = [pool.backend for pool in pools.values()] if pools is not None else [backend]
        for target in targets:
            while True:
                try:
                    target.close()
                    break
                except BackendError:
                    logging.exception('Native cleanup is uncertain; retaining the agent until the target exits.')
                    while target.agent_connected:
                        time.sleep(0.5)


configure_backend('all')


if __name__ == '__main__':
    main()
